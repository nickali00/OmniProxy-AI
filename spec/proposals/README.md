# Public proposal process

Propose contract changes in a repository issue or pull request using the
template below. Do not include credentials, account sessions or private
prompts. Link the use case and a minimal synthetic request/response.

States: draft → discussion → accepted/rejected → implemented. Acceptance
requires an explicit recorded maintainer decision, rationale, wire schema,
ownership/permission boundaries, failure/retry semantics, migration impact
and conformance test. Implementation alone does not imply acceptance as a
stable contract. Record dissent and alternatives in the proposal. A change
to an accepted decision requires a new proposal linking the original.

No RFC has been independently reviewed yet. This candidate establishes the
process and artifacts locally; repository publication and external review
must be reported separately.

```markdown
# RFC-NNN: concrete capability
Status: draft
Contract/product versions affected:
Problem and reproducible use case:
Request/response and SSE examples:
Tool owner and permissions:
Limits, errors, cancellation and retry/idempotency:
Storage/retention and migration/rollback:
Compatibility and conformance checks:
Alternatives and tradeoffs:
Decision, date and reviewer references:
```
