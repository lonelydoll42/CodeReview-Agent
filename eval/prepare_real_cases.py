"""Prepare the current small real-history review batch outside the workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "case0001",
        "source_key": "local",
        "source_repository": "lonelydoll42/CodeReview-Agent",
        "base": "59ddba937ea45b95f79dc8d0c77d955c67e3b812",
        "head": "e8d0788fc58fc2be18d7b1f65410654d6a5f5233",
        "scope_paths": None,
        "related_group": "recheck-hardening",
        "annotation": {
            "annotation_evidence_status": "evidence_supported_not_human_verified",
            "candidate_issue": {
                "root_cause": "A resolved recheck decision can be accepted without any repair evidence.",
                "trigger": "The current result supplies a resolved decision and reason but omits evidence.",
                "impact": "A prior finding may disappear from the report without evidence that its trigger changed.",
            },
            "target_test": "tests/test_recheck.py::test_recheck_requires_explicit_resolution_reason",
            "later_regression_test_reference": {
                "commit": "0a1a8756546ee15801180b06b3d9c6ac3558d481",
                "test": "tests/test_recheck.py::test_resolved_decision_without_evidence_is_unverified",
            },
            "future_fix_is_operator_only": True,
            "probe_status": "target and later regression tests passed in their respective snapshots",
        },
    },
    {
        "case_id": "case0002",
        "source_key": "local",
        "source_repository": "lonelydoll42/CodeReview-Agent",
        "base": "ce99a9fbf77f64093ba5920082648b6221d4f2dd",
        "head": "6749ffffe674a23cc57edda7f47d489b5bf9b257",
        "scope_paths": ["ui/app.py"],
        "related_group": "ui-performance-chain",
        "annotation": {
            "annotation_evidence_status": "pending_no_behavior_test",
            "change_characterization": "Removes a remote font import and adds a local system-font fallback in the same declaration.",
            "expected_scope_outcome": "Undetermined until human review; do not score as a clean control.",
            "test_evidence": None,
        },
    },
    {
        "case_id": "case0003",
        "source_key": "click",
        "source_repository": "pallets/click",
        "base": "cfa6f4ad3e0078db43f866246b483544afa33ed3",
        "head": "262bdf02288daf9a37b9e815b44acafd62b5c6fe",
        "scope_paths": None,
        "related_group": "click-eof-regression",
        "annotation": {
            "annotation_evidence_status": "evidence_supported_not_human_verified",
            "candidate_issue": {
                "root_cause": "The change makes the underlying text stream's iterator raise EOFError globally at exhaustion.",
                "trigger": "A CliRunner consumer iterates or reads the stream through the changed end-of-input path.",
                "impact": "The iterator's EOF behavior changes outside prompt reads and can break callers that rely on normal iteration termination.",
            },
            "later_fix_reference": {
                "commit": "2a0e3ba907927ade6951d5732b775f11b54cb766",
                "message": "testing/CliRunner: Fix regression related to EOF introduced in 262bdf0",
                "changed_behavior": "Removes the global iterator override and translates StopIteration to EOFError only in prompt handlers.",
            },
            "target_behavior_test": "tests/test_utils.py (new EOF behavior coverage in target commit)",
            "operator_probe_status": "pending",
        },
    },
    {
        "case_id": "case0004",
        "source_key": "local",
        "source_repository": "lonelydoll42/CodeReview-Agent",
        "base": "e8d0788fc58fc2be18d7b1f65410654d6a5f5233",
        "head": "0a1a8756546ee15801180b06b3d9c6ac3558d481",
        "scope_paths": None,
        "related_group": "recheck-hardening",
        "annotation": {
            "annotation_evidence_status": "target_tests_present_not_human_adjudicated",
            "change_characterization": "Hardens evidence requirements, relevant coverage checks, conflicting decisions, and one-to-one association.",
            "target_test_evidence": [
                "tests/test_recheck.py::test_resolved_decision_without_evidence_is_unverified",
                "tests/test_recheck.py::test_incomplete_related_file_coverage_blocks_resolution",
                "tests/test_recheck.py::test_resolved_decision_cannot_override_current_matching_finding",
                "tests/test_recheck.py::test_stable_id_match_is_reserved_before_fallback",
                "tests/test_recheck.py::test_ambiguous_fallback_association_keeps_all_prior_findings_unverified",
            ],
            "human_label_status": "not_reviewed",
        },
    },
    {
        "case_id": "case0005",
        "source_key": "local",
        "source_repository": "lonelydoll42/CodeReview-Agent",
        "base": "e27be7374aef778120dd5e1acc9f45e8f627e136",
        "head": "06bfc8a12644c7c2c68c3876f5f21751e8c1e862",
        "scope_paths": None,
        "related_group": "deletion-and-static-analysis-hardening",
        "annotation": {
            "annotation_evidence_status": "target_tests_present_not_human_adjudicated",
            "change_characterization": "Adds review context for deletion-only changes and preserves static scanner failures as failures.",
            "target_test_evidence": [
                "tests/test_deletion_review.py",
                "tests/test_semgrep_runner.py",
                "tests/test_orchestrator_status_contract.py",
            ],
            "human_label_status": "not_reviewed",
        },
    },
    {
        "case_id": "case0006",
        "source_key": "local",
        "source_repository": "lonelydoll42/CodeReview-Agent",
        "base": "0af1f5d5acaa0427ad4cade08916c8fbea22acd1",
        "head": "4a51528d5ea65a7d4095e3c3796e82df1b4b6f3c",
        "scope_paths": None,
        "related_group": "aggregator-correction",
        "annotation": {
            "annotation_evidence_status": "target_tests_present_not_human_adjudicated",
            "change_characterization": "Corrects severity arbitration and aggregates counts for repeated agent results.",
            "target_test_evidence": [
                "tests/test_aggregator.py::test_single_finding_preserves_severity",
                "tests/test_aggregator.py::test_severity_uses_combined_weighted_votes",
                "tests/test_aggregator.py::test_tied_severity_votes_choose_higher_risk",
                "tests/test_aggregator.py::test_agent_stats_sum_findings_across_files",
            ],
            "human_label_status": "not_reviewed",
        },
    },
    {
        "case_id": "case0007",
        "source_key": "local",
        "source_repository": "lonelydoll42/CodeReview-Agent",
        "base": "2fbad67f6c30200a3ff6f2b82d0cf489a0871c29",
        "head": "3827bd6c5e0a38e09f06e00120c6ce435126fb33",
        "scope_paths": None,
        "related_group": "snapshot-storage-hardening",
        "annotation": {
            "annotation_evidence_status": "opt_in_database_test_not_run",
            "change_characterization": "Changes the SQLAlchemy enum server default and adds PostgreSQL integration coverage.",
            "target_test_evidence": "tests/test_database_integration.py is skipped unless TEST_DATABASE_URL is configured.",
            "operator_validation": "not_run_no_disposable_postgresql_configured",
            "human_label_status": "not_reviewed",
        },
    },
    {
        "case_id": "case0008",
        "source_key": "local",
        "source_repository": "lonelydoll42/CodeReview-Agent",
        "base": "b5dc2e2ad96049e10af58ab6b7f9ba3cab04b86b",
        "head": "2fbad67f6c30200a3ff6f2b82d0cf489a0871c29",
        "scope_paths": None,
        "related_group": "snapshot-storage-hardening",
        "annotation": {
            "annotation_evidence_status": "target_unit_tests_present_not_human_adjudicated",
            "change_characterization": "Pins review snapshots, preserves source coordinates, and reuses cached reports.",
            "target_test_evidence": [
                "tests/test_review_snapshot.py",
                "tests/test_review_cache_flow.py",
            ],
            "human_label_status": "not_reviewed",
        },
    },
    {
        "case_id": "case0009",
        "source_key": "local",
        "source_repository": "lonelydoll42/CodeReview-Agent",
        "base": "6749ffffe674a23cc57edda7f47d489b5bf9b257",
        "head": "9c1d43a8abd2fd9f22a1248ab9bb0e670a414f00",
        "scope_paths": ["ui/app.py"],
        "related_group": "ui-performance-chain",
        "annotation": {
            "annotation_evidence_status": "pending_no_behavior_test",
            "change_characterization": "Separates task result cache TTL from list and dashboard TTLs.",
            "expected_scope_outcome": "Undetermined until human review; do not score as a clean control.",
            "test_evidence": None,
        },
    },
    {
        "case_id": "case0010",
        "source_key": "local",
        "source_repository": "lonelydoll42/CodeReview-Agent",
        "base": "06fc0ad27e060642b1be6dc1798b1d7fff25c77c",
        "head": "f35b31e015d2585a2cbd29b9ad1ae3e146eaf0bb",
        "scope_paths": ["skills/review-changes/scripts/review_changes.py"],
        "related_group": "session-cli-portability",
        "annotation": {
            "annotation_evidence_status": "fix_commit_present_future_behavior_test",
            "change_characterization": "Handles UnicodeEncodeError while printing CLI paths on terminals with a limited encoding.",
            "target_commit_message": "fix: print unicode paths from review CLI",
            "subsequent_validation": "The next commit adds package tests; this target commit itself has no dedicated regression test.",
            "human_label_status": "not_reviewed",
        },
    },
    {
        "case_id": "case0011",
        "source_key": "local",
        "source_repository": "lonelydoll42/CodeReview-Agent",
        "base": "9c1d43a8abd2fd9f22a1248ab9bb0e670a414f00",
        "head": "66d9834d48ab04f6a2c381168c4f92718b19af79",
        "scope_paths": ["ui/app.py"],
        "related_group": "ui-performance-chain",
        "annotation": {
            "annotation_evidence_status": "pending_no_behavior_test",
            "change_characterization": "Uses explicit HTTP connect/read/write/pool timeout and connection pool limits.",
            "expected_scope_outcome": "Undetermined until human review; do not score as a clean control.",
            "test_evidence": None,
        },
    },
)

CONDITIONS = ("same_host_direct", "current_skill")


def _git(repository: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _prepare_repository(
    source: Path,
    destination: Path,
    base: str,
    head: str,
) -> tuple[list[str], dict[str, str]]:
    _git(source, "cat-file", "-e", f"{base}^{{commit}}")
    _git(source, "cat-file", "-e", f"{head}^{{commit}}")
    parents = _git(source, "show", "-s", "--format=%P", head).decode().split()
    if base not in parents:
        raise ValueError(f"Base must be a direct parent of the selected head: {base}..{head}.")

    if destination.exists():
        if not (destination / ".git").exists():
            raise FileExistsError(f"Refusing to reuse a non-Git destination: {destination}")
        actual_base = _git(destination, "rev-parse", "refs/bench/base").decode().strip()
        actual_head = _git(destination, "rev-parse", "refs/bench/head").decode().strip()
        remotes = _git(destination, "remote").decode().splitlines()
        if (actual_base, actual_head, remotes) != (base, head, []):
            raise ValueError(f"Existing blind repository identity differs: {destination}")
    else:
        destination.mkdir(parents=True)
        _git(destination, "init", "--quiet")
        _git(destination, "remote", "add", "snapshot-source", str(source))
        _git(destination, "fetch", "--quiet", "--no-tags", "--depth=2", "snapshot-source", head)
        _git(destination, "update-ref", "refs/bench/base", base)
        _git(destination, "update-ref", "refs/bench/head", head)
        _git(destination, "checkout", "--quiet", "--detach", head)
        _git(destination, "remote", "remove", "snapshot-source")

    if _git(destination, "rev-parse", "HEAD").decode().strip() != head:
        raise ValueError(f"Blind worktree is not checked out at the selected head: {destination}")

    changed_paths = _git(destination, "diff", "--name-only", base, head).decode().splitlines()
    if not changed_paths:
        raise ValueError(f"Empty source change: {base}..{head}")
    tree_hashes = {
        "base_tree": _git(destination, "rev-parse", f"{base}^{{tree}}").decode().strip(),
        "head_tree": _git(destination, "rev-parse", f"{head}^{{tree}}").decode().strip(),
    }
    diff = _git(destination, "diff", "--binary", base, head)
    tree_hashes["scoped_diff_sha256"] = hashlib.sha256(diff).hexdigest()
    return changed_paths, tree_hashes


def prepare(
    sources: dict[str, Path], blind_root: Path, operator_root: Path
) -> dict[str, Any]:
    sources = {key: value.resolve() for key, value in sources.items()}
    blind_root = blind_root.resolve()
    operator_root = operator_root.resolve()
    workspace = Path(__file__).resolve().parents[1]
    if blind_root == operator_root or _is_within(blind_root, operator_root) or _is_within(operator_root, blind_root):
        raise ValueError("Blind inputs and operator annotations must use separate roots.")
    for root in (blind_root, operator_root):
        if _is_within(root, workspace) or any(_is_within(root, source) for source in sources.values()):
            raise ValueError(f"Output must be outside the workspace and source repositories: {root}")
        root.mkdir(parents=True, exist_ok=True)

    prepared: list[dict[str, Any]] = []
    for case in CASES:
        case_id = case["case_id"]
        base = case["base"]
        head = case["head"]
        source = sources[case["source_key"]]
        case_root = blind_root / case_id
        paths_by_condition: dict[str, str] = {}
        fingerprints: dict[str, str] | None = None
        changed_paths: list[str] | None = None
        for condition in CONDITIONS:
            repository = case_root / condition / "repository"
            paths, hashes = _prepare_repository(source, repository, base, head)
            if changed_paths is None:
                changed_paths = paths
                fingerprints = hashes
            elif changed_paths != paths or fingerprints != hashes:
                raise RuntimeError("A/B snapshots diverged during preparation.")
            paths_by_condition[condition] = str(repository)

        scope_paths = case["scope_paths"] or changed_paths
        if any(path not in (changed_paths or []) for path in scope_paths):
            raise ValueError(f"Scope contains paths outside the actual change for {case_id}.")
        reviewer_input = {
            "case_id": case_id,
            "scope": "branch",
            "base": base,
            "head": head,
            "scope_paths": scope_paths,
            "snapshot": fingerprints,
        }
        reviewer_input_path = case_root / "reviewer-input.json"
        repositories_path = case_root / "repositories.json"
        repositories_record = {"case_id": case_id, "repositories": paths_by_condition}
        for output_path, value in (
            (reviewer_input_path, reviewer_input),
            (repositories_path, repositories_record),
        ):
            if output_path.exists():
                if json.loads(output_path.read_text(encoding="utf-8")) != value:
                    raise ValueError(f"Refusing to overwrite changed blind input: {output_path}")
            else:
                _write_json(output_path, value)

        annotation_path = operator_root / "annotations" / f"{case_id}.json"
        annotation: dict[str, Any] = {
            "case_id": case_id,
            "source_repository": case["source_repository"],
            "base": base,
            "head": head,
            "source_commit_parent_verified": True,
            "scope_paths": scope_paths,
            "snapshot": fingerprints,
            "human_label_status": "not_reviewed",
            "host_evaluation_status": "not_run",
            "annotation_evidence_status": "pending_operator_adjudication",
        }
        annotation.update(case["annotation"])
        if annotation_path.exists():
            existing = json.loads(annotation_path.read_text(encoding="utf-8"))
            for key in ("case_id", "base", "head", "scope_paths", "snapshot"):
                if existing.get(key) != annotation.get(key):
                    raise ValueError(f"Existing operator annotation identity differs: {annotation_path}")
            annotation = existing
        else:
            _write_json(annotation_path, annotation)

        prepared.append(
            {
                "case_id": case_id,
                "case_origin": "real_repository",
                "source_repository": case["source_repository"],
                "base": base,
                "head": head,
                "scope": "branch",
                "scope_paths": scope_paths,
                "repositories": paths_by_condition,
                "reviewer_input": str(case_root / "reviewer-input.json"),
                "annotation": str(annotation_path),
                "snapshot": fingerprints,
                "related_group": case["related_group"],
                "blind_input_prepared": True,
                "blind_certification": "not_certified",
                "reviewer_access_boundary": "Prompt-restricted; filesystem isolation is not enforced by this preparer.",
                "human_label_status": "not_reviewed",
                "annotation_evidence_status": annotation["annotation_evidence_status"],
                "host_evaluation_status": "not_run",
            }
        )

    return {
        "schema_version": 1,
        "benchmark_id": "review-changes-real-history-v1",
        "status": "prepared_partial",
        "case_count": len(prepared),
        "blind_root": str(blind_root),
        "operator_root": str(operator_root),
        "blind_certification": "not_certified",
        "reviewer_access_boundary": "Prompt-restricted; filesystem isolation is not enforced by this preparer.",
        "cases": prepared,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--click-source", type=Path, required=True)
    parser.add_argument("--blind-root", type=Path, required=True)
    parser.add_argument("--operator-root", type=Path, required=True)
    parser.add_argument("--index", type=Path, default=Path(__file__).resolve().parent / "data" / "real-cases.json")
    args = parser.parse_args()
    index = prepare(
        {"local": args.source_repo, "click": args.click_source},
        args.blind_root,
        args.operator_root,
    )
    _write_json(args.index.resolve(), index)
    print(json.dumps({"status": index["status"], "case_count": index["case_count"], "index": str(args.index.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
