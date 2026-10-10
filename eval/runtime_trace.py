"""Conservatively normalize syscall events from raw strace output.

Capture with ``strace -f -yy -e raw=read,pread64,readv,preadv,preadv2``. Read
buffers must remain raw pointers; decoded buffer text is rejected so the trace
cannot carry file data.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import Any


NORMALIZER_NAME = "eval.runtime_trace"
NORMALIZER_VERSION = "2"
NORMALIZER_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

_PID_PREFIX = re.compile(r"^(?:\[pid\s+(?P<bracketed>\d+)\]|(?P<plain>\d+))\s+(?P<body>.*)$")
_CALL = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\((?P<args>.*)\)\s+=\s+(?P<result>.*)$")
_NUMBER = r"-?(?:0x[0-9a-fA-F]+|\d+)"
_RETURN = re.compile(rf"^\s*({_NUMBER})\b(?:\s+([A-Z][A-Z0-9_]+))?")
_FD = re.compile(rf"^\s*({_NUMBER})\b")
_READ_SYSCALLS = {"read", "pread64", "readv", "preadv", "preadv2"}
_RELEVANT = re.compile(
    r"^(?:execve(?:at)?|open(?:at2?|at)?|creat|close|read|pread64|readv|preadv2?|mmap2?|pipe2?|dup2?|dup3|fcntl|close_range|clone3?|fork|vfork)\("
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

    def invalidate_uncertain_parent_fd(pid: int, fd_number: int) -> None:
        parent_table = uncertain_fd_sharing.get(pid)
        if parent_table is not None:
            parent_table[fd_number] = None

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
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        prefix = _PID_PREFIX.match(line)
        if prefix is None:
            records.append({"line": line_number, "body": line, "pid": None})
            continue
        pid = int(prefix.group("bracketed") or prefix.group("plain"))
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
            call_args = _split_args(call.group("args"))
            child_pid, error = _return(call.group("result"))
        except (SyntaxError, ValueError):
            continue
        if error is None and child_pid > 0:
            start_index = record.get("call_start_index", index)
            process_starts[start_index] = {
                "parent_pid": pid,
                "child_pid": child_pid,
                "syscall": call.group("name"),
                "shares_fds": _clone_shares_fds(call.group("name"), call_args),
                "line": record.get("call_start_line", record["line"]),
            }
    for pid, pending in pending_calls.items():
        mark_unknown(
            pending["line"], "unfinished_syscall_not_normalized",
            pid=pid, operation=pending["name"],
        )

    for index, record in enumerate(records):
        pid = record["pid"]
        line_number = record["line"]
        body = record["body"]
        if pid is None:
            if body.startswith("+++") or _RELEVANT.match(body):
                operation = body.split("(", 1)[0] if "(" in body else "process_exit"
                mark_unknown(line_number, "missing_pid_prefix", operation=operation)
            continue

        relation = process_starts.get(index)
        if relation is not None:
            parent_table = fd_tables.setdefault(pid, {})
            if relation["shares_fds"] is True:
                fd_tables[relation["child_pid"]] = parent_table
            elif relation["shares_fds"] is False:
                fd_tables[relation["child_pid"]] = _copy_fd_table(parent_table)
            else:
                fd_tables[relation["child_pid"]] = _copy_fd_table(parent_table)
                uncertain_fd_sharing[relation["child_pid"]] = parent_table
            events.append(_event(
                "process_create", pid, relation["line"],
                child_pid=relation["child_pid"], syscall=relation["syscall"],
            ))

        table = fd_tables.setdefault(pid, {})
        exited = _EXITED.match(body)
        if exited is not None:
            events.append(_event("process_exit", pid, line_number, exit_code=int(exited.group(1))))
            if not any(other is table for other_pid, other in fd_tables.items() if other_pid != pid):
                table.clear()
            fd_tables.pop(pid, None)
            uncertain_fd_sharing.pop(pid, None)
            continue
        signaled = _SIGNALED.match(body)
        if signaled is not None:
            events.append(_event("process_exit", pid, line_number, exit_code=None, signal=signaled.group(1)))
            if not any(other is table for other_pid, other in fd_tables.items() if other_pid != pid):
                table.clear()
            fd_tables.pop(pid, None)
            uncertain_fd_sharing.pop(pid, None)
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
            "close_range", "clone", "clone3", "fork", "vfork",
        }:
            continue
        try:
            args = _split_args(call.group("args"))
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
                    invalidate_uncertain_parent_fd(pid, fd_number)
            events.append(_event(
                "pipe", pid, line_number, syscall=name,
                fds=list(fds) if fds is not None else None, success=success,
                result=returned, error=error,
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
            try:
                path, flags = _open_path_and_flags(name, args)
            except (SyntaxError, ValueError):
                path, flags = None, ""
            success = returned >= 0
            absolute_path = path if isinstance(path, str) and path.startswith("/") else None
            readable = False if name == "creat" else _readable_open(flags)
            if success:
                table[returned] = {
                    "kind": "file", "path": absolute_path,
                    "cloexec": "O_CLOEXEC" in flags, "readable": readable,
                }
                invalidate_uncertain_parent_fd(pid, returned)
            event = _event(
                "open", pid, line_number, syscall=name, fd=returned if success else None,
                path=absolute_path, readable=readable,
                success=success, result=returned, error=error,
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
            except ValueError:
                mark_unknown(line_number, "mmap_descriptor_or_protection_unavailable", pid=pid, operation=name)
                continue
            mapping_success = not call.group("result").strip().startswith("-1")
            state = table.get(fd_number) if fd_number >= 0 else None
            path = state.get("path") if state is not None else None
            event = _event(
                "mmap", pid, line_number, syscall=name, fd=fd_number,
                path=path, readable=readable, success=mapping_success,
            )
            if mapping_success and readable is True and fd_number >= 0 and path is None:
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
            if success:
                table.pop(fd_number, None)
                invalidate_uncertain_parent_fd(pid, fd_number)
            else:
                table[fd_number] = None
                invalidate_uncertain_parent_fd(pid, fd_number)
                mark_unknown(line_number, "close_result_leaves_fd_state_unknown", pid=pid, operation=name)
            events.append(_event(
                "close", pid, line_number, fd=fd_number, success=success,
                result=returned, error=error,
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
            state = table.get(fd_number)
            path = state.get("path") if state is not None else None
            event = _event(
                "read", pid, line_number, syscall=name, fd=fd_number, count=requested,
                result=returned, bytes_read=returned if returned > 0 else None,
                path=path, fd_kind=state.get("kind") if state is not None else "unknown",
                error=error,
            )
            if path is None and event["fd_kind"] != "pipe":
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
