"""Conservatively normalize syscall events from raw strace output.

Capture with ``strace -f --decode-pids=pidns -yy -s 65536 -e raw=read,pread64,readv,preadv,preadv2``.
Read buffers must remain raw pointers; decoded buffer text is rejected so the
trace cannot carry file data.
"""

from __future__ import annotations

import ast
from bisect import bisect_right
import hashlib
import posixpath
import re
from pathlib import Path
from typing import Any


NORMALIZER_NAME = "eval.runtime_trace"
NORMALIZER_VERSION = "9"
STRACE_STRING_LIMIT = 65536
NORMALIZER_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

_PID_PREFIX = re.compile(r"^(?:\[pid\s+(?P<bracketed>\d+)\]|(?P<plain>\d+))\s+(?P<body>.*)$")
_CALL = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\((?P<args>.*)\)\s+=\s+(?P<result>.*)$")
_NUMBER = r"-?(?:0x[0-9a-fA-F]+|\d+)"
_RETURN = re.compile(rf"^\s*({_NUMBER})\b(?:\s+([A-Z][A-Z0-9_]+))?")
_STRACE_NAMESPACE_PID = re.compile(r"/\*\s*(?P<pid>\d+)\s+in strace's PID NS\s*\*/")
_UNAVAILABLE_RESULT = re.compile(
    r"^\s*\?(?:\s+(?P<restart_code>"
    r"ERESTART(?:SYS|NOHAND|NOINTR|_RESTARTBLOCK))\b"
    r"(?:\s+\([^()\r\n]*\))?)?\s*$"
)
_FD = re.compile(rf"^\s*({_NUMBER})\b")
_FD_ANNOTATION = re.compile(
    rf"^\s*(?:AT_FDCWD|{_NUMBER})<(?P<path>/[^<>]*)>\s*$"
)
_FD_KIND_ANNOTATION = re.compile(rf"^\s*({_NUMBER})<(?P<kind>[^<>]+)>\s*$")
_READ_SYSCALLS = {"read", "pread64", "readv", "preadv", "preadv2"}
_RELEVANT = re.compile(
    r"^(?:execve(?:at)?|open(?:at2?|at)?|creat|close|read|pread64|readv|preadv2?|mmap2?|pipe2?|eventfd2?|dup2?|dup3|fcntl|close_range|clone3?|fork|vfork)\("
)
_EXITED = re.compile(r"^\+\+\+ exited with (\d+) \+\+\+$")
_SIGNALED = re.compile(r"^\+\+\+ killed by ([A-Z0-9]+)(?: \(.*\))? \+\+\+$")
_RAW_POINTER = re.compile(r"^(?:0x[0-9a-fA-F]+|NULL)$")
_RESUMED = re.compile(r"^<\.\.\. ([A-Za-z_][A-Za-z0-9_]*) resumed>(.*)$")


def normalizer_identity() -> dict[str, str]:
    """Return the parser source identity pinned when this module was loaded."""
    return {
        "name": NORMALIZER_NAME,
        "version": NORMALIZER_VERSION,
        "source_sha256": NORMALIZER_SOURCE_SHA256,
    }


def _split_args(value: str) -> list[str]:
    args: list[str] = []
    start = 0
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}

    for index, char in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}" and stack and stack[-1] == pairs[char]:
            stack.pop()
        elif char == "," and not stack:
            args.append(value[start:index].strip())
            start = index + 1
    if quote is not None or stack:
        raise ValueError("unbalanced syscall arguments")
    tail = value[start:].strip()
    if tail or value.strip():
        args.append(tail)
    return args


def _call_args(name: str, value: str) -> list[str]:
    if name in {"clone", "clone3"}:
        value = _STRACE_NAMESPACE_PID.sub("", value)
    return _split_args(value)


def _literal_string(value: str) -> str:
    parsed = ast.literal_eval(value.strip())
    if not isinstance(parsed, str) or not parsed.isascii():
        raise ValueError("expected an ASCII quoted string")
    return parsed


def _literal_argv(value: str) -> list[str]:
    parsed = ast.literal_eval(value.strip())
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ValueError("argv is incomplete or not a string array")
    return parsed


def _return(result: str) -> tuple[int, str | None]:
    match = _RETURN.match(result)
    if match is None:
        raise ValueError("syscall result is not an integer")
    return _integer(match.group(1)), match.group(2)


def _clone_child_trace_pid(result: str) -> tuple[int, str | None, bool]:
    child_pid, error = _return(result)
    translated = _STRACE_NAMESPACE_PID.search(result)
    if error is None:
        if translated is not None:
            child_pid = int(translated.group("pid"))
    return child_pid, error, translated is not None


def _unavailable_syscall_result(result: str) -> tuple[bool, str | None]:
    match = _UNAVAILABLE_RESULT.fullmatch(result)
    return match is not None, match.group("restart_code") if match is not None else None


def _fd(value: str) -> int:
    match = _FD.match(value)
    if match is None:
        raise ValueError("file descriptor is not numeric")
    return _integer(match.group(1))


def _integer(value: str) -> int:
    return int(value, 16) if value.lstrip("-").lower().startswith("0x") else int(value, 10)


def _open_path_and_flags(name: str, args: list[str]) -> tuple[str, str]:
    if name == "open":
        path_index, flags_index = 0, 1
    elif name == "creat":
        path_index, flags_index = 0, None
    else:
        path_index, flags_index = 1, 2
    if len(args) <= path_index:
        raise ValueError("open syscall has no path argument")
    path = _literal_string(args[path_index])
    flags = args[flags_index] if flags_index is not None and len(args) > flags_index else ""
    return path, flags


def _absolute_fd_path(value: str) -> str | None:
    match = _FD_ANNOTATION.fullmatch(value.strip())
    if match is None:
        return None
    path = match.group("path")
    if not path.isascii() or "\\" in path or path.endswith(" (deleted)"):
        return None
    return path


def _fd_annotation_kind(value: str) -> str | None:
    match = _FD_KIND_ANNOTATION.fullmatch(value.strip())
    if match is None:
        return None
    annotation = match.group("kind")
    if annotation.startswith("pipe:["):
        return "pipe"
    if annotation.startswith("anon_inode:"):
        return "anon_inode"
    if annotation.startswith("socket:["):
        return "socket"
    return None


def _fd_path(
    value: str,
    table: dict[int, dict[str, Any] | None],
) -> tuple[str | None, str | None, str | None, bool, bool]:
    annotated = _absolute_fd_path(value)
    try:
        state = table.get(_fd(value))
    except ValueError:
        state = None
    tracked = state.get("path") if state is not None else None
    disagreement = tracked is not None and annotated is not None and tracked != annotated
    conflict = disagreement and not _known_path_equivalent(tracked, annotated)
    return tracked or annotated, tracked, annotated, disagreement, conflict


def _unproven_proc_alias(left: str, right: str) -> bool:
    proc_self = "/proc/self"
    for requested, observed in ((left, right), (right, left)):
        if requested != proc_self and not requested.startswith(proc_self + "/"):
            continue
        match = re.fullmatch(r"/proc/[1-9][0-9]*(?P<suffix>/.*)?", observed)
        if match is not None and (match.group("suffix") or "") == requested[len(proc_self):]:
            return True
    return False


def _known_path_equivalent(requested: str, observed: str) -> bool:
    if requested == observed:
        return True

    requested_candidates = [requested]
    for alias, target in (("/lib", "/usr/lib"), ("/lib64", "/usr/lib64")):
        if requested == alias or requested.startswith(alias + "/"):
            resolved = target + requested[len(alias):]
            if resolved == observed:
                return True
            requested_candidates.append(resolved)

    observed_parent, observed_name = posixpath.split(observed)
    library_roots = ("/lib/", "/lib64/", "/usr/lib/", "/usr/lib64/")
    for candidate in requested_candidates:
        requested_parent, requested_name = posixpath.split(candidate)
        soname = re.fullmatch(r"lib[^/]+\.so(?:\.\d+)*", requested_name)
        if (
            requested_parent == observed_parent
            and requested_parent.startswith(library_roots)
            and soname is not None
            and observed_name.startswith(requested_name + ".")
            and re.fullmatch(r"(?:\d+\.)*\d+", observed_name[len(requested_name) + 1:])
        ):
            return True
    return False


def _observed_open_path(
    name: str,
    args: list[str],
    result: str,
    table: dict[int, dict[str, Any] | None],
) -> dict[str, Any]:
    try:
        path, flags = _open_path_and_flags(name, args)
    except (IndexError, SyntaxError, ValueError):
        return {
            "path": None, "requested_path": None, "returned_path": None,
            "path_candidates": [], "flags": "", "dirfd_tracked_path": None,
            "dirfd_annotation_path": None, "dirfd_path_disagreement": False,
            "dirfd_path_conflict": False, "path_disagreement": False,
            "path_conflict": False, "path_alias_status": "same",
        }

    returned_path = _absolute_fd_path(result)
    requested_paths: list[str] = []
    dirfd_tracked_path = None
    dirfd_annotation_path = None
    dirfd_path_disagreement = False
    dirfd_path_conflict = False
    if path.startswith("/"):
        requested_paths.append(posixpath.normpath(path))
    elif name in {"openat", "openat2"} and args:
        base, dirfd_tracked_path, dirfd_annotation_path, dirfd_path_disagreement, dirfd_path_conflict = _fd_path(
            args[0], table,
        )
        for candidate_base in (dirfd_tracked_path, dirfd_annotation_path):
            if candidate_base is not None:
                candidate = posixpath.normpath(posixpath.join(candidate_base, path))
                if candidate not in requested_paths:
                    requested_paths.append(candidate)
        if not requested_paths and base is not None:
            requested_paths.append(posixpath.normpath(posixpath.join(base, path)))

    requested_path = requested_paths[0] if requested_paths else None
    path_disagreement = dirfd_path_disagreement or (
        requested_path is not None
        and returned_path is not None
        and requested_path != returned_path
    )
    result_path_disagreement = (
        requested_path is not None
        and returned_path is not None
        and requested_path != returned_path
    )
    result_path_conflict = (
        result_path_disagreement
        and not _known_path_equivalent(requested_path, returned_path)
        and not _unproven_proc_alias(requested_path, returned_path)
    )
    path_conflict = dirfd_path_conflict or result_path_conflict
    unproven_alias = (
        (dirfd_path_disagreement and dirfd_tracked_path is not None
         and dirfd_annotation_path is not None
         and _unproven_proc_alias(dirfd_tracked_path, dirfd_annotation_path))
        or (result_path_disagreement and _unproven_proc_alias(requested_path, returned_path))
    )
    if path_conflict:
        alias_status = "conflict"
    elif unproven_alias:
        alias_status = "unproven"
    elif path_disagreement:
        alias_status = "known"
    else:
        alias_status = "same"

    path_candidates = list(requested_paths)
    if returned_path is not None and returned_path not in path_candidates:
        path_candidates.append(returned_path)
    return {
        "path": returned_path or requested_path,
        "requested_path": requested_path,
        "returned_path": returned_path,
        "path_candidates": path_candidates,
        "flags": flags,
        "dirfd_tracked_path": dirfd_tracked_path,
        "dirfd_annotation_path": dirfd_annotation_path,
        "dirfd_path_disagreement": dirfd_path_disagreement,
        "dirfd_path_conflict": dirfd_path_conflict,
        "path_disagreement": path_disagreement,
        "path_conflict": path_conflict,
        "path_alias_status": alias_status,
    }


def _remember_fd_path(
    table: dict[int, dict[str, Any] | None], fd_number: int, path: str
) -> dict[str, Any]:
    previous = table.get(fd_number)
    state = {
        "kind": "file",
        "path": path,
        "cloexec": previous.get("cloexec") if previous is not None else None,
        "readable": previous.get("readable") if previous is not None else None,
    }
    table[fd_number] = state
    return state


def _fd_observed_path(
    value: str,
    fd_number: int,
    table: dict[int, dict[str, Any] | None],
    pid: int,
) -> tuple[dict[str, Any] | None, str | None, str | None, bool, bool, str, list[str]]:
    state = table.get(fd_number)
    tracked_path = state.get("path") if state is not None else None
    annotated_path = _absolute_fd_path(value)
    disagreement = (
        annotated_path is not None
        and tracked_path is not None
        and annotated_path != tracked_path
    )
    conflict = (
        disagreement
        and not _known_path_equivalent(tracked_path, annotated_path)
        and not _unproven_proc_alias(tracked_path, annotated_path)
    )
    if annotated_path is not None and tracked_path is None:
        state = _remember_fd_path(table, fd_number, annotated_path)
    elif state is None:
        kind = _fd_annotation_kind(value)
        if kind is not None:
            state = {"kind": kind, "path": None, "cloexec": None, "readable": None}
            table[fd_number] = state
    if conflict:
        alias_status = "conflict"
    elif disagreement and _unproven_proc_alias(tracked_path, annotated_path):
        alias_status = "unproven"
    elif disagreement:
        alias_status = "known"
    else:
        alias_status = "same"
    paths = [path for path in (tracked_path, annotated_path) if path is not None]
    return state, annotated_path or tracked_path, tracked_path, disagreement, conflict, alias_status, paths


def _event(event: str, pid: int, line: int, **fields: Any) -> dict[str, Any]:
    return {"event": event, "pid": pid, **fields, "trace_line": line}


def _pipe_fds(value: str) -> tuple[int, int]:
    match = re.fullmatch(
        r"\[\s*(-?(?:0x[0-9a-fA-F]+|\d+))(?:<[^>]*>)?\s*,\s*"
        r"(-?(?:0x[0-9a-fA-F]+|\d+))(?:<[^>]*>)?\s*\]",
        value.strip(),
    )
    if match is None:
        raise ValueError("pipe descriptors are unavailable")
    return _integer(match.group(1)), _integer(match.group(2))


def _copy_fd_table(table: dict[int, dict[str, Any] | None]) -> dict[int, dict[str, Any] | None]:
    return {fd: dict(state) if state is not None else None for fd, state in table.items()}


def _clone_shares_fds(name: str, args: list[str]) -> bool | None:
    if name == "fork":
        return False
    if name == "vfork":
        return False
    if name == "clone3":
        flags = args[0] if args else ""
        if "flags=" not in flags and not re.match(r"^\s*(?:0x[0-9a-fA-F]+|\d+)\s*$", flags):
            return None
    else:
        if len(args) < 2:
            return None
        flags = args[1]
    if "CLONE_FILES" in flags:
        return True
    numeric = re.match(r"^\s*(0x[0-9a-fA-F]+|\d+)\b", flags)
    if numeric is not None:
        return bool(_integer(numeric.group(1)) & 0x400)
    if "flags=" in flags or flags.strip():
        return False
    return None


def _readable_open(flags: str) -> bool | None:
    if "O_PATH" in flags or "O_WRONLY" in flags:
        return False
    if any(flag in flags for flag in ("O_RDONLY", "O_RDWR")) or not flags:
        return True
    numeric = re.fullmatch(r"\s*(0x[0-9a-fA-F]+|\d+)\s*", flags)
    if numeric is not None:
        value = _integer(numeric.group(1))
        if value & 0x200000:
            return False
        return value & 0x3 != 1
    return None


def _readable_mapping(protection: str) -> bool | None:
    if "PROT_READ" in protection:
        return True
    if "PROT_NONE" in protection:
        return False
    numeric = re.fullmatch(r"\s*(0x[0-9a-fA-F]+|\d+)\s*", protection)
    if numeric is not None:
        return bool(_integer(numeric.group(1)) & 0x1)
    if protection and not re.search(r"[A-Za-z_]", protection):
        return False
    return None


_MAPPING_FLAGS = {
    "MAP_SHARED", "MAP_PRIVATE", "MAP_SHARED_VALIDATE", "MAP_FIXED",
    "MAP_ANONYMOUS", "MAP_ANON", "MAP_32BIT", "MAP_GROWSDOWN",
    "MAP_DENYWRITE", "MAP_EXECUTABLE", "MAP_LOCKED", "MAP_NORESERVE",
    "MAP_POPULATE", "MAP_NONBLOCK", "MAP_STACK", "MAP_HUGETLB", "MAP_SYNC",
    "MAP_FIXED_NOREPLACE", "MAP_UNINITIALIZED", "MAP_FILE",
}


def _anonymous_mapping(flags: str) -> bool | None:
    numeric = re.fullmatch(r"\s*(0x[0-9a-fA-F]+|\d+)\s*", flags)
    if numeric is not None:
        value = _integer(numeric.group(1))
        if value < 0 or value & 0xF not in {1, 2, 3}:
            return None
        return bool(value & 0x20)

    tokens = {token.strip() for token in flags.split("|") if token.strip()}
    if not tokens or not tokens <= _MAPPING_FLAGS:
        return None
    if "MAP_PRIVATE" in tokens and ("MAP_SHARED" in tokens or "MAP_SHARED_VALIDATE" in tokens):
        return None
    return bool(tokens & {"MAP_ANONYMOUS", "MAP_ANON"})


def parse_raw_trace(raw_trace: bytes) -> dict[str, Any]:
    """Parse raw strace bytes without inferring call IDs or unknown FD paths.

    Forked descriptors inherit their observed state. Pipes are known non-file
    descriptors; reads from other descriptors without an observed absolute
    path are reported separately as path-attribution gaps.
    """
    result: dict[str, Any] = {
        "schema_version": 1,
        "normalizer": normalizer_identity(),
        "raw_trace_sha256": hashlib.sha256(raw_trace).hexdigest(),
        "events": [],
        "unknown": [],
        "attribution_unknown": [],
        "complete": True,
        "reason": None,
    }
    events: list[dict[str, Any]] = result["events"]
    unknown: list[dict[str, Any]] = result["unknown"]
    attribution_unknown: list[dict[str, Any]] = result["attribution_unknown"]
    fd_tables: dict[int, dict[int, dict[str, Any] | None]] = {}
    uncertain_fd_sharing: dict[int, dict[int, dict[str, Any] | None]] = {}
    successfully_closed: dict[int, set[int]] = {}

    def mark_unknown(line: int, reason: str, *, pid: int | None = None, operation: str | None = None) -> None:
        item: dict[str, Any] = {"trace_line": line, "reason": reason}
        if pid is not None:
            item["pid"] = pid
        if operation is not None:
            item["operation"] = operation
        unknown.append(item)

    def mark_attribution_unknown(
        line: int, reason: str, *, pid: int, operation: str
    ) -> None:
        attribution_unknown.append({
            "trace_line": line, "reason": reason, "pid": pid, "operation": operation,
        })

    def clear_closed_markers(table: dict[int, dict[str, Any] | None], fd_number: int) -> None:
        for owner_pid, owner_table in fd_tables.items():
            if owner_table is table:
                successfully_closed.setdefault(owner_pid, set()).discard(fd_number)

    def invalidate_uncertain_parent_fd(pid: int, fd_number: int) -> None:
        parent_table = uncertain_fd_sharing.get(pid)
        if parent_table is not None:
            parent_table[fd_number] = None
            clear_closed_markers(parent_table, fd_number)

    try:
        text = raw_trace.decode("utf-8")
    except UnicodeDecodeError as error:
        bad_line = raw_trace[:error.start].count(b"\n") + 1
        mark_unknown(bad_line, "trace_is_not_utf8")
        result.update(complete=False, reason="unparseable_trace")
        return result

    if not raw_trace:
        unknown.append({"reason": "trace_is_empty"})
        result.update(complete=False, reason="empty_trace")
        return result
    if not raw_trace.endswith((b"\n", b"\r")):
        mark_unknown(max(1, len(text.splitlines())), "trace_ends_with_unterminated_line")

    records: list[dict[str, Any]] = []
    record_indices_by_pid: dict[int, list[int]] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        prefix = _PID_PREFIX.match(line)
        if prefix is None:
            records.append({"line": line_number, "body": line, "pid": None})
            continue
        pid = int(prefix.group("bracketed") or prefix.group("plain"))
        record_indices_by_pid.setdefault(pid, []).append(len(records))
        records.append({"line": line_number, "body": prefix.group("body"), "pid": pid})

    pending_calls: dict[int, dict[str, Any]] = {}
    process_starts: dict[int, dict[str, Any]] = {}
    for index, record in enumerate(records):
        pid = record["pid"]
        body = record["body"]
        if pid is None:
            continue
        if "<unfinished ...>" in body:
            marker = body.find("<unfinished ...>")
            opening = body.find("(")
            name = body[:opening] if opening >= 0 else ""
            if opening >= 0 and _RELEVANT.match(body):
                if pid in pending_calls:
                    previous = pending_calls.pop(pid)
                    mark_unknown(
                        previous["line"], "unfinished_syscall_not_normalized",
                        pid=pid, operation=previous["name"],
                    )
                pending_calls[pid] = {
                    "name": name,
                    "args": body[opening + 1:marker].rstrip(),
                    "line": record["line"],
                    "index": index,
                }
                record["pending_start"] = name
            continue
        resumed = _RESUMED.match(body)
        if resumed is not None:
            call_name = resumed.group(1)
            pending = pending_calls.pop(pid, None)
            if pending is None or pending["name"] != call_name:
                if _RELEVANT.match(f"{call_name}("):
                    mark_unknown(record["line"], "unmatched_syscall_resume", pid=pid, operation=call_name)
                continue
            record["body"] = f"{call_name}({pending['args']}{resumed.group(2)}"
            record["call_start_index"] = pending["index"]
            record["call_start_line"] = pending["line"]
        call = _CALL.match(record["body"])
        if call is None or call.group("name") not in {"clone", "clone3", "fork", "vfork"}:
            continue
        try:
            call_args = _call_args(call.group("name"), call.group("args"))
            child_pid, error, namespace_pid = _clone_child_trace_pid(call.group("result"))
        except (SyntaxError, ValueError):
            continue
        if error is None and child_pid > 0:
            start_index = record.get("call_start_index", index)
            process_starts[start_index] = {
                "parent_pid": pid,
                "child_pid": child_pid,
                "namespace_pid": namespace_pid,
                "syscall": call.group("name"),
                "shares_fds": _clone_shares_fds(call.group("name"), call_args),
                "line": record.get("call_start_line", record["line"]),
            }
    for pid, pending in pending_calls.items():
        mark_unknown(
            pending["line"], "unfinished_syscall_not_normalized",
            pid=pid, operation=pending["name"],
        )

    live_trace_pids: set[int] = set()
    for index, record in enumerate(records):
        pid = record["pid"]
        line_number = record["line"]
        body = record["body"]
        if pid is None:
            if body.startswith("+++") or _RELEVANT.match(body):
                operation = body.split("(", 1)[0] if "(" in body else "process_exit"
                mark_unknown(line_number, "missing_pid_prefix", operation=operation)
            continue

        live_trace_pids.add(pid)
        relation = process_starts.get(index)
        if relation is not None:
            child_pid = relation["child_pid"]
            if child_pid in live_trace_pids:
                mark_unknown(
                    relation["line"],
                    "process_creation_child_pid_conflicts_with_live_trace_pid",
                    pid=pid,
                    operation=relation["syscall"],
                )
                relation = None
            elif not relation["namespace_pid"]:
                child_records = record_indices_by_pid.get(child_pid, [])
                if bisect_right(child_records, index) == len(child_records):
                    mark_unknown(
                        relation["line"],
                        "process_creation_child_pid_not_observed",
                        pid=pid,
                        operation=relation["syscall"],
                    )
                    relation = None

        if relation is not None:
            child_pid = relation["child_pid"]
            parent_table = fd_tables.setdefault(pid, {})
            if relation["shares_fds"] is True:
                fd_tables[child_pid] = parent_table
            elif relation["shares_fds"] is False:
                fd_tables[child_pid] = _copy_fd_table(parent_table)
            else:
                fd_tables[child_pid] = _copy_fd_table(parent_table)
                uncertain_fd_sharing[child_pid] = parent_table
            successfully_closed[child_pid] = set()
            events.append(_event(
                "process_create", pid, relation["line"],
                child_pid=child_pid, syscall=relation["syscall"],
            ))

        table = fd_tables.setdefault(pid, {})
        exited = _EXITED.match(body)
        if exited is not None:
            events.append(_event("process_exit", pid, line_number, exit_code=int(exited.group(1))))
            if not any(other is table for other_pid, other in fd_tables.items() if other_pid != pid):
                table.clear()
            fd_tables.pop(pid, None)
            uncertain_fd_sharing.pop(pid, None)
            successfully_closed.pop(pid, None)
            live_trace_pids.discard(pid)
            continue
        signaled = _SIGNALED.match(body)
        if signaled is not None:
            events.append(_event("process_exit", pid, line_number, exit_code=None, signal=signaled.group(1)))
            if not any(other is table for other_pid, other in fd_tables.items() if other_pid != pid):
                table.clear()
            fd_tables.pop(pid, None)
            uncertain_fd_sharing.pop(pid, None)
            successfully_closed.pop(pid, None)
            live_trace_pids.discard(pid)
            continue

        if record.get("pending_start") is not None:
            continue
        if _RESUMED.match(body) is not None:
            continue

        call = _CALL.match(body)
        if call is None:
            if _RELEVANT.match(body):
                operation = body.split("(", 1)[0]
                mark_unknown(line_number, "malformed_relevant_syscall", pid=pid, operation=operation)
            continue

        name = call.group("name")
        if name not in {
            "execve", "execveat", "open", "openat", "openat2", "creat",
            "close", *_READ_SYSCALLS, "mmap", "mmap2", "pipe", "pipe2", "dup", "dup2", "dup3", "fcntl",
            "eventfd", "eventfd2", "close_range", "clone", "clone3", "fork", "vfork",
        }:
            continue
        try:
            args = _call_args(name, call.group("args"))
            unavailable, restart_code = _unavailable_syscall_result(call.group("result"))
            if unavailable:
                mark_unknown(
                    line_number,
                    "syscall_result_unavailable_restart" if restart_code else "syscall_result_unavailable",
                    pid=pid,
                    operation=name,
                )
                if restart_code is not None:
                    unknown[-1]["restart_code"] = restart_code
                continue
            returned, error = _return(call.group("result"))
        except (SyntaxError, ValueError):
            mark_unknown(line_number, "malformed_relevant_syscall", pid=pid, operation=name)
            continue

        if name in {"clone", "clone3", "fork", "vfork"}:
            if returned < 0:
                events.append(_event(
                    "process_create", pid, line_number,
                    child_pid=None, syscall=name, success=False, result=returned, error=error,
                ))
            elif returned > 0 and record.get("call_start_index", index) not in process_starts:
                mark_unknown(line_number, "process_creation_child_unavailable", pid=pid, operation=name)
            continue

        if name in {"pipe", "pipe2"}:
            success = returned == 0
            try:
                fds = _pipe_fds(args[0]) if success else None
            except (IndexError, ValueError):
                mark_unknown(line_number, "pipe_descriptors_unavailable", pid=pid, operation=name)
                continue
            cloexec = name == "pipe2" and len(args) > 1 and "O_CLOEXEC" in args[1]
            if fds is not None:
                for fd_number in fds:
                    table[fd_number] = {"kind": "pipe", "path": None, "cloexec": cloexec}
                    clear_closed_markers(table, fd_number)
                    invalidate_uncertain_parent_fd(pid, fd_number)
            events.append(_event(
                "pipe", pid, line_number, syscall=name,
                fds=list(fds) if fds is not None else None, success=success,
                result=returned, error=error,
            ))
            continue

        if name in {"eventfd", "eventfd2"}:
            if returned >= 0:
                table[returned] = {
                    "kind": "anon_inode", "path": None,
                    "cloexec": name == "eventfd2" and len(args) > 1 and "EFD_CLOEXEC" in args[1],
                    "readable": None,
                }
                clear_closed_markers(table, returned)
                invalidate_uncertain_parent_fd(pid, returned)
            events.append(_event(
                "fd_operation", pid, line_number, syscall=name, result=returned, error=error,
            ))
            continue

        if name in {"execve", "execveat"}:
            argv_index = 1 if name == "execve" else 2
            if len(args) <= argv_index:
                mark_unknown(line_number, "exec_argv_missing", pid=pid, operation=name)
                continue
            try:
                argv = _literal_argv(args[argv_index])
            except (SyntaxError, ValueError):
                mark_unknown(line_number, "exec_argv_unavailable_or_truncated", pid=pid, operation=name)
                continue
            events.append(_event(
                "exec", pid, line_number, argv=argv, success=returned == 0,
                result=returned, error=error,
            ))
            if returned == 0:
                uncertain_fd_sharing.pop(pid, None)
                if any(other is table for other_pid, other in fd_tables.items() if other_pid != pid):
                    table = _copy_fd_table(table)
                    fd_tables[pid] = table
                for fd_number, state in list(table.items()):
                    if state is not None and state.get("cloexec") is True:
                        del table[fd_number]
                    elif state is not None and state.get("cloexec") is None:
                        table[fd_number] = None
            continue

        if name in {"open", "openat", "openat2", "creat"}:
            open_paths = _observed_open_path(
                name, args, call.group("result"), table,
            )
            success = returned >= 0
            path = open_paths["path"]
            requested_path = open_paths["requested_path"]
            returned_path = open_paths["returned_path"]
            flags = open_paths["flags"]
            absolute_path = path if isinstance(path, str) and path.startswith("/") else None
            readable = False if name == "creat" else _readable_open(flags)
            if success:
                table[returned] = {
                    "kind": "file", "path": absolute_path,
                    "cloexec": "O_CLOEXEC" in flags, "readable": readable,
                }
                clear_closed_markers(table, returned)
                invalidate_uncertain_parent_fd(pid, returned)
            event = _event(
                "open", pid, line_number, syscall=name, fd=returned if success else None,
                path=absolute_path, requested_path=requested_path,
                returned_path=returned_path,
                path_candidates=open_paths["path_candidates"],
                dirfd_tracked_path=open_paths["dirfd_tracked_path"],
                dirfd_annotation_path=open_paths["dirfd_annotation_path"],
                dirfd_path_disagreement=open_paths["dirfd_path_disagreement"],
                dirfd_path_conflict=open_paths["dirfd_path_conflict"],
                path_disagreement=open_paths["path_disagreement"],
                path_conflict=open_paths["path_conflict"],
                path_alias_status=open_paths["path_alias_status"],
                readable=readable,
                success=success, result=returned, error=error,
            )
            if success and open_paths["path_conflict"]:
                reason = (
                    "open_dirfd_path_disagrees_with_tracked_path"
                    if open_paths["dirfd_path_conflict"]
                    else "open_request_and_fd_path_disagree"
                )
                mark_unknown(
                    line_number, reason, pid=pid, operation=name,
                )
            if success and absolute_path is None:
                event["unknown_reason"] = "open_path_not_absolute_or_unavailable"
                mark_attribution_unknown(
                    line_number, event["unknown_reason"], pid=pid, operation=name,
                )
            events.append(event)
            continue

        if name in {"mmap", "mmap2"}:
            if len(args) < 5:
                mark_unknown(line_number, "mmap_arguments_missing", pid=pid, operation=name)
                continue
            try:
                fd_number = _fd(args[4])
                readable = _readable_mapping(args[2])
                anonymous = _anonymous_mapping(args[3])
            except ValueError:
                mark_unknown(line_number, "mmap_descriptor_or_protection_unavailable", pid=pid, operation=name)
                continue
            mapping_success = not call.group("result").strip().startswith("-1")
            (
                state, path, tracked_path, path_disagreement, path_conflict,
                path_alias_status, path_candidates,
            ) = _fd_observed_path(
                args[4], fd_number, table, pid,
            )
            event = _event(
                "mmap", pid, line_number, syscall=name, fd=fd_number,
                path=path, tracked_path=tracked_path,
                path_candidates=path_candidates,
                path_disagreement=path_disagreement, path_conflict=path_conflict,
                path_alias_status=path_alias_status,
                readable=readable,
                anonymous=anonymous, success=mapping_success,
            )
            if mapping_success and path_conflict:
                mark_unknown(
                    line_number, "mmap_fd_path_disagrees_with_tracked_path", pid=pid, operation=name,
                )
            if (
                mapping_success and readable is True and path is None
                and anonymous is not True
            ):
                event["unknown_reason"] = "fd_not_opened_absolutely_in_same_pid"
                mark_attribution_unknown(
                    line_number, event["unknown_reason"], pid=pid, operation=name,
                )
            events.append(event)
            continue

        if name == "close":
            try:
                fd_number = _fd(args[0])
            except (IndexError, ValueError):
                mark_unknown(line_number, "close_fd_unavailable", pid=pid, operation=name)
                continue
            success = returned == 0
            known_closed = (
                returned == -1
                and error == "EBADF"
                and fd_number not in table
                and fd_number in successfully_closed.get(pid, set())
            )
            if success:
                table.pop(fd_number, None)
                clear_closed_markers(table, fd_number)
                successfully_closed.setdefault(pid, set()).add(fd_number)
                invalidate_uncertain_parent_fd(pid, fd_number)
            elif not known_closed:
                table[fd_number] = None
                clear_closed_markers(table, fd_number)
                invalidate_uncertain_parent_fd(pid, fd_number)
                mark_unknown(line_number, "close_result_leaves_fd_state_unknown", pid=pid, operation=name)
            events.append(_event(
                "close", pid, line_number, fd=fd_number, success=success,
                result=returned, error=error, known_closed=known_closed,
            ))
            continue

        if name in _READ_SYSCALLS:
            if len(args) < 3:
                mark_unknown(line_number, "read_arguments_missing", pid=pid, operation=name)
                continue
            if not _RAW_POINTER.fullmatch(args[1].strip()):
                mark_unknown(line_number, "read_buffer_is_not_raw_pointer", pid=pid, operation=name)
                continue
            try:
                fd_number = _fd(args[0])
                requested = _integer(args[2])
            except ValueError:
                mark_unknown(line_number, "read_fd_or_count_unavailable", pid=pid, operation=name)
                continue
            (
                state, path, tracked_path, path_disagreement, path_conflict,
                path_alias_status, path_candidates,
            ) = _fd_observed_path(
                args[0], fd_number, table, pid,
            )
            event = _event(
                "read", pid, line_number, syscall=name, fd=fd_number, count=requested,
                result=returned, bytes_read=returned if returned > 0 else None,
                path=path, tracked_path=tracked_path, path_candidates=path_candidates,
                path_disagreement=path_disagreement, path_conflict=path_conflict,
                path_alias_status=path_alias_status,
                fd_kind=state.get("kind") if state is not None else "unknown",
                error=error,
            )
            if path_conflict:
                mark_unknown(
                    line_number, "read_fd_path_disagrees_with_tracked_path", pid=pid, operation=name,
                )
            if path is None and event["fd_kind"] not in {"pipe", "anon_inode", "socket"}:
                event["unknown_reason"] = "fd_not_opened_absolutely_in_same_pid"
                if returned > 0:
                    mark_attribution_unknown(
                        line_number, event["unknown_reason"], pid=pid, operation=name,
                    )
            events.append(event)
            continue

        if name in {"dup", "dup2", "dup3", "fcntl"}:
            try:
                source_fd = _fd(args[0])
                duplicate = name in {"dup", "dup2", "dup3"} or (
                    name == "fcntl" and len(args) > 1 and args[1].startswith("F_DUPFD")
                )
                if duplicate and returned >= 0:
                    source_state = table.get(source_fd)
                    if name in {"dup2", "dup3"} and len(args) > 1:
                        target_fd = _fd(args[1])
                        if returned != target_fd:
                            mark_unknown(line_number, "dup_target_mismatch", pid=pid, operation=name)
                            continue
                    else:
                        target_fd = returned
                    table[target_fd] = dict(source_state) if source_state is not None else None
                    clear_closed_markers(table, target_fd)
                    invalidate_uncertain_parent_fd(pid, target_fd)
                elif name == "fcntl" and len(args) > 1 and args[1] == "F_SETFD":
                    state = table.get(source_fd)
                    if state is not None:
                        state["cloexec"] = "FD_CLOEXEC" in call.group("args")
                    parent_table = uncertain_fd_sharing.get(pid)
                    if parent_table is not None and source_fd in parent_table:
                        parent_state = parent_table[source_fd]
                        if parent_state is not None:
                            parent_state["cloexec"] = None
            except (IndexError, ValueError):
                mark_unknown(line_number, "descriptor_operation_unavailable", pid=pid, operation=name)
                continue
            events.append(_event("fd_operation", pid, line_number, syscall=name, result=returned, error=error))
            continue

        if name == "close_range":
            try:
                first = _integer(args[0])
                last = _integer(args[1])
                if returned == 0:
                    for fd_number in list(table):
                        if first <= fd_number <= last:
                            del table[fd_number]
                    parent_table = uncertain_fd_sharing.get(pid)
                    if parent_table is not None:
                        possible_fds = set(table) | set(parent_table)
                        for fd_number in possible_fds:
                            if first <= fd_number <= last:
                                parent_table[fd_number] = None
                else:
                    mark_unknown(line_number, "close_range_result_leaves_fd_state_unknown", pid=pid, operation=name)
            except (IndexError, ValueError):
                table.clear()
                mark_unknown(line_number, "close_range_bounds_unavailable", pid=pid, operation=name)
            events.append(_event("fd_operation", pid, line_number, syscall=name, result=returned, error=error))

    events.sort(key=lambda event: event["trace_line"])
    if unknown:
        result.update(complete=False, reason="one_or_more_trace_events_are_unknown")
    return result
