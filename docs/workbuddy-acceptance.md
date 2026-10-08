# WorkBuddy Acceptance

This is a run sheet for a real WorkBuddy session, not evidence that WorkBuddy has passed. The `0.2.1` package and isolated script tests are available; WorkBuddy import, script execution, and the review-to-recheck workflow remain pending until the artifacts below are captured from WorkBuddy itself.

Current preflight is limited to the package: all 23 ZIP entries matched the pinned manifest, and the extracted core/scripts plus an external-repository collector run succeeded under Python `3.10.12 -S`. These checks do not exercise WorkBuddy. The current Linux x86_64 environment has no WorkBuddy executable, application process, or connector, and no authoritative import procedure could be verified. A logged-in WorkBuddy runtime or desktop that can import and invoke a Skill is the remaining prerequisite for host acceptance; this environment probe does not imply that WorkBuddy is unavailable on other systems.

## Package under test

Exercise this exact release from an independent Git project:

- Package: `review-changes` version `0.2.1`
- Archive: `dist/review-changes-0.2.1/review-changes.zip`
- Source revision: `0a1a8756546ee15801180b06b3d9c6ac3558d481`
- Archive SHA-256: `42948cb0de941299c5e6ed39628acaeadd22bda26e0fbcefa0804460d05b74aa`
- Manifest SHA-256: `5d8f6febfedf6532bc985b7d638099cacc7f99fdd408902c696c045135471ecf`

Before import, calculate the archive hash on the machine running WorkBuddy and compare it with the pinned value. Also verify `review-changes.manifest.json` against its recorded hash and confirm that the archive hash in that manifest matches. Do not rebuild or modify the archive during this test. The ZIP and manifest are local build artifacts; this document does not claim they are published downloads.

Transfer the release directory and [preparation helper](../scripts/prepare_workbuddy_acceptance.py) to the WorkBuddy machine; no service checkout is needed. Run preparation there so repositories and snapshots use their final host paths. Do not reuse Linux manifests after moving the kit or changing OS. The helper pins the source revision and release SHA-256 values, verifies all package entries, then creates three independent repositories and their baseline snapshots.

```sh
python3 -S /path/to/prepare_workbuddy_acceptance.py prepare \
  --release-dir /path/to/review-changes-0.2.1 \
  --output '/path/to/WorkBuddy 验收 0.2.1'
```

Use a new output path; the helper refuses to overwrite one. Its `README.md` records the generated locations. Candidate package paths are `provenance/review-changes.zip` (verified archive) and `verified-release/review-changes/` (verified extraction); use the form supported by the tested WorkBuddy build. The three reviewed repositories are under `repositories/01 本地项目 验收样例/`, `repositories/02 应用工作区 代码样例/`, and `repositories/03 独立仓库 变更样例/`. Host-facing snapshots are `snapshots/case-01/`, `snapshots/case-02/`, and `snapshots/case-03/`; output folders are `host-artifacts/case-01/`, `host-artifacts/case-02/`, and `host-artifacts/case-03/`. `host-prompts/review.txt` and `host-prompts/recheck.txt` contain neutral instructions. `run-record.json` records absolute paths and prep status; project test phases are stored at `validation/project-tests/<operator-case-id>.json`; the external probe phase for case 01 is operator-only at `validation/operator-probes/runtime-missing-record.json`. `host-outcomes.json` stays `not_run` until real WorkBuddy evidence is entered. Keep the oracle, probe, validation records, run record, and prepared patches out of WorkBuddy prompts.

Using the import flow documented for the tested WorkBuddy build, choose its supported package form from `provenance/review-changes.zip` or `verified-release/review-changes/`. Record the exact UI steps or official command, WorkBuddy version/build, and operating system; neither format is presumed supported. If no supported import route is available, record the failed attempt and stop the host workflow. Manually invoking a script does not count as a WorkBuddy pass.

## Run the cases

Open one prepared repository at a time in WorkBuddy and use `host-prompts/review.txt`. The host-facing mapping is `case-01` / `01 本地项目 验收样例`, `case-02` / `02 应用工作区 代码样例`, and `case-03` / `03 独立仓库 变更样例`. Operator CLI IDs remain `runtime-missing-record`, `safe-authorization-move`, and `cross-file-contract`; keep those IDs, `operator/oracle.md`, and patches out of host prompts. Save WorkBuddy's original transcript, Markdown, validated JSON, and the exact manifest used by that result under `host-artifacts/case-NN/`; the prepared fixture manifests are `snapshots/case-NN/review-manifest.json`.

These are synthetic isolated fixtures for checking package import and the review/recheck/export workflow, not a quality evaluation on real repositories. `case-01` contains a reproducible missing-record defect fixture; it is not a production issue sourced from an independent project.

After each initial review and export, explicitly apply the prepared follow-up from the transferred helper. Replace the output path with the same absolute path used for `prepare`:

```sh
python3 -S /path/to/prepare_workbuddy_acceptance.py apply-fix --output '/path/to/WorkBuddy 验收 0.2.1' --case runtime-missing-record
python3 -S /path/to/prepare_workbuddy_acceptance.py apply-fix --output '/path/to/WorkBuddy 验收 0.2.1' --case safe-authorization-move
python3 -S /path/to/prepare_workbuddy_acceptance.py apply-fix --output '/path/to/WorkBuddy 验收 0.2.1' --case cross-file-contract
```

The helper verifies the recorded repository, patch, and snapshot paths stay inside this prepared output; it refuses changed `HEAD`, index, tracked worktree, or untracked-file state and refuses a repeated patch. On success it runs the fixture tests and writes `apply-results/case-NN.json` plus `snapshots/case-NN/current-initial.json` and `current-manifest.json`. The apply record contains project-test and, for case 01, operator-probe results; keep the whole record operator-only. For the `safe-authorization-move` operator case (`case-02` to WorkBuddy), the follow-up is a behavior-preserving helper extraction, not a bug fix; never describe an empty prior finding list as resolved.

Start a new WorkBuddy conversation for each recheck and use `host-prompts/recheck.txt`. Provide the prior Markdown report, exact prior manifest used by its validated result, and that result. Read the current manifest path from the operator-only `apply-results/case-NN.json`, but share only that path and the current snapshot, never the full apply record, `run-record.json`, validation records, or operator probe. Ask WorkBuddy to inspect relevant callers/tests, produce a current validated result tied to that manifest, and export the deterministic recheck. Export Markdown and JSON with the installed package's `finalize_review.py` and `recheck_review.py`; see [the exact script arguments](skill-first.md#recheck-a-previous-report). Save current result, recheck report, and raw transcript under `host-artifacts/case-NN/`. Verify both result fingerprints match their manifests. The recheck JSON must distinguish `resolved`, `persisting`, and `unverified`, and cite the current snapshot. Do not count fixture tests or prepared snapshots alone as WorkBuddy evidence.

## Pass criteria

- WorkBuddy imports the pinned package and invokes its packaged scripts from the independent project at the Chinese-and-space path.
- Each initial review and recheck is tied to the saved manifest fingerprint; required prior artifacts remain available in the new conversation.
- All three cases retain host-generated raw outputs, initial/current manifests, normalized JSON, and Markdown reports. The recheck JSON and Markdown are also saved.
- The `case-01` defect fixture is checked against its independent reproducer; the authorization refactor is checked against the complete authorization path; the cross-file repair is checked for the prior issue and new issues.
- Missing or failed tools, incomplete coverage, path/import problems, context omissions, and manual interventions are recorded. They are not silently treated as clean success.
- Unknown model, token, or cost measurements are recorded as `null`/unknown, never as zero.

## Acceptance record

Copy this section once per run and fill it from the WorkBuddy environment. Leave status fields `pending` if not attempted; use `null`, not `0`, for unmeasured numeric values. Do not infer a pass from Codex CLI or repository tests.

```text
Run date/time and timezone:
Operator:
WorkBuddy version/build:
WorkBuddy host OS / architecture:
Preparation output root:
run-record.json / host-outcomes.json paths:
Documented import procedure and exact steps:
Install attempt status (not_run / passed / failed) and evidence:
UI or command-trace evidence path:
Package/resource path observed by host:
Imported package version:
Archive SHA-256 measured / expected:
Manifest SHA-256 measured / expected:
Independent repository and absolute path:
Git version:
Python version:
Semgrep state/version:
Initial conversation/session reference:
Recheck conversation/session reference:
Raw initial transcript path:
Raw recheck transcript path:
Initial manifest and fingerprint:
Initial validated JSON path:
Initial Markdown path:
Current manifest and fingerprint:
Current validated JSON path:
Current Markdown path:
Recheck JSON path and status:
Recheck Markdown path:
Case outcomes (defect fixture / safe auth refactor / cross-file contract):
Install/path failures:
Context omissions or evidence gaps:
Manual interventions (count; null if unmeasured; description):
Host/model/tokens/cost (use null when unavailable):
Operator notes and unresolved issues:
```

## Keep host acceptance separate

The project's unit tests, package builder, `python -S` checks, and isolated fixture runs establish script/package behavior only. They do not establish that WorkBuddy can import the package, expose its resources, execute scripts, preserve context across a new conversation, or export the required files. Mark WorkBuddy acceptance complete only after the record above contains the host version, pinned package verification, all three case artifacts, and original WorkBuddy transcripts.
