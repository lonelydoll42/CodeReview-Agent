"""Small, byte-safe helpers for reading Git change metadata."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class GitCommandError(RuntimeError):
    """A Git command needed to capture a review input did not complete."""

    def __init__(self, args: Iterable[str], stderr: bytes) -> None:
        self.args = tuple(args)
        self.stderr = stderr.decode("utf-8", "replace").strip()
        detail = self.stderr or "git command failed"
        super().__init__(f"git {' '.join(self.args)}: {detail}")


@dataclass(frozen=True)
class FileChange:
    filename: str
    previous_filename: str | None
    status: str


def run_git(repository: str | Path, *args: str) -> bytes:
    """Run Git without a shell and return stdout as raw bytes."""
    command = ("-C", os.fspath(repository), *args)
    completed = subprocess.run(
        ("git", *command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if completed.returncode:
        raise GitCommandError(args, completed.stderr)
    return completed.stdout


def literal_pathspec(path: str) -> str:
    """Prevent Git pathspec metacharacters in a filename from changing scope."""
    return f":(literal){path}"


def parse_name_status(payload: bytes) -> list[FileChange]:
    """Parse ``git diff --name-status -z`` without decoding paths prematurely."""
    fields = payload.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    changes: list[FileChange] = []
    cursor = 0
    while cursor < len(fields):
        status_field = fields[cursor]
        cursor += 1
        if not status_field:
            continue
        code = chr(status_field[0])
        path_count = 2 if code in {"R", "C"} else 1
        if cursor + path_count > len(fields):
            raise ValueError("truncated NUL-delimited Git name-status output")
        paths = [os.fsdecode(field) for field in fields[cursor:cursor + path_count]]
        cursor += path_count
        if code in {"R", "C"}:
            old_path, new_path = paths
            changes.append(
                FileChange(
                    filename=new_path,
                    previous_filename=old_path,
                    status="renamed" if code == "R" else "copied",
                )
            )
        else:
            path = paths[0]
            status = {
                "A": "added",
                "D": "deleted",
                "T": "type_changed",
                "U": "unmerged",
            }.get(code, "modified")
            changes.append(FileChange(filename=path, previous_filename=None, status=status))
    return changes


def _diff_args(scope: str, base: str, head: str | None, paths: list[str] | None) -> list[str]:
    args = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--find-renames=50%",
        "--unified=3",
    ]
    if scope == "staged":
        args.append("--cached")
    args.append(base)
    if scope == "branch":
        if head is None:
            raise ValueError("branch diff requires a fixed head commit")
        args.append(head)
    if paths is not None:
        args.extend(("--", *(literal_pathspec(path) for path in paths)))
    return args


def git_diff(
    repository: str | Path,
    scope: str,
    base: str,
    head: str | None = None,
    paths: list[str] | None = None,
) -> bytes:
    """Return a unified patch for the requested immutable endpoints."""
    if scope not in {"worktree", "staged", "branch"}:
        raise ValueError(f"unsupported review scope: {scope}")
    return run_git(repository, *_diff_args(scope, base, head, paths))


def git_changes(
    repository: str | Path, scope: str, base: str, head: str | None = None
) -> list[FileChange]:
    args = [
        "diff",
        "--name-status",
        "-z",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--find-renames=50%",
    ]
    if scope == "staged":
        args.append("--cached")
    args.append(base)
    if scope == "branch":
        if head is None:
            raise ValueError("branch diff requires a fixed head commit")
        args.append(head)
    return parse_name_status(run_git(repository, *args))


def untracked_paths(repository: str | Path) -> list[str]:
    payload = run_git(repository, "ls-files", "--others", "--exclude-standard", "-z")
    paths = [os.fsdecode(path) for path in payload.split(b"\0") if path]
    return sorted(paths, key=os.fsencode)

