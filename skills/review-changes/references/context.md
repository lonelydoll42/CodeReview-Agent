# Context and Scope

Use the selected Git snapshot as the source of truth. Preserve `scope`, repository identity, `base`, `head`, `merge_base`, file status, and the manifest's `content_fingerprint` in the result. For a branch review, distinguish the requested base/head comparison from unrelated working-tree changes.

Read project-specific review instructions first. Start with the changed function, class, or configuration block, then follow only the relevant paths:

- For a changed call or signature, inspect its callers and the implementation it invokes.
- For a changed return shape, inspect every meaningful consumer and tests that assert the contract.
- For authorization, trace the request entry point and any protection moved to middleware, decorators, or a parent layer.
- For persistence or serialization, inspect the schema, migrations, readers, and writers involved.
- For error handling, trace whether an outer layer handles the condition as intended.

Prefer focused searches for symbols and configuration keys over reading the whole repository. If a supporting file is needed as evidence, add its repository-relative path to the `--context` arguments when collecting the final manifest. Cite captured before/after content or captured context only. If the source changes between inspection and capture, compare the fingerprint and re-read the fixed snapshot before deciding.

Record missing, binary, oversized, unsupported, or unreadable context as a coverage gap. Do not quietly replace unavailable evidence with a nearby line or a different revision.
