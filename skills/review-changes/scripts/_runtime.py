from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]


def load_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read valid UTF-8 JSON from {source}: {exc}") from exc


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def import_review_core() -> None:
    candidates = [SKILL_ROOT, *SKILL_ROOT.parents]
    for candidate in candidates:
        if (candidate / "review_core").is_dir():
            sys.path.insert(0, str(candidate))
            return
    raise RuntimeError(
        "review_core is missing; use the built review-changes package with its bundled review_core"
    )


def scope_unchanged(initial: Any, current: Any) -> bool:
    if not isinstance(initial, dict) or not isinstance(current, dict):
        return False
    identity_fields = ("scope", "repository", "base", "head", "merge_base")
    if any(initial.get(field) != current.get(field) for field in identity_fields):
        return False
    if initial.get("range_fingerprint") != current.get("range_fingerprint"):
        return False

    original_files = initial.get("files")
    current_files = current.get("files")
    if not isinstance(original_files, list) or not isinstance(current_files, list):
        return False

    def content_hashes_match(manifest: dict[str, Any]) -> bool:
        for group_name in ("files", "context_files"):
            entries = manifest.get(group_name, [])
            if not isinstance(entries, list):
                return False
            for entry in entries:
                if not isinstance(entry, dict):
                    return False
                for side_name in ("before", "after"):
                    side = entry.get(side_name)
                    if not isinstance(side, dict):
                        return False
                    content = side.get("content")
                    if content is not None:
                        if not isinstance(content, str):
                            return False
                        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
                        if side.get("fingerprint") != actual:
                            return False
        return True

    if not content_hashes_match(initial) or not content_hashes_match(current):
        return False

    def file_key(entry: Any) -> tuple[Any, ...] | None:
        if not isinstance(entry, dict) or entry.get("status") == "context":
            return None
        return (
            entry.get("filename"),
            entry.get("previous_filename"),
            entry.get("status"),
        )

    def side_key(side: Any) -> Any:
        if not isinstance(side, dict):
            return side
        return (
            side.get("content"),
            side.get("fingerprint"),
            side.get("unavailable_reason"),
        )

    def change_key(entry: dict[str, Any]) -> tuple[Any, ...]:
        return (
            file_key(entry),
            entry.get("language"),
            entry.get("diff"),
            side_key(entry.get("before")),
            side_key(entry.get("after")),
        )

    current_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for entry in current_files:
        key = file_key(entry)
        if key is not None:
            current_by_key[key] = entry
    original_keys = {file_key(entry) for entry in original_files}
    original_keys.discard(None)
    if original_keys != set(current_by_key):
        return False
    for entry in original_files:
        key = file_key(entry)
        if key is None or key not in current_by_key:
            return False
        if change_key(entry) != change_key(current_by_key[key]):
            return False

    if initial.get("untracked_files", []) != current.get("untracked_files", []):
        return False
    return True
