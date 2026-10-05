"""Typed descriptions of the version 2 review interchange format."""

from __future__ import annotations

from typing import Literal, TypedDict


Scope = Literal["worktree", "staged", "branch"]
ReviewStatus = Literal["completed", "partial", "uncovered", "failed"]
SemanticStatus = Literal["completed", "partial", "failed", "uncovered"]
ValidationStatus = Literal["confirmed", "needs_confirmation"]
EvidenceSide = Literal["before", "after"]
HeadSource = Literal["worktree", "index", "commit"]


class Snapshot(TypedDict):
    path: str | None
    content: str | None
    fingerprint: str | None
    unavailable_reason: str | None


class ReviewFile(TypedDict):
    filename: str
    previous_filename: str | None
    status: str
    language: str
    diff: str
    before: Snapshot
    after: Snapshot


class ReviewInput(TypedDict):
    schema_version: Literal["2"]
    scope: Scope
    repository: str
    base: str | None
    head: str | None
    head_source: HeadSource
    merge_base: str | None
    content_fingerprint: str
    files: list[ReviewFile]
    untracked_files: list[str]
    context_files: list[ReviewFile]
    context_policy: dict[str, object]
    limits: dict[str, object]


class Evidence(TypedDict):
    file: str
    side: EvidenceSide
    line_start: int
    line_end: int
    snapshot_fingerprint: str
    description: str


class Finding(TypedDict):
    finding_id: str
    category: str
    severity: str
    root_cause: str
    trigger: str
    impact: str
    change_reason: str
    suggestion: str
    validation_status: ValidationStatus
    evidence: list[Evidence]
    sources: list[str]


class FileCoverage(TypedDict):
    file: str
    semantic_status: SemanticStatus
    static_status: str
    reason: str
    context_gaps: list[str]


class Measurements(TypedDict):
    host: str | None
    model: str | None
    tokens: int | None
    cost: int | float | None


class ReviewResult(TypedDict):
    schema_version: Literal["2"]
    input_fingerprint: str
    rules_version: str
    review_status: ReviewStatus
    findings: list[Finding]
    coverage: list[FileCoverage]
    tool_runs: list[dict[str, object]]
    limits: dict[str, object]
    measurements: Measurements
