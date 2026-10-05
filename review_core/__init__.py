"""Dependency-free contracts and deterministic helpers for code reviews."""

from review_core.dedup import deduplicate_findings
from review_core.report import render_review
from review_core.validation import ReviewValidationError, validate_review

__all__ = [
    "ReviewValidationError",
    "deduplicate_findings",
    "render_review",
    "validate_review",
]
