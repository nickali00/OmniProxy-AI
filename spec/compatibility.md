# Compatibility and evidence

The [adapter matrix](../docs/wiki/capacita-adattatori.md) is authoritative
for per-provider support. A vendor SDK can use a different provider through
a text facade. An integration tested by this project is not evidence of an
independent deployment, user adoption or vendor endorsement.

Candidate test targets, Linux, 2026-11-05:

| Client | Portable tested surface | Evidence |
| --- | --- | --- |
| OpenAI Python3.46.0 | Chat text, Responses store/retrieve/delete and SSE | conformance.sdk_checks |
| Anthropic Python2.5.0 | Messages text and native SSE | conformance.sdk_checks |
| Google Gen AI2.34.0 | generate_content and SSE text | conformance.sdk_checks |
| Continue2.0.0 / VS Code1.109.3 | Native client tool read and chat SSE through Codex | scripts.verify_vscode, actual extension host |
| Continue CLI1.5.47 | Coding tool cycle through Codex | Recorded baseline |
| Plain Python stdlib | Auth, discovery, limits, Chat/SSE, Responses, files/citations and ownership | conformance.check |

SDK checks use a real loopback HTTP listener and Ollama qwen3.5:2b, with
temporary keys/database and synthetic data. Core suite needs no gateway
imports. SDK dependencies are isolated from the server. See the dated report
in reports/ after execution; server-specific tests separately cover actual
Codex/MCP/jobs, rollback, PostgreSQL and telemetry. External adopters should
run the same suite with dedicated test keys and publish only sanitized
reports, naming their platform and enabled capability subsets.

Explicit exclusions: native Claude Code account not available; client
Windows and legacy Continue textual UI not certified; Codex images+client
functions rejected; full vendor API/Open Responses equivalence, structured
JSON output, realtime audio and arbitrary commands not supported. Local
neural audio/image backends are optional and not configured in the default
live deployment. Tiny image fixture tests establish valid PNG generation,
not production visual quality. Voice output is basic synthetic speech.
Organization replicas are verified on one Linux host with shared storage,
not a multi-host production reliability/SLO certification.

Adoption ledger starts at **no independently reported deployments or
contributors measured**. Do not equate SDK compatibility to standard status.
For independent reports collect platform, protocol/version, enabled subset,
sanitized conformance output, reproducible use case and consent to list it.
Track pass rates, upgrade failures, latency distributions and independent
contributors from evidence rather than invented counters or targets.
