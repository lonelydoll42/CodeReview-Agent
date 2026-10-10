# A/B Evaluation Recorder

`eval/ab_eval.py` is a Python standard-library-only recorder and scorer. It makes no model calls. Raw host output, input fingerprints, Skill bundle identity, workflow state, provisional evidence checks, and paired summaries are recorded separately from human quality labels.

The runner places each finished output here:

```text
<runs-root>/<case-id>/<condition>/repeatNN/host-output.json
```

Use the common output fields in [common-output.md](prompts/common-output.md). The case index points at the authoritative `reviewer-input.json` and fixed repositories. Do not prepare or score a run while its host output is still being written.

Optional sidecars:

- `run-metadata.json` distinguishes retries from independent replicates. Use `{"schema_version":1,"replicate":1,"attempt":2,"attempt_kind":"retry"}` for a retry. For an independent repeat use a new replicate number and `attempt:1`; `attempt_kind` is `independent_replicate`.
- `operator-events.json` can record verified handling events as `{"verified":true,"events":[{"actor":"agent","kind":"corrected_scope_path","count":1}]}`. These events are counted separately from user intervention; agent automation does not imply human effort. Keep `manual_interventions` null unless a host report or verified user event establishes the human count. The raw host output remains unchanged.
- If a host writes condition `A` or `B`, the recorder maps it to the canonical condition only when it agrees with the run directory (`A` = `same_host_direct`, `B` = `current_skill`). It records that mapping in `execution.normalization_events`; the raw host output and its digest are preserved unchanged. Other condition mismatches fail record creation.
- `coverage-audit.json` is optional and must only be marked `verified:true` after an independent transcript/tool-trace audit. It supplies `read_paths`, `uncovered_paths`, `context_omissions`, `review_status`, and boolean `scope_verified`. Without it, full coverage and known-defect recall stay unknown even when the host reports that it read every scoped path.
- Human labels are a separate `labels.json` with `label_source:"human"`, `status:"adjudicated"`, `case_classification`, `known_findings`, and an explicit `prediction_labels` entry for every reported finding. Each prediction verdict is `valid`, `false_positive`, `duplicate`, or `unresolved`; map valid findings to known issue IDs in `matches_known_findings`. Unmapped findings remain unresolved and do not become false positives.
- The human-label reference may be supplied by the case index or its operator annotation. A run is marked adjudicated only when the referenced file exists and says both `label_source:"human"` and `status:"adjudicated"`. Every prediction ID must occur exactly once; duplicate or missing IDs make human metrics invalid.
- Skill provenance records the provided bundle digest, the preserved artifact digest, and the digest reported by the host separately. A bundle path or matching local artifact does not prove that the host loaded it; `verified_loaded` is true only when the host-reported digest matches the preserved bundle digest.
- Repository checks record the pinned base/head commits, their trees, the full diff digest, input fingerprints, and worktree status at record-build and scoring time. Evidence line bounds are read from the pinned Git objects. These checks cannot establish that the repository remained unchanged during review.
- A protocol-incident sidecar can be linked to a run by its exact raw host-output path and digest. A digest-matched protocol violation marks the A/B pair inconclusive while preserving both run records.
- A protocol incident may include `restoration_attribution` with the generic actor, operational role, and phase. This can distinguish a coordinator/finisher agent restoring a template after review from the run agent that modified it; the raw host output and its digest remain unchanged.

Build records only after the desired outputs are complete. Pin the exact Skill package supplied to the host; a digest mismatch stops record creation.

```sh
python3 eval/ab_eval.py build \
  --index eval/data/real-cases.json \
  --runs-root /path/to/benchmark/runs \
  --skill-root skills/review-changes \
  --skill-label 0.2.2-dev \
  --skill-revision f35b31e015d2585a2cbd29b9ad1ae3e146eaf0bb \
  --skill-bundle /path/to/pinned/review-changes.zip \
  --skill-bundle-sha256 <sha256>
```

Then validate or rescore an individual record, or create the descriptive A/B report:

```sh
python3 eval/ab_eval.py validate /path/to/run.json
python3 eval/ab_eval.py score /path/to/run.json
python3 eval/ab_eval.py summarize \
  --index eval/data/real-cases.json \
  --runs-root /path/to/benchmark/runs
```

The summary chooses the latest completed attempt within each replicate and keeps every attempt in history. It requires matching input fingerprints and verified repository checks for a comparable pair; a known host mismatch or Skill bundle digest mismatch also limits comparison. Missing host identity or an unreported Skill load is shown explicitly in pair status. Workflow completion is not review correctness. Operator matches and evidence shape checks are provisional; precision, recall, clean-case false alarms, and wrong-resolution counts remain null until complete human adjudication. Unknown usage, cost, time, interventions, and coverage remain unknown rather than zero.

The current 11-case set is concentrated in its originating repository, with one external repository case; it supports a narrow initial comparison, not broad claims about external projects. Pending clean-case candidates are not included in a false-alarm denominator.
