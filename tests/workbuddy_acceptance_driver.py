from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import NoReturn


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VERSION = "0.2.1"
FIXTURE_SOURCE_REVISION = "0a1a8756546ee15801180b06b3d9c6ac3558d481"
FIXTURE_ARCHIVE_SHA256 = "42948cb0de941299c5e6ed39628acaeadd22bda26e0fbcefa0804460d05b74aa"
FIXTURE_MANIFEST_SHA256 = "5d8f6febfedf6532bc985b7d638099cacc7f99fdd408902c696c045135471ecf"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "review-changes-0.2.1"
FIXTURE_ARTIFACTS = (
    "review-changes.zip",
    "review-changes.manifest.json",
    "review-changes.manifest.json.sha256",
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def build_test_release(release_dir: Path) -> None:
    release_dir.mkdir(parents=True)
    for name in FIXTURE_ARTIFACTS:
        source = FIXTURE_DIR / name
        if not source.is_file():
            raise FileNotFoundError(f"pinned test release artifact not found: {source}")
        shutil.copyfile(source, release_dir / name)

    archive_sha256 = _sha256((release_dir / FIXTURE_ARTIFACTS[0]).read_bytes())
    manifest_sha256 = _sha256((release_dir / FIXTURE_ARTIFACTS[1]).read_bytes())
    recorded_manifest_sha256 = (release_dir / FIXTURE_ARTIFACTS[2]).read_text(
        encoding="ascii"
    ).strip()
    if (
        archive_sha256 != FIXTURE_ARCHIVE_SHA256
        or manifest_sha256 != FIXTURE_MANIFEST_SHA256
        or recorded_manifest_sha256 != FIXTURE_MANIFEST_SHA256
    ):
        raise AssertionError(
            "pinned test release fixture changed: "
            f"archive={archive_sha256}, manifest={manifest_sha256}, "
            f"recorded_manifest={recorded_manifest_sha256}"
        )


def main() -> NoReturn:
    sys.path.insert(0, str(ROOT))
    from scripts import prepare_workbuddy_acceptance

    raise SystemExit(prepare_workbuddy_acceptance.main())


if __name__ == "__main__":
    main()
