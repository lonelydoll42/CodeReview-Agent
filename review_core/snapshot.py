"""Capture immutable, byte-fingerprinted local Git review inputs."""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from .diff import (
    FileChange,
    GitCommandError,
    git_changes,
    git_diff,
    literal_pathspec,
    run_git,
    untracked_paths,
)


SCHEMA_VERSION = "2"
_READ_CHUNK_BYTES = 64 * 1024
_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".sql": "sql",
}


class SnapshotError(RuntimeError):
    """The requested review input could not be captured reliably."""


class SnapshotChangedError(SnapshotError):
    """The Git range or source files changed while they were being captured."""


class _SymlinkComponentError(OSError):
    """A worktree path traverses a symbolic-link directory."""


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return _sha256(encoded.encode("utf-8"))


def _language_for_path(path: str) -> str:
    return _LANGUAGES.get(Path(path).suffix.lower(), "unknown")


def _repository_root(repository: str | Path) -> Path:
    candidate = Path(repository).expanduser().resolve()
    try:
        root_bytes = run_git(candidate, "rev-parse", "--show-toplevel")
    except GitCommandError as exc:
        raise SnapshotError(f"not a Git repository: {candidate}") from exc
    return Path(os.fsdecode(root_bytes.rstrip(b"\r\n"))).resolve()


def _resolve_commit(repository: Path, reference: str) -> str:
    output = run_git(
        repository,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{reference}^{{commit}}",
    )
    commit = output.decode("ascii", "strict").strip()
    if not commit:
        raise SnapshotError(f"Git reference did not resolve to a commit: {reference}")
    return commit


def _default_branch_base(repository: Path) -> str:
    try:
        upstream = run_git(
            repository, "rev-parse", "--symbolic-full-name", "@{upstream}"
        ).decode("utf-8", "replace").strip()
        if upstream:
            return upstream
    except GitCommandError:
        pass
    try:
        origin_head = run_git(
            repository, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"
        ).decode("utf-8", "replace").strip()
        if origin_head:
            return origin_head
    except GitCommandError:
        pass
    raise SnapshotError(
        "branch scope needs base=... or a configured upstream/origin/HEAD"
    )


def _prepare_range(
    repository: Path,
    scope: str,
    base: str | None,
    head: str,
) -> tuple[str, str | None, str | None, str, str | None]:
    if scope not in {"worktree", "staged", "branch"}:
        raise ValueError("scope must be 'worktree', 'staged', or 'branch'")
    if scope in {"worktree", "staged"}:
        start_head = _resolve_commit(repository, "HEAD")
        return start_head, None, None, start_head, None

    base_ref = base if base is not None else _default_branch_base(repository)
    base_sha = _resolve_commit(repository, base_ref)
    head_sha = _resolve_commit(repository, head)
    merge_base_output = run_git(repository, "merge-base", base_sha, head_sha)
    merge_base = merge_base_output.decode("ascii", "strict").strip()
    if not merge_base:
        raise SnapshotError("Git could not determine the requested branch merge-base")
    return base_sha, head_sha, merge_base, merge_base, head_sha


def _state_bytes(
    repository: Path,
    scope: str,
    diff_base: str,
    diff_head: str | None,
) -> tuple[bytes, bytes, bytes]:
    changes = run_git(
        repository,
        "diff",
        "--name-status",
        "-z",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--find-renames=50%",
        *( ("--cached",) if scope == "staged" else () ),
        diff_base,
        *( (diff_head,) if scope == "branch" and diff_head else () ),
    )
    whole_diff = git_diff(repository, scope, diff_base, diff_head)
    untracked = (
        "\0".join(untracked_paths(repository)).encode("utf-8", "surrogateescape")
        if scope == "worktree"
        else b""
    )
    return changes, whole_diff, untracked


def _state_fingerprint(state: tuple[bytes, bytes, bytes]) -> str:
    digest = hashlib.sha256()
    for part in state:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _git_entry(repository: Path, revision: str, path: str) -> tuple[str, int] | None:
    payload = run_git(
        repository,
        "ls-tree",
        "-r",
        "-z",
        "-l",
        revision,
        "--",
        literal_pathspec(path),
    )
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            metadata, entry_path = record.split(b"\t", 1)
            mode, object_type, oid, size = metadata.split()
        except ValueError as exc:
            raise SnapshotError("could not parse Git tree entry") from exc
        if os.fsdecode(entry_path) != path:
            continue
        if object_type != b"blob":
            return ("", -1)
        return oid.decode("ascii"), int(size)
    return None


def _index_entry(repository: Path, path: str) -> tuple[str, int] | str | None:
    payload = run_git(
        repository,
        "ls-files",
        "--stage",
        "-z",
        "--",
        literal_pathspec(path),
    )
    entries: list[tuple[int, str, int]] = []
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            metadata, entry_path = record.split(b"\t", 1)
            _mode, oid, stage = metadata.split()
        except ValueError as exc:
            raise SnapshotError("could not parse Git index entry") from exc
        if os.fsdecode(entry_path) == path:
            entries.append((int(stage), oid.decode("ascii"), 0))
    if not entries:
        return None
    stage_zero = [entry for entry in entries if entry[0] == 0]
    if len(stage_zero) != 1 or len(entries) != 1:
        return "unmerged_index"
    oid = stage_zero[0][1]
    size_output = run_git(repository, "cat-file", "-s", oid)
    return oid, int(size_output.strip())


def _read_blob(repository: Path, oid: str, expected_size: int, max_file_bytes: int) -> dict[str, Any]:
    process = subprocess.Popen(
        ("git", "-C", os.fspath(repository), "cat-file", "blob", oid),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    contains_nul = False
    keep_content = expected_size <= max_file_bytes
    while True:
        chunk = process.stdout.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        hasher.update(chunk)
        contains_nul = contains_nul or b"\0" in chunk
        if keep_content:
            if total <= max_file_bytes:
                chunks.append(chunk)
            else:
                keep_content = False
                chunks.clear()
    stderr = process.stderr.read() if process.stderr is not None else b""
    returncode = process.wait()
    if returncode:
        raise GitCommandError(("cat-file", "blob", oid), stderr)
    if total != expected_size:
        raise SnapshotChangedError(f"Git blob size changed while reading {oid}")
    fingerprint = hasher.hexdigest()
    if total > max_file_bytes:
        content, reason = None, "file_too_large"
    elif contains_nul:
        content, reason = None, "binary_file"
    else:
        data = b"".join(chunks)
        try:
            content, reason = data.decode("utf-8"), None
        except UnicodeDecodeError:
            content, reason = None, "not_utf8_text"
    return {
        "content": content,
        "fingerprint": fingerprint,
        "unavailable_reason": reason,
    }


def _path_on_disk(repository: Path, path: str) -> Path:
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"path is not repository-relative: {path!r}")
    return repository.joinpath(*pure.parts)


@contextmanager
def _open_worktree_parent(repository: Path, path: str) -> Iterator[tuple[int | None, str, Path]]:
    """Open every parent directory without following symlinks where supported."""
    candidate = _path_on_disk(repository, path)
    parts = PurePosixPath(path).parts
    descriptors: list[int] = []
    parent_fd: int | None = None
    supports_dir_fd = (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.readlink in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
    )
    try:
        if supports_dir_fd:
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
            parent_fd = os.open(repository, flags)
            descriptors.append(parent_fd)
            for part in parts[:-1]:
                metadata = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise _SymlinkComponentError(f"symlink path component: {part}")
                if not stat.S_ISDIR(metadata.st_mode):
                    raise NotADirectoryError(part)
                try:
                    next_fd = os.open(
                        part,
                        flags | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_fd,
                    )
                except OSError:
                    try:
                        changed = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                    except OSError:
                        raise
                    if stat.S_ISLNK(changed.st_mode):
                        raise _SymlinkComponentError(f"symlink path component: {part}")
                    raise
                descriptors.append(next_fd)
                parent_fd = next_fd
        else:
            current = repository
            for part in parts[:-1]:
                current = current / part
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise _SymlinkComponentError(f"symlink path component: {part}")
                if not stat.S_ISDIR(metadata.st_mode):
                    raise NotADirectoryError(os.fspath(current))
            if not current.resolve(strict=True).is_relative_to(repository):
                raise _SymlinkComponentError("worktree path escaped the repository")
        yield parent_fd, parts[-1], candidate
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _stat_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_worktree(repository: Path, path: str, max_file_bytes: int) -> dict[str, Any]:
    try:
        with _open_worktree_parent(repository, path) as (parent_fd, leaf, candidate):
            before_stat = (
                candidate.lstat()
                if parent_fd is None
                else os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            )

            if stat.S_ISLNK(before_stat.st_mode):
                target = (
                    os.readlink(candidate)
                    if parent_fd is None
                    else os.readlink(leaf, dir_fd=parent_fd)
                )
                data = os.fsencode(target)
                after_stat = (
                    candidate.lstat()
                    if parent_fd is None
                    else os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                )
                if _stat_signature(before_stat) != _stat_signature(after_stat):
                    raise SnapshotChangedError(f"working-tree path changed while reading: {path}")
                if len(data) > max_file_bytes:
                    return {"content": None, "fingerprint": _sha256(data), "unavailable_reason": "file_too_large"}
                if b"\0" in data:
                    return {"content": None, "fingerprint": _sha256(data), "unavailable_reason": "binary_file"}
                try:
                    content = data.decode("utf-8")
                    reason = None
                except UnicodeDecodeError:
                    content, reason = None, "not_utf8_text"
                return {"content": content, "fingerprint": _sha256(data), "unavailable_reason": reason}

            if not stat.S_ISREG(before_stat.st_mode):
                return {"content": None, "fingerprint": None, "unavailable_reason": "non_regular_file"}

            hasher = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            contains_nul = False
            keep_content = before_stat.st_size <= max_file_bytes
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = (
                os.open(candidate, flags)
                if parent_fd is None
                else os.open(leaf, flags, dir_fd=parent_fd)
            )
            with os.fdopen(descriptor, "rb") as source:
                opened_stat = os.fstat(source.fileno())
                if _stat_signature(before_stat) != _stat_signature(opened_stat):
                    raise SnapshotChangedError(f"working-tree path changed while opening: {path}")
                while True:
                    chunk = source.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    hasher.update(chunk)
                    contains_nul = contains_nul or b"\0" in chunk
                    if keep_content:
                        if total <= max_file_bytes:
                            chunks.append(chunk)
                        else:
                            keep_content = False
                            chunks.clear()
            after_stat = (
                candidate.lstat()
                if parent_fd is None
                else os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            )
    except FileNotFoundError:
        return {"content": None, "fingerprint": None, "unavailable_reason": "source_not_found"}
    except _SymlinkComponentError:
        return {"content": None, "fingerprint": None, "unavailable_reason": "symlink_component"}
    except NotADirectoryError:
        return {"content": None, "fingerprint": None, "unavailable_reason": "non_directory_path"}
    except (OSError, ValueError):
        return {"content": None, "fingerprint": None, "unavailable_reason": "unreadable_source"}
    if _stat_signature(before_stat) != _stat_signature(after_stat):
        raise SnapshotChangedError(f"working-tree path changed while reading: {path}")
    fingerprint = hasher.hexdigest()
    if total > max_file_bytes:
        content, reason = None, "file_too_large"
    elif contains_nul:
        content, reason = None, "binary_file"
    else:
        try:
            content, reason = b"".join(chunks).decode("utf-8"), None
        except UnicodeDecodeError:
            content, reason = None, "not_utf8_text"
    return {"content": content, "fingerprint": fingerprint, "unavailable_reason": reason}


def _source(repository: Path, scope: str, path: str | None, side: str, base: str, head: str,
            merge_base: str | None, max_file_bytes: int) -> dict[str, Any]:
    if path is None:
        return {"path": None, "content": None, "fingerprint": None, "unavailable_reason": None}
    if scope == "worktree" and side == "after":
        result = _read_worktree(repository, path, max_file_bytes)
    elif scope == "staged" and side == "after":
        entry = _index_entry(repository, path)
        if entry is None:
            result = {"content": None, "fingerprint": None, "unavailable_reason": "source_not_found"}
        elif isinstance(entry, str):
            result = {"content": None, "fingerprint": None, "unavailable_reason": entry}
        else:
            result = _read_blob(repository, entry[0], entry[1], max_file_bytes)
    else:
        revision = merge_base if scope == "branch" and side == "before" else None
        if scope == "branch" and side == "after":
            revision = head
        elif scope != "branch":
            revision = base
        assert revision is not None
        entry = _git_entry(repository, revision, path)
        if entry is None:
            result = {"content": None, "fingerprint": None, "unavailable_reason": "source_not_found"}
        elif entry[1] < 0:
            result = {"content": None, "fingerprint": _sha256(entry[0].encode()), "unavailable_reason": "submodule"}
        else:
            result = _read_blob(repository, entry[0], entry[1], max_file_bytes)
    return {"path": path, **result}


def _change_paths(change: FileChange) -> tuple[str | None, str | None]:
    old_path = change.previous_filename or change.filename
    new_path = change.filename
    if change.status in {"added", "untracked"}:
        return None, new_path
    if change.status == "deleted":
        return old_path, None
    return old_path, new_path


def _git_record(repository: Path, scope: str, diff_base: str, diff_head: str | None,
                change: FileChange, max_file_bytes: int, base: str, head: str | None,
                merge_base: str | None) -> dict[str, Any]:
    before_path, after_path = _change_paths(change)
    paths = list(dict.fromkeys(path for path in (before_path, after_path) if path is not None))
    before = _source(repository, scope, before_path, "before", base, head, merge_base, max_file_bytes)
    after = _source(repository, scope, after_path, "after", base, head, merge_base, max_file_bytes)
    if scope == "worktree" and after["unavailable_reason"] == "symlink_component":
        patch = b""
    else:
        patch = git_diff(repository, scope, diff_base, diff_head, paths)
    if (
        before["unavailable_reason"] is None
        and after["unavailable_reason"] is None
        and before["fingerprint"] != after["fingerprint"]
    ):
        patch_text = _render_text_diff(
            before_path, after_path, before["content"] or "", after["content"] or ""
        )
        if patch_text:
            patch = patch_text.encode("utf-8", "surrogateescape")
    return {
        "filename": change.filename,
        "previous_filename": change.previous_filename,
        "status": change.status,
        "language": _language_for_path(change.filename),
        "diff": _decode(patch),
        "before": before,
        "after": after,
    }


def _quote_git_path(value: str) -> str:
    raw = os.fsencode(value)
    if all(0x20 <= byte < 0x7f and byte not in (ord('"'), ord("\\")) for byte in raw):
        return value
    pieces: list[str] = ['"']
    for byte in raw:
        if byte == ord('"'):
            pieces.append('\\"')
        elif byte == ord("\\"):
            pieces.append("\\\\")
        elif 0x20 <= byte < 0x7f:
            pieces.append(chr(byte))
        elif byte == 0x09:
            pieces.append("\\t")
        elif byte == 0x0a:
            pieces.append("\\n")
        elif byte == 0x0d:
            pieces.append("\\r")
        else:
            pieces.append(f"\\{byte:03o}")
    pieces.append('"')
    return "".join(pieces)


def _range(start: int, count: int) -> str:
    if count == 0:
        return f"{start},0"
    if count == 1:
        return str(start + 1)
    return f"{start + 1},{count}"


def _render_diff_line(output: list[str], marker: str, line: str) -> None:
    output.append(marker + line)
    if not line.endswith("\n"):
        output.append("\n\\ No newline at end of file\n")


def _render_text_diff(
    old_path: str | None, new_path: str | None, before: str, after: str
) -> str:
    old_lines = before.splitlines(keepends=True)
    new_lines = after.splitlines(keepends=True)
    old_label = "/dev/null" if old_path is None else _quote_git_path(f"a/{old_path}")
    new_label = "/dev/null" if new_path is None else _quote_git_path(f"b/{new_path}")
    display_old = old_path or new_path or ""
    display_new = new_path or old_path or ""
    output = [
        f"diff --git {_quote_git_path(f'a/{display_old}')} {_quote_git_path(f'b/{display_new}')}\n",
        f"--- {old_label}\n",
        f"+++ {new_label}\n",
    ]
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for group in matcher.get_grouped_opcodes(3):
        old_start, old_end = group[0][1], group[-1][2]
        new_start, new_end = group[0][3], group[-1][4]
        output.append(
            f"@@ -{_range(old_start, old_end - old_start)} "
            f"+{_range(new_start, new_end - new_start)} @@\n"
        )
        for tag, i1, i2, j1, j2 in group:
            if tag in {"equal", "delete", "replace"}:
                for line in old_lines[i1:i2]:
                    _render_diff_line(output, " " if tag == "equal" else "-", line)
            if tag in {"insert", "replace"}:
                for line in new_lines[j1:j2]:
                    _render_diff_line(output, "+", line)
    return "".join(output)


def _render_untracked_diff(path: str, content: str) -> str:
    return _render_text_diff(None, path, "", content)


def _untracked_record(repository: Path, path: str, max_file_bytes: int) -> dict[str, Any]:
    after = {"path": path, **_read_worktree(repository, path, max_file_bytes)}
    if after["content"] is not None:
        patch = _render_untracked_diff(path, after["content"])
    elif after["unavailable_reason"] == "symlink_component":
        patch = ""
    else:
        patch = ""
    return {
        "filename": path,
        "previous_filename": None,
        "status": "untracked",
        "language": _language_for_path(path),
        "diff": patch,
        "before": {"path": None, "content": None, "fingerprint": None, "unavailable_reason": None},
        "after": after,
    }


def _manifest_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest["schema_version"],
        "scope": manifest["scope"],
        "repository": manifest["repository"],
        "base": manifest["base"],
        "head": manifest["head"],
        "head_source": manifest["head_source"],
        "merge_base": manifest["merge_base"],
        "context_policy": manifest["context_policy"],
        "limits": manifest["limits"],
        "files": manifest["files"],
        "untracked_files": manifest["untracked_files"],
        "context_files": manifest["context_files"],
    }


def _refresh_content_fingerprint(manifest: dict[str, Any]) -> None:
    manifest["content_fingerprint"] = _canonical_fingerprint(_manifest_payload(manifest))


def _range_fingerprint(manifest: dict[str, Any]) -> str:
    payload = {
        "schema_version": manifest["schema_version"],
        "scope": manifest["scope"],
        "repository": manifest["repository"],
        "base": manifest["base"],
        "head": manifest["head"],
        "head_source": manifest["head_source"],
        "merge_base": manifest["merge_base"],
        "files": manifest["files"],
        "untracked_files": manifest["untracked_files"],
    }
    return _canonical_fingerprint(payload)


def _capture_once(repository: Path, scope: str, base: str | None, head: str,
                  include_untracked: bool, max_file_bytes: int) -> tuple[dict[str, Any], str]:
    base_value, head_value, merge_base, diff_base, diff_head = _prepare_range(
        repository, scope, base, head
    )
    state_before = _state_bytes(repository, scope, diff_base, diff_head)
    changes = []
    for change in git_changes(repository, scope, diff_base, diff_head):
        changes.append(
            _git_record(
                repository,
                scope,
                diff_base,
                diff_head,
                change,
                max_file_bytes,
                base_value,
                head_value,
                merge_base,
            )
        )
    paths = untracked_paths(repository) if scope == "worktree" else []
    if scope == "worktree" and include_untracked:
        changes.extend(_untracked_record(repository, path, max_file_bytes) for path in paths)
    changes.sort(key=lambda item: (os.fsencode(item["filename"]), item["status"]))
    state_after = _state_bytes(repository, scope, diff_base, diff_head)
    if state_before != state_after:
        raise SnapshotChangedError("Git change range changed while the snapshot was being collected")
    if scope in {"worktree", "staged"} and _resolve_commit(repository, "HEAD") != base_value:
        raise SnapshotChangedError("HEAD changed while the snapshot was being collected")

    context_policy = {
        "worktree": "HEAD to the final tracked working-tree contents; staged and unstaged edits are represented once",
        "staged": "HEAD to the exact Git index blobs; working-tree-only edits are excluded",
        "branch": "merge-base(base, head) to the fixed head commit; worktree and index edits are excluded",
        "context_files": "captured from the same source endpoints as the selected scope",
    }
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "repository": os.fspath(repository),
        "base": base_value,
        "head": head_value,
        "head_source": {"worktree": "worktree", "staged": "index", "branch": "commit"}[scope],
        "merge_base": merge_base,
        "content_fingerprint": "",
        "files": changes,
        "untracked_files": paths,
        "context_files": [],
        "context_policy": context_policy,
        "limits": {
            "max_file_bytes": max_file_bytes,
            "include_untracked": bool(include_untracked and scope == "worktree"),
            "untracked_contents_included": bool(include_untracked and scope == "worktree"),
            "change_detection": "double-pass; Git range is checked before and after each pass",
        },
    }
    manifest["range_fingerprint"] = _range_fingerprint(manifest)
    manifest["git_state_fingerprint"] = _state_fingerprint(state_after)
    _refresh_content_fingerprint(manifest)
    return manifest, _state_fingerprint(state_after)


def _collect_stable(repository: Path, scope: str, base: str | None, head: str,
                    include_untracked: bool, max_file_bytes: int) -> dict[str, Any]:
    first, first_state = _capture_once(
        repository, scope, base, head, include_untracked, max_file_bytes
    )
    second, second_state = _capture_once(
        repository, scope, base, head, include_untracked, max_file_bytes
    )
    if first_state != second_state or first["range_fingerprint"] != second["range_fingerprint"]:
        raise SnapshotChangedError("Git changes or source contents changed during snapshot collection")
    return second


def _validate_options(include_untracked: bool, max_file_bytes: int) -> None:
    if not isinstance(include_untracked, bool):
        raise TypeError("include_untracked must be a bool")
    if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 0:
        raise ValueError("max_file_bytes must be a non-negative integer")


def collect_changes(
    repository: str | Path,
    scope: str = "worktree",
    base: str | None = None,
    head: str = "HEAD",
    include_untracked: bool = False,
    max_file_bytes: int = 1_048_576,
    context_paths: Iterable[str | Path] = (),
) -> dict[str, Any]:
    """Collect a content-pinned Git review input with schema version 2.

    ``worktree`` compares HEAD to the final tracked worktree, ``staged`` reads
    HEAD-to-index blobs, and ``branch`` compares the fixed merge-base/head
    commits. Optional context files use those same endpoints.
    """
    _validate_options(include_untracked, max_file_bytes)
    if include_untracked and scope != "worktree":
        raise ValueError("include_untracked is only valid for worktree scope")
    root = _repository_root(repository)
    manifest = _collect_stable(root, scope, base, head, include_untracked, max_file_bytes)
    requested_context = list(context_paths)
    if requested_context:
        return capture_context(manifest, requested_context, max_file_bytes=max_file_bytes)
    return manifest


def _normalize_context_path(path: str | Path) -> str:
    value = os.fspath(path)
    if "\0" in value:
        raise ValueError("context paths cannot contain NUL")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in {".", ".."} for part in pure.parts):
        raise ValueError(f"context path must be repository-relative: {value!r}")
    return pure.as_posix()


def _capture_context_once(manifest: dict[str, Any], paths: list[str], max_file_bytes: int) -> list[dict[str, Any]]:
    repository = Path(manifest["repository"])
    scope = manifest["scope"]
    base = manifest["base"]
    head = manifest["head"]
    merge_base = manifest["merge_base"]
    changed = {
        item["filename"] for item in manifest["files"]
    } | {
        item["previous_filename"] for item in manifest["files"] if item["previous_filename"]
    }
    result = []
    for path in paths:
        if path in changed:
            continue
        before = _source(repository, scope, path, "before", base, head, merge_base, max_file_bytes)
        after = _source(repository, scope, path, "after", base, head, merge_base, max_file_bytes)
        result.append({
            "filename": path,
            "previous_filename": None,
            "status": "context",
            "language": _language_for_path(path),
            "diff": "",
            "before": before,
            "after": after,
        })
    return result


def capture_context(
    manifest: dict[str, Any],
    paths: list[str | Path],
    *,
    max_file_bytes: int | None = None,
) -> dict[str, Any]:
    """Add unchanged callers/tests from the manifest's pinned source endpoints."""
    if not isinstance(paths, list):
        raise TypeError("paths must be a list")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("capture_context requires a schema_version='2' manifest")
    byte_limit = max_file_bytes
    if byte_limit is None:
        byte_limit = manifest.get("limits", {}).get("max_file_bytes", 1_048_576)
    _validate_options(False, byte_limit)
    normalized = list(dict.fromkeys(_normalize_context_path(path) for path in paths))
    if not normalized:
        return copy.deepcopy(manifest)

    current = _collect_stable(
        Path(manifest["repository"]),
        manifest["scope"],
        manifest["base"] if manifest["scope"] == "branch" else None,
        manifest["head"] if manifest["scope"] == "branch" else "HEAD",
        bool(manifest.get("limits", {}).get("include_untracked", False)),
        byte_limit,
    )
    if current["range_fingerprint"] != manifest.get("range_fingerprint"):
        raise SnapshotChangedError("review range changed since the manifest was captured")

    first = _capture_context_once(manifest, normalized, byte_limit)
    second = _capture_context_once(manifest, normalized, byte_limit)
    if _canonical_fingerprint(first) != _canonical_fingerprint(second):
        raise SnapshotChangedError("context files changed while the context snapshot was being collected")

    current_after = _collect_stable(
        Path(manifest["repository"]),
        manifest["scope"],
        manifest["base"] if manifest["scope"] == "branch" else None,
        manifest["head"] if manifest["scope"] == "branch" else "HEAD",
        bool(manifest.get("limits", {}).get("include_untracked", False)),
        byte_limit,
    )
    if current_after["range_fingerprint"] != manifest.get("range_fingerprint"):
        raise SnapshotChangedError("review range changed while context files were being collected")

    result = copy.deepcopy(manifest)
    merged = {item["filename"]: item for item in result.get("context_files", [])}
    merged.update({item["filename"]: item for item in second})
    result["context_files"] = sorted(merged.values(), key=lambda item: os.fsencode(item["filename"]))
    result["limits"]["context_unavailable"] = [
        {"filename": item["filename"], "before": item["before"]["unavailable_reason"],
         "after": item["after"]["unavailable_reason"]}
        for item in result["context_files"]
        if item["before"]["unavailable_reason"] or item["after"]["unavailable_reason"]
    ]
    _refresh_content_fingerprint(result)
    return result
