"""Small, line-bounded context for changes with no added lines."""
from __future__ import annotations

import re

from agents.base import FileDiff

_HUNK_HEADER = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")
DELETION_CHUNK_SIZE = 40


def deletion_anchors(file_diff: FileDiff) -> list[tuple[int, str, int]]:
    """Map removed old-side lines to nearby surviving head-side lines."""
    source_lines = file_diff.full_source.splitlines() if file_diff.full_source is not None else []
    max_line = max(len(source_lines), 1)
    anchors: list[tuple[int, str, int]] = []
    old_line = new_line = 0
    in_hunk = False
    for line in file_diff.raw_diff.splitlines():
        header = _HUNK_HEADER.match(line)
        if header:
            old_line = int(header.group("old")) - 1
            new_line = int(header.group("new")) - 1
            in_hunk = True
        elif not in_hunk or line.startswith("\\"):
            continue
        elif line.startswith("-"):
            old_line += 1
            anchors.append((old_line, line[1:], min(max(new_line + 1, 1), max_line)))
        elif line.startswith("+"):
            new_line += 1
        else:
            old_line += 1
            new_line += 1

    if len(anchors) != len(file_diff.removed_lines):
        return [
            (old, text, min(max(old, 1), max_line))
            for old, text in file_diff.removed_lines
        ]
    return anchors


def deletion_chunks(file_diff: FileDiff) -> list[list[tuple[int, str, int]]]:
    anchors = deletion_anchors(file_diff)
    return [
        anchors[index:index + DELETION_CHUNK_SIZE]
        for index in range(0, len(anchors), DELETION_CHUNK_SIZE)
    ]


def deletion_context(file_diff: FileDiff, chunk: list[tuple[int, str, int]]) -> str:
    """Show removed code and bounded head-side context around its anchors."""
    removed = "\n".join(
        f"old {old:4d} -> head {head:4d} | -{text}"
        for old, text, head in chunk
    )
    source = file_diff.full_source.splitlines() if file_diff.full_source is not None else []
    context_lines = sorted({
        line
        for _, _, head in chunk
        for line in range(max(1, head - 4), min(len(source), head + 4) + 1)
    })
    context = "\n".join(f"head {line:4d} | {source[line - 1]}" for line in context_lines)
    return f"Removed lines (old coordinates and suggested head anchors):\n{removed}\n\nCurrent source near the removal:\n{context or '(empty file)'}"
