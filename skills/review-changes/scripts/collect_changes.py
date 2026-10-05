from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from _runtime import import_review_core, load_json, scope_unchanged, write_json


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture an immutable local Git review manifest."
    )
    parser.add_argument("--repo", required=True, help="Path to the target Git repository")
    parser.add_argument(
        "--scope", choices=("worktree", "staged", "branch"), required=True
    )
    parser.add_argument("--base", help="Base ref for --scope branch")
    parser.add_argument("--head", default="HEAD", help="Head ref for --scope branch (default: HEAD)")
    parser.add_argument("--compare-to", help="Initial manifest to verify the same scope snapshot")
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="Include non-ignored untracked text files for worktree reviews",
    )
    parser.add_argument(
        "--context",
        nargs="+",
        default=[],
        metavar="PATH",
        help="Repository-relative paths to capture as supporting context",
    )
    parser.add_argument("--output", required=True, help="Manifest JSON output path")
    args = parser.parse_args()
    if args.scope == "branch" and not args.base:
        parser.error("--scope branch requires --base")
    if args.scope != "branch" and (args.base or args.head != "HEAD"):
        parser.error("--base and --head are only valid with --scope branch")
    if args.include_untracked and args.scope != "worktree":
        parser.error("--include-untracked is only valid with --scope worktree")
    return args


def main() -> int:
    args = _arguments()
    try:
        import_review_core()
        snapshot = importlib.import_module("review_core.snapshot")
        manifest = snapshot.collect_changes(
            repository=Path(args.repo).expanduser().resolve(),
            scope=args.scope,
            base=args.base,
            head=args.head,
            include_untracked=args.include_untracked,
            context_paths=args.context,
        )
        if not isinstance(manifest, dict):
            raise ValueError("snapshot collector did not return a manifest object")
        if manifest.get("schema_version") != "2":
            raise ValueError("snapshot collector must return manifest schema_version '2'")
        if not manifest.get("content_fingerprint"):
            raise ValueError("snapshot manifest is missing content_fingerprint")
        if args.compare_to:
            initial = load_json(args.compare_to)
            if not scope_unchanged(initial, manifest):
                raise RuntimeError(
                    "the original review scope changed while context was collected; restart from a fresh manifest"
                )
        write_json(args.output, manifest)
        return 0
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"collect_changes: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
