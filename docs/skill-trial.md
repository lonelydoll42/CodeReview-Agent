# review-changes 0.3.0-rc.1 Trial

This pre-release packages a host-driven Skill for local code review, saved-session recovery, and evidence-based rechecks. It is being offered to learn whether these workflows are useful in everyday review. The strict A/B evaluation is paused; this trial does not establish recall, quality improvement, or Skill gain. No external-user trial results have been collected.

The host assistant performs semantic review. Scripts capture the selected Git snapshot, run optional local checks, validate evidence, and render reports. The package does not fetch remote pull requests. WorkBuddy import and runtime behavior have not been validated.

## Download

The [GitHub pre-release](https://github.com/lonelydoll42/CodeReview-Agent/releases/tag/review-changes-v0.3.0-rc.1) provides the [ZIP](https://github.com/lonelydoll42/CodeReview-Agent/releases/download/review-changes-v0.3.0-rc.1/review-changes-0.3.0-rc.1.zip), [manifest](https://github.com/lonelydoll42/CodeReview-Agent/releases/download/review-changes-v0.3.0-rc.1/review-changes-0.3.0-rc.1.manifest.json), and [manifest checksum](https://github.com/lonelydoll42/CodeReview-Agent/releases/download/review-changes-v0.3.0-rc.1/review-changes-0.3.0-rc.1.manifest.json.sha256). ZIP SHA-256: `cc17d9cc9270613fe84bd86a876f09a0c7a92918f928d08f8d0786edcaf97bed`. Manifest SHA-256: `4ea7eca2cc18e63a0906d95b331464b7cf8b9b4d76a42bc9d58d12779070d87f`.

## Try the Workflow

1. Verify the versioned archive and install/register the Skill using your assistant's supported flow. Codex CLI users can follow the local install steps below; no source checkout is needed.
2. Choose a local branch or test repository with changes to review, then start a new Codex CLI session and explicitly invoke the installed Skill with this prompt:

   ```text
   $review-changes Review the current working tree, including relevant untracked files. Save the review report and session artifacts, then return their paths and any coverage limits.
   ```

3. If the report identifies an issue, fix it and ask the host to recheck using the saved session path. A finding should not become `resolved` just because it disappeared; the result should cite current evidence and explain how the original trigger path changed.
4. Save or share the review and recheck reports as your host supports. Note any extra command or manual step needed.

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
mkdir ./review-changes-import &&
python3 -X utf8 -S -m zipfile -e ./review-changes-0.3.0-rc.1.zip ./review-changes-import
```

The extracted package directory is `./review-changes-import/review-changes`, with its instructions at `./review-changes-import/review-changes/SKILL.md`.

### Codex CLI: Install

This copy-based installation was tested locally with Codex CLI `0.153.4`; other Codex configurations and host integrations have not been tested. It uses the existing `$CODEX_HOME` when set, otherwise `~/.codex`, and refuses to overwrite an existing destination, including a dangling symlink. It needs no pip installation, additional model-provider key, or review server; Codex CLI must already be configured and logged in.

```sh
python3 -X utf8 -S -c '
import os, pathlib, shutil, sys

source = pathlib.Path(sys.argv[1]).resolve()
if not (source / "SKILL.md").is_file():
    raise SystemExit("source folder does not contain SKILL.md")
codex_home = pathlib.Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else pathlib.Path.home() / ".codex"
destination = codex_home / "skills" / "review-changes"
if destination.exists() or destination.is_symlink():
    raise SystemExit("destination already exists: " + str(destination))
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(source, destination)
print("Installed Skill at " + str(destination))
' ./review-changes-import/review-changes
```

Start a new Codex CLI session after installation before invoking the registered Skill.

### Optional host-selection checks

These checks are optional and are not required before using the explicit prompt above.

- Catalog discovery: if your setup provides a normal Skills list or picker, check whether `Review changes` / `review-changes` appears after installation. This optional check establishes visibility only; it has not been measured in this trial.
- Natural-language selection: in a separate fresh session, make a relevant local-review request without the Skill name, its path, or `$review-changes`. Count it only when ordinary host session evidence shows that `review-changes` was selected and loaded.

Record these outcomes separately from explicit invocation and workflow completion. A visible catalog entry or a review response alone does not establish that the Skill was selected and loaded.

One internal CLI run per invocation mode is recorded in the [first-use check](validation/2026-10-15-first-use.md); the normal Skills list or picker was not tested.

### Manual absolute-path fallback

If you cannot register the Skill, start a session with filesystem access and ask the host to read and follow the absolute path to the extracted `review-changes/SKILL.md`, along with its supporting files, for that review. This is manual file reading for one session: it does not register the Skill, make it discoverable in the normal catalog, or test `$review-changes` or natural-language selection. It uses the verified package without cloning this repository.

## Feedback Notes

```text
Trial date:
Host and version:
Operating system / Python:
Project type / primary language:
Package version / archive SHA-256:
Install method and any installation error:
Catalog discovery result:
Explicit `$review-changes` invocation result:
Natural-language selection result:
Manual absolute-path fallback used? What happened?
Could you complete the workflow without author assistance? If not, where did you need help?
Commands or prompts I had to correct:
First report readability:
Were any findings worth fixing? Why or why not?
Optional unrelated-change recheck: expected / observed status
Repair recheck: expected / observed status and evidence quality
Was the repair recheck conclusion trustworthy? Why or why not?
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
