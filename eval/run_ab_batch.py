#!/usr/bin/env python3
"""Prepare and run the fixed 2026-10-10 A/B workflow validation batch."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility for non-freeze operations.
    tomllib = None

if __package__:
    from .runtime_evidence import NORMALIZER_VERSION, validate_sidecar_shape, verify_runtime_evidence
    from .runtime_trace import parse_raw_trace
else:
    from runtime_evidence import NORMALIZER_VERSION, validate_sidecar_shape, verify_runtime_evidence
    from runtime_trace import parse_raw_trace


BENCHMARK_ID = "2026-10-10-ab-workflow-validation"
PLAN_STATE = "waiting_for_independent_go"
CASE_IDS = ("case0002", "case0003", "case0006")
SLOT_ORDER = (
    ("case0002", "A", 1),
    ("case0003", "B", 1),
    ("case0006", "A", 1),
    ("case0003", "A", 1),
    ("case0002", "B", 1),
    ("case0006", "B", 1),
    ("case0002", "B", 2),
    ("case0003", "A", 2),
    ("case0006", "B", 2),
    ("case0003", "B", 2),
    ("case0002", "A", 2),
    ("case0006", "A", 2),
)
HOST_SCHEMA = Path(__file__).with_name("schemas") / "host-output.schema.json"
COMMON_PROMPT = Path(__file__).with_name("prompts") / "ab-batch-neutral.md"
TRACE_NORMALIZER = Path(__file__).with_name("runtime_trace.py")
DEFAULT_SKILL_SOURCE = Path(__file__).resolve().parents[1] / "skills" / "review-changes"
TRACE_SYSCALLS = (
    "execve,execveat,open,openat,openat2,creat,close,read,pread64,readv,preadv,preadv2,"
    "mmap,mmap2,pipe,pipe2,dup,dup2,dup3,fcntl,close_range,clone,clone3,fork,vfork"
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_slot_plan(case_index: dict[str, Any]) -> dict[str, Any]:
    """Build the fixed 12-slot plan from case metadata, never annotation data."""
    indexed = {
        item.get("case_id"): item
        for item in case_index.get("cases", [])
        if isinstance(item, dict) and item.get("case_id") in CASE_IDS
    }
    if set(indexed) != set(CASE_IDS):
        missing = sorted(set(CASE_IDS) - set(indexed))
        raise ValueError(f"case index is missing selected case metadata: {missing}")

    cases: dict[str, dict[str, Any]] = {}
    for case_id in CASE_IDS:
        item = indexed[case_id]
        required = ("base", "head", "snapshot", "scope_paths", "reviewer_input", "repositories")
        if any(key not in item for key in required):
            raise ValueError(f"{case_id} is missing required input metadata")
        repositories = item["repositories"]
        if not isinstance(repositories, dict) or not repositories.get("same_host_direct"):
            raise ValueError(f"{case_id} has no canonical same_host_direct repository")
        cases[case_id] = {
            "case_id": case_id,
            "case_origin": "real_repository",
            "base": item["base"],
            "head": item["head"],
            "snapshot": item["snapshot"],
            "scope_paths": item["scope_paths"],
            "reviewer_input_source": item["reviewer_input"],
            "repository_source": repositories["same_host_direct"],
            "annotation_status": "pending",
        }

    slots = [
        {
            "run_order": order,
            "case_id": case_id,
            "condition": condition,
            "replicate": replicate,
            "attempt": 1,
            "attempt_kind": "independent_replicate",
        }
        for order, (case_id, condition, replicate) in enumerate(SLOT_ORDER, start=1)
    ]
    return {
        "schema_version": 1,
        "benchmark_id": BENCHMARK_ID,
        "purpose": "workflow_reproducibility_validation_of_existing_corpus",
        "fresh_blind_test": False,
        "plan_state": PLAN_STATE,
        "execution_authorized": False,
        "annotation_status": "pending",
        "quality_metrics_separate": True,
        "conditions": {
            "A": "same_host_direct",
            "B": "same_host_direct_plus_pinned_unreleased_skill_source_snapshot",
        },
        "condition_difference": "B adds only the pinned Skill resource and its path instruction to the common prompt.",
        "cases": cases,
        "slots": slots,
    }


def make_artifact_ref(root: Path, artifact: Path) -> dict[str, str]:
    """Return a root-relative path and exact byte digest for a preserved artifact."""
    root = root.resolve()
    artifact = artifact.resolve()
    try:
        relative = artifact.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"artifact escapes run root: {artifact}") from exc
    if not artifact.is_file():
        raise ValueError(f"artifact is missing: {relative.as_posix()}")
    return {"path": relative.as_posix(), "sha256": sha256_file(artifact)}


def build_runtime_evidence(
    run_root: Path,
    codex_events: Path,
    supervisor_events: Path,
    raw_trace: Path,
    source_bundle_manifest: Path,
    normalizer_source: Path,
    common_bindings: dict[str, Path] | None = None,
    pinned_skill_host_path: str | None = None,
) -> dict[str, Any]:
    """Create a strict sidecar binding normalized events to their raw sources."""
    refs = {
        "codex_events": make_artifact_ref(run_root, codex_events),
        "supervisor_events": make_artifact_ref(run_root, supervisor_events),
        "raw_trace": make_artifact_ref(run_root, raw_trace),
        "source_bundle_manifest": make_artifact_ref(run_root, source_bundle_manifest),
        "normalizer_source": make_artifact_ref(run_root, normalizer_source),
    }
    evidence = {
        "schema_version": 1,
        "codex_events": refs["codex_events"],
        "supervisor_events": {
            **refs["supervisor_events"],
            "source_raw_trace": {**refs["raw_trace"]},
            "normalizer": {
                "version": NORMALIZER_VERSION,
                "source": refs["normalizer_source"],
            },
        },
        "raw_trace": {**refs["raw_trace"]},
        "source_bundle_manifest": refs["source_bundle_manifest"],
    }
    if common_bindings is not None:
        evidence["common_bindings"] = {
            name: make_artifact_ref(run_root, path)
            for name, path in sorted(common_bindings.items())
        }
    if pinned_skill_host_path is not None:
        evidence["pinned_skill_host_path"] = pinned_skill_host_path
    return evidence


def validate_runtime_evidence(value: Any) -> None:
    """Validate the sidecar shape without treating operator claims as proof."""
    validate_sidecar_shape(value)


def build_supervisor_header(raw_trace: bytes, normalizer_source: Path) -> dict[str, Any]:
    """Describe the source of normalized trace events; payloads are never copied."""
    return {
        "event": "normalizer_header",
        "schema_version": 1,
        "source_raw_trace_sha256": sha256_bytes(raw_trace),
        "normalizer": {
            "version": NORMALIZER_VERSION,
            "source_sha256": sha256_file(normalizer_source),
        },
    }


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    if os.name == "posix":
        path.chmod(0o600)


def _write_json(path: Path, value: Any) -> None:
    _write_private(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _slot_key(slot: dict[str, Any]) -> str:
    condition = "same_host_direct" if slot["condition"] == "A" else "current_skill"
    return f"{slot['case_id']}/{condition}/repeat{slot['replicate']:02d}"


def _immutable_plan(value: dict[str, Any]) -> dict[str, Any]:
    """Exclude only the GO switch; slots and case inputs stay bound."""
    return {key: item for key, item in value.items() if key not in {"plan_state", "execution_authorized"}}


def immutable_plan_sha256(value: dict[str, Any]) -> str:
    return sha256_bytes(_canonical_bytes(_immutable_plan(value)))


def _expected_slots() -> list[dict[str, Any]]:
    return [
        {
            "run_order": order,
            "case_id": case_id,
            "condition": condition,
            "replicate": replicate,
            "attempt": 1,
            "attempt_kind": "independent_replicate",
        }
        for order, (case_id, condition, replicate) in enumerate(SLOT_ORDER, start=1)
    ]


def validate_plan(value: Any, *, require_go: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("plan must be a schema_version 1 object")
    if value.get("benchmark_id") != BENCHMARK_ID or value.get("slots") != _expected_slots():
        raise ValueError("plan does not match the frozen benchmark identity and 12-slot order")
    if not isinstance(value.get("cases"), dict) or set(value["cases"]) != set(CASE_IDS):
        raise ValueError("plan case metadata does not match the frozen selected cases")
    if value.get("conditions") != {
        "A": "same_host_direct",
        "B": "same_host_direct_plus_pinned_unreleased_skill_source_snapshot",
    }:
        raise ValueError("plan conditions do not match the frozen A/B definitions")
    if require_go and not (value.get("execution_authorized") is True and value.get("plan_state") == "go"):
        raise ValueError("execution requires plan_state=go and execution_authorized=true")
    return value


def _load_plan(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    return validate_plan(json.loads(raw)), raw


def _runtime_config(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("runtime config must be UTF-8 JSON") from exc
    required = {
        "schema_version", "codex_executable", "strace_executable",
        "model", "provider_definition", "reasoning_effort", "timeout_seconds",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != 1:
        raise ValueError("runtime config has an unsupported shape")
    for name in ("codex_executable", "strace_executable"):
        executable = value.get(name)
        if (
            not isinstance(executable, str)
            or not Path(executable).is_absolute()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError(f"runtime config {name} must name an executable absolute path")
    model = value.get("model")
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise ValueError("runtime config model must be a non-empty string")
    provider = value.get("provider_definition")
    provider_fields = {"name", "base_url", "wire_api", "requires_openai_auth"}
    if not isinstance(provider, dict) or set(provider) != provider_fields:
        raise ValueError("runtime config provider_definition has an unsupported shape")
    if not isinstance(provider["name"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", provider["name"]):
        raise ValueError("runtime config provider name must be a simple config identifier")
    if not isinstance(provider["base_url"], str):
        raise ValueError("runtime config provider base_url must be a string")
    parsed_url = urllib.parse.urlsplit(provider["base_url"])
    try:
        parsed_url.port
    except ValueError as exc:
        raise ValueError("runtime config provider base_url has an invalid port") from exc
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
        or any(ord(char) < 0x20 for char in provider["base_url"])
    ):
        raise ValueError("runtime config provider base_url must be an HTTP(S) URL without credentials or query")
    if provider["wire_api"] not in {"responses", "chat"}:
        raise ValueError("runtime config provider wire_api is unsupported")
    if not isinstance(provider["requires_openai_auth"], bool):
        raise ValueError("runtime config provider requires_openai_auth must be boolean")
    if value.get("reasoning_effort") not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("runtime config reasoning_effort is unsupported")
    timeout = value.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 86400:
        raise ValueError("runtime config timeout_seconds must be an integer from 1 to 86400")
    return value


def _codex_config_overrides(config: dict[str, Any]) -> list[str]:
    provider = config["provider_definition"]
    prefix = f"model_providers.{provider['name']}"
    settings = (
        ("model", config["model"]),
        ("model_provider", provider["name"]),
        (f"{prefix}.name", provider["name"]),
        (f"{prefix}.base_url", provider["base_url"]),
        (f"{prefix}.wire_api", provider["wire_api"]),
        (f"{prefix}.requires_openai_auth", provider["requires_openai_auth"]),
        ("model_reasoning_effort", config["reasoning_effort"]),
    )
    overrides: list[str] = []
    for key, value in settings:
        rendered = json.dumps(value) if isinstance(value, str) else str(value).lower()
        overrides.extend(("-c", f"{key}={rendered}"))
    return overrides


def _freeze_runtime_config(
    codex_config_path: Path,
    *,
    codex_executable: Path,
    strace_executable: Path,
    timeout_seconds: int = 3600,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    if tomllib is None:
        raise ValueError("runtime config freezing requires Python 3.11 or newer")
    try:
        source = tomllib.loads(codex_config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Codex config must be readable UTF-8 TOML") from exc
    provider_name = source.get("model_provider")
    provider_sources = source.get("model_providers")
    provider_source = provider_sources.get(provider_name) if isinstance(provider_sources, dict) else None
    if not isinstance(provider_name, str) or not isinstance(provider_source, dict):
        raise ValueError("Codex config must define its selected model_providers entry")
    selected_effort = source.get("model_reasoning_effort")
    if selected_effort is None:
        selected_effort = reasoning_effort
    elif reasoning_effort is not None and reasoning_effort != selected_effort:
        raise ValueError("explicit reasoning effort differs from the active Codex config")
    provider_definition = {
        "name": provider_source.get("name", provider_name),
        "base_url": provider_source.get("base_url"),
        "wire_api": provider_source.get("wire_api"),
        "requires_openai_auth": provider_source.get("requires_openai_auth"),
    }
    value = {
        "schema_version": 1,
        "codex_executable": str(codex_executable.resolve(strict=True)),
        "strace_executable": str(strace_executable.resolve(strict=True)),
        "model": source.get("model"),
        "provider_definition": provider_definition,
        "reasoning_effort": selected_effort,
        "timeout_seconds": timeout_seconds,
    }
    return _runtime_config(_canonical_bytes(value))


def _preflight_codex_config(config: dict[str, Any]) -> None:
    executable = config["codex_executable"]
    overrides = _codex_config_overrides(config)
    with tempfile.TemporaryDirectory(prefix="ab-codex-config-check-") as isolated_codex_home:
        environment = os.environ.copy()
        environment["CODEX_HOME"] = isolated_codex_home
        checks = (
            [executable, *overrides, "features", "list"],
            [executable, "exec", "--ignore-user-config", "--ignore-rules", *overrides, "--version"],
        )
        for argv in checks:
            try:
                result = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError("Codex non-model configuration preflight could not run") from exc
            if result.returncode != 0:
                raise ValueError(
                    f"Codex non-model configuration preflight failed with exit {result.returncode}"
                )


def write_runtime_config(
    codex_config_path: Path,
    output_path: Path,
    *,
    reasoning_effort: str | None = None,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    codex_path_text = shutil.which("codex")
    strace_path_text = shutil.which("strace")
    if codex_path_text is None or strace_path_text is None:
        raise ValueError("codex and strace executables must be available on PATH")
    config = _freeze_runtime_config(
        codex_config_path,
        codex_executable=Path(codex_path_text),
        strace_executable=Path(strace_path_text),
        timeout_seconds=timeout_seconds,
        reasoning_effort=reasoning_effort,
    )
    _preflight_codex_config(config)
    output_path = output_path.resolve()
    _mkdir_private(output_path.parent)
    data = (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"refusing to overwrite runtime config: {output_path}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    if os.name == "posix":
        output_path.chmod(0o600)
    return config


def _copy_source_tree(
    source: Path,
    destination: Path,
    *,
    manifest_base: Path | None = None,
) -> list[dict[str, Any]]:
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError(f"source tree is not a directory: {source}")
    manifest_base = manifest_base.resolve() if manifest_base is not None else destination.parent.parent.resolve()
    _mkdir_private(destination)
    files: list[dict[str, Any]] = []
    for root_text, dirs, names in os.walk(source, topdown=True, followlinks=False):
        root = Path(root_text)
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in dirs:
            child = root / name
            if child.is_symlink():
                raise ValueError(f"source tree contains a symlink: {child}")
            _mkdir_private(destination / child.relative_to(source))
        for name in sorted(names):
            child = root / name
            if name == ".git":
                continue
            if child.is_symlink():
                raise ValueError(f"source tree contains a symlink: {child}")
            mode = child.stat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"source tree contains a non-regular file: {child}")
            payload = child.read_bytes()
            target = destination / child.relative_to(source)
            _write_private(target, payload)
            if os.name == "posix" and mode & 0o111:
                target.chmod(0o700)
            files.append({
                "path": target.relative_to(manifest_base).as_posix(),
                "sha256": sha256_bytes(payload),
                "bytes": len(payload),
            })
    return files


def _zip_tree(source: Path, target: Path) -> tuple[str, str]:
    files = [path for path in sorted(source.rglob("*")) if path.is_file()]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(f"review-changes/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if os.access(path, os.X_OK) else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, path.read_bytes())
    target.chmod(0o600)
    return sha256_file(target), sha256_file(source / "SKILL.md")


def _read_prompt(common_prompt: bytes, skill_host_path: str | None) -> bytes:
    prompt = common_prompt.rstrip(b"\n")
    if skill_host_path is not None:
        prompt += (
            b"\nFor condition B, read and follow the pinned Skill source at "
            + skill_host_path.encode("utf-8")
            + b" using cat -- <path>. The input is presealed and has no .git; "
            b"do not claim a Git-based Skill preparation workflow ran.\n"
        )
    else:
        prompt += b"\n"
    return prompt


class _set_umask:
    def __init__(self, value: int) -> None:
        self.value = value
        self.previous: int | None = None

    def __enter__(self) -> None:
        self.previous = os.umask(self.value)

    def __exit__(self, *_: object) -> None:
        if self.previous is not None:
            os.umask(self.previous)


def preseal_plan(
    plan: dict[str, Any],
    plan_bytes: bytes,
    runs_root: Path,
    runtime_config_path: Path,
    skill_source: Path = DEFAULT_SKILL_SOURCE,
) -> list[Path]:
    """Freeze model-visible bytes for all slots without starting Codex."""
    validate_plan(plan)
    config_bytes = runtime_config_path.read_bytes()
    config = _runtime_config(config_bytes)
    _preflight_codex_config(config)
    common_prompt = COMMON_PROMPT.read_bytes()
    schema_bytes = HOST_SCHEMA.read_bytes()
    normalizer_bytes = TRACE_NORMALIZER.read_bytes()
    skill_source = skill_source.resolve(strict=True)
    if not (skill_source / "SKILL.md").is_file():
        raise ValueError("pinned Skill source must contain SKILL.md")

    with _set_umask(0o077):
        _mkdir_private(runs_root)
        prepared: list[Path] = []
        for slot in plan["slots"]:
            case = plan["cases"][slot["case_id"]]
            run_root = runs_root / _slot_key(slot)
            if run_root.exists():
                raise ValueError(f"refusing to overwrite an existing run slot: {run_root}")
            _mkdir_private(run_root)
            input_root = run_root / "input"
            artifacts = run_root / "artifacts"
            _mkdir_private(input_root)
            _mkdir_private(artifacts)
            _mkdir_private(run_root / "runtime")
            repo_files = _copy_source_tree(Path(case["repository_source"]), input_root / "repository")
            manifest_files = list(repo_files)

            reviewer_data = Path(case["reviewer_input_source"]).read_bytes()
            _write_private(input_root / "reviewer-input.json", reviewer_data)
            _write_private(input_root / "common-prompt.md", common_prompt)
            _write_private(input_root / "runtime-config.json", config_bytes)
            _write_private(input_root / "protocol" / "host-output.schema.json", schema_bytes)
            for relative, payload in (
                ("input/reviewer-input.json", reviewer_data),
                ("input/common-prompt.md", common_prompt),
                ("input/runtime-config.json", config_bytes),
                ("input/protocol/host-output.schema.json", schema_bytes),
            ):
                manifest_files.append({"path": relative, "sha256": sha256_bytes(payload), "bytes": len(payload)})

            condition = slot["condition"]
            skill_resource = None
            skill_host_path = None
            if condition == "B":
                skill_dest = input_root / "pinned-skill" / "review-changes"
                skill_files = _copy_source_tree(skill_source, skill_dest, manifest_base=run_root)
                manifest_files.extend(skill_files)
                skill_host_path = str((skill_dest / "SKILL.md").resolve())
                skill_bundle = run_root / "skill-bundle.zip"
                bundle_sha256, member_sha256 = _zip_tree(skill_dest, skill_bundle)
                manifest_files.append({
                    "path": "skill-bundle.zip",
                    "sha256": bundle_sha256,
                    "bytes": skill_bundle.stat().st_size,
                })
                skill_resource = {
                    "host_path": skill_host_path,
                    "zip_path": "skill-bundle.zip",
                    "zip_sha256": bundle_sha256,
                    "zip_member": "review-changes/SKILL.md",
                    "member_sha256": member_sha256,
                }

            prompt_bytes = _read_prompt(common_prompt, skill_host_path)
            _write_private(input_root / "prompt.txt", prompt_bytes)
            manifest_files.append({
                "path": "input/prompt.txt",
                "sha256": sha256_bytes(prompt_bytes),
                "bytes": len(prompt_bytes),
            })
            manifest = {
                "schema_version": 1,
                "benchmark_id": plan["benchmark_id"],
                "case_id": slot["case_id"],
                "condition": condition,
                "plan_sha256": sha256_bytes(plan_bytes),
                "immutable_plan_sha256": immutable_plan_sha256(plan),
                "files": sorted(manifest_files, key=lambda item: item["path"]),
            }
            if skill_resource is not None:
                manifest["skill_resource"] = skill_resource
            _write_json(artifacts / "source-bundle-manifest.json", manifest)
            _write_private(artifacts / "runtime_trace.py", normalizer_bytes)
            _write_private(artifacts / "batch-plan.json", plan_bytes)
            metadata = {
                "schema_version": 1,
                "benchmark_id": plan["benchmark_id"],
                "case_id": slot["case_id"],
                "condition": condition,
                "run_order": slot["run_order"],
                "replicate": slot["replicate"],
                "attempt": slot["attempt"],
                "attempt_kind": slot["attempt_kind"],
                "group_id": f"{plan['benchmark_id']}:{slot['case_id']}:replicate-{slot['replicate']}",
                "immutable_plan_sha256": immutable_plan_sha256(plan),
                "prepared_at": _utc_now(),
            }
            _write_json(run_root / "run-metadata.json", metadata)
            _write_json(run_root / "preseal.json", {
                "schema_version": 1,
                "case_id": slot["case_id"],
                "condition": condition,
                "run_order": slot["run_order"],
                "immutable_plan_sha256": immutable_plan_sha256(plan),
                "source_bundle_manifest": make_artifact_ref(run_root, artifacts / "source-bundle-manifest.json"),
            })
            prepared.append(run_root)
    return prepared


def _codex_events(data: bytes) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return values
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _command_items(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    items: dict[str, str] = {}
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") not in {
            "command_execution", "command", "tool_call", "command.completed",
        }:
            continue
        call_id = item.get("id") or event.get("call_id") or event.get("callId")
        command = item.get("command") or item.get("input")
        if isinstance(call_id, str) and isinstance(command, str):
            items[call_id] = command
    return list(items.items())


def _command_matches(argv: list[str], command: str) -> bool:
    try:
        wanted = shlex.split(command)
    except ValueError:
        return False
    if argv == wanted or (
        argv and wanted and Path(argv[0]).name == Path(wanted[0]).name and argv[1:] == wanted[1:]
    ):
        return True
    if argv and Path(argv[0]).name in {"sh", "bash", "dash", "zsh"}:
        for index, argument in enumerate(argv[:-1]):
            if argument == "-c" or (argument.startswith("-") and argument.endswith("c")):
                try:
                    script_tokens = shlex.split(argv[index + 1])
                except ValueError:
                    return False
                if script_tokens == wanted or (
                    script_tokens
                    and wanted
                    and Path(script_tokens[0]).name == Path(wanted[0]).name
                    and script_tokens[1:] == wanted[1:]
                ):
                    return True
                if "&&" in script_tokens:
                    boundary = len(script_tokens) - 1 - script_tokens[::-1].index("&&")
                    try:
                        return shlex.split(" ".join(script_tokens[boundary + 1 :])) == wanted
                    except ValueError:
                        return False
    return False


def normalized_supervisor_events(
    raw_trace: bytes,
    codex_bytes: bytes,
    normalizer_source: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Add observed CLI session/tool IDs to events from the trusted parser."""
    parsed = parse_raw_trace(raw_trace)
    cli_events = _codex_events(codex_bytes)
    thread_ids = {
        event.get("thread_id")
        for event in cli_events
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str)
    }
    thread_id = next(iter(thread_ids)) if len(thread_ids) == 1 else None
    commands = _command_items(cli_events)
    normalized = [build_supervisor_header(raw_trace, normalizer_source)]
    pid_calls: dict[int, str] = {}
    for event in parsed["events"]:
        pid = event.get("pid")
        if event.get("event") == "exec" and isinstance(event.get("argv"), list):
            argv = event["argv"]
            matches = [call_id for call_id, command in commands if _command_matches(argv, command)]
            if len(matches) == 1:
                pid_calls[pid] = matches[0]
            elif argv and Path(argv[0]).name in {"codex", "codex.exe"} and isinstance(thread_id, str):
                pid_calls[pid] = thread_id
            elif isinstance(thread_id, str):
                pid_calls.setdefault(pid, thread_id)
        call_id = pid_calls.get(pid, thread_id)
        normalized.append({**event, **({"call_id": call_id} if isinstance(call_id, str) else {})})
        if event.get("event") == "process_exit":
            pid_calls.pop(pid, None)
    return normalized, parsed


def _jsonl_bytes(events: list[dict[str, Any]]) -> bytes:
    return b"".join(_canonical_bytes(event) for event in events)


def _artifact_refs(run_root: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for path in sorted(run_root.rglob("*")):
        if not path.is_file() or path.name == "slot-result.json":
            continue
        relative = path.relative_to(run_root)
        if relative.parts[:2] in {("input", "repository"), ("input", "pinned-skill")}:
            continue
        result[relative.as_posix()] = make_artifact_ref(run_root, path)
    return result


def _preseal_status(run_root: Path, immutable_sha: str) -> dict[str, Any]:
    metadata = json.loads((run_root / "run-metadata.json").read_bytes())
    manifest_path = run_root / "artifacts" / "source-bundle-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if metadata.get("immutable_plan_sha256") != immutable_sha or manifest.get("immutable_plan_sha256") != immutable_sha:
        raise ValueError(f"prepared slot is bound to a different frozen plan: {run_root}")
    runtime = run_root / "runtime"
    if any((runtime / name).exists() for name in ("adapter-start.json", "adapter-failure.json", "execution.json")):
        raise ValueError(f"slot has already been attempted; retries are disabled: {run_root}")
    if manifest.get("plan_sha256") != sha256_file(run_root / "artifacts" / "batch-plan.json"):
        raise ValueError(f"prepared plan copy digest mismatch: {run_root}")
    for entry in manifest.get("files", []):
        path = (run_root / entry["path"]).resolve()
        try:
            path.relative_to(run_root.resolve())
        except ValueError as exc:
            raise ValueError(f"prepared input escapes run root: {entry.get('path')}") from exc
        if not path.is_file() or sha256_file(path) != entry["sha256"]:
            raise ValueError(f"prepared input digest mismatch: {entry.get('path')}")
    return {"metadata": metadata, "manifest": manifest}


def _empty_runtime_evidence(run_status: str, reason: str) -> dict[str, Any]:
    return {
        "status": "incomplete",
        "supervisor_trace_status": "unknown",
        "supervisor_exit_code": None,
        "host_reported_completed": run_status == "completed",
        "skill_load_status": "unknown",
        "verified_loaded": None,
        "protocol_violation": False,
        "common_binding_status": "incomplete",
        "common_bindings": None,
        "limitations": [reason],
    }


def execute_slot(
    run_root: Path,
    config: dict[str, Any],
    *,
    go_authorized: bool = False,
) -> dict[str, Any]:
    """Run one presealed slot under strace and retain outputs, including failures."""
    if not go_authorized:
        raise ValueError("slot execution requires the plan GO authorization gate")
    run_root = run_root.resolve(strict=True)
    metadata = json.loads((run_root / "run-metadata.json").read_bytes())
    manifest_path = run_root / "artifacts" / "source-bundle-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    runtime = run_root / "runtime"
    artifacts = run_root / "artifacts"
    if any((runtime / name).exists() for name in ("adapter-start.json", "adapter-failure.json", "execution.json")):
        raise ValueError(f"slot has already been attempted; retries are disabled: {run_root}")
    _mkdir_private(runtime)
    raw_output = artifacts / "raw-model-output.txt"
    stdout_path = runtime / "codex-events.jsonl"
    stderr_path = runtime / "strace-stderr.log"
    trace_path = runtime / "strace.log"
    for path in (raw_output, stdout_path, stderr_path, trace_path):
        _write_private(path, b"")

    codex_argv = [
        config["codex_executable"], "exec", "--json", "--ephemeral",
        "--ignore-user-config", "--ignore-rules", *_codex_config_overrides(config),
        "--sandbox", "read-only", "--skip-git-repo-check",
        "--output-schema", str((run_root / "input" / "protocol" / "host-output.schema.json").resolve()),
        "--output-last-message", str(raw_output.resolve()),
        "--cd", str((run_root / "input").resolve()),
    ]
    codex_argv.append("-")
    strace_argv = [
        config["strace_executable"], "-f", "-yy", "-s", "4096",
        "-e", "raw=read,pread64,readv,preadv,preadv2", "-e", f"trace={TRACE_SYSCALLS}",
        "-o", str(trace_path.resolve()), "--", *codex_argv,
    ]

    started_at = _utc_now()
    monotonic_start = time.monotonic()
    wrapper_exit: int | None = None
    timed_out = False
    failure_reason: str | None = None
    _write_json(runtime / "adapter-start.json", {
        "schema_version": 1,
        "started_at": started_at,
        "argv": codex_argv,
    })
    with _set_umask(0o077), stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
        try:
            process = subprocess.Popen(
                strace_argv,
                stdin=subprocess.PIPE,
                stdout=stdout_stream,
                stderr=stderr_stream,
                start_new_session=True,
                close_fds=True,
            )
            try:
                process.communicate(
                    input=(run_root / "input" / "prompt.txt").read_bytes(),
                    timeout=config["timeout_seconds"],
                )
                wrapper_exit = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                failure_reason = "supervisor_timeout"
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.communicate()
                wrapper_exit = process.returncode
        except OSError as exc:
            failure_reason = f"supervisor_start_failed:{exc.__class__.__name__}"

    duration = max(0.0, time.monotonic() - monotonic_start)
    finished_at = _utc_now()
    for path in (raw_output, stdout_path, stderr_path, trace_path):
        if not path.exists():
            _write_private(path, b"")
        if os.name == "posix":
            path.chmod(0o600)

    output_bytes = raw_output.read_bytes()
    host_output: dict[str, Any] | None = None
    try:
        parsed_output = json.loads(output_bytes.decode("utf-8"))
        if isinstance(parsed_output, dict):
            host_output = parsed_output
            _write_private(run_root / "host-output.json", output_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    if host_output is None and failure_reason is None:
        failure_reason = "model_output_missing_or_not_json_object"
    adapter_status = "completed" if wrapper_exit == 0 and host_output is not None and not timed_out else (
        "failed" if wrapper_exit not in {0, None} else "incomplete"
    )
    if wrapper_exit not in {0, None} and failure_reason is None:
        failure_reason = f"supervisor_exit_{wrapper_exit}"
    reported_status = host_output.get("run_status") if host_output else None
    run_status = (
        adapter_status
        if adapter_status != "completed"
        else reported_status if reported_status in ("completed", "failed", "incomplete") else "incomplete"
    )
    if run_status == "failed" and failure_reason is None:
        reported_failure = host_output.get("failure_reason") if host_output else None
        failure_reason = reported_failure if isinstance(reported_failure, str) and reported_failure else "host_reported_failed"

    codex_bytes = stdout_path.read_bytes()
    raw_trace = trace_path.read_bytes()
    supervisor_events, parsed_trace = normalized_supervisor_events(
        raw_trace, codex_bytes, artifacts / "runtime_trace.py"
    )
    supervisor_path = runtime / "supervisor-events.jsonl"
    _write_private(supervisor_path, _jsonl_bytes(supervisor_events))
    execution = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration,
        "wrapper_exit_code": wrapper_exit,
        "host_reported_run_status": reported_status,
        "timed_out": timed_out,
        "failure_reason": failure_reason,
        "argv": codex_argv,
        "trace_complete": parsed_trace["complete"],
        "trace_reason": parsed_trace["reason"],
        "trace_unknown": parsed_trace["unknown"],
    }
    _write_json(runtime / "execution.json", execution)

    common_bindings = {
        "review_manifest": run_root / "input" / "reviewer-input.json",
        "common_prompt": run_root / "input" / "common-prompt.md",
        "output_schema": run_root / "input" / "protocol" / "host-output.schema.json",
        "runtime_config": run_root / "input" / "runtime-config.json",
    }
    skill_resource = manifest.get("skill_resource")
    evidence = build_runtime_evidence(
        run_root,
        stdout_path,
        supervisor_path,
        trace_path,
        manifest_path,
        artifacts / "runtime_trace.py",
        common_bindings=common_bindings,
        pinned_skill_host_path=skill_resource["host_path"] if isinstance(skill_resource, dict) else None,
    )
    _write_json(run_root / "runtime-evidence.json", evidence)
    preview_record: dict[str, Any] = {
        "condition": "same_host_direct" if metadata["condition"] == "A" else "current_skill",
        "run_status": run_status,
        "input": {"review_manifest_sha256": sha256_file(common_bindings["review_manifest"])},
        "artifacts": {},
    }
    if isinstance(skill_resource, dict):
        preview_record["artifacts"]["skill_bundle"] = skill_resource["zip_path"]
        preview_record["skill_provenance"] = {"bundle_sha256": skill_resource["zip_sha256"]}
    try:
        verified_runtime = verify_runtime_evidence(run_root, "runtime-evidence.json", preview_record)
        verification_error = None
    except ValueError as exc:
        verified_runtime = _empty_runtime_evidence(run_status, f"runtime evidence could not be verified: {exc}")
        verification_error = str(exc)
    result = {
        "schema_version": 1,
        "benchmark_id": metadata["benchmark_id"],
        "case_id": metadata["case_id"],
        "condition": metadata["condition"],
        "run_order": metadata["run_order"],
        "replicate": metadata["replicate"],
        "attempt": metadata["attempt"],
        "attempt_kind": metadata["attempt_kind"],
        "run_status": run_status,
        "failure_reason": failure_reason,
        "host_output_json_object": host_output is not None,
        "wrapper_exit_code": wrapper_exit,
        "duration_seconds": duration,
        "started_at": started_at,
        "finished_at": finished_at,
        "immutable_plan_sha256": metadata["immutable_plan_sha256"],
        "runtime_evidence": verified_runtime,
        "runtime_evidence_verification_error": verification_error,
        "artifacts": _artifact_refs(run_root),
    }
    _write_json(run_root / "slot-result.json", result)
    return result


def execute_plan(plan: dict[str, Any], runs_root: Path, config_path: Path) -> list[dict[str, Any]]:
    validate_plan(plan, require_go=True)
    config_bytes = config_path.read_bytes()
    config = _runtime_config(config_bytes)
    _preflight_codex_config(config)
    immutable_sha = immutable_plan_sha256(plan)
    run_roots: list[Path] = []
    for slot in plan["slots"]:
        run_root = runs_root / _slot_key(slot)
        _preseal_status(run_root, immutable_sha)
        if (run_root / "input" / "runtime-config.json").read_bytes() != config_bytes:
            raise ValueError(f"runtime config differs from presealed bytes: {run_root}")
        run_roots.append(run_root)

    results: list[dict[str, Any]] = []
    for run_root in run_roots:
        try:
            results.append(execute_slot(run_root, config, go_authorized=True))
        except Exception as exc:
            runtime = run_root / "runtime"
            _mkdir_private(runtime)
            _write_json(runtime / "adapter-failure.json", {
                "schema_version": 1,
                "failure_reason": f"adapter_failure:{exc.__class__.__name__}",
                "detail": str(exc),
            })
            results.append({
                "case_id": run_root.parts[-3],
                "run_root": str(run_root),
                "run_status": "failed",
            })
    return results


def _validate_plan_inputs(plan: dict[str, Any], case_index_path: Path) -> None:
    index = json.loads(case_index_path.read_bytes())
    expected = build_slot_plan(index)
    if _immutable_plan(plan) != _immutable_plan(expected):
        raise ValueError("plan case inputs differ from the selected case-index snapshot")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-index", type=Path, default=Path("eval/data/real-cases.json"))
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--prepare", action="store_true", help="Preseal all fixed slots without model calls.")
    operations.add_argument("--execute", action="store_true", help="Run fixed slots only with an explicit GO plan.")
    operations.add_argument("--write-runtime-config", action="store_true", help="Freeze the allowlisted active Codex model config after non-model preflight.")
    parser.add_argument("--plan", type=Path, help="Existing frozen plan for prepare/execute.")
    parser.add_argument("--plan-output", type=Path, help="Write a new waiting plan.")
    parser.add_argument("--runs-root", type=Path, help="Prepared slot destination.")
    parser.add_argument("--runtime-config", type=Path, help="Frozen Codex/strace config JSON.")
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex" / "config.toml", help="Active Codex TOML to read through the runtime-config allowlist.")
    parser.add_argument("--runtime-config-output", type=Path, help="New private runtime-config output path.")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max"), help="Explicit legal effort only when the active Codex config omits one.")
    parser.add_argument("--timeout-seconds", type=int, default=3600, help="Frozen execution timeout from 1 to 86400 seconds.")
    parser.add_argument("--skill-source", type=Path, default=DEFAULT_SKILL_SOURCE)
    args = parser.parse_args(argv)
    try:
        if args.write_runtime_config:
            if args.runtime_config_output is None:
                parser.error("--write-runtime-config requires --runtime-config-output")
            if isinstance(args.timeout_seconds, bool) or not 1 <= args.timeout_seconds <= 86400:
                parser.error("--timeout-seconds must be an integer from 1 to 86400")
            config = write_runtime_config(
                args.codex_config,
                args.runtime_config_output,
                reasoning_effort=args.reasoning_effort,
                timeout_seconds=args.timeout_seconds,
            )
            print(json.dumps({
                "runtime_config": str(args.runtime_config_output.resolve()),
                "codex_preflight": "passed",
                "model": config["model"],
                "provider": config["provider_definition"]["name"],
                "reasoning_effort": config["reasoning_effort"],
            }, sort_keys=True))
            return 0
        if args.prepare or args.execute:
            if args.plan is None or args.runs_root is None or args.runtime_config is None:
                parser.error("--prepare/--execute require --plan, --runs-root, and --runtime-config")
            plan, plan_bytes = _load_plan(args.plan)
            _validate_plan_inputs(plan, args.case_index)
            if args.prepare:
                preseal_plan(plan, plan_bytes, args.runs_root, args.runtime_config, args.skill_source)
                return 0
            results = execute_plan(plan, args.runs_root, args.runtime_config)
            print(json.dumps({"runs": results}, sort_keys=True))
            return 0 if all(item.get("run_status") == "completed" for item in results) else 1
        if args.plan_output is None:
            parser.error("plan-only operation requires --plan-output")
        index = json.loads(args.case_index.read_bytes())
        plan = build_slot_plan(index)
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        args.plan_output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
