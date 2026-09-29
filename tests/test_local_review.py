"""Focused tests for the service-free local diff entry point."""
from __future__ import annotations

from unittest.mock import patch

from tools.local_review import parse_unified_diff, review_diff
from tools.semgrep_runner import SecurityIssue


AUTH_DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,4 +1,3 @@
-    if not current_user.is_admin:
-        raise PermissionError()
+    return user.email
"""


def test_parse_unified_diff_preserves_changed_line_coordinates() -> None:
    files = parse_unified_diff(AUTH_DIFF)

    assert len(files) == 1
    assert files[0].filename == "app.py"
    assert files[0].language == "python"
    assert files[0].added_lines == [(1, "    return user.email")]
    assert files[0].removed_lines == [
        (1, "    if not current_user.is_admin:"),
        (2, "        raise PermissionError()"),
    ]


def test_local_review_reports_removed_authorization_guard() -> None:
    report = review_diff(AUTH_DIFF)

    assert any(f.category == "removed_authorization_check" for f in report.findings)
    assert "No database" not in report.markdown_report
    assert report.stats["total"] == 1


def test_local_review_filters_static_findings_to_added_lines() -> None:
    diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 old_secret = "existing"
 password = "new-secret"
+token = "changed-secret"
"""
    issue = SecurityIssue(
        rule_id="hardcoded-secret-python",
        severity="WARNING",
        message="Possible hardcoded secret",
        line=2,
        cwe="CWE-798",
    )
    with patch("tools.local_review.SemgrepRunner.scan", return_value=[issue]):
        report = review_diff(diff)

    assert len(report.findings) == 0


def test_local_review_never_calls_model_for_summary() -> None:
    diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,1 @@
+password = "new-secret"
"""
    # The explicit local mode bypasses the model branch entirely.  The actual
    # client object is still constructed by the shared aggregator, but no
    # request is made.
    with patch("anthropic.resources.messages.Messages.create") as create:
        report = review_diff(diff)

    create.assert_not_called()
    assert report.findings
