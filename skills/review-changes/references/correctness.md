# Correctness Review

Load this method when a change alters control flow, boundaries, state, contracts, concurrency, errors, deletion, migration, or compatibility.

Trace changed behavior from inputs through state changes to outputs. Check empty and singleton values, lower/upper bounds, null or missing data, repeated calls, retries, partial failure, and ordering where relevant. Use the actual contract and nearby tests rather than assuming a convention.

When a signature, response, event, schema, or configuration changes, search relevant consumers and verify the full migration. For removed code or files, inspect captured `before` content and determine whether references remain; deletion alone is not a defect. For error-handling changes, follow the exception or failure to the layer that owns recovery.

Check whether a changed invariant is enforced elsewhere and whether tests cover the behavior. A missing test can justify a suggestion, but is not by itself a production defect. Confirm that a reported failure is reachable in the supported execution path.
