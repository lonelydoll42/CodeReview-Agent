"""Validate independently captured runtime evidence for A/B review runs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import zipfile
from pathlib import Path
from typing import Any

if __package__:
    from .runtime_trace import normalizer_identity, parse_raw_trace
else:
    from runtime_trace import normalizer_identity, parse_raw_trace


NORMALIZER_VERSION = "2"
COMMON_BINDING_NAMES = (
    "review_manifest",
    "common_prompt",
    "output_schema",
    "runtime_config",
)
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RuntimeEvidenceError(ValueError):
    """Raised when a recorded runtime artifact no longer matches its binding."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_ref(run_dir: Path, reference: Any, field: str) -> tuple[Path, bytes]:
    _validate_ref(reference, field)
    relative = Path(reference["path"])
    if relative.is_absolute() or ".." in relative.parts or "\\" in reference["path"]:
        raise RuntimeEvidenceError(f"{field}.path must stay inside the run directory")
    root = run_dir.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeEvidenceError(f"{field}.path escapes the run directory") from exc
    if not path.is_file():
        raise RuntimeEvidenceError(f"{field} references a missing file: {reference['path']}")
    _assert_private_path(root, path, field)
    data = path.read_bytes()
    if sha256_bytes(data) != reference["sha256"]:
        raise RuntimeEvidenceError(f"{field} SHA-256 does not match: {reference['path']}")
    return path, data


def _assert_private_path(root: Path, path: Path, field: str) -> None:
    if os.name != "posix":
        return
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise RuntimeEvidenceError(f"{field} must have mode 0600")
        directory = path.parent
        while True:
            if stat.S_IMODE(directory.stat().st_mode) != 0o700:
                raise RuntimeEvidenceError(f"{field} directories must have mode 0700")
            if directory == root:
                return
            directory.relative_to(root)
            directory = directory.parent
    except RuntimeEvidenceError:
        raise
    except (OSError, ValueError) as exc:
        raise RuntimeEvidenceError(f"{field} permissions cannot be verified") from exc


def _validate_ref(value: Any, field: str) -> None:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise RuntimeEvidenceError(f"{field} must contain exactly path and sha256")
    if not isinstance(value["path"], str) or not value["path"]:
        raise RuntimeEvidenceError(f"{field}.path must be non-empty")
    if not isinstance(value["sha256"], str) or not _HEX_SHA256.fullmatch(value["sha256"]):
        raise RuntimeEvidenceError(f"{field}.sha256 must be a lowercase SHA-256 digest")


def validate_sidecar_shape(value: Any) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeEvidenceError("runtime evidence must be a schema_version 1 object")
    expected = {
        "schema_version", "codex_events", "supervisor_events", "raw_trace",
        "source_bundle_manifest",
    }
    optional = {"common_bindings", "pinned_skill_host_path"}
    if not expected <= value.keys() or value.keys() - expected - optional:
        raise RuntimeEvidenceError("runtime evidence has unexpected or missing fields")
    for name in ("codex_events", "raw_trace", "source_bundle_manifest"):
        _validate_ref(value.get(name), name)
    supervisor = value.get("supervisor_events")
    if not isinstance(supervisor, dict) or set(supervisor) != {
        "path", "sha256", "source_raw_trace", "normalizer"
    }:
        raise RuntimeEvidenceError("supervisor_events has unexpected or missing fields")
    _validate_ref({"path": supervisor.get("path"), "sha256": supervisor.get("sha256")}, "supervisor_events")
    _validate_ref(supervisor.get("source_raw_trace"), "supervisor_events.source_raw_trace")
    normalizer = supervisor.get("normalizer")
    if (
        not isinstance(normalizer, dict)
        or set(normalizer) != {"version", "source"}
        or normalizer.get("version") != NORMALIZER_VERSION
    ):
        raise RuntimeEvidenceError("supervisor_events.normalizer must bind version 2")
    _validate_ref(normalizer.get("source"), "supervisor_events.normalizer.source")
    if supervisor["source_raw_trace"] != value["raw_trace"]:
        raise RuntimeEvidenceError("normalized supervisor events must bind the raw trace reference")
    bindings = value.get("common_bindings")
    if bindings is not None:
        if not isinstance(bindings, dict) or bindings.keys() - set(COMMON_BINDING_NAMES):
            raise RuntimeEvidenceError("common_bindings has an unsupported shape")
        for name, reference in bindings.items():
            _validate_ref(reference, f"common_bindings.{name}")
    pinned_skill_path = value.get("pinned_skill_host_path")
    if pinned_skill_path is not None and (
        not isinstance(pinned_skill_path, str) or not pinned_skill_path.startswith("/")
    ):
        raise RuntimeEvidenceError("pinned_skill_host_path must be an absolute path")


def _json_lines(data: bytes, field: str) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeEvidenceError(f"{field} is not UTF-8 JSONL") from exc
    values: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeEvidenceError(f"{field} line {number} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeEvidenceError(f"{field} line {number} is not a JSON object")
        values.append(value)
    return values


def _event_name(event: dict[str, Any]) -> str | None:
    name = event.get("event")
    if isinstance(name, str):
        return name.lower()
    return None


def _without_call_id(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "call_id"}


def _call_id(event: dict[str, Any]) -> str | None:
    for key in ("call_id", "callId"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    item = event.get("item")
    if isinstance(item, dict):
        for key in ("call_id", "callId", "id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    value = event.get("id")
    return value if isinstance(value, str) and value else None


def _command_item(event: dict[str, Any]) -> dict[str, Any] | None:
    item = event.get("item")
    candidate = item if isinstance(item, dict) else event
    item_type = candidate.get("type") or event.get("type") or event.get("event")
    if item_type not in {"command_execution", "command", "tool_call", "command.completed"}:
        return None
    command = candidate.get("command") or candidate.get("input")
    if not isinstance(command, str):
        return None
    output = candidate.get("aggregated_output")
    if not isinstance(output, str):
        output = candidate.get("output") if isinstance(candidate.get("output"), str) else None
    return {"call_id": _call_id(event) or _call_id(candidate), "command": command, "output": output}


def _cat_path(command: str) -> str | None:
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or Path(argv[0]).name != "cat":
        return None
    args = argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if len(args) != 1 or not args[0].startswith("/"):
        return None
    return args[0]


def _bundle_manifest(data: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeEvidenceError("source bundle manifest is not valid JSON")
    if not isinstance(value, dict):
        raise RuntimeEvidenceError("source bundle manifest must be an object")
    entries = value.get("files")
    if entries is not None:
        if not isinstance(entries, list):
            raise RuntimeEvidenceError("source bundle manifest files must be an array")
        for number, entry in enumerate(entries):
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or not entry["path"]
                or not isinstance(entry.get("sha256"), str)
                or not _HEX_SHA256.fullmatch(entry["sha256"])
            ):
                raise RuntimeEvidenceError(f"source bundle manifest files[{number}] is invalid")
    skill_resource = value.get("skill_resource")
    if skill_resource is not None:
        required = {"host_path", "zip_path", "zip_sha256", "zip_member", "member_sha256"}
        if (
            not isinstance(skill_resource, dict)
            or set(skill_resource) != required
            or not isinstance(skill_resource.get("host_path"), str)
            or not skill_resource["host_path"].startswith("/")
            or not isinstance(skill_resource.get("zip_path"), str)
            or not isinstance(skill_resource.get("zip_member"), str)
            or any(
                not isinstance(skill_resource.get(name), str)
                or not _HEX_SHA256.fullmatch(skill_resource[name])
                for name in ("zip_sha256", "member_sha256")
            )
        ):
            raise RuntimeEvidenceError("source bundle manifest skill_resource is invalid")
    return value


def _bundle_members(
    run_dir: Path, record: dict[str, Any], manifest: dict[str, Any]
) -> tuple[bytes, str] | None:
    resource = manifest.get("skill_resource")
    artifacts = record.get("artifacts", {})
    provenance = record.get("skill_provenance")
    reference = artifacts.get("skill_bundle") if isinstance(artifacts, dict) else None
    if not isinstance(resource, dict) or not isinstance(reference, str) or not isinstance(provenance, dict):
        return None
    if resource["zip_path"] != reference:
        raise RuntimeEvidenceError("source manifest Skill ZIP path does not match the run artifact")
    source_files = manifest.get("files")
    manifest_files = {
        entry["path"]: entry["sha256"]
        for entry in (source_files if isinstance(source_files, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    if manifest_files.get(resource["zip_path"]) != resource["zip_sha256"]:
        raise RuntimeEvidenceError("source manifest does not bind the pinned Skill ZIP")
    bundle_path = Path(reference)
    if bundle_path.is_absolute() or ".." in bundle_path.parts:
        raise RuntimeEvidenceError("pinned Skill bundle reference must stay inside the run directory")
    bundle_path = (run_dir.resolve() / bundle_path).resolve()
    try:
        bundle_path.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise RuntimeEvidenceError("pinned Skill bundle escapes the run directory") from exc
    if not bundle_path.is_file():
        return None
    _assert_private_path(run_dir.resolve(), bundle_path, "pinned Skill bundle")
    bundle = bundle_path.read_bytes()
    bundle_hash = sha256_bytes(bundle)
    if bundle_hash != resource["zip_sha256"] or bundle_hash != provenance.get("bundle_sha256"):
        raise RuntimeEvidenceError("pinned Skill ZIP digest does not match runtime/source bindings")
    try:
        with zipfile.ZipFile(bundle_path) as archive:
            if archive.namelist().count(resource["zip_member"]) != 1:
                raise RuntimeEvidenceError("pinned Skill ZIP member is missing or duplicated")
            member = archive.read(resource["zip_member"])
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise RuntimeEvidenceError("pinned Skill ZIP member is missing or invalid") from exc
    if sha256_bytes(member) != resource["member_sha256"]:
        raise RuntimeEvidenceError("pinned Skill ZIP member digest does not match its manifest")
    return member, resource["host_path"]


def verify_runtime_evidence(
    run_dir: Path,
    sidecar_reference: str | None,
    record: dict[str, Any],
) -> dict[str, Any]:
    """Recompute trace status, common bindings, and Skill-load evidence from bytes."""
    if sidecar_reference is None:
        return {
            "status": "missing",
            "supervisor_trace_status": "unknown",
            "supervisor_exit_code": None,
            "host_reported_completed": record.get("run_status") == "completed",
            "skill_load_status": "unknown",
            "verified_loaded": None,
            "protocol_violation": False,
            "common_binding_status": "incomplete",
            "common_bindings": None,
            "limitations": ["runtime evidence sidecar is absent"],
        }
    sidecar_path = Path(sidecar_reference)
    if sidecar_path.is_absolute() or ".." in sidecar_path.parts:
        raise RuntimeEvidenceError("runtime evidence reference must stay inside the run directory")
    sidecar_path = (run_dir.resolve() / sidecar_path).resolve()
    try:
        sidecar_path.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise RuntimeEvidenceError("runtime evidence reference escapes the run directory") from exc
    if not sidecar_path.is_file():
        raise RuntimeEvidenceError(f"runtime evidence sidecar is missing: {sidecar_reference}")
    _assert_private_path(run_dir.resolve(), sidecar_path, "runtime evidence sidecar")
    try:
        sidecar = json.loads(sidecar_path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeEvidenceError("runtime evidence sidecar is invalid JSON") from exc
    validate_sidecar_shape(sidecar)

    codex_path, codex_bytes = _read_ref(run_dir, sidecar["codex_events"], "codex_events")
    supervisor_path, supervisor_bytes = _read_ref(
        run_dir, {"path": sidecar["supervisor_events"]["path"], "sha256": sidecar["supervisor_events"]["sha256"]},
        "supervisor_events",
    )
    raw_path, raw_bytes = _read_ref(run_dir, sidecar["raw_trace"], "raw_trace")
    manifest_path, manifest_bytes = _read_ref(
        run_dir, sidecar["source_bundle_manifest"], "source_bundle_manifest"
    )
    normalizer_path, _normalizer_bytes = _read_ref(
        run_dir,
        sidecar["supervisor_events"]["normalizer"]["source"],
        "normalizer_source",
    )
    del codex_path, supervisor_path, raw_path, manifest_path, normalizer_path

    normalizer = sidecar["supervisor_events"]["normalizer"]
    parser_identity = normalizer_identity()
    if (
        normalizer["version"] != parser_identity["version"]
        or normalizer["source"]["sha256"] != parser_identity["source_sha256"]
        or _normalizer_bytes != Path(__file__).with_name("runtime_trace.py").read_bytes()
    ):
        raise RuntimeEvidenceError("normalizer source does not match the trusted runtime parser")
    parsed_trace = parse_raw_trace(raw_bytes)
    supervisor_events = _json_lines(supervisor_bytes, "supervisor_events")
    header = supervisor_events[0] if supervisor_events else None
    normalizer = sidecar["supervisor_events"]["normalizer"]
    expected_header = {
        "event": "normalizer_header",
        "schema_version": 1,
        "source_raw_trace_sha256": sha256_bytes(raw_bytes),
        "normalizer": {
            "version": normalizer["version"],
            "source_sha256": normalizer["source"]["sha256"],
        },
    }
    if header is not None and header != expected_header:
        raise RuntimeEvidenceError("normalizer header does not match raw trace and source bindings")
    events = supervisor_events[1:] if header == expected_header else []
    parsed_events = parsed_trace["events"]
    has_association_gap = (
        header != expected_header
        or not parsed_trace["complete"]
        or bool(parsed_trace["unknown"])
        or len(events) != len(parsed_events)
    )
    valid_trace_events: list[dict[str, Any]] = []
    for index, parsed_event in enumerate(parsed_events):
        if index >= len(events):
            has_association_gap = True
            continue
        supervisor_event = events[index]
        if _without_call_id(supervisor_event) != parsed_event:
            has_association_gap = True
            continue
        call_id = supervisor_event.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            has_association_gap = True
            continue
        valid_trace_events.append({**parsed_event, "call_id": call_id})

    codex_events = _json_lines(codex_bytes, "codex_events")
    command_items = [item for event in codex_events if (item := _command_item(event)) is not None]
    codex_processes = [
        event for event in valid_trace_events
        if _event_name(event) in {"exec", "execve"}
        and isinstance(event.get("argv"), list)
        and event["argv"]
        and Path(event["argv"][0]).name in {"codex", "codex.exe"}
        and isinstance(_call_id(event), str)
    ]
    trace_status = "unknown"
    trace_exit_code = None
    if len(codex_processes) == 1:
        process = codex_processes[0]
        exits = [
            event for event in valid_trace_events
            if _event_name(event) in {"process_exit", "exit"}
            and event.get("pid") == process.get("pid")
            and _call_id(event) == _call_id(process)
        ]
        if len(exits) == 1:
            trace_exit_code = exits[0].get("exit_code")
            trace_status = "success" if trace_exit_code == 0 else "failed"
        else:
            has_association_gap = True
    else:
        has_association_gap = True

    manifest = _bundle_manifest(manifest_bytes)
    common_bindings: dict[str, str] | None = None
    binding_status = "incomplete"
    binding_refs = sidecar.get("common_bindings")
    if isinstance(binding_refs, dict):
        common_bindings = {}
        manifest_entries = manifest.get("files") if manifest else None
        manifest_files = {
            entry["path"]: entry["sha256"]
            for entry in (manifest_entries if isinstance(manifest_entries, list) else [])
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        } if manifest else {}
        for name, reference in binding_refs.items():
            _path, _data = _read_ref(run_dir, reference, f"common_bindings.{name}")
            common_bindings[name] = reference["sha256"]
            manifest_digest = manifest_files.get(reference["path"])
            if manifest_digest is None:
                has_association_gap = True
            elif manifest_digest != reference["sha256"]:
                raise RuntimeEvidenceError(
                    f"source bundle manifest digest does not match common binding {name}"
                )
        if set(binding_refs) == set(COMMON_BINDING_NAMES):
            input_digest = record.get("input", {}).get("review_manifest_sha256")
            if common_bindings.get("review_manifest") != input_digest:
                raise RuntimeEvidenceError("runtime review manifest does not match the run input digest")
            binding_status = "verified" if all(
                manifest_files.get(reference["path"]) == reference["sha256"]
                for reference in binding_refs.values()
            ) else "incomplete"

    skill_loaded: bool | None = None
    protocol_violation = False
    skill_status = "unknown"
    resource = manifest.get("skill_resource") if manifest else None
    skill_bundle = _bundle_members(run_dir, record, manifest) if manifest else None
    host_skill_path = sidecar.get("pinned_skill_host_path")
    if isinstance(resource, dict) and host_skill_path is not None and resource["host_path"] != host_skill_path:
        raise RuntimeEvidenceError("pinned Skill host path differs from the source bundle manifest")
    if host_skill_path is None and isinstance(resource, dict):
        host_skill_path = resource["host_path"]
    matching_cats: list[tuple[dict[str, Any], str]] = []
    for item in command_items:
        cat_path = _cat_path(item["command"])
        if cat_path is not None and host_skill_path is not None and cat_path == host_skill_path:
            matching_cats.append((item, cat_path))
    pinned_resource_accesses = [
        event for event in parsed_events
        if event.get("path") == host_skill_path and (
            (
                _event_name(event) == "open"
                and event.get("success") is True
                and event.get("readable") is not False
            )
            or (
                _event_name(event) == "mmap"
                and event.get("success") is True
                and event.get("readable") is True
            )
            or (
                _event_name(event) == "read"
                and isinstance(event.get("bytes_read"), int)
                and event["bytes_read"] > 0
            )
        )
    ] if host_skill_path is not None else []
    if record.get("condition") == "same_host_direct" and pinned_resource_accesses:
        protocol_violation = True
        skill_status = "protocol_violation"

    if len(matching_cats) == 1:
        item, cat_path = matching_cats[0]
        call_id = item.get("call_id")
        cat_execs = [
            event for event in valid_trace_events
            if _event_name(event) in {"exec", "execve"}
            and _call_id(event) == call_id
            and isinstance(event.get("argv"), list)
            and event["argv"]
            and Path(event["argv"][0]).name == "cat"
            and cat_path in event["argv"]
        ] if isinstance(call_id, str) else []
        if len(cat_execs) == 1:
            cat_exec = cat_execs[0]
            successful_reads = [
                event for event in valid_trace_events
                if _event_name(event) == "read"
                and event.get("pid") == cat_exec.get("pid")
                and _call_id(event) == call_id
                and event.get("path") == cat_path
                and isinstance(event.get("bytes_read"), int)
                and event["bytes_read"] > 0
            ]
            if successful_reads and record.get("condition") != "same_host_direct":
                if (
                    skill_bundle is not None
                    and cat_path == skill_bundle[1]
                    and isinstance(item.get("output"), str)
                ):
                    matches = item["output"].encode("utf-8") == skill_bundle[0]
                    skill_loaded = matches
                    skill_status = "verified_loaded" if matches else "digest_mismatch"
    elif len(matching_cats) > 1:
        has_association_gap = True

    status = "verified" if not has_association_gap and trace_status in {"success", "failed"} else "incomplete"
    if status != "verified" and skill_loaded is True:
        skill_loaded = None
        skill_status = "incomplete_trace"
    limitations = []
    if status != "verified":
        limitations.append("runtime traces lack a complete PID/call-id association")
    if binding_status != "verified":
        limitations.append("common prompt/config/schema/input bindings are incomplete")
    if parsed_trace["attribution_unknown"]:
        limitations.append(
            f"{len(parsed_trace['attribution_unknown'])} file descriptor path attribution(s) are unknown"
        )
    if skill_status == "unknown" and record.get("condition") == "current_skill":
        limitations.append("pinned Skill load was not proven by a traced cat read and exact ZIP-member output")
    return {
        "status": status,
        "supervisor_trace_status": trace_status,
        "supervisor_exit_code": trace_exit_code,
        "host_reported_completed": record.get("run_status") == "completed",
        "skill_load_status": skill_status,
        "verified_loaded": skill_loaded,
        "protocol_violation": protocol_violation,
        "common_binding_status": binding_status,
        "common_bindings": common_bindings,
        "limitations": limitations,
    }
