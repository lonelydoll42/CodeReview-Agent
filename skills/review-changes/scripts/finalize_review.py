from __future__ import annotations

import argparse
import importlib
import sys

from _runtime import import_review_core, load_json, write_json


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and render a review result.")
    parser.add_argument("--input", required=True, help="Captured manifest JSON")
    parser.add_argument("--result", required=True, help="Structured review result JSON")
    parser.add_argument("--output", required=True, help="Markdown report output path")
    parser.add_argument(
        "--validated-output",
        help="Optional path to save the normalized, validated result JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        manifest = load_json(args.input)
        result = load_json(args.result)
        if not isinstance(manifest, dict) or not isinstance(result, dict):
            raise ValueError("manifest and result must both be JSON objects")
        if manifest.get("schema_version") != "2":
            raise ValueError("manifest must have schema_version '2'")
        import_review_core()
        contract = importlib.import_module("review_core")
        validated = contract.validate_review(result, manifest)
        if not isinstance(validated, dict):
            raise ValueError("validate_review did not return a result object")
        report = contract.render_review(validated)
        if not isinstance(report, str):
            raise ValueError("render_review did not return Markdown text")
        from pathlib import Path

        output = Path(args.output)
        validated_output = Path(args.validated_output) if args.validated_output else None
        if validated_output is not None and output.resolve() == validated_output.resolve():
            raise ValueError("--validated-output and --output must be different paths")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        if validated_output is not None:
            write_json(validated_output, validated)
        return 0
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"finalize_review: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
