"""Review a local Git diff without the service stack.

The normal application reviews GitHub pull requests through PostgreSQL,
Redis, and model-backed agents.  This module is intentionally smaller: it
reads a unified diff, runs the repository's bundled static security checks and
focused change heuristics, and renders the same ``AggregatedReport`` shape.

Examples::

    python -m tools.local_review
    python -m tools.local_review --staged
    python -m tools.local_review --base main --output review.md

No database, Redis instance, Streamlit process, GitHub token, or model API key
is required.  The local path never calls Anthropic, even when a key is present
in the environment.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from agents.aggregator import AggregatedReport, Aggregator
from agents.base import AgentResult, FileDiff, Finding
from tools.semgrep_runner import SemgrepRunner


# Keep local parsing independent from the GitHub client.  The list mirrors the
# languages that can be represented by the existing FileDiff contract and the
# extension map used by tools.github_client.
_EXT_TO_LANGUAGE = {
    "py": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "java": "java",
    "go": "go",
    "rb": "ruby",
    "rs": "rust",
    "cpp": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "c": "c",
    "h": "c",
    "cs": "csharp",
    "php": "php",
    "swift": "swift",
    "kt": "kotlin",
    "scala": "scala",
    "sh": "bash",
    "bash": "bash",
    "zsh": "bash",
    "sql": "sql",
}
_SUPPORTED_LANGUAGES = set(_EXT_TO_LANGUAGE.values())
_MAX_SOURCE_BYTES = 1_048_576

_SEVERITY_FROM_SEMGREP = {
    "ERROR": "CRITICAL",
    "WARNING": "HIGH",
    "INFO": "MEDIUM",
}
_SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}

_AUTH_TERMS = re.compile(
    r"(?:\bauth(?:enticate|entication|orization|orized)?\b"
    r"|\bauthori[sz]e(?:d|r|ation)?\b|\bpermission(?:s)?\b"
    r"|\bis_admin\b|\badmin(?:istrator)?\b|\bcurrent_user\b"
    r"|\bowner(?:_id)?\b|\b(?:login|access)_required\b"
    r"|\b(?:check|verify|require)_?(?:permission|auth|token)\b"
    r"|\brole\b|\bacl\b|\bsession\b|\btoken\b)",
    re.IGNORECASE,
)
_GUARD_TERMS = re.compile(
    r"(?:\b(?:if|elif|assert|raise|return|check|verify|require|ensure|deny|forbid)\b"
    r"|\.(?:is_authenticated|has_permission|check_permission|authorize)\b"
    r"|@(?:login_required|requires?_auth))",
    re.IGNORECASE,
)


def _language_for_path(filename: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return _EXT_TO_LANGUAGE.get(suffix, "unknown")


def _patch_path(line: str) -> str | None:
    """Extract a path from a ``---``/``+++`` header."""
    value = line[4:].split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith("a/") or value.startswith("b/"):
        return value[2:]
    return value


def _parse_patch(patch: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Parse added/removed source lines from one unified diff section."""
    added: list[tuple[int, str]] = []
    removed: list[tuple[int, str]] = []
    old_lineno = new_lineno = 0
    in_hunk = False

    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(
                r"@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@",
                line,
            )
            if match:
                in_hunk = True
                old_lineno = int(match.group("old")) - 1
                new_lineno = int(match.group("new")) - 1
            continue
        if not in_hunk or line.startswith("\\"):
            continue
        if line.startswith("+"):
            new_lineno += 1
            added.append((new_lineno, line[1:]))
        elif line.startswith("-"):
            old_lineno += 1
            removed.append((old_lineno, line[1:]))
        else:
            old_lineno += 1
            new_lineno += 1
    return added, removed


def _read_worktree_source(root: Path, filename: str) -> str | None:
    """Read a small text file from the worktree, if it is available."""
    root = root.resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    try:
        if not candidate.is_file() or candidate.stat().st_size > _MAX_SOURCE_BYTES:
            return None
        data = candidate.read_bytes()
        if b"\x00" in data:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def parse_unified_diff(diff_text: str, source_root: Path | str | None = None) -> list[FileDiff]:
    """Parse a complete ``git diff`` payload into existing ``FileDiff`` models.

    Unsupported files are omitted because the current agent contract only
    promises source review for supported programming languages.
    """
    if not diff_text.strip():
        return []

    lines = diff_text.splitlines()
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("diff --git ") and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)
    if not sections:
        sections = [lines]

    root = Path(source_root or Path.cwd())
    parsed: list[FileDiff] = []
    for section in sections:
        old_path: str | None = None
        new_path: str | None = None
        for line in section:
            if line.startswith("--- "):
                old_path = _patch_path(line)
            elif line.startswith("+++ "):
                new_path = _patch_path(line)
        filename = new_path or old_path
        if not filename or new_path is None:
            # Deleted files have no head-side source to inspect.  Their
            # removed authorization checks are still represented by a normal
            # FileDiff when a path is available.
            filename = old_path
        if not filename:
            continue
        language = _language_for_path(filename)
        if language not in _SUPPORTED_LANGUAGES:
            continue
        added, removed = _parse_patch("\n".join(section))
        status = "modified"
        if old_path is None:
            status = "added"
        elif new_path is None:
            status = "removed"
        elif old_path != new_path:
            status = "renamed"
        parsed.append(
            FileDiff(
                filename=filename,
                language=language,
                added_lines=added,
                removed_lines=removed,
                raw_diff="\n".join(section),
                full_source=_read_worktree_source(root, filename),
                status=status,
                previous_filename=old_path if old_path != filename else None,
            )
        )
    return parsed


def _category_for_rule(rule_id: str) -> str:
    rule = rule_id.lower()
    for needle, category in (
        ("sql", "sql_injection"),
        ("xss", "xss"),
        ("secret", "hardcoded_secret"),
        ("path", "path_traversal"),
        ("command", "command_injection"),
    ):
        if needle in rule:
            return category
    return "security_issue"


def _suggestion_for_category(category: str) -> str:
    return {
        "sql_injection": "Use parameterized queries and bind user-controlled values.",
        "xss": "Encode untrusted output or use a safe escaping/template API.",
        "hardcoded_secret": "Move credentials to a secret manager or environment configuration.",
        "path_traversal": "Validate and constrain user-controlled paths before filesystem access.",
        "command_injection": "Avoid shell interpolation and pass trusted argument lists to subprocess APIs.",
        "removed_authorization_check": "Restore an explicit authorization check before returning protected data.",
        "dangerous_eval": "Avoid evaluating untrusted text as code; use a constrained parser instead.",
        "dangerous_exec": "Avoid executing dynamically constructed code.",
        "bare_except": "Handle the expected exception explicitly and preserve unexpected errors.",
    }.get(category, "Review the changed code and add an appropriate guard or validation.")


def _security_finding(
    file_diff: FileDiff,
    line: int,
    severity: str,
    category: str,
    description: str,
    confidence: float,
) -> Finding:
    return Finding(
        file=file_diff.filename,
        line_start=max(line, 1),
        line_end=max(line, 1),
        severity=severity,
        category=category,
        description=description,
        suggestion=_suggestion_for_category(category),
        confidence=confidence,
    )


def _review_file(file_diff: FileDiff, semgrep: SemgrepRunner) -> list[AgentResult]:
    changed_lines = {line for line, _ in file_diff.added_lines}
    security_findings: list[Finding] = []
    logic_findings: list[Finding] = []
    source = file_diff.analysis_source()

    # SemgrepRunner includes a regex fallback, so this remains useful on a
    # fresh checkout where the semgrep executable has not been installed.
    issues = semgrep.scan(source, file_diff.language)
    for issue in issues:
        if issue.line not in changed_lines:
            continue
        category = _category_for_rule(issue.rule_id)
        severity = _SEVERITY_FROM_SEMGREP.get(issue.severity.upper(), "MEDIUM")
        detail = issue.message or "Static security rule matched this changed line."
        if issue.cwe and issue.cwe not in detail:
            detail = f"{detail} ({issue.cwe})"
        security_findings.append(
            _security_finding(
                file_diff,
                issue.line,
                severity,
                category,
                detail,
                confidence=0.9,
            )
        )

    added = list(file_diff.added_lines)
    for index, (line, text) in enumerate(added):
        stripped = text.strip()
        if re.search(r"\b(?:eval|exec)\s*\(", text):
            category = "dangerous_eval" if re.search(r"\beval\s*\(", text) else "dangerous_exec"
            security_findings.append(
                _security_finding(
                    file_diff,
                    line,
                    "HIGH",
                    category,
                    "Dynamic code evaluation was introduced on a changed line.",
                    confidence=0.86,
                )
            )
        if re.search(r"\bsubprocess\.(?:run|Popen|call|check_output)\b", text) and re.search(
            r"\bshell\s*=\s*True\b", text
        ):
            security_findings.append(
                _security_finding(
                    file_diff,
                    line,
                    "HIGH",
                    "command_injection",
                    "A subprocess call enables shell interpretation on a changed line.",
                    confidence=0.88,
                )
            )

        if re.match(r"^\s*except(?:\s+Exception)?\s*:\s*$", text):
            following = added[index + 1][1].strip() if index + 1 < len(added) else ""
            if following == "pass":
                logic_findings.append(
                    _security_finding(
                        file_diff,
                        line,
                        "MEDIUM",
                        "bare_except",
                        "The changed exception handler silently discards errors.",
                        confidence=0.92,
                    )
                )

    # A deleted guard is the key local-diff regression that cannot be found by
    # scanning added lines alone.  If a replacement guard is added in the same
    # hunk, leave the change for human review instead of duplicating it.
    replacement_guard = any(_AUTH_TERMS.search(text) and _GUARD_TERMS.search(text) for _, text in added)
    for line, text in file_diff.removed_lines:
        if replacement_guard or not (_AUTH_TERMS.search(text) and _GUARD_TERMS.search(text)):
            continue
        security_findings.append(
            _security_finding(
                file_diff,
                line,
                "HIGH",
                "removed_authorization_check",
                f"An authorization-related guard was removed: `{text.strip()}`",
                confidence=0.9,
            )
        )

    results = [
        AgentResult(
            agent_name="SecurityAgent",
            findings=security_findings,
            summary=(
                f"Local static security checks found {len(security_findings)} issue(s)."
                if security_findings
                else "No local static security issues found."
            ),
            execution_time=0.0,
            token_used=0,
        ),
        AgentResult(
            agent_name="LogicAgent",
            findings=logic_findings,
            summary=(
                f"Local logic checks found {len(logic_findings)} issue(s)."
                if logic_findings
                else "No local logic issues found."
            ),
            execution_time=0.0,
            token_used=0,
        ),
    ]
    return results


def review_diff(
    diff_text: str,
    source_root: Path | str | None = None,
) -> AggregatedReport:
    """Review a unified diff and return the standard aggregated report."""
    file_diffs = parse_unified_diff(diff_text, source_root=source_root)
    semgrep = SemgrepRunner()
    results: list[AgentResult] = []
    for file_diff in file_diffs:
        results.extend(_review_file(file_diff, semgrep))

    aggregator = Aggregator(enable_llm_summary=False)
    return aggregator.aggregate(
        results,
        pr_url="",
        pr_metadata={
            "source": "local git diff",
            "files_reviewed": len(file_diffs),
        },
    )


def _git_diff(root: Path, staged: bool = False, base: str | None = None) -> str:
    command = ["git", "diff", "--no-ext-diff", "--no-color", "--unified=80"]
    if staged:
        command.append("--cached")
    elif base:
        command.append(base)
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"Unable to run git: {exc}") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip() or "git diff failed"
        raise RuntimeError(message)
    return completed.stdout


def _read_diff_file(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Unable to read diff file {path!r}: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review local changes with bundled static checks; no services or model key required."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Git worktree used for the diff and source snapshots (default: current directory).",
    )
    parser.add_argument("--staged", action="store_true", help="Review staged changes instead of the working tree.")
    parser.add_argument("--base", help="Review changes relative to a base ref, e.g. main.")
    parser.add_argument(
        "--diff-file",
        metavar="PATH",
        help="Read a unified diff from PATH, or '-' for stdin, instead of running git diff.",
    )
    parser.add_argument("--output", type=Path, help="Write the report to PATH instead of stdout.")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument(
        "--fail-on",
        choices=("CRITICAL", "HIGH", "MEDIUM", "LOW"),
        help="Exit with status 1 when a finding reaches this severity.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        diff_text = _read_diff_file(args.diff_file) if args.diff_file else _git_diff(root, args.staged, args.base)
        report = review_diff(diff_text, source_root=root)
    except RuntimeError as exc:
        parser.error(str(exc))

    if args.format == "json":
        output = report.model_dump_json(indent=2)
    else:
        output = report.markdown_report
    output = output.rstrip() + "\n"
    if args.output:
        try:
            args.output.write_text(output, encoding="utf-8")
        except OSError as exc:
            parser.error(f"Unable to write report {args.output}: {exc}")
    else:
        sys.stdout.write(output)

    if args.fail_on:
        threshold = _SEVERITY_RANK[args.fail_on]
        if any(_SEVERITY_RANK.get(f.severity, 0) >= threshold for f in report.findings):
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m``
    raise SystemExit(main())
