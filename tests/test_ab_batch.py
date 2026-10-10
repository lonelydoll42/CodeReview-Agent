from __future__ import annotations

import json
import hashlib
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.run_ab_batch import (
    BENCHMARK_ID,
    PLAN_STATE,
    TRACE_NORMALIZER,
    build_runtime_evidence,
    build_slot_plan,
    execute_slot,
    immutable_plan_sha256,
    normalized_supervisor_events,
    preseal_plan,
    validate_plan,
    sha256_file,
    validate_runtime_evidence,
    _codex_config_overrides,
    _freeze_runtime_config,
    _preflight_codex_config,
    write_runtime_config,
)


class AbBatchPlanTests(unittest.TestCase):
    def test_plan_has_fixed_balanced_slots_and_pending_labels(self) -> None:
        index = json.loads((Path(__file__).parents[1] / "eval/data/real-cases.json").read_bytes())
        plan = build_slot_plan(index)

        self.assertEqual(plan["benchmark_id"], BENCHMARK_ID)
        self.assertFalse(plan["fresh_blind_test"])
        self.assertEqual(plan["plan_state"], PLAN_STATE)
        self.assertFalse(plan["execution_authorized"])
        self.assertEqual(len(plan["slots"]), 12)
        self.assertEqual(
            {(slot["case_id"], slot["condition"], slot["replicate"]) for slot in plan["slots"]},
            {
                (case_id, condition, replicate)
                for case_id in ("case0002", "case0003", "case0006")
                for condition in ("A", "B")
                for replicate in (1, 2)
            },
        )
        self.assertTrue(all(slot["attempt"] == 1 for slot in plan["slots"]))
        self.assertTrue(all(slot["attempt_kind"] == "independent_replicate" for slot in plan["slots"]))
        self.assertTrue(all(case["annotation_status"] == "pending" for case in plan["cases"].values()))

    def test_missing_selected_case_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing selected case metadata"):
            build_slot_plan({"cases": []})

    def test_execution_requires_explicit_plan_go(self) -> None:
        index = json.loads((Path(__file__).parents[1] / "eval/data/real-cases.json").read_bytes())
        plan = build_slot_plan(index)

        with self.assertRaisesRegex(ValueError, "requires plan_state=go"):
            validate_plan(plan, require_go=True)

        authorized = dict(plan, plan_state="go", execution_authorized=True)
        validate_plan(authorized, require_go=True)

    def test_preseal_binds_same_inputs_and_excludes_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            (source / ".git").mkdir(parents=True)
            (source / ".git/config").write_text("must not be copied", encoding="utf-8")
            (source / "src.py").write_text("print('fixture')\n", encoding="utf-8")
            skill = root / "skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("Pinned test-only Skill.\n", encoding="utf-8")
            cases = []
            for case_id in ("case0002", "case0003", "case0006"):
                reviewer = root / f"{case_id}.json"
                reviewer.write_text(json.dumps({
                    "base": f"{case_id}-base",
                    "head": f"{case_id}-head",
                    "scope_paths": ["src.py"],
                }), encoding="utf-8")
                cases.append({
                    "case_id": case_id,
                    "base": f"{case_id}-base",
                    "head": f"{case_id}-head",
                    "snapshot": {"base_tree": "b", "head_tree": "h", "scoped_diff_sha256": "d"},
                    "scope_paths": ["src.py"],
                    "reviewer_input": str(reviewer),
                    "repositories": {"same_host_direct": str(source)},
                })
            plan = build_slot_plan({"cases": cases})
            plan_bytes = (json.dumps(plan, sort_keys=True) + "\n").encode()
            runtime_config = root / "runtime-config.json"
            config = {
                "schema_version": 1,
                "codex_executable": sys.executable,
                "strace_executable": "/usr/bin/strace",
                "model": "gpt-6.1-sol",
                "provider_definition": {
                    "name": "custom",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "wire_api": "responses",
                    "requires_openai_auth": True,
                },
                "reasoning_effort": "high",
                "timeout_seconds": 30,
            }
            runtime_config.write_text(json.dumps(config) + "\n", encoding="utf-8")

            with mock.patch("eval.run_ab_batch._preflight_codex_config"):
                prepared = preseal_plan(plan, plan_bytes, root / "runs", runtime_config, skill)

            self.assertEqual(len(prepared), 12)
            case2_a = prepared[0]
            case3_b = prepared[1]
            case2_b = prepared[4]
            self.assertFalse((case2_a / "input/repository/.git").exists())
            self.assertFalse((case3_b / "input/repository/.git").exists())
            self.assertEqual(
                (case2_a / "input/repository/src.py").read_bytes(),
                (case2_b / "input/repository/src.py").read_bytes(),
            )
            self.assertEqual(
                (case2_a / "input/reviewer-input.json").read_bytes(),
                (case2_b / "input/reviewer-input.json").read_bytes(),
            )
            self.assertEqual(
                (case2_a / "input/common-prompt.md").read_bytes(),
                (case2_b / "input/common-prompt.md").read_bytes(),
            )
            self.assertNotEqual(
                (case2_a / "input/prompt.txt").read_bytes(),
                (case2_b / "input/prompt.txt").read_bytes(),
            )
            manifest = json.loads((case3_b / "artifacts/source-bundle-manifest.json").read_bytes())
            self.assertEqual(manifest["skill_resource"]["member_sha256"], hashlib.sha256(b"Pinned test-only Skill.\n").hexdigest())
            self.assertEqual(manifest["skill_resource"]["zip_path"], "skill-bundle.zip")
            self.assertTrue((case3_b / manifest["skill_resource"]["zip_path"]).is_file())
            self.assertEqual(manifest["immutable_plan_sha256"], immutable_plan_sha256(plan))
            self.assertEqual(stat.S_IMODE(case3_b.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((case3_b / "artifacts/source-bundle-manifest.json").stat().st_mode),
                0o600,
            )


class RuntimeConfigTests(unittest.TestCase):
    def _codex_toml(self, root: Path, *, effort: str | None = "max") -> Path:
        effort_line = f'model_reasoning_effort = "{effort}"\n' if effort is not None else ""
        path = root / "config.toml"
        path.write_text(
            "model = \"gpt-6.1-sol\"\n"
            "model_provider = \"custom\"\n"
            + effort_line
            + "openai_base_url = \"http://ignored.example/v1\"\n"
            + "[model_providers.custom]\n"
            + "name = \"custom\"\n"
            + "base_url = \"http://127.0.0.1:8080/v1\"\n"
            + "wire_api = \"responses\"\n"
            + "requires_openai_auth = true\n"
            + "api_key_env = \"MUST_NOT_BE_COPIED\"\n"
            + "env_http_headers = { X-Test = \"MUST_NOT_BE_COPIED\" }\n",
            encoding="utf-8",
        )
        return path

    def test_freeze_reads_only_allowlisted_provider_and_active_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._codex_toml(Path(temp_dir))
            config = _freeze_runtime_config(
                config_path,
                codex_executable=Path(sys.executable),
                strace_executable=Path("/usr/bin/strace"),
            )

        self.assertEqual(config["provider_definition"], {
            "name": "custom",
            "base_url": "http://127.0.0.1:8080/v1",
            "wire_api": "responses",
            "requires_openai_auth": True,
        })
        self.assertEqual(config["reasoning_effort"], "max")
        self.assertNotIn("api_key_env", json.dumps(config))
        self.assertNotIn("MUST_NOT_BE_COPIED", json.dumps(config))
        self.assertNotIn("ignored.example", json.dumps(config))

    def test_missing_effort_requires_an_explicit_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._codex_toml(Path(temp_dir), effort=None)
            with self.assertRaisesRegex(ValueError, "reasoning_effort is unsupported"):
                _freeze_runtime_config(
                    config_path,
                    codex_executable=Path(sys.executable),
                    strace_executable=Path("/usr/bin/strace"),
                )
            config = _freeze_runtime_config(
                config_path,
                codex_executable=Path(sys.executable),
                strace_executable=Path("/usr/bin/strace"),
                reasoning_effort="high",
            )
        self.assertEqual(config["reasoning_effort"], "high")

    def test_overrides_are_explicit_and_preflight_is_non_model(self) -> None:
        config = {
            "codex_executable": sys.executable,
            "model": "gpt-6.1-sol",
            "provider_definition": {
                "name": "custom",
                "base_url": "http://127.0.0.1:8080/v1",
                "wire_api": "responses",
                "requires_openai_auth": True,
            },
            "reasoning_effort": "max",
        }
        overrides = _codex_config_overrides(config)
        self.assertIn('model_providers.custom.base_url="http://127.0.0.1:8080/v1"', overrides)
        self.assertIn('model_providers.custom.wire_api="responses"', overrides)
        self.assertIn("model_providers.custom.requires_openai_auth=true", overrides)
        self.assertIn('model_reasoning_effort="max"', overrides)

        completed = mock.Mock(returncode=0)
        with mock.patch("eval.run_ab_batch.subprocess.run", return_value=completed) as run:
            _preflight_codex_config(config)

        self.assertEqual(run.call_count, 2)
        features_argv = run.call_args_list[0].args[0]
        execution_argv = run.call_args_list[1].args[0]
        self.assertEqual(features_argv[-2:], ["features", "list"])
        self.assertEqual(execution_argv[1:4], ["exec", "--ignore-user-config", "--ignore-rules"])
        self.assertIn("--version", execution_argv)
        self.assertEqual(
            [item for item in features_argv if item == "-c"],
            [item for item in execution_argv if item == "-c"],
        )
        self.assertEqual(run.call_args_list[0].kwargs["stdin"], -3)

    def test_runtime_config_file_is_private_and_does_not_copy_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self._codex_toml(root)
            output_path = root / "benchmark" / "runtime-config.json"
            with mock.patch("eval.run_ab_batch._preflight_codex_config"):
                config = write_runtime_config(config_path, output_path)

            self.assertEqual(stat.S_IMODE(output_path.stat().st_mode), 0o600)
            encoded = output_path.read_text(encoding="utf-8")
            self.assertNotIn("MUST_NOT_BE_COPIED", encoded)
            self.assertNotIn("ignored.example", encoded)
            self.assertEqual(json.loads(encoded), config)


def _private_write(path: Path, data: bytes, *, directory_mode: int = 0o700) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=directory_mode)
    path.write_bytes(data)
    path.chmod(0o600)


def _fake_cli_slot(tmp_path: Path) -> tuple[Path, dict]:
    run_root = tmp_path / "case0002" / "same_host_direct" / "repeat01"
    for relative in ("input/protocol", "artifacts", "runtime"):
        path = run_root / relative
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)
    run_root.chmod(0o700)
    (run_root / "input").chmod(0o700)

    reviewer = b'{"base":"abc","head":"def","scope_paths":["src.py"]}\n'
    prompt = b"synthetic prompt\n"
    schema = b'{"type":"object"}\n'
    config_bytes = b'{"schema_version":1,"codex_executable":"/fake/codex","strace_executable":"/usr/bin/strace","model":"gpt-6.1-sol","provider_definition":{"name":"custom","base_url":"http://127.0.0.1:8080/v1","wire_api":"responses","requires_openai_auth":true},"reasoning_effort":"high","timeout_seconds":30}\n'
    binding_payloads = {
        "input/reviewer-input.json": reviewer,
        "input/common-prompt.md": prompt,
        "input/protocol/host-output.schema.json": schema,
        "input/runtime-config.json": config_bytes,
    }
    for relative, payload in binding_payloads.items():
        _private_write(run_root / relative, payload)
    _private_write(run_root / "input/prompt.txt", prompt)
    normalizer = run_root / "artifacts/runtime_trace.py"
    _private_write(normalizer, TRACE_NORMALIZER.read_bytes())
    manifest = {
        "schema_version": 1,
        "files": [
            {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}
            for relative, payload in binding_payloads.items()
        ],
    }
    _private_write(
        run_root / "artifacts/source-bundle-manifest.json",
        (json.dumps(manifest, sort_keys=True) + "\n").encode(),
    )
    metadata = {
        "schema_version": 1,
        "benchmark_id": BENCHMARK_ID,
        "case_id": "case0002",
        "condition": "A",
        "run_order": 1,
        "replicate": 1,
        "attempt": 1,
        "attempt_kind": "independent_replicate",
        "immutable_plan_sha256": "a" * 64,
    }
    _private_write(run_root / "run-metadata.json", (json.dumps(metadata) + "\n").encode())

    executable = tmp_path / "fake-bin" / "codex"
    script = """#!/usr/bin/python3
import json
import sys
from pathlib import Path
args = sys.argv[1:]
output_path = Path(args[args.index("--output-last-message") + 1])
output_path.write_text(json.dumps({
    "schema_version": 1,
    "case_id": "case0002",
    "condition": "A",
    "run_status": "completed",
    "review_status": "completed",
    "scope": {"base": "abc", "head": "def", "paths": ["src.py"]},
    "read_paths": [],
    "uncovered_paths": [],
    "context_omissions": [],
    "failure_reason": None,
    "findings": [],
    "host": {"name": "fake", "version": "1", "model": None, "configuration_fingerprint": None},
    "environment": {"os": "test", "architecture": "test"},
    "model_usage": {"input_tokens": None, "output_tokens": None, "cost": None, "currency": None},
    "manual_interventions": None,
    "duration_seconds": 0,
    "started_at": None,
    "finished_at": None,
    "tool_runs": []
}))
print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
"""
    executable.parent.mkdir(mode=0o700)
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o700)
    config = {
        "schema_version": 1,
        "codex_executable": str(executable),
        "strace_executable": "/usr/bin/strace",
        "model": "gpt-6.1-sol",
        "provider_definition": {
            "name": "custom",
            "base_url": "http://127.0.0.1:8080/v1",
            "wire_api": "responses",
            "requires_openai_auth": True,
        },
        "reasoning_effort": "high",
        "timeout_seconds": 30,
    }
    return run_root, config


class BatchExecutionAdapterTests(unittest.TestCase):
    def test_tool_call_id_comes_from_unique_matching_cli_command(self) -> None:
        host_path = "/tmp/pinned/skill/SKILL.md"
        raw_trace = (
            f'424 execve("/usr/bin/cat", ["cat", "--", "{host_path}"], 0x0, 0x0) = 0\n'
            f'424 openat(AT_FDCWD, "{host_path}", O_RDONLY) = 3<{host_path}>\n'
            "424 read(0x3, 0x7fff0000, 0x10) = 0x10\n"
            "424 +++ exited with 0 +++\n"
        ).encode()
        cli = (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}) + "\n"
            + json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "tool-7",
                    "type": "command_execution",
                    "command": f"cat -- {host_path}",
                    "aggregated_output": "pinned bytes",
                },
            }) + "\n"
        ).encode()
        events, parsed = normalized_supervisor_events(raw_trace, cli, TRACE_NORMALIZER)

        self.assertTrue(parsed["complete"])
        self.assertEqual([event.get("call_id") for event in events[1:]], ["tool-7"] * 4)
        self.assertEqual(events[2]["event"], "open")
        self.assertEqual(events[3]["path"], host_path)

    @unittest.skipUnless(Path("/usr/bin/strace").is_file(), "strace is required for the adapter trace test")
    def test_fake_cli_is_wrapped_and_all_outputs_are_sealed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root, config = _fake_cli_slot(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, "GO authorization gate"):
                execute_slot(run_root, config)
            result = execute_slot(run_root, config, go_authorized=True)

            self.assertEqual(result["run_status"], "completed")
            self.assertTrue(result["host_output_json_object"])
            self.assertEqual(result["runtime_evidence"]["status"], "verified")
            self.assertEqual(result["runtime_evidence"]["supervisor_exit_code"], 0)
            self.assertEqual(result["runtime_evidence"]["common_binding_status"], "verified")
            argv = json.loads((run_root / "runtime/adapter-start.json").read_bytes())["argv"]
            self.assertIn("--ignore-user-config", argv)
            self.assertIn("--ignore-rules", argv)
            self.assertIn('model_providers.custom.name="custom"', argv)
            self.assertIn('model_providers.custom.base_url="http://127.0.0.1:8080/v1"', argv)
            self.assertIn('model_providers.custom.wire_api="responses"', argv)
            self.assertIn("model_providers.custom.requires_openai_auth=true", argv)
            self.assertIn('model_reasoning_effort="high"', argv)
            for relative in (
                "runtime/codex-events.jsonl",
                "runtime/supervisor-events.jsonl",
                "runtime/strace.log",
                "runtime/strace-stderr.log",
                "runtime/execution.json",
                "runtime-evidence.json",
                "host-output.json",
                "artifacts/raw-model-output.txt",
            ):
                self.assertEqual(stat.S_IMODE((run_root / relative).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(run_root.stat().st_mode), 0o700)
            self.assertEqual(
                json.loads((run_root / "runtime/codex-events.jsonl").read_text().splitlines()[0])["thread_id"],
                "fake-thread",
            )


class RuntimeEvidenceTests(unittest.TestCase):
    def test_sidecar_hashes_raw_trace_and_binds_normalizer_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "runtime").mkdir()
            (root / "artifacts").mkdir()
            (root / "runtime/codex-events.jsonl").write_bytes(b"{}\n")
            (root / "runtime/supervisor-events.jsonl").write_bytes(b"{}\n")
            (root / "runtime/strace.log").write_bytes(b"trace\n")
            (root / "artifacts/source-bundle-manifest.json").write_bytes(b"{}\n")
            (root / "artifacts/reviewer-input.json").write_bytes(b"{}\n")
            (root / "artifacts/common-prompt.md").write_bytes(b"Review.\n")
            (root / "artifacts/output-schema.json").write_bytes(b"{}\n")
            (root / "artifacts/runtime-config.json").write_bytes(b"{}\n")
            source = root / "normalizer.py"
            source.write_text("normalizer source\n", encoding="utf-8")

            evidence = build_runtime_evidence(
                root,
                root / "runtime/codex-events.jsonl",
                root / "runtime/supervisor-events.jsonl",
                root / "runtime/strace.log",
                root / "artifacts/source-bundle-manifest.json",
                source,
                common_bindings={
                    "review_manifest": root / "artifacts/reviewer-input.json",
                    "common_prompt": root / "artifacts/common-prompt.md",
                    "output_schema": root / "artifacts/output-schema.json",
                    "runtime_config": root / "artifacts/runtime-config.json",
                },
            )
            validate_runtime_evidence(evidence)

            self.assertEqual(
                evidence["supervisor_events"]["source_raw_trace"], evidence["raw_trace"]
            )
            self.assertEqual(
                evidence["supervisor_events"]["normalizer"]["source"]["sha256"],
                sha256_file(source),
            )
            self.assertEqual(
                set(evidence["common_bindings"]),
                {"review_manifest", "common_prompt", "output_schema", "runtime_config"},
            )

    def test_sidecar_rejects_mismatched_raw_trace_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "runtime").mkdir()
            (root / "artifacts").mkdir()
            for name in ("codex-events.jsonl", "supervisor-events.jsonl", "strace.log"):
                (root / "runtime" / name).write_bytes(b"x")
            manifest = root / "artifacts/source-bundle-manifest.json"
            manifest.write_bytes(b"{}")
            source = root / "normalizer.py"
            source.write_bytes(b"pass")
            evidence = build_runtime_evidence(
                root,
                root / "runtime/codex-events.jsonl",
                root / "runtime/supervisor-events.jsonl",
                root / "runtime/strace.log",
                manifest,
                source,
            )
            evidence["supervisor_events"]["source_raw_trace"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "bind the raw trace reference"):
                validate_runtime_evidence(evidence)


if __name__ == "__main__":
    unittest.main()
