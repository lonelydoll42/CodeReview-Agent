from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import List

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class SecurityIssue(BaseModel):
    rule_id: str
    severity: str   # ERROR / WARNING / INFO
    message: str
    line: int
    cwe: str = ""


# ---------------------------------------------------------------------------
# Built-in rules (inline YAML, no network required)
# Each rule targets both Python and JavaScript where applicable.
# ---------------------------------------------------------------------------

_RULES_YAML = textwrap.dedent("""\
rules:

  # ── SQL Injection ────────────────────────────────────────────────────────
  - id: sql-injection-python-format
    languages: [python]
    severity: ERROR
    message: "Possible SQL injection via string formatting (CWE-89)"
    metadata:
      cwe: CWE-89
    pattern-either:
      - pattern: $DB.execute("..." % ...)
      - pattern: $DB.execute("..." + ...)
      - pattern: $DB.execute(f"...")

  - id: sql-injection-python-format2
    languages: [python]
    severity: ERROR
    message: "Possible SQL injection via .format() (CWE-89)"
    metadata:
      cwe: CWE-89
    pattern: $DB.execute("{}".format(...))

  - id: sql-injection-js
    languages: [javascript, typescript]
    severity: ERROR
    message: "Possible SQL injection via string concatenation (CWE-89)"
    metadata:
      cwe: CWE-89
    pattern-either:
      - pattern: $DB.query("..." + ...)
      - pattern: $DB.execute("..." + ...)

  # ── XSS ──────────────────────────────────────────────────────────────────
  - id: xss-innerhtml
    languages: [javascript, typescript]
    severity: ERROR
    message: "Assigning user data to innerHTML can cause XSS (CWE-79)"
    metadata:
      cwe: CWE-79
    pattern: $X.innerHTML = $INPUT

  - id: xss-document-write
    languages: [javascript, typescript]
    severity: ERROR
    message: "document.write() with user input can cause XSS (CWE-79)"
    metadata:
      cwe: CWE-79
    pattern: document.write(...)

  - id: xss-python-jinja-autoescape
    languages: [python]
    severity: WARNING
    message: "Jinja2 environment with autoescape disabled may allow XSS (CWE-79)"
    metadata:
      cwe: CWE-79
    pattern: jinja2.Environment(..., autoescape=False, ...)

  # ── Hardcoded Secrets ────────────────────────────────────────────────────
  - id: hardcoded-secret-python
    languages: [python]
    severity: WARNING
    message: "Possible hardcoded secret or credential (CWE-798)"
    metadata:
      cwe: CWE-798
    pattern-either:
      - pattern: password = "..."
      - pattern: secret = "..."
      - pattern: api_key = "..."
      - pattern: token = "..."
      - pattern: passwd = "..."

  - id: hardcoded-secret-js
    languages: [javascript, typescript]
    severity: WARNING
    message: "Possible hardcoded secret or credential (CWE-798)"
    metadata:
      cwe: CWE-798
    pattern-either:
      - pattern: const password = "..."
      - pattern: const secret = "..."
      - pattern: const apiKey = "..."
      - pattern: const token = "..."
      - pattern: var password = "..."
      - pattern: let password = "..."

  # ── Path Traversal ───────────────────────────────────────────────────────
  - id: path-traversal-python-open
    languages: [python]
    severity: ERROR
    message: "User-controlled path passed to open() may allow path traversal (CWE-22)"
    metadata:
      cwe: CWE-22
    patterns:
      - pattern: open($PATH, ...)
      - pattern-not: open("...", ...)

  - id: path-traversal-python-pathlib
    languages: [python]
    severity: ERROR
    message: "User-controlled path in Path() may allow path traversal (CWE-22)"
    metadata:
      cwe: CWE-22
    patterns:
      - pattern: Path($PATH)
      - pattern-not: Path("...")

  - id: path-traversal-js-fs
    languages: [javascript, typescript]
    severity: ERROR
    message: "User-controlled path in fs operations may allow path traversal (CWE-22)"
    metadata:
      cwe: CWE-22
    pattern-either:
      - pattern: fs.readFile($PATH, ...)
      - pattern: fs.writeFile($PATH, ...)
      - pattern: fs.readFileSync($PATH, ...)
""")

# ---------------------------------------------------------------------------
# Severity normalisation
# ---------------------------------------------------------------------------

_SEVERITY_MAP: dict[str, str] = {
    "error": "ERROR",
    "warning": "WARNING",
    "warn": "WARNING",
    "info": "INFO",
    "note": "INFO",
}


def _normalise_severity(raw: str) -> str:
    return _SEVERITY_MAP.get(raw.lower(), raw.upper())


# ---------------------------------------------------------------------------
# Language → file extension
# ---------------------------------------------------------------------------

_LANG_EXT: dict[str, str] = {
    "python": ".py",
    "javascript": ".js",
    "typescript": ".ts",
    "java": ".java",
    "go": ".go",
    "ruby": ".rb",
    "rust": ".rs",
    "cpp": ".cpp",
    "c": ".c",
    "csharp": ".cs",
    "php": ".php",
}


# ---------------------------------------------------------------------------
# SemgrepRunner
# ---------------------------------------------------------------------------

class SemgrepScanError(RuntimeError):
    """A Semgrep invocation did not produce a trustworthy scan result."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("Semgrep scan failed")


class SemgrepRunner:
    """Run semgrep against a code snippet using the built-in rule set."""

    RULES = ["sql-injection", "xss", "hardcoded-secret", "path-traversal"]

    def __init__(self) -> None:
        self._semgrep_available: bool | None = None

    def _check_semgrep(self) -> bool:
        if self._semgrep_available is None:
            self._semgrep_available = shutil.which("semgrep") is not None
        return self._semgrep_available

    # ------------------------------------------------------------------
    def scan(self, code: str, language: str) -> List[SecurityIssue]:
        """
        Scan *code* for security issues.

        Strategy:
        1. Try semgrep with the bundled inline rules.
        2. If semgrep is not installed, fall back to a lightweight
           regex-based scanner so the tool always returns something useful.
        """
        if not code.strip():
            return []

        if self._check_semgrep():
            return self._scan_with_semgrep(code, language)
        return self._scan_regex_fallback(code, language)

    # ------------------------------------------------------------------
    def _scan_with_semgrep(self, code: str, language: str) -> List[SecurityIssue]:
        ext = _LANG_EXT.get(language, ".txt")
        issues: list[SecurityIssue] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            code_file = tmp / f"target{ext}"
            rules_file = tmp / "rules.yaml"

            code_file.write_text(code, encoding="utf-8")
            rules_file.write_text(_RULES_YAML, encoding="utf-8")

            try:
                result = subprocess.run(
                    [
                        "semgrep",
                        "--config", str(rules_file),
                        "--json",
                        "--no-git-ignore",
                        "--quiet",
                        str(code_file),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                raise SemgrepScanError("semgrep_timeout") from exc
            except OSError as exc:
                raise SemgrepScanError("semgrep_execution_failed") from exc

            try:
                data = json.loads(result.stdout or "")
            except json.JSONDecodeError as exc:
                raise SemgrepScanError("semgrep_invalid_output") from exc

            if not isinstance(data, dict):
                raise SemgrepScanError("semgrep_invalid_output")
            if result.returncode != 0:
                raise SemgrepScanError("semgrep_execution_failed")
            if data.get("errors"):
                raise SemgrepScanError("semgrep_reported_error")

            findings = data.get("results", [])
            if not isinstance(findings, list):
                raise SemgrepScanError("semgrep_invalid_output")

            for finding in findings:
                if not isinstance(finding, dict):
                    raise SemgrepScanError("semgrep_invalid_output")
                meta = finding.get("extra", {}).get("metadata", {})
                cwe = meta.get("cwe", "")
                if isinstance(cwe, list):
                    cwe = cwe[0] if cwe else ""
                issues.append(SecurityIssue(
                    rule_id=finding.get("check_id", "unknown"),
                    severity=_normalise_severity(
                        finding.get("extra", {}).get("severity", "INFO")
                    ),
                    message=finding.get("extra", {}).get("message", ""),
                    line=finding.get("start", {}).get("line", 0),
                    cwe=cwe,
                ))

        return issues

    # ------------------------------------------------------------------
    def _scan_regex_fallback(self, code: str, language: str) -> List[SecurityIssue]:
        """
        Lightweight regex scanner used when semgrep is not installed.
        Covers the same four rule categories at a basic level.
        """
        import re
        issues: list[SecurityIssue] = []
        lines = code.splitlines()

        patterns: list[tuple[str, re.Pattern[str], str, str, str]] = []

        if language == "python":
            patterns = [
                ("sql-injection-python",
                 re.compile(r'\.execute\(.*(%\s|\+|f"|format\()'),
                 "ERROR", "Possible SQL injection via string formatting", "CWE-89"),
                ("hardcoded-secret-python",
                 re.compile(r'(?:password|secret|api_key|token|passwd)\s*=\s*["\'][^"\']{4,}["\']',
                            re.IGNORECASE),
                 "WARNING", "Possible hardcoded secret or credential", "CWE-798"),
                ("path-traversal-python",
                 re.compile(r'\bopen\s*\((?!\s*["\'])'),
                 "ERROR", "User-controlled path passed to open()", "CWE-22"),
                ("xss-python-jinja",
                 re.compile(r'autoescape\s*=\s*False'),
                 "WARNING", "Jinja2 autoescape disabled may allow XSS", "CWE-79"),
            ]
        elif language in ("javascript", "typescript"):
            patterns = [
                ("sql-injection-js",
                 re.compile(r'(?:query|execute)\s*\(.*\+'),
                 "ERROR", "Possible SQL injection via string concatenation", "CWE-89"),
                ("xss-innerhtml",
                 re.compile(r'\.innerHTML\s*='),
                 "ERROR", "Assigning to innerHTML can cause XSS", "CWE-79"),
                ("xss-document-write",
                 re.compile(r'document\.write\s*\('),
                 "ERROR", "document.write() can cause XSS", "CWE-79"),
                ("hardcoded-secret-js",
                 re.compile(r'(?:const|let|var)\s+(?:password|secret|apiKey|token)\s*=\s*["\'][^"\']{4,}["\']',
                            re.IGNORECASE),
                 "WARNING", "Possible hardcoded secret or credential", "CWE-798"),
                ("path-traversal-js",
                 re.compile(r'fs\.(?:readFile|writeFile|readFileSync)\s*\((?!\s*["\'])'),
                 "ERROR", "User-controlled path in fs operations may allow path traversal", "CWE-22"),
            ]

        for rule_id, pattern, severity, message, cwe in patterns:
            for lineno, line in enumerate(lines, start=1):
                if pattern.search(line):
                    issues.append(SecurityIssue(
                        rule_id=rule_id,
                        severity=severity,
                        message=message,
                        line=lineno,
                        cwe=cwe,
                    ))

        return issues
