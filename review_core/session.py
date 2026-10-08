"""Persistent, snapshot-bound orchestration for local review sessions."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from review_core.diff import run_git
from review_core.report import render_review
from review_core.recheck import recheck_review, render_recheck
from review_core.snapshot import capture_context, collect_changes
from review_core.validation import validate_manifest, validate_review


SESSION_SCHEMA_VERSION = "1"


class ReviewSessionError(RuntimeError):
    """A review session cannot safely continue with its recorded inputs."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewSessionError(f"cannot read valid UTF-8 JSON from {path}: {exc}") from exc


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    _atomic_write(path, encoded.encode("utf-8"))


def _write_text(path: Path, value: str) -> None:
    _atomic_write(path, value.encode("utf-8"))


def _write_content_addressed(path: Path, value: Any) -> None:
    if path.exists():
        if _read_json(path) != value:
            raise ReviewSessionError(f"refusing to replace existing session artifact: {path}")
        return
    _write_json(path, value)


def _repository_identity(repository: str | Path) -> dict[str, str]:
    candidate = Path(repository).expanduser().resolve()
    try:
        root = Path(os.fsdecode(run_git(candidate, "rev-parse", "--show-toplevel").strip())).resolve()
        git_dir = Path(os.fsdecode(run_git(root, "rev-parse", "--absolute-git-dir").strip())).resolve()
        common_text = os.fsdecode(run_git(root, "rev-parse", "--git-common-dir").strip())
    except (OSError, RuntimeError) as exc:
        raise ReviewSessionError(f"cannot identify Git worktree at {candidate}: {exc}") from exc
    common_dir = Path(common_text)
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    return {
        "repository_root": str(root),
        "git_dir": str(git_dir.resolve()),
        "git_common_dir": str(common_dir.resolve()),
    }


def _identity_matches(left: Any, right: Any) -> bool:
    fields = ("repository_root", "git_dir", "git_common_dir")
    return isinstance(left, dict) and isinstance(right, dict) and all(
        left.get(field) == right.get(field) for field in fields
    )


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_outside_repository(path: Path, repository_root: Path) -> None:
    resolved = path.expanduser().resolve()
    if _inside(resolved, repository_root):
        raise ReviewSessionError(
            f"session output must be outside the reviewed worktree: {resolved}"
        )


def _default_session_directory(repository_root: Path) -> Path:
    root = (Path.home() / ".codex" / "review-sessions").resolve()
    if _inside(root, repository_root):
        root = (Path(tempfile.gettempdir()) / "review-changes-sessions").resolve()
    _assert_outside_repository(root, repository_root)
    return root / f"review-{uuid.uuid4().hex}"


def create_review_draft(manifest: dict[str, Any]) -> dict[str, Any]:
    """Create a valid result draft whose semantic coverage is always uncovered."""
    checked = validate_manifest(manifest)
    draft = {
        "schema_version": "2",
        "input_fingerprint": checked["content_fingerprint"],
        "rules_version": "host-semantic-review-pending",
        "review_status": "uncovered",
        "findings": [],
        "coverage": [
            {
                "file": item["filename"],
                "semantic_status": "uncovered",
                "static_status": "not_run",
                "reason": "Host semantic review has not been recorded.",
                "context_gaps": [],
            }
            for item in checked["files"]
        ],
        "tool_runs": [],
        "limits": copy.deepcopy(checked["limits"]),
        "measurements": {"host": None, "model": None, "tokens": None, "cost": None},
    }
    return validate_review(draft, checked)


def _manifest_path(session_dir: Path, fingerprint: str, kind: str) -> Path:
    return session_dir / "snapshots" / f"{kind}-{fingerprint}.json"


def _draft_path(session_dir: Path, fingerprint: str) -> Path:
    return session_dir / "drafts" / f"draft-{fingerprint}.json"


def _save_snapshot_artifacts(
    session_dir: Path,
    manifest: dict[str, Any],
    *,
    kind: str,
) -> tuple[str, str]:
    fingerprint = manifest["content_fingerprint"]
    manifest_path = _manifest_path(session_dir, fingerprint, kind)
    draft_path = _draft_path(session_dir, fingerprint)
    _write_content_addressed(manifest_path, manifest)
    _write_content_addressed(draft_path, create_review_draft(manifest))
    return (
        manifest_path.relative_to(session_dir).as_posix(),
        draft_path.relative_to(session_dir).as_posix(),
    )


def _safe_reference(session_dir: Path, value: Any, location: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ReviewSessionError(f"session.{location} must be a non-empty relative path")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ReviewSessionError(f"session.{location} must stay inside the session directory")
    resolved = (session_dir / candidate).resolve()
    if not _inside(resolved, session_dir.resolve()):
        raise ReviewSessionError(f"session.{location} escapes the session directory")
    return resolved


def _capture_previous_review(
    session_dir: Path,
    record: dict[str, Any],
    manifest: dict[str, Any],
    validated_result: dict[str, Any] | None,
) -> dict[str, Any]:
    refs = record.get("references")
    if not isinstance(refs, dict) or validated_result is None:
        raise ReviewSessionError("the previous session has no validated review result")
    session_id = record.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ReviewSessionError("the previous session identity is missing")
    manifest_ref = refs.get("manifest")
    result_ref = refs.get("validated_result")
    report_ref = refs.get("report")
    manifest_path = _safe_reference(session_dir, manifest_ref, "references.manifest")
    result_path = _safe_reference(session_dir, result_ref, "references.validated_result")
    report_path = _safe_reference(session_dir, report_ref, "references.report")
    try:
        manifest_bytes = manifest_path.read_bytes()
        result_bytes = result_path.read_bytes()
        report_bytes = report_path.read_bytes()
    except OSError as exc:
        raise ReviewSessionError(f"previous review artifact is unavailable: {exc}") from exc
    try:
        stored_manifest = validate_manifest(json.loads(manifest_bytes.decode("utf-8")))
        stored_result = validate_review(json.loads(result_bytes.decode("utf-8")), stored_manifest)
        report_text = report_bytes.decode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReviewSessionError(f"previous review artifact is invalid: {exc}") from exc
    if stored_manifest != manifest:
        raise ReviewSessionError("previous manifest changed while linking the review")
    if stored_result != validated_result or report_text != render_review(stored_result):
        raise ReviewSessionError("previous validated result or report is inconsistent")
    return {
        "session_directory": str(session_dir),
        "session_id": session_id,
        "repository_identity": copy.deepcopy(record.get("repository_identity")),
        "manifest": manifest_ref,
        "validated_result": result_ref,
        "report": report_ref,
        "input_fingerprint": stored_manifest["content_fingerprint"],
        "artifacts_sha256": {
            "manifest": hashlib.sha256(manifest_bytes).hexdigest(),
            "validated_result": hashlib.sha256(result_bytes).hexdigest(),
            "report": hashlib.sha256(report_bytes).hexdigest(),
        },
    }


def _load_linked_previous_review(
    link: Any, current_identity: dict[str, str]
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    if not isinstance(link, dict):
        raise ReviewSessionError("linked previous review reference is missing")
    session_dir = Path(str(link.get("session_directory", ""))).expanduser().resolve()
    record = _read_json(session_dir / "session.json")
    if not isinstance(record, dict) or record.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise ReviewSessionError("linked previous session record is missing or invalid")
    if record.get("session_directory") != str(session_dir):
        raise ReviewSessionError("linked previous session directory identity changed")
    if not isinstance(link.get("session_id"), str) or record.get("session_id") != link["session_id"]:
        raise ReviewSessionError("linked previous session identity changed")
    linked_identity = link.get("repository_identity")
    if not _identity_matches(linked_identity, current_identity) or not _identity_matches(
        record.get("repository_identity"), linked_identity
    ):
        raise ReviewSessionError("linked previous review belongs to a different Git worktree")

    hashes = link.get("artifacts_sha256")
    if not isinstance(hashes, dict):
        raise ReviewSessionError("linked previous artifact fingerprints are missing")
    payloads: dict[str, bytes] = {}
    for name in ("manifest", "validated_result", "report"):
        path = _safe_reference(session_dir, link.get(name), f"previous_session.{name}")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ReviewSessionError(f"linked previous {name} artifact is unavailable: {exc}") from exc
        if not isinstance(hashes.get(name), str) or hashlib.sha256(payload).hexdigest() != hashes[name]:
            raise ReviewSessionError(f"linked previous {name} artifact changed after it was linked")
        payloads[name] = payload

    try:
        manifest = validate_manifest(json.loads(payloads["manifest"].decode("utf-8")))
        result = validate_review(
            json.loads(payloads["validated_result"].decode("utf-8")), manifest
        )
        report = payloads["report"].decode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReviewSessionError(f"linked previous review artifacts are invalid: {exc}") from exc
    fingerprint = link.get("input_fingerprint")
    if (
        manifest.get("repository") != current_identity["repository_root"]
        or manifest.get("content_fingerprint") != fingerprint
        or result.get("input_fingerprint") != fingerprint
    ):
        raise ReviewSessionError("linked previous review fingerprint or repository identity changed")
    if report != render_review(result):
        raise ReviewSessionError("linked previous report does not match its validated result")
    return session_dir, manifest, result


def _load_session(session_directory: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    session_dir = Path(session_directory).expanduser().resolve()
    record_path = session_dir / "session.json"
    record = _read_json(record_path)
    if not isinstance(record, dict) or record.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise ReviewSessionError(f"unsupported or malformed session record: {record_path}")
    if record.get("session_directory") != str(session_dir):
        raise ReviewSessionError("session directory identity does not match its record")

    refs = record.get("references")
    fingerprints = record.get("fingerprints")
    if not isinstance(refs, dict) or not isinstance(fingerprints, dict):
        raise ReviewSessionError("session references or fingerprints are missing")
    initial_path = _safe_reference(session_dir, refs.get("initial_manifest"), "references.initial_manifest")
    manifest_path = _safe_reference(session_dir, refs.get("manifest"), "references.manifest")
    draft_path = _safe_reference(session_dir, refs.get("draft_result"), "references.draft_result")
    initial = validate_manifest(_read_json(initial_path))
    manifest = validate_manifest(_read_json(manifest_path))
    draft = _read_json(draft_path)
    if initial["content_fingerprint"] != fingerprints.get("initial"):
        raise ReviewSessionError("initial manifest fingerprint does not match the session record")
    if manifest["content_fingerprint"] != fingerprints.get("final"):
        raise ReviewSessionError("current manifest fingerprint does not match the session record")
    if initial["range_fingerprint"] != manifest["range_fingerprint"]:
        raise ReviewSessionError("initial and final manifests do not describe the same Git range")
    scope = record.get("scope")
    if not isinstance(scope, dict) or any(
        scope.get(record_key) != manifest.get(manifest_key)
        for record_key, manifest_key in (
            ("kind", "scope"),
            ("base", "base"),
            ("head", "head"),
            ("merge_base", "merge_base"),
        )
    ):
        raise ReviewSessionError("session scope does not match its recorded manifests")
    if record.get("fingerprints", {}).get("range") != manifest.get("range_fingerprint"):
        raise ReviewSessionError("session range fingerprint does not match its manifest")
    if not _identity_matches(record.get("repository_identity"), _repository_identity(manifest["repository"])):
        raise ReviewSessionError("session repository identity does not match its manifest")
    if manifest["repository"] != record["repository_identity"].get("repository_root"):
        raise ReviewSessionError("manifest repository path does not match the session identity")
    if draft != create_review_draft(manifest):
        raise ReviewSessionError("stored draft is not the uncovered draft for the current manifest")

    validated_result_ref = refs.get("validated_result")
    validated_result = None
    if validated_result_ref is not None:
        validated_path = _safe_reference(session_dir, validated_result_ref, "references.validated_result")
        validated_result = validate_review(_read_json(validated_path), manifest)
    for name in ("report", "recheck_result", "recheck_report"):
        reference = refs.get(name)
        if reference is not None:
            path = _safe_reference(session_dir, reference, f"references.{name}")
            if not path.is_file():
                raise ReviewSessionError(f"session {name} artifact is missing: {path}")
    return session_dir, record, manifest, {"initial": initial, "draft": draft, "validated": validated_result}


def _record_failure(
    session_dir: Path,
    name: str,
    error: BaseException,
    *,
    attempt_directory: Path | None = None,
) -> None:
    record_path = session_dir / "session.json"
    try:
        record = _read_json(record_path)
        if not isinstance(record, dict):
            return
        message = str(error)
        record.setdefault("failures", []).append(
            {"step": name, "at": _now(), "error": message}
        )
        record.setdefault("steps", []).append(
            {"name": name, "status": "failed", "at": _now(), "error": message}
        )
        if attempt_directory is not None:
            sequence = int(attempt_directory.name)
            record["attempt_sequence"] = max(int(record.get("attempt_sequence", 0)), sequence)
            record.setdefault("attempts", []).append(
                {
                    "sequence": sequence,
                    "kind": name,
                    "status": "failed",
                    "at": _now(),
                    "failure": (attempt_directory / "failure.json")
                    .relative_to(session_dir)
                    .as_posix(),
                }
            )
        _write_json(record_path, record)
    except (OSError, ReviewSessionError, TypeError, ValueError):
        return


def _verify_current_snapshot(
    record: dict[str, Any], initial: dict[str, Any], final: dict[str, Any]
) -> None:
    identity = _repository_identity(record["repository_identity"]["repository_root"])
    if not _identity_matches(identity, record["repository_identity"]):
        raise ReviewSessionError(
            "repository/worktree identity changed; start a new review session"
        )
    limits = final.get("limits", {})
    scope = initial["scope"]
    current_initial = collect_changes(
        repository=identity["repository_root"],
        scope=scope,
        base=initial["base"] if scope == "branch" else None,
        head=initial["head"] if scope == "branch" else "HEAD",
        include_untracked=bool(limits.get("include_untracked", False)),
        max_file_bytes=int(limits.get("max_file_bytes", 1_048_576)),
    )
    if current_initial["content_fingerprint"] != initial["content_fingerprint"]:
        raise ReviewSessionError(
            "selected Git snapshot changed; the original session is preserved, start a new session"
        )
    context_paths = [item["filename"] for item in final.get("context_files", [])]
    current_final = (
        capture_context(current_initial, context_paths)
        if context_paths
        else current_initial
    )
    if current_final["content_fingerprint"] != final["content_fingerprint"]:
        raise ReviewSessionError(
            "captured context changed; the original session is preserved, start a new session"
        )


def _missing_review_inputs(result: dict[str, Any]) -> list[str]:
    missing = []
    for item in result.get("coverage", []):
        if item.get("semantic_status") != "completed":
            missing.append(
                f"semantic_coverage:{item.get('file', '')}:{item.get('semantic_status', 'unknown')}"
            )
        for gap in item.get("context_gaps", []):
            missing.append(f"context:{item.get('file', '')}:{gap}")
    if result.get("review_status") == "failed":
        missing.append("semantic_review:failed")
    if result.get("review_status") == "uncovered" and not result.get("coverage"):
        missing.append("semantic_coverage:no_selected_files")
    return missing


def create_session(
    repository: str | Path,
    scope: str = "worktree",
    *,
    base: str | None = None,
    head: str = "HEAD",
    include_untracked: bool = False,
    session_directory: str | Path | None = None,
    session_root: str | Path | None = None,
    context_paths: list[str | Path] | None = None,
    previous_session: str | Path | None = None,
) -> dict[str, Any]:
    """Capture a new immutable session and create its uncovered result draft."""
    identity = _repository_identity(repository)
    repository_root = Path(identity["repository_root"])
    if session_directory is not None and session_root is not None:
        raise ReviewSessionError("choose either session_directory or session_root")

    manifest = collect_changes(
        repository=repository_root,
        scope=scope,
        base=base,
        head=head,
        include_untracked=include_untracked,
    )
    initial_manifest = copy.deepcopy(manifest)
    if context_paths:
        manifest = capture_context(manifest, list(context_paths))

    previous_info = None
    if previous_session is not None:
        previous_dir, previous_record, previous_manifest, previous_parts = _load_session(previous_session)
        if not _identity_matches(identity, previous_record.get("repository_identity")):
            raise ReviewSessionError("a prior review can only be linked from the same Git worktree")
        previous_info = _capture_previous_review(
            previous_dir, previous_record, previous_manifest, previous_parts["validated"]
        )

    if session_directory is not None:
        target = Path(session_directory).expanduser().resolve()
    elif session_root is not None:
        root = Path(session_root).expanduser().resolve()
        _assert_outside_repository(root, repository_root)
        target = root / f"review-{uuid.uuid4().hex}"
    else:
        target = _default_session_directory(repository_root)
    _assert_outside_repository(target, repository_root)
    if target.exists():
        raise ReviewSessionError(f"session directory already exists; refusing to overwrite: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()

    if initial_manifest["content_fingerprint"] == manifest["content_fingerprint"]:
        final_ref, draft_ref = _save_snapshot_artifacts(target, manifest, kind="final")
        initial_ref = final_ref
    else:
        initial_ref, _ = _save_snapshot_artifacts(target, initial_manifest, kind="initial")
        final_ref, draft_ref = _save_snapshot_artifacts(target, manifest, kind="final")

    record: dict[str, Any] = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "session_id": uuid.uuid4().hex,
        "session_directory": str(target),
        "created_at": _now(),
        "repository_identity": identity,
        "scope": {
            "kind": manifest["scope"],
            "base": manifest["base"],
            "head": manifest["head"],
            "merge_base": manifest["merge_base"],
        },
        "fingerprints": {
            "initial": initial_manifest["content_fingerprint"],
            "final": manifest["content_fingerprint"],
            "range": manifest["range_fingerprint"],
        },
        "snapshot_history": [
            {
                "kind": "initial",
                "manifest": initial_ref,
                "fingerprint": initial_manifest["content_fingerprint"],
            },
            {
                "kind": "final",
                "manifest": final_ref,
                "fingerprint": manifest["content_fingerprint"],
            },
        ],
        "references": {
            "initial_manifest": initial_ref,
            "manifest": final_ref,
            "draft_result": draft_ref,
            "host_result": None,
            "validated_result": None,
            "report": None,
            "recheck_result": None,
            "recheck_report": None,
            "previous_session": previous_info,
        },
        "workflow_status": "awaiting_semantic_review",
        "review_coverage_status": "uncovered",
        "missing_inputs": ["semantic_result"],
        "steps": [
            {"name": "snapshot_capture", "status": "completed", "at": _now()},
            {"name": "draft_creation", "status": "completed", "at": _now()},
            {"name": "semantic_review", "status": "pending", "at": _now()},
        ],
        "failures": [],
        "attempts": [],
        "attempt_sequence": 0,
    }
    _write_json(target / "session.json", record)
    return record


def add_context(
    session_directory: str | Path, context_paths: list[str | Path]
) -> dict[str, Any]:
    """Capture additional context from the same fixed scope and refresh its draft."""
    session_dir = Path(session_directory).expanduser().resolve()
    try:
        _, record, final, parts = _load_session(session_dir)
        _verify_current_snapshot(record, parts["initial"], final)
        requested = [*final.get("context_files", [])]
        paths = [item["filename"] for item in requested]
        paths.extend(os.fspath(path) for path in context_paths)
        updated = capture_context(final, paths)
        if updated["range_fingerprint"] != final["range_fingerprint"]:
            raise ReviewSessionError("context capture changed the selected range")
        manifest_ref, draft_ref = _save_snapshot_artifacts(session_dir, updated, kind="final")
        if updated["content_fingerprint"] != final["content_fingerprint"]:
            record["snapshot_history"].append(
                {
                    "kind": "final",
                    "manifest": manifest_ref,
                    "fingerprint": updated["content_fingerprint"],
                    "at": _now(),
                }
            )
            record["fingerprints"]["final"] = updated["content_fingerprint"]
            record["references"].update(
                {
                    "manifest": manifest_ref,
                    "draft_result": draft_ref,
                    "host_result": None,
                    "validated_result": None,
                    "report": None,
                    "recheck_result": None,
                    "recheck_report": None,
                }
            )
            record["workflow_status"] = "awaiting_semantic_review"
            record["review_coverage_status"] = "uncovered"
            record["missing_inputs"] = ["semantic_result"]
            record["steps"].append(
                {"name": "context_capture", "status": "completed", "at": _now()}
            )
            _write_json(session_dir / "session.json", record)
        else:
            record["steps"].append(
                {"name": "context_capture", "status": "completed", "at": _now(), "details": "no snapshot change"}
            )
            _write_json(session_dir / "session.json", record)
        return record
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _record_failure(session_dir, "context_capture", exc)
        raise


def resume_session(
    session_directory: str | Path, repository: str | Path | None = None
) -> dict[str, Any]:
    """Verify the session's worktree identity and recapture its selected snapshot."""
    session_dir = Path(session_directory).expanduser().resolve()
    try:
        _, record, final, parts = _load_session(session_dir)
        expected = record["repository_identity"]
        actual = _repository_identity(repository or expected["repository_root"])
        if not _identity_matches(expected, actual):
            raise ReviewSessionError(
                "repository/worktree identity does not match this session; start a new session"
            )
        _verify_current_snapshot(record, parts["initial"], final)
        record["steps"].append({"name": "resume", "status": "completed", "at": _now()})
        _write_json(session_dir / "session.json", record)
        return record
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _record_failure(session_dir, "resume", exc)
        raise


def _new_attempt(record: dict[str, Any], session_dir: Path) -> tuple[int, Path]:
    sequence = int(record.get("attempt_sequence", 0))
    while True:
        sequence += 1
        attempt_dir = session_dir / "runs" / f"{sequence:04d}"
        try:
            attempt_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            continue
    return sequence, attempt_dir


def _input_object(value: dict[str, Any] | str | Path) -> Any:
    if isinstance(value, (str, Path)):
        return _read_json(Path(value).expanduser().resolve())
    return copy.deepcopy(value)


def finalize_session(
    session_directory: str | Path, result: dict[str, Any] | str | Path
) -> dict[str, Any]:
    """Validate a host result and save deterministic validated JSON and Markdown."""
    session_dir = Path(session_directory).expanduser().resolve()
    attempt_dir: Path | None = None
    try:
        _, record, manifest, parts = _load_session(session_dir)
        _verify_current_snapshot(record, parts["initial"], manifest)
        sequence, attempt_dir = _new_attempt(record, session_dir)
        raw_result = _input_object(result)
        _write_json(attempt_dir / "host-result.json", raw_result)
        validated = validate_review(raw_result, manifest)
        report = render_review(validated)
        _write_json(attempt_dir / "validated-result.json", validated)
        _verify_current_snapshot(record, parts["initial"], manifest)
        _write_text(attempt_dir / "report.md", report)
        attempt = {
            "sequence": sequence,
            "kind": "finalize",
            "status": "completed",
            "at": _now(),
            "host_result": (attempt_dir / "host-result.json").relative_to(session_dir).as_posix(),
            "validated_result": (attempt_dir / "validated-result.json").relative_to(session_dir).as_posix(),
            "report": (attempt_dir / "report.md").relative_to(session_dir).as_posix(),
        }
        record["attempt_sequence"] = sequence
        record["attempts"].append(attempt)
        record["references"].update(
            {
                "host_result": attempt["host_result"],
                "validated_result": attempt["validated_result"],
                "report": attempt["report"],
                "recheck_result": None,
                "recheck_report": None,
            }
        )
        record["workflow_status"] = "report_ready"
        record["review_coverage_status"] = validated["review_status"]
        record["missing_inputs"] = _missing_review_inputs(validated)
        record["steps"].extend(
            [
                {
                    "name": "semantic_review",
                    "status": validated["review_status"],
                    "at": _now(),
                },
                {"name": "result_validation", "status": "completed", "at": _now()},
                {"name": "report_render", "status": "completed", "at": _now()},
            ]
        )
        if validated["review_status"] == "failed":
            record["failures"].append(
                {"step": "semantic_review", "at": _now(), "error": "host result reports failed review status"}
            )
        _write_json(session_dir / "session.json", record)
        return record
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if attempt_dir is not None:
            try:
                _write_json(attempt_dir / "failure.json", {"step": "finalize", "error": str(exc), "at": _now()})
            except OSError:
                pass
        _record_failure(session_dir, "finalize", exc, attempt_directory=attempt_dir)
        raise


def recheck_session(
    session_directory: str | Path,
    current_result: dict[str, Any] | str | Path | None = None,
) -> dict[str, Any]:
    """Run the existing strict recheck contract against the linked prior session."""
    session_dir = Path(session_directory).expanduser().resolve()
    attempt_dir: Path | None = None
    try:
        _, record, current_manifest, current_parts = _load_session(session_dir)
        _verify_current_snapshot(record, current_parts["initial"], current_manifest)
        previous_info = record.get("references", {}).get("previous_session")
        if not isinstance(previous_info, dict):
            raise ReviewSessionError("this session has no linked previous review")
        previous_dir, previous_manifest, previous_result = _load_linked_previous_review(
            previous_info, record["repository_identity"]
        )

        sequence, attempt_dir = _new_attempt(record, session_dir)
        raw_current = _input_object(current_result) if current_result is not None else None
        if raw_current is not None:
            _write_json(attempt_dir / "host-result.json", raw_current)
            validated_current = validate_review(raw_current, current_manifest)
            _write_json(attempt_dir / "validated-result.json", validated_current)
            _write_text(attempt_dir / "report.md", render_review(validated_current))
            record["references"].update(
                {
                    "host_result": (attempt_dir / "host-result.json").relative_to(session_dir).as_posix(),
                    "validated_result": (attempt_dir / "validated-result.json").relative_to(session_dir).as_posix(),
                    "report": (attempt_dir / "report.md").relative_to(session_dir).as_posix(),
                }
            )
            record["review_coverage_status"] = validated_current["review_status"]
            current_for_recheck = validated_current
            missing_inputs = _missing_review_inputs(validated_current)
        elif current_parts["validated"] is not None:
            current_for_recheck = current_parts["validated"]
            record["review_coverage_status"] = current_for_recheck["review_status"]
            missing_inputs = _missing_review_inputs(current_for_recheck)
        else:
            current_for_recheck = None
            record["review_coverage_status"] = "uncovered"
            missing_inputs = ["current_semantic_result"]

        _verify_current_snapshot(record, current_parts["initial"], current_manifest)
        result = recheck_review(
            previous_result,
            previous_manifest,
            current_manifest,
            current_for_recheck,
        )
        report = render_recheck(result)
        _write_json(attempt_dir / "recheck.json", result)
        _write_text(attempt_dir / "recheck.md", report)
        attempt = {
            "sequence": sequence,
            "kind": "recheck",
            "status": "completed",
            "at": _now(),
            "recheck_result": (attempt_dir / "recheck.json").relative_to(session_dir).as_posix(),
            "recheck_report": (attempt_dir / "recheck.md").relative_to(session_dir).as_posix(),
        }
        record["attempt_sequence"] = sequence
        record["attempts"].append(attempt)
        record["references"].update(
            {
                "recheck_result": attempt["recheck_result"],
                "recheck_report": attempt["recheck_report"],
            }
        )
        record["workflow_status"] = "recheck_report_ready"
        record["missing_inputs"] = missing_inputs
        record["steps"].append({"name": "recheck", "status": "completed", "at": _now()})
        _write_json(session_dir / "session.json", record)
        return record
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if attempt_dir is not None:
            try:
                _write_json(attempt_dir / "failure.json", {"step": "recheck", "error": str(exc), "at": _now()})
            except OSError:
                pass
        _record_failure(session_dir, "recheck", exc, attempt_directory=attempt_dir)
        raise
