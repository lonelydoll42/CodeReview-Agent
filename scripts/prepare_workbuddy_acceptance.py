from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


PACKAGE_NAME = "review-changes"
PACKAGE_VERSION = "0.2.1"
PINNED_SOURCE_REVISION = "0a1a8756546ee15801180b06b3d9c6ac3558d481"
PINNED_ARCHIVE_SHA256 = "42948cb0de941299c5e6ed39628acaeadd22bda26e0fbcefa0804460d05b74aa"
PINNED_MANIFEST_SHA256 = "5d8f6febfedf6532bc985b7d638099cacc7f99fdd408902c696c045135471ecf"
CASE_IDS = (
    "runtime-missing-record",
    "safe-authorization-move",
    "cross-file-contract",
)
TEST_COMMAND = ("-B", "-S", "-m", "unittest", "discover", "-s", "tests", "-v")


@dataclass(frozen=True)
class Fixture:
    case_id: str
    host_id: str
    directory_name: str
    context_paths: tuple[str, ...]
    baseline: dict[str, str]
    review: dict[str, str]
    followup: dict[str, str]


def _common_files() -> dict[str, str]:
    return {
        ".gitignore": "__pycache__/\n*.py[cod]\n",
        "pyproject.toml": (
            "[project]\n"
            'name = "workbuddy-acceptance-fixture"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.10"\n'
        ),
    }


def _fixtures() -> tuple[Fixture, ...]:
    common = _common_files()
    return (
        Fixture(
            case_id="runtime-missing-record",
            host_id="case-01",
            directory_name="01 本地项目 验收样例",
            context_paths=("records.py", "tests/test_records.py", "pyproject.toml"),
            baseline={
                **common,
                "records.py": (
                    "def record_label(records, record_id):\n"
                    "    record = records.get(record_id)\n"
                    "    if record is None:\n"
                    '        return "Record unavailable"\n'
                    '    return f\"{record[\'name\']} [{record[\'status\']}]\"\n'
                ),
                "tests/test_records.py": (
                    "import unittest\n\n"
                    "from records import record_label\n\n\n"
                    "class RecordLabelTests(unittest.TestCase):\n"
                    "    def test_existing_record_has_a_label(self):\n"
                    "        records = {\"r-1\": {\"name\": \"North\", \"status\": \"open\"}}\n"
                    "        self.assertEqual(record_label(records, \"r-1\"), \"North [open]\")\n\n"
                    "if __name__ == \"__main__\":\n"
                    "    unittest.main()\n"
                ),
            },
            review={
                "records.py": (
                    "def record_label(records, record_id):\n"
                    "    record = records.get(record_id)\n"
                    '    return f\"{record[\'name\']} [{record[\'status\']}]\"\n'
                )
            },
            followup={
                "records.py": (
                    "def record_label(records, record_id):\n"
                    "    record = records.get(record_id)\n"
                    "    if record is None:\n"
                    '        return "Record unavailable"\n'
                    '    return f\"{record[\'name\']} [{record[\'status\']}]\"\n'
                )
            },
        ),
        Fixture(
            case_id="safe-authorization-move",
            host_id="case-02",
            directory_name="02 应用工作区 代码样例",
            context_paths=("router.py", "permissions.py", "tests/test_router.py", "pyproject.toml"),
            baseline={
                **common,
                "permissions.py": (
                    "def admin_report(user):\n"
                    '    if not user.get("is_admin", False):\n'
                    '        raise PermissionError("administrator access required")\n'
                    '    return {"summary": "restricted totals", "count": 3}\n'
                ),
                "router.py": (
                    "from permissions import admin_report\n\n\n"
                    "def get_admin_report(user):\n"
                    "    return admin_report(user)\n"
                ),
                "tests/test_router.py": (
                    "import unittest\n\n"
                    "from router import get_admin_report\n\n\n"
                    "class AdminReportTests(unittest.TestCase):\n"
                    "    def test_administrator_can_read_report(self):\n"
                    "        self.assertEqual(\n"
                    '            get_admin_report({"is_admin": True}),\n'
                    '            {"summary": "restricted totals", "count": 3},\n'
                    "        )\n\n"
                    "    def test_non_administrator_is_rejected(self):\n"
                    "        with self.assertRaises(PermissionError):\n"
                    '            get_admin_report({"is_admin": False})\n\n'
                    "    def test_missing_role_is_rejected(self):\n"
                    "        with self.assertRaises(PermissionError):\n"
                    "            get_admin_report({})\n\n\n"
                    "if __name__ == \"__main__\":\n"
                    "    unittest.main()\n"
                ),
            },
            review={
                "permissions.py": (
                    "def _admin_report_for_authorized_user():\n"
                    '    return {"summary": "restricted totals", "count": 3}\n'
                ),
                "router.py": (
                    "from permissions import _admin_report_for_authorized_user\n\n\n"
                    "def get_admin_report(user):\n"
                    '    if not user.get("is_admin", False):\n'
                    '        raise PermissionError("administrator access required")\n'
                    "    return _admin_report_for_authorized_user()\n"
                ),
            },
            followup={
                "router.py": (
                    "from permissions import _admin_report_for_authorized_user\n\n\n"
                    "def _require_admin(user):\n"
                    '    if not user.get("is_admin", False):\n'
                    '        raise PermissionError("administrator access required")\n\n\n'
                    "def get_admin_report(user):\n"
                    "    _require_admin(user)\n"
                    "    return _admin_report_for_authorized_user()\n"
                )
            },
        ),
        Fixture(
            case_id="cross-file-contract",
            host_id="case-03",
            directory_name="03 独立仓库 变更样例",
            context_paths=("views.py", "models.py", "tests/test_views.py", "pyproject.toml"),
            baseline={
                **common,
                "models.py": (
                    "def get_user(user_id):\n"
                    '    return {"id": user_id, "name": "Lin"}\n'
                ),
                "views.py": (
                    "from models import get_user\n\n\n"
                    "def greeting(user_id):\n"
                    "    user = get_user(user_id)\n"
                    '    return f\"Hello, {user[\'name\']}.\"\n'
                ),
                "tests/test_views.py": (
                    "import unittest\n\n"
                    "from views import greeting\n\n\n"
                    "class GreetingTests(unittest.TestCase):\n"
                    "    def test_greeting_uses_the_user_name(self):\n"
                    '        self.assertEqual(greeting("u-1"), "Hello, Lin.")\n\n\n'
                    "if __name__ == \"__main__\":\n"
                    "    unittest.main()\n"
                ),
            },
            review={
                "models.py": (
                    "from dataclasses import dataclass\n\n\n"
                    "@dataclass(frozen=True)\n"
                    "class User:\n"
                    "    id: str\n"
                    "    name: str\n\n\n"
                    "def get_user(user_id):\n"
                    "    return User(id=user_id, name=\"Lin\")\n"
                )
            },
            followup={
                "views.py": (
                    "from models import get_user\n\n\n"
                    "def greeting(user_id):\n"
                    "    user = get_user(user_id)\n"
                    '    return f\"Hello, {user.name}.\"\n'
                )
            },
        ),
    )


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _archive_relative_path(filename: str) -> PurePosixPath:
    if "\\" in filename or ":" in filename:
        raise ValueError(f"unsafe Windows ZIP path: {filename}")
    windows_path = PureWindowsPath(filename)
    parts = filename.split("/")
    if (
        windows_path.drive
        or windows_path.root
        or windows_path.is_absolute()
        or not filename
        or filename.endswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"unsafe ZIP path: {filename}")
    path = PurePosixPath(filename)
    if path.is_absolute() or len(path.parts) < 2 or path.parts[0] != PACKAGE_NAME:
        raise ValueError(f"unexpected ZIP path: {filename}")
    return PurePosixPath(*path.parts[1:])


def _assert_contained(root: Path, target: Path, label: str) -> None:
    resolved_root = root.resolve(strict=True)
    resolved_target = target.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes its allowed directory: {target}") from exc


def _verify_release(release_dir: Path) -> tuple[Path, dict[str, Any]]:
    release_dir = release_dir.expanduser().resolve()
    archive = release_dir / f"{PACKAGE_NAME}.zip"
    manifest_path = release_dir / f"{PACKAGE_NAME}.manifest.json"
    checksum_path = release_dir / f"{PACKAGE_NAME}.manifest.json.sha256"
    for path in (archive, manifest_path, checksum_path):
        if not path.is_file():
            raise FileNotFoundError(f"release artifact not found: {path}")

    manifest_bytes = manifest_path.read_bytes()
    recorded_manifest_hash = checksum_path.read_text(encoding="ascii").strip()
    if recorded_manifest_hash != PINNED_MANIFEST_SHA256:
        raise ValueError("release manifest checksum does not match the pinned 0.2.1 checksum")
    if _sha256_bytes(manifest_bytes) != PINNED_MANIFEST_SHA256:
        raise ValueError("release manifest does not match the pinned 0.2.1 manifest")
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict):
        raise ValueError("release manifest must be a JSON object")
    if manifest.get("name") != PACKAGE_NAME or manifest.get("version") != PACKAGE_VERSION:
        raise ValueError(f"expected {PACKAGE_NAME} version {PACKAGE_VERSION}")
    if manifest.get("source_revision") != PINNED_SOURCE_REVISION:
        raise ValueError("release source revision does not match the pinned 0.2.1 revision")
    if manifest.get("archive") != archive.name:
        raise ValueError("release manifest archive name does not match the selected ZIP")
    if manifest.get("archive_sha256") != PINNED_ARCHIVE_SHA256:
        raise ValueError("release archive hash in manifest does not match the pinned 0.2.1 hash")
    if _sha256_file(archive) != PINNED_ARCHIVE_SHA256:
        raise ValueError("release ZIP hash does not match the pinned 0.2.1 hash")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("release manifest has no file inventory")
    expected: dict[str, str] = {}
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("release file inventory contains an invalid entry")
        path_text = entry["path"]
        path = PurePosixPath(path_text)
        windows_path = PureWindowsPath(path_text)
        if (
            "\\" in path_text
            or ":" in path_text
            or path.is_absolute()
            or windows_path.drive
            or windows_path.root
            or not path.parts
            or any(part in {"", ".", ".."} for part in path_text.split("/"))
        ):
            raise ValueError(f"unsafe path in release inventory: {entry['path']}")
        if entry["path"] in expected or not isinstance(entry.get("sha256"), str):
            raise ValueError(f"invalid or duplicate release inventory entry: {entry['path']}")
        expected[entry["path"]] = entry["sha256"]

    with zipfile.ZipFile(archive) as bundle:
        actual_names: set[str] = set()
        for info in bundle.infolist():
            if info.is_dir():
                raise ValueError(f"unexpected directory entry in release ZIP: {info.filename}")
            relative_path = _archive_relative_path(info.filename)
            relative = relative_path.as_posix()
            if relative in actual_names:
                raise ValueError(f"duplicate file in release ZIP: {relative}")
            actual_names.add(relative)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"release ZIP contains a symbolic link: {info.filename}")
            if relative not in expected:
                raise ValueError(f"unlisted file in release ZIP: {relative}")
            if _sha256_bytes(bundle.read(info)) != expected[relative]:
                raise ValueError(f"release file hash mismatch: {relative}")
        if actual_names != set(expected):
            missing = sorted(set(expected) - actual_names)
            raise ValueError(f"release ZIP is missing manifest files: {', '.join(missing)}")
    return archive, manifest


def _extract_verified(archive: Path, package_root: Path) -> None:
    if package_root.is_symlink():
        raise ValueError(f"package root must not be a symbolic link: {package_root}")
    package_root.mkdir(parents=True, exist_ok=True)
    resolved_root = package_root.resolve(strict=True)
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                raise ValueError(f"unexpected directory entry in release ZIP: {info.filename}")
            relative = _archive_relative_path(info.filename)
            destination = resolved_root.joinpath(*relative.parts)
            _assert_contained(resolved_root, destination, "release file")
            destination.parent.mkdir(parents=True, exist_ok=True)
            _assert_contained(resolved_root, destination, "release file")
            if destination.is_symlink():
                raise ValueError(f"release target is a symbolic link: {destination}")
            destination.write_bytes(bundle.read(info))
    if not (resolved_root / "SKILL.md").is_file():
        raise ValueError("verified release did not contain SKILL.md")


def _write_files(repository: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _run_record(result: subprocess.CompletedProcess[str], expected_exit_code: int) -> dict[str, Any]:
    return {
        "command": result.args,
        "exit_code": result.returncode,
        "expected_exit_code": expected_exit_code,
        "expectation_met": result.returncode == expected_exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _git(repository: Path, *args: str) -> str:
    result = _run(["git", *args], cwd=repository)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed in {repository}: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_bytes(repository: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repository}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def _output_path(
    output: Path,
    relative: tuple[str, ...],
    *,
    label: str,
    must_exist: bool = True,
) -> Path:
    root = output.resolve(strict=True)
    candidate = root.joinpath(*relative)
    current = root
    for part in relative:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} contains a symbolic link: {current}")
        if must_exist and not current.exists():
            raise FileNotFoundError(f"{label} is missing: {current}")
        if current.exists():
            _assert_contained(root, current, label)
    return candidate


def _recorded_output_path(
    output: Path,
    recorded: Any,
    relative: tuple[str, ...],
    *,
    label: str,
    must_exist: bool = True,
) -> Path:
    expected = _output_path(output, relative, label=label, must_exist=must_exist)
    if not isinstance(recorded, str) or not Path(recorded).is_absolute() or Path(recorded) != expected:
        raise ValueError(f"recorded {label} path does not match this acceptance output")
    return expected


def _verify_fixture_state(
    repository: Path,
    case: dict[str, Any],
) -> None:
    if _git(repository, "rev-parse", "HEAD") != case["baseline_commit"]:
        raise RuntimeError("fixture HEAD changed since preparation")
    index_state = _git_bytes(repository, "ls-files", "--stage", "-z")
    if _sha256_bytes(index_state) != case["index_sha256"]:
        raise RuntimeError("fixture index changed since preparation")
    untracked = _git_bytes(repository, "ls-files", "--others", "--exclude-standard", "-z")
    if _sha256_bytes(untracked) != case["untracked_sha256"] or untracked:
        raise RuntimeError("fixture has untracked files since preparation")
    if _sha256_bytes(_review_diff(repository)) != case["review_diff_sha256"]:
        raise RuntimeError("fixture worktree changed since preparation")


def _commit_baseline(repository: Path) -> str:
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "WorkBuddy Acceptance Fixture")
    _git(repository, "config", "user.email", "acceptance-fixture@example.invalid")
    _git(repository, "add", "--all")
    _git(repository, "commit", "--quiet", "-m", "baseline")
    return _git(repository, "rev-parse", "HEAD")


def _run_tests(repository: Path) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, *TEST_COMMAND], cwd=repository)


def _capture_snapshot(
    package_root: Path,
    repository: Path,
    context_paths: tuple[str, ...],
    destination: Path,
    launcher: Path,
    *,
    compare_to: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-B",
        "-S",
        str(package_root / "scripts" / "collect_changes.py"),
        "--repo",
        str(repository),
        "--scope",
        "worktree",
        "--include-untracked",
        "--output",
        str(destination),
    ]
    if context_paths:
        command.extend(("--context", *context_paths))
    if compare_to is not None:
        command.extend(("--compare-to", str(compare_to)))
    return _run(command, cwd=launcher)


def _review_diff(repository: Path) -> bytes:
    result = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--binary"],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"could not capture fixture review diff: {result.stderr.decode(errors='replace')}")
    return result.stdout


def _write_patch(repository: Path, files: dict[str, str], patch_path: Path) -> None:
    pieces: list[str] = []
    for relative, after in files.items():
        before = (repository / relative).read_text(encoding="utf-8").splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        diff = difflib.unified_diff(
            before,
            after_lines,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
        patch = "".join(diff)
        if patch:
            pieces.append(f"diff --git a/{relative} b/{relative}\n{patch}")
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text("".join(pieces), encoding="utf-8", newline="\n")


def _fixture_expected_exit(case_id: str) -> int:
    return 1 if case_id == "cross-file-contract" else 0


def _runtime_probe_text() -> str:
    return (
        "from __future__ import annotations\n\n"
        "import argparse\n"
        "import sys\n"
        "from pathlib import Path\n\n\n"
        "def main() -> int:\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--repo', type=Path, required=True)\n"
        "    repository = parser.parse_args().repo.resolve(strict=True)\n"
        "    sys.path.insert(0, str(repository))\n"
        "    from records import record_label\n\n"
        "    assert record_label({}, 'missing') == 'Record unavailable'\n"
        "    return 0\n\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )


def _run_runtime_probe(
    repository: Path,
    probe_script: Path,
    launcher: Path,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            sys.executable,
            "-B",
            "-S",
            str(probe_script),
            "--repo",
            str(repository),
        ],
        cwd=launcher,
    )


def _oracle_text() -> str:
    return (
        "# Operator-only oracle\n\n"
        "Keep this file outside each reviewed Git repository and do not include it in host prompts. "
        "These are acceptance expectations, not host-generated findings.\n\n"
        "## runtime-missing-record\n\n"
        "The review change dereferences the result of `dict.get` without handling an absent record. "
        "The missing-record test should fail before the prepared patch and pass after it.\n\n"
        "## safe-authorization-move\n\n"
        "The sole public route moves the administrator check upstream before calling the private "
        "data function. Existing tests must reject a false or missing role and allow an administrator "
        "both before and after the review change. The follow-up patch is a behavior-preserving helper "
        "extraction, not a bug repair.\n\n"
        "## cross-file-contract\n\n"
        "`get_user` changes from a dictionary to a `User` object while `greeting` keeps dictionary "
        "subscription. The caller test should fail before the prepared patch and pass after it.\n"
    )


def _write_host_prompts(root: Path) -> None:
    prompts = root / "host-prompts"
    prompts.mkdir(parents=True)
    (prompts / "review.txt").write_text(
        "Review the requested local worktree changes in the currently open repository using the "
        "installed review-changes Skill. Follow repository guidance, capture relevant callers, "
        "tests, and configuration, verify any findings against the available evidence, run bounded "
        "relevant tests, and export the validated review report. Save the raw session transcript "
        "separately from the report.\n",
        encoding="utf-8",
    )
    (prompts / "recheck.txt").write_text(
        "Recheck the saved review against the current repository snapshot in this session. Read the "
        "prior manifest and validated result named in the operator's paths, inspect the current "
        "functions and callers, run relevant bounded tests, and classify every prior finding as "
        "resolved, persisting, or unverified. Record new findings separately and export the "
        "deterministic recheck report. Do not infer resolution from a finding disappearing. Save "
        "the raw session transcript separately from the report.\n",
        encoding="utf-8",
    )


def _make_case(
    fixture: Fixture,
    *,
    output: Path,
    package_root: Path,
    launcher: Path,
) -> dict[str, Any]:
    repository = output / "repositories" / fixture.directory_name
    repository.mkdir(parents=True)
    _write_files(repository, fixture.baseline)
    baseline_commit = _commit_baseline(repository)
    baseline_tests = _run_tests(repository)
    probe_baseline: subprocess.CompletedProcess[str] | None = None
    probe_path: Path | None = None
    probe_record_path: Path | None = None
    if fixture.case_id == "runtime-missing-record":
        probe_path = output / "operator" / "probes" / "runtime_missing_record.py"
        probe_record_path = (
            output / "validation" / "operator-probes" / f"{fixture.case_id}.json"
        )
        probe_baseline = _run_runtime_probe(repository, probe_path, launcher)

    _write_files(repository, fixture.review)
    review_tests = _run_tests(repository)
    probe_review = (
        _run_runtime_probe(repository, probe_path, launcher) if probe_path is not None else None
    )

    expected_review_exit = _fixture_expected_exit(fixture.case_id)
    test_result = {
        "case_id": fixture.case_id,
        "python": sys.executable,
        "test_command": [sys.executable, *TEST_COMMAND],
        "baseline": _run_record(baseline_tests, 0),
        "review_changes": _run_record(review_tests, expected_review_exit),
        "follow_up": None,
    }
    test_result["preparation_expectations_met"] = (
        test_result["baseline"]["expectation_met"]
        and test_result["review_changes"]["expectation_met"]
    )
    test_path = output / "validation" / "project-tests" / f"{fixture.case_id}.json"
    _write_json(test_path, test_result)

    probe_expectations_met = True
    if probe_baseline is not None and probe_review is not None:
        assert probe_path is not None and probe_record_path is not None
        probe_record = {
            "case_id": fixture.case_id,
            "probe": str(probe_path.resolve()),
            "probe_sha256": _sha256_file(probe_path),
            "phases": {
                "baseline": _run_record(probe_baseline, 0),
                "review_changes": _run_record(probe_review, 1),
                "follow_up": None,
            },
        }
        probe_expectations_met = all(
            probe_record["phases"][phase]["expectation_met"]
            for phase in ("baseline", "review_changes")
        )
        probe_record["preparation_expectations_met"] = probe_expectations_met
        _write_json(probe_record_path, probe_record)

    snapshot_dir = output / "snapshots" / fixture.host_id
    initial_manifest = snapshot_dir / "review-initial.json"
    final_manifest = snapshot_dir / "review-manifest.json"
    initial_result = _capture_snapshot(
        package_root,
        repository,
        (),
        initial_manifest,
        launcher,
    )
    if initial_result.returncode:
        raise RuntimeError(f"initial snapshot failed for {fixture.case_id}: {initial_result.stderr}")
    final_result = _capture_snapshot(
        package_root,
        repository,
        fixture.context_paths,
        final_manifest,
        launcher,
        compare_to=initial_manifest,
    )
    if final_result.returncode:
        raise RuntimeError(f"context snapshot failed for {fixture.case_id}: {final_result.stderr}")

    patch_path = output / "operator" / "prepared-patches" / f"{fixture.case_id}.patch"
    _write_patch(repository, fixture.followup, patch_path)
    review_diff_hash = _sha256_bytes(_review_diff(repository))
    index_hash = _sha256_bytes(_git_bytes(repository, "ls-files", "--stage", "-z"))
    untracked_hash = _sha256_bytes(
        _git_bytes(repository, "ls-files", "--others", "--exclude-standard", "-z")
    )
    return {
        "case_id": fixture.case_id,
        "host_id": fixture.host_id,
        "repository": str(repository.resolve()),
        "baseline_commit": baseline_commit,
        "review_snapshot": str(final_manifest.resolve()),
        "initial_snapshot": str(initial_manifest.resolve()),
        "test_record": str(test_path.resolve()),
        "probe_record": (
            str(probe_record_path.resolve()) if probe_record_path is not None else None
        ),
        "prepared_patch": str(patch_path.resolve()),
        "prepared_patch_sha256": _sha256_file(patch_path),
        "review_diff_sha256": review_diff_hash,
        "index_sha256": index_hash,
        "untracked_sha256": untracked_hash,
        "context_paths": list(fixture.context_paths),
        "preparation_expectations_met": (
            test_result["preparation_expectations_met"] and probe_expectations_met
        ),
        "host_state": "pending",
    }


def _read_run_record(output: Path) -> dict[str, Any]:
    record_path = output / "run-record.json"
    if not record_path.is_file():
        raise FileNotFoundError(f"acceptance output not found: {record_path}")
    record = _load_json(record_path)
    if record.get("schema_version") != "1":
        raise ValueError("unsupported acceptance run-record schema")
    return record


def prepare(release_dir: Path, output: Path) -> int:
    archive, manifest = _verify_release(release_dir)
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing acceptance output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    package_root = output / "verified-release" / PACKAGE_NAME
    _extract_verified(archive, package_root)
    release_dir = release_dir.expanduser().resolve()
    provenance_dir = output / "provenance"
    provenance_dir.mkdir()
    for filename in (
        f"{PACKAGE_NAME}.zip",
        f"{PACKAGE_NAME}.manifest.json",
        f"{PACKAGE_NAME}.manifest.json.sha256",
    ):
        shutil.copyfile(release_dir / filename, provenance_dir / filename)
    (provenance_dir / "verification.json").write_text(
        json.dumps(
            {
                "verified": True,
                "name": manifest["name"],
                "version": manifest["version"],
                "source_revision": manifest.get("source_revision"),
                "archive_sha256": manifest["archive_sha256"],
                "manifest_sha256": _sha256_file(provenance_dir / f"{PACKAGE_NAME}.manifest.json"),
                "files_verified": len(manifest["files"]),
                "extracted_from_verified_zip": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    launcher = output / "host-workdir"
    launcher.mkdir()
    (output / "repositories").mkdir()
    (output / "validation" / "project-tests").mkdir(parents=True)
    (output / "validation" / "operator-probes").mkdir(parents=True)
    (output / "snapshots").mkdir()
    operator = output / "operator"
    (operator / "prepared-patches").mkdir(parents=True)
    probes = operator / "probes"
    probes.mkdir()
    (probes / "runtime_missing_record.py").write_text(
        _runtime_probe_text(), encoding="utf-8", newline="\n"
    )
    (operator / "oracle.md").write_text(_oracle_text(), encoding="utf-8")
    _write_host_prompts(output)
    host_artifacts = output / "host-artifacts"
    host_artifacts.mkdir()

    cases: dict[str, Any] = {}
    expectations_met = True
    for fixture in _fixtures():
        cases[fixture.case_id] = _make_case(
            fixture,
            output=output,
            package_root=package_root,
            launcher=launcher,
        )
        (host_artifacts / fixture.host_id).mkdir()
        expectations_met = (
            expectations_met and cases[fixture.case_id]["preparation_expectations_met"]
        )

    (output / "README.md").write_text(_acceptance_readme(output), encoding="utf-8")
    _write_json(
        output / "host-outcomes.json",
        {
            "status": "not_run",
            "host": {"name": None, "version": None, "model": None, "cost": None},
            "installation": {"status": "pending", "evidence_path": None},
            "counts": {
                "install_failures": None,
                "path_errors": None,
                "context_omissions": None,
                "manual_interventions": None,
            },
            "cases": {
                case_id: {
                    "review": {"status": "pending", "raw_transcript": None, "report": None},
                    "fix": {"status": "pending", "operator_notes": None},
                    "recheck": {"status": "pending", "raw_transcript": None, "report": None},
                    "export": {"status": "pending", "path": None},
                    "notes": [],
                }
                for case_id in cases
            },
        },
    )
    _write_json(
        output / "run-record.json",
        {
            "schema_version": "1",
            "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
            "preparation_status": "ready" if expectations_met else "preparation_check_failed",
            "release": {
                "version": manifest["version"],
                "source_revision": manifest.get("source_revision"),
                "archive_sha256": manifest["archive_sha256"],
                "manifest_sha256": _sha256_file(provenance_dir / f"{PACKAGE_NAME}.manifest.json"),
                "verified": True,
            },
            "host_installation": "pending",
            "host_identity": {"name": None, "version": None, "model": None, "cost": None},
            "unknown_counts": {
                "install_failures": None,
                "path_errors": None,
                "context_omissions": None,
                "manual_interventions": None,
            },
            "cases": cases,
            "host_outcomes_file": str((output / "host-outcomes.json").resolve()),
        },
    )
    print(f"output: {output}")
    print(f"verified Skill: {package_root}")
    print(f"run record: {output / 'run-record.json'}")
    print(f"host outcomes (pending): {output / 'host-outcomes.json'}")
    print(f"preparation checks: {'passed' if expectations_met else 'FAILED'}")
    return 0 if expectations_met else 1


def _acceptance_readme(output: Path) -> str:
    return (
        "# WorkBuddy acceptance run\n\n"
        "The release ZIP was hash-verified and extracted to `verified-release/review-changes/`. "
        "Use the official import documentation for the tested WorkBuddy build to choose the "
        "supported form: `provenance/review-changes.zip` or `verified-release/review-changes/`. "
        "Neither form is presumed supported; record the documented procedure and exact steps. "
        "This kit does not write undocumented host installation paths.\n\n"
        "Open one repository at a time from `repositories/`, use `host-prompts/review.txt`, and "
        "save the raw host transcript and exported report under the matching neutral "
        "`host-artifacts/case-NN/` directory. "
        "The operator oracle, probe, and prepared patches are under `operator/`, outside all "
        "reviewed Git repositories. Keep them out of host prompts.\n\n"
        "After completing the review and saving its manifest and validated result, apply the "
        "prepared bug fix or behavior-preserving follow-up explicitly:\n\n"
        "```sh\n"
        f"python3 -S /path/to/prepare_workbuddy_acceptance.py apply-fix --output '{output}' "
        "--case runtime-missing-record\n"
        "```\n\n"
        "The command refuses a changed target tree, applies only that case's prepared patch, runs "
        "the real unittest suite, and captures a new context manifest outside the repository. "
        "Repeat for the other cases after their review. Use `host-prompts/recheck.txt` in a new "
        "host session with the prior manifest/result and new snapshot paths from `apply-results/`. "
        "Export recheck output with the packaged `recheck_review.py`; use `finalize_review.py` for "
        "review report validation/rendering. The kit never fabricates semantic results or host "
        "reports.\n\n"
        "Project test phases live in `validation/project-tests/`; external operator probe phases "
        "live separately in `validation/operator-probes/`. Each record has baseline, review, and "
        "follow-up results; host outcomes remain `pending` in "
        "`host-outcomes.json` until observed. Fill unknown host identity, cost, failure counts, "
        "path errors, missed context, and manual-intervention counts only from the real session.\n\n"
        "Run preparation on the WorkBuddy machine where the repositories will be reviewed. Snapshot "
        "manifests include absolute repository identity; if a repository moves, recapture the "
        "relevant manifests before review or recheck.\n"
    )


def apply_fix(output: Path, case_id: str) -> int:
    output = output.expanduser().resolve()
    record = _read_run_record(output)
    if case_id not in record.get("cases", {}):
        raise ValueError(f"unknown case {case_id!r}; choose one of: {', '.join(CASE_IDS)}")
    fixture = next(item for item in _fixtures() if item.case_id == case_id)
    case = record["cases"][case_id]
    repository = _recorded_output_path(
        output,
        case.get("repository"),
        ("repositories", fixture.directory_name),
        label="fixture repository",
    )
    patch = _recorded_output_path(
        output,
        case.get("prepared_patch"),
        ("operator", "prepared-patches", f"{case_id}.patch"),
        label="prepared patch",
    )
    snapshot_dir = _recorded_output_path(
        output,
        case.get("review_snapshot"),
        ("snapshots", fixture.host_id, "review-manifest.json"),
        label="prior snapshot",
    ).parent
    _recorded_output_path(
        output,
        case.get("initial_snapshot"),
        ("snapshots", fixture.host_id, "review-initial.json"),
        label="initial snapshot",
    )
    project_test_path = _recorded_output_path(
        output,
        case.get("test_record"),
        ("validation", "project-tests", f"{case_id}.json"),
        label="project test record",
    )
    project_tests = _load_json(project_test_path)
    if project_tests.get("follow_up") is not None:
        raise ValueError("project test record already has a follow-up result")
    probe_record_path: Path | None = None
    probe_script: Path | None = None
    probe_record: dict[str, Any] | None = None
    if case_id == "runtime-missing-record":
        probe_record_path = _recorded_output_path(
            output,
            case.get("probe_record"),
            ("validation", "operator-probes", f"{case_id}.json"),
            label="operator probe record",
        )
        probe_script = _output_path(
            output,
            ("operator", "probes", "runtime_missing_record.py"),
            label="operator probe",
        )
        probe_record = _load_json(probe_record_path)
        phases = probe_record.get("phases")
        if not isinstance(phases, dict) or phases.get("follow_up") is not None:
            raise ValueError("operator probe record already has a follow-up result")
        if _sha256_file(probe_script) != probe_record.get("probe_sha256"):
            raise ValueError("operator probe is missing or its hash changed")
    package_root = _output_path(
        output,
        ("verified-release", PACKAGE_NAME),
        label="verified release",
    )
    launcher = _output_path(output, ("host-workdir",), label="host workdir")
    if not (repository / ".git").exists() or (repository / ".git").is_symlink():
        raise FileNotFoundError(f"prepared fixture repository is unavailable: {repository}")
    if not patch.is_file() or _sha256_file(patch) != case["prepared_patch_sha256"]:
        raise ValueError("prepared patch is missing or its hash changed")
    result_dir = _output_path(output, ("apply-results",), label="apply result directory", must_exist=False)
    result_path = result_dir / f"{fixture.host_id}.json"
    if result_path.exists() or result_path.is_symlink():
        raise FileExistsError(f"apply result already exists; refusing to apply twice: {result_path}")
    current_initial = snapshot_dir / "current-initial.json"
    current_manifest = snapshot_dir / "current-manifest.json"
    for path in (current_initial, current_manifest):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"current snapshot already exists; refusing to overwrite: {path}")
    _verify_fixture_state(repository, case)

    result_dir.mkdir(exist_ok=True)
    _assert_contained(output, result_dir, "apply result directory")

    check = _run(["git", "apply", "--check", str(patch)], cwd=repository)
    if check.returncode:
        raise RuntimeError(f"prepared patch no longer applies cleanly: {check.stderr.strip()}")
    applied = _run(["git", "apply", str(patch)], cwd=repository)
    if applied.returncode:
        raise RuntimeError(f"could not apply prepared patch: {applied.stderr.strip()}")

    test_result = _run_tests(repository)
    test_record = _run_record(test_result, 0)
    project_tests["follow_up"] = test_record
    _write_json(project_test_path, project_tests)

    probe_test_record: dict[str, Any] | None = None
    if probe_script is not None and probe_record_path is not None:
        assert probe_record is not None
        phases = probe_record.get("phases")
        assert isinstance(phases, dict)
        probe_test_record = _run_record(
            _run_runtime_probe(repository, probe_script, launcher),
            0,
        )
        phases["follow_up"] = probe_test_record
        _write_json(probe_record_path, probe_record)

    initial_result = _capture_snapshot(
        package_root,
        repository,
        (),
        current_initial,
        launcher,
    )
    if initial_result.returncode:
        raise RuntimeError(f"post-change initial snapshot failed: {initial_result.stderr}")
    final_result = _capture_snapshot(
        package_root,
        repository,
        fixture.context_paths,
        current_manifest,
        launcher,
        compare_to=current_initial,
    )
    if final_result.returncode:
        raise RuntimeError(f"post-change context snapshot failed: {final_result.stderr}")
    _write_json(
        result_path,
        {
            "case_id": case_id,
            "patch_sha256": case["prepared_patch_sha256"],
            "review_snapshot": case["review_snapshot"],
            "current_snapshot": str(current_manifest.resolve()),
            "current_initial_snapshot": str(current_initial.resolve()),
            "test_result": test_record,
            "operator_probe_result": probe_test_record,
            "host_recheck": "pending",
            "host_export": "pending",
        },
    )
    print(f"patch applied to: {repository}")
    print(f"tests: {'passed' if test_record['expectation_met'] else 'FAILED'}")
    if probe_test_record is not None:
        print(f"operator probe: {'passed' if probe_test_record['expectation_met'] else 'FAILED'}")
    print(f"previous snapshot: {case['review_snapshot']}")
    print(f"current snapshot: {current_manifest.resolve()}")
    print(f"apply result: {result_path}")
    return 0 if test_record["expectation_met"] and (
        probe_test_record is None or probe_test_record["expectation_met"]
    ) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare standalone WorkBuddy acceptance repositories from the verified review-changes "
            "0.2.1 release ZIP, then explicitly apply one prepared isolated follow-up."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser(
        "prepare",
        help="verify/extract the release and create three committed-baseline Git fixtures",
        description=(
            "Creates new output only; it refuses an existing output path. All fixture repositories "
            "are inside that output and are separate from the source checkout."
        ),
    )
    prepare_parser.add_argument(
        "--release-dir",
        type=Path,
        required=True,
        help="directory containing the 0.2.1 ZIP, manifest JSON, and manifest .sha256 file",
    )
    prepare_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new acceptance output directory (must not already exist; Chinese/spaces supported)",
    )
    fix_parser = commands.add_parser(
        "apply-fix",
        help="explicitly apply one prepared bug fix or benign follow-up in an isolated fixture",
    )
    fix_parser.add_argument("--output", type=Path, required=True, help="prepared acceptance output")
    fix_parser.add_argument("--case", choices=CASE_IDS, required=True, help="fixture case to update")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            return prepare(args.release_dir, args.output)
        return apply_fix(args.output, args.case)
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"prepare_workbuddy_acceptance: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
