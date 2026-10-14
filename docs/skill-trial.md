# review-changes 0.3.0-rc.1 Trial

This pre-release packages a host-driven Skill for local code review, saved-session recovery, and evidence-based rechecks. It is being offered to learn whether these workflows are useful in everyday review. The strict A/B evaluation is paused; this trial does not establish recall, quality improvement, or Skill gain. No external-user trial results have been collected.

The host assistant performs semantic review. Scripts capture the selected Git snapshot, run optional local checks, validate evidence, and render reports. The package does not fetch remote pull requests. WorkBuddy import and runtime behavior have not been validated.

## Download

The [GitHub pre-release](https://github.com/lonelydoll42/CodeReview-Agent/releases/tag/review-changes-v0.3.0-rc.1) provides the [ZIP](https://github.com/lonelydoll42/CodeReview-Agent/releases/download/review-changes-v0.3.0-rc.1/review-changes-0.3.0-rc.1.zip), [manifest](https://github.com/lonelydoll42/CodeReview-Agent/releases/download/review-changes-v0.3.0-rc.1/review-changes-0.3.0-rc.1.manifest.json), and [manifest checksum](https://github.com/lonelydoll42/CodeReview-Agent/releases/download/review-changes-v0.3.0-rc.1/review-changes-0.3.0-rc.1.manifest.json.sha256). ZIP SHA-256: `cc17d9cc9270613fe84bd86a876f09a0c7a92918f928d08f8d0786edcaf97bed`. Manifest SHA-256: `4ea7eca2cc18e63a0906d95b331464b7cf8b9b4d76a42bc9d58d12779070d87f`.

## Try the Workflow

1. Verify the versioned archive and import the Skill using your assistant's supported flow. Codex users can use the standalone install steps below; no source checkout is needed.
2. Choose a local branch or test repository and ask the host to review the selected worktree, staged index, or local branch comparison. Save the report and session output.
3. Optionally make an unrelated documentation change, then ask the host to recheck the prior report. A finding should not become `resolved` solely because it disappeared; it may remain `persisting` or `unverified`.
4. Make an actual repair and run review/recheck again. A `resolved` result should cite current captured evidence and explain how the original trigger path changed.
5. Export the review and recheck reports. Note any session-resume step or extra command needed along the way.

## Verify and Unpack

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

The `.sha256` file covers the exact manifest bytes; `archive_sha256` in the manifest covers the ZIP. Extract the verified archive into a new, empty directory. `mkdir` fails if that destination already exists, so this will not overwrite an existing copy:

```sh
mkdir ./review-changes-import
python3 -X utf8 -S -m zipfile -e ./review-changes-0.3.0-rc.1.zip ./review-changes-import
```

The extracted instructions are at `./review-changes-import/review-changes/SKILL.md`. In Codex, register/import that Skill folder using the current instructions for your Codex setup; the Skill directory can vary by configuration. If your setup has no folder import flow, start a session with filesystem access and ask Codex to read and follow the absolute path to that `SKILL.md` and its supporting files for the review; this uses the package without installing it as a registered Skill. Neither path requires cloning this repository. WorkBuddy import and runtime behavior have not been validated.

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

## Published GitHub Pre-release

- Release: [review-changes 0.3.0-rc.1](https://github.com/lonelydoll42/CodeReview-Agent/releases/tag/review-changes-v0.3.0-rc.1)
- Tag: `review-changes-v0.3.0-rc.1` at source commit `956439ea9c08afaffa4084473a19cc7c98cc521c`.
- Source CI: [run 38506803679](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38506803679), Ubuntu/Windows × Python 3.10/3.11 all passed.
- ZIP SHA-256: `cc17d9cc9270613fe84bd86a876f09a0c7a92918f928d08f8d0786edcaf97bed`; manifest SHA-256: `4ea7eca2cc18e63a0906d95b331464b7cf8b9b4d76a42bc9d58d12779070d87f`; 27 package resources.
- An independent download of all three public assets matched the locally verified files. The standalone projector returned `[]` for the empty tool-run input; packaging tests were 5 passed and AB batch tests were 37 passed, 2 skipped.
- The strict A/B remains paused at zero valid pairs; external trial feedback has not been collected. These checks are not a recall, review-quality, or Skill-gain result. WorkBuddy import and runtime remain unvalidated.

The official `0.2.1` fixture remains unchanged: ZIP SHA-256 `42948cb0de941299c5e6ed39628acaeadd22bda26e0fbcefa0804460d05b74aa`, manifest SHA-256 `5d8f6febfedf6532bc985b7d638099cacc7f99fdd408902c696c045135471ecf`. Historical agent disposition remains 9 supported, 0 unsupported, and 1 unresolved; see the [public adjudication record](https://github.com/lonelydoll42/CodeReview-Agent/blob/956439ea9c08afaffa4084473a19cc7c98cc521c/docs/validation/2026-10-14-agent-adjudication.md).
