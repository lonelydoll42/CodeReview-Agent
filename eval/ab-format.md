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

## Portable Pilot Export

The public pilot summary and evidence index are a linked export. `eval/export_ab_pilot.py build` writes both from a summary generated by `ab_eval.py summarize` and a preserved evidence template when every run has the required historical input binding. It hashes the exact summary file bytes after generation, embeds that same parsed object in the evidence, and checks that the 23 run identities, A/B completion counts, pair statuses, and provisional v2 projection remain intact. It does not change the recorded agent verdicts or fill human metrics.

The digest scopes are explicit in `source_digest_scopes`: `portable_summary_sha256` covers the bytes of the published JSON file; `ab_summary_sha256` covers the controlled external `review-benchmark-operator-20261009/ab-summary.json`; the remaining historical adjudication digests cover their named external files. These source hashes are not interchangeable with the portable summary digest or a canonical-JSON hash.

For a new, strictly bound run set, generate the summary and export together:

```sh
python eval/ab_eval.py summarize \
  --index eval/data/real-cases.json \
  --runs-root /root/project/review-benchmark-20261009/runs \
  --output /tmp/2026-10-09-ab-pilot-summary.json
python eval/export_ab_pilot.py build \
  --summary /tmp/2026-10-09-ab-pilot-summary.json \
  --evidence-template eval/results/2026-10-09-ab-pilot-evidence.json \
  --operator-root /root/project/review-benchmark-operator-20261009 \
  --summary-output eval/results/2026-10-09-ab-pilot-summary.json \
  --evidence-output eval/results/2026-10-09-ab-pilot-evidence.json
python eval/export_ab_pilot.py check \
  --summary eval/results/2026-10-09-ab-pilot-summary.json \
  --evidence eval/results/2026-10-09-ab-pilot-evidence.json
```

The preserved 2026-10-09 records lack `input.review_manifest_sha256`. Do not synthesize that historical binding from today's reviewer-input copies. To refresh their descriptive export, use `build-historical`; it checks raw host-output bytes against each original record, recomputes workflow/duration/finding counts, records current input digests as audit-only, and records original score hashes without replaying or reusing those scores. The resulting summary marks descriptive replay `completed` and strict rescore `blocked`; historical scoring checks remain labeled `not_replayed` with their source-summary status shown separately:

```sh
python eval/export_ab_pilot.py build-historical \
  --summary-template eval/results/2026-10-09-ab-pilot-summary.json \
  --evidence-template eval/results/2026-10-09-ab-pilot-evidence.json \
  --runs-root /root/project/review-benchmark-20261009/runs \
  --operator-root /root/project/review-benchmark-operator-20261009 \
  --summary-output eval/results/2026-10-09-ab-pilot-summary.json \
  --evidence-output eval/results/2026-10-09-ab-pilot-evidence.json
python eval/export_ab_pilot.py check \
  --summary eval/results/2026-10-09-ab-pilot-summary.json \
  --evidence eval/results/2026-10-09-ab-pilot-evidence.json
```

Strict score and summary replay remains blocked until contemporaneous reviewer-input digests are available. Before another rebuild, verify the external archive and the run/score entries in its source manifest:

```sh
sha256sum -c /root/project/review-benchmark-operator-20261009/backups/ab-pilot-original-20261010/archive-sha256.txt
awk '/review-benchmark-20261009\/runs\//' /root/project/review-benchmark-operator-20261009/backups/ab-pilot-original-20261010/source-files.sha256 | sha256sum -c -
```

The b0 public evidence's raw-output hashes match 23/23 original records, but its projected run-record hashes match 0/23 and score hashes match 3/23 exact files. The separately pinned `run-score-original.tar` is the source-byte baseline used for the local audit; the builder currently does not read that archive itself. A successful export check validates summary/evidence file bytes and internal provenance, but cannot independently establish external source integrity, strict replay, human labels, coverage traces, or a quality comparison. The dedicated A/B export workflow runs this check and its focused tests without starting the standalone Skill CI.

## Preparing Human Blind Review

The condition-blind input, frozen source index, empty label template, and procedure are linked from [the pilot blind-review guide](results/2026-10-09-ab-pilot-blind-review.md). They contain ten anonymous predictions and no agent verdicts, known-issue mapping, or human result. They are preparation artifacts only and do not make the pilot human-adjudicated. Actual file-read and tool-call traces are still needed for an independent coverage audit; a matching Skill bundle hash alone does not prove that the host read or used the bundle.

The current 11-case set is concentrated in its originating repository, with one external repository case; it supports a narrow initial comparison, not broad claims about external projects. Pending clean-case candidates are not included in a false-alarm denominator.
