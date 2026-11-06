# Standalone contract checks and SDK examples

Run from a copy of this repository, or copy the conformance/ directory alone
to an empty directory. The core runner uses only Python3.12+ standard
library and HTTP; it never imports app or server settings. No account key
for a cloud vendor is needed: use local OmniProxy application test keys.

Prepare private0600 JSON files `{"api_key":"<dedicated test key>"}` through
your provisioning workflow. Supply a second key to verify ownership. Use a
text-capable model alias returned by GET /v1/models. The suite consumes
inference budget and creates only synthetic resources; it deletes its own
stored responses/files, including dependent citations. It never approves
patches or runs workspace tools. Do not use keys with sensitive workflows.

```bash
python -m conformance.check --base-url http://127.0.0.1:8181 \
  --key-file /private/test-key.json --other-key-file /private/other-key.json \
  --model YOUR_ALIAS --output /tmp/core-report.json
```

Exit0 means every selected check passed; exit1 means failure. Reports contain
check names, durations and pass/fail only. No response bodies, keys, prompts,
host URLs or resource IDs are serialized. Automatic redirects are disabled
in the core HTTP runner. Use the intended origin, never an untrusted proxy.
Without a second key, ownership is explicitly marked unverified.

For three real SDK integrations and executable minimal examples:

```bash
python -m venv /tmp/omniproxy-client-env
/tmp/omniproxy-client-env/bin/pip install -r conformance/requirements.txt
/tmp/omniproxy-client-env/bin/python -m conformance.sdk_checks \
  --base-url http://127.0.0.1:8181 --key-file /private/test-key.json \
  --model YOUR_ALIAS --output /tmp/sdk-report.json
```

SDK helpers explicitly override base URLs and use the same local key. They
do not use default cloud endpoints. No inference SDK dependencies are added
to the gateway runtime. See `sdk_checks.py` for OpenAI Chat/Responses,
Anthropic Messages and Google generateContent examples.

Server-maintainer harness (optional, requires this implementation and real
Ollama) provisions an isolated SQLite gateway and two synthetic keys, opens
a real loopback HTTP listener and launches the independent runner as a
separate process. SDKs can be provided through isolated pip --target paths:

```bash
.venv/bin/python -m scripts.verify_conformance \
  --sdk-path /tmp/omniproxy-client-env/lib/python3.12/site-packages \
  --output /tmp/conformance.json
.venv/bin/python -m scripts.export_openapi --check
```

Scope: **core text/document HTTP**, not proof of every adapter or endpoint.
Media quality, job execution, MCP, team administration, PostgreSQL,
cancellation and coding-client integration have separate reproducible
verifiers documented in the [release workflow](../docs/wiki/verifiche-rilascio.md).
Implementers must publish unsupported subsets rather than treating a core
pass as universal API conformance. Generated OpenAPI describes request
schemas, authentication and routes; dynamic responses/SSE semantics are
defined in spec/README.md. Neither artifact licenses a full vendor-API claim.
