from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from _runtime import import_review_core


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, resume, finalize, and recheck a snapshot-bound local review session."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Capture a review snapshot and uncovered draft")
    start.add_argument("--repo", required=True, help="Path to the target Git worktree")
    start.add_argument("--scope", choices=("worktree", "staged", "branch"), default="worktree")
    start.add_argument("--base", help="Base ref for branch scope")
    start.add_argument("--head", default="HEAD", help="Head ref for branch scope")
    start.add_argument("--include-untracked", action="store_true")
    start.add_argument("--context", nargs="+", default=[], metavar="PATH")
    destination = start.add_mutually_exclusive_group()
    destination.add_argument("--session-dir", help="Exact new session directory")
    destination.add_argument("--session-root", help="Parent for a uniquely named session")
    start.add_argument("--previous-session", help="Link a prior finalized review session")

    context = commands.add_parser("context", help="Capture supporting paths into the final snapshot")
    context.add_argument("--session", required=True)
    context.add_argument("--context", nargs="+", required=True, metavar="PATH")

    resume = commands.add_parser("resume", help="Verify and reopen an existing session")
    resume.add_argument("--session", required=True)
    resume.add_argument("--repo", help="Expected worktree path; defaults to its recorded path")

    finalize = commands.add_parser("finalize", help="Validate a host result and render its report")
    finalize.add_argument("--session", required=True)
    finalize.add_argument("--result", required=True, help="Host semantic result JSON")

    recheck = commands.add_parser("recheck", help="Recheck a linked prior review")
    recheck.add_argument("--session", required=True)
    recheck.add_argument("--current-result", help="Current host semantic result JSON")
    return parser


def _show(record: dict[str, object], *reference_names: str) -> None:
    session_dir = Path(str(record["session_directory"]))
    print(f"session: {session_dir}")
    print(f"workflow: {record.get('workflow_status')}")
    print(f"review coverage: {record.get('review_coverage_status')}")
    refs = record.get("references")
    if isinstance(refs, dict):
        for name in reference_names:
            value = refs.get(name)
            if isinstance(value, str):
                print(f"{name}: {session_dir / value}")
    fingerprints = record.get("fingerprints")
    if isinstance(fingerprints, dict):
        print(f"snapshot: {fingerprints.get('final')}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import_review_core()
        session = importlib.import_module("review_core.session")
        if args.command == "start":
            if args.scope == "branch" and not args.base:
                raise ValueError("--scope branch requires --base")
            if args.scope != "branch" and (args.base or args.head != "HEAD"):
                raise ValueError("--base and non-default --head are only valid with --scope branch")
            if args.include_untracked and args.scope != "worktree":
                raise ValueError("--include-untracked is only valid with --scope worktree")
            record = session.create_session(
                args.repo,
                args.scope,
                base=args.base,
                head=args.head,
                include_untracked=args.include_untracked,
                context_paths=args.context,
                session_directory=args.session_dir,
                session_root=args.session_root,
                previous_session=args.previous_session,
            )
            _show(record, "initial_manifest", "manifest", "draft_result")
        elif args.command == "context":
            record = session.add_context(args.session, args.context)
            _show(record, "manifest", "draft_result")
        elif args.command == "resume":
            record = session.resume_session(args.session, repository=args.repo)
            _show(record, "manifest", "draft_result", "validated_result", "report")
        elif args.command == "finalize":
            record = session.finalize_session(args.session, args.result)
            _show(record, "manifest", "validated_result", "report")
        else:
            record = session.recheck_session(args.session, args.current_result)
            _show(record, "manifest", "validated_result", "report", "recheck_result", "recheck_report")
        return 0
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"review_changes: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
