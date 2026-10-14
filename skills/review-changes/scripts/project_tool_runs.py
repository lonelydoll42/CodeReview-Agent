from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RUN_FIELDS = (
    "tool",
    "version",
    "rules_version",
    "status",
    "duration_ms",
    "files_scanned",
    "covered_files",
    "uncovered_files",
    "findings",
    "error_code",
    "error",
)
FINDING_FIELDS = ("file", "line", "rule_id", "severity", "message", "metadata")


class ProjectionError(ValueError):
    """Raised when a run_static result cannot be represented without data loss."""


def require_utf8_filesystem() -> bool:
    if sys.getfilesystemencoding().lower().replace("-", "") == "utf8":
        return True
    print(
        "Python is using a non-UTF-8 filesystem encoding. Restart this command with "
        "`python3 -X utf8 -S ...` (Windows: `py -3 -X utf8 -S ...`). No files were written.",
        file=sys.stderr,
    )
    return False


def _object(
    value: Any,
    location: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectionError(f"{location} must be an object")
    missing = required - value.keys()
    unknown = value.keys() - required - (optional or set())
    if missing:
        raise ProjectionError(f"{location} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ProjectionError(f"{location} has unsupported fields: {', '.join(sorted(unknown))}")
    return value


def _string(value: Any, location: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ProjectionError(f"{location} must be {'a' if allow_empty else 'a non-empty'} string")
    return value


def _nullable_string(value: Any, location: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ProjectionError(f"{location} must be a string or null")


def _metadata_json(value: Any, location: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProjectionError(f"{location} is not representable as strict UTF-8 JSON") from exc
    return encoded


def _project_finding(raw: Any, location: str) -> dict[str, Any]:
    finding = _object(raw, location, set(FINDING_FIELDS))
    _string(finding["file"], f"{location}.file")
    line = finding["line"]
    if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
        raise ProjectionError(f"{location}.line must be a positive integer or null")
    _string(finding["rule_id"], f"{location}.rule_id")
    _string(finding["severity"], f"{location}.severity")
    _string(finding["message"], f"{location}.message", allow_empty=True)
    return {
        "file": finding["file"],
        "line": line,
        "rule_id": finding["rule_id"],
        "severity": finding["severity"],
        "message": finding["message"],
        "metadata_json": _metadata_json(finding["metadata"], f"{location}.metadata"),
    }


def _project_run(raw: Any, index: int) -> dict[str, Any]:
    location = f"tool_runs[{index}]"
    run = _object(raw, location, set(RUN_FIELDS), {"note"})
    _string(run["tool"], f"{location}.tool")
    for field in ("version", "rules_version", "error_code", "error"):
        _nullable_string(run[field], f"{location}.{field}")
    status = _string(run["status"], f"{location}.status")
    if status not in {"missing", "failed", "completed"}:
        raise ProjectionError(f"{location}.status is unsupported")
    duration = run["duration_ms"]
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        raise ProjectionError(f"{location}.duration_ms must be a non-negative integer")
    for field in ("files_scanned", "covered_files"):
        files = run[field]
        if not isinstance(files, list):
            raise ProjectionError(f"{location}.{field} must be an array")
        for file_index, filename in enumerate(files):
            _string(filename, f"{location}.{field}[{file_index}]")
    uncovered = run["uncovered_files"]
    if not isinstance(uncovered, list):
        raise ProjectionError(f"{location}.uncovered_files must be an array")
    normalized_uncovered = []
    for file_index, raw_file in enumerate(uncovered):
        entry_location = f"{location}.uncovered_files[{file_index}]"
        entry = _object(raw_file, entry_location, {"file", "reason"})
        normalized_uncovered.append({
            "file": _string(entry["file"], f"{entry_location}.file"),
            "reason": _string(entry["reason"], f"{entry_location}.reason"),
        })
    findings = run["findings"]
    if not isinstance(findings, list):
        raise ProjectionError(f"{location}.findings must be an array")
    note = run.get("note")
    _nullable_string(note, f"{location}.note")
    return {
        "tool": run["tool"],
        "version": run["version"],
        "rules_version": run["rules_version"],
        "status": status,
        "duration_ms": duration,
        "files_scanned": list(run["files_scanned"]),
        "covered_files": list(run["covered_files"]),
        "uncovered_files": normalized_uncovered,
        "findings": [
            _project_finding(item, f"{location}.findings[{finding_index}]")
            for finding_index, item in enumerate(findings)
        ],
        "error_code": run["error_code"],
        "error": run["error"],
        "note": note,
    }


def project_tool_runs(payload: Any) -> list[dict[str, Any]]:
    """Project a complete run_static JSON envelope into the closed A/B contract."""
    try:
        json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProjectionError("input must contain strict UTF-8 JSON values") from exc
    envelope = _object(
        payload,
        "run_static output",
        {"schema_version", "input_fingerprint", "tool_runs"},
    )
    if envelope["schema_version"] != "1":
        raise ProjectionError("run_static output schema_version must be '1'")
    _nullable_string(envelope["input_fingerprint"], "run_static output.input_fingerprint")
    runs = envelope["tool_runs"]
    if not isinstance(runs, list):
        raise ProjectionError("run_static output.tool_runs must be an array")
    projected = [_project_run(run, index) for index, run in enumerate(runs)]
    try:
        json.dumps(projected, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProjectionError("projected output is not strict UTF-8 JSON") from exc
    return projected


def main() -> int:
    if not require_utf8_filesystem():
        return 2
    parser = argparse.ArgumentParser(
        description="Project run_static JSON into the closed A/B host-output tool_runs format."
    )
    parser.add_argument(
        "--input",
        default="-",
        help="run_static JSON envelope to project (default: read standard input)",
    )
    args = parser.parse_args()
    try:
        source = sys.stdin.buffer.read() if args.input == "-" else Path(args.input).read_bytes()
        payload = json.loads(source.decode("utf-8"))
        projected = project_tool_runs(payload)
        sys.stdout.write(json.dumps(projected, ensure_ascii=False, allow_nan=False) + "\n")
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ProjectionError) as exc:
        print(f"project_tool_runs: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
