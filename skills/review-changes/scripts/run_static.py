from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from _runtime import SKILL_ROOT, load_json, write_json


def _rules_content() -> str | None:
    bundled = SKILL_ROOT / "static" / "semgrep-rules.yaml"
    if bundled.is_file():
        try:
            return bundled.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    source_root = next(
        (parent for parent in SKILL_ROOT.parents if (parent / "tools" / "semgrep_runner.py").is_file()),
        None,
    )
    if source_root is None:
        return None
    source = source_root / "tools" / "semgrep_runner.py"
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "_RULES_YAML" for target in node.targets):
                continue
            value = node.value
            if isinstance(value, ast.Call) and value.args:
                return textwrap.dedent(ast.literal_eval(value.args[0]))
            content = ast.literal_eval(value)
            return content if isinstance(content, str) else None
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return None
    return None


def _relative_file(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if (
        candidate.is_absolute()
        or PureWindowsPath(path).is_absolute()
        or "\\" in path
        or not candidate.parts
        or ".." in candidate.parts
    ):
        raise ValueError(f"manifest contains an unsafe file path: {path!r}")
    return candidate


def _rule_languages(content: str) -> set[str]:
    languages: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("languages: [") and stripped.endswith("]"):
            languages.update(
                value.strip().strip("'\"")
                for value in stripped[len("languages: [") : -1].split(",")
                if value.strip()
            )
    return languages


def _sources(
    manifest: dict[str, Any],
) -> tuple[list[tuple[str, str, str]], list[dict[str, str]]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifest.files must be a list")
    sources: list[tuple[str, str, str]] = []
    unavailable: list[dict[str, str]] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("manifest.files contains a non-object entry")
        filename = entry.get("filename")
        if not isinstance(filename, str) or not filename:
            raise ValueError("manifest file is missing filename")
        relative = _relative_file(filename)
        after = entry.get("after")
        if not isinstance(after, dict):
            unavailable.append({"file": relative.as_posix(), "reason": "invalid_after_snapshot"})
            continue
        if after.get("path") is None:
            if entry.get("status") in {"removed", "deleted", "delete"}:
                unavailable.append({"file": relative.as_posix(), "reason": "removed_file_not_static_scanned"})
            else:
                unavailable.append({"file": relative.as_posix(), "reason": "no_after_side"})
            continue
        content = after.get("content")
        if isinstance(content, str):
            language = entry.get("language")
            sources.append((relative.as_posix(), str(language or "unknown"), content))
        else:
            unavailable.append(
                {
                    "file": relative.as_posix(),
                    "reason": str(after.get("unavailable_reason") or "content_unavailable"),
                }
            )
    return sources, unavailable


def _uncovered(
    initial: list[dict[str, str]],
    files: list[str],
    reason: str,
) -> list[dict[str, str]]:
    return initial + [{"file": filename, "reason": reason} for filename in files]


def _tool_record(
    *,
    status: str,
    version: str | None,
    rules_version: str | None,
    duration_ms: int,
    files_scanned: list[str],
    covered_files: list[str],
    uncovered_files: list[dict[str, str]],
    findings: list[dict[str, Any]] | None = None,
    error_code: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "tool": "semgrep",
        "version": version,
        "rules_version": rules_version,
        "status": status,
        "duration_ms": duration_ms,
        "files_scanned": files_scanned,
        "covered_files": covered_files,
        "uncovered_files": uncovered_files,
        "findings": findings or [],
        "error_code": error_code,
        "error": error,
    }


def _write_run(path: str, manifest: dict[str, Any], record: dict[str, Any]) -> None:
    write_json(
        path,
        {
            "schema_version": "1",
            "input_fingerprint": manifest.get("content_fingerprint"),
            "tool_runs": [record],
        },
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run optional static checks on a review manifest.")
    parser.add_argument("--input", required=True, help="Captured manifest JSON")
    parser.add_argument("--output", required=True, help="Tool run JSON output path")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        manifest = load_json(args.input)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != "2":
            raise ValueError("input must be a manifest with schema_version '2'")
        sources, unavailable = _sources(manifest)
        rules = _rules_content()
        rules_version = hashlib.sha256(rules.encode("utf-8")).hexdigest() if rules is not None else None
        languages = _rule_languages(rules) if rules is not None else set()
        supported_sources = [source for source in sources if source[1] in languages]
        uncovered = unavailable + [
            {"file": filename, "reason": "language_not_covered_by_rules"}
            for filename, language, _content in sources
            if language not in languages
        ]
        executable = shutil.which("semgrep")
        if executable is None:
            record = _tool_record(
                status="missing",
                version=None,
                rules_version=rules_version,
                duration_ms=0,
                files_scanned=[],
                covered_files=[],
                uncovered_files=_uncovered(
                    uncovered,
                    [filename for filename, _language, _content in supported_sources],
                    "semgrep_not_installed",
                ),
                error_code="semgrep_not_installed",
                error="Semgrep is not installed; semantic review can continue.",
            )
            _write_run(args.output, manifest, record)
            return 0

        if rules is None:
            record = _tool_record(
                status="failed",
                version=None,
                rules_version=None,
                duration_ms=0,
                files_scanned=[],
                covered_files=[],
                uncovered_files=_uncovered(
                    uncovered,
                    [filename for filename, _language, _content in sources],
                    "rules_unavailable",
                ),
                error_code="semgrep_rules_unavailable",
                error="The bundled Semgrep rules could not be loaded.",
            )
            _write_run(args.output, manifest, record)
            return 0

        if not _rule_languages(rules):
            record = _tool_record(
                status="failed",
                version=None,
                rules_version=rules_version,
                duration_ms=0,
                files_scanned=[],
                covered_files=[],
                uncovered_files=_uncovered(
                    uncovered,
                    [filename for filename, _language, _content in sources],
                    "rules_have_no_recognized_languages",
                ),
                error_code="semgrep_rules_invalid",
                error="No supported language declarations were found in the bundled rules.",
            )
            _write_run(args.output, manifest, record)
            return 0

        try:
            version_result = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, timeout=10
            )
            if version_result.returncode != 0:
                raise RuntimeError(f"Semgrep --version exited with status {version_result.returncode}")
            version = version_result.stdout.strip() or None
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            record = _tool_record(
                status="failed",
                version=None,
                rules_version=rules_version,
                duration_ms=0,
                files_scanned=[],
                covered_files=[],
                uncovered_files=_uncovered(
                    uncovered,
                    [filename for filename, _language, _content in supported_sources],
                    "semgrep_version_failed",
                ),
                error_code="semgrep_version_failed",
                error=str(exc),
            )
            _write_run(args.output, manifest, record)
            return 0

        if not supported_sources:
            record = _tool_record(
                status="completed",
                version=version,
                rules_version=rules_version,
                duration_ms=0,
                files_scanned=[],
                covered_files=[],
                uncovered_files=uncovered,
            )
            record["note"] = "No available after-side files match a language in the bundled rules."
            _write_run(args.output, manifest, record)
            return 0

        with tempfile.TemporaryDirectory(prefix="review-changes-static-") as temp_name:
            temp_root = Path(temp_name)
            rules_path = temp_root / "semgrep-rules.yaml"
            rules_path.write_text(rules, encoding="utf-8")
            paths: dict[str, str] = {}
            for filename, _language, content in supported_sources:
                target = temp_root.joinpath(*PurePosixPath(filename).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                paths[str(target.resolve())] = filename

            started = time.monotonic()
            try:
                result = subprocess.run(
                    [
                        executable,
                        "--config",
                        str(rules_path),
                        "--json",
                        "--no-git-ignore",
                        "--quiet",
                        *paths.keys(),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
            except subprocess.TimeoutExpired:
                elapsed = int((time.monotonic() - started) * 1000)
                record = _tool_record(
                    status="failed",
                    version=version,
                    rules_version=rules_version,
                    duration_ms=elapsed,
                    files_scanned=list(paths.values()),
                    covered_files=[],
                    uncovered_files=_uncovered(uncovered, list(paths.values()), "semgrep_scan_failed"),
                    error_code="semgrep_timeout",
                    error="Semgrep exceeded the 90-second limit.",
                )
                _write_run(args.output, manifest, record)
                return 0
            except OSError as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                record = _tool_record(
                    status="failed",
                    version=version,
                    rules_version=rules_version,
                    duration_ms=elapsed,
                    files_scanned=list(paths.values()),
                    covered_files=[],
                    uncovered_files=_uncovered(uncovered, list(paths.values()), "semgrep_scan_failed"),
                    error_code="semgrep_execution_failed",
                    error=str(exc),
                )
                _write_run(args.output, manifest, record)
                return 0

            elapsed = int((time.monotonic() - started) * 1000)
            try:
                data = json.loads(result.stdout or "")
                if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
                    raise ValueError("invalid JSON result shape")
                if data.get("errors"):
                    raise RuntimeError("Semgrep reported scan errors")
                if result.returncode != 0:
                    detail = result.stderr.strip()[:2000]
                    raise RuntimeError(
                        f"Semgrep exited with status {result.returncode}: {detail}"
                    )
                findings: list[dict[str, Any]] = []
                for item in data.get("results", []):
                    if not isinstance(item, dict):
                        raise ValueError("invalid finding shape")
                    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
                    start = item.get("start") if isinstance(item.get("start"), dict) else {}
                    raw_path = Path(str(item.get("path", "")))
                    resolved = raw_path.resolve() if raw_path.is_absolute() else (temp_root / raw_path).resolve()
                    filename = paths.get(str(resolved))
                    if filename is None:
                        raise ValueError("Semgrep returned a path outside the captured snapshot")
                    findings.append(
                        {
                            "file": filename,
                            "line": start.get("line"),
                            "rule_id": item.get("check_id", "unknown"),
                            "severity": extra.get("severity", "INFO"),
                            "message": extra.get("message", ""),
                            "metadata": extra.get("metadata", {}),
                        }
                    )
                record = _tool_record(
                    status="completed",
                    version=version,
                    rules_version=rules_version,
                    duration_ms=elapsed,
                    files_scanned=list(paths.values()),
                    covered_files=list(paths.values()),
                    uncovered_files=uncovered,
                    findings=findings,
                )
            except (json.JSONDecodeError, RuntimeError, ValueError) as exc:
                record = _tool_record(
                    status="failed",
                    version=version,
                    rules_version=rules_version,
                    duration_ms=elapsed,
                    files_scanned=list(paths.values()),
                    covered_files=[],
                    uncovered_files=_uncovered(uncovered, list(paths.values()), "semgrep_scan_failed"),
                    error_code="semgrep_invalid_or_failed_scan",
                    error=str(exc),
                )
        _write_run(args.output, manifest, record)
        return 0
    except (OSError, TypeError, ValueError) as exc:
        print(f"run_static: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
