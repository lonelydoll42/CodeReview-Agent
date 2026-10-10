# Common Host Output Format

Use the case-level `reviewer-input.json` at the path supplied by the benchmark runner as the authoritative case, snapshot, and scope input. Keep the response data in `host-output.json` using this JSON shape. This file defines output fields only; it does not prescribe a review method.

```json
{
  "schema_version": 1,
  "case_id": "case0001",
  "condition": "same_host_direct",
  "run_status": "completed",
  "review_status": "completed",
  "scope": {
    "base": "<base from reviewer-input.json>",
    "head": "<head from reviewer-input.json>",
    "paths": ["<reviewed path>"]
  },
  "read_paths": ["<path the host reports reading>"],
  "uncovered_paths": [],
  "context_omissions": [],
  "failure_reason": null,
  "findings": [
    {
      "finding_id": "finding-1",
      "category": "<category>",
      "root_cause": "<specific cause>",
      "trigger": "<input or state that activates it>",
      "impact": "<concrete consequence>",
      "introduced_by_change": true,
      "change_reason": "<why this change introduces the issue>",
      "evidence": [
        {
          "path": "<repository-relative path>",
          "side": "after",
          "line_start": 1,
          "line_end": 1,
          "basis": "<specific code evidence>"
        }
      ]
    }
  ],
  "host": {
    "name": null,
    "version": null,
    "model": null,
    "configuration_fingerprint": null
  },
  "environment": {"os": null, "architecture": null},
  "model_usage": {
    "input_tokens": null,
    "output_tokens": null,
    "cost": null,
    "currency": null
  },
  "manual_interventions": null,
  "duration_seconds": null,
  "started_at": null,
  "finished_at": null,
  "tool_runs": []
}
```

Allowed `run_status` values are `completed`, `failed`, and `incomplete`. Allowed `review_status` values are `completed`, `partial`, `uncovered`, and `failed`. Use `null` when a value is unknown or unreported. An empty `findings` array means no findings were reported; it does not mean the review was complete. Host-reported `read_paths` are retained as claims and do not independently verify file reads.

Store an optional `recheck.json` separately, with the script's `recheck_status` and findings intact. Do not convert an omitted or unrun recheck into an empty successful result.
