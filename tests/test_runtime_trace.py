from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from eval.runtime_trace import STRACE_STRING_LIMIT, normalizer_identity, parse_raw_trace


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


def test_repeated_close_ebadf_is_known_only_after_same_pid_close_without_reopen() -> None:
    parsed = _parse([
        '705 open("/synthetic/closed", O_RDONLY) = 3',
        "705 close(3</synthetic/closed>) = 0",
        "705 close(3) = -1 EBADF (Bad file descriptor)",
        "706 close(3) = -1 EBADF (Bad file descriptor)",
        '707 open("/synthetic/reused", O_RDONLY) = 3',
        "707 close(3) = 0",
        '707 open("/synthetic/reopened", O_RDONLY) = 3',
        "707 close(3) = -1 EBADF (Bad file descriptor)",
    ])

    closes = [event for event in parsed["events"] if event["event"] == "close"]
    assert parsed["normalizer"]["version"] == "6"
    assert closes[1]["known_closed"] is True
    assert closes[2]["known_closed"] is False
    assert closes[4]["known_closed"] is False
    assert parsed["complete"] is False
    assert len(parsed["unknown"]) == 2


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


@pytest.mark.parametrize("flags", [
    "MAP_PRIVATE|MAP_ANONYMOUS",
    "MAP_PRIVATE|MAP_ANON",
    "34",
    "0x22",
])
def test_anonymous_readable_mmap_is_not_a_file_path_gap(flags: str) -> None:
    parsed = _parse([
        f"812 mmap(NULL, 4096, PROT_READ, {flags}, -1, 0) = 0x7f000000",
    ])

    mapping = parsed["events"][0]
    assert mapping["anonymous"] is True
    assert mapping["path"] is None
    assert parsed["attribution_unknown"] == []


@pytest.mark.parametrize("flags", [
    "MAP_PRIVATE|MAP_UNKNOWN|MAP_ANONYMOUS",
    "MAP_PRIVATE|MAP_SHARED|MAP_ANONYMOUS",
    "0x24",
])
def test_unproven_anonymous_flags_keep_readable_mmap_unattributed(flags: str) -> None:
    parsed = _parse([
        f"812 mmap(NULL, 4096, PROT_READ, {flags}, -1, 0) = 0x7f000000",
    ])

    mapping = parsed["events"][0]
    assert mapping["anonymous"] is None
    assert len(parsed["attribution_unknown"]) == 1


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


def test_yy_fd_paths_resolve_relative_openat_and_read_descriptors() -> None:
    parsed = _parse([
        '815 openat(AT_FDCWD</synthetic/root>, "sub/input.txt", O_RDONLY) = 3</synthetic/root/sub/input.txt>',
        '815 read(3</synthetic/root/sub/input.txt>, 0x7fff0000, 0x4) = 0x4',
        '815 pread64(3</synthetic/root/sub/input.txt>, 0x7fff0000, 0x4, 0x0) = 0x4',
    ])

    assert parsed["unknown"] == []
    assert parsed["attribution_unknown"] == []
    open_event, read_event, pread_event = parsed["events"]
    assert open_event["path"] == "/synthetic/root/sub/input.txt"
    assert read_event["path"] == "/synthetic/root/sub/input.txt"
    assert pread_event["path"] == "/synthetic/root/sub/input.txt"
    assert read_event["bytes_read"] == pread_event["bytes_read"] == 4


def test_relative_openat_uses_traced_dirfd_and_read_fd_annotations() -> None:
    parsed = _parse([
        '816 openat(AT_FDCWD</synthetic/root>, "sub", O_RDONLY|O_DIRECTORY) = 3</synthetic/root/sub>',
        '816 openat(3</synthetic/root/sub>, "input.txt", O_RDONLY) = 4',
        "816 read(4, 0x7fff0000, 0x2) = 0x2",
        '817 read(9</synthetic/unobserved>, 0x7fff0000, 0x1) = 0x1',
    ])

    opens = [event for event in parsed["events"] if event["event"] == "open"]
    reads = [event for event in parsed["events"] if event["event"] == "read"]
    assert [event["path"] for event in opens] == [
        "/synthetic/root/sub", "/synthetic/root/sub/input.txt",
    ]
    assert [event["path"] for event in reads] == [
        "/synthetic/root/sub/input.txt", "/synthetic/unobserved",
    ]
    assert parsed["attribution_unknown"] == []


def test_relative_openat_dirfd_conflict_keeps_both_path_candidates() -> None:
    pinned_root = "/slot/input/pinned-skill/review-changes"
    parsed = _parse([
        f'824 open("{pinned_root}", O_RDONLY|O_DIRECTORY) = 3<{pinned_root}>',
        '824 openat(3</synthetic/other-dir>, "SKILL.md", O_RDONLY) = 4</synthetic/other-dir/SKILL.md>',
        "824 read(4, 0x7fff0000, 0x1) = 0x1",
    ])

    opened = [event for event in parsed["events"] if event["event"] == "open"][1]
    assert opened["dirfd_tracked_path"] == pinned_root
    assert opened["dirfd_annotation_path"] == "/synthetic/other-dir"
    assert opened["dirfd_path_disagreement"] is True
    assert opened["dirfd_path_conflict"] is True
    assert opened["requested_path"] == f"{pinned_root}/SKILL.md"
    assert opened["returned_path"] == "/synthetic/other-dir/SKILL.md"
    assert opened["path_candidates"] == [
        f"{pinned_root}/SKILL.md",
        "/synthetic/other-dir/SKILL.md",
    ]
    assert opened["path_conflict"] is True
    assert parsed["complete"] is False
    assert any(
        item["reason"] == "open_dirfd_path_disagrees_with_tracked_path"
        for item in parsed["unknown"]
    )


def test_unannotated_relative_openat_and_read_remain_unattributed() -> None:
    parsed = _parse([
        '818 openat(AT_FDCWD, "relative.txt", O_RDONLY) = 3',
        "818 read(3, 0x7fff0000, 0x1) = 0x1",
        "818 +++ exited with 0 +++",
    ])

    opens = [event for event in parsed["events"] if event["event"] == "open"]
    reads = [event for event in parsed["events"] if event["event"] == "read"]
    assert opens[0]["path"] is None
    assert reads[0]["path"] is None
    assert len(parsed["attribution_unknown"]) == 2
    assert parsed["events"][-1]["exit_code"] == 0


def test_open_fd_annotation_does_not_erase_normalized_request_path() -> None:
    parsed = _parse([
        '820 openat(AT_FDCWD, "/slot/input/pinned-skill/review-changes/SKILL.md", O_RDONLY) = 3</other-target>',
        '820 read(3</other-target>, 0x7fff0000, 0x1) = 0x1',
    ])

    opened, read = parsed["events"]
    assert opened["requested_path"] == "/slot/input/pinned-skill/review-changes/SKILL.md"
    assert opened["returned_path"] == opened["path"] == "/other-target"
    assert opened["path_disagreement"] is True
    assert opened["path_conflict"] is True
    assert read["tracked_path"] == read["path"] == "/other-target"
    assert any(item["reason"] == "open_request_and_fd_path_disagree" for item in parsed["unknown"])
    assert parsed["complete"] is False


@pytest.mark.parametrize(("requested", "observed"), [
    (
        "/usr/lib/x86_64-linux-gnu/libc.so.6",
        "/usr/lib/x86_64-linux-gnu/libc.so.6.0.0",
    ),
])
def test_known_open_path_aliases_do_not_create_trace_conflicts(
    requested: str,
    observed: str,
) -> None:
    parsed = _parse([
        f'822 open("{requested}", O_RDONLY) = 3<{observed}>',
        f'822 read(3<{observed}>, 0x7fff0000, 0x1) = 0x1',
    ])

    opened, read = parsed["events"]
    assert opened["requested_path"] == requested
    assert opened["returned_path"] == observed
    assert opened["path_disagreement"] is True
    assert opened["path_conflict"] is False
    assert opened["path_alias_status"] == "known"
    assert read["tracked_path"] == read["path"] == observed
    assert read["path_conflict"] is False
    assert parsed["unknown"] == []
    assert parsed["attribution_unknown"] == []


def test_proc_self_numeric_pid_annotation_is_preserved_as_unproven() -> None:
    parsed = _parse([
        '825 open("/proc/self/maps", O_RDONLY) = 3</proc/3/maps>',
        '826 open("/proc/self/maps", O_RDONLY) = 3</proc/3/maps>',
    ])

    opens = [event for event in parsed["events"] if event["event"] == "open"]
    assert len(opens) == 2
    assert [event["pid"] for event in opens] == [825, 826]
    assert all(event["requested_path"] == "/proc/self/maps" for event in opens)
    assert all(event["returned_path"] == "/proc/3/maps" for event in opens)
    assert all(event["path_alias_status"] == "unproven" for event in opens)
    assert all(event["path_conflict"] is False for event in opens)
    assert parsed["complete"] is True
    assert parsed["unknown"] == []


def test_eventfd_read_is_a_known_non_file_descriptor() -> None:
    parsed = _parse([
        "823 eventfd2(0, EFD_CLOEXEC) = 4",
        "823 read(4<anon_inode:[eventfd]>, 0x7fff0000, 0x8) = 0x8",
    ])

    read = parsed["events"][-1]
    assert read["event"] == "read"
    assert read["fd_kind"] == "anon_inode"
    assert read["path"] is None
    assert "unknown_reason" not in read
    assert parsed["attribution_unknown"] == []


def test_read_fd_annotation_does_not_erase_tracked_path() -> None:
    parsed = _parse([
        '821 open("/slot/input/pinned-skill/review-changes/SKILL.md", O_RDONLY) = 3',
        "821 read(3</other-target>, 0x7fff0000, 0x1) = 0x1",
    ])

    read = parsed["events"][-1]
    assert read["tracked_path"] == "/slot/input/pinned-skill/review-changes/SKILL.md"
    assert read["path"] == "/other-target"
    assert read["path_disagreement"] is True
    assert any(item["reason"] == "read_fd_path_disagrees_with_tracked_path" for item in parsed["unknown"])
    assert parsed["complete"] is False


def test_truncated_execve_argv_stays_unknown_after_zero_exit() -> None:
    parsed = _parse([
        '819 execve("/usr/bin/true", ["/usr/bin/true", "SYNTHETIC_ARG", ...], 0x0 /* 1 var */) = 0',
        "819 +++ exited with 0 +++",
    ])

    assert parsed["complete"] is False
    assert parsed["unknown"] == [{
        "trace_line": 1,
        "reason": "exec_argv_unavailable_or_truncated",
        "pid": 819,
        "operation": "execve",
    }]
    assert not any(event["event"] == "exec" for event in parsed["events"])
    assert parsed["events"][-1]["exit_code"] == 0


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
        "tracked_path": "/synthetic/library",
        "path_candidates": ["/synthetic/library"],
        "path_disagreement": False,
        "path_conflict": False,
        "path_alias_status": "same",
        "fd_kind": "file",
        "error": None,
        "trace_line": 2,
    }


@pytest.mark.parametrize(
    "restart_code,description",
    [
        ("ERESTARTSYS", "To be restarted if SA_RESTART is set"),
        ("ERESTARTNOHAND", "To be restarted if no handler"),
        ("ERESTARTNOINTR", "To be restarted"),
        ("ERESTART_RESTARTBLOCK", "To be restarted"),
    ],
)
def test_unavailable_restart_result_is_unknown_and_keeps_trace_incomplete(
    restart_code: str,
    description: str,
) -> None:
    parsed = _parse([
        f"811 pread64(0x3, 0x7fff0000, 0x8, 0x0) = ? {restart_code} ({description})",
        "811 +++ exited with 1 +++",
    ])

    assert parsed["complete"] is False
    assert parsed["unknown"] == [{
        "trace_line": 1,
        "reason": "syscall_result_unavailable_restart",
        "pid": 811,
        "operation": "pread64",
        "restart_code": restart_code,
    }]
    assert not any(event.get("syscall") == "pread64" for event in parsed["events"])
    assert parsed["events"][-1] == {
        "event": "process_exit",
        "pid": 811,
        "exit_code": 1,
        "trace_line": 2,
    }


def test_completed_errno_return_is_not_an_unavailable_restart() -> None:
    parsed = _parse([
        '812 open("/synthetic/library", O_RDONLY) = 3',
        "812 pread64(0x3, 0x7fff0000, 0x8, 0x0) = -1 EINTR (Interrupted system call)",
        "812 +++ exited with 0 +++",
    ])

    assert parsed["complete"] is True
    assert parsed["unknown"] == []
    assert parsed["events"][1]["event"] == "read"
    assert parsed["events"][1]["result"] == -1
    assert parsed["events"][1]["error"] == "EINTR"
    assert parsed["events"][1]["bytes_read"] is None


def test_bare_unavailable_read_result_is_explicitly_unknown_after_pid_exit() -> None:
    parsed = _parse([
        "813 pread64(0x3, 0x7fff0000, 0x8, 0x0) = ?",
        "813 +++ exited with 1 +++",
    ])

    assert parsed["complete"] is False
    assert parsed["unknown"] == [{
        "trace_line": 1,
        "reason": "syscall_result_unavailable",
        "pid": 813,
        "operation": "pread64",
    }]
    assert not any(event.get("syscall") == "pread64" for event in parsed["events"])
    assert parsed["events"][-1] == {
        "event": "process_exit",
        "pid": 813,
        "exit_code": 1,
        "trace_line": 2,
    }


def test_malformed_relevant_line_remains_malformed_and_incomplete() -> None:
    parsed = _parse([
        "814 pread64(0x3, 0x7fff0000, 0x8, 0x0) ?",
        "814 +++ exited with 1 +++",
    ])

    assert parsed["complete"] is False
    assert parsed["unknown"] == [{
        "trace_line": 1,
        "reason": "malformed_relevant_syscall",
        "pid": 814,
        "operation": "pread64",
    }]
    assert not any(event.get("syscall") == "pread64" for event in parsed["events"])


def test_normalizer_identity_hashes_current_module_source() -> None:
    module_path = Path(__import__("eval.runtime_trace", fromlist=["__file__"]).__file__)
    identity = normalizer_identity()
    assert identity["name"] == "eval.runtime_trace"
    assert identity["version"] == "6"
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
            strace, "-f", "-yy", "-s", str(STRACE_STRING_LIMIT),
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


def test_real_strace_finite_limit_keeps_long_argv_and_abbreviates_environment(
    tmp_path: Path,
) -> None:
    strace = shutil.which("strace")
    executable = shutil.which("true")
    if strace is None or executable is None:
        pytest.skip("strace and true are required")

    long_argument = "SYNTHETIC_ARG_" + "x" * 50000
    trace_path = tmp_path / "long-argv-strace.log"
    run = subprocess.run(
        [
            strace, "-f", "-yy", "-s", str(STRACE_STRING_LIMIT),
            "-e", "trace=execve", "-o", str(trace_path), "--", executable, long_argument,
        ],
        env={"PATH": os.defpath, "SYNTHETIC_ENV_SENTINEL": "SYNTHETIC_SECRET_MARKER"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    raw = trace_path.read_bytes()
    parsed = parse_raw_trace(raw)

    assert run.returncode == 0
    assert parsed["complete"] is True
    assert b"SYNTHETIC_SECRET_MARKER" not in raw
    traced = [
        event for event in parsed["events"]
        if event["event"] == "exec" and event["success"] and Path(event["argv"][0]).name == "true"
    ]
    assert len(traced) == 1
    assert len(traced[0]["argv"][-1]) == len(long_argument)


def test_real_strace_argv_over_finite_limit_stays_incomplete(tmp_path: Path) -> None:
    strace = shutil.which("strace")
    executable = shutil.which("true")
    if strace is None or executable is None:
        pytest.skip("strace and true are required")

    long_argument = "SYNTHETIC_ARG_" + "x" * (STRACE_STRING_LIMIT + 4096)
    trace_path = tmp_path / "over-limit-argv-strace.log"
    run = subprocess.run(
        [
            strace, "-f", "-yy", "-s", str(STRACE_STRING_LIMIT),
            "-e", "trace=execve", "-o", str(trace_path), "--", executable, long_argument,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    raw = trace_path.read_bytes()
    parsed = parse_raw_trace(raw)

    assert run.returncode == 0
    assert parsed["complete"] is False
    assert any(item["reason"] == "exec_argv_unavailable_or_truncated" for item in parsed["unknown"])
    assert parsed["events"][-1]["event"] == "process_exit"
    assert parsed["events"][-1]["exit_code"] == 0
