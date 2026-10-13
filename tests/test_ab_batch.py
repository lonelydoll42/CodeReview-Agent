from __future__ import annotations

import json
import hashlib
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from eval import run_ab_batch as batch
from eval.run_ab_batch import (
    BENCHMARK_ID,
    PLAN_STATE,
    SANDBOX_SKILL_PATH,
    TRACE_NORMALIZER,
    build_runtime_evidence,
    build_slot_plan,
    execute_plan,
    execute_slot,
    immutable_plan_sha256,
    normalized_supervisor_events,
    preseal_plan,
    validate_plan,
    sha256_file,
    validate_runtime_evidence,
    _codex_config_overrides,
    _find_bundled_bwrap,
    _freeze_runtime_config,
    _preflight_codex_config,
    write_runtime_config,
)
from eval.runtime_trace import STRACE_STRING_LIMIT, parse_raw_trace


def _git_case_repository(repository: Path) -> dict[str, str]:
    repository.mkdir(parents=True)
    (repository / "src.py").write_text("value = 'base'\n", encoding="utf-8")
    commands = (
        ["git", "init", "--quiet", str(repository)],
        ["git", "-C", str(repository), "config", "user.name", "Test Fixture"],
        ["git", "-C", str(repository), "config", "user.email", "fixture@example.invalid"],
        ["git", "-C", str(repository), "add", "src.py"],
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "base"],
    )
    for command in commands:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    base = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True, stdout=subprocess.PIPE,
    ).stdout.decode().strip()
    (repository / "src.py").write_text("value = 'head'\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-am", "head"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True, stdout=subprocess.PIPE,
    ).stdout.decode().strip()
    base_tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", f"{base}^{{tree}}"],
        check=True, stdout=subprocess.PIPE,
    ).stdout.decode().strip()
    head_tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", f"{head}^{{tree}}"],
        check=True, stdout=subprocess.PIPE,
    ).stdout.decode().strip()
    return {"base": base, "head": head, "base_tree": base_tree, "head_tree": head_tree}


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

    def test_plan_accepts_bounded_ascii_ids_and_rejects_invalid_ids(self) -> None:
        index = json.loads((Path(__file__).parents[1] / "eval/data/real-cases.json").read_bytes())
        plan = build_slot_plan(index)
        self.assertEqual(plan["benchmark_id"], BENCHMARK_ID)

        for benchmark_id in ("2026-10-12-ab-workflow-validation-v3", "A" * 128, "v3._-batch"):
            with self.subTest(benchmark_id=benchmark_id):
                validate_plan({**plan, "benchmark_id": benchmark_id})

        for benchmark_id in (None, "", "_starts-with-underscore", "../escape", "has space", "非ASCII", "A" * 129):
            with self.subTest(benchmark_id=benchmark_id):
                with self.assertRaisesRegex(ValueError, "benchmark_id must be a non-empty ASCII identifier"):
                    validate_plan({**plan, "benchmark_id": benchmark_id})

        missing = dict(plan)
        del missing["benchmark_id"]
        with self.assertRaisesRegex(ValueError, "benchmark_id must be a non-empty ASCII identifier"):
            validate_plan(missing)

    def test_host_output_schema_constants_and_strict_objects_are_consistent(self) -> None:
        schema = json.loads((Path(__file__).parents[1] / "eval/schemas/host-output.schema.json").read_bytes())
        batch.validate_host_output_schema(schema)
        object_nodes: list[tuple[str, dict]] = []

        def walk(value: object, path: str = "$") -> None:
            if isinstance(value, dict):
                if "const" in value:
                    constant = value["const"]
                    if isinstance(constant, bool):
                        expected_type = "boolean"
                    elif isinstance(constant, int):
                        expected_type = "integer"
                    elif isinstance(constant, float):
                        expected_type = "number"
                    elif isinstance(constant, str):
                        expected_type = "string"
                    else:
                        self.fail(f"unsupported const value at {path}: {constant!r}")
                    self.assertEqual(value.get("type"), expected_type, path)
                node_type = value.get("type")
                if node_type == "object" or (
                    isinstance(node_type, list) and "object" in node_type
                ):
                    object_nodes.append((path, value))
                for key, child in value.items():
                    walk(child, f"{path}/{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{path}/{index}")

        walk(schema)
        opaque_objects = []
        for path, node in object_nodes:
            properties = node.get("properties")
            if properties is None:
                opaque_objects.append(path)
                continue
            self.assertIs(node.get("additionalProperties"), False, path)
            self.assertEqual(set(node.get("required", [])), set(properties), path)

        self.assertEqual(opaque_objects, [])
        tool_run = schema["$defs"]["tool_run"]
        self.assertEqual(
            tool_run["properties"]["status"]["enum"],
            ["missing", "failed", "completed"],
        )
        self.assertEqual(tool_run["properties"]["error"]["type"], ["string", "null"])
        self.assertEqual(tool_run["properties"]["note"]["type"], ["string", "null"])
        tool_finding = schema["$defs"]["tool_finding"]
        self.assertEqual(tool_finding["properties"]["metadata_json"]["type"], "string")
        self.assertNotIn("metadata", tool_finding["properties"])

    def test_host_output_schema_rejects_open_objects_at_nested_schema_paths(self) -> None:
        schema_path = Path(__file__).parents[1] / "eval/schemas/host-output.schema.json"
        schema = json.loads(schema_path.read_bytes())
        legacy_schema = json.loads(json.dumps(schema))
        legacy_schema["properties"]["tool_runs"]["items"] = {"type": "object"}
        with self.assertRaisesRegex(ValueError, r"\$/properties/tool_runs/items.*additionalProperties"):
            batch.validate_host_output_schema(legacy_schema)

        nested_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["entries"],
            "properties": {
                "entries": {
                    "type": "array",
                    "items": {"anyOf": [{"type": "object"}, {"type": "null"}]},
                }
            },
            "$defs": {"open": {"type": "object"}},
        }
        with self.assertRaises(ValueError) as raised:
            batch.validate_host_output_schema(nested_schema)
        self.assertRegex(str(raised.exception), r"\$/properties/entries/items/anyOf/0.*additionalProperties")
        self.assertRegex(str(raised.exception), r"\$.*\$defs/open.*additionalProperties")

    def test_host_output_schema_rejects_unconstrained_nested_items_and_cycles(self) -> None:
        schema_path = Path(__file__).parents[1] / "eval/schemas/host-output.schema.json"
        schema = json.loads(schema_path.read_bytes())
        open_object = {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {},
        }

        for items, message in (
            (True, "boolean schemas are unsupported"),
            (False, "boolean schemas are unsupported"),
            ({}, "schema must constrain instances"),
            ({"anyOf": [open_object, True]}, "boolean schemas are unsupported"),
        ):
            with self.subTest(items=items):
                candidate = json.loads(json.dumps(schema))
                candidate["properties"]["tool_runs"]["items"] = items
                with self.assertRaisesRegex(ValueError, message):
                    batch.validate_host_output_schema(candidate)

        recursive = json.loads(json.dumps(schema))
        recursive["properties"]["tool_runs"]["items"] = {"$ref": "#/$defs/recursive"}
        recursive["$defs"]["recursive"] = {
            "anyOf": [
                {"$ref": "#/$defs/recursive"},
                open_object,
            ]
        }
        with self.assertRaisesRegex(ValueError, "recursive references are unsupported"):
            batch.validate_host_output_schema(recursive)

    def test_host_output_schema_requires_required_to_match_properties(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {"value": {"type": "string"}},
        }
        with self.assertRaisesRegex(ValueError, r"\$/required.*every declared property"):
            batch.validate_host_output_schema(schema)

    def test_invalid_schema_stops_prepare_and_execute_before_codex_preflight(self) -> None:
        index = json.loads((Path(__file__).parents[1] / "eval/data/real-cases.json").read_bytes())
        plan = build_slot_plan(index)
        plan_bytes = (json.dumps(plan, sort_keys=True) + "\n").encode()
        authorized_plan = dict(plan, plan_state="go", execution_authorized=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid_schema = root / "open-schema.json"
            schema = json.loads((Path(__file__).parents[1] / "eval/schemas/host-output.schema.json").read_bytes())
            schema["properties"]["tool_runs"]["items"] = {"type": "object"}
            invalid_schema.write_text(json.dumps(schema), encoding="utf-8")
            with (
                mock.patch.object(batch, "HOST_SCHEMA", invalid_schema),
                mock.patch("eval.run_ab_batch._preflight_codex_config") as preflight,
            ):
                with self.assertRaisesRegex(ValueError, "strict schema contract"):
                    preseal_plan(plan, plan_bytes, root / "prepared", root / "missing-config.json")
                with self.assertRaisesRegex(ValueError, "strict schema contract"):
                    execute_plan(authorized_plan, root / "prepared", root / "missing-config.json")
            preflight.assert_not_called()
            self.assertFalse((root / "prepared").exists())

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

    def test_scoped_patch_requires_available_base_and_head_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repository"
            git_case = _git_case_repository(repository)
            case = {
                "repository_source": str(repository),
                "base": git_case["base"],
                "head": git_case["head"],
                "snapshot": {
                    "base_tree": git_case["base_tree"],
                    "head_tree": git_case["head_tree"],
                },
                "scope_paths": ["src.py"],
            }
            patch_bytes, record = batch._scoped_patch(case)
            self.assertEqual(hashlib.sha256(patch_bytes).hexdigest(), record["sha256"])

            missing_base = dict(case, base="0" * 40)
            with self.assertRaisesRegex(ValueError, "Git could not verify"):
                batch._scoped_patch(missing_base)

    def test_preseal_binds_same_inputs_and_excludes_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            git_case = _git_case_repository(source)
            snapshot = {
                "base_tree": git_case["base_tree"],
                "head_tree": git_case["head_tree"],
                "scoped_diff_sha256": "legacy-full-diff-hash",
            }
            skill = root / "skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("Pinned test-only Skill.\n", encoding="utf-8")
            (skill / "scripts").mkdir()
            projector = Path(__file__).parents[1] / "skills/review-changes/scripts/project_tool_runs.py"
            (skill / "scripts/project_tool_runs.py").write_bytes(projector.read_bytes())
            cases = []
            for case_id in ("case0002", "case0003", "case0006"):
                reviewer = root / f"{case_id}.json"
                reviewer.write_text(json.dumps({
                    "base": git_case["base"],
                    "head": git_case["head"],
                    "scope_paths": ["src.py"],
                    "snapshot": snapshot,
                }), encoding="utf-8")
                cases.append({
                    "case_id": case_id,
                    "base": git_case["base"],
                    "head": git_case["head"],
                    "snapshot": snapshot,
                    "scope_paths": ["src.py"],
                    "reviewer_input": str(reviewer),
                    "repositories": {"same_host_direct": str(source)},
                })
            case_index_path = root / "case-index.json"
            case_index_path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
            plan = build_slot_plan({"cases": cases})
            plan["benchmark_id"] = "2026-10-12-ab-workflow-validation-v3"
            batch._validate_plan_inputs(plan, case_index_path)
            plan_bytes = (json.dumps(plan, sort_keys=True) + "\n").encode()
            runtime_config = root / "runtime-config.json"
            config = {
                "schema_version": 3,
                "codex_executable": "/usr/bin/codex",
                "strace_executable": "/usr/bin/strace",
                "bwrap_executable": str(_find_bundled_bwrap(Path("/usr/bin/codex"))),
                "model": "gpt-6.1-sol",
                "provider_definition": {
                    "name": "custom",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "wire_api": "responses",
                    "requires_openai_auth": True,
                },
                "reasoning_effort": "high",
                "timeout_seconds": 30,
                "sandbox_identity": {
                    "environment": {"TZ": "UTC0"},
                    "effective_timezone": "UTC",
                },
            }
            runtime_config.write_text(json.dumps(config) + "\n", encoding="utf-8")

            with mock.patch("eval.run_ab_batch._preflight_codex_config"):
                prepared = preseal_plan(
                    plan,
                    plan_bytes,
                    root / "runs",
                    runtime_config,
                    skill,
                    repository_git_cases={"case0006"},
                )

            self.assertEqual(len(prepared), 12)
            case2_a = prepared[0]
            case3_b = prepared[1]
            case2_b = prepared[4]
            by_identity = {
                (slot["case_id"], slot["condition"]): run_root
                for slot, run_root in zip(plan["slots"], prepared, strict=True)
            }
            case6_a = by_identity[("case0006", "A")]
            case6_b = by_identity[("case0006", "B")]
            self.assertFalse((case2_a / "input/repository/.git").exists())
            self.assertFalse((case3_b / "input/repository/.git").exists())
            self.assertTrue((case6_a / "input/repository/.git").is_dir())
            self.assertTrue((case6_b / "input/repository/.git").is_dir())
            manifest6_a = json.loads((case6_a / "artifacts/source-bundle-manifest.json").read_bytes())
            manifest6_b = json.loads((case6_b / "artifacts/source-bundle-manifest.json").read_bytes())
            git_files_a = [
                item for item in manifest6_a["files"]
                if item["path"].startswith("input/repository/.git/")
            ]
            git_files_b = [
                item for item in manifest6_b["files"]
                if item["path"].startswith("input/repository/.git/")
            ]
            self.assertTrue(git_files_a)
            self.assertEqual(git_files_a, git_files_b)
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
            patch_bytes = (case2_a / "input/scoped-diff.patch").read_bytes()
            self.assertEqual(patch_bytes, (case2_b / "input/scoped-diff.patch").read_bytes())
            self.assertIn(b"value = 'base'", patch_bytes)
            self.assertIn(b"value = 'head'", patch_bytes)
            self.assertNotEqual(
                (case2_a / "input/prompt.txt").read_bytes(),
                (case2_b / "input/prompt.txt").read_bytes(),
            )
            manifest = json.loads((case3_b / "artifacts/source-bundle-manifest.json").read_bytes())
            manifest_a = json.loads((case2_a / "artifacts/source-bundle-manifest.json").read_bytes())
            manifest_case2_b = json.loads((case2_b / "artifacts/source-bundle-manifest.json").read_bytes())
            self.assertEqual(manifest["skill_resource"]["member_sha256"], hashlib.sha256(b"Pinned test-only Skill.\n").hexdigest())
            self.assertEqual(manifest["skill_resource"]["zip_path"], "skill-bundle.zip")
            self.assertEqual(manifest["skill_resource"]["path"], str(SANDBOX_SKILL_PATH))
            self.assertEqual(manifest["scoped_patch"], manifest_a["scoped_patch"])
            self.assertEqual(manifest["scoped_patch"]["sha256"], hashlib.sha256(patch_bytes).hexdigest())
            self.assertEqual(manifest["scoped_patch"]["scope_paths"], ["src.py"])
            self.assertEqual(manifest_a["scoped_patch"], manifest_case2_b["scoped_patch"])
            reviewer_payload = json.loads((case2_a / "input/reviewer-input.json").read_bytes())
            self.assertEqual(reviewer_payload["scoped_patch"], manifest_a["scoped_patch"])
            common_prompt = (case2_a / "input/common-prompt.md").read_bytes()
            self.assertTrue(common_prompt.startswith(batch.COMMON_PROMPT.read_bytes().rstrip(b"\n")))
            self.assertIn(manifest_a["scoped_patch"]["sha256"].encode(), common_prompt)
            for run_root in (case2_a, case2_b):
                prompt = (run_root / "input/prompt.txt").read_bytes()
                self.assertTrue(prompt.startswith(common_prompt.rstrip(b"\n")))
                self.assertIn(b"Review the branch change in repository from base to head", prompt)
                self.assertIn(b"Review only these scoped changes. Read the patch file as the exact base-to-head diff.", prompt)
                self.assertIn(b"/slot/input/scoped-diff.patch", prompt)
                self.assertIn(manifest_a["scoped_patch"]["sha256"].encode(), prompt)
                self.assertIn(manifest_a["scoped_patch"]["base"].encode(), prompt)
                self.assertIn(manifest_a["scoped_patch"]["head"].encode(), prompt)
                self.assertIn(b'["src.py"]', prompt)
                self.assertNotIn(b"has no .git", prompt)
            case6_b_prompt = (case6_b / "input/prompt.txt").read_bytes()
            self.assertTrue((case6_b / "input/repository/.git").is_dir())
            self.assertNotIn(b"has no .git", case6_b_prompt)
            self.assertTrue(manifest["skill_resource"]["mounted"])
            self.assertNotEqual(
                manifest["skill_resource"]["host_path"],
                manifest["skill_resource"]["path"],
            )
            self.assertEqual(
                Path(manifest["skill_resource"]["host_path"]).read_bytes(),
                b"Pinned test-only Skill.\n",
            )
            self.assertEqual(manifest_a["skill_resource"], {
                "path": str(SANDBOX_SKILL_PATH),
                "mounted": False,
            })
            self.assertIn(
                b"/slot/input/pinned-skill/review-changes/SKILL.md",
                (case3_b / "input/prompt.txt").read_bytes(),
            )
            self.assertNotIn(str(case3_b).encode(), (case3_b / "input/prompt.txt").read_bytes())
            self.assertTrue((case3_b / manifest["skill_resource"]["zip_path"]).is_file())
            self.assertEqual(
                (case2_a / "input/protocol/project_tool_runs.py").read_bytes(),
                projector.read_bytes(),
            )
            self.assertEqual(manifest["immutable_plan_sha256"], immutable_plan_sha256(plan))
            self.assertEqual(stat.S_IMODE(case3_b.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((case3_b / "artifacts/source-bundle-manifest.json").stat().st_mode),
                0o600,
            )
            immutable_sha = immutable_plan_sha256(plan)
            for prepared_root, slot in zip(prepared, plan["slots"], strict=True):
                metadata = json.loads((prepared_root / "run-metadata.json").read_bytes())
                manifest = json.loads((prepared_root / "artifacts/source-bundle-manifest.json").read_bytes())
                preseal = json.loads((prepared_root / "preseal.json").read_bytes())
                self.assertEqual(metadata["benchmark_id"], plan["benchmark_id"])
                self.assertEqual(manifest["benchmark_id"], plan["benchmark_id"])
                self.assertEqual(preseal["benchmark_id"], plan["benchmark_id"])
                self.assertEqual(metadata["immutable_plan_sha256"], immutable_sha)
                self.assertEqual(manifest["immutable_plan_sha256"], immutable_sha)
                self.assertEqual(preseal["immutable_plan_sha256"], immutable_sha)
                self.assertEqual(batch._preseal_status(prepared_root, plan, slot)["metadata"], metadata)
                self.assertEqual(
                    batch._preseal_status(prepared_root, immutable_sha)["metadata"],
                    metadata,
                )

            prompt_path = case2_a / "input/prompt.txt"
            original_prompt = prompt_path.read_bytes()
            prompt_path.write_bytes(original_prompt.replace(
                manifest_a["scoped_patch"]["sha256"].encode(), b"0" * 64,
            ))
            with self.assertRaisesRegex(ValueError, "prepared prompt does not preserve"):
                batch._preseal_status(case2_a, plan, plan["slots"][0])
            prompt_path.write_bytes(original_prompt)

            patch_path = case2_a / "input/scoped-diff.patch"
            original_patch = patch_path.read_bytes()
            patch_path.write_bytes(original_patch + b"tampered\n")
            with self.assertRaisesRegex(ValueError, "scoped patch content or digest"):
                batch._preseal_status(case2_a, plan, plan["slots"][0])
            patch_path.write_bytes(original_patch)
            patch_path.unlink()
            with self.assertRaisesRegex(ValueError, "scoped patch content or digest"):
                batch._preseal_status(case2_a, plan, plan["slots"][0])
            patch_path.write_bytes(original_patch)

            drift_root = prepared[0]
            drift_slot = plan["slots"][0]
            for relative in (
                "run-metadata.json",
                "artifacts/source-bundle-manifest.json",
                "preseal.json",
            ):
                record_path = drift_root / relative
                original = record_path.read_bytes()
                drifted = json.loads(original)
                drifted["benchmark_id"] = "different-batch"
                record_path.write_text(json.dumps(drifted) + "\n", encoding="utf-8")
                with self.subTest(drifted_record=relative):
                    with self.assertRaisesRegex(ValueError, "does not match|does not bind"):
                        batch._preseal_status(drift_root, plan, drift_slot)
                record_path.write_bytes(original)

            archived_plan_path = drift_root / "artifacts/batch-plan.json"
            archived_plan_bytes = archived_plan_path.read_bytes()
            archived_plan = json.loads(archived_plan_bytes)
            archived_plan["benchmark_id"] = "different-batch"
            archived_plan_path.write_text(json.dumps(archived_plan) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "plan identity"):
                batch._preseal_status(drift_root, plan, drift_slot)

            archived_plan["benchmark_id"] = plan["benchmark_id"]
            archived_plan["cases"]["case0002"]["annotation_status"] = "annotated"
            archived_plan_path.write_text(json.dumps(archived_plan) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "plan identity"):
                batch._preseal_status(drift_root, plan, drift_slot)
            archived_plan_path.write_bytes(archived_plan_bytes)

            legacy_plan = {**plan, "benchmark_id": BENCHMARK_ID}
            legacy_plan_bytes = (json.dumps(legacy_plan, sort_keys=True) + "\n").encode()
            legacy_prepared = preseal_plan(
                legacy_plan,
                legacy_plan_bytes,
                root / "legacy-runs",
                runtime_config,
                skill,
            )
            legacy_root = legacy_prepared[0]
            legacy_slot = legacy_plan["slots"][0]
            self.assertEqual(
                batch._preseal_status(legacy_root, legacy_plan, legacy_slot)["metadata"]["benchmark_id"],
                BENCHMARK_ID,
            )

            legacy_preseal_path = legacy_root / "preseal.json"
            legacy_preseal = json.loads(legacy_preseal_path.read_bytes())
            for field in ("benchmark_id", "plan_sha256", "replicate", "attempt", "attempt_kind"):
                del legacy_preseal[field]
            legacy_preseal_path.write_text(json.dumps(legacy_preseal) + "\n", encoding="utf-8")
            self.assertEqual(
                batch._preseal_status(legacy_root, legacy_plan, legacy_slot)["metadata"]["benchmark_id"],
                BENCHMARK_ID,
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
                codex_executable=Path("/usr/bin/codex"),
                strace_executable=Path("/usr/bin/strace"),
                bwrap_executable=_find_bundled_bwrap(Path("/usr/bin/codex")),
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
        self.assertEqual(config["schema_version"], 3)
        self.assertEqual(config["sandbox_identity"], {
            "environment": {"TZ": "UTC0"},
            "effective_timezone": "UTC",
        })
        self.assertTrue(Path(config["bwrap_executable"]).is_file())

    def test_missing_effort_requires_an_explicit_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._codex_toml(Path(temp_dir), effort=None)
            with self.assertRaisesRegex(ValueError, "reasoning_effort is unsupported"):
                _freeze_runtime_config(
                    config_path,
                    codex_executable=Path("/usr/bin/codex"),
                    strace_executable=Path("/usr/bin/strace"),
                    bwrap_executable=_find_bundled_bwrap(Path("/usr/bin/codex")),
                )
            config = _freeze_runtime_config(
                config_path,
                codex_executable=Path("/usr/bin/codex"),
                strace_executable=Path("/usr/bin/strace"),
                bwrap_executable=_find_bundled_bwrap(Path("/usr/bin/codex")),
                reasoning_effort="high",
            )
        self.assertEqual(config["reasoning_effort"], "high")
        self.assertEqual(config["schema_version"], 3)
        self.assertEqual(
            config["bwrap_executable"],
            str(_find_bundled_bwrap(Path("/usr/bin/codex"))),
        )

    def test_overrides_are_explicit_and_preflight_is_non_model(self) -> None:
        config = {
            "codex_executable": sys.executable,
            "strace_executable": "/usr/bin/strace",
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

        completed = mock.Mock(returncode=0, stdout=b"--decode-pids=pidns")
        with mock.patch("eval.run_ab_batch.subprocess.run", return_value=completed) as run:
            _preflight_codex_config(config)

        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0], ["/usr/bin/strace", "--help"])
        self.assertEqual(run.call_args_list[0].kwargs["stdout"], -1)
        features_argv = run.call_args_list[1].args[0]
        execution_argv = run.call_args_list[2].args[0]
        self.assertEqual(features_argv[-2:], ["features", "list"])
        self.assertEqual(execution_argv[1:4], ["exec", "--ignore-user-config", "--ignore-rules"])
        self.assertIn("--version", execution_argv)
        self.assertEqual(
            [item for item in features_argv if item == "-c"],
            [item for item in execution_argv if item == "-c"],
        )
        self.assertEqual(run.call_args_list[1].kwargs["stdin"], -3)

    def test_preflight_rejects_strace_without_pid_namespace_decoding(self) -> None:
        config = {
            "codex_executable": sys.executable,
            "strace_executable": "/usr/bin/strace",
            "model": "gpt-6.1-sol",
            "provider_definition": {
                "name": "custom",
                "base_url": "http://127.0.0.1:8080/v1",
                "wire_api": "responses",
                "requires_openai_auth": True,
            },
            "reasoning_effort": "max",
        }
        unsupported = mock.Mock(returncode=0, stdout=b"usage: strace [options]")
        with mock.patch("eval.run_ab_batch.subprocess.run", return_value=unsupported) as run:
            with self.assertRaisesRegex(ValueError, "must support --decode-pids=pidns"):
                _preflight_codex_config(config)
        self.assertEqual(run.call_count, 1)

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


def _fake_cli_slot(
    tmp_path: Path,
    *,
    condition: str = "A",
    benchmark_id: str = BENCHMARK_ID,
    bwrap_executable: str = "/fake/bwrap",
) -> tuple[Path, dict]:
    cases = [
        {
            "case_id": case_id,
            "base": "abc",
            "head": "def",
            "snapshot": {"base_tree": "b", "head_tree": "h", "scoped_diff_sha256": "d"},
            "scope_paths": ["src.py"],
            "reviewer_input": "/unused/reviewer-input.json",
            "repositories": {"same_host_direct": "/unused/repository"},
        }
        for case_id in ("case0002", "case0003", "case0006")
    ]
    plan = build_slot_plan({"cases": cases})
    plan["benchmark_id"] = benchmark_id
    slot = next(
        item for item in plan["slots"]
        if item["case_id"] == "case0002" and item["condition"] == condition and item["replicate"] == 1
    )
    plan_bytes = (json.dumps(plan, sort_keys=True) + "\n").encode()
    condition_dir = "same_host_direct" if condition == "A" else "current_skill"
    run_root = tmp_path / "case0002" / condition_dir / "repeat01"
    for relative in ("input/protocol", "artifacts", "runtime"):
        path = run_root / relative
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)
    run_root.chmod(0o700)
    (run_root / "input").chmod(0o700)

    patch_bytes = b"diff --git a/src.py b/src.py\nsynthetic patch\n"
    patch_record = {
        "path": "input/scoped-diff.patch",
        "base": "abc",
        "head": "def",
        "base_tree": "b",
        "head_tree": "h",
        "scope_paths": ["src.py"],
        "sha256": hashlib.sha256(patch_bytes).hexdigest(),
        "bytes": len(patch_bytes),
    }
    reviewer = (json.dumps({
        "base": "abc",
        "head": "def",
        "scope_paths": ["src.py"],
        "snapshot": plan["cases"]["case0002"]["snapshot"],
        "scoped_patch": patch_record,
    }) + "\n").encode()
    common_prompt = batch._case_common_prompt(batch.COMMON_PROMPT.read_bytes(), patch_record)
    prompt = batch._read_prompt(common_prompt, str(SANDBOX_SKILL_PATH) if condition == "B" else None)
    schema = batch.HOST_SCHEMA.read_bytes()
    config_bytes = b'{"schema_version":3,"codex_executable":"/fake/codex","strace_executable":"/usr/bin/strace","bwrap_executable":"/fake/bwrap","model":"gpt-6.1-sol","provider_definition":{"name":"custom","base_url":"http://127.0.0.1:8080/v1","wire_api":"responses","requires_openai_auth":true},"reasoning_effort":"high","timeout_seconds":30,"sandbox_identity":{"environment":{"TZ":"UTC0"},"effective_timezone":"UTC"}}\n'
    binding_payloads = {
        "input/reviewer-input.json": reviewer,
        "input/common-prompt.md": common_prompt,
        "input/protocol/host-output.schema.json": schema,
        "input/protocol/project_tool_runs.py": (
            Path(__file__).parents[1] / "skills/review-changes/scripts/project_tool_runs.py"
        ).read_bytes(),
        "input/runtime-config.json": config_bytes,
    }
    for relative, payload in binding_payloads.items():
        _private_write(run_root / relative, payload)
    _private_write(run_root / "input/scoped-diff.patch", patch_bytes)
    _private_write(run_root / "input/prompt.txt", prompt)
    skill_text = b"Pinned test-only Skill.\n"
    skill_resource = {
        "path": str(SANDBOX_SKILL_PATH),
        "mounted": condition == "B",
    }
    extra_files = [
        {
            "path": "input/scoped-diff.patch",
            "sha256": hashlib.sha256(patch_bytes).hexdigest(),
            "bytes": len(patch_bytes),
        },
        {"path": "input/prompt.txt", "sha256": hashlib.sha256(prompt).hexdigest()},
    ]
    if condition == "B":
        skill_host_path = run_root / "input/pinned-skill/review-changes/SKILL.md"
        _private_write(skill_host_path, skill_text)
        for directory in skill_host_path.parents[:3]:
            directory.chmod(0o700)
        skill_bundle = run_root / "skill-bundle.zip"
        with zipfile.ZipFile(skill_bundle, "w") as archive:
            archive.writestr("review-changes/SKILL.md", skill_text)
        skill_bundle.chmod(0o600)
        skill_resource.update({
            "host_path": str(skill_host_path.resolve()),
            "zip_path": "skill-bundle.zip",
            "zip_sha256": hashlib.sha256(skill_bundle.read_bytes()).hexdigest(),
            "zip_member": "review-changes/SKILL.md",
            "member_sha256": hashlib.sha256(skill_text).hexdigest(),
        })
        extra_files.extend([
            {
                "path": "input/pinned-skill/review-changes/SKILL.md",
                "sha256": hashlib.sha256(skill_text).hexdigest(),
            },
            {
                "path": "skill-bundle.zip",
                "sha256": skill_resource["zip_sha256"],
            },
        ])
    normalizer = run_root / "artifacts/runtime_trace.py"
    _private_write(normalizer, TRACE_NORMALIZER.read_bytes())
    manifest = {
        "schema_version": 2,
        "benchmark_id": plan["benchmark_id"],
        "case_id": slot["case_id"],
        "condition": slot["condition"],
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "immutable_plan_sha256": immutable_plan_sha256(plan),
        "scoped_patch": patch_record,
        "files": [
            {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}
            for relative, payload in binding_payloads.items()
        ] + extra_files,
        "skill_resource": skill_resource,
    }
    _private_write(
        run_root / "artifacts/source-bundle-manifest.json",
        (json.dumps(manifest, sort_keys=True) + "\n").encode(),
    )
    _private_write(run_root / "artifacts/batch-plan.json", plan_bytes)
    metadata = {
        "schema_version": 1,
        "benchmark_id": plan["benchmark_id"],
        "case_id": slot["case_id"],
        "condition": slot["condition"],
        "run_order": slot["run_order"],
        "replicate": slot["replicate"],
        "attempt": slot["attempt"],
        "attempt_kind": slot["attempt_kind"],
        "group_id": f"{plan['benchmark_id']}:{slot['case_id']}:replicate-{slot['replicate']}",
        "immutable_plan_sha256": immutable_plan_sha256(plan),
    }
    _private_write(run_root / "run-metadata.json", (json.dumps(metadata) + "\n").encode())

    canaries = {
        "sibling": tmp_path / "sibling-run" / "private.txt",
        "operator": tmp_path / "operator" / "private.txt",
        "checkout": Path(__file__).resolve().parents[1] / "eval" / "run_ab_batch.py",
    }
    for name in ("sibling", "operator"):
        _private_write(canaries[name], b"not visible in the bwrap namespace\n")
    script = """#!/usr/bin/python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path
args = sys.argv[1:]
time.tzset()
prompt_data = sys.stdin.buffer.read()
output_path = Path(args[args.index("--output-last-message") + 1])
input_path = Path.cwd()
auth_path = Path(os.environ["CODEX_HOME"]) / "auth.json"
skill_path = Path(__SKILL_PATH__)
skill_read = subprocess.run(["/usr/bin/cat", "--", str(skill_path)], capture_output=True)
readonly = getattr(os, "ST_RDONLY", 1)
checks = {{
    "input_read_only": bool(os.statvfs(input_path).f_flag & readonly),
    "auth_present": auth_path.is_file(),
    "auth_read_only": bool(os.statvfs(auth_path).f_flag & readonly),
    "output_writable": not bool(os.statvfs(output_path).f_flag & readonly),
    "environment_cleared": set(os.environ) == {
        "PATH", "HOME", "CODEX_HOME", "TMPDIR", "LC_CTYPE", "TZ", "PWD",
    },
    "timezone_value_is_pinned": os.environ.get("TZ") == "UTC0",
    "timezone_effective_utc": time.localtime(0) == time.gmtime(0),
    "sibling_hidden": not Path(__SIBLING_CANARY__).exists(),
    "operator_hidden": not Path(__OPERATOR_CANARY__).exists(),
    "checkout_hidden": not Path(__CHECKOUT_CANARY__).exists(),
    "skill_read_expected": __SKILL_READ_CHECK__,
    "auth_write_denied": False,
    "input_write_denied": False,
    "prompt_stdin_received": prompt_data.startswith(b"Review the branch change in repository from base to head"),
}}
try:
    os.open(auth_path, os.O_WRONLY | os.O_APPEND)
except OSError:
    checks["auth_write_denied"] = True
else:
    raise AssertionError("auth mount accepted a write open")
try:
    os.open(input_path / "reviewer-input.json", os.O_WRONLY | os.O_APPEND)
except OSError:
    checks["input_write_denied"] = True
else:
    raise AssertionError("slot input accepted a write open")
if not all(checks.values()):
    raise AssertionError("sandbox mount checks failed: " + json.dumps(checks, sort_keys=True))
(Path(os.environ["CODEX_HOME"]) / "sandbox-check.json").write_text(json.dumps(checks))
installation_id = Path(os.environ["CODEX_HOME"]) / "installation_id"
installation_id.write_text("fake installation\\n")
installation_id.chmod(0o644)
output_path.write_text(json.dumps({
    "schema_version": 1,
    "case_id": "case0002",
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
print(json.dumps({"type": "item.completed", "item": {
    "type": "command_execution",
    "id": "skill-cat-call",
    "command": "cat -- " + str(skill_path),
    "aggregated_output": skill_read.stdout.decode("utf-8"),
}}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
"""
    script = (
        script.replace("__SIBLING_CANARY__", repr(str(canaries["sibling"])))
        .replace("__OPERATOR_CANARY__", repr(str(canaries["operator"])))
        .replace("__CHECKOUT_CANARY__", repr(str(canaries["checkout"])))
        .replace("__SKILL_PATH__", repr(str(SANDBOX_SKILL_PATH)))
        .replace(
            "__SKILL_READ_CHECK__",
            "skill_read.returncode == 0" if condition == "B" else "skill_read.returncode != 0",
        )
        .replace("checks = {{", "checks = {")
        .replace("\n}}\ntry:", "\n}\ntry:")
    )
    executable = run_root / "input" / "codex"
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o700)
    manifest["files"].append({
        "path": "input/codex",
        "sha256": hashlib.sha256(script.encode()).hexdigest(),
    })
    _private_write(
        run_root / "artifacts/source-bundle-manifest.json",
        (json.dumps(manifest, sort_keys=True) + "\n").encode(),
    )
    _private_write(run_root / "preseal.json", (
        json.dumps({
            "schema_version": 1,
            "benchmark_id": plan["benchmark_id"],
            "case_id": slot["case_id"],
            "condition": slot["condition"],
            "run_order": slot["run_order"],
            "replicate": slot["replicate"],
            "attempt": slot["attempt"],
            "attempt_kind": slot["attempt_kind"],
            "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
            "immutable_plan_sha256": immutable_plan_sha256(plan),
            "scoped_patch": patch_record,
            "source_bundle_manifest": batch.make_artifact_ref(
                run_root, run_root / "artifacts/source-bundle-manifest.json",
            ),
        }, sort_keys=True) + "\n"
    ).encode())
    config = {
        "schema_version": 3,
        "codex_executable": str(executable),
        "strace_executable": "/usr/bin/strace",
        "bwrap_executable": bwrap_executable,
        "model": "gpt-6.1-sol",
        "provider_definition": {
            "name": "custom",
            "base_url": "http://127.0.0.1:8080/v1",
            "wire_api": "responses",
            "requires_openai_auth": True,
        },
        "reasoning_effort": "high",
        "timeout_seconds": 30,
        "sandbox_identity": {
            "environment": {"TZ": "UTC0"},
            "effective_timezone": "UTC",
        },
    }
    return run_root, config


def _real_adapter_cli_slot(
    tmp_path: Path,
    *,
    condition: str = "A",
    benchmark_id: str = BENCHMARK_ID,
) -> tuple[Path, dict]:
    bwrap_executable = str(_find_bundled_bwrap(Path("/usr/bin/codex")))
    return _fake_cli_slot(
        tmp_path,
        condition=condition,
        benchmark_id=benchmark_id,
        bwrap_executable=bwrap_executable,
    )


class BatchExecutionAdapterTests(unittest.TestCase):
    def test_execute_cli_status_matches_exit_code(self) -> None:
        case_index_path = Path(__file__).parents[1] / "eval/data/real-cases.json"
        index = json.loads(case_index_path.read_bytes())
        plan = dict(build_slot_plan(index), plan_state="go", execution_authorized=True)
        successful = [{"run_status": "completed"} for _ in plan["slots"]]
        failed = [dict(result) for result in successful]
        failed[0]["run_status"] = "failed"
        blocked = [dict(result) for result in successful]
        blocked[0].update(run_status="not_run", execution_status="blocked")
        blocked[1]["run_status"] = "failed"
        cases = (
            (successful, "completed", 0),
            (failed, "failed", 1),
            (blocked, "blocked", 3),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            plan_path = Path(temp_dir) / "plan.json"
            plan_path.write_text(json.dumps(plan, sort_keys=True) + "\n", encoding="utf-8")
            for results, expected_status, expected_exit_code in cases:
                with self.subTest(expected_status=expected_status):
                    with (
                        mock.patch("eval.run_ab_batch.execute_plan", return_value=results),
                        mock.patch("builtins.print") as output,
                    ):
                        exit_code = batch.main([
                            "--case-index", str(case_index_path),
                            "--plan", str(plan_path),
                            "--execute",
                            "--runs-root", str(Path(temp_dir) / "runs"),
                            "--runtime-config", str(Path(temp_dir) / "runtime-config.json"),
                        ])

                    response = json.loads(output.call_args.args[0])
                    self.assertEqual(response["status"], expected_status)
                    self.assertEqual(exit_code, expected_exit_code)
                    self.assertEqual(
                        response.get("next_step"),
                        "adapter_validation" if expected_status == "blocked" else None,
                    )

    def test_preflight_without_runtime_config_reports_blocked_when_codex_is_missing(self) -> None:
        with (
            mock.patch("eval.run_ab_batch.shutil.which", return_value=None) as which,
            mock.patch("eval.run_ab_batch._find_bundled_bwrap") as find_bwrap,
            mock.patch("eval.run_ab_batch.subprocess.Popen") as popen,
            mock.patch("eval.run_ab_batch.subprocess.run") as run,
            mock.patch("builtins.print") as output,
        ):
            exit_code = batch.main(["--preflight"])

        status = json.loads(output.call_args.args[0])
        self.assertEqual(exit_code, 3)
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["runtime_config"], "not_provided")
        self.assertEqual(status["tools"]["codex_executable"]["status"], "unavailable")
        self.assertEqual(status["raw_trace_parser"]["status"], "available")
        self.assertEqual(status["causal_binding"]["reason"], "cli_process_binding_unavailable")
        self.assertEqual(status["next_step"], "adapter_validation")
        self.assertEqual(which.call_args_list, [mock.call("codex"), mock.call("strace")])
        find_bwrap.assert_not_called()
        popen.assert_not_called()
        run.assert_not_called()

    def test_trace_argv_uses_finite_limit_without_environment_expansion(self) -> None:
        argv = batch._build_trace_argv(
            "/usr/bin/strace", Path("/tmp/synthetic-trace.log"), ["/usr/bin/true"],
        )

        self.assertEqual(argv[argv.index("-s") + 1], str(STRACE_STRING_LIMIT))
        self.assertIn("--decode-pids=pidns", argv)
        self.assertNotIn("-v", argv)
        self.assertIn("raw=read,pread64,readv,preadv,preadv2", argv)
        captured_syscalls = argv[argv.index("-e", argv.index("-e") + 1) + 1]
        self.assertIn("signalfd", captured_syscalls)
        self.assertIn("signalfd4", captured_syscalls)
        self.assertIn("inotify_init", captured_syscalls)
        self.assertIn("inotify_init1", captured_syscalls)
        self.assertEqual(argv[-2:], ["--", "/usr/bin/true"])

    def test_execute_slot_records_block_without_starting_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch(
                    "eval.run_ab_batch._find_bundled_bwrap",
                    side_effect=AssertionError("pure blocking fixture must not inspect a Codex package"),
                ) as find_bwrap,
                mock.patch("eval.run_ab_batch.subprocess.Popen") as popen,
                mock.patch("eval.run_ab_batch.subprocess.run") as run,
            ):
                run_root, config = _fake_cli_slot(Path(temp_dir))
                result = execute_slot(run_root, config, go_authorized=True)

            find_bwrap.assert_not_called()
            self.assertEqual(result["run_status"], "not_run")
            self.assertFalse(result["launched"])
            self.assertEqual(result["outcome"], "not_started")
            self.assertEqual(result["execution_status"], "blocked")
            self.assertEqual(result["blocked_reason"], "cli_process_binding_unavailable")
            self.assertFalse((run_root / "runtime/adapter-start.json").exists())
            self.assertTrue((run_root / "runtime/adapter-preflight.json").is_file())
            self.assertFalse((run_root / "host-output.json").exists())
            self.assertFalse((run_root / "artifacts/raw-model-output.txt").exists())
            popen.assert_not_called()
            run.assert_not_called()

    def test_execute_slot_preserves_preexisting_result_without_runtime_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root, config = _fake_cli_slot(Path(temp_dir))
            result_path = run_root / "slot-result.json"
            original_result = b'{"legacy_status":"completed"}\n'
            result_path.write_bytes(original_result)

            with (
                mock.patch("eval.run_ab_batch.subprocess.Popen") as popen,
                mock.patch("eval.run_ab_batch.subprocess.run") as run,
                self.assertRaisesRegex(ValueError, "already been attempted"),
            ):
                execute_slot(run_root, config, go_authorized=True)

            self.assertEqual(result_path.read_bytes(), original_result)
            popen.assert_not_called()
            run.assert_not_called()

    def test_execute_plan_records_blocked_slots_without_starting_processes(self) -> None:
        index = json.loads((Path(__file__).parents[1] / "eval/data/real-cases.json").read_bytes())
        plan = dict(build_slot_plan(index), plan_state="go", execution_authorized=True)
        plan["benchmark_id"] = "blocked-capability-batch.v1"
        config = {
            "schema_version": 3,
            "codex_executable": "/missing/codex",
            "strace_executable": "/missing/strace",
            "bwrap_executable": "/missing/bwrap",
            "model": "gpt-6.1-sol",
            "provider_definition": {
                "name": "custom",
                "base_url": "http://127.0.0.1:8080/v1",
                "wire_api": "responses",
                "requires_openai_auth": True,
            },
            "reasoning_effort": "high",
            "timeout_seconds": 30,
            "sandbox_identity": {
                "environment": {"TZ": "UTC0"},
                "effective_timezone": "UTC",
            },
        }
        config_bytes = (json.dumps(config) + "\n").encode()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runs_root = root / "runs"
            config_path = root / "runtime-config.json"
            config_path.write_bytes(config_bytes)
            for slot in plan["slots"]:
                condition = "same_host_direct" if slot["condition"] == "A" else "current_skill"
                run_root = runs_root / slot["case_id"] / condition / f"repeat{slot['replicate']:02d}"
                input_root = run_root / "input"
                input_root.mkdir(parents=True)
                (input_root / "runtime-config.json").write_bytes(config_bytes)

            with (
                mock.patch("eval.run_ab_batch._preseal_status", return_value={}),
                mock.patch("eval.run_ab_batch._preflight_codex_config") as codex_preflight,
                mock.patch("eval.run_ab_batch.subprocess.Popen") as popen,
                mock.patch("eval.run_ab_batch.subprocess.run") as run,
            ):
                results = execute_plan(plan, runs_root, config_path)

            self.assertEqual(len(results), len(plan["slots"]))
            self.assertTrue(all(result["run_status"] == "not_run" for result in results))
            self.assertTrue(all(not result["launched"] for result in results))
            self.assertTrue(all(result["outcome"] == "not_started" for result in results))
            self.assertTrue(all(result["execution_status"] == "blocked" for result in results))
            self.assertTrue(all(
                result["blocked_reason"] == "cli_process_binding_unavailable"
                for result in results
            ))
            for slot in plan["slots"]:
                condition = "same_host_direct" if slot["condition"] == "A" else "current_skill"
                run_root = runs_root / slot["case_id"] / condition / f"repeat{slot['replicate']:02d}"
                self.assertTrue((run_root / "runtime/adapter-preflight.json").is_file())
                self.assertFalse((run_root / "runtime/adapter-start.json").exists())
                self.assertFalse((run_root / "host-output.json").exists())
                self.assertFalse((run_root / "artifacts/raw-model-output.txt").exists())
            codex_preflight.assert_not_called()
            popen.assert_not_called()
            run.assert_not_called()

    def test_execute_slot_rejects_open_presealed_schema_before_adapter_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root, config = _fake_cli_slot(Path(temp_dir))
            schema_path = run_root / "input/protocol/host-output.schema.json"
            schema = json.loads(schema_path.read_bytes())
            schema["properties"]["tool_runs"]["items"] = {"type": "object"}
            schema_bytes = (json.dumps(schema, sort_keys=True) + "\n").encode()
            schema_path.write_bytes(schema_bytes)

            manifest_path = run_root / "artifacts/source-bundle-manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            schema_entry = next(
                item for item in manifest["files"]
                if item["path"] == "input/protocol/host-output.schema.json"
            )
            schema_entry["sha256"] = hashlib.sha256(schema_bytes).hexdigest()
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

            preseal_path = run_root / "preseal.json"
            preseal = json.loads(preseal_path.read_bytes())
            preseal["source_bundle_manifest"] = batch.make_artifact_ref(run_root, manifest_path)
            preseal_path.write_text(json.dumps(preseal, sort_keys=True) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "additionalProperties to false"):
                execute_slot(run_root, config, go_authorized=True)
            self.assertFalse((run_root / "runtime/adapter-start.json").exists())

    def test_execute_plan_records_failure_artifacts_and_continues(self) -> None:
        index = json.loads((Path(__file__).parents[1] / "eval/data/real-cases.json").read_bytes())
        plan = dict(build_slot_plan(index), plan_state="go", execution_authorized=True)
        plan["benchmark_id"] = "new-safe-batch.v3"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runs_root = root / "runs"
            config_path = root / "runtime-config.json"
            config_bytes = b"{}\n"
            config_path.write_bytes(config_bytes)
            run_roots = []
            for slot in plan["slots"]:
                condition = "same_host_direct" if slot["condition"] == "A" else "current_skill"
                run_root = runs_root / slot["case_id"] / condition / f"repeat{slot['replicate']:02d}"
                (run_root / "input").mkdir(parents=True)
                (run_root / "input/runtime-config.json").write_bytes(config_bytes)
                run_roots.append(run_root)

            first_root = run_roots[0]

            def run_slot(run_root: Path, _config: dict, *, go_authorized: bool) -> dict:
                self.assertTrue(go_authorized)
                if run_root == first_root:
                    (run_root / "runtime").mkdir()
                    (run_root / "runtime/strace.log").write_bytes(b"partial trace\n")
                    raise RuntimeError("simulated adapter failure")
                return {"run_status": "completed"}

            real_inventory = batch._artifact_inventory

            def inspect_ledger_then_collect(run_root: Path) -> dict:
                ledger_path = run_root / "slot-failure-ledger.json"
                self.assertTrue(ledger_path.is_file())
                early_ledger = json.loads(ledger_path.read_bytes())
                self.assertEqual(early_ledger["artifacts"], {})
                self.assertEqual(
                    ledger_path.with_suffix(".json.sha256").read_text(encoding="ascii"),
                    f"{hashlib.sha256(ledger_path.read_bytes()).hexdigest()}  {ledger_path.name}\n",
                )
                return real_inventory(run_root)

            with (
                mock.patch("eval.run_ab_batch._runtime_config", return_value={}),
                mock.patch("eval.run_ab_batch._adapter_capability_status", return_value={"status": "available"}),
                mock.patch("eval.run_ab_batch._preflight_codex_config"),
                mock.patch("eval.run_ab_batch._preseal_status", return_value={}),
                mock.patch("eval.run_ab_batch._artifact_inventory", side_effect=inspect_ledger_then_collect),
                mock.patch("eval.run_ab_batch.execute_slot", side_effect=run_slot) as execute,
            ):
                results = execute_plan(plan, runs_root, config_path)

            self.assertEqual(len(results), 12)
            self.assertEqual(results[0]["run_status"], "failed")
            self.assertTrue(all(result["run_status"] == "completed" for result in results[1:]))
            self.assertEqual(execute.call_count, 12)
            ledger_path = first_root / "slot-failure-ledger.json"
            ledger = json.loads(ledger_path.read_bytes())
            self.assertEqual(ledger["benchmark_id"], plan["benchmark_id"])
            self.assertEqual(ledger["immutable_plan_sha256"], immutable_plan_sha256(plan))
            self.assertEqual(ledger["run_order"], 1)
            self.assertEqual(ledger["failure_reason"], "adapter_failure:RuntimeError")
            self.assertEqual(
                ledger["artifacts"]["runtime/strace.log"]["sha256"],
                hashlib.sha256(b"partial trace\n").hexdigest(),
            )
            self.assertNotIn("slot-failure-ledger.json", ledger["artifacts"])
            self.assertNotIn("slot-failure-ledger.json.sha256", ledger["artifacts"])
            self.assertEqual(ledger["excluded_runtime_symlinks"], [])
            self.assertEqual(
                ledger_path.with_suffix(".json.sha256").read_text(encoding="ascii"),
                f"{hashlib.sha256(ledger_path.read_bytes()).hexdigest()}  {ledger_path.name}\n",
            )

    def test_artifact_inventory_excludes_external_symlinks_and_records_link_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "run"
            artifact = root / "runtime" / "trace.log"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"trace\n")
            executable = Path(temp_dir) / "codex-native"
            executable.write_bytes(b"system executable")
            link = root / "runtime" / "codex-home" / "tmp" / "arg0" / "codex-execve-wrapper"
            link.parent.mkdir(parents=True)
            link.symlink_to(executable)

            inventory = batch._artifact_inventory(root)

        self.assertEqual(set(inventory["artifacts"]), {"runtime/trace.log"})
        self.assertEqual(
            inventory["excluded_runtime_symlinks"],
            [{"path": "runtime/codex-home/tmp/arg0/codex-execve-wrapper", "target": str(executable)}],
        )

    def test_artifact_inventory_propagates_scandir_denial_with_partial_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "run"
            root.mkdir()
            (root / "a-trace.log").write_bytes(b"partial trace\n")
            denied = root / "denied"
            denied.mkdir()
            real_scandir = batch.os.scandir

            def controlled_scandir(path: str | Path):
                if Path(path) == denied:
                    raise PermissionError("controlled denied directory")
                return real_scandir(path)

            with mock.patch("eval.run_ab_batch.os.scandir", side_effect=controlled_scandir):
                with self.assertRaises(batch.ArtifactCollectionError) as raised:
                    batch._artifact_inventory(root)

        self.assertIn("denied", str(raised.exception))
        self.assertIn("controlled denied directory", str(raised.exception))
        self.assertEqual(
            raised.exception.inventory["artifacts"],
            {
                "a-trace.log": {
                    "path": "a-trace.log",
                    "sha256": hashlib.sha256(b"partial trace\n").hexdigest(),
                },
            },
        )
        self.assertEqual(raised.exception.inventory["excluded_runtime_symlinks"], [])

    def test_runtime_mode_normalization_does_not_follow_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = root / "runtime"
            codex_home = runtime / "codex-home"
            codex_home.mkdir(parents=True)
            installation_id = codex_home / "installation_id"
            installation_id.write_text("local id\n", encoding="ascii")
            installation_id.chmod(0o644)
            external = root / "external executable"
            external.write_bytes(b"external")
            external.chmod(0o644)
            link = codex_home / "native-codex"
            link.symlink_to(external)

            batch._normalize_runtime_modes(runtime)

            self.assertEqual(stat.S_IMODE(installation_id.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o644)
            self.assertTrue(link.is_symlink())

    def test_runtime_root_symlink_is_rejected_without_changing_target_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            external = root / "external"
            external.mkdir()
            external.chmod(0o755)
            runtime = root / "runtime"
            runtime.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "real directory"):
                batch._normalize_runtime_modes(runtime)
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                batch._mkdir_private(runtime)

            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o755)
            self.assertTrue(runtime.is_symlink())

    def test_execute_plan_does_not_retry_a_failed_artifact_scan(self) -> None:
        index = json.loads((Path(__file__).parents[1] / "eval/data/real-cases.json").read_bytes())
        plan = dict(build_slot_plan(index), plan_state="go", execution_authorized=True)
        plan["benchmark_id"] = "new-safe-batch.v3"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runs_root = root / "runs"
            config_path = root / "runtime-config.json"
            config_bytes = b"{}\n"
            config_path.write_bytes(config_bytes)
            run_roots = []
            for slot in plan["slots"]:
                condition = "same_host_direct" if slot["condition"] == "A" else "current_skill"
                run_root = runs_root / slot["case_id"] / condition / f"repeat{slot['replicate']:02d}"
                (run_root / "input").mkdir(parents=True)
                (run_root / "input/runtime-config.json").write_bytes(config_bytes)
                run_roots.append(run_root)

            first_root = run_roots[0]
            partial = {"runtime/strace.log": {"path": "runtime/strace.log", "sha256": "a" * 64}}
            links = [{"path": "runtime/codex-home/tmp/arg0/codex", "target": "/usr/lib/codex"}]
            scan_error = batch.ArtifactCollectionError(
                "artifact inventory failed after partial collection",
                {"artifacts": partial, "excluded_runtime_symlinks": links},
            )

            def run_slot(run_root: Path, _config: dict, *, go_authorized: bool) -> dict:
                self.assertTrue(go_authorized)
                if run_root == first_root:
                    runtime = run_root / "runtime"
                    runtime.mkdir()
                    (runtime / "adapter-start.json").write_text("{}\n", encoding="utf-8")
                    raise scan_error
                return {"run_status": "completed"}

            with (
                mock.patch("eval.run_ab_batch._runtime_config", return_value={}),
                mock.patch("eval.run_ab_batch._adapter_capability_status", return_value={"status": "available"}),
                mock.patch("eval.run_ab_batch._preflight_codex_config"),
                mock.patch("eval.run_ab_batch._preseal_status", return_value={}),
                mock.patch("eval.run_ab_batch._artifact_inventory") as inventory,
                mock.patch("eval.run_ab_batch.execute_slot", side_effect=run_slot),
            ):
                results = execute_plan(plan, runs_root, config_path)

            self.assertEqual(len(results), 12)
            self.assertEqual(results[0]["run_status"], "failed")
            self.assertTrue(all(result["run_status"] == "completed" for result in results[1:]))
            inventory.assert_not_called()
            ledger_path = first_root / "slot-failure-ledger.json"
            ledger = json.loads(ledger_path.read_bytes())
            self.assertEqual(ledger["artifacts"], partial)
            self.assertEqual(ledger["excluded_runtime_symlinks"], links)
            self.assertTrue(ledger["artifact_collection_error"])
            slot_result = json.loads((first_root / "slot-result.json").read_bytes())
            self.assertEqual(slot_result["schema_version"], 1)
            self.assertEqual(slot_result["benchmark_id"], plan["benchmark_id"])
            self.assertEqual(slot_result["immutable_plan_sha256"], immutable_plan_sha256(plan))
            self.assertTrue(slot_result["planned"])
            self.assertTrue(slot_result["launched"])
            self.assertFalse(slot_result["host_completed"])
            self.assertFalse(slot_result["evidence_valid"])
            self.assertEqual(slot_result["outcome"], "failure")
            self.assertEqual(slot_result["condition"], "A")
            self.assertEqual(slot_result["case_id"], first_root.parts[-3])
            self.assertEqual(slot_result["run_order"], 1)
            self.assertEqual(slot_result["replicate"], 1)
            self.assertEqual(slot_result["attempt"], 1)
            self.assertEqual(slot_result["attempt_kind"], "independent_replicate")
            self.assertEqual(slot_result["run_status"], "failed")
            self.assertEqual(slot_result["failure_reason"], ledger["failure_reason"])
            self.assertEqual(slot_result["adapter_failure"], ledger["adapter_failure"])
            self.assertEqual(slot_result["artifacts"], ledger["artifacts"])
            self.assertEqual(slot_result["excluded_runtime_symlinks"], ledger["excluded_runtime_symlinks"])
            self.assertEqual(slot_result["artifact_collection_error"], ledger["artifact_collection_error"])
            self.assertEqual(slot_result["runtime_evidence"]["status"], "incomplete")
            self.assertEqual(slot_result["immutable_plan_sha256"], immutable_plan_sha256(plan))
            adapter_failure = json.loads((first_root / "runtime/adapter-failure.json").read_bytes())
            self.assertEqual(adapter_failure["benchmark_id"], plan["benchmark_id"])
            self.assertEqual(adapter_failure["immutable_plan_sha256"], immutable_plan_sha256(plan))
            self.assertEqual(ledger["immutable_plan_sha256"], slot_result["immutable_plan_sha256"])
            self.assertEqual(ledger["attempt_kind"], slot_result["attempt_kind"])
            self.assertEqual(
                ledger_path.with_suffix(".json.sha256").read_text(encoding="ascii"),
                f"{hashlib.sha256(ledger_path.read_bytes()).hexdigest()}  {ledger_path.name}\n",
            )

    def test_unique_cli_command_text_is_only_a_candidate(self) -> None:
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
        self.assertFalse(parsed["association_complete"])
        self.assertEqual(parsed["association_errors"], ["cli_process_binding_unavailable"])
        self.assertTrue(all("call_id" not in event for event in events[1:]))
        self.assertEqual(events[2]["event"], "open")
        self.assertEqual(events[3]["path"], host_path)

    def test_tool_call_id_never_falls_back_to_thread_or_ambiguous_command(self) -> None:
        host_path = "/tmp/pinned/skill/SKILL.md"
        raw_trace = (
            f'424 execve("/usr/bin/cat", ["cat", "--", "{host_path}"], 0x0, 0x0) = 0\n'
            f'424 openat(AT_FDCWD, "{host_path}", O_RDONLY) = 3<{host_path}>\n'
            "424 read(0x3, 0x7fff0000, 0x10) = 0x10\n"
            "424 +++ exited with 0 +++\n"
        ).encode()
        thread = json.dumps({"type": "thread.started", "thread_id": "thread-1"}) + "\n"
        command = {
            "type": "command_execution",
            "command": f"cat -- {host_path}",
        }
        cli_cases = (
            thread.encode(),
            (
                thread
                + json.dumps({"type": "item.completed", "item": {**command, "id": "tool-1"}})
                + "\n"
                + json.dumps({"type": "item.completed", "item": {**command, "id": "tool-2"}})
                + "\n"
            ).encode(),
        )

        for cli in cli_cases:
            events, parsed = normalized_supervisor_events(raw_trace, cli, TRACE_NORMALIZER)
            self.assertTrue(parsed["complete"])
            self.assertFalse(parsed["association_complete"])
            self.assertTrue(all("call_id" not in event for event in events[1:]))

    def test_child_command_candidate_cannot_overwrite_parent_command_candidate(self) -> None:
        raw_trace = (
            '424 execve("/bin/sh", ["sh", "-c", "echo setup; cat /tmp/x"], 0x0, 0x0) = 0\n'
            "424 clone(child_stack=NULL, flags=SIGCHLD) = 425\n"
            '425 execve("/usr/bin/cat", ["cat", "/tmp/x"], 0x0, 0x0) = 0\n'
            '425 openat(AT_FDCWD, "/tmp/x", O_RDONLY) = 3</tmp/x>\n'
            '425 read(3</tmp/x>, 0x7fff0000, 0x10) = 0x10\n'
            "425 close(3</tmp/x>) = 0\n"
            "425 +++ exited with 0 +++\n"
            "424 +++ exited with 0 +++\n"
        ).encode()
        cli = (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}) + "\n"
            + json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "command-a",
                    "type": "command_execution",
                    "command": "sh -c 'echo setup; cat /tmp/x'",
                },
            }) + "\n"
            + json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "command-b",
                    "type": "command_execution",
                    "command": "cat /tmp/x",
                },
            }) + "\n"
        ).encode()

        events, parsed = normalized_supervisor_events(raw_trace, cli, TRACE_NORMALIZER)

        self.assertTrue(parsed["complete"])
        self.assertFalse(parsed["association_complete"])
        child_events = [event for event in events[1:] if event.get("pid") == 425]
        self.assertTrue(child_events)
        self.assertTrue(all("call_id" not in event for event in events[1:]))
        self.assertTrue(all("call_id" not in event for event in child_events))

    def test_later_cli_text_match_cannot_bind_an_earlier_trace_process(self) -> None:
        raw_trace = (
            '424 execve("/usr/bin/cat", ["cat", "/tmp/later"], 0x0, 0x0) = 0\n'
            '424 openat(AT_FDCWD, "/tmp/later", O_RDONLY) = 3</tmp/later>\n'
            "424 +++ exited with 0 +++\n"
        ).encode()
        cli = (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}) + "\n"
            + json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "later-command",
                    "type": "command_execution",
                    "command": "cat /tmp/later",
                },
            }) + "\n"
        ).encode()

        events, parsed = normalized_supervisor_events(raw_trace, cli, TRACE_NORMALIZER)

        self.assertTrue(parsed["complete"])
        self.assertFalse(parsed["association_complete"])
        self.assertEqual(parsed["association_errors"], ["cli_process_binding_unavailable"])
        self.assertTrue(all("call_id" not in event for event in events[1:]))

    def test_conflicting_duplicate_cli_command_id_is_rejected(self) -> None:
        raw_trace = b'424 execve("/usr/bin/cat", ["cat", "/tmp/b"], 0x0, 0x0) = 0\n'
        cli = (
            json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "same-id",
                    "type": "command_execution",
                    "command": "cat /tmp/a",
                },
            }) + "\n"
            + json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "same-id",
                    "type": "command_execution",
                    "command": "cat /tmp/b",
                },
            }) + "\n"
        ).encode()

        with self.assertRaisesRegex(ValueError, "conflicting commands"):
            batch._command_items(batch._codex_events(cli))

        events, parsed = normalized_supervisor_events(raw_trace, cli, TRACE_NORMALIZER)

        self.assertTrue(parsed["complete"])
        self.assertFalse(parsed["association_complete"])
        self.assertEqual(
            parsed["association_errors"],
            ["cli_command_id_conflict", "cli_process_binding_unavailable"],
        )
        self.assertTrue(all("call_id" not in event for event in events[1:]))

    @unittest.skip("requires adapter validation and the Codex-bundled bwrap runtime before process launch")
    def test_fake_cli_is_wrapped_and_all_outputs_are_sealed(self) -> None:
        if not Path("/usr/bin/strace").is_file():
            self.skipTest("strace is required for the adapter trace test")
        if not Path("/usr/bin/codex").exists():
            self.skipTest("the Codex-bundled bwrap runtime is required for the adapter test")
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root, config = _real_adapter_cli_slot(
                Path(temp_dir), benchmark_id="new-safe-batch.v3",
            )
            fake_home = Path(temp_dir) / "host-home"
            _private_write(fake_home / ".codex" / "auth.json", b"test-only auth placeholder\n")
            with self.assertRaisesRegex(ValueError, "GO authorization gate"):
                execute_slot(run_root, config)
            with mock.patch("pathlib.Path.home", return_value=fake_home):
                result = execute_slot(run_root, config, go_authorized=True)

            self.assertEqual(result["run_status"], "completed")
            self.assertTrue(result["host_output_json_object"])
            self.assertEqual(
                result["runtime_evidence"]["status"],
                "incomplete",
                result.get("runtime_evidence_verification_error"),
            )
            self.assertFalse(result["evidence_valid"])
            self.assertFalse(result["runtime_evidence"]["protocol_violation"])
            self.assertIsNone(result["runtime_evidence"]["verified_loaded"])
            self.assertIsNone(result["runtime_evidence"]["supervisor_exit_code"])
            self.assertEqual(result["runtime_evidence"]["common_binding_status"], "verified")
            start = json.loads((run_root / "runtime/adapter-start.json").read_bytes())
            self.assertEqual(start["benchmark_id"], result["benchmark_id"])
            self.assertEqual(start["immutable_plan_sha256"], result["immutable_plan_sha256"])
            argv = start["argv"]
            self.assertEqual(argv[0], "/usr/bin/strace")
            self.assertIn("--decode-pids=pidns", argv)
            self.assertEqual(argv[argv.index("-s") + 1], str(STRACE_STRING_LIMIT))
            self.assertNotIn("-v", argv)
            self.assertIn(config["bwrap_executable"], argv)
            self.assertIn("--unshare-all", argv)
            self.assertIn("--share-net", argv)
            self.assertIn("--ro-bind", argv)
            self.assertIn("--clearenv", argv)
            setenv_arguments = [
                argv[index:index + 3]
                for index, value in enumerate(argv)
                if value == "--setenv"
            ]
            self.assertIn(["--setenv", "TZ", "UTC0"], setenv_arguments)
            self.assertIn("--ignore-user-config", argv)
            self.assertIn("--ignore-rules", argv)
            self.assertIn('model_providers.custom.name="custom"', argv)
            self.assertIn('model_providers.custom.base_url="http://127.0.0.1:8080/v1"', argv)
            self.assertIn('model_providers.custom.wire_api="responses"', argv)
            self.assertIn("model_providers.custom.requires_openai_auth=true", argv)
            self.assertIn('model_reasoning_effort="high"', argv)
            execution = json.loads((run_root / "runtime/execution.json").read_bytes())
            self.assertEqual(execution["argv"], argv)
            self.assertIn("--decode-pids=pidns", execution["argv"])
            self.assertFalse(execution["trace_association_complete"])
            self.assertEqual(
                execution["trace_association_errors"],
                ["cli_process_binding_unavailable"],
            )
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
            execution = json.loads((run_root / "runtime/execution.json").read_bytes())
            self.assertEqual(execution["benchmark_id"], result["benchmark_id"])
            self.assertEqual(execution["immutable_plan_sha256"], result["immutable_plan_sha256"])
            self.assertEqual(execution["raw_trace_sha256"], sha256_file(run_root / "runtime/strace.log"))
            self.assertEqual(
                execution["supervisor_events_sha256"],
                sha256_file(run_root / "runtime/supervisor-events.jsonl"),
            )
            self.assertEqual(
                execution["normalizer"]["source_sha256"],
                sha256_file(run_root / "artifacts/runtime_trace.py"),
            )
            self.assertEqual(
                execution["source_bundle_manifest_sha256"],
                sha256_file(run_root / "artifacts/source-bundle-manifest.json"),
            )
            self.assertEqual(
                execution["runtime_evidence_sha256"],
                sha256_file(run_root / "runtime-evidence.json"),
            )
            checks = json.loads((run_root / "runtime/codex-home/sandbox-check.json").read_bytes())
            self.assertTrue(all(checks.values()))
            self.assertTrue(checks["prompt_stdin_received"])
            self.assertTrue(checks["timezone_value_is_pinned"])
            self.assertTrue(checks["timezone_effective_utc"])
            self.assertEqual(
                stat.S_IMODE((run_root / "runtime/codex-home/installation_id").stat().st_mode),
                0o600,
            )
            trace_path = run_root / "runtime/strace.log"
            parsed_trace = parse_raw_trace(trace_path.read_bytes())
            self.assertFalse(parsed_trace["complete"])
            self.assertTrue(any(
                item["reason"] == "open_request_and_fd_path_disagree"
                for item in parsed_trace["unknown"]
            ))
            prompt_open = next(
                event for event in parsed_trace["events"]
                if event["event"] == "open"
                and event.get("path") == "/slot/input/prompt.txt"
                and event.get("success") is True
            )
            prompt_dup2 = next(
                event for event in parsed_trace["events"]
                if event["event"] == "fd_operation"
                and event.get("syscall") == "dup2"
                and event.get("result") == 0
                and event["pid"] == prompt_open["pid"]
                and event["trace_line"] > prompt_open["trace_line"]
            )
            codex_exec = next(
                event for event in parsed_trace["events"]
                if event["event"] == "exec"
                and event.get("success") is True
                and event.get("argv", [None])[0] == "/slot/input/codex"
                and event["pid"] == prompt_open["pid"]
                and event["trace_line"] > prompt_dup2["trace_line"]
            )
            prompt_read = next(
                event for event in parsed_trace["events"]
                if event["event"] == "read"
                and event.get("fd") == 0
                and event.get("path") == "/slot/input/prompt.txt"
                and event.get("bytes_read", 0) > 0
                and event["pid"] == prompt_open["pid"]
                and event["trace_line"] > codex_exec["trace_line"]
            )
            self.assertLess(
                prompt_open["trace_line"], prompt_dup2["trace_line"],
            )
            self.assertLess(
                prompt_dup2["trace_line"], codex_exec["trace_line"],
            )
            self.assertLess(
                codex_exec["trace_line"], prompt_read["trace_line"],
            )
            assert any(
                event["event"] == "open"
                and event.get("path") == str(SANDBOX_SKILL_PATH)
                and event.get("success") is False
                for event in parsed_trace["events"]
            )
            self.assertFalse(any(
                event.get("path") == str(SANDBOX_SKILL_PATH)
                and (
                    (event["event"] == "open" and event.get("success") is True)
                    or (event["event"] == "read" and event.get("bytes_read", 0) > 0)
                )
                for event in parsed_trace["events"]
            ))
            evidence = json.loads((run_root / "runtime-evidence.json").read_bytes())
            self.assertEqual(evidence["raw_trace"]["sha256"], sha256_file(trace_path))
            self.assertEqual(
                evidence["supervisor_events"]["sha256"],
                sha256_file(run_root / "runtime/supervisor-events.jsonl"),
            )
            self.assertEqual(
                evidence["codex_events"]["sha256"],
                sha256_file(run_root / "runtime/codex-events.jsonl"),
            )

    @unittest.skip("requires adapter validation and the Codex-bundled bwrap runtime before process launch")
    def test_bwrap_allows_b_to_read_and_verifies_the_pinned_skill(self) -> None:
        if not Path("/usr/bin/strace").is_file():
            self.skipTest("strace is required for the adapter trace test")
        if not Path("/usr/bin/codex").exists():
            self.skipTest("the Codex-bundled bwrap runtime is required for the adapter test")
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root, config = _real_adapter_cli_slot(Path(temp_dir), condition="B")
            fake_home = Path(temp_dir) / "host-home"
            _private_write(fake_home / ".codex" / "auth.json", b"test-only auth placeholder\n")
            with mock.patch("pathlib.Path.home", return_value=fake_home):
                result = execute_slot(run_root, config, go_authorized=True)

            self.assertEqual(result["run_status"], "completed")
            self.assertEqual(
                result["runtime_evidence"]["status"],
                "incomplete",
                result.get("runtime_evidence_verification_error"),
            )
            self.assertFalse(result["evidence_valid"])
            self.assertIsNone(result["runtime_evidence"]["verified_loaded"])
            self.assertEqual(result["runtime_evidence"]["skill_load_status"], "unknown")
            self.assertFalse(result["runtime_evidence"]["protocol_violation"])
            parsed_trace = parse_raw_trace((run_root / "runtime/strace.log").read_bytes())
            self.assertFalse(parsed_trace["complete"])
            self.assertTrue(any(
                item["reason"] == "open_request_and_fd_path_disagree"
                for item in parsed_trace["unknown"]
            ))
            cat_reads = [
                event["event"] == "read"
                and event.get("path") == str(SANDBOX_SKILL_PATH)
                and isinstance(event.get("bytes_read"), int)
                and event["bytes_read"] > 0
                for event in parsed_trace["events"]
            ]
            self.assertTrue(any(cat_reads))
            supervisor_events = [
                json.loads(line)
                for line in (run_root / "runtime/supervisor-events.jsonl").read_text().splitlines()[1:]
            ]
            skill_read = next(
                event for event in supervisor_events
                if event.get("event") == "read"
                and event.get("path") == str(SANDBOX_SKILL_PATH)
                and event.get("bytes_read", 0) > 0
            )
            self.assertNotIn("call_id", skill_read)


class RuntimeEvidenceTests(unittest.TestCase):
    def test_v11_parser_keeps_v7_sidecar_untrusted(self) -> None:
        raw_trace = (
            b'701 open("/synthetic/legacy", O_RDONLY) = 3\n'
            b"701 close(3) = 0\n"
            b"701 close(3) = -1 EBADF (Bad file descriptor)\n"
        )
        parsed = parse_raw_trace(raw_trace)
        closes = [event for event in parsed["events"] if event["event"] == "close"]
        self.assertEqual(parsed["normalizer"]["version"], "11")
        self.assertTrue(closes[1]["known_closed"])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "run"
            root.mkdir(mode=0o700)
            raw_ref = {"path": "runtime/strace.log", "sha256": hashlib.sha256(raw_trace).hexdigest()}
            sidecar = {
                "schema_version": 1,
                "codex_events": {"path": "runtime/codex-events.jsonl", "sha256": "a" * 64},
                "supervisor_events": {
                    "path": "runtime/supervisor-events.jsonl",
                    "sha256": "b" * 64,
                    "source_raw_trace": raw_ref,
                    "normalizer": {
                        "version": "7",
                        "source": {"path": "artifacts/runtime_trace.py", "sha256": "c" * 64},
                    },
                },
                "raw_trace": raw_ref,
                "source_bundle_manifest": {
                    "path": "artifacts/source-bundle-manifest.json",
                    "sha256": "d" * 64,
                },
            }
            batch._write_json(root / "runtime-evidence.json", sidecar)

            with self.assertRaisesRegex(ValueError, "normalizer must bind version 11"):
                batch.verify_runtime_evidence(root, "runtime-evidence.json", {"run_status": "completed"})

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
            (root / "artifacts/tool-run-projector.py").write_bytes(b"projector source\n")
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
                    "tool_run_projector": root / "artifacts/tool-run-projector.py",
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
                {
                    "review_manifest", "common_prompt", "output_schema",
                    "tool_run_projector", "runtime_config",
                },
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
