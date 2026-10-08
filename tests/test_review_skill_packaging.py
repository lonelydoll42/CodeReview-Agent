from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_review_skill.py"


def test_builder_creates_hashed_standalone_package(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--output", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    package = output / "review-changes"
    archive = output / "review-changes.zip"
    manifest_path = output / "review-changes.manifest.json"
    checksum_path = output / "review-changes.manifest.json.sha256"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)

    assert (package / "SKILL.md").is_file()
    assert (package / "review_core").is_dir()
    assert (package / "static" / "semgrep-rules.yaml").is_file()
    assert hashlib.sha256(manifest_bytes).hexdigest() == checksum_path.read_text().strip()
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive_sha256"]
    assert manifest["source_tree_sha256"]
    assert {entry["path"] for entry in manifest["files"]} >= {
        "SKILL.md",
        "review_core/__init__.py",
        "static/semgrep-rules.yaml",
    }
    assert all("__pycache__" not in entry["path"] and not entry["path"].endswith(".pyc") for entry in manifest["files"])
    with zipfile.ZipFile(archive) as bundle:
        assert "review-changes/SKILL.md" in bundle.namelist()
        assert "review-changes/review_core/__init__.py" in bundle.namelist()


def test_builder_refuses_to_overwrite_existing_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    first = subprocess.run(
        [sys.executable, str(BUILDER), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    archive = output / "review-changes.zip"
    original = archive.read_bytes()
    second = subprocess.run(
        [sys.executable, str(BUILDER), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 2
    assert archive.read_bytes() == original


def test_unpacked_skill_runs_from_an_external_unicode_repository(tmp_path: Path) -> None:
    build_output = tmp_path / "skill-build"
    build_result = subprocess.run(
        [sys.executable, "-S", str(BUILDER), "--output", str(build_output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert build_result.returncode == 0, build_result.stderr

    archive = build_output / "review-changes.zip"
    manifest_path = build_output / "review-changes.manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["name"] == "review-changes"
    assert manifest["archive"] == archive.name
    assert manifest["version"] == (ROOT / "skills" / "review-changes" / "VERSION").read_text(
        encoding="utf-8"
    ).strip()
    source_revision = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert source_revision.returncode == 0, source_revision.stderr
    assert manifest["source_revision"] == source_revision.stdout.strip()
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive_sha256"]
    assert hashlib.sha256(manifest_bytes).hexdigest() == (
        build_output / "review-changes.manifest.json.sha256"
    ).read_text(encoding="ascii").strip()
    inventory_bytes = json.dumps(
        manifest["files"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert manifest["source_tree_sha256"] == hashlib.sha256(inventory_bytes).hexdigest()
    package_files = {item["path"] for item in manifest["files"]}
    assert {
        "review_core/session.py",
        "scripts/review_changes.py",
        "references/session.md",
    } <= package_files

    extraction_root = tmp_path / "临时 解包"
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        expected_members = {
            f"review-changes/{item['path']}" for item in manifest["files"]
        }
        assert set(bundle.namelist()) == expected_members
        bundle.extractall(extraction_root)
    package = extraction_root / "review-changes"
    assert (package / "VERSION").read_text(encoding="utf-8").strip() == manifest["version"]
    for item in manifest["files"]:
        extracted_file = package / Path(item["path"])
        assert hashlib.sha256(extracted_file.read_bytes()).hexdigest() == item["sha256"]

    repository = tmp_path / "外部项目" / "中文 示例 仓库"
    repository.mkdir(parents=True)
    init = subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        cwd=repository.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert init.returncode == 0, init.stderr

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            cwd=repository.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    git("config", "user.name", "Standalone Skill Test")
    git("config", "user.email", "skill-test@example.invalid")
    git("config", "core.autocrlf", "false")
    source = repository / "src" / "worker.py"
    source.parent.mkdir()
    source.write_bytes(b"def compute():\n    return 0\n")
    git("add", ".")
    git("commit", "-m", "baseline")
    git("switch", "-c", "feature/portable")
    source.write_bytes(b"def compute():\n    return 1\n")
    git("add", "src/worker.py")
    git("commit", "-m", "change compute")
    branch_head = git("rev-parse", "HEAD")

    output_dir = tmp_path / "验收 输出"

    def collect(scope: str) -> dict[str, object]:
        output = output_dir / f"{scope}.json"
        command = [
            sys.executable,
            "-S",
            str(package / "scripts" / "collect_changes.py"),
            "--repo",
            str(repository),
            "--scope",
            scope,
            "--output",
            str(output),
        ]
        if scope == "branch":
            command.extend(("--base", "main"))
        result = subprocess.run(
            command,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(output.read_text(encoding="utf-8"))

    branch = collect("branch")
    assert branch["scope"] == "branch"
    assert branch["head"] == branch_head
    branch_file = next(item for item in branch["files"] if item["filename"] == "src/worker.py")
    assert branch_file["after"]["content"].endswith("return 1\n")

    source.write_bytes(b"def compute():\n    return 2\n")
    git("add", "src/worker.py")
    source.write_bytes(b"def compute():\n    return 3\n")

    staged = collect("staged")
    assert staged["scope"] == "staged"
    staged_file = next(item for item in staged["files"] if item["filename"] == "src/worker.py")
    assert staged_file["after"]["content"].endswith("return 2\n")

    worktree = collect("worktree")
    assert worktree["scope"] == "worktree"
    worktree_file = next(item for item in worktree["files"] if item["filename"] == "src/worker.py")
    assert worktree_file["after"]["content"].endswith("return 3\n")

    session_dir = tmp_path / "review sessions" / "first"
    entrypoint = package / "scripts" / "review_changes.py"
    start = subprocess.run(
        [
            sys.executable,
            "-B",
            "-S",
            str(entrypoint),
            "start",
            "--repo",
            str(repository),
            "--scope",
            "staged",
            "--session-dir",
            str(session_dir),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert start.returncode == 0, start.stderr
    record_path = session_dir / "session.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["workflow_status"] == "awaiting_semantic_review"
    assert record["review_coverage_status"] == "uncovered"
    assert record["scope"]["kind"] == "staged"
    assert record["repository_identity"]["repository_root"] == str(repository.resolve())
    assert session_dir.resolve().is_relative_to(tmp_path.resolve())
    assert not session_dir.resolve().is_relative_to(repository.resolve())
    manifest = json.loads(
        (session_dir / record["references"]["manifest"]).read_text(encoding="utf-8")
    )
    draft = json.loads(
        (session_dir / record["references"]["draft_result"]).read_text(encoding="utf-8")
    )
    assert manifest["repository"] == str(repository.resolve())
    assert manifest["scope"] == "staged"
    assert manifest["content_fingerprint"] == record["fingerprints"]["final"]
    assert draft["input_fingerprint"] == record["fingerprints"]["final"]
    assert draft["review_status"] == "uncovered"
    assert draft["coverage"]
    assert all(item["semantic_status"] == "uncovered" for item in draft["coverage"])

    resumed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-S",
            str(entrypoint),
            "resume",
            "--session",
            str(session_dir),
            "--repo",
            str(repository),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr
    resumed_record = json.loads(record_path.read_text(encoding="utf-8"))
    assert resumed_record["workflow_status"] == "awaiting_semantic_review"
    assert resumed_record["review_coverage_status"] == "uncovered"
    assert resumed_record["fingerprints"] == record["fingerprints"]
