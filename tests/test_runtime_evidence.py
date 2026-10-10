from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from eval.run_ab_batch import build_runtime_evidence, build_supervisor_header
from eval.runtime_evidence import RuntimeEvidenceError, verify_runtime_evidence
from eval.runtime_trace import parse_raw_trace


SKILL_HOST_PATH = "/workspace/skills/review-changes/SKILL.md"
SKILL_TEXT = "Pinned synthetic skill\n"


def _write_json(path: Path, value: object) -> bytes:
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return data


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalizer_source(run_dir: Path) -> Path:
    source = Path(__import__("eval.runtime_trace", fromlist=["__file__"]).__file__)
    target = run_dir / "runtime-normalizer.py"
    target.write_bytes(source.read_bytes())
    target.chmod(0o600)
    return target


def _refresh_trace_artifacts(
    run_dir: Path,
    raw: bytes,
    *,
    reader_call_id: str = "cat-call",
    skill_host_path: str = SKILL_HOST_PATH,
) -> dict:
    runtime = run_dir / "runtime"
    raw_trace = runtime / "strace.log"
    raw_trace.write_bytes(raw)
    raw_trace.chmod(0o600)
    parsed = parse_raw_trace(raw)
    events = parsed["events"]
    cli_exec = next(
        event for event in events
        if event["event"] == "exec" and Path(event["argv"][0]).name == "codex"
    )
    reader_exec = next(
        event for event in events
        if event["event"] == "exec" and Path(event["argv"][0]).name in {"cat", "head"}
    )
    supervisor_events = runtime / "supervisor-events.jsonl"
    normalizer_source = run_dir / "runtime-normalizer.py"
    normalized = [build_supervisor_header(raw, normalizer_source)]
    for event in events:
        call_id = "cli-call"
        if event["event"] == "process_exit" and event["pid"] == cli_exec["pid"]:
            call_id = "cli-call"
        elif (
            event["pid"] == reader_exec["pid"]
            and event["trace_line"] >= reader_exec["trace_line"]
        ):
            call_id = reader_call_id
        normalized.append({**event, "call_id": call_id})
    supervisor_events.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in normalized),
        encoding="utf-8",
    )
    supervisor_events.chmod(0o600)

    artifact_dir = run_dir / "artifacts"
    binding_paths = {
        name: artifact_dir / filename
        for name, filename in (
            ("review_manifest", "reviewer-input.json"),
            ("common_prompt", "common-prompt.txt"),
            ("output_schema", "output-schema.json"),
            ("runtime_config", "runtime-config.json"),
        )
    }
    sidecar = build_runtime_evidence(
        run_dir,
        runtime / "codex-events.jsonl",
        supervisor_events,
        raw_trace,
        artifact_dir / "source-bundle-manifest.json",
        normalizer_source,
        common_bindings=binding_paths,
        pinned_skill_host_path=skill_host_path,
    )
    _write_json(run_dir / "runtime-evidence.json", sidecar)
    return parsed


def _runtime_fixture(
    tmp_path: Path,
    *,
    cli_exit_code: int = 0,
    skill_host_path: str = SKILL_HOST_PATH,
    skill_text: str = SKILL_TEXT,
) -> tuple[Path, dict, dict]:
    run_dir = tmp_path / "run"
    runtime = run_dir / "runtime"
    artifacts = run_dir / "artifacts"
    runtime.mkdir(parents=True)
    artifacts.mkdir()
    run_dir.chmod(0o700)
    runtime.chmod(0o700)
    artifacts.chmod(0o700)

    skill_bundle = artifacts / "skill-bundle.zip"
    with zipfile.ZipFile(skill_bundle, "w") as archive:
        archive.writestr("review-changes/SKILL.md", skill_text)
    skill_bundle.chmod(0o600)
    skill_bundle_digest = _sha(skill_bundle.read_bytes())
    member_digest = _sha(skill_text.encode("utf-8"))

    binding_paths = {
        "review_manifest": artifacts / "reviewer-input.json",
        "common_prompt": artifacts / "common-prompt.txt",
        "output_schema": artifacts / "output-schema.json",
        "runtime_config": artifacts / "runtime-config.json",
    }
    binding_payloads = {
        "review_manifest": b'{"review":"input"}\n',
        "common_prompt": b"Review the selected changes.\n",
        "output_schema": b'{"type":"object"}\n',
        "runtime_config": b'{"model":"requested-model"}\n',
    }
    for name, path in binding_paths.items():
        path.write_bytes(binding_payloads[name])
        path.chmod(0o600)

    raw_trace_lines = [
        '101 execve("/usr/bin/codex", ["codex", "exec"], 0x0, 0x0) = 0',
        f'102 execve("/usr/bin/cat", ["cat", "--", "{skill_host_path}"], 0x0, 0x0) = 0',
        f'102 openat(AT_FDCWD, "{skill_host_path}", O_RDONLY|O_CLOEXEC) = 3<{skill_host_path}>',
        f"102 read(0x3, 0x7fff0000, 0x{len(skill_text.encode('utf-8')):x}) = 0x{len(skill_text.encode('utf-8')):x}",
        "102 close(0x3) = 0",
        "102 +++ exited with 0 +++",
        f"101 +++ exited with {cli_exit_code} +++",
    ]
    raw_trace = runtime / "strace.log"
    raw_trace.write_text("\n".join(raw_trace_lines) + "\n", encoding="utf-8")
    raw_trace.chmod(0o600)

    codex_events = runtime / "codex-events.jsonl"
    codex_events.write_text(
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "cat-call",
                "command": f"cat -- {skill_host_path}",
                "aggregated_output": skill_text,
            },
        }) + "\n",
        encoding="utf-8",
    )
    codex_events.chmod(0o600)
    normalizer_source = _normalizer_source(run_dir)

    manifest = {
        "schema_version": 1,
        "files": [
            {"path": path.relative_to(run_dir).as_posix(), "sha256": _sha(path.read_bytes())}
            for path in (*binding_paths.values(), skill_bundle)
        ],
        "skill_resource": {
            "host_path": skill_host_path,
            "zip_path": "artifacts/skill-bundle.zip",
            "zip_sha256": skill_bundle_digest,
            "zip_member": "review-changes/SKILL.md",
            "member_sha256": member_digest,
        },
    }
    manifest_path = artifacts / "source-bundle-manifest.json"
    _write_json(manifest_path, manifest)
    _refresh_trace_artifacts(
        run_dir, raw_trace.read_bytes(), skill_host_path=skill_host_path
    )

    record = {
        "condition": "current_skill",
        "run_status": "completed",
        "input": {"review_manifest_sha256": _sha(binding_payloads["review_manifest"])},
        "artifacts": {"skill_bundle": "artifacts/skill-bundle.zip"},
        "skill_provenance": {"bundle_sha256": skill_bundle_digest},
    }
    return run_dir, record, manifest


def test_skill_load_requires_cli_event_os_read_and_exact_zip_member_output(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert evidence["status"] == "verified"
    assert evidence["supervisor_trace_status"] == "success"
    assert evidence["host_reported_completed"] is True
    assert evidence["verified_loaded"] is True
    assert evidence["common_binding_status"] == "verified"
    assert evidence["common_bindings"]["runtime_config"] == _sha(
        (run_dir / "artifacts/runtime-config.json").read_bytes()
    )


def test_missing_call_id_is_incomplete_and_does_not_verify_skill_load(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    sidecar_path = run_dir / "runtime-evidence.json"
    sidecar = json.loads(sidecar_path.read_bytes())
    supervisor_path = run_dir / sidecar["supervisor_events"]["path"]
    events = [json.loads(line) for line in supervisor_path.read_text().splitlines()]
    events[2].pop("call_id")
    supervisor_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    sidecar["supervisor_events"]["sha256"] = _sha(supervisor_path.read_bytes())
    _write_json(sidecar_path, sidecar)

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert evidence["status"] == "incomplete"
    assert evidence["supervisor_trace_status"] == "success"
    assert evidence["verified_loaded"] is None


def test_raw_trace_digest_mismatch_rejects_runtime_scoring(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    with (run_dir / "runtime/strace.log").open("ab") as trace:
        trace.write(b"tampered\n")

    with pytest.raises(RuntimeEvidenceError, match="raw_trace SHA-256 does not match"):
        verify_runtime_evidence(run_dir, "runtime-evidence.json", record)


def test_host_claimed_bundle_hash_without_runtime_evidence_remains_unknown(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    record["skill_provenance"] = {
        "bundle_sha256": "a" * 64,
        "host_reported_bundle_sha256": "a" * 64,
        "verified_loaded": True,
    }

    evidence = verify_runtime_evidence(run_dir, None, record)

    assert evidence["status"] == "missing"
    assert evidence["verified_loaded"] is None


def test_host_completion_and_supervisor_exit_are_reported_separately(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path, cli_exit_code=7)

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert evidence["host_reported_completed"] is True
    assert evidence["supervisor_trace_status"] == "failed"
    assert evidence["supervisor_exit_code"] == 7


def test_direct_condition_reading_skill_is_a_protocol_violation(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    record["condition"] = "same_host_direct"

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert evidence["protocol_violation"] is True
    assert evidence["skill_load_status"] == "protocol_violation"
    assert evidence["verified_loaded"] is None


def test_direct_non_cat_reading_pinned_skill_is_a_protocol_violation(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    record["condition"] = "same_host_direct"
    raw_trace = run_dir / "runtime/strace.log"
    raw = raw_trace.read_text(encoding="utf-8").replace(
        'execve("/usr/bin/cat", ["cat", "--",',
        'execve("/usr/bin/head", ["head", "--",',
    )
    codex_events = run_dir / "runtime/codex-events.jsonl"
    item = json.loads(codex_events.read_text(encoding="utf-8"))
    item["item"]["id"] = "head-call"
    item["item"]["command"] = f"head -- {SKILL_HOST_PATH}"
    _write_jsonl = json.dumps(item, sort_keys=True) + "\n"
    codex_events.write_text(_write_jsonl, encoding="utf-8")
    codex_events.chmod(0o600)
    _refresh_trace_artifacts(run_dir, raw.encode("utf-8"), reader_call_id="head-call")

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert evidence["protocol_violation"] is True
    assert evidence["skill_load_status"] == "protocol_violation"
    assert evidence["verified_loaded"] is None


def test_direct_readable_mmap_of_pinned_skill_is_a_protocol_violation(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    record["condition"] = "same_host_direct"
    raw = "\n".join([
        f'101 execve("/usr/bin/codex", ["codex", "exec"], 0x0, 0x0) = 0',
        f'102 execve("/usr/bin/cat", ["cat", "--", "{SKILL_HOST_PATH}"], 0x0, 0x0) = 0',
        f'102 openat(AT_FDCWD, "{SKILL_HOST_PATH}", O_RDONLY|O_CLOEXEC) = 3',
        "102 mmap(NULL, 4096, PROT_READ, MAP_PRIVATE, 3, 0) = 0x7f000000",
        "102 close(3) = 0",
        "102 +++ exited with 0 +++",
        "101 +++ exited with 0 +++",
        "",
    ]).encode("utf-8")
    _refresh_trace_artifacts(run_dir, raw)

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert evidence["protocol_violation"] is True
    assert evidence["skill_load_status"] == "protocol_violation"
    assert evidence["verified_loaded"] is None


@pytest.mark.parametrize("break_association", ["missing_open", "closed_before_read"])
def test_unattributed_read_cannot_prove_skill_load(
    tmp_path: Path, break_association: str
) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    raw_path = run_dir / "runtime/strace.log"
    lines = raw_path.read_text(encoding="utf-8").splitlines()
    if break_association == "missing_open":
        lines = [line for line in lines if " openat(" not in line]
    else:
        close_index = next(index for index, line in enumerate(lines) if " close(" in line)
        close_line = lines.pop(close_index)
        read_index = next(index for index, line in enumerate(lines) if " read(" in line)
        lines.insert(read_index, close_line)
    _refresh_trace_artifacts(run_dir, ("\n".join(lines) + "\n").encode("utf-8"))

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert evidence["status"] == "verified"
    assert evidence["verified_loaded"] is None


def test_unknown_inherited_fd_read_is_a_path_gap_not_a_skill_load_proof(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    raw = "\n".join([
        '101 execve("/usr/bin/codex", ["codex", "exec"], 0x0, 0x0) = 0',
        f'102 execve("/usr/bin/cat", ["cat", "--", "{SKILL_HOST_PATH}"], 0x0, 0x0) = 0',
        "102 read(0x3, 0x7fff0000, 0x17) = 0x17",
        "102 +++ exited with 0 +++",
        "101 +++ exited with 0 +++",
        "",
    ]).encode("utf-8")
    parsed = _refresh_trace_artifacts(run_dir, raw)

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert parsed["complete"] is True
    assert parsed["attribution_unknown"]
    assert evidence["status"] == "verified"
    assert evidence["verified_loaded"] is None
    assert any("path attribution" in item for item in evidence["limitations"])


def test_decoded_read_buffer_makes_runtime_evidence_incomplete(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    raw_path = run_dir / "runtime/strace.log"
    raw = raw_path.read_text(encoding="utf-8").replace(
        "read(0x3, 0x7fff0000, 0x17) = 0x17",
        'read(0x3, "Pinned synthetic skill\\n", 0x17) = 0x17',
    )
    _refresh_trace_artifacts(run_dir, raw.encode("utf-8"))

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert evidence["status"] == "incomplete"
    assert evidence["verified_loaded"] is None


def test_supervisor_event_list_must_exactly_match_trusted_parser(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    sidecar_path = run_dir / "runtime-evidence.json"
    sidecar = json.loads(sidecar_path.read_bytes())
    supervisor_path = run_dir / sidecar["supervisor_events"]["path"]
    events = [json.loads(line) for line in supervisor_path.read_text().splitlines()]
    events.append({"event": "read", "pid": 102, "call_id": "cat-call", "path": SKILL_HOST_PATH})
    supervisor_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    supervisor_path.chmod(0o600)
    sidecar["supervisor_events"]["sha256"] = _sha(supervisor_path.read_bytes())
    _write_json(sidecar_path, sidecar)

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert evidence["status"] == "incomplete"
    assert evidence["verified_loaded"] is None


@pytest.mark.parametrize("target", ["sidecar", "runtime_directory"])
def test_runtime_evidence_requires_private_artifact_permissions(
    tmp_path: Path, target: str
) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    if target == "sidecar":
        (run_dir / "runtime-evidence.json").chmod(0o644)
        expected = "runtime evidence sidecar must have mode 0600"
    else:
        (run_dir / "runtime").chmod(0o755)
        expected = "directories must have mode 0700"

    with pytest.raises(RuntimeEvidenceError, match=expected):
        verify_runtime_evidence(run_dir, "runtime-evidence.json", record)


def test_normalizer_source_must_match_trusted_parser(tmp_path: Path) -> None:
    run_dir, record, _manifest = _runtime_fixture(tmp_path)
    sidecar_path = run_dir / "runtime-evidence.json"
    sidecar = json.loads(sidecar_path.read_bytes())
    source_ref = sidecar["supervisor_events"]["normalizer"]["source"]
    source_path = run_dir / source_ref["path"]
    source_path.write_bytes(b"# substituted parser\n")
    source_path.chmod(0o600)
    source_ref["sha256"] = _sha(source_path.read_bytes())
    _write_json(sidecar_path, sidecar)

    with pytest.raises(RuntimeEvidenceError, match="trusted runtime parser"):
        verify_runtime_evidence(run_dir, "runtime-evidence.json", record)


def test_real_strace_subprocess_capture_proves_exact_pinned_skill_load(tmp_path: Path) -> None:
    strace = shutil.which("strace")
    bash = shutil.which("bash")
    if strace is None or bash is None:
        pytest.skip("strace and bash are required for the raw-trace integration test")

    skill_text = "strace-only-synthetic-payload"
    skill_path = (tmp_path / "pinned-skill.md").resolve()
    skill_path.write_text(skill_text, encoding="utf-8")
    run_dir, record, _manifest = _runtime_fixture(
        tmp_path,
        skill_host_path=str(skill_path),
        skill_text=skill_text,
    )
    runtime = run_dir / "runtime"
    codex_events = runtime / "codex-events.jsonl"
    raw_trace = runtime / "strace.log"
    fake_cli = run_dir / "fake_cli.py"
    fake_cli.write_text(
        "import json, subprocess, sys\n"
        "skill_path, output, events_path = sys.argv[1:]\n"
        "completed = subprocess.run(['/bin/cat', '--', skill_path], capture_output=True, check=True)\n"
        "item = {'type': 'item.completed', 'item': {\n"
        "    'type': 'command_execution', 'id': 'cat-call',\n"
        "    'command': f'cat -- {skill_path}', 'aggregated_output': completed.stdout.decode('utf-8'),\n"
        "}}\n"
        "with open(events_path, 'w', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(item) + '\\n')\n"
        "sys.stdout.buffer.write(completed.stdout)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o600)
    launcher = 'exec -a codex "$@"'
    old_umask = os.umask(0o077)
    try:
        run = subprocess.run(
            [
                strace,
                "-f",
                "-yy",
                "-s",
                "4096",
                "-e",
                "raw=read,pread64,readv,preadv,preadv2",
                "-o",
                str(raw_trace),
                "--",
                bash,
                "-c",
                launcher,
                "codex",
                shutil.which("python3") or "/usr/bin/python3",
                str(fake_cli),
                str(skill_path),
                skill_text,
                str(codex_events),
            ],
            capture_output=True,
            check=False,
            timeout=20,
        )
    finally:
        os.umask(old_umask)
    assert run.returncode == 0, run.stderr.decode("utf-8", errors="replace")
    assert run.stdout == skill_text.encode("utf-8")
    raw_trace.chmod(0o600)
    codex_events.chmod(0o600)
    parsed = _refresh_trace_artifacts(
        run_dir, raw_trace.read_bytes(), skill_host_path=str(skill_path)
    )
    assert parsed["complete"] is True, parsed["unknown"]
    assert any(event["event"] == "pipe" for event in parsed["events"])
    assert any(
        event["event"] == "read" and event.get("fd_kind") == "pipe"
        for event in parsed["events"]
    )

    evidence = verify_runtime_evidence(run_dir, "runtime-evidence.json", record)

    assert evidence["status"] == "verified"
    assert evidence["supervisor_trace_status"] == "success"
    assert evidence["verified_loaded"] is True
