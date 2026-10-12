Review the branch change in repository from base to head and within the exact scope in reviewer-input.json. Report actionable defects only. Do not modify repository files. Return one JSON object conforming to protocol/host-output.schema.json. Use null or an empty list when a value is unknown; do not infer benchmark labels.

The repository is a presealed input copy without .git. Treat it and reviewer-input.json as authoritative. Do not run Git-dependent preparation commands or claim that a full Skill preparation workflow ran.

If you record results from `run_static.py`, pass its complete JSON output through `python3 -S protocol/project_tool_runs.py --input <path>` and copy the resulting array into `tool_runs`. The projector preserves each run's optional `note` and every finding metadata value as canonical `metadata_json`; it fails on unsupported producer fields. Keep the original `run_static.py` output unchanged.
