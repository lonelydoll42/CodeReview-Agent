# Security Review

Load this method when a change touches authentication, authorization, input handling, queries, templates, file paths, process execution, secrets, cryptography, deserialization, network boundaries, or sensitive data.

Trace untrusted input to the sensitive operation and inspect validation, encoding, parameter binding, capability checks, and trust-boundary transitions along that path. Check both the changed code and relevant captured callers or guards.

For a suspected authorization regression, identify the protected resource and the concrete request path. Check whether equivalent enforcement moved to a decorator, middleware, service boundary, or policy helper, and whether all affected callers still pass through it. A removed check is a lead, not proof.

For injection concerns, identify the exact attacker-controlled value, the sink, and whether the API treats the value as code, query syntax, a path, or shell input. Confirm the mitigation is not already applied upstream. Semgrep output is a candidate signal and does not establish exploitability by itself.

State the preconditions and impact precisely. Do not claim remote exploitability, privilege escalation, or exposure of sensitive data unless the captured call path supports it.
