"""Performance Agent – detects efficiency issues via Claude tool-use.

Checks performed on *added* lines only:
  1. N+1 query patterns (ORM/DB calls inside loops)
  2. Loop-invariant computations that could be hoisted
  3. Unnecessary list/dict copies
  4. High cyclomatic complexity (> 10)
  5. Inefficient data structures (list for membership test)
  6. Synchronous/blocking I/O inside async functions
  7. Redundant re-computation of the same value in a loop
"""
from __future__ import annotations

import os
import textwrap
import time
from typing import Any, Dict, List

import anthropic

from agents.base import AgentResult, BaseReviewAgent, FileDiff, Finding
from tools.ast_parser import ASTParser

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "claude-opus-4-6"
MAX_ADDED_LINES_PER_CHUNK = 200

# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

REPORT_FINDINGS_TOOL: Dict[str, Any] = {
    "name": "report_performance_findings",
    "description": (
        "Report all performance findings discovered in the supplied code diff. "
        "Call this tool exactly once with the complete list of findings. "
        "If there are no issues, call it with an empty list."
    ),
    "input_schema": {
        "type": "object",
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "description": "List of performance findings (may be empty).",
                "items": {
                    "type": "object",
                    "required": [
                        "line_start", "line_end", "severity",
                        "category", "description", "suggestion", "confidence",
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
                            "enum": [
                                "n_plus_one",
                                "loop_invariant",
                                "unnecessary_copy",
                                "high_complexity",
                                "inefficient_structure",
                                "blocking_call",
                                "redundant_computation",
                                "other",
                            ],
                        },
                        "description": {"type": "string"},
                        "suggestion":  {"type": "string"},
                        "confidence":  {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(
    file_diff: FileDiff,
    chunk_added: List[tuple[int, str]],
    complexity: int,
) -> str:
    """Build the user prompt for a single chunk of added lines."""
    added_block = "\n".join(
        f"  {lineno:4d} | {text}" for lineno, text in chunk_added
    )
    complexity_note = (
        f"{complexity} (⚠️ exceeds threshold of 10)"
        if complexity > 10
        else str(complexity)
    )
    return textwrap.dedent(f"""\
        You are a performance engineer reviewing code for efficiency issues.
        Review the following {file_diff.language} code diff from `{file_diff.filename}`.

        Complexity analysis: max cyclomatic complexity = {complexity_note}

        Focus on:
        - N+1 query patterns: ORM calls or DB queries inside loops
        - Loop-invariant computations that could be hoisted out of the loop
        - Unnecessary list/dict copies (e.g. list(some_list) when not needed)
        - High cyclomatic complexity functions (> 10) that are hard to optimize
        - Inefficient data structures (list for membership test instead of set)
        - Synchronous/blocking I/O calls inside async functions
        - Redundant re-computation of the same value in a loop

        Rules:
        - Only report issues in the shown added lines.
        - Do NOT report style issues; focus purely on performance.
        - confidence >= 0.6 for MEDIUM and above.
        - Provide concrete fix suggestions.

        ## Added lines (format: line_number | code)
        {added_block}

        Call `report_performance_findings` now with all findings (or an empty list).
    """)


# ---------------------------------------------------------------------------
# Chunking helper
# ---------------------------------------------------------------------------

def _chunk_added_lines(
    added_lines: List[tuple[int, str]],
    chunk_size: int,
) -> List[List[tuple[int, str]]]:
    """Split added_lines into sublists of at most chunk_size entries."""
    if not added_lines:
        return [[]]
    return [
        added_lines[i : i + chunk_size]
        for i in range(0, len(added_lines), chunk_size)
    ]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class PerformanceAgent(BaseReviewAgent):
    """Reviews code diffs for performance issues using Claude tool-use."""

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        self._parser = ASTParser()

    async def review(self, file_diff: FileDiff) -> AgentResult:
        """Run performance review on file_diff and return an AgentResult."""
        start = time.monotonic()

        # Pre-compute complexity from all added line text
        added_code = file_diff.analysis_source()
        complexity = self._parser.get_complexity(added_code, file_diff.language)

        all_findings: List[Finding] = []
        total_tokens = 0

        chunks = _chunk_added_lines(file_diff.added_lines, MAX_ADDED_LINES_PER_CHUNK)
        for chunk in chunks:
            findings, tokens = self._call_claude(file_diff, chunk, complexity)
            all_findings.extend(findings)
            total_tokens += tokens

        return AgentResult(
            agent_name="PerformanceAgent",
            findings=all_findings,
            summary=self._build_summary(all_findings),
            execution_time=time.monotonic() - start,
            token_used=total_tokens,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call_claude(
        self,
        file_diff: FileDiff,
        chunk_added: List[tuple[int, str]],
        complexity: int,
    ) -> tuple[List[Finding], int]:
        """Send one chunk to Claude and return (findings, tokens_used)."""
        prompt = _build_prompt(file_diff, chunk_added, complexity)
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=[REPORT_FINDINGS_TOOL],
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": prompt}],
        )
        tokens = response.usage.input_tokens + response.usage.output_tokens
        return self._parse_response(response, file_diff.filename), tokens

    @staticmethod
    def _parse_response(
        response: anthropic.types.Message,
        filename: str,
    ) -> List[Finding]:
        """Extract Finding objects from a Claude tool-use response."""
        for block in response.content:
            if (
                block.type != "tool_use"
                or block.name != "report_performance_findings"
            ):
                continue
            raw: List[Dict[str, Any]] = block.input.get("findings", [])
            results: List[Finding] = []
            for item in raw:
                try:
                    results.append(
                        Finding(
                            file=filename,
                            line_start=item["line_start"],
                            line_end=item["line_end"],
                            severity=item["severity"],
                            category=item["category"],
                            description=item["description"],
                            suggestion=item["suggestion"],
                            confidence=float(item["confidence"]),
                        )
                    )
                except (KeyError, ValueError):
                    continue
            return results
        return []

    @staticmethod
    def _build_summary(findings: List[Finding]) -> str:
        """Return a one-line summary of findings grouped by severity."""
        if not findings:
            return "No performance issues found."
        counts: Dict[str, int] = {}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        parts = [f"{sev}: {n}" for sev, n in sorted(counts.items())]
        return f"Performance review found {len(findings)} issue(s) – {', '.join(parts)}."
