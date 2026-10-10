# A/B Batch Runner Checkpoint

`run_ab_batch.py` builds the fixed 12-slot plan for workflow reproducibility
validation. The selected cases are from the prior real-repository corpus, so
this is not a fresh blind test. It records no quality result; annotations stay
pending and quality metrics remain separate.

The frozen slot order is:

| # | Case | Condition | Replicate | Attempt |
|---:|---|---|---:|---:|
| 1 | case0002 | A | 1 | 1 |
| 2 | case0003 | B | 1 | 1 |
| 3 | case0006 | A | 1 | 1 |
| 4 | case0003 | A | 1 | 1 |
| 5 | case0002 | B | 1 | 1 |
| 6 | case0006 | B | 1 | 1 |
| 7 | case0002 | B | 2 | 1 |
| 8 | case0003 | A | 2 | 1 |
| 9 | case0006 | B | 2 | 1 |
| 10 | case0003 | B | 2 | 1 |
| 11 | case0002 | A | 2 | 1 |
| 12 | case0006 | A | 2 | 1 |

Generate a plan without creating attempt records or making model calls:

```sh
python3 eval/run_ab_batch.py \
  --case-index eval/data/real-cases.json \
  --plan-output /tmp/2026-10-10-ab-workflow-validation-plan.preview.json
```

The independent review uses the already frozen plan at
`/root/project/review-benchmark-20261010/batch-plan.json`; do not replace it
with a regenerated preview.

Each A/B pair must share the exact reviewer-input bytes, repository tree, scope,
output schema, runtime config, and common prompt bytes. B adds only the frozen
unreleased Skill source bundle as a readable resource and the prompt line naming
its pinned path. The existing 0.2.1 package is not changed or represented as a
new release. Use one common absolute manifest path for both conditions; do not
rewrite it with condition-specific paths.

The host-visible source bundle is assembled from each selected case's
`same_host_direct` repository snapshot, excluding `.git` entirely. That keeps
answer commits and history out of the model view. Record a path-and-SHA-256
manifest for every included repository file and the Skill ZIP. The only other
host inputs are the exact reviewer-input bytes, common output schema, common
prompt, and (for B) the Skill resource. Do not mount the case index, public pilot
exports, prior labels/adjudications, the operator root, answer directories, or
any repository `.git` directory. Keep the human-review packet under
`/root/project/review-benchmark-operator-20261010/` with mode 0700/0600 and out
of every model-visible mount.

The adapter has separate plan, prepare, and execute operations. Presealing
copies each selected same-host repository snapshot while excluding every
`.git` directory. It rejects symlinks and non-regular repository inputs,
copies the reviewer input byte-for-byte, and records SHA-256 for every model
input. A and B receive identical reviewer input, repository tree, schema,
runtime config, and common prompt. B also receives the pinned Skill source tree
and a prompt line naming its absolute path. This is prepared-input mode: the
presealed repository has no `.git`, so the Skill's Git-based `start` helper
does not run and the model must not claim that full preparation workflow ran.

Create the private runtime config from the active Codex TOML. The writer copies
only the selected model, reasoning effort, and the provider's `name`, `base_url`,
`wire_api`, and `requires_openai_auth`; it ignores every other TOML key. It runs
`codex features list` with those exact explicit overrides in an isolated
`CODEX_HOME`, then checks the `codex exec` options and overrides with `--version`.
Both checks are local and make no model call. If the TOML omits reasoning
effort, pass a supported choice explicitly.

```sh
python3.11 eval/run_ab_batch.py --write-runtime-config --codex-config /root/.codex/config.toml --runtime-config-output /root/project/review-benchmark-20261010/runtime-config.json
```

The output is created once with mode 0600; the containing directory is mode
0700. It never includes API keys, credential environment variables, or HTTP
headers. The current local preflight accepts model `gpt-6.1-sol`, provider
`custom` at `http://127.0.0.1:8080/v1` using `responses` with auth required, and
reasoning effort `max`.

The runtime config is exact UTF-8 JSON with this shape:

```json
{
  "schema_version": 1,
  "codex_executable": "/usr/bin/codex",
  "strace_executable": "/usr/bin/strace",
  "model": "gpt-6.1-sol",
  "provider_definition": {
    "name": "custom",
    "base_url": "http://127.0.0.1:8080/v1",
    "wire_api": "responses",
    "requires_openai_auth": true
  },
  "reasoning_effort": "max",
  "timeout_seconds": 3600
}
```

`provider_definition`, `model`, and `reasoning_effort` record requested values;
they do not authenticate the remote service's actual model identity.
Credentials stay in the CLI's normal credential store and are never copied
into runtime config or traced environment values.

Preseal all slots without making model calls:

```sh
python3 eval/run_ab_batch.py \
  --plan /root/project/review-benchmark-20261010/batch-plan.json \
  --prepare \
  --runs-root /root/project/review-benchmark-20261010/runs \
  --runtime-config /root/project/review-benchmark-20261010/runtime-config.json
```

Each execution is a fresh `codex exec` subprocess. The adapter runs slots in
the frozen order, keeps failures and continues without retrying, and refuses to
start unless the plan has `plan_state=go` and `execution_authorized=true`.
The GO fields may change after independent review; the immutable plan digest
must still match every presealed slot. The current external plan remains
waiting and execution is not authorized.

```sh
python3 eval/run_ab_batch.py \
  --plan /root/project/review-benchmark-20261010/batch-plan.json \
  --execute \
  --runs-root /root/project/review-benchmark-20261010/runs \
  --runtime-config /root/project/review-benchmark-20261010/runtime-config.json
```

Execution writes raw CLI JSONL, raw model output, strace stderr, raw strace,
normalized supervisor JSONL, exact exit/timing metadata, and a sealed slot
result. All evidence files are mode 0600 under mode 0700 run directories. The
adapter uses `strace -f -yy` with raw `read`, `pread64`, `readv`,
`preadv`, and `preadv2` buffers and an explicit syscall list that includes
process creation, pipes, descriptor operations, and file mappings. It does not
trace environment values or network payload syscalls. Host-reported model/version values are not proof of remote
service identity. Unknown model version, token usage, and cost remain null.

For a completed run, preserve `runtime/codex-events.jsonl`, the normalized
`runtime/supervisor-events.jsonl`, raw `runtime/strace.log`, and
`artifacts/source-bundle-manifest.json`. Write `runtime-evidence.json` with
`build_runtime_evidence`; bind the exact reviewer-input, common prompt, output
schema, and runtime-config files under `common_bindings`. The source manifest
must list each binding path and digest, plus a `skill_resource` object with the
host-visible Skill path, ZIP path and digest, member path, and member digest.
The sidecar also binds the normalized trace to the exact raw trace SHA and to
the normalizer version/source hash.

The first supervisor JSONL line is the exact normalizer header. Its normalizer
version and source SHA must match the checked-in trusted `eval/runtime_trace.py`.
Run that parser over the preserved raw trace. The supervisor JSONL must contain
the exact same ordered event objects, including every parser field, with only a
non-empty `call_id` added to each event; missing, extra, reordered, or changed
events leave the trace incomplete. Each event keeps the parser's observed PID
and one-based `trace_line`. Bind `call_id` only from the matching CLI event; do
not infer process IDs or descriptor paths in the adapter.

Capture with `strace -f -yy -e raw=read,pread64,readv,preadv,preadv2`, without buffer dumping, set
`umask 077`, and verify all evidence files are mode 0600
with run directories mode 0700. Raw read buffers must remain pointers. Never
trace environment values, headers, or network payloads. Parser `unknown`
entries for malformed or unassociated syscall records remain incomplete.
Unknown file paths and successful reads from unknown descriptors are retained
as path-attribution gaps; they do
not invalidate known pipe traffic or prove a B Skill load. A B Skill-load
claim requires a unique CLI `cat` event, its successful same-PID and same-call
ID read of the pinned path, and complete `aggregated_output` equality with the
pinned ZIP member bytes. Any successful readable open, read, or mapping of
the pinned path in A is a
protocol violation, regardless of which CLI command made it.

The run record references this sidecar through `artifacts.runtime_evidence`;
the sidecar remains a separate evidence artifact, not a replacement for the
run record. Summary comparability requires matching `review_manifest_sha256`
and common prompt/config/schema digests. Host-reported model/version values
are not proof of remote service identity. A coverage audit marked verified
must name an independent auditor and source and bind its trace artifact path
and digest.
