# review-changes 0.3.0-rc.1 Trial

This pre-release packages a host-driven Skill for local code review, saved-session recovery, and evidence-based rechecks. It is being offered to learn whether these workflows are useful in everyday review. The strict A/B evaluation is paused; this trial does not establish recall, quality improvement, or Skill gain. No external-user trial results are claimed.

The host assistant performs semantic review. Scripts capture the selected Git snapshot, run optional local checks, validate evidence, and render reports. The package does not fetch remote pull requests. WorkBuddy import and runtime behavior have not been validated.

## Try the Workflow

1. Verify the versioned archive and import the Skill using your assistant's supported flow. Codex users can use the standalone install steps below; no source checkout is needed.
2. Choose a local branch or test repository and ask the host to review the selected worktree, staged index, or local branch comparison. Save the report and session output.
3. Optionally make an unrelated documentation change, then ask the host to recheck the prior report. A finding should not become `resolved` solely because it disappeared; it may remain `persisting` or `unverified`.
4. Make an actual repair and run review/recheck again. A `resolved` result should cite current captured evidence and explain how the original trigger path changed.
5. Export the review and recheck reports. Note any session-resume step or extra command needed along the way.

## Import into Codex

From a POSIX shell in the directory containing the three versioned artifacts, verify the manifest checksum and the ZIP hash recorded in that manifest:

```sh
python3 -X utf8 -S -c '
import hashlib, json, pathlib, sys
manifest_path = pathlib.Path(sys.argv[1])
manifest_bytes = manifest_path.read_bytes()
expected_manifest_hash = pathlib.Path(sys.argv[2]).read_text(encoding="ascii").strip()
if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_hash:
    raise SystemExit("manifest checksum mismatch")
manifest = json.loads(manifest_bytes)
archive_path = manifest_path.with_name(manifest["archive"])
if hashlib.sha256(archive_path.read_bytes()).hexdigest() != manifest["archive_sha256"]:
    raise SystemExit("archive hash mismatch")
print("Verified " + manifest["version"] + ": " + archive_path.name)
' ./review-changes-0.3.0-rc.1.manifest.json ./review-changes-0.3.0-rc.1.manifest.json.sha256
```

The `.sha256` file covers the exact manifest bytes; `archive_sha256` in the manifest covers the ZIP. Then extract the verified package into your Codex user skills directory. The command reads an existing `CODEX_HOME` without changing it, falls back to `~/.codex` when unset, and stops if a `review-changes` installation already exists:

```sh
python3 -X utf8 -S -c '
import os, pathlib, sys, zipfile
codex_home = pathlib.Path(os.environ.get("CODEX_HOME") or pathlib.Path.home() / ".codex").expanduser()
skills_dir = codex_home / "skills"
destination = skills_dir / "review-changes"
if destination.exists() or destination.is_symlink():
    raise SystemExit("refusing to overwrite existing Skill: " + str(destination))
with zipfile.ZipFile(sys.argv[1]) as archive:
    bad_member = archive.testzip()
    if bad_member is not None:
        raise SystemExit("corrupt archive member: " + bad_member)
    if any(not name.startswith("review-changes/") for name in archive.namelist()):
        raise SystemExit("unexpected archive path")
    skills_dir.mkdir(parents=True, exist_ok=True)
    archive.extractall(skills_dir)
print("Installed to " + str(destination))
' ./review-changes-0.3.0-rc.1.zip
```

Restart or refresh Codex's Skill list if needed, then select `review-changes` for a local review. WorkBuddy import and runtime behavior have not been validated.

## Feedback Notes

```text
Trial date:
Host and version:
Operating system / Python:
Package version / archive SHA-256:
Import result and any installation error:
Commands or prompts I had to correct:
First report readability:
Optional unrelated-change recheck: expected / observed status
Repair recheck: expected / observed status and evidence quality
Extra manual steps:
Other notes:
```

## Proposed GitHub Pre-release

- Title: `review-changes 0.3.0-rc.1`
- Tag: `review-changes-v0.3.0-rc.1`
- Mark as a pre-release.
- Assets: `review-changes-0.3.0-rc.1.zip`, `review-changes-0.3.0-rc.1.manifest.json`, and `review-changes-0.3.0-rc.1.manifest.json.sha256`.

Release description:

> `review-changes` is a host-driven Skill for reviewing local worktree, staged-index, and branch changes, recovering a saved review session, and rechecking findings against a new snapshot.
>
> This is an early trial build intended to evaluate practical value in everyday code review. The strict A/B evaluation is paused. This release makes no claim about measured recall, review-quality improvement, or Skill gain, and no external-user outcomes are claimed.
>
> Download the versioned ZIP and verify it with the included manifest and checksum before importing it through your assistant's Skill flow. See the repository's `docs/skill-trial.md` for a short workflow and feedback notes template.
