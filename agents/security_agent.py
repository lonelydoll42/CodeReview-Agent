"""Security Agent – detects security vulnerabilities via Semgrep + Claude tool-use.

Checks performed on added lines and deletion-only changes:
  1. SQL injection (CWE-89)
  2. XSS / unsafe HTML injection (CWE-79)
  3. Hardcoded secrets / credentials (CWE-798)
  4. Path traversal (CWE-22)
  5. Command injection (CWE-78)
  6. Insecure deserialization (CWE-502)
  7. Use of dangerous / deprecated APIs
  8. Missing authentication / authorization checks
"""
from __future__ import annotations

import textwrap
import time
from typing import Any, Dict, List

from agents.base import AgentResult, BaseReviewAgent, FileDiff, Finding
from agents.change_context import deletion_chunks, deletion_context
from agents.errors import (
    AgentRuntime,
    AgentRuntimeError,
    max_source_line,
    maybe_await,
    parse_tool_findings,
    run_blocking,
    token_count,
)
from tools.semgrep_runner import SemgrepRunner, SemgrepScanError, SecurityIssue

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "claude-opus-4-6"
MAX_ADDED_LINES_PER_CHUNK = 200

_SEVERITY_MAP = {
    "ERROR": "CRITICAL",
    "WARNING": "HIGH",
    "INFO": "MEDIUM",
}

# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

REPORT_FINDINGS_TOOL: Dict[str, Any] = {
    "name": "report_security_findings",
    "description": (
        "Report all security findings discovered in the supplied code diff. "
        "Call this tool exactly once with the complete list of findings. "
        "If there are no issues, call it with an empty list."
    ),
    "input_schema": {
        "type": "object",
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "description": "List of security findings (may be empty).",
                "items": {
                    "type": "object",
                    "required": [
                        "line_start", "line_end", "severity",
                        "category", "description", "suggestion",
                        "confidence", "cwe",
                    ],
                    "properties": {
                        "line_start": {"type": "integer"},
                        "line_end":   {"type": "integer"},
                        "severity": {
                            "type": "string",
                            "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                        },
                        "category": {
                            "type": "string",
                            "description": (
                                "sql_injection | xss | hardcoded_secret "
                                "| path_traversal | command_injection "
                                "| insecure_deserialization | dangerous_api "
                                "| missing_auth | other"
                            ),
                        },
                        "cwe": {
                            "type": "string",
                            "description": "CWE identifier, e.g. CWE-89",
                        },
                        "description": {"type": "string"},
                        "suggestion":  {"type": "string"},
                        "confidence":  {"type": "number"},
                    },
                },
            }
        },
    },
}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class SecurityAgent(BaseReviewAgent):
    """Review agent that detects security vulnerabilities."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: Any | None = None,
        request_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._runtime = AgentRuntime(
            agent_name="SecurityAgent",
            api_key=api_key,
            client=client,
            request_timeout=request_timeout,
            max_retries=max_retries,
        )
        self._client = self._runtime.client
        self._semgrep = SemgrepRunner()

    async def aclose(self) -> None:
        """Close the shared async provider client during worker shutdown."""
        await self._runtime.close()

    # ------------------------------------------------------------------
    async def review(self, file_diff: FileDiff) -> AgentResult:
        start = time.monotonic()
        if not file_diff.added_lines and not file_diff.removed_lines:
            return AgentResult(
                agent_name="SecurityAgent",
                findings=[],
                summary="No changed lines to review.",
                execution_time=time.monotonic() - start,
                token_used=0,
            )

        # Run Semgrep on the added code first
        # Semgrep invokes a synchronous subprocess/regex scanner.  Cancellation
        # stops waiting for it but cannot terminate a worker thread already in
        # progress; the scanner is therefore kept side-effect free.
        semgrep_issues = (
            await run_blocking(self._run_semgrep, file_diff)
            if file_diff.added_lines else []
        )

        all_findings: List[Finding] = []
        total_tokens = 0

        # Process in chunks
        chunks = self._chunk_lines(file_diff.added_lines) if file_diff.added_lines else []
        for chunk in chunks:
            findings, tokens = await maybe_await(
                self._review_chunk(
                    chunk, file_diff.filename, file_diff.language, semgrep_issues,
                    max_line=max_source_line(file_diff),
                )
            )
            all_findings.extend(findings)
            total_tokens += tokens

        if file_diff.removed_lines:
            for removed_chunk in deletion_chunks(file_diff):
                findings, tokens = await maybe_await(
                    self._review_chunk(
                        [], file_diff.filename, file_diff.language, [],
                        max_line=max_source_line(file_diff),
                        change_context=deletion_context(file_diff, removed_chunk),
                    )
                )
                all_findings.extend(findings)
                total_tokens += tokens

        return AgentResult(
            agent_name="SecurityAgent",
            findings=all_findings,
            summary=self._build_summary(all_findings),
            execution_time=time.monotonic() - start,
            token_used=total_tokens,
        )

    # ------------------------------------------------------------------
    def _run_semgrep(self, file_diff: FileDiff) -> List[SecurityIssue]:
        """Scan complete source and retain only issues on added lines."""
        code = file_diff.analysis_source()
        changed_lines = {number for number, _ in file_diff.added_lines}
        lang = file_diff.language
        try:
            return [issue for issue in self._semgrep.scan(code, lang) if issue.line in changed_lines]
        except AgentRuntimeError:
            raise
        except SemgrepScanError as exc:
            raise AgentRuntimeError(
                exc.reason_code,
                "static security analysis failed",
                retryable=False,
                agent_name="SecurityAgent",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - scanner implementations vary
            raise AgentRuntimeError(
                "static_analysis_failed",
                "static security analysis failed",
                retryable=False,
                agent_name="SecurityAgent",
            ) from exc

    @staticmethod
    def _chunk_lines(
        lines: List[tuple[int, str]],
        size: int = MAX_ADDED_LINES_PER_CHUNK,
    ) -> List[List[tuple[int, str]]]:
        return [lines[i: i + size] for i in range(0, max(len(lines), 1), size)]

    async def _review_chunk(
        self,
        chunk: List[tuple[int, str]],
        filename: str,
        language: str,
        semgrep_issues: List[SecurityIssue],
        *,
        max_line: int = 10**9,
        change_context: str = "",
    ) -> tuple[List[Finding], int]:
        """Send one chunk to Claude and return (findings, tokens)."""
        code_block = "\n".join(f"{ln:4d} | {text}" for ln, text in chunk)

        semgrep_ctx = ""
        if semgrep_issues:
            lines_ctx = []
            for iss in semgrep_issues:
                lines_ctx.append(
                    f"  - Line {iss.line}: [{iss.rule_id}] {iss.message}"
                    + (f" ({iss.cwe})" if iss.cwe else "")
                )
            semgrep_ctx = (
                "\n\nStatic analysis pre-scan found these potential issues "
                "(use as hints, validate each one):\n" + "\n".join(lines_ctx)
            )

        review_scope = (
            "Review the removed protection and current source below. Report only risks "
            "introduced by this deletion; use a suggested head anchor as the finding "
            "line. An old-side line number is not a valid finding coordinate."
            if change_context else
            "Only report issues introduced in the shown added lines."
        )
        code_block = change_context or code_block
        prompt = textwrap.dedent(f"""\
            You are a senior security engineer performing a code security review.
            Review the following {language} code change from `{filename}`.

            Focus on these vulnerability classes:
            - SQL Injection (CWE-89)
            - Cross-Site Scripting / XSS (CWE-79)
            - Hardcoded secrets, passwords, API keys (CWE-798)
            - Path traversal / directory traversal (CWE-22)
            - Command injection / OS injection (CWE-78)
            - Insecure deserialization (CWE-502)
            - Use of dangerous or deprecated APIs
            - Missing authentication or authorization checks

            Rules:
            - {review_scope}
            - Be precise about line numbers.
            - Avoid false positives: only report when you are reasonably confident.
            - Set confidence between 0.0 and 1.0.
            - For CRITICAL findings, confidence must be ≥ 0.7.
            - Provide a concrete remediation suggestion for each finding.
            {semgrep_ctx}

            Changed code and context:
            ```{language}
            {code_block}
            ```

            Call the `report_security_findings` tool with your results.
        """)

        response = await self._runtime.create_message(
            model=MODEL,
            max_tokens=2048,
            tools=[REPORT_FINDINGS_TOOL],
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": prompt}],
        )

        tokens = token_count(response)
        findings = self._parse_findings(response, filename, max_line)
        return findings, tokens

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_findings(
        response: Any,
        filename: str,
        max_line: int = 10**9,
    ) -> List[Finding]:
        return parse_tool_findings(
            response,
            expected_tool_name="report_security_findings",
            filename=filename,
            max_line=max_line,
            agent_name="SecurityAgent",
            allowed_categories={
                "sql_injection",
                "xss",
                "hardcoded_secret",
                "path_traversal",
                "command_injection",
                "insecure_deserialization",
                "dangerous_api",
                "missing_auth",
                "other",
            },
            extra_required_keys={"cwe"},
        )

    @staticmethod
    def _build_summary(findings: List[Finding]) -> str:
        if not findings:
            return "No security issues found."
        counts: Dict[str, int] = {}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        parts = [f"{sev}: {n}" for sev, n in sorted(counts.items())]
        return f"Security review found {len(findings)} issue(s) – {', '.join(parts)}."
