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
python3.11 eval/run_ab_batch.py --write-runtime-config --codex-config /root/.codex/config.toml --runtime-config-output /root/project/review-benchmark-20261010/runtime-config-v2.json
```

The output is created once with mode 0600; the containing directory is mode
0700. It discovers the executable bwrap shipped beside the selected Codex
Linux package; a system `bwrap` found on `PATH` is not used. It never includes
API keys, credential environment variables, or HTTP headers. The current local
preflight accepts model `gpt-6.1-sol`, provider
`custom` at `http://127.0.0.1:8080/v1` using `responses` with auth required, and
reasoning effort `max`.

Run the non-launching adapter capability probe with or without a frozen runtime
config:

```sh
python3 eval/run_ab_batch.py --preflight
python3 eval/run_ab_batch.py --preflight \
  --runtime-config /path/to/runtime-config-v3.json
```

The JSON report separates executable availability, the raw trace parser's
self-check, and CLI-to-process causal binding. A parser self-check or available
tools do not establish that binding. The default strict CLI event path has no
trusted OS PID or causal process identity, so the A/B runner reports `blocked`
with `next_step: adapter_validation` and exit status 3, even when the local
tools are installed. Strict evaluation can resume only when the host integration
provides verifiable call-ID-to-OS-process identity and execution-result
association. A successful simple command, text matching, or installed tools
alone are insufficient. If this requires maintaining a long-lived host fork or
extending the syscall parser, keep strict evaluation paused. This evidence gate
applies to the A/B runner; daily Skill trials and the optional review service
remain available.

The v2 runtime config is exact UTF-8 JSON with this shape:

```json
{
  "schema_version": 2,
  "codex_executable": "/usr/lib/node_modules/@openai/codex/bin/codex.js",
  "strace_executable": "/usr/bin/strace",
  "bwrap_executable": "/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/codex-resources/bwrap",
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

The v1 `runs/` tree and `runtime-config.json` are retained as
`runs-v1/` and `runtime-config-v1.json`. Prepare the isolated v2 input set in a
new directory without making model calls:

```sh
python3 eval/run_ab_batch.py \
  --plan /root/project/review-benchmark-20261010/batch-plan.json \
  --prepare \
  --runs-root /root/project/review-benchmark-20261010/runs-v2 \
  --runtime-config /root/project/review-benchmark-20261010/runtime-config-v2.json
```

Each execution is a fresh `strace -> bwrap -> codex exec` process chain. For
each slot, bwrap creates user, PID, mount, IPC, UTS, and cgroup namespaces and
shares the host network namespace so the configured loopback endpoint remains
reachable. It exposes `/usr`, `/lib`, and `/lib64` read-only, mounts only that
slot's `input/` tree read-only, bind-mounts the raw output file and a private
per-slot `CODEX_HOME` writable, and overlays the single host auth file at
`CODEX_HOME/auth.json` read-only. The checkout, sibling slots, and operator
directories are absent. The CLI environment is cleared and the adapter
explicitly sets `PATH`, `HOME`, `CODEX_HOME`, `TMPDIR`, and `LC_CTYPE`. `PWD`
was present in the six-variable canary, but the adapter does not explicitly
set it; bwrap or the shell may add it.

The adapter runs slots in the frozen order, records failures without retrying,
and refuses to start unless the plan has `plan_state=go` and
`execution_authorized=true`. A coordinator may pause remaining slots; v3 was
paused after its first failed slot as recorded below.
The GO fields may change after independent review; the immutable plan digest
must still match every presealed slot. The v2 command below is the historical
2026-10-12 invocation, which stopped after its first slot; it is not
authorization to retry v2. The separately frozen v3 batch has also run and is
now `closed_incomplete`; see the execution status below. Do not retry either
batch. No next-batch plan, runtime config, or model request has been issued.

The capability gate runs before any model subprocess. If it blocks execution,
each planned slot is recorded as `run_status=not_run`, `launched=false`,
`outcome=not_started`, and `execution_status=blocked`; no adapter-start record
or host output is created. The slot's `runtime/adapter-preflight.json` records
the capability report. Such a batch is blocked, not started or failed, and
cannot be retried in place. The required next step is adapter validation with
non-model fixtures and a trusted CLI-to-process binding; only after that work
and independent review may a new frozen execution be considered. Do not rerun
a blocked batch.

```sh
python3 eval/run_ab_batch.py \
  --plan /root/project/review-benchmark-20261010/batch-plan.json \
  --execute \
  --runs-root /root/project/review-benchmark-20261010/runs-v2 \
  --runtime-config /root/project/review-benchmark-20261010/runtime-config-v2.json
```

Execution writes raw CLI JSONL, raw model output, strace stderr, raw strace,
normalized supervisor JSONL, exact exit/timing metadata, and a sealed slot
result. Successful `slot-result.json` files bind every artifact digest; an
adapter exception writes `slot-failure-ledger.json` and an adjacent SHA-256
sidecar for the partial artifacts before any later slot may start. All
evidence files are mode 0600 under mode 0700 run directories. The adapter uses
`strace -f --decode-pids=pidns -yy` with raw `read`, `pread64`, `readv`,
`preadv`, and `preadv2` buffers and an explicit syscall list that includes
process creation, pipes, descriptor operations, and file mappings. It does not
trace environment values or network payload syscalls. Host-reported model/version values are not proof of remote
service identity. Unknown model version, token usage, and cost remain null.

For a completed run, preserve `runtime/codex-events.jsonl`, the normalized
`runtime/supervisor-events.jsonl`, raw `runtime/strace.log`, and
`artifacts/source-bundle-manifest.json`. Write `runtime-evidence.json` with
`build_runtime_evidence`; bind the exact reviewer-input, common prompt, output
schema, and runtime-config files under `common_bindings`. The schema-version-2
source manifest lists each binding path and digest. Its `skill_resource.path`
is the sandbox-visible `/slot/input/pinned-skill/review-changes/SKILL.md` for
both conditions; A records `mounted:false`, while B records `mounted:true` and
also records the host copy path, ZIP path and digest, member path, and member
digest. The verifier checks traced reads against `path` and checks the host
copy and ZIP member independently. The sidecar also binds the normalized trace
to the exact raw trace SHA and to the normalizer version/source hash.

The first supervisor JSONL line is the exact normalizer header. Its normalizer
version and source SHA must match the checked-in trusted `eval/runtime_trace.py`.
Run that parser over the preserved raw trace. The supervisor JSONL must contain
the exact same ordered event objects, including every parser field. A command
string match is only a candidate: current CLI JSONL has no process ID or causal
process binding, so the adapter does not add `call_id` from command text or
propagate it through trace descendants. A unique match, command suffix, or
thread ID cannot establish command identity. The raw syscall parser's
`trace_complete` status is separate from command association; absent a trusted
CLI-to-PID binding, runtime evidence and the slot remain incomplete. Conflicting
commands under one CLI item ID are rejected as ambiguous. Each event keeps the
parser's observed PID and one-based `trace_line`; process-creation events also
preserve the syscall return PID in the creator's namespace, which alone does
not prove PID namespace identity. Do not infer process IDs or descriptor paths
in the adapter.

Capture with `strace -f --decode-pids=pidns -yy -s 65536 -e raw=read,pread64,readv,preadv,preadv2`, including `eventfd` and `eventfd2`, without buffer dumping; set
`umask 077`, and verify all evidence files are mode 0600
with run directories mode 0700. Raw read buffers must remain pointers. Never
trace environment values, headers, or network payloads. The adapter records its
actual argv in `adapter-start.json` and `execution.json`, uses the finite
per-string `-s 65536` limit, and does not enable `-v`. This improves capture for
long argv within the limit; truncated or abbreviated argv remains unknown and
keeps the trace incomplete. Parser `unknown`
entries for malformed or unassociated syscall records remain incomplete.
Successful opens with unknown paths that are readable or have unknown
readability, positive reads from unknown non-pipe descriptors, and readable
mappings with unknown paths make runtime evidence incomplete. Unknown
write-only opens remain attribution limitations but do not alone make evidence
incomplete; known pipe traffic remains exempt. Path gaps never prove a B Skill
load. A B Skill-load claim requires a unique CLI `cat` event, a trusted
command/PID binding, its successful same-PID and same-call ID read of the pinned
path, and complete `aggregated_output` equality with the pinned ZIP member
bytes. Any successful
readable open, read, or mapping of the pinned path in A is a protocol violation,
regardless of which CLI command made it.

The run record references this sidecar through `artifacts.runtime_evidence`;
the sidecar remains a separate evidence artifact, not a replacement for the
run record. Summary comparability requires matching `review_manifest_sha256`
and common prompt/config/schema digests. Host-reported model/version values
are not proof of remote service identity. A coverage audit marked verified
must name an independent auditor and source and bind its trace artifact path
and digest.

## 2026-10-12 Execution Status

The v2 batch had 12 planned slots: 1 launched, 0 host-completed, 0
evidence-valid, and 11 not started. No host output or run record was produced.
The provider rejected the first-slot output schema because `schema_version`
lacked a `type`. An external binary symlink at argument 0 let artifact
enumeration escape its intended boundary, and the ledger error path re-raised
the failure. Separately, 18 `close` events reported `EBADF` after successful
closes, which made the trace `unknown`.

The final seal's `accepted` status covers dependency, frozen-plan, and
runtime-config identity consistency only. Its artifact-reference check examined
0 references and reported 12 unavailable slot results, so artifact completeness
is `incomplete`; this is not a workflow or quality pass. See the [v2 failure report](results/2026-10-12-ab-workflow-v2.md) and [evaluation record](README.md).

The earlier v2 remediation had a historical full-suite result of 325 passed,
4 skipped; three Semgrep tests were skipped when Semgrep was absent from
`PATH`, then 9 Semgrep checks passed with the existing `.venv/bin` on `PATH`.
One PostgreSQL-dependent test was skipped because `TEST_DATABASE_URL` was
unset. Independent focused regression checks passed 54 tests and the v2
workflow exporter CLI check passed. These results predate the current v3
repairs and do not certify them or A/B evaluation quality.

The v3 batch ID is `2026-10-12-ab-workflow-validation-v3`. It passed two
independent GO reviews and formally started, then closed as `closed_incomplete`.
Of 12 frozen slots, only `case0002/A/rep1` started. Codex rejected the open
object schema at `properties.tool_runs.items`; the wrapper exited 1 without
valid JSON. The other 11 slots were paused without retry. Counts: started 1,
failed 1, unstarted 11, host-completed 0, evidence-valid 0, pairs 0/6. Strict
build produced 0 records; validate, score, and summarize are NA.

Trace event `11198` is a `pread64` returning a bare `?`, not `ERESTARTSYS`;
the result is unavailable and the trace remains incomplete. The partial seal's
`accepted` means only that 98 checked references were consistent; 11 slot
results are unavailable. The private failure report is outside this repository
and has SHA-256
`5e4ccf3b366859d9139235c8861454d18e729fc55f51dd0abc98ec63295559a7`; the
snapshot-after SHA-256 is
`e0908e2057bf6fb91d988b1f3b86342af49d6593afe08b4a65d5f0bf3b3c755d`.

The v3 closure export is integrated in `eval/results/`:

- `2026-10-12-ab-workflow-validation-v3-summary.json` SHA-256 `24aef235909d8c554b508fbbb20519742933789477cd5073357bc21608f01e5a`
- `2026-10-12-ab-workflow-validation-v3-evidence.json` SHA-256 `05a706ff69abeb76c6c6b417d89a1fdefdc0bc538454f5fb49dbd50f226d293f`
- `2026-10-12-ab-workflow-validation-v3-check.json` SHA-256 `4a424c4e79197cbc2efeebe9240863054e81a048085f2627d0de727957a798eb`

The independent second schema review is GO: the production schema was
accepted, and actual `tool_runs.items` values of `true` and `{}` were each
rejected in separate runs; five focused reproduction tests passed. The
closure export's 30 related CI tests, Ruff, and CLI check passed.
`eval_reliability_review` gave Limited GO for the actual zero-record v3 closure
export: sealed-source replay, the summary's actual SHA, and the check agree.
This GO covers only that zero-record result and cannot be generalized to a
nonzero summary. Tool projection is complete with 84 independently passed
focused tests. The full suite passed 358 tests with 4 skips: three Semgrep
tests could not find the executable on the default PATH, and the PostgreSQL
integration test lacked `TEST_DATABASE_URL`. Adding `.venv/bin` to PATH
allowed the Semgrep tests to pass separately (9 passed). Ruff, `pip check`,
`git diff --check`, and the v3 closure, v2 workflow, and historical pilot
exporter checks passed. These results do not change v3's `closed_incomplete`
state or produce a quality result.

The v4 closure export is now integrated in `eval/results/`; its
[summary](results/2026-10-12-ab-workflow-validation-v4-summary.json),
[evidence](results/2026-10-12-ab-workflow-validation-v4-evidence.json), and
[check](results/2026-10-12-ab-workflow-validation-v4-check.json) retain
`closed_incomplete`. Of 12 planned slots, one was launched and completed its
host/wrapper output (exit 0), but its evidence was invalid; 11 were not
started. There are zero evidence-valid slots and zero eligible A/B pairs.
The accepted export bindings do not establish review quality. The sealed v4
artifacts retain parser v3 and the `c80ab00` baseline; parser v6 applies to
future runs.

The older 23-run pilot and its ten pending human judgments remain separate;
WorkBuddy remains `not_run`, quality metrics remain null, and historical
model identity/cost remain unknown. Those ten judgments must come from human
reviewers; model output cannot replace them. Next, verify real tracing
permissions, fixed inputs, output format, and Skill loading evidence before
running just one case with independent A and B sessions. Both runs must pass
evidence validation and complete rescoring before the pair is eligible; only
then should the batch expand. Human judgments require recorded reasons and
may remain unresolved. The fixed 0.2.1 fixtures remain the release baseline,
and PR4/PR5 remain later work.
