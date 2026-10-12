# A/B Workflow v2 Failure Report

The 2026-10-10 prepared-input workflow batch stopped after its first launched slot. The exported projection records 12 planned slots, 1 launched slot, 0 host-completed slots, 0 slots with valid evidence, 0 eligible pairs, 1 adapter failure, and 11 slots that were not started. No host output or run record was produced.

The final seal reports `accepted` because dependency, frozen-plan, and runtime-config identities matched their pre-execution bindings. That status is an integrity result, not a workflow or quality pass. The seal's artifact-reference check examined 0 references and reported 12 unavailable `slot-result.json` files, so seal artifact completeness is `incomplete`; `no_mismatches` with zero checked references does not establish that all batch artifacts were verified.

The six pair statuses are preserved from the final seal without reinterpretation: each is `pair_incomplete_missing_run_record`. Strict `ab_eval` build created 0 run records. Validation and scoring are `not_applicable`; this export does not create successful evaluations or scores.

Independent review attributed the first-slot failure to an output-schema declaration rejected by the provider because `schema_version` lacked a `type`. The same review found that an external binary symlink at argument zero let artifact enumeration escape its intended boundary and that the ledger error path re-raised the failure. It also found 18 `close` events reported as `EBADF` even though the descriptors had already been successfully closed, which caused the trace result to become `unknown`.

As a separate integrity cross-check, the first failure slot's 9 non-null artifact references matched the on-disk failure ledger, and all 98 artifact references and hashes recorded by that ledger matched disk. This independent check does not change the final seal's recorded completeness: the exportable seal report still has 0 references checked across 12 unavailable slot results. No private archive, trace payload, host output, runtime-config contents, auth data, or human labels are published here.

The inputs came from the existing prepared corpus. This is workflow reproducibility only, not a fresh blind quality benchmark. Human labels remain empty and unread; precision, recall, clean-case false alarms, and wrong-resolution count are `null`. Model cost and execution identity remain unknown.

## Bound Files

| File | SHA-256 |
| --- | --- |
| Final seal report source | `d4b906458da7d58b52472beed0b4eca53bb4d45bcb6965eac26580e8523faba3` |
| Final `ab_eval` summary source | `27fff54bb1527104c043ad00b99c0c2eebadef4b2dc1e3d7a22b14ea444b6654` |
| Pre-execution dependency snapshot bytes | `4898d099734b4964bb8c3a8744eab0340a354f225a8636d7bf7c3e0c640bce3b` |
| Frozen plan bytes | `50dfd7d0f48b4720424521f9e611260d509e44b68e083c6777090fdedebe4268` |
| Runtime-config bytes | `6488d53483e83ec9b9ce7b6688063c3f66d3e3e7730ca1f7ba028270468b8f22` |
| Exported summary bytes | `77d259a14e62affc90eaca35104fd6b8d7a33a788001466ad8a2846edfb1c239` |
| Exported evidence bytes | `3c4217b9dc55d2001f6b881fc4b94fe9d2e2c1f806883fbf99b779a1bf729cb6` |
| Recomputed check bytes | `03688e3e3ef262010a8e9377a98ba3eb279d67e13e89f33c301619f36c2d2ceb` |

Run `python eval/export_workflow_batch.py check` with the source files and exported summary/evidence to recompute the public projection and verify these byte bindings. The check passed for the files listed above.
