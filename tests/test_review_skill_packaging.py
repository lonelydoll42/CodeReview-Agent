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
