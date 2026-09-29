# Local Diff Review

Use this skill when a user asks for a quick review of uncommitted or staged
changes in the current Git worktree and a full GitHub review service is not
running.

Run from the repository root:

```bash
python -m tools.local_review
```

Use `--staged` for the index, `--base <ref>` for a base ref, and
`--output review.md` when the user requests a file. The command performs local
static checks only and does not need PostgreSQL, Redis, Streamlit, GitHub
credentials, or an LLM key. Explain that a clean local report is not proof that
the change is safe; ask for the full service-backed review when broader source
context or model analysis is needed.

When reporting results, quote the severity, file, line, reason, and suggested
fix from the generated Markdown. Preserve the command's non-zero status when
the user explicitly passes `--fail-on` for CI use.
