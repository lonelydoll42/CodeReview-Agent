Review the branch change in repository from base to head and within the exact scope in reviewer-input.json. Report actionable defects only. Do not modify repository files. Return one JSON object conforming to protocol/host-output.schema.json. Use null or an empty list when a value is unknown; do not infer benchmark labels.

The repository is a presealed input copy without .git. Treat it and reviewer-input.json as authoritative. Do not run Git-dependent preparation commands or claim that a full Skill preparation workflow ran.
