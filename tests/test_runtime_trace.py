from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from eval.runtime_trace import normalizer_identity, parse_raw_trace


def _parse(lines: list[str]) -> dict:
    return parse_raw_trace(("\n".join(lines) + "\n").encode("utf-8"))


def test_normalizes_pid_scoped_exec_open_raw_read_and_exit() -> None:
    parsed = _parse([
        '410 execve("/usr/bin/cat", ["cat", "--", "/synthetic/file"], 0x0, 0x0) = 0',
        '410 openat(AT_FDCWD, "/synthetic/file", O_RDONLY) = 3</synthetic/file>',
        "410 read(0x3, 0x7fff0000, 0x10) = 0x8",
        "410 close(3</synthetic/file>) = 0",
        "410 +++ exited with 0 +++",
    ])

    assert parsed["complete"] is True
    assert parsed["unknown"] == []
    assert parsed["raw_trace_sha256"] == hashlib.sha256(
        b'410 execve("/usr/bin/cat", ["cat", "--", "/synthetic/file"], 0x0, 0x0) = 0\n'
        b'410 openat(AT_FDCWD, "/synthetic/file", O_RDONLY) = 3</synthetic/file>\n'
        b"410 read(0x3, 0x7fff0000, 0x10) = 0x8\n"
        b"410 close(3</synthetic/file>) = 0\n"
        b"410 +++ exited with 0 +++\n"
    ).hexdigest()
    assert [event["event"] for event in parsed["events"]] == [
        "exec", "open", "read", "close", "process_exit"
    ]
    assert parsed["events"][0]["argv"] == ["cat", "--", "/synthetic/file"]
    assert parsed["events"][0]["trace_line"] == 1
    read = parsed["events"][2]
    assert {key: read[key] for key in ("pid", "fd", "count", "result", "bytes_read", "path", "trace_line")} == {
        "pid": 410,
        "fd": 3,
        "count": 16,
        "result": 8,
        "bytes_read": 8,
        "path": "/synthetic/file",
        "trace_line": 3,
    }
    assert all("call_id" not in event for event in parsed["events"])


def test_unprefixed_leader_and_inherited_fd_remain_unknown() -> None:
    unprefixed = _parse([
        'execve("/usr/bin/cat", ["cat"], 0x0, 0x0) = 0',
        "+++ exited with 0 +++",
    ])
    assert unprefixed["events"] == []
    assert [item["reason"] for item in unprefixed["unknown"]] == [
        "missing_pid_prefix", "missing_pid_prefix"
    ]

    inherited = _parse([
        '501 open("/tmp/observed-parent-file", O_RDONLY) = 3',
        "502 read(0x3, 0x7fff0000, 0x4) = 0x4",
    ])
    child_read = inherited["events"][-1]
    assert child_read["event"] == "read"
    assert child_read["pid"] == 502
    assert child_read["path"] is None
    assert child_read["unknown_reason"] == "fd_not_opened_absolutely_in_same_pid"
    assert inherited["complete"] is True
    assert inherited["unknown"] == []
    assert len(inherited["attribution_unknown"]) == 1


def test_close_and_fd_reuse_cannot_retain_the_old_path() -> None:
    parsed = _parse([
        '700 open("/synthetic/first", O_RDONLY) = 3',
        "700 close(3) = 0",
        "700 read(0x3, 0x7fff0000, 0x1) = 0x1",
        '700 openat(AT_FDCWD, "/synthetic/second", O_RDONLY) = 3</synthetic/second>',
        "700 read(0x3, 0x7fff0000, 0x2) = 0x2",
    ])
    reads = [event for event in parsed["events"] if event["event"] == "read"]
    assert reads[0]["path"] is None
    assert reads[0]["unknown_reason"] == "fd_not_opened_absolutely_in_same_pid"
    assert reads[1]["path"] == "/synthetic/second"
    assert reads[1]["count"] == 2
    assert reads[1]["result"] == 2


def test_fork_inherits_observed_descriptors_without_global_invalidation() -> None:
    parsed = _parse([
        '900 open("/synthetic/inherited", O_RDONLY) = 3',
        "900 clone(child_stack=NULL, flags=SIGCHLD) = 901",
        "900 read(0x3, 0x7fff0000, 0x1) = 0x1",
        "901 read(0x3, 0x7fff0000, 0x1) = 0x1",
        "900 +++ exited with 0 +++",
        "900 read(0x3, 0x7fff0000, 0x1) = 0x1",
    ])
    reads = [event for event in parsed["events"] if event["event"] == "read"]
    assert [event["path"] for event in reads] == [
        "/synthetic/inherited", "/synthetic/inherited", None,
    ]
    assert parsed["complete"] is True
    assert parsed["unknown"] == []
    assert len(parsed["attribution_unknown"]) == 1


def test_unrecognized_clone_preserves_inherited_paths_until_fd_table_changes() -> None:
    parsed = _parse([
        '901 open("/synthetic/inherited", O_RDONLY) = 3',
        "901 clone(child_stack=NULL) = 902",
        "902 read(0x3, 0x7fff0000, 0x1) = 0x1",
        "901 read(0x3, 0x7fff0000, 0x1) = 0x1",
    ])

    reads = [event for event in parsed["events"] if event["event"] == "read"]
    assert parsed["complete"] is True
    assert [event["path"] for event in reads] == [
        "/synthetic/inherited", "/synthetic/inherited",
    ]
    assert parsed["unknown"] == []
    assert parsed["attribution_unknown"] == []


def test_unrecognized_shared_clone_invalidates_only_descriptors_it_may_change() -> None:
    parsed = _parse([
        '903 open("/synthetic/first", O_RDONLY) = 3',
        '903 open("/synthetic/second", O_RDONLY) = 4',
        "903 clone(child_stack=NULL) = 904",
        "904 close(3) = 0",
        "903 read(0x3, 0x7fff0000, 0x1) = 0x1",
        "903 read(0x4, 0x7fff0000, 0x1) = 0x1",
    ])

    reads = [event for event in parsed["events"] if event["event"] == "read"]
    assert parsed["complete"] is True
    assert [event["path"] for event in reads] == [None, "/synthetic/second"]
    assert len(parsed["attribution_unknown"]) == 1


def test_pipe_read_and_unfinished_vfork_are_normalized() -> None:
    parsed = _parse([
        "910 pipe2([4<pipe:[123]>, 5<pipe:[123]>], O_CLOEXEC) = 0",
        "910 vfork( <unfinished ...>",
        "911 close(4<pipe:[123]>) = 0",
        '911 execve("/usr/bin/cat", ["cat"], 0x0, 0x0) = 0',
        "910 <... vfork resumed>) = 911",
        "910 read(0x4, 0x7fff0000, 0x4) = 0x4",
    ])

    assert parsed["complete"] is True
    assert parsed["unknown"] == []
    assert [event["event"] for event in parsed["events"]] == [
        "pipe", "process_create", "close", "exec", "read",
    ]
    assert parsed["events"][-1]["fd_kind"] == "pipe"
    assert parsed["events"][-1]["path"] is None
    assert parsed["attribution_unknown"] == []


def test_mmap_keeps_path_and_readability_for_resource_access_checks() -> None:
    parsed = _parse([
        '812 open("/synthetic/skill", O_RDONLY|O_CLOEXEC) = 3',
        "812 mmap(NULL, 4096, PROT_READ, MAP_PRIVATE, 3, 0) = 0x7f000000",
    ])

    mapping = parsed["events"][-1]
    assert parsed["complete"] is True
    assert mapping["event"] == "mmap"
    assert mapping["path"] == "/synthetic/skill"
    assert mapping["readable"] is True
    assert mapping["success"] is True


def test_relative_open_and_buffered_read_are_rejected_without_copying_data() -> None:
    parsed = _parse([
        '801 openat(AT_FDCWD, "relative.txt", O_RDONLY) = 3',
        "801 read(0x3, 0x7fff0000, 0x4) = 0x4",
        '801 read(0x3, "SYNTHETIC_BUFFER_MUST_NOT_ESCAPE", 0x20) = 0x20',
    ])
    assert parsed["complete"] is False
    assert [event["path"] for event in parsed["events"] if event["event"] == "read"] == [None]
    assert any(
        item["reason"] == "open_path_not_absolute_or_unavailable"
        for item in parsed["attribution_unknown"]
    )
    assert any(item["reason"] == "read_buffer_is_not_raw_pointer" for item in parsed["unknown"])
    assert "SYNTHETIC_BUFFER_MUST_NOT_ESCAPE" not in json.dumps(parsed)


def test_raw_pread64_uses_the_same_pid_fd_path_attribution() -> None:
    parsed = _parse([
        '811 open("/synthetic/library", O_RDONLY) = 3',
        "811 pread64(0x3, 0x7fff0000, 0x8, 0x0) = 0x8",
    ])

    assert parsed["complete"] is True
    assert parsed["events"][-1] == {
        "event": "read",
        "pid": 811,
        "syscall": "pread64",
        "fd": 3,
        "count": 8,
        "result": 8,
        "bytes_read": 8,
        "path": "/synthetic/library",
        "fd_kind": "file",
        "error": None,
        "trace_line": 2,
    }


def test_normalizer_identity_hashes_current_module_source() -> None:
    module_path = Path(__import__("eval.runtime_trace", fromlist=["__file__"]).__file__)
    identity = normalizer_identity()
    assert identity["name"] == "eval.runtime_trace"
    assert identity["version"] == "2"
    assert identity["source_sha256"] == hashlib.sha256(module_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("program,arguments", [("cat", ["--"]), ("head", ["-c", "1"])])
def test_real_strace_raw_read_preflight(tmp_path: Path, program: str, arguments: list[str]) -> None:
    strace = shutil.which("strace")
    executable = shutil.which(program)
    if strace is None or executable is None:
        pytest.skip("strace and the requested child utility are required")

    target = tmp_path / "synthetic-input.txt"
    target.write_bytes(b"synthetic-only-runtime-trace-payload\n" * 512)
    trace_path = tmp_path / f"{program}-strace.log"
    run = subprocess.run(
        [
            strace, "-f", "-yy", "-s", "4096",
            "-e", "trace=execve,execveat,open,openat,openat2,creat,close,read,pread64,readv,preadv,preadv2,dup,dup2,dup3,fcntl,close_range,clone,clone3,fork,vfork,exit,exit_group",
            "-e", "raw=read,pread64,readv,preadv,preadv2",
            "-o", str(trace_path), "--", executable, *arguments, str(target),
        ],
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert run.returncode == 0, run.stderr.decode("utf-8", errors="replace")
    if program == "head":
        assert run.stdout == b"s"
    else:
        assert run.stdout == target.read_bytes()

    raw = trace_path.read_bytes()
    parsed = parse_raw_trace(raw)
    assert any(
        event["event"] == "exec" and event["success"] and Path(event["argv"][0]).name == program
        for event in parsed["events"]
    )
    target_reads = [
        event for event in parsed["events"]
        if event["event"] == "read" and event.get("path") == str(target)
    ]
    assert target_reads
    assert any(event["result"] > 0 and event["count"] >= event["result"] for event in target_reads)
    trace_text = raw.decode("utf-8")
    assert re.search(
        r"\bread\(0x[0-9a-fA-F]+, 0x[0-9a-fA-F]+, 0x[0-9a-fA-F]+\)\s*=\s*0x[0-9a-fA-F]+",
        trace_text,
    )
