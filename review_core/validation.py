"""Strict validation for version 2 review inputs and findings."""

from __future__ import annotations

import copy
import hashlib
import math
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from review_core.snapshot import _canonical_fingerprint, _manifest_payload


class ReviewValidationError(ValueError):
    """A review result cannot be safely tied to its captured input."""


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SCOPE_VALUES = {"worktree", "staged", "branch"}
_REVIEW_STATUSES = {"completed", "partial", "uncovered", "failed"}
_SEMANTIC_STATUSES = {"completed", "partial", "failed", "uncovered"}
_VALIDATION_STATUSES = {"confirmed", "needs_confirmation"}
_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
_STATIC_STATUSES = {
    "completed", "partial", "failed", "missing", "not_run", "unavailable",
    "unsupported", "skipped",
}
_TOOL_STATUSES = _STATIC_STATUSES | {"timeout"}


def _fail(location: str, message: str) -> None:
    raise ReviewValidationError(f"{location}: {message}")


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(location, "must be an object")
    return value


def _string(value: Any, location: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(location, "must be a string")
    if not allow_empty and not value.strip():
        _fail(location, "must not be empty")
    if "\x00" in value:
        _fail(location, "must not contain a NUL character")
    return value


def _relative_path(value: Any, location: str) -> str:
    path = _string(value, location)
    windows_path = PureWindowsPath(path)
    if "\\" in path or path.startswith("/") or windows_path.is_absolute() or windows_path.drive:
        _fail(location, "must be a repository-relative POSIX path")
    raw_parts = path.split("/")
    parts = PurePosixPath(path).parts
    if not parts or any(part in {"", ".", ".."} for part in raw_parts):
        _fail(location, "must not escape the repository root")
    if parts[0].endswith(":"):
        _fail(location, "must not contain a drive-qualified path")
    return path


def _fingerprint(value: Any, location: str) -> str:
    fingerprint = _string(value, location).lower()
    if not _SHA256.fullmatch(fingerprint):
        _fail(location, "must be a 64-character SHA-256 fingerprint")
    return fingerprint


def _git_sha(value: Any, location: str) -> str | None:
    if value is None:
        return None
    sha = _string(value, location)
    if not _GIT_SHA.fullmatch(sha):
        _fail(location, "must be null or a hexadecimal Git SHA")
    return sha.lower()


def _snapshot(value: Any, location: str) -> dict[str, Any]:
    snapshot = _object(value, location)
    for key in ("path", "content", "fingerprint", "unavailable_reason"):
        if key not in snapshot:
            _fail(f"{location}.{key}", "is required")

    path_value = snapshot["path"]
    path = None if path_value is None else _relative_path(path_value, f"{location}.path")
    content = snapshot["content"]
    if content is not None and not isinstance(content, str):
        _fail(f"{location}.content", "must be a string or null")
    reason = snapshot["unavailable_reason"]
    if reason is not None:
        reason = _string(reason, f"{location}.unavailable_reason")

    fingerprint_value = snapshot["fingerprint"]
    fingerprint = (
        None
        if fingerprint_value is None
        else _fingerprint(fingerprint_value, f"{location}.fingerprint")
    )
    if path is None:
        if content is not None or fingerprint is not None:
            _fail(location, "an absent side cannot have content or a fingerprint")
    elif content is not None:
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if fingerprint is None:
            _fail(f"{location}.fingerprint", "is required when content is captured")
        if fingerprint != expected:
            _fail(f"{location}.fingerprint", "does not match the captured content")
        if reason:
            _fail(f"{location}.unavailable_reason", "must be null when content is available")
    elif not reason:
        _fail(
            f"{location}.unavailable_reason",
            "must explain why a referenced path has no captured content",
        )

    return {
        "path": path,
        "content": content,
        "fingerprint": fingerprint,
        "unavailable_reason": reason,
    }


def _review_file(value: Any, location: str, *, context: bool = False) -> dict[str, Any]:
    item = _object(value, location)
    if context:
        if "filename" in item:
            filename = _relative_path(item["filename"], f"{location}.filename")
        elif "path" in item:
            filename = _relative_path(item["path"], f"{location}.path")
        else:
            _fail(location, "must identify a captured filename or path")
    else:
        filename = _relative_path(item.get("filename"), f"{location}.filename")
        for key in ("previous_filename", "status", "language", "diff"):
            if key not in item:
                _fail(f"{location}.{key}", "is required")

    previous_value = item.get("previous_filename")
    previous_filename = (
        None
        if previous_value is None
        else _relative_path(previous_value, f"{location}.previous_filename")
    )
    before = _snapshot(item.get("before"), f"{location}.before")
    after = _snapshot(item.get("after"), f"{location}.after")

    if context:
        allowed_paths = {filename}
        if previous_filename:
            allowed_paths.add(previous_filename)
        for side in (before, after):
            if side["path"] is not None and side["path"] not in allowed_paths:
                _fail(location, "context snapshot path does not match its filename")
        normalized = copy.deepcopy(item)
        normalized.update({"filename": filename, "before": before, "after": after})
        if "previous_filename" in item:
            normalized["previous_filename"] = previous_filename
        return normalized

    status = _string(item["status"], f"{location}.status").lower()
    language = _string(item["language"], f"{location}.language", allow_empty=True)
    diff = _string(item["diff"], f"{location}.diff", allow_empty=True)
    is_added = status in {"added", "add", "untracked"}
    is_removed = status in {"removed", "deleted", "delete"}
    expected_before = None if is_added else previous_filename or filename
    expected_after = None if is_removed else filename
    if before["path"] != expected_before:
        _fail(f"{location}.before.path", "does not match the input file's old path")
    if after["path"] != expected_after:
        _fail(f"{location}.after.path", "does not match the input file's new path")

    return {
        "filename": filename,
        "previous_filename": previous_filename,
        "status": status,
        "language": language,
        "diff": diff,
        "before": before,
        "after": after,
    }


def _captured_sources(manifest: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    sources: dict[tuple[str, str], dict[str, Any]] = {}

    def add_file(item: dict[str, Any], *, context: bool = False) -> None:
        for side_name in ("before", "after"):
            snapshot = item[side_name]
            path = snapshot["path"]
            if path is None:
                continue
            key = (path, side_name)
            existing = sources.get(key)
            if existing is not None and existing != snapshot:
                _fail(
                    "manifest.context_files",
                    f"conflicting snapshots were supplied for {side_name} path {path!r}",
                )
            sources[key] = snapshot

    for item in manifest["files"]:
        add_file(item)
    for index, raw in enumerate(manifest.get("context_files", [])):
        location = f"manifest.context_files[{index}]"
        item = _review_file(raw, location, context=True)
        add_file(item, context=True)
    return sources


def _manifest(value: Any) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    manifest = _object(value, "manifest")
    for key in (
        "scope", "repository", "base", "head", "head_source", "merge_base", "content_fingerprint",
        "files", "untracked_files", "context_files", "context_policy", "limits",
    ):
        if key not in manifest:
            _fail(f"manifest.{key}", "is required")
    if manifest.get("schema_version") != "2":
        _fail("manifest.schema_version", "must be '2'")
    scope = _string(manifest.get("scope"), "manifest.scope").lower()
    if scope not in _SCOPE_VALUES:
        _fail("manifest.scope", "must be worktree, staged, or branch")
    repository = _string(manifest.get("repository"), "manifest.repository")
    content_fingerprint = _fingerprint(
        manifest.get("content_fingerprint"), "manifest.content_fingerprint"
    )
    files_value = manifest.get("files")
    if not isinstance(files_value, list):
        _fail("manifest.files", "must be an array")
    files = [_review_file(item, f"manifest.files[{i}]") for i, item in enumerate(files_value)]
    names = [item["filename"] for item in files]
    if len(names) != len(set(names)):
        _fail("manifest.files", "must not contain duplicate filenames")

    untracked_value = manifest.get("untracked_files", [])
    if not isinstance(untracked_value, list):
        _fail("manifest.untracked_files", "must be an array")
    untracked: list[str] = []
    for index, item in enumerate(untracked_value):
        if isinstance(item, dict):
            item = item.get("filename")
        untracked.append(_relative_path(item, f"manifest.untracked_files[{index}]"))

    context_value = manifest.get("context_files", [])
    if not isinstance(context_value, list):
        _fail("manifest.context_files", "must be an array")
    context_policy = _object(manifest.get("context_policy"), "manifest.context_policy")
    limits = _object(manifest.get("limits"), "manifest.limits")
    normalized = copy.deepcopy(manifest)
    normalized.update(
        {
            "scope": scope,
            "repository": repository,
            "base": _git_sha(manifest.get("base"), "manifest.base"),
            "head": _git_sha(manifest.get("head"), "manifest.head"),
            "merge_base": _git_sha(manifest.get("merge_base"), "manifest.merge_base"),
            "content_fingerprint": content_fingerprint,
            "files": files,
            "untracked_files": untracked,
            "context_policy": context_policy,
            "limits": limits,
        }
    )
    normalized["context_files"] = [
        _review_file(item, f"manifest.context_files[{i}]", context=True)
        for i, item in enumerate(context_value)
    ]
    try:
        expected_fingerprint = _canonical_fingerprint(_manifest_payload(normalized))
    except (KeyError, TypeError, ValueError) as exc:
        _fail("manifest", f"cannot compute its content fingerprint: {exc}")
    if content_fingerprint != expected_fingerprint:
        _fail("manifest.content_fingerprint", "does not match the captured manifest contents")
    return normalized, _captured_sources(normalized)


def _measurement_number(value: Any, location: str, *, integer: bool) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(location, "must be a non-negative number or null")
    if isinstance(value, float) and not math.isfinite(value):
        _fail(location, "must be a finite non-negative number or null")
    if integer and not isinstance(value, int):
        _fail(location, "must be a non-negative integer or null")
    if value < 0:
        _fail(location, "must be a finite non-negative number or null")
    return value


def _validate_finding(
    value: Any,
    location: str,
    sources: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    finding = _object(value, location)
    required = (
        "finding_id", "category", "severity", "root_cause", "trigger", "impact",
        "change_reason", "suggestion", "validation_status", "evidence", "sources",
    )
    for key in required:
        if key not in finding:
            _fail(f"{location}.{key}", "is required")
    normalized = copy.deepcopy(finding)
    for key in (
        "finding_id", "category", "root_cause", "trigger", "impact",
        "change_reason", "suggestion",
    ):
        normalized[key] = _string(finding[key], f"{location}.{key}")
    severity = _string(finding["severity"], f"{location}.severity").upper()
    if severity not in _SEVERITIES:
        _fail(f"{location}.severity", "must be CRITICAL, HIGH, MEDIUM, or LOW")
    normalized["severity"] = severity
    validation_status = _string(
        finding["validation_status"], f"{location}.validation_status"
    ).lower()
    if validation_status not in _VALIDATION_STATUSES:
        _fail(
            f"{location}.validation_status",
            "must be confirmed or needs_confirmation",
        )
    normalized["validation_status"] = validation_status

    raw_evidence = finding["evidence"]
    if not isinstance(raw_evidence, list) or not raw_evidence:
        _fail(f"{location}.evidence", "must be a non-empty array")
    evidence: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_evidence):
        evidence_location = f"{location}.evidence[{index}]"
        item = _object(raw, evidence_location)
        for key in ("file", "side", "line_start", "line_end", "snapshot_fingerprint", "description"):
            if key not in item:
                _fail(f"{evidence_location}.{key}", "is required")
        filename = _relative_path(item["file"], f"{evidence_location}.file")
        side = _string(item["side"], f"{evidence_location}.side").lower()
        if side not in {"before", "after"}:
            _fail(f"{evidence_location}.side", "must be before or after")
        line_start = item["line_start"]
        line_end = item["line_end"]
        for line, key in ((line_start, "line_start"), (line_end, "line_end")):
            if isinstance(line, bool) or not isinstance(line, int) or line < 1:
                _fail(f"{evidence_location}.{key}", "must be a positive integer")
        if line_end < line_start:
            _fail(evidence_location, "line_end must be greater than or equal to line_start")
        snapshot = sources.get((filename, side))
        if snapshot is None:
            _fail(
                f"{evidence_location}.file",
                "is not captured on the declared before/after side in the manifest",
            )
        content = snapshot["content"]
        if content is None:
            _fail(
                evidence_location,
                "cannot point to a line in a snapshot whose content is unavailable",
            )
        line_count = len(content.splitlines())
        if line_end > line_count:
            _fail(
                f"{evidence_location}.line_end",
                f"exceeds the captured snapshot's {line_count} line(s)",
            )
        snapshot_fingerprint = _fingerprint(
            item["snapshot_fingerprint"], f"{evidence_location}.snapshot_fingerprint"
        )
        if snapshot_fingerprint != snapshot["fingerprint"]:
            _fail(
                f"{evidence_location}.snapshot_fingerprint",
                "does not match the captured before/after snapshot",
            )
        evidence.append(
            {
                **copy.deepcopy(item),
                "file": filename,
                "side": side,
                "line_start": line_start,
                "line_end": line_end,
                "snapshot_fingerprint": snapshot_fingerprint,
                "description": _string(item["description"], f"{evidence_location}.description"),
            }
        )
    normalized["evidence"] = evidence

    raw_sources = finding["sources"]
    if not isinstance(raw_sources, list):
        _fail(f"{location}.sources", "must be an array")
    normalized["sources"] = [
        _string(item, f"{location}.sources[{index}]")
        for index, item in enumerate(raw_sources)
    ]
    return normalized


def _source_availability(item: dict[str, Any]) -> tuple[list[str], int]:
    status = item["status"]
    expected_sides = (
        ("after",)
        if status in {"added", "add", "untracked"}
        else ("before",)
        if status in {"removed", "deleted", "delete"}
        else ("before", "after")
    )
    unavailable = [side for side in expected_sides if item[side]["content"] is None]
    available_count = len(expected_sides) - len(unavailable)
    return unavailable, available_count


def _normalize_review_status(claimed: str, statuses: list[str]) -> str:
    if not statuses:
        derived = "uncovered"
    elif all(status == "failed" for status in statuses):
        derived = "failed"
    elif all(status == "uncovered" for status in statuses):
        derived = "uncovered"
    elif all(status == "completed" for status in statuses):
        derived = "completed"
    else:
        derived = "partial"

    if claimed == "failed" or derived == "failed":
        return "failed"
    if claimed == "partial" or derived == "partial":
        return "partial"
    if derived == "uncovered":
        return "uncovered"
    return "completed"


def validate_review(result: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a v2 result against the exact captured manifest.

    Invalid output raises :class:`ReviewValidationError`; it is never converted
    into an empty successful result. The returned object is an independent copy.
    """
    normalized_manifest, captured_sources = _manifest(manifest)
    raw_result = _object(result, "result")
    if raw_result.get("schema_version") != "2":
        _fail("result.schema_version", "must be '2'")
    input_fingerprint = _fingerprint(
        raw_result.get("input_fingerprint"), "result.input_fingerprint"
    )
    if input_fingerprint != normalized_manifest["content_fingerprint"]:
        _fail("result.input_fingerprint", "does not match the captured input")

    rules_version = _string(raw_result.get("rules_version"), "result.rules_version")
    claimed_status = _string(raw_result.get("review_status"), "result.review_status").lower()
    if claimed_status not in _REVIEW_STATUSES:
        _fail("result.review_status", "must be completed, partial, uncovered, or failed")

    raw_coverage = raw_result.get("coverage")
    if not isinstance(raw_coverage, list):
        _fail("result.coverage", "must be an array")
    expected_files = {item["filename"] for item in normalized_manifest["files"]}
    coverage_by_file: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_coverage):
        location = f"result.coverage[{index}]"
        item = _object(raw, location)
        for key in ("file", "semantic_status", "static_status", "reason", "context_gaps"):
            if key not in item:
                _fail(f"{location}.{key}", "is required")
        filename = _relative_path(item["file"], f"{location}.file")
        if filename not in expected_files:
            _fail(f"{location}.file", "is not part of the selected review range")
        if filename in coverage_by_file:
            _fail(f"{location}.file", "has duplicate coverage records")
        semantic_status = _string(
            item["semantic_status"], f"{location}.semantic_status"
        ).lower()
        if semantic_status not in _SEMANTIC_STATUSES:
            _fail(
                f"{location}.semantic_status",
                "must be completed, partial, failed, or uncovered",
            )
        static_status = _string(item["static_status"], f"{location}.static_status").lower()
        if static_status not in _STATIC_STATUSES:
            _fail(f"{location}.static_status", "is not a supported static status")
        reason = _string(item["reason"], f"{location}.reason", allow_empty=True)
        context_gaps = item["context_gaps"]
        if not isinstance(context_gaps, list):
            _fail(f"{location}.context_gaps", "must be an array")
        normalized_gaps = [
            _string(gap, f"{location}.context_gaps[{gap_index}]")
            for gap_index, gap in enumerate(context_gaps)
        ]
        selected_file = next(
            source for source in normalized_manifest["files"] if source["filename"] == filename
        )
        missing_sides, available_count = _source_availability(selected_file)
        if missing_sides and semantic_status not in {"failed", "uncovered"}:
            semantic_status = "partial" if available_count else "uncovered"
            detail = "required source snapshot unavailable: " + ", ".join(missing_sides)
            reason = f"{reason}; {detail}" if reason else detail
        if normalized_gaps and semantic_status == "completed":
            semantic_status = "partial"
        coverage_by_file[filename] = {
            **copy.deepcopy(item),
            "file": filename,
            "semantic_status": semantic_status,
            "static_status": static_status,
            "reason": reason,
            "context_gaps": normalized_gaps,
        }
    missing_files = expected_files - coverage_by_file.keys()
    if missing_files:
        _fail("result.coverage", f"missing selected file(s): {', '.join(sorted(missing_files))}")
    coverage = [coverage_by_file[item["filename"]] for item in normalized_manifest["files"]]

    raw_findings = raw_result.get("findings")
    if not isinstance(raw_findings, list):
        _fail("result.findings", "must be an array")
    findings = [
        _validate_finding(item, f"result.findings[{index}]", captured_sources)
        for index, item in enumerate(raw_findings)
    ]
    finding_ids = [finding["finding_id"] for finding in findings]
    if len(finding_ids) != len(set(finding_ids)):
        _fail("result.findings", "finding_id values must be unique")

    raw_tool_runs = raw_result.get("tool_runs")
    if not isinstance(raw_tool_runs, list):
        _fail("result.tool_runs", "must be an array")
    tool_runs: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_tool_runs):
        location = f"result.tool_runs[{index}]"
        item = _object(raw, location)
        for key in ("tool", "status"):
            if key not in item:
                _fail(f"{location}.{key}", "is required")
        tool = _string(item["tool"], f"{location}.tool")
        tool_status = _string(item["status"], f"{location}.status").lower()
        if tool_status not in _TOOL_STATUSES:
            _fail(f"{location}.status", "is not a supported tool status")
        normalized_run = {
            **copy.deepcopy(item),
            "tool": tool,
            "status": tool_status,
        }
        if "duration_ms" in item:
            _measurement_number(item["duration_ms"], f"{location}.duration_ms", integer=True)
        if "files_scanned" in item:
            if not isinstance(item["files_scanned"], list):
                _fail(f"{location}.files_scanned", "must be an array")
            normalized_run["files_scanned"] = []
            for file_index, path_value in enumerate(item["files_scanned"]):
                path = _relative_path(path_value, f"{location}.files_scanned[{file_index}]")
                if path not in expected_files:
                    _fail(f"{location}.files_scanned[{file_index}]", "is outside the selected review range")
                normalized_run["files_scanned"].append(path)
        if "findings" in item:
            if not isinstance(item["findings"], list):
                _fail(f"{location}.findings", "must be an array")
            normalized_candidates = []
            for candidate_index, raw_candidate in enumerate(item["findings"]):
                candidate_location = f"{location}.findings[{candidate_index}]"
                candidate = _object(raw_candidate, candidate_location)
                filename = _relative_path(candidate.get("file"), f"{candidate_location}.file")
                if filename not in expected_files:
                    _fail(f"{candidate_location}.file", "is outside the selected review range")
                selected_file = next(
                    source for source in normalized_manifest["files"] if source["filename"] == filename
                )
                line = candidate.get("line")
                if isinstance(line, bool) or not isinstance(line, int) or line < 1:
                    _fail(f"{candidate_location}.line", "must be a positive integer")
                after_content = selected_file["after"]["content"]
                if after_content is None or line > len(after_content.splitlines()):
                    _fail(f"{candidate_location}.line", "is not within a captured after-side source")
                for key in ("rule_id", "severity", "message"):
                    if key in candidate:
                        _string(candidate[key], f"{candidate_location}.{key}", allow_empty=(key == "message"))
                normalized_candidates.append({**copy.deepcopy(candidate), "file": filename})
            normalized_run["findings"] = normalized_candidates
        tool_runs.append(normalized_run)

    limits = _object(raw_result.get("limits"), "result.limits")
    measurements = _object(raw_result.get("measurements"), "result.measurements")
    for key in ("host", "model", "tokens", "cost"):
        if key not in measurements:
            _fail(f"result.measurements.{key}", "is required")
    normalized_measurements = {
        **copy.deepcopy(measurements),
        "host": (
            None
            if measurements["host"] is None
            else _string(measurements["host"], "result.measurements.host")
        ),
        "model": (
            None
            if measurements["model"] is None
            else _string(measurements["model"], "result.measurements.model")
        ),
        "tokens": _measurement_number(
            measurements["tokens"], "result.measurements.tokens", integer=True
        ),
        "cost": _measurement_number(
            measurements["cost"], "result.measurements.cost", integer=False
        ),
    }
    review_status = _normalize_review_status(
        claimed_status, [item["semantic_status"] for item in coverage]
    )
    normalized_result = copy.deepcopy(raw_result)
    normalized_result.update(
        {
            "input_fingerprint": input_fingerprint,
            "rules_version": rules_version,
            "review_status": review_status,
            "findings": findings,
            "coverage": coverage,
            "tool_runs": tool_runs,
            "limits": copy.deepcopy(limits),
            "measurements": normalized_measurements,
        }
    )
    return normalized_result


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a captured schema-version-2 manifest.

    Review results normally validate their manifest as part of
    :func:`validate_review`. Recheck inputs also need to validate a current
    manifest when no new host result is available, so this small public wrapper
    exposes the same strict manifest contract without inventing a result.
    """
    normalized, _ = _manifest(manifest)
    return normalized
