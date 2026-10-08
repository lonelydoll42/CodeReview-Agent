# Result and Report

The machine result must pass `review_core.validate_review(result, manifest)` before it is rendered. Follow the validator's exact field names and allowed values. Use `schema_version: "2"`, the final manifest's `content_fingerprint` as `input_fingerprint`, `rules_version`, `review_status`, `findings`, `coverage`, `tool_runs`, `limits`, and `measurements`. Set unknown host/model identity, token count, and cost to `null`.

A confirmed finding needs:

- a stable identifier, category, and severity;
- a concise root cause, concrete trigger, practical impact, and repair direction;
- `confirmed` validation status and evidence tied to the manifest;
- one or more evidence locations with file, `before` or `after` side, valid line numbers, snapshot fingerprint, and a short explanation. Unchanged supporting files are listed under `manifest.context_files` and still use the captured `before`/`after` side.

Keep tool findings separate from confirmed semantic findings. Preserve `missing`, `failed`, and `completed` tool states, including useful failure codes, rules version, `covered_files`, and `uncovered_files`. `completed` means the scanner invocation succeeded; it does not mean every changed language or file was covered. Do not convert an unavailable tool into a successful empty scan. Per-file `static_status` must use a validator-supported value such as `completed`, `missing`, `failed`, `unsupported`, `unavailable`, or `not_run`.

Use the result's review status to distinguish a completed review from partial, uncovered, or failed work. An empty findings list is not sufficient to infer completion. Host or model identity, token counts, and cost must be null/unknown when unavailable.

The final Markdown is produced by `review_core.render_review(result)`. `finalize_review.py --validated-output` saves the normalized result; use that file as the machine record because validation can downgrade a claimed `completed` status when coverage is incomplete.

## Complete Result Example

This schema-version-2 example describes a confirmed authorization issue in `src/permissions.py` and supporting evidence in captured context `tests/test_permissions.py`. Replace the input fingerprint and evidence snapshot hashes with the exact values from the final manifest. Keep paths, sides, and line numbers aligned with its captured contents.

```json
{
  "schema_version": "2",
  "input_fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "rules_version": "0de666706419c03465306ae07176f64308d90b1ad626356fa5d99433a6169af6",
  "review_status": "completed",
  "findings": [
    {
      "finding_id": "authorization-owner-check",
      "category": "authorization_bypass",
      "severity": "HIGH",
      "root_cause": "The changed permission helper returns true without checking ownership or administrator role.",
      "trigger": "A non-admin user requests a record owned by another user.",
      "impact": "The caller can read a record outside their authorization scope.",
      "change_reason": "This branch change replaced the prior owner-or-admin predicate with an unconditional allow.",
      "suggestion": "Restore the owner-or-admin check and add a regression test for an unrelated user.",
      "validation_status": "confirmed",
      "evidence": [
        {
          "file": "src/permissions.py",
          "side": "after",
          "line_start": 2,
          "line_end": 2,
          "snapshot_fingerprint": "ddbd1d018b05548db2abecb017bf83207c831d47da11daa74989cc2bd562053a",
          "description": "The changed implementation unconditionally authorizes the request."
        },
        {
          "file": "tests/test_permissions.py",
          "side": "after",
          "line_start": 2,
          "line_end": 2,
          "snapshot_fingerprint": "2bab92f00cbb3e696cc269397c902c5777dbd11ed70a238070d54aefa2e83d2b",
          "description": "The captured test contract expects an unrelated user to be denied."
        }
      ],
      "sources": ["host-semantic-review"]
    }
  ],
  "coverage": [
    {
      "file": "src/permissions.py",
      "semantic_status": "completed",
      "static_status": "missing",
      "reason": "Semgrep was not installed; semantic review completed.",
      "context_gaps": []
    }
  ],
  "tool_runs": [
    {
      "tool": "semgrep",
      "version": null,
      "rules_version": "0de666706419c03465306ae07176f64308d90b1ad626356fa5d99433a6169af6",
      "status": "missing",
      "duration_ms": 0,
      "files_scanned": [],
      "covered_files": [],
      "uncovered_files": [
        {"file": "src/permissions.py", "reason": "semgrep_not_installed"}
      ],
      "findings": [],
      "error_code": "semgrep_not_installed",
      "error": "Semgrep is not installed; semantic review can continue."
    }
  ],
  "limits": {
    "max_file_bytes": 1048576,
    "context_gaps": []
  },
  "measurements": {
    "host": null,
    "model": null,
    "tokens": null,
    "cost": null
  }
}
```

If any selected file is semantically partial, failed, or uncovered, record that per-file status and let validation derive the aggregate status. Do not copy the example's `completed` value over incomplete work.

## Recheck output

`recheck_review.py` produces a separate result with `recheck_status`, the previous and current input fingerprints, one `findings` entry per prior finding, `new_findings`, and `coverage_gaps`. Each prior entry is `resolved`, `persisting`, or `unverified` and includes a reason. Matching first reserves all stable finding IDs. The remaining findings use exact equality on `category`, `root_cause`, `trigger`, and `impact` after case and whitespace normalization; captured evidence paths may disambiguate candidates. A current finding can be associated only once, and ambiguity remains unverified. This deterministic fallback is normalized exact text matching, not fuzzy semantic similarity.

The host may provide an explicit `recheck_decisions` entry in the current semantic result. `resolved` requires valid evidence tied to the current captured manifest, a reason explaining how the trigger path changed, and an `after` citation to a prior evidence path with a detectable source change relative to the prior snapshot. The helper compares captured source contents exactly, including whitespace. Keep every prior evidence path accessible in current snapshots, and cite every available prior evidence path using current `after` evidence. A deleted prior path must have its current `before` snapshot; also cite the remaining caller on its current `after` side when it was part of the prior evidence. All related changed files must have completed semantic coverage without context gaps. A same-ID current `confirmed` or `needs_confirmation` finding conflicts with a resolved decision and cannot be marked resolved. Missing or invalid evidence, a failed review, or incomplete relevant coverage leaves the prior finding unverified. Unrelated incomplete files may keep the aggregate `recheck_status` at `partial` while a finding with complete relevant coverage is resolved. The host is responsible for semantic verification; the script checks captured evidence and decision/scope constraints but does not prove the explanation is true.

Example decision structure:

```json
{
  "previous_finding_id": "finding-null-lookup",
  "status": "resolved",
  "reason": "The caller now returns the not-found response before dereferencing the optional record, so the prior null-trigger path cannot reach this access.",
  "evidence": [
    {
      "file": "src/records.py",
      "side": "after",
      "line_start": 24,
      "line_end": 27,
      "snapshot_fingerprint": "<copy the exact after.fingerprint for src/records.py from the current manifest>",
      "description": "The repaired branch checks for a missing record before dereferencing it."
    },
    {
      "file": "src/routes.py",
      "side": "after",
      "line_start": 41,
      "line_end": 44,
      "snapshot_fingerprint": "<copy the exact after.fingerprint for src/routes.py from the current manifest>",
      "description": "The caller propagates the not-found result without accessing the missing record."
    },
    {
      "file": "tests/test_records.py",
      "side": "after",
      "line_start": 18,
      "line_end": 22,
      "snapshot_fingerprint": "<copy the exact after.fingerprint for tests/test_records.py from the current manifest>",
      "description": "The regression test covers a missing record through the caller."
    }
  ]
}
```

This is a schema example, not a ready-to-run result: replace each fingerprint placeholder with the exact `after.fingerprint` from the named file in the current manifest, and keep the cited lines within that captured snapshot. Keep every prior evidence path accessible in current snapshots and cite every available path on its `after` side; for a deleted path, cite the current `before` snapshot and the after-side caller when that caller was prior evidence. The helper compares captured source contents exactly, including whitespace. The relevant changed files need completed semantic coverage and no context gaps; unrelated incomplete files may still leave the aggregate result `partial`. An empty current result or a finding disappearing from a report never proves that a prior issue was fixed. The host must verify that the explanation is semantically correct; structural validation alone cannot prove it.
