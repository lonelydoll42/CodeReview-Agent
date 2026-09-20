"""Logic Agent – detects logical defects via AST analysis + Claude tool-use.

Checks performed on *added* lines only:
  1. Null/None dereference without guard
  2. Off-by-one errors and boundary conditions
  3. Bare except / swallowed exceptions
  4. Missing error handling on I/O, network, or DB calls
  5. High cyclomatic complexity (> 10)
  6. Loops that may never terminate
  7. Recursive functions without a clear base case
  8. Silently ignored return values
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
    "name": "report_logic_findings",
    "description": (
        "Report all logical defects discovered in the supplied code diff. "
        "Call this tool exactly once with the complete list of findings. "
        "If there are no issues, call it with an empty list."
    ),
    "input_schema": {
        "type": "object",
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "description": "List of logic findings (may be empty).",
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
                                "null_dereference",
                                "boundary_condition",
                                "bare_except",
                                "missing_error_handling",
                                "high_complexity",
                                "infinite_loop_risk",
                                "unused_return",
                                "infinite_recursion",
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
    function_list: str,
    has_error_handling: bool,
    complexity: int,
) -> str:
    """Build the user prompt for a single chunk of added lines."""
    added_block = "\n".join(
        f"  {lineno:4d} | {text}" for lineno, text in chunk_added
    )
    complexity_str = str(complexity) if complexity >= 0 else "unknown"
    return textwrap.dedent(f"""\
        You are a senior software engineer reviewing code for logical defects.
        Review the following {file_diff.language} code diff (added lines only) from `{file_diff.filename}`.

        Code structure analysis:
        - Functions: {function_list}
        - Has error handling: {has_error_handling}
        - Max cyclomatic complexity: {complexity_str}

        Focus on:
        - Null/None dereference without guard
        - Off-by-one errors and boundary conditions
        - Bare `except:` or `except Exception: pass` (swallowed errors)
        - Missing error handling on I/O, network, or DB calls
        - Functions with cyclomatic complexity > 10 (already detected: {complexity_str})
        - Loops that may never terminate
        - Recursive functions without a clear base case
        - Return values of important calls being silently ignored

        Rules:
        - Only report issues in the shown added lines.
        - confidence >= 0.7 for HIGH/CRITICAL.
        - Provide concrete fix suggestions.

        ## Added lines (format: line_number | code)
        {added_block}

        Call `report_logic_findings` now with all findings (or an empty list).
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

class LogicAgent(BaseReviewAgent):
    """Detects logical defects using AST analysis and Claude."""

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        self._parser = ASTParser()

    async def review(self, file_diff: FileDiff) -> AgentResult:
        start = time.monotonic()
        all_findings: List[Finding] = []
        total_tokens = 0

        if file_diff.added_lines:
            # AST pre-processing (best-effort; non-fatal on failure)
            added_code = file_diff.analysis_source()
            structure = self._parser.parse_python(added_code)
            complexity = self._parser.get_complexity(added_code, file_diff.language)

            function_list = ", ".join(
                f"{fn.name}(line {fn.lineno}, {fn.arg_count} args)"
                for fn in structure.functions
            ) or "none detected"

            chunks = _chunk_added_lines(file_diff.added_lines, MAX_ADDED_LINES_PER_CHUNK)
            for chunk in chunks:
                findings, tokens = self._call_claude(
                    file_diff, chunk, function_list,
                    structure.has_error_handling, complexity,
                )
                all_findings.extend(findings)
                total_tokens += tokens

        return AgentResult(
            agent_name="LogicAgent",
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
        function_list: str,
        has_error_handling: bool,
        complexity: int,
    ) -> tuple[List[Finding], int]:
        """Send one chunk to Claude and return (findings, tokens_used)."""
        prompt = _build_prompt(
            file_diff, chunk_added, function_list, has_error_handling, complexity
        )
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=[REPORT_FINDINGS_TOOL],
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": prompt}],
        )
        tokens = response.usage.input_tokens + response.usage.output_tokens
        return self._parse_tool_response(response, file_diff.filename), tokens

    @staticmethod
    def _parse_tool_response(
        response: anthropic.types.Message,
        filename: str,
    ) -> List[Finding]:
        """Extract findings from the Claude tool-use response."""
        for block in response.content:
            if (
                block.type == "tool_use"
                and block.name == "report_logic_findings"
            ):
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
            return "No logic issues found."
        counts: Dict[str, int] = {}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        parts = [f"{sev}: {n}" for sev, n in sorted(counts.items())]
        return f"Logic review found {len(findings)} issue(s) – {', '.join(parts)}."
