from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_review_skill.py"


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"command failed: {command}\n{result.stdout}\n{result.stderr}")
    return result


def _git(repository: Path, *args: str) -> str:
    result = _run(["git", "-C", str(repository), *args], cwd=repository)
    return result.stdout.strip()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_package(destination: Path) -> Path:
    _run([sys.executable, "-S", str(BUILDER), "--output", str(destination)], cwd=destination.parent)
    return destination / "review-changes"


def _run_skill(package: Path, script: str, *args: str, cwd: Path, env: dict[str, str] | None = None,
               check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(
        [sys.executable, "-S", str(package / "scripts" / script), *args],
        cwd=cwd,
        env=env,
        check=check,
    )


def _init_repository(path: Path) -> None:
    path.mkdir(parents=True)
    _run(["git", "init", "--initial-branch=main", str(path)], cwd=path.parent)
    _git(path, "config", "user.name", "Review Skill Test")
    _git(path, "config", "user.email", "review-skill-test@example.invalid")
    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "src" / "permissions.py").write_text(
        "def can_read(user, record):\n"
        "    return user.is_admin or user.id == record.owner_id\n",
        encoding="utf-8",
    )
    (path / "src" / "handler.go").write_text(
        'package src\n\nfunc Route() string { return "safe" }\n', encoding="utf-8"
    )
    (path / "app.py").write_text(
        "from src.permissions import can_read\n\n"
        "def handle(user, record):\n"
        "    return can_read(user, record)\n",
        encoding="utf-8",
    )
    (path / "tests" / "test_permissions.py").write_text(
        "def test_unrelated_user_is_denied():\n"
        "    assert can_read(unrelated_user, record) is False\n",
        encoding="utf-8",
    )
    _git(path, "add", ".")
    _git(path, "commit", "-m", "baseline")
    _git(path, "switch", "-c", "review-case")

    (path / "src" / "permissions.py").write_text(
        "def can_read(user, record):\n"
        "    return True\n",
        encoding="utf-8",
    )
    _git(path, "add", "src/permissions.py")
    _git(path, "commit", "-m", "introduce authorization regression")

    (path / "src" / "permissions.py").write_text(
        "def can_read(user, record):\n"
        "    return user.is_admin\n",
        encoding="utf-8",
    )
    (path / "src" / "handler.go").write_text(
        'package src\n\nfunc Route() string { return "changed" }\n', encoding="utf-8"
    )
    _git(path, "add", "src/permissions.py", "src/handler.go")

    (path / "src" / "permissions.py").write_text(
        "def can_read(user, record):\n"
        "    return user.is_authenticated\n",
        encoding="utf-8",
    )
    (path / "app.py").write_text(
        "from src.permissions import can_read\n\n"
        "def handle(user, record):\n"
        "    return bool(user) and can_read(user, record)\n",
        encoding="utf-8",
    )


def _manifest(package: Path, repository: Path, scope: str, output: Path, *extra: str) -> dict[str, Any]:
    args = [
        "--repo",
        str(repository),
        "--scope",
        scope,
        "--output",
        str(output),
        *extra,
    ]
    _run_skill(package, "collect_changes.py", *args, cwd=repository.parent)
    return json.loads(output.read_text(encoding="utf-8"))


def _fake_semgrep(directory: Path, *, version_status: int = 0) -> None:
    directory.mkdir()
    executable = directory / "semgrep"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        f"    print('fake semgrep')\n    raise SystemExit({version_status})\n"
        "paths = args[args.index('--quiet') + 1:]\n"
        "print(json.dumps({'results': [], 'errors': []}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def test_independent_package_pins_scopes_context_and_evidence(tmp_path: Path) -> None:
    package = _build_package(tmp_path / "dist")
    repository = tmp_path / "ordinary project"
    _init_repository(repository)

    staged_initial_path = tmp_path / "staged-initial.json"
    staged_initial = _manifest(package, repository, "staged", staged_initial_path)
    assert staged_initial["files"]
    assert next(item for item in staged_initial["files"] if item["filename"] == "src/permissions.py")["after"]["content"].endswith(
        "return user.is_admin\n"
    )

    tampered = json.loads(staged_initial_path.read_text(encoding="utf-8"))
    tampered_file = next(item for item in tampered["files"] if item["filename"] == "src/permissions.py")
    tampered_file["after"]["content"] += "# changed without updating its snapshot hash\n"
    tampered_path = tmp_path / "tampered-initial.json"
    _write_json(tampered_path, tampered)
    rejected = _run_skill(
        package,
        "collect_changes.py",
        "--repo",
        str(repository),
        "--scope",
        "staged",
        "--context",
        "app.py",
        "--compare-to",
        str(tampered_path),
        "--output",
        str(tmp_path / "tampered-final.json"),
        cwd=repository.parent,
        check=False,
    )
    assert rejected.returncode != 0

    branch_path = tmp_path / "branch.json"
    branch = _manifest(
        package,
        repository,
        "branch",
        branch_path,
        "--base",
        "main",
        "--context",
        "app.py",
        "tests/test_permissions.py",
    )
    assert next(item for item in branch["files"] if item["filename"] == "src/permissions.py")["after"]["content"].endswith(
        "return True\n"
    )

    staged_final_path = tmp_path / "staged-final.json"
    staged_final = _manifest(
        package,
        repository,
        "staged",
        staged_final_path,
        "--context",
        "app.py",
        "tests/test_permissions.py",
        "--compare-to",
        str(staged_initial_path),
    )
    context = {item["filename"]: item for item in staged_final["context_files"]}
    assert "app.py" in context
    assert "tests/test_permissions.py" in context
    assert context["app.py"]["after"]["content"].endswith("return can_read(user, record)\n")

    worktree = _manifest(package, repository, "worktree", tmp_path / "worktree.json")
    worktree_source = next(item for item in worktree["files"] if item["filename"] == "src/permissions.py")
    assert worktree_source["after"]["content"].endswith("return user.is_authenticated\n")
    assert staged_final["content_fingerprint"] != worktree["content_fingerprint"]

    fake_bin = tmp_path / "fake-bin"
    _fake_semgrep(fake_bin)
    static_output = tmp_path / "tool-runs.json"
    static_env = dict(os.environ)
    static_env["PATH"] = str(fake_bin) + os.pathsep + static_env.get("PATH", "")
    _run_skill(
        package,
        "run_static.py",
        "--input",
        str(staged_final_path),
        "--output",
        str(static_output),
        cwd=repository.parent,
        env=static_env,
    )
    static = json.loads(static_output.read_text(encoding="utf-8"))
    assert static["input_fingerprint"] == staged_final["content_fingerprint"]
    scan = static["tool_runs"][0]
    assert scan["status"] == "completed"
    assert scan["rules_version"]
    assert "src/permissions.py" in scan["covered_files"]
    assert {item["file"] for item in scan["uncovered_files"]} == {"src/handler.go"}

    finding = {
        "finding_id": "owner-access-1",
        "category": "authorization_denial",
        "severity": "MEDIUM",
        "root_cause": "The permission helper requires administrator status and ignores record ownership.",
        "trigger": "A non-admin user requests their own record.",
        "impact": "The legitimate owner is denied access to their record.",
        "change_reason": "The staged change replaced the owner-or-admin predicate with an administrator-only check.",
        "suggestion": "Allow the owner as well as administrators.",
        "validation_status": "confirmed",
        "evidence": [
            {
                "file": "src/permissions.py",
                "side": "after",
                "line_start": 2,
                "line_end": 2,
                "snapshot_fingerprint": next(
                    item for item in staged_final["files"]
                    if item["filename"] == "src/permissions.py"
                )["after"]["fingerprint"],
                "description": "The permission helper contains the authorization predicate.",
            },
            {
                "file": "app.py",
                "side": "after",
                "line_start": 4,
                "line_end": 4,
                "snapshot_fingerprint": context["app.py"]["after"]["fingerprint"],
                "description": "The selected application caller delegates to the permission helper.",
            },
        ],
        "sources": ["host-semantic-review"],
    }
    result = {
        "schema_version": "2",
        "input_fingerprint": staged_final["content_fingerprint"],
        "rules_version": scan["rules_version"],
        "review_status": "completed",
        "findings": [finding],
        "coverage": [
            {
                "file": item["filename"],
                "semantic_status": "completed" if item["language"] == "python" else "uncovered",
                "static_status": "completed" if item["language"] == "python" else "unsupported",
                "reason": "" if item["language"] == "python" else "No semantic analysis was run for this fixture file.",
                "context_gaps": [] if item["language"] == "python" else ["semantic review not run"],
            }
            for item in staged_final["files"]
        ],
        "tool_runs": scan and static["tool_runs"],
        "limits": {},
        "measurements": {"host": None, "model": None, "tokens": None, "cost": None},
    }
    result_path = tmp_path / "result.json"
    _write_json(result_path, result)
    report_path = tmp_path / "review.md"
    validated_path = tmp_path / "validated-result.json"
    _run_skill(
        package,
        "finalize_review.py",
        "--input",
        str(staged_final_path),
        "--result",
        str(result_path),
        "--output",
        str(report_path),
        "--validated-output",
        str(validated_path),
        cwd=repository.parent,
    )
    report = report_path.read_text(encoding="utf-8")
    validated = json.loads(validated_path.read_text(encoding="utf-8"))
    assert "authorization_denial" in report
    assert "**Status:** partial" in report
    assert validated["review_status"] == "partial"

    branch_context = {item["filename"]: item for item in branch["context_files"]}
    branch_permission = next(
        item for item in branch["files"] if item["filename"] == "src/permissions.py"
    )
    prior_finding = copy.deepcopy(finding)
    prior_finding.update(
        {
            "finding_id": "auth-1",
            "category": "authorization_bypass",
            "severity": "HIGH",
            "root_cause": "The selected caller permits access without an ownership check.",
            "trigger": "A non-admin user requests another user's record.",
            "impact": "The handler can return a record the caller does not own.",
            "change_reason": "The branch change replaced the owner/admin predicate with an unconditional allow.",
            "suggestion": "Restore an owner or administrator authorization check.",
            "evidence": [
                {
                    "file": "src/permissions.py",
                    "side": "after",
                    "line_start": 2,
                    "line_end": 2,
                    "snapshot_fingerprint": branch_permission["after"]["fingerprint"],
                    "description": "The branch version unconditionally allows access.",
                },
                {
                    "file": "app.py",
                    "side": "after",
                    "line_start": 4,
                    "line_end": 4,
                    "snapshot_fingerprint": branch_context["app.py"]["after"]["fingerprint"],
                    "description": "The selected caller delegates without another authorization check.",
                },
                {
                    "file": "tests/test_permissions.py",
                    "side": "after",
                    "line_start": 2,
                    "line_end": 2,
                    "snapshot_fingerprint": branch_context["tests/test_permissions.py"]["after"]["fingerprint"],
                    "description": "The captured test contract denies unrelated users.",
                },
            ],
        }
    )
    prior_result = copy.deepcopy(validated)
    prior_result.update(
        {
            "input_fingerprint": branch["content_fingerprint"],
            "review_status": "completed",
            "findings": [prior_finding],
            "coverage": [
                {
                    "file": item["filename"],
                    "semantic_status": "completed",
                    "static_status": "not_run",
                    "reason": "",
                    "context_gaps": [],
                }
                for item in branch["files"]
            ],
            "tool_runs": [],
        }
    )
    prior_result_path = tmp_path / "prior-result.json"
    _write_json(prior_result_path, prior_result)
    prior_report_path = tmp_path / "prior-review.md"
    prior_validated_path = tmp_path / "prior-validated-result.json"
    _run_skill(
        package,
        "finalize_review.py",
        "--input",
        str(branch_path),
        "--result",
        str(prior_result_path),
        "--output",
        str(prior_report_path),
        "--validated-output",
        str(prior_validated_path),
        cwd=repository.parent,
    )
    prior_validated = json.loads(prior_validated_path.read_text(encoding="utf-8"))
    assert prior_validated["review_status"] == "completed"
    assert "authorization_bypass" in prior_report_path.read_text(encoding="utf-8")

    # Recheck a genuinely changed snapshot. Keep the repaired permission path,
    # its caller, and the existing test contract available as after-side proof.
    (repository / "src" / "permissions.py").write_text(
        "def can_read(user, record):\n"
        "    return user.is_admin or user.id == record.owner_id\n",
        encoding="utf-8",
    )
    (repository / "app.py").write_text(
        "from src.permissions import can_read\n\n"
        "def handle(user, record):\n"
        "    if user is None:\n"
        "        return False\n"
        "    return can_read(user, record)\n",
        encoding="utf-8",
    )
    _git(repository, "add", "src/permissions.py", "app.py")
    fixed_manifest_path = tmp_path / "fixed-manifest.json"
    fixed_manifest = _manifest(
        package,
        repository,
        "staged",
        fixed_manifest_path,
        "--context",
        "app.py",
        "tests/test_permissions.py",
    )
    assert fixed_manifest["content_fingerprint"] != branch["content_fingerprint"]
    assert fixed_manifest["content_fingerprint"] != staged_final["content_fingerprint"]

    fixed_result = copy.deepcopy(prior_validated)
    fixed_result["input_fingerprint"] = fixed_manifest["content_fingerprint"]
    fixed_result["findings"] = []
    fixed_result["coverage"] = [
        {
            "file": item["filename"],
            "semantic_status": "uncovered" if item["language"] == "go" else "completed",
            "static_status": "unsupported" if item["language"] == "go" else "completed",
            "reason": "No semantic analysis was run for this fixture file." if item["language"] == "go" else "",
            "context_gaps": ["semantic review not run"] if item["language"] == "go" else [],
        }
        for item in fixed_manifest["files"]
    ]
    fixed_after_evidence = [
        {
            "file": "src/permissions.py",
            "side": "after",
            "line_start": 2,
            "line_end": 2,
            "snapshot_fingerprint": next(
                item for item in fixed_manifest["files"]
                if item["filename"] == "src/permissions.py"
            )["after"]["fingerprint"],
            "description": "The repaired helper denies non-admin users who do not own the record.",
        },
        {
            "file": "app.py",
            "side": "after",
            "line_start": 6,
            "line_end": 6,
            "snapshot_fingerprint": next(
                item for item in fixed_manifest["files"]
                if item["filename"] == "app.py"
            )["after"]["fingerprint"],
            "description": "The caller rejects a missing user before delegating to authorization.",
        },
        {
            "file": "tests/test_permissions.py",
            "side": "after",
            "line_start": 2,
            "line_end": 2,
            "snapshot_fingerprint": next(
                item for item in fixed_manifest["context_files"]
                if item["filename"] == "tests/test_permissions.py"
            )["after"]["fingerprint"],
            "description": "The captured test contract denies unrelated users.",
        },
    ]
    fixed_result["review_status"] = "partial"
    fixed_result["recheck_decisions"] = [
        {
            "previous_finding_id": "auth-1",
            "status": "resolved",
            "reason": "The updated permission path checks the caller before returning the record.",
            "evidence": fixed_after_evidence,
        }
    ]
    fixed_result_path = tmp_path / "fixed-result.json"
    _write_json(fixed_result_path, fixed_result)
    recheck_output = tmp_path / "recheck.json"
    recheck_report = tmp_path / "recheck.md"
    _run_skill(
        package,
        "recheck_review.py",
        "--previous-manifest",
        str(branch_path),
        "--previous-result",
        str(prior_validated_path),
        "--current-manifest",
        str(fixed_manifest_path),
        "--current-result",
        str(fixed_result_path),
        "--output",
        str(recheck_output),
        "--markdown-output",
        str(recheck_report),
        cwd=repository.parent,
    )
    rechecked = json.loads(recheck_output.read_text(encoding="utf-8"))
    assert rechecked["recheck_status"] == "partial"
    assert rechecked["findings"][0]["status"] == "resolved"
    assert rechecked["current_input_fingerprint"] == fixed_manifest["content_fingerprint"]
    assert rechecked["coverage_gaps"] == [
        {
            "file": "src/handler.go",
            "semantic_status": "uncovered",
            "reason": "No semantic analysis was run for this fixture file.",
        }
    ]
    assert "resolved" in recheck_report.read_text(encoding="utf-8")

    unchanged_result = copy.deepcopy(prior_validated)
    unchanged_result["findings"] = []
    unchanged_result["recheck_decisions"] = [
        {
            "previous_finding_id": "auth-1",
            "status": "resolved",
            "reason": "The permission behavior is now correct.",
            "evidence": [],
        }
    ]
    unchanged_result_path = tmp_path / "unchanged-result.json"
    _write_json(unchanged_result_path, unchanged_result)
    no_proof_output = tmp_path / "no-proof-recheck.json"
    no_proof_report = tmp_path / "no-proof-recheck.md"
    _run_skill(
        package,
        "recheck_review.py",
        "--previous-manifest",
        str(branch_path),
        "--previous-result",
        str(prior_validated_path),
        "--current-manifest",
        str(branch_path),
        "--current-result",
        str(unchanged_result_path),
        "--output",
        str(no_proof_output),
        "--markdown-output",
        str(no_proof_report),
        cwd=repository.parent,
    )
    no_proof = json.loads(no_proof_output.read_text(encoding="utf-8"))
    assert no_proof["recheck_status"] == "partial"
    assert no_proof["current_input_fingerprint"] == prior_validated["input_fingerprint"]
    assert no_proof["findings"][0]["status"] == "unverified"
    assert "evidence" in no_proof["findings"][0]["reason"].casefold()
    assert "**Status:** partial" in no_proof_report.read_text(encoding="utf-8")

    result["findings"][0]["evidence"][0]["file"] = "not-captured.py"
    bad_result = tmp_path / "bad-result.json"
    _write_json(bad_result, result)
    failed = _run_skill(
        package,
        "finalize_review.py",
        "--input",
        str(staged_final_path),
        "--result",
        str(bad_result),
        "--output",
        str(tmp_path / "bad-review.md"),
        cwd=repository.parent,
        check=False,
    )
    assert failed.returncode != 0


def test_compare_to_rejects_live_review_scope_drift(tmp_path: Path) -> None:
    package = _build_package(tmp_path / "dist")
    repository = tmp_path / "ordinary repo"
    _init_repository(repository)
    initial_path = tmp_path / "initial.json"
    _manifest(package, repository, "staged", initial_path)

    source = repository / "src" / "permissions.py"
    source.write_text(
        "def can_read(user, record):\n"
        "    return False\n",
        encoding="utf-8",
    )
    _git(repository, "add", "src/permissions.py")

    final_path = tmp_path / "final.json"
    drifted = _run_skill(
        package,
        "collect_changes.py",
        "--repo",
        str(repository),
        "--scope",
        "staged",
        "--context",
        "app.py",
        "--compare-to",
        str(initial_path),
        "--output",
        str(final_path),
        cwd=repository.parent,
        check=False,
    )
    assert drifted.returncode == 2
    assert "scope changed while context was collected" in drifted.stderr
    assert not final_path.exists()


def test_static_tool_missing_or_version_failure_remains_explicit(tmp_path: Path) -> None:
    package = _build_package(tmp_path / "dist")
    repository = tmp_path / "repo"
    _init_repository(repository)
    manifest_path = tmp_path / "manifest.json"
    _manifest(package, repository, "staged", manifest_path)

    missing_env = dict(os.environ)
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()
    missing_env["PATH"] = str(empty_path)
    missing_output = tmp_path / "missing.json"
    _run_skill(
        package,
        "run_static.py",
        "--input",
        str(manifest_path),
        "--output",
        str(missing_output),
        cwd=repository.parent,
        env=missing_env,
    )
    missing = json.loads(missing_output.read_text(encoding="utf-8"))["tool_runs"][0]
    assert missing["status"] == "missing"
    assert missing["covered_files"] == []
    assert missing["uncovered_files"]

    fake_bin = tmp_path / "failed-bin"
    _fake_semgrep(fake_bin, version_status=9)
    failed_env = dict(os.environ)
    failed_env["PATH"] = str(fake_bin) + os.pathsep + failed_env.get("PATH", "")
    failed_output = tmp_path / "failed.json"
    _run_skill(
        package,
        "run_static.py",
        "--input",
        str(manifest_path),
        "--output",
        str(failed_output),
        cwd=repository.parent,
        env=failed_env,
    )
    failed = json.loads(failed_output.read_text(encoding="utf-8"))["tool_runs"][0]
    assert failed["status"] == "failed"
    assert failed["error_code"] == "semgrep_version_failed"
    assert failed["covered_files"] == []


def test_real_semgrep_scans_the_captured_source_when_available(tmp_path: Path) -> None:
    semgrep = shutil.which("semgrep")
    if semgrep is None:
        local_semgrep = ROOT / ".venv" / "bin" / "semgrep"
        if not local_semgrep.is_file():
            pytest.skip("Semgrep is not installed")
        semgrep = str(local_semgrep)

    package = _build_package(tmp_path / "dist")
    repository = tmp_path / "repo"
    _init_repository(repository)
    query = repository / "src" / "query.py"
    query.write_text(
        "def run(db, user_input):\n"
        "    return db.execute(\"SELECT * FROM users WHERE name = '%s'\" % user_input)\n",
        encoding="utf-8",
    )
    _git(repository, "add", "src/query.py")
    manifest_path = tmp_path / "manifest.json"
    manifest = _manifest(package, repository, "staged", manifest_path)

    output = tmp_path / "tool-runs.json"
    env = dict(os.environ)
    semgrep_dir = str(Path(semgrep).resolve().parent)
    env["PATH"] = semgrep_dir + os.pathsep + env.get("PATH", "")
    _run_skill(
        package,
        "run_static.py",
        "--input",
        str(manifest_path),
        "--output",
        str(output),
        cwd=repository.parent,
        env=env,
    )
    scan = json.loads(output.read_text(encoding="utf-8"))["tool_runs"][0]
    assert scan["status"] == "completed", scan
    assert scan["rules_version"]
    assert "src/query.py" in scan["covered_files"]
    assert any("sql-injection" in finding["rule_id"] for finding in scan["findings"])
