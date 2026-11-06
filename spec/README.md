# OmniProxy public contract 1

Release: **1.0.0-rc.3**, candidate, 2026-11-06. This document and the
[OpenAPI artifact](openapi.json) describe the supported wire contract. They
can be used without importing or reading the implementation. This is an
OmniProxy contract, not a certification of complete vendor API equivalence
or Open Responses conformance. Normative words MUST/SHOULD describe this
contract; provider support and test evidence are separate.

## Transport, identity and discovery

Use an operator-provided HTTP origin on a trusted local network, or HTTPS
for remote access. Application requests MUST carry
`Authorization: Bearer <local application key>`. `/v1/messages` also accepts
`x-api-key`; Gemini model endpoints also accept `x-goog-api-key`. Do not put
keys in URLs. Human administrative sessions cannot authorize inference;
application keys cannot authorize `/admin/v1`. Admin cookie sessions require
same-origin CSRF protection; the OpenAPI artifact describes bearer access.

Responses under `/v1/`, `/v1beta/` and `/admin/v1/` carry
`X-OmniProxy-Contract-Version: 1` and `X-OmniProxy-Version` (product version),
including authentication errors. Clients MUST ignore unknown additive
response fields and treat resource IDs and proposal hashes as opaque.

`GET /v1/models` returns `{"object":"list","data":[{"id":"alias",
"object":"model","created":0,"owned_by":"..."}]}`. A bound application
key sees only its profile alias; clients MUST use the returned alias. Model
selection never supplies a provider URL, credential or executable.

`GET /v1/capabilities` is authenticated and `Cache-Control: no-store`. It
returns contract/product versions, allowed model aliases, protocol subsets,
tool ownership, feature declarations, per-key limits and technical limits.
`availability: configuration_only_not_health` means no inference, provider
login check or backend health probe was performed. An endpoint being
implemented or configured does not establish current provider availability.
`adapter: null` means the key is not bound to one profile; capability details
then depend on the selected model. `client_functions` is true only for a
bound supported profile without managed MCP. `repo_patch_jobs` additionally
requires current workspace grants before a job can be created.

`GET /v1/usage` returns `limits`, `usage` and technical context information.
Limits are independent `input_tokens`, `output_tokens`, `context_tokens`,
each an integer or `null` (no key-imposed cap). A calendar budget uses
`token_budget` plus `budget_period: daily|weekly|monthly`; unlimited is
`token_budget: null, budget_period: unlimited`. Calendar windows use the
server timezone, default Europe/Rome; weeks begin Monday. Physical model,
HTTP, file, extraction and server concurrency ceilings remain effective.
`context_mode` is `reject` or opt-in `summarize`; summaries consume budget.
Usage is a gateway token estimate, not provider billing or an unlimited
provider subscription. Reservations prevent parallel budget overspend.

## Chat Completions subset

`POST /v1/chat/completions`, JSON:

```json
{"model":"alias","messages":[{"role":"user","content":"Say OK."}],"max_completion_tokens":64,"stream":false}
```

Roles: system, developer, user, assistant, tool. Text content is a string;
user multimodal content may contain `{"type":"text","text":"..."}`,
`{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`
or `{"type":"file","file":{"file_id":"file_...","mode":"auto"}}`.
Remote image URLs are rejected. Images require an adapter and model with
vision support; upstream deployments require explicit operator opt-in.
PDF/Office/text/video/audio use the gateway extraction pipeline; this does
not imply native PDF/audio/video support by the model. See the
[file format contract](../docs/wiki/allegati-multimodali.md).

Supported request controls include `temperature`, `top_p`, `max_tokens` or
`max_completion_tokens`, `stop`, `seed`, `stream`, `stream_options` and
supported client functions. Controls can be adapter-dependent. Unknown Chat
fields are not a promise that they affect inference. `response_format`,
non-text output modalities and inline input_audio are rejected. Use separate
media endpoints. Parallel function execution is outside the portable subset.

Success: `id`, `object: chat.completion`, `created`, `model`, `choices` with
`index`, `message` and `finish_reason`, plus `usage` containing `prompt_tokens`,
`completion_tokens`, `total_tokens` (sum of the first two). Text is
`choices[0].message.content`. Client function calls use `message.tool_calls`
with `id`, `type:function`, `function:{name,arguments}`; arguments are a JSON
string. The client executes the tool and supplies `role:tool`, `tool_call_id`
and string result in the next history. The gateway MUST NOT also execute it.
Client functions are supported by Codex and OpenAI-compatible upstream
profiles, not direct Ollama/Claude/Antigravity profiles. Codex rejects images
and client functions combined in one request.

Streaming uses `Content-Type: text/event-stream`, `data: <JSON>\n\n` chunks
with `object:chat.completion.chunk` and `choices[].delta`. With
`stream_options:{include_usage:true}`, a usage chunk with empty choices
precedes terminal `data: [DONE]`. Errors after headers are error events, not
new HTTP statuses. A client MUST detect errors/truncated streams and MUST
NOT blindly replay calls that may have invoked tools. Disconnect cancels
the active provider operation where possible; it cannot undo prior effects.

## Responses subset

`POST /v1/responses` accepts `model`, `input` (string or item list), optional
`instructions`, function `tools`, `tool_choice`, `stream`, `store`,
`previous_response_id`, `max_output_tokens`, `temperature`, `top_p`.
`parallel_tool_calls:true` and unsupported fields/content are rejected.
Text/image/file input and function_call/function_call_output items map to
the same Chat capabilities and ownership rules.

Success has `id`, `object:response`, `status:completed|incomplete`, `model`,
`output` message/function_call items, `output_text`, `usage` (input_tokens,
output_tokens,total_tokens), `store` and `previous_response_id`. Output
messages contain `content:[{type:output_text,text,annotations:[]}]`.
`store` defaults to **false**. With true, GET/DELETE
`/v1/responses/{id}` retrieve/delete only for the creating key; another key
gets 404. Delete returns `{id,object:response.deleted,deleted:true}`.
Continuation MUST use the same model and include only new input; the server
reconstructs prior input/output, capped at 256 items and 2 MB. Stored content
is retained until deletion; use explicit deletion for privacy.

SSE named events carry JSON with matching `type` and increasing
`sequence_number`. Lifecycle: `response.created`, `response.in_progress`,
output item/content/delta/done events, then `response.completed`,
`response.incomplete` (length), or `response.failed` (error). Successful
streams also terminate with `[DONE]`. Consumers MUST inspect terminal status
and not interpret connection closure alone as success. This contract does
not include realtime, background responses or WebSocket sessions.

## Native text facades

`POST /v1/messages` accepts model alias, positive required `max_tokens`,
`messages` with user/assistant string or text blocks, optional string
`system`, `temperature`, `top_p`, `stream`. Success follows the text message
shape with `content:[{type:text,text}]`, `stop_reason` end_turn/max_tokens
and usage input_tokens/output_tokens. SSE uses message_start,
content_block_start/delta/stop, message_delta, message_stop; failure uses
error. Tools, images and other native vendor features are rejected.

`POST /v1beta/models/{alias}:generateContent` and
`:streamGenerateContent?alt=sse` accept `contents:[{role:user|model,
parts:[{text:...}]}]`, optional `systemInstruction` and `generationConfig`
with maxOutputTokens/temperature/topP. Equivalent `/v1/models/` paths exist.
Success has candidates[].content.parts[].text, finishReason STOP/MAX_TOKENS,
modelVersion, responseId, usageMetadata. Streaming is JSON data SSE.
Only text is supported. These facades can route to a local model; using an
SDK does not imply an account for that SDK's vendor.

## Files and verifiable citations

POST `/v1/files` is multipart with `file`; success201 returns an opaque ID,
metadata and expiry. GET `/v1/files`, `/{id}`, `/{id}/content` list/get/download;
DELETE `/{id}` returns deleted:true and invalidates dependent citations.
Files belong to the creating key; expired or other-key IDs return404.
Technical byte/storage/processing limits and retention apply independently
of token limits. Extracted text is untrusted input to a model.

GET `/v1/files/{id}/extraction?mode=auto|text|vision` returns file_id,
source_sha256 (original bytes), mode, units (id,page,text,sha256), warnings,
expires_at and protected content_url. PDF units correspond to page numbers;
other text is document-1. Offsets count **Unicode code points**, start
inclusive/end exclusive, not bytes or JavaScript UTF-16 units. Images in
auto mode have no text units; mode=text requests OCR. Empty extraction is
not evidence of a quote.

POST `/v1/citations/verify` accepts file_id, unit_id, mode, source_sha256,
text_sha256, start, end, quote (1–4000 characters). Exact bytes, extraction,
hashes and substring MUST match. Success returns valid:true and the locator.
POST `/v1/citations` stores the locator, returns201/id; identical replay
returns200/same ID. GET/DELETE `/v1/citations/{id}` read/delete (204) only
for that key. Changed source/text returns409; invented quote422; missing or
other-owner source/citation404. At most1000 stored citations per key.
This proves provenance to extraction, not factual truth or OCR accuracy.

## Managed work and tool ownership

Modes are mutually exclusive: client functions, server-authorized MCP or
agent execution. Managed MCP is selected by an operator profile, uses only
granted tool names, records metadata audit and caches exact duplicate calls
within a request. No client may inject an arbitrary server URL/credential.

Jobs use a bound Codex/Ollama/upstream profile and granted workspace ID.
GET `/v1/workspaces` lists grants without host roots. POST `/v1/jobs` with
`Idempotency-Key` and `{type:repo_patch,workspace_id,instruction,paths?}`
returns202 on creation,200 on replay,409 on a changed body. GET `/v1/jobs`
lists metadata (`limit`1–100, `after`, optional state); GET `/{id}` gets details.
States: queued, running, awaiting_approval, applying, completed, cancelled,
failed. Generation cannot run repository commands or write files.

GET `/{id}/events` is SSE with monotone event IDs; use Last-Event-ID or after,
`follow=false` for replay only. POST `/{id}/cancel` can cancel queued/running/
awaiting_approval and prevents further actions. POST `/{id}/approvals` needs
`{decision:approve|reject,proposal_hash,revised_instruction?}`. Only the
current proposal can authorize a write. The server checks current file
hashes before writing; stale files prevent application. Repeated approval
cannot write twice. There is no delete-file or arbitrary command operation.
Rejection with instructions regenerates and increments revision.

POST `/{id}/resume` with a NEW Idempotency-Key and `{instruction?}` restarts
only failed/cancelled read analysis under current grants, increments revision
and requires new approval. Completed/manual-recovery jobs cannot resume.
Identical replay is idempotent. DELETE `/{id}` removes safe terminal history,
never repository bytes; a tombstone returns410 for a deleted creation key
instead of executing it again. Default history retention is unlimited;
operator-configured retention excludes active work/manual recovery.

`/v1/job-profiles` POST201/GET, `/{id}` GET/DELETE204 create/list/get/delete
immutable key-owned recipes (name,workspace_id,instruction,paths). POST
`/{id}/jobs` with Idempotency-Key and optional instruction starts an instance;
it adds no grants and freezes recipe ID/fingerprint. Limits:100 recipes/key,
100 distinct resumes/job,50 files,256KB/file,1MB snapshot,16k instruction.
Tests use separately authorized profiles and authenticated pull leases,
never a Docker socket or privileged command listener in the gateway. The
[full job and executor wire contract](../docs/wiki/contratto-lavori.md)
and [executor contract](../docs/wiki/esecutori.md) are part of this contract.

## Separate media APIs

POST `/v1/audio/transcriptions` accepts multipart audio; local Whisper or
upstream must be configured. POST `/v1/audio/speech` JSON uses model
speech-local, input, voice it/en-us/en-gb/es/fr/de, response_format
wav/mp3/flac/opus/pcm/aac and speed0.5–2. It returns binary audio; PCM is
mono signed16le24kHz. This is basic synthetic speech, not neural voice cloning.
POST `/v1/images/generations` JSON uses model image, prompt, n1–4, size
(default512x512), response_format b64_json; success returns PNG base64 entries.
Optional local Diffusers or configured upstream is required. Chat output
remains text. Runtime availability, quality and model licensing are operator
choices. [Audio](../docs/wiki/audio-uscita.md) and
[image limits](../docs/wiki/immagini-uscita.md) define duration/pixel budgets.

## Errors, retries and compatibility

Business, authentication and request validation errors use
`{"error":{"message":"...","type":"...","param":null,"code":"..."}}`.
Clients MUST branch on status/code, not localized message text. Generic
framework routing errors (404/405) may use detail instead. Validation is400
`validation_error` and does not echo rejected prompts or credentials.

| Status | Meaning / examples |
| --- | --- |
| 400 | Invalid request, unsupported capability, input/context limit exceeded |
| 401 | invalid_api_key (revoked/paused/out-of-scope included) |
| 403 | Missing explicit workspace/tool/administrative grant |
| 404 | Unknown model or absent/other-owner resource |
| 409 | Changed idempotent request, state conflict, stale workspace/source |
| 410 | Deleted job creation idempotency tombstone |
| 413 | HTTP/file size exceeded |
| 422 | Citation quote/range mismatch (domain validation) |
| 429 | Budget, concurrency, resource cap or upstream quota |
| 502/503/504 | Upstream/runtime failure, unavailable configuration, deadline |

Fallback is operator opt-in, only before any output, for configured safe
busy/connect/quota cases; never automatic replay of client tools or after
content. HTTP retries are not a general guarantee of idempotence. See
[versioning/support](versioning.md), [compatibility/evidence](compatibility.md)
and [standalone conformance instructions](../conformance/README.md).
