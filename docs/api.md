# Local Responses API

The service intentionally exposes a small surface. JSON, SSE, and WebSocket
payloads are Responses-compatible only to the extent required by Codex.

## Responses endpoints

### `POST /v1/responses`

Dispatches one request to the active provider. `Content-Type: application/json`
is required and `input` must be present. HTTP fallback requests may use
`Content-Encoding: gzip`, `deflate`, or `zstd`; compressed and decoded sizes are
bounded independently.

- `stream: true` returns `text/event-stream`.
- Other values return one JSON Responses object.
- `X-Switchboard-Provider` identifies the provider sampled at request start.
- Provider failures use an OpenAI-style `error` object for non-streaming calls
  or a terminal `response.failed` event after a stream has started.
- Streaming output is decoded into complete typed JSON SSE records before it is
  forwarded. If an upstream closes, truncates an event, or raises after HTTP
  status 200 has already been sent, the adapter emits one terminal
  `response.failed` event instead of leaving Codex to classify an EOF as a
  retryable transport disconnect.
- A deterministic local prompt overflow ends with
  `response.failed.response.error.code = "invalid_prompt"` and is not exposed as
  an interrupted stream.
- Final assistant messages carry `phase=final_answer`. Before a Kiro or local
  Cursor tool round, a bridge-provided progress message may stream with
  `phase=commentary`; tool-call items are emitted only after the full envelope
  and payloads pass validation.
- The active provider's compatibility profile decides whether custom tools,
  `tool_search`, namespaces, and hosted multi-agent fields are passed through,
  lowered to ordinary functions and restored, or rendered into the strict
  prompt bridge. Dynamic tools returned by a completed `tool_search_output`
  remain available on later tool rounds.
- Native profiles forward only allow-listed Codex beta/lineage headers. When
  `multi_agent.enabled` is true they merge `responses_multi_agent=v1` into
  `OpenAI-Beta` without replacing unrelated beta tokens.

### `POST /v1/responses/compact`

Forwards the narrow Responses compaction request only when the active direct or
custom provider uses the `native_codex` compatibility profile. Switchboard uses
the selected model and retains only the documented compaction fields, including
`input`, `instructions`, `previous_response_id`, prompt-cache options, and
`service_tier`. Provider errors remain explicit JSON errors.

This endpoint returns `400 unsupported_feature` for Kiro, Cursor,
function-only gateways, and prompt bridges. It also rejects explicit compaction
when `multi_agent.enabled` is true; hosted multi-agent services compact each
agent context independently. Switchboard never converts ordinary assistant text
into a forged opaque `compaction` item.

### `WebSocket /v1/responses`

Accepts Codex Responses WebSocket `response.create` JSON text frames and emits
one complete Responses event per JSON text frame. Incoming frames, buffered SSE
records, and decoded event shapes are bounded and validated before emission.
The compatibility target is OpenAI's documented
[Responses WebSocket mode](https://developers.openai.com/api/docs/guides/websocket-mode),
[streaming event lifecycle](https://developers.openai.com/api/docs/guides/streaming-responses),
and [conversation-state model](https://developers.openai.com/api/docs/guides/conversation-state),
plus observed compatibility with the bundled Codex desktop client.
Top-level validation errors include an HTTP-like `status`, allowing Codex to
classify them immediately instead of waiting for a disconnected stream. The
transport follows the Responses WebSocket lane model: requests without a
`stream_id` share the default FIFO lane; requests with the same named
`stream_id` are also FIFO; different named lanes may run concurrently and their
events can interleave. At most 16 responses are active and at most 32 distinct
named stream IDs are registered per connection. This endpoint is not the
Realtime audio protocol.

`stream_id` controls routing, while `previous_response_id` controls response
lineage. They are deliberately independent. Every event for a named lane echoes
its `stream_id`. A new `response.create` never cancels work implicitly;
`response.cancel` cancels only the active response in its selected lane. The
provider is sampled when a request is accepted, so changing the dashboard does
not reroute a request that is already queued.

A typical Codex frame has this shape (values and content abbreviated):

```json
{
  "type": "response.create",
  "stream": true,
  "model": "gpt-5.6-sol",
  "client_metadata": {"thread_id": "..."},
  "input": [
    {"type": "additional_tools", "tools": [{"name": "exec"}]},
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "..."}]}
  ],
  "tools": [],
  "reasoning": {"effort": "max"},
  "parallel_tool_calls": true
}
```

An empty top-level `tools` array does not prove that no tools were supplied:
Codex can carry its effective catalog in the `additional_tools` input item.
Later frames commonly contain only `previous_response_id` plus new tool outputs;
the adapter reconstructs the full logical request before provider dispatch.

The connection retains a bounded LRU of completed response states in memory. A
`response.create` frame that cites a cached `previous_response_id` may therefore
send only new input items; the adapter reconstructs the logical request before
provider/session routing. This preserves instructions, tools, and exact item
fingerprints across model/tool round trips, including branching continuations.
An evicted or unknown ID returns a request-scoped
`previous_response_not_found` error rather than silently dropping context.

Codex v2 `response.create` prewarm frames (`generate: false`) complete locally
without invoking a provider and enter the same bounded connection-local cache.
The current Codex desktop transport may explicitly send `stream: true` in a
WebSocket frame; it is accepted for compatibility, while `stream: false` is
rejected.

The managed Codex configuration uses a dedicated, non-OpenAI provider name, so
Kiro/Cursor routes select Codex's local summarization flow. A
`compaction_trigger` is rejected with `400 unsupported_feature` before those
providers are invoked. Direct OpenAI-compatible routes and custom gateways
explicitly configured as `native_codex` may pass the native operation through;
an upstream rejection is returned as an error rather than synthesized locally.

Kiro responses carry `usage: null` because the CLI does not expose
authoritative per-run token usage. Cursor usage is included only when the
selected backend reports it. This prevents guessed bridge-prompt character
counts from driving Codex context-window decisions.

### `GET /v1/models`

Returns the model descriptor currently represented by the active provider. It
includes the standard `data` list plus an empty Codex-private `models` catalog,
which leaves Codex's bundled model metadata in control. It does not call Kiro
or Cursor.

## Operations

### `GET /health`

Returns version, active provider, provider readiness, and session settings. It
does not expose a key, prompt, or session ID.

## Local control plane

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/control/state` | Redacted settings, readiness, log policy, last metadata |
| `PUT` | `/api/control/settings` | Change provider, model, or redacted credentials |
| `GET` | `/api/control/kiro/models` | Run/cache Kiro's machine-readable model catalog |
| `GET` | `/api/control/cursor/models` | Read the selected CLI or Cloud Cursor catalog |
| `POST` | `/api/control/cursor/test` | Force a selected-backend catalog refresh |
| `GET` | `/api/control/custom/models` | Read a configured third-party model catalog |
| `POST` | `/api/control/custom/test` | Verify third-party auth and model discovery |
| `GET` | `/api/control/direct/platforms` | Read the fixed non-secret direct-provider catalog |
| `GET` | `/api/control/direct/models` | Read an official or compatibility model catalog |
| `PUT` | `/api/control/direct/api-key` | Store a key in the separate private credential file |
| `POST` | `/api/control/direct/test` | Validate direct authentication and model discovery |
| `DELETE` | `/api/control/direct/auth/{platform}` | Delete locally stored direct authentication |
| `POST` | `/api/control/direct/auth/{platform}/import` | Import a fixed, allow-listed local credential source |
| `POST` | `/api/control/direct/auth/{platform}/login` | Start a bounded OAuth/device login |
| `GET` | `/api/control/direct/auth/login/{id}` | Poll redacted login progress |
| `POST` | `/api/control/direct/auth/login/{id}/respond` | Submit a manual OAuth code or redirect URL |
| `POST` | `/api/control/direct/auth/login/{id}/cancel` | Cancel an active login task |
| `GET` | `/api/control/imports/pi` | Preview compatible Pi accounts without returning secrets |
| `POST` | `/api/control/imports/pi` | Import all compatible Pi accounts with explicit overwrite policy |
| `GET` | `/api/control/{provider}/quota` | Read normalized quota/status information |
| `GET` | `/api/control/codex-config` | Read backup/takeover status, never file contents |
| `POST` | `/api/control/codex-config/enable` | Require `ENABLE`, back up, and apply route plus selected agent fields |
| `POST` | `/api/control/codex-config/agents` | Require `APPLY` and update only managed Codex agent fields |
| `POST` | `/api/control/codex-config/disable` | Require `RESTORE` and restore only managed fields |

Browser mutations must be same-origin. When `SWITCHBOARD_TOKEN` is set, API
requests require `Authorization: Bearer <token>`. The standard dashboard is
intended for the default loopback/no-token deployment.

The settings API accepts only:

```json
{
  "active_provider": "cursor",
  "kiro": {"model_id": "gpt-5.6-sol"},
  "cursor": {
    "backend": "cli",
    "api_key": "value submitted once and never returned",
    "clear_api_key": false,
    "model_id": "advertised-model-id",
    "model_params": [{"id": "advertised-param", "value": "advertised-value"}],
    "model_display_name": "UI label",
    "follow_codex_effort": true,
    "timeout_seconds": 1800
  },
  "custom": {
    "api_key": "value submitted once and never returned",
    "clear_api_key": false,
    "base_url": "https://api.example.com/v1",
    "model_id": "gpt-5.5",
    "compatibility_profile": "function_only",
    "models_path": "/models",
    "quota_path": "/account/quota",
    "quota_total_field": "data.credits.total",
    "quota_used_field": "data.credits.used",
    "quota_remaining_field": "data.credits.remaining",
    "quota_reset_field": "data.reset_at",
    "quota_unit": "credits"
  },
  "direct": {
    "platform_id": "openai_codex",
    "model_id": "gpt-5.6-sol",
    "follow_codex_effort": true,
    "timeout_seconds": 600
  }
}
```

Direct credentials are deliberately outside the settings payload. The API-key
endpoint accepts only `platform_id` and `api_key`; responses contain redacted
configured/source/type metadata. OAuth callbacks are loopback-only and scoped
to an unguessable in-memory login ID. They cannot redirect to user-supplied
origins.

Pi import is intentionally narrow. Both endpoints read only the fixed
`~/.pi/agent/auth.json` source; the request cannot supply a path. Reads are
bounded and reject symbolic links, non-regular files, permissive file modes,
and malformed records. `GET /api/control/imports/pi` returns only provider,
target, credential type, and configured status. The POST body is:

```json
{"replace_existing": false}
```

The safe default skips existing credentials and active environment overrides.
Setting `replace_existing` to `true` can replace only Switchboard-owned stored
values and should be guarded by a browser confirmation. Compatible mappings
include API-key records for OpenAI, Anthropic, xAI, OpenRouter, and explicit
Cursor provider IDs; supported OAuth records include ChatGPT Codex, Anthropic,
GitHub Copilot, xAI, OpenRouter, and Kiro. Unsupported records are counted with
a generic label and reason without echoing arbitrary provider IDs or values.
Import does not change
the active provider or modify Pi's file. The legacy direct-Kiro endpoint remains
available for compatibility and uses the same validated importer. Each target
is written independently because Cursor and direct providers use separate
stores; the result lists every imported or skipped target, including a redacted
storage-failure reason, so a partial import is never reported as complete.

Fields may be omitted for partial updates. An empty `api_key` does not overwrite
the saved key; use `clear_api_key: true` to remove it. An environment-provided
`CURSOR_API_KEY` remains active after the file value is cleared. The same rule
applies to `THIRD_PARTY_API_KEY`/`CUSTOM_API_KEY` for the custom provider.

`cursor.backend` accepts `cli` (default) or `cloud_api`. CLI mode discovers
models using `cursor-agent --list-models`, reports normalized usage from each
terminal result event, and resumes the mapped CLI chat ID. The CLI does not
expose account remaining balance. Cursor's public Cloud Agents API also lacks a
remaining-balance endpoint, so both modes report that limitation instead of
inventing a balance.
