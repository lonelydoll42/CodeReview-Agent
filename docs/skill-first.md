# Skill-First Implementation and Acceptance

`review-changes` is the primary local review entry point. The host assistant performs semantic analysis with its current model; the package provides pinned Git inputs, review methods, optional static signals, strict result validation, and deterministic Markdown rendering. It does not require this repository, the API service, a model SDK, a database, or Redis in the target project.

This package supports worktree, staged-index, and local branch reviews, plus rechecking a prior schema-version-2 report against a new local snapshot. It does not fetch remote pull requests. WorkBuddy packaging and behavior have not been validated.

## Build

From a clean output directory, use Python 3.10 or newer:

```bash
REVIEW_DIST="$(mktemp -d /tmp/review-changes-dist.XXXXXX)"
python -S scripts/build_review_skill.py --output "$REVIEW_DIST"
```

The output contains an unpacked `review-changes/` package, `review-changes.zip`, a version manifest with every package file hash and the archive hash, and a `.sha256` checksum for the manifest. The builder copies `review_core/` from its single repository source and extracts the optional Semgrep YAML from `tools/semgrep_runner.py`; neither is maintained as a second source copy. The output path must not already contain these artifact names.

The package scripts can be run from outside this checkout. For example, run `python -S "$REVIEW_DIST/review-changes/scripts/collect_changes.py" ...` while your current directory is a different Git repository. `-S` disables Python site initialization, so it also verifies that the scripts do not rely on installed site packages.

## Isolated Repository Check

The following creates a temporary project with three deliberately different versions: a branch commit that removes authorization, a staged version that permits only administrators, and an unstaged worktree version that checks authentication. An unchanged caller and test provide context. The project has no dependency on this repository.

```bash
REVIEW_TMP="$(mktemp -d /tmp/review-changes-check.XXXXXX)"
REVIEW_REPO="$REVIEW_TMP/ordinary project"
REVIEW_SKILL="$REVIEW_DIST/review-changes"
mkdir -p "$REVIEW_REPO/src" "$REVIEW_REPO/tests"
git -C "$REVIEW_REPO" init --initial-branch=main
git -C "$REVIEW_REPO" config user.name "Review Skill Check"
git -C "$REVIEW_REPO" config user.email "review-skill-check@example.invalid"
```

Create the baseline files:

```bash
cat > "$REVIEW_REPO/src/permissions.py" <<'PY'
def can_read(user, record):
    return user.is_admin or user.id == record.owner_id
PY
cat > "$REVIEW_REPO/app.py" <<'PY'
from src.permissions import can_read

def handle(user, record):
    return can_read(user, record)
PY
cat > "$REVIEW_REPO/tests/test_permissions.py" <<'PY'
from types import SimpleNamespace

from src.permissions import can_read


def test_unrelated_user_is_denied():
    user = SimpleNamespace(is_admin=False, is_authenticated=True, id="member-2")
    record = SimpleNamespace(owner_id="member-1")
    assert can_read(user, record) is False
PY
git -C "$REVIEW_REPO" add .
git -C "$REVIEW_REPO" commit -m "baseline"
git -C "$REVIEW_REPO" switch -c review-case
```

Create a branch change that introduces an authorization regression, commit it, then make staged and unstaged versions that differ:

```bash
cat > "$REVIEW_REPO/src/permissions.py" <<'PY'
def can_read(user, record):
    return True
PY
git -C "$REVIEW_REPO" add src/permissions.py
git -C "$REVIEW_REPO" commit -m "remove record authorization"
cat > "$REVIEW_REPO/src/permissions.py" <<'PY'
def can_read(user, record):
    return user.is_admin
PY
git -C "$REVIEW_REPO" add src/permissions.py
cat > "$REVIEW_REPO/src/permissions.py" <<'PY'
def can_read(user, record):
    return user.is_authenticated
PY
cat > "$REVIEW_REPO/app.py" <<'PY'
from src.permissions import can_read

def handle(user, record):
    return bool(user) and can_read(user, record)
PY
```

Capture each scope from the packaged scripts, not from project modules in this checkout:

```bash
python -S "$REVIEW_SKILL/scripts/collect_changes.py" \
  --repo "$REVIEW_REPO" --scope staged \
  --output "$REVIEW_TMP/staged-initial.json"

python -S "$REVIEW_SKILL/scripts/collect_changes.py" \
  --repo "$REVIEW_REPO" --scope staged \
  --context app.py tests/test_permissions.py \
  --compare-to "$REVIEW_TMP/staged-initial.json" \
  --output "$REVIEW_TMP/staged-final.json"

python -S "$REVIEW_SKILL/scripts/collect_changes.py" \
  --repo "$REVIEW_REPO" --scope worktree \
  --output "$REVIEW_TMP/worktree.json"

python -S "$REVIEW_SKILL/scripts/collect_changes.py" \
  --repo "$REVIEW_REPO" --scope branch --base main \
  --output "$REVIEW_TMP/branch.json"
```

Check that staged, worktree, and branch manifests contain `is_admin`, `is_authenticated`, and `return True` respectively. The staged context must use the staged scope's source endpoint. `--compare-to` verifies the selected scope identity and captured change contents against the initial manifest; it must fail without writing a final manifest if that selected scope changes while context is being captured (for example, a staged file is restaged or a worktree file is edited). The regression `test_compare_to_rejects_live_review_scope_drift` exercises this failure from an independent repository.

Run the optional static step and inspect its status, `rules_version`, `covered_files`, and `uncovered_files`:

```bash
python -S "$REVIEW_SKILL/scripts/run_static.py" \
  --input "$REVIEW_TMP/staged-final.json" \
  --output "$REVIEW_TMP/tool-runs.json"
```

Depending on the environment, Semgrep is `missing`, `failed`, or `completed`. A completed run can still leave a language such as Go uncovered by the bundled rules. The host continues semantic review in all three cases.

Finally, install the package in a disposable assistant test environment, ask it to review each manifest's corresponding scope, and check that the branch case reports the missing authorization with evidence from captured source/context. Have the assistant create a schema-version-2 result tied to the final manifest fingerprint and run:

```bash
python -S "$REVIEW_SKILL/scripts/finalize_review.py" \
  --input "$REVIEW_TMP/staged-final.json" \
  --result "$REVIEW_TMP/result.json" \
  --output "$REVIEW_TMP/review.md" \
  --validated-output "$REVIEW_TMP/validated-result.json"
```

The finalizer must reject evidence paths, sides, fingerprints, or line numbers that are not present in the captured manifest. It saves the normalized result so aggregate status in JSON and Markdown agrees; for example, a claimed `completed` result with uncovered changed files is persisted and rendered as `partial`. A failed validation is not a clean review. The tests `tests/test_review_skill_runtime.py` exercise package execution from outside the repository under `python -S`, differing index/worktree snapshots, captured context, live scope-drift rejection, static-tool missing/failure/coverage states, evidence validation, and normalized status persistence.

## Recheck a previous report

After modifying the code, capture the same scope again and ask the host assistant to produce a new schema-version-2 result tied to the new manifest. The assistant should inspect each prior finding's root cause, trigger, impact, callers, and tests, then add an explicit `recheck_decisions` entry when it can prove a finding is resolved:

```json
{
  "previous_finding_id": "authorization-owner-check",
  "status": "resolved",
  "reason": "The caller now rejects an unrelated user before the record is returned."
}
```

Run the packaged deterministic association and renderer with the prior manifest/result and the new manifest/result:

```bash
python -S "$REVIEW_DIST/review-changes/scripts/recheck_review.py" \
  --previous-manifest "$REVIEW_TMP/previous-manifest.json" \
  --previous-result "$REVIEW_TMP/previous-result.json" \
  --current-manifest "$REVIEW_TMP/current-manifest.json" \
  --current-result "$REVIEW_TMP/current-result.json" \
  --output "$REVIEW_TMP/recheck.json" \
  --markdown-output "$REVIEW_TMP/recheck.md"
```

The report associates stable finding IDs first, then falls back to the root cause, trigger, impact, and captured evidence paths. Renames and line-number drift are allowed when the captured snapshots support the association. Each prior finding is `resolved`, `persisting`, or `unverified`; a missing current result, failed review, unavailable source, or insufficient evidence remains unverified. A finding disappearing from the new report never proves that it was fixed. The aggregate recheck status is `partial` when the current review has failed, uncovered, or incomplete coverage. New confirmed findings and coverage gaps are listed separately.

## Current Boundaries

The host model does semantic analysis; this project does not measure its token use or cost, so unknown measurements stay `null`. Semgrep is optional and the bundled rules cover only their declared languages. Unsupported languages and files whose content could not be captured remain uncovered; the review or recheck must be marked partial or uncovered as appropriate. This package is local-only. PR retrieval, GitHub comments, and WorkBuddy packaging await separate implementation and acceptance.
