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
- A deterministic local prompt overflow ends with
  `response.failed.response.error.code = "invalid_prompt"` and is not exposed as
  an interrupted stream.
- Final assistant messages carry `phase=final_answer`. Before a Kiro or local
  Cursor tool round, a bridge-provided progress message may stream with
  `phase=commentary`; tool-call items are emitted only after the full envelope
  and payloads pass validation.

### `WebSocket /v1/responses`

Accepts Codex Responses WebSocket `response.create` JSON text frames and emits
one complete Responses event per JSON text frame. Incoming frames, buffered SSE
records, and decoded event shapes are bounded and validated before emission.
Top-level validation errors include an HTTP-like `status`, allowing Codex to
classify them immediately instead of waiting for a disconnected stream. The
connection can carry sequential requests. This endpoint is not the Realtime
audio protocol.

A typical Codex frame has this shape (values and content abbreviated):

```json
{
  "type": "response.create",
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

The connection retains one bounded previous-response state in memory. A
`response.create` frame that cites its `previous_response_id` may therefore
send only new input items; the adapter reconstructs the logical request before
provider/session routing. This preserves instructions, tools, and exact item
fingerprints across model/tool round trips. An uncached ID returns
`previous_response_not_found`.

Codex v2 `response.create` prewarm frames (`generate: false`) complete locally
without invoking a provider and become that same connection-local previous
state.

Native OpenAI remote compaction is intentionally not advertised. In particular,
the adapter does not convert a normal provider message into an opaque
`compaction` output item. The managed Codex configuration uses a dedicated,
non-OpenAI provider name so Codex selects its local summarization flow rather
than sending a remote-v2 `compaction_trigger` request.
As defense in depth, an accidentally received `compaction_trigger` is rejected
immediately with `400 unsupported_feature` before Kiro/Cursor is invoked; the
adapter never turns an ordinary message into a forged compaction item.

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
| `GET` | `/api/control/{provider}/quota` | Read normalized quota/status information |
| `GET` | `/api/control/codex-config` | Read backup/takeover status, never file contents |
| `POST` | `/api/control/codex-config/enable` | Require `ENABLE`, back up, and apply managed fields |
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

Credential import is currently limited to
`POST /api/control/direct/auth/kiro_direct/import` with
`{"source":"pi"}`. It reads only `~/.pi/agent/auth.json`, rejects symbolic
links, oversized or malformed files, converts Pi's packed Kiro refresh field,
and returns only the normal redacted control state. It never accepts a path in
the request and does not switch the active provider.

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
