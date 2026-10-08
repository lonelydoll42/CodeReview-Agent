from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.prepare_workbuddy_acceptance import _extract_verified


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_workbuddy_acceptance.py"
RELEASE = ROOT / "dist" / "review-changes-0.2.1"
CASE_IDS = (
    "runtime-missing-record",
    "safe-authorization-move",
    "cross-file-contract",
)
HOST_IDS = {
    "runtime-missing-record": "case-01",
    "safe-authorization-move": "case-02",
    "cross-file-contract": "case-03",
}


def _run(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-S", str(SCRIPT), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_preparation_and_followups_work_outside_source_checkout(tmp_path: Path) -> None:
    launcher = tmp_path / "unrelated 工作目录 with spaces"
    launcher.mkdir()
    output = tmp_path / "WorkBuddy 验收 输出 0.2.1"
    result = _run(
        launcher,
        "prepare",
        "--release-dir",
        str(RELEASE),
        "--output",
        str(output),
    )
    assert result.returncode == 0, result.stderr

    provenance = json.loads((output / "provenance" / "verification.json").read_text())
    assert provenance["verified"] is True
    assert provenance["version"] == "0.2.1"
    assert provenance["files_verified"] == 23
    assert provenance["extracted_from_verified_zip"] is True
    verified_package = output / "verified-release" / "review-changes"
    assert not list(verified_package.rglob("__pycache__"))

    run_record = json.loads((output / "run-record.json").read_text())
    host_outcomes = json.loads((output / "host-outcomes.json").read_text())
    assert run_record["preparation_status"] == "ready"
    assert run_record["host_installation"] == "pending"
    assert all(value is None for value in run_record["unknown_counts"].values())
    assert host_outcomes["status"] == "not_run"
    assert host_outcomes["installation"]["status"] == "pending"
    acceptance_readme = (output / "README.md").read_text()
    assert "provenance/review-changes.zip" in acceptance_readme
    assert "verified-release/review-changes/" in acceptance_readme
    assert "Neither form is presumed supported" in acceptance_readme
    assert "Import that folder" not in acceptance_readme
    review_prompt = (output / "host-prompts" / "review.txt").read_text()
    assert "operator/probes" not in review_prompt

    expected_context = {
        "runtime-missing-record": {"tests/test_records.py", "pyproject.toml"},
        "safe-authorization-move": {
            "tests/test_router.py",
            "pyproject.toml",
        },
        "cross-file-contract": {"views.py", "tests/test_views.py", "pyproject.toml"},
    }
    for case_id in CASE_IDS:
        case = run_record["cases"][case_id]
        host_id = HOST_IDS[case_id]
        repository = Path(case["repository"])
        assert case["host_id"] == host_id
        assert " " in repository.name and any(ord(char) > 127 for char in repository.name)
        assert not any(word in repository.name.lower() for word in ("runtime", "auth", "contract"))
        assert (repository / ".git").is_dir()
        assert (repository / ".git" / "HEAD").is_file()
        assert case["baseline_commit"]
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=repository,
            capture_output=True,
            check=False,
        )
        assert untracked.returncode == 0
        assert untracked.stdout == b""

        tests = json.loads(Path(case["test_record"]).read_text())
        assert tests["baseline"]["exit_code"] == 0
        assert tests["baseline"]["expectation_met"] is True
        expected_review_exit = 1 if case_id == "cross-file-contract" else 0
        assert tests["review_changes"]["exit_code"] == expected_review_exit
        assert tests["review_changes"]["expectation_met"] is True
        assert tests["follow_up"] is None
        assert tests["baseline"]["stdout"] or tests["baseline"]["stderr"]
        assert tests["review_changes"]["stdout"] or tests["review_changes"]["stderr"]
        assert not list(repository.rglob("__pycache__"))

        if case_id == "runtime-missing-record":
            fixture_tests = repository / "tests" / "test_records.py"
            assert "test_missing_record_has_a_safe_label" not in fixture_tests.read_text()
            probe_record = json.loads(Path(case["probe_record"]).read_text())
            probe_script = Path(probe_record["probe"])
            assert repository not in probe_script.parents
            assert probe_script.is_relative_to(output / "operator")
            assert "record_label({}, 'missing')" in probe_script.read_text()
            assert probe_record["phases"]["baseline"]["exit_code"] == 0
            assert probe_record["phases"]["review_changes"]["exit_code"] == 1
            assert probe_record["phases"]["follow_up"] is None
            assert probe_record["preparation_expectations_met"] is True
        else:
            assert case["probe_record"] is None

        manifest = json.loads(Path(case["review_snapshot"]).read_text())
        assert manifest["repository"] == str(repository.resolve())
        assert host_id in case["review_snapshot"]
        assert case_id not in case["review_snapshot"]
        context_paths = {item["filename"] for item in manifest["context_files"]}
        assert context_paths == expected_context[case_id]
        changed_paths = {item["filename"] for item in manifest["files"]}
        assert changed_paths

    for case_id in CASE_IDS:
        assert (output / "host-artifacts" / HOST_IDS[case_id]).is_dir()

    untracked_case = run_record["cases"]["runtime-missing-record"]
    untracked_repository = Path(untracked_case["repository"])
    record_source = untracked_repository / "records.py"
    original_source = record_source.read_bytes()

    staged = subprocess.run(
        ["git", "add", "records.py"],
        cwd=untracked_repository,
        capture_output=True,
        check=False,
    )
    assert staged.returncode == 0, staged.stderr.decode(errors="replace")
    rejected_index = _run(
        launcher,
        "apply-fix",
        "--output",
        str(output),
        "--case",
        "runtime-missing-record",
    )
    assert rejected_index.returncode == 2
    assert "index changed" in rejected_index.stderr
    subprocess.run(
        ["git", "reset", "--quiet", "HEAD", "--", "records.py"],
        cwd=untracked_repository,
        check=True,
    )

    record_source.write_bytes(original_source + b"# unexpected tracked edit\n")
    rejected_worktree = _run(
        launcher,
        "apply-fix",
        "--output",
        str(output),
        "--case",
        "runtime-missing-record",
    )
    assert rejected_worktree.returncode == 2
    assert "worktree changed" in rejected_worktree.stderr
    record_source.write_bytes(original_source)

    untracked_path = untracked_repository / "unexpected.py"
    untracked_path.write_text("value = 1\n", encoding="utf-8")
    rejected_untracked = _run(
        launcher,
        "apply-fix",
        "--output",
        str(output),
        "--case",
        "runtime-missing-record",
    )
    assert rejected_untracked.returncode == 2
    assert "untracked files" in rejected_untracked.stderr
    assert not (output / "apply-results").exists()
    untracked_path.unlink()

    original_record = (output / "run-record.json").read_bytes()
    second_prepare = _run(
        launcher,
        "prepare",
        "--release-dir",
        str(RELEASE),
        "--output",
        str(output),
    )
    assert second_prepare.returncode == 2
    assert (output / "run-record.json").read_bytes() == original_record

    for case_id in CASE_IDS:
        host_id = HOST_IDS[case_id]
        case = run_record["cases"][case_id]
        applied = _run(
            launcher,
            "apply-fix",
            "--output",
            str(output),
            "--case",
            case_id,
        )
        assert applied.returncode == 0, applied.stderr
        apply_result = json.loads((output / "apply-results" / f"{host_id}.json").read_text())
        assert apply_result["test_result"]["exit_code"] == 0
        assert apply_result["test_result"]["expectation_met"] is True
        project_tests = json.loads(Path(case["test_record"]).read_text())
        assert project_tests["follow_up"]["exit_code"] == 0
        assert project_tests["follow_up"]["expectation_met"] is True
        assert project_tests["baseline"]["exit_code"] == 0
        assert project_tests["review_changes"]["exit_code"] == (
            1 if case_id == "cross-file-contract" else 0
        )
        if case_id == "runtime-missing-record":
            probe_record = json.loads(Path(case["probe_record"]).read_text())
            assert probe_record["phases"]["follow_up"]["exit_code"] == 0
            assert probe_record["phases"]["follow_up"]["expectation_met"] is True
            assert apply_result["operator_probe_result"]["exit_code"] == 0
        else:
            assert apply_result["operator_probe_result"] is None
        assert Path(apply_result["current_snapshot"]).is_file()
        assert apply_result["review_snapshot"] != apply_result["current_snapshot"]
        assert host_id in apply_result["current_snapshot"]
        assert case_id not in apply_result["current_snapshot"]
        assert apply_result["host_recheck"] == "pending"
        assert apply_result["host_export"] == "pending"
        current_manifest = json.loads(Path(apply_result["current_snapshot"]).read_text())
        assert current_manifest["repository"] == str(
            Path(run_record["cases"][case_id]["repository"]).resolve()
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=Path(run_record["cases"][case_id]["repository"]),
            capture_output=True,
            check=False,
        )
        assert untracked.returncode == 0
        assert untracked.stdout == b""
        assert not list(Path(run_record["cases"][case_id]["repository"]).rglob("__pycache__"))

    outcomes_after_fixtures = json.loads((output / "host-outcomes.json").read_text())
    assert outcomes_after_fixtures["status"] == "not_run"
    assert all(
        outcome["review"]["status"] == "pending"
        and outcome["recheck"]["status"] == "pending"
        and outcome["export"]["status"] == "pending"
        for outcome in outcomes_after_fixtures["cases"].values()
    )
    assert {path.name for path in (output / "host-artifacts").iterdir()} == set(HOST_IDS.values())
    assert not list(verified_package.rglob("__pycache__"))


def test_release_hash_failure_stops_before_creating_output(tmp_path: Path) -> None:
    tampered_release = tmp_path / "tampered release"
    tampered_release.mkdir()
    for name in (
        "review-changes.zip",
        "review-changes.manifest.json",
        "review-changes.manifest.json.sha256",
    ):
        shutil.copyfile(RELEASE / name, tampered_release / name)
    archive = tampered_release / "review-changes.zip"
    archive.write_bytes(archive.read_bytes() + b"tampered")
    output = tmp_path / "should not be created"
    result = _run(
        tmp_path,
        "prepare",
        "--release-dir",
        str(tampered_release),
        "--output",
        str(output),
    )
    assert result.returncode == 2
    assert "ZIP hash does not match" in result.stderr
    assert not output.exists()


def test_rehashed_same_version_release_is_rejected_by_pinned_manifest(tmp_path: Path) -> None:
    tampered_release = tmp_path / "self-consistent replacement"
    tampered_release.mkdir()
    archive_path = tampered_release / "review-changes.zip"
    with (
        zipfile.ZipFile(RELEASE / "review-changes.zip") as original,
        zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as replacement,
    ):
        for info in original.infolist():
            replacement.writestr(info, original.read(info))
        replacement.writestr("review-changes/extra.txt", b"replacement file\n")

    manifest = json.loads((RELEASE / "review-changes.manifest.json").read_text())
    manifest["files"].append(
        {
            "path": "extra.txt",
            "sha256": hashlib.sha256(b"replacement file\n").hexdigest(),
        }
    )
    manifest["files"].sort(key=lambda item: item["path"])
    inventory = json.dumps(
        manifest["files"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest["source_tree_sha256"] = hashlib.sha256(inventory).hexdigest()
    manifest["archive_sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    (tampered_release / "review-changes.manifest.json").write_bytes(manifest_bytes)
    (tampered_release / "review-changes.manifest.json.sha256").write_text(
        hashlib.sha256(manifest_bytes).hexdigest() + "\n", encoding="ascii"
    )

    output = tmp_path / "must reject replacement"
    result = _run(
        tmp_path,
        "prepare",
        "--release-dir",
        str(tampered_release),
        "--output",
        str(output),
    )
    assert result.returncode == 2
    assert "pinned 0.2.1" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "member",
    (
        r"review-changes/..\..\outside.txt",
        "review-changes/../../outside.txt",
        "review-changes/C:/outside.txt",
        r"review-changes/\\server\share\outside.txt",
    ),
)
def test_extraction_rejects_posix_and_windows_traversal_members(
    tmp_path: Path, member: str
) -> None:
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as malicious:
        malicious.writestr(member, b"do not extract\n")

    package_root = tmp_path / "output" / "review-changes"
    with pytest.raises(ValueError, match="unsafe|unexpected"):
        _extract_verified(archive, package_root)
    assert not (tmp_path / "outside.txt").exists()


def test_extraction_rejects_package_root_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "valid.zip"
    with zipfile.ZipFile(archive, "w") as valid:
        valid.writestr("review-changes/SKILL.md", "name: review-changes\n")
    outside = tmp_path / "outside package root"
    outside.mkdir()
    package_root = tmp_path / "output" / "review-changes"
    package_root.parent.mkdir()
    package_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="package root must not be a symbolic link"):
        _extract_verified(archive, package_root)
    assert list(outside.iterdir()) == []


def test_apply_fix_rejects_paths_outside_output_symlinks_and_moved_runs(
    tmp_path: Path,
) -> None:
    launcher = tmp_path / "launch"
    launcher.mkdir()
    output = tmp_path / "acceptance output"
    prepared = _run(
        launcher,
        "prepare",
        "--release-dir",
        str(RELEASE),
        "--output",
        str(output),
    )
    assert prepared.returncode == 0, prepared.stderr
    record_path = output / "run-record.json"
    original_bytes = record_path.read_bytes()
    record = json.loads(original_bytes)
    case_id = "runtime-missing-record"
    repo = Path(record["cases"][case_id]["repository"])

    outside_repo = tmp_path / "outside repo"
    outside_repo.mkdir()
    (outside_repo / ".git").mkdir()
    record["cases"][case_id]["repository"] = str(outside_repo.resolve())
    record_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    outside_repo_attempt = _run(
        launcher, "apply-fix", "--output", str(output), "--case", case_id
    )
    assert outside_repo_attempt.returncode == 2
    assert "does not match this acceptance output" in outside_repo_attempt.stderr
    record_path.write_bytes(original_bytes)

    outside_patch = tmp_path / "outside.patch"
    outside_patch.write_text("not an authorized patch\n", encoding="utf-8")
    record = json.loads(original_bytes)
    record["cases"][case_id]["prepared_patch"] = str(outside_patch.resolve())
    record["cases"][case_id]["prepared_patch_sha256"] = hashlib.sha256(
        outside_patch.read_bytes()
    ).hexdigest()
    record_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    outside_patch_attempt = _run(
        launcher, "apply-fix", "--output", str(output), "--case", case_id
    )
    assert outside_patch_attempt.returncode == 2
    assert "does not match this acceptance output" in outside_patch_attempt.stderr
    record_path.write_bytes(original_bytes)

    saved_repo = repo.with_name(repo.name + " saved")
    repo.rename(saved_repo)
    try:
        repo.symlink_to(saved_repo, target_is_directory=True)
        symlink_attempt = _run(
            launcher, "apply-fix", "--output", str(output), "--case", case_id
        )
        assert symlink_attempt.returncode == 2
        assert "symbolic link" in symlink_attempt.stderr
    finally:
        if repo.is_symlink():
            repo.unlink()
        saved_repo.rename(repo)

    moved_output = tmp_path / "moved acceptance output"
    output.rename(moved_output)
    moved_attempt = _run(
        launcher, "apply-fix", "--output", str(moved_output), "--case", case_id
    )
    assert moved_attempt.returncode == 2
    assert "does not match this acceptance output" in moved_attempt.stderr
    moved_output.rename(output)
    assert not (output / "apply-results").exists()
