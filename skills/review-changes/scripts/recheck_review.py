from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from _runtime import import_review_core, load_json, write_json


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Associate a prior review with a new snapshot.")
    parser.add_argument("--previous-manifest", required=True)
    parser.add_argument("--previous-result", required=True)
    parser.add_argument("--current-manifest", required=True)
    parser.add_argument("--current-result", help="Validated semantic result for the new snapshot")
    parser.add_argument("--output", required=True, help="Structured recheck JSON output")
    parser.add_argument("--markdown-output", required=True, help="Markdown recheck report output")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        import_review_core()
        contract = importlib.import_module("review_core")
        previous_manifest = load_json(args.previous_manifest)
        previous_result = load_json(args.previous_result)
        current_manifest = load_json(args.current_manifest)
        current_result = load_json(args.current_result) if args.current_result else None
        recheck = contract.recheck_review(
            previous_result,
            previous_manifest,
            current_manifest,
            current_result,
        )
        write_json(args.output, recheck)
        markdown = contract.render_recheck(recheck)
        output = Path(args.markdown_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
        return 0
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"recheck_review: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
