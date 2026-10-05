from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = REPOSITORY_ROOT / "skills" / "review-changes"
CORE_SOURCE = REPOSITORY_ROOT / "review_core"
RULE_SOURCE = REPOSITORY_ROOT / "tools" / "semgrep_runner.py"
PACKAGE_NAME = "review-changes"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ignored(path: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name == "__pycache__" or name.endswith(".pyc") or name.endswith(".pyo")
    }


def _static_rules() -> str | None:
    if not RULE_SOURCE.is_file():
        return None
    try:
        tree = ast.parse(RULE_SOURCE.read_text(encoding="utf-8"), filename=str(RULE_SOURCE))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "_RULES_YAML"
                for target in node.targets
            ):
                continue
            value = node.value
            if isinstance(value, ast.Call) and value.args:
                result = textwrap.dedent(ast.literal_eval(value.args[0]))
            else:
                result = ast.literal_eval(value)
            return result if isinstance(result, str) else None
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return None
    return None


def _source_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else None


def _file_inventory(package: Path) -> list[dict[str, str]]:
    inventory = []
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        inventory.append({"path": relative, "sha256": _sha256(path.read_bytes())})
    return inventory


def _create_archive(package: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(package.rglob("*")):
            if not path.is_file():
                continue
            relative = Path(PACKAGE_NAME) / path.relative_to(package)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            output.writestr(info, path.read_bytes())


def build(output_dir: Path) -> tuple[Path, Path, Path]:
    if not SKILL_SOURCE.is_dir():
        raise FileNotFoundError(f"Skill source not found: {SKILL_SOURCE}")
    if not CORE_SOURCE.is_dir():
        raise FileNotFoundError(f"shared review_core source not found: {CORE_SOURCE}")
    version_file = SKILL_SOURCE / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip()
    if not version or any(char.isspace() for char in version):
        raise ValueError("skills/review-changes/VERSION must contain one non-empty version token")

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    package_target = output_dir / PACKAGE_NAME
    archive_target = output_dir / f"{PACKAGE_NAME}.zip"
    manifest_target = output_dir / f"{PACKAGE_NAME}.manifest.json"
    checksum_target = output_dir / f"{PACKAGE_NAME}.manifest.json.sha256"
    targets = (package_target, archive_target, manifest_target, checksum_target)
    conflicts = [path for path in targets if path.exists()]
    if conflicts:
        raise FileExistsError(
            "build target already contains output(s): "
            + ", ".join(str(path) for path in conflicts)
        )

    with tempfile.TemporaryDirectory(prefix="review-changes-build-", dir=output_dir) as temporary:
        stage = Path(temporary)
        package = stage / PACKAGE_NAME
        shutil.copytree(SKILL_SOURCE, package, ignore=_ignored)
        shutil.copytree(CORE_SOURCE, package / "review_core", ignore=_ignored)
        rules = _static_rules()
        if rules is not None:
            static_dir = package / "static"
            static_dir.mkdir()
            (static_dir / "semgrep-rules.yaml").write_text(rules, encoding="utf-8")

        inventory = _file_inventory(package)
        inventory_bytes = json.dumps(
            inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        archive = stage / f"{PACKAGE_NAME}.zip"
        _create_archive(package, archive)
        archive_digest = _sha256(archive.read_bytes())
        manifest = {
            "schema_version": "1",
            "name": PACKAGE_NAME,
            "version": version,
            "source_revision": _source_revision(),
            "source_tree_sha256": _sha256(inventory_bytes),
            "archive": archive_target.name,
            "archive_sha256": archive_digest,
            "files": inventory,
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        staged_manifest = stage / manifest_target.name
        staged_manifest.write_bytes(manifest_bytes)
        staged_checksum = stage / checksum_target.name
        staged_checksum.write_text(_sha256(manifest_bytes) + "\n", encoding="ascii")

        os.replace(package, package_target)
        os.replace(archive, archive_target)
        os.replace(staged_manifest, manifest_target)
        os.replace(staged_checksum, checksum_target)
    return package_target, archive_target, manifest_target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an independent review-changes Skill package from repository sources."
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output directory for the package folder, zip, and version manifest",
    )
    args = parser.parse_args()
    try:
        package, archive, manifest = build(args.output)
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"build_review_skill: {exc}", file=sys.stderr)
        return 2
    print(f"package: {package}")
    print(f"archive: {archive}")
    print(f"manifest: {manifest}")
    print(f"manifest checksum: {manifest}.sha256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
