# Review Sessions

Use `scripts/review_changes.py` to keep local review artifacts tied to one Git worktree and one selected snapshot. Session files are stored outside the reviewed repository by default. An explicit `--session-dir` or `--session-root` inside the repository is rejected to avoid adding review artifacts to the worktree.

The scripts require Python 3.10+ and Git. Choose a Python 3.10+ interpreter already available to the host, such as `python3`, `python`, Windows `py -3`, or a configured absolute interpreter path. The examples use `python` as shorthand; replace that command prefix with the selected interpreter (for Windows, use `py -3 -S`). These commands use the standard library and do not require installing Python packages, changing global `PATH`, or configuring a model key.

## Start and Context

Start a session before reading changed source. The initial manifest pins the requested scope and the generated draft is bound to its `content_fingerprint`; all changed-file coverage is `uncovered` until the host records semantic review.

```sh
python -S "$REVIEW_SKILL/scripts/review_changes.py" start \
  --repo "$REPOSITORY" --scope worktree --include-untracked \
  --session-dir "$SESSION_DIRECTORY"
```

For `staged`, omit `--include-untracked`; this captures the Git index, so unstaged edits do not alter the reviewed content. For `branch`, provide `--base` and optionally `--head`; the session records and later reuses the fixed commit IDs. The target session directory must not already exist. Without an explicit destination, a unique directory is created under `~/.codex/review-sessions` (or the system temporary directory if needed to keep it outside the worktree).

After identifying supporting paths, capture them into a final manifest:

```sh
python -S "$REVIEW_SKILL/scripts/review_changes.py" context \
  --session "$SESSION_DIRECTORY" --context src/caller.py tests/test_contract.py
```

Each context capture retains the initial snapshot, records a new fingerprinted final snapshot and matching `uncovered` draft, and clears the current result references if the input changed. Repeated paths are harmless. Use the manifest and draft paths printed by the command; host evidence must cite that exact final manifest.

## Resume and Finalize

```sh
python -S "$REVIEW_SKILL/scripts/review_changes.py" resume \
  --session "$SESSION_DIRECTORY"
python -S "$REVIEW_SKILL/scripts/review_changes.py" finalize \
  --session "$SESSION_DIRECTORY" --result "$SEMANTIC_RESULT_JSON"
```

Resume checks the resolved repository root, worktree-specific Git directory, and common Git directory, then recollects the selected scope and captured context. The repository root and Git directory distinguish linked worktrees that share a common Git directory. Branch sessions use their recorded base/head commit IDs. A changed selected snapshot or context causes resume to fail and directs the host to start a new session; the old record and artifacts remain available. A different repository or worktree cannot resume the session even when its branch name or content happens to match.

`session.json` tracks workflow progress separately from `review_coverage_status`. The initial result draft and an empty finding list never imply completed semantic coverage. A validated report can be `partial`, `uncovered`, or `failed` while the workflow itself reached `report_ready`. Result attempts live under unique `runs/NNNN/` directories; JSON and Markdown outputs are written atomically, and failed attempts are recorded without changing the prior coverage status.

## Recheck

Link a previous finalized session when starting the new snapshot:

```sh
python -S "$REVIEW_SKILL/scripts/review_changes.py" start \
  --repo "$REPOSITORY" --scope worktree \
  --previous-session "$PREVIOUS_SESSION_DIRECTORY" \
  --session-dir "$CURRENT_SESSION_DIRECTORY"
python -S "$REVIEW_SKILL/scripts/review_changes.py" recheck \
  --session "$CURRENT_SESSION_DIRECTORY" \
  --current-result "$CURRENT_SEMANTIC_RESULT_JSON"
```

The previous review must be a validated result from the same worktree. Recheck calls the shared strict schema-version-2 implementation and preserves its `resolved`, `persisting`, and `unverified` decisions. If `--current-result` is omitted, the helper reuses the current session's latest validated result when one exists. Otherwise it still writes a recheck report, leaves prior findings unverified, and records the missing semantic input. A recheck report does not promote uncovered current review coverage.

The session module does not call a model or service. The host performs semantic analysis; scripts own snapshot identity, result validation, deterministic report rendering, and recovery bookkeeping.
