# Architecture

Codex Provider Switchboard follows a ports-and-adapters layout. The design goal
is to keep protocol translation testable without starting a server, process, or
real provider request.

## Layers

| Layer | Responsibility | May depend on |
| --- | --- | --- |
| `domain` | Responses objects, SSE events, bridge envelopes, task-key hashing | standard library |
| `application` | active-provider routing, readiness, safe status, inspection | domain, provider ports |
| `providers` | Kiro/Cursor workflows, native direct adapters, custom Responses relay | domain, infrastructure ports |
| `infrastructure` | filesystem/backup state, CLI subprocesses, HTTP/SSE/event-stream clients | domain models where required |
| `web` | authentication, origin/Host checks, JSON limits, HTTP/WebSocket responses, UI | application |
| `runtime.py` | dependency construction | every concrete adapter |

FastAPI does not know whether Cursor uses a local process or the Cloud Agents
API. `CursorCliRunner` and `CursorClient` do not know what a Responses event
looks like. This separation lets each boundary be tested with a fake runner or
`httpx.MockTransport`.

## Request flow

```mermaid
sequenceDiagram
    participant C as Codex
    participant W as FastAPI web layer
    participant A as SwitchboardService
    participant P as Active provider
    participant M as SessionCache
    participant U as Kiro CLI, Cursor CLI/API, or custom API

    C->>W: POST /v1/responses or WS response.create
    W->>W: Host/auth/body validation
    W->>A: complete(body) or stream(body)
    A->>A: read active provider
    A->>P: dispatch immutable request
    opt Kiro or Cursor adapter
        P->>M: acquire task-key lease
        M-->>P: full input or continuation delta
    end
    P->>U: new session/agent or resume/run
    U-->>P: stdout or run SSE
    P-->>C: validated Responses JSON/SSE/WS events
    opt Kiro or Cursor adapter
        P->>M: commit output fingerprints + upstream ID
        P->>M: release per-task lock
    end
```

Provider choice is sampled once at request start. A dashboard change therefore
affects subsequent requests without mutating an in-flight stream.

## Bridge protocol

Kiro CLI and Cursor Agent receive a prompt containing the Responses request and
a random nonce. They are instructed to produce exactly one envelope:

```text
CODEX_SWITCHBOARD_BRIDGE_BEGIN_<nonce>
{"kind":"message","text":"..."}
CODEX_SWITCHBOARD_BRIDGE_END_<nonce>
```

or a validated tool-call envelope with a short user-visible progress update:

```text
CODEX_SWITCHBOARD_BRIDGE_BEGIN_<nonce>
{"kind":"tool_calls","commentary":"I will inspect the configuration first.","calls":[...]}
CODEX_SWITCHBOARD_BRIDGE_END_<nonce>
```

The nonce makes accidental marker collisions unlikely. Final message text is
emitted with Responses `phase=final_answer`. A tool round's `commentary` JSON
string is decoded incrementally and emitted with `phase=commentary`; it is a
concise action update, not hidden chain-of-thought or a synthesized reasoning
summary. Tool calls are held until the complete envelope can be checked against
the request's tool catalog.

The bridge also carries a compact tool-scheduling policy. It asks the upstream
model to minimize inference round trips by batching already-known independent
operations. When Codex exposes its custom exec orchestrator, one exec call can
await independent nested tools with Promise.all, even when the Responses
request disallows multiple top-level calls. Without exec, independent top-level
calls are grouped only when parallel_tool_calls permits it. Dependencies,
conflicting writes, destructive actions, and separate approval-sensitive
actions remain sequential. Switchboard never executes Codex tools itself; Codex
retains tool execution, sandbox, and approval ownership.

All bridge markers are control data, including markers carrying an older nonce.
The streaming decoder retains only the longest trailing substring that could
still become a bridge-marker prefix across an upstream chunk boundary, and
releases all other text immediately. Nested,
duplicate, stale, incomplete, or malformed envelopes fail closed. Normal
message text from both new and resumed Kiro sessions streams as soon as the
decoder proves that fragment is not control data. Protocol contamination before
visible output deletes that task's mapping and replays the complete request once
in a fresh Kiro session; after visible output, the interrupted stream fails
terminally instead of replaying duplicate text.
Tool-call events are never emitted until the full JSON envelope and every tool
name/payload have passed validation.

When Codex advertises `update_plan` and the work has several dependent steps,
the bridge asks the upstream model to maintain that real tool-backed plan. The
Codex UI can then render its native current-step indicator. Switchboard does not
invent a step count when the tool is absent or the upstream model does not call
it.

When session affinity reduces a resumed request to only its new input suffix,
the bridge still builds the tool catalog from the reconstructed full logical
request. This preserves `additional_tools` entries that Codex sent before the
suffix and prevents the upstream model from seeing a misleading empty catalog.

Before a new Kiro or local Cursor session is created, the bridge removes
transport-only IDs/status fields, annotations/logprobs, opaque encrypted
reasoning payloads, and duplicate `additional_tools` entries. If the rendered
prompt is still over the configured limit, it drops only the oldest complete
user-turn groups and records content-free truncation metadata in the bridge
request. Continuation deltas and the newest user turn are never trimmed. If the
active turn itself cannot fit, the adapter emits the terminal Responses error
code `invalid_prompt` without invoking the upstream CLI.

The protocol is a compatibility mechanism, not a security sandbox. Outer Codex
still resolves and approves returned tools. The selected upstream product keeps
its own execution and safety model.

The custom provider already speaks Responses, so it bypasses the bridge
envelope and relays bounded JSON/SSE directly. It removes local-only
`client_metadata`, optionally pins the configured model, and does not maintain
an extra session map.

## Session affinity

The mapping algorithm is intentionally conservative:

1. Prefer `client_metadata.thread_id`; fall back to `prompt_cache_key`.
2. Hash the key with SHA-256 before using it as a directory name.
3. Acquire one asynchronous lock for that digest.
4. Normalize input items and compare their SHA-256 fingerprints with the last
   successfully committed sequence.
5. Resume only when the previous sequence is an exact prefix of the new input.
6. Send only the suffix to an existing upstream session.
7. Commit the current input fingerprints, returned output fingerprints, and
   upstream session/agent ID atomically.

Codex currently gives a main task and each subagent different metadata thread
IDs even when an agent tree shares another cache key. Preferring the metadata
thread ID prevents a subagent from resuming its parent's Kiro session.

Cursor adds both the selected backend and model/parameter fingerprint to its
mapping key. Switching between CLI and Cloud API, or changing a model variant,
therefore creates an independent mapping.

If Kiro reports no output while resuming, the provider retries once with the
complete request in a fresh session. Cursor does the same for stale Cloud agent
IDs (`404`/`410`) and recognizable missing local CLI chat IDs.
If Kiro emits a context-overflow, mixed overflow/truncation, or truncated Bridge
status, the streaming parser withholds every possible status prefix until it can
classify the complete output. The provider clears the mapping and retries once
with older complete turns trimmed to the configured recovery bound. A repeated
status produces a terminal Responses failure. Responses omit usage when the
upstream does not report real tokens; bridge character counts are not a valid
replacement.

## State and concurrency

- `ConfigStore` uses a process-local reentrant lock and atomic replace.
- `SessionCache` serializes requests per task key but permits different tasks to
  progress concurrently.
- `KiroRunner` adds a configurable global semaphore because local CLI processes
  are relatively expensive. Per-task locks allow parallel tasks, while
  `KIRO_MAX_CONCURRENCY` controls how many actually enter Kiro simultaneously.
- `CursorCliRunner` serializes local processes by a configurable semaphore,
  bounds every NDJSON line/stream, and caches the CLI model catalog.
- The optional Cloud `CursorClient` serializes model-catalog cache refreshes.
- `CustomResponsesClient` pins auxiliary paths to the configured origin,
  refuses redirects, and bounds catalog, quota, JSON, and SSE payloads.
- `CodexConfigManager` serializes takeover/restore, stores a verified complete
  backup plus the exact managed-field set (including individual table keys),
  and restores those fields into the latest live TOML so unrelated automatic
  updates survive. Managed-field drift never creates an enable/disable deadlock:
  externally removed routing self-reconciles, while a missing backup falls back
  to removing only recorded routing values that still match. Default takeover
  uses a dedicated provider whose display
  name is not `OpenAI`; this prevents Codex from inferring native remote
  compaction support that the bridge does not implement.
- The WebSocket adapter decodes only complete JSON SSE records from providers
  before emitting one JSON text frame per Responses event.
- Its in-flight receive task is retained across a completed response, so an
  immediate tool result or continuation cannot be lost in a cancellation race.
- The WebSocket adapter retains one bounded previous-response state per
  connection. Matching `previous_response_id` turns are reconstructed from the
  prior request, prior output, and new input before fingerprint/session routing;
  uncached IDs fail explicitly instead of silently losing context.
- Native `compaction_trigger` input is rejected before provider dispatch. The
  managed non-OpenAI provider identity prevents Codex from sending it during
  normal operation.
- WebSocket `generate: false` prewarm is completed locally and installed as
  that same connection-local state, so a prewarm never starts a duplicate
  Kiro/Cursor inference.
- The HTTP adapter accepts identity, gzip, deflate, and zstd request bodies,
  enforcing the configured limit both before and during decompression.
- The inspector stores one redacted metadata snapshot under a lock.
- A permission-restricted rotating handler persists content-free operational
  history after applying credential, bridge-marker, and identifier redaction.

Run a single server process. The on-disk operations are atomic, but the
in-memory locks and active-provider inspector are intentionally process-local.

## Native direct providers

The `direct` provider is implemented entirely in this repository. Its catalog
fixes platform origins, protocol families, authentication modes, and default
models. Infrastructure clients implement Responses SSE, Anthropic Messages SSE,
AWS event-stream decoding for experimental Kiro direct mode, bounded model
catalog requests, and normalized quota data. Domain translators convert only
the public Responses request and tool shapes required by Codex.
Kiro's narrower tool-name grammar is handled with deterministic, collision-safe
aliases of at most 64 characters. Codex namespace and wire names are restored
on returned tool calls and reused consistently in tool-result continuations.
Kiro Direct also advertises a collision-safe internal final-answer tool whose
description carries the terminal-action contract without modifying the user's
message. Plain assistant text is treated as commentary until Kiro either calls
a real Codex tool or submits its complete answer through that internal tool. A
plain-text EOF receives one bounded corrective continuation; a repeated
incomplete EOF is reported as a terminal failure instead of a false
`response.completed`.

There is no Pi package, Node worker, plugin loader, or provider subprocess in
this path. External projects were used as interoperability research only.
Cursor is intentionally not in this catalog: its supported paths remain the
official local CLI and optional Cloud Agents API because unofficial internal
RPC adapters would broaden the security and compatibility boundary.

Authentication is separate from ordinary settings. `credentials.json` is
locked, written atomically, symlink-resistant, and never serialized to the
control plane. The web layer receives only configured/source/type/expiry
metadata. OAuth login sessions are bounded, retained briefly in memory, and
cancelled during application shutdown. A second login request for the same
platform reuses its active session. Explicit Pi-to-Kiro import reads only the
fixed `~/.pi/agent/auth.json` source with byte, regular-file, and symlink
checks; after validation it cancels any waiting Kiro login before atomically
storing the imported credential.

## Stored data

| Location | Contents | Permission target |
| --- | --- | --- |
| `config.json` | provider settings, optional keys, models, quota mapping | `0600` |
| `credentials.json` | direct-provider API keys and OAuth refresh state | `0600` in a `0700` directory |
| `config.toml.switchboard-backup-*` | exact pre-takeover Codex config | `0600` |
| `runtime/**/.bridge-session.json` | upstream ID, item hashes, timestamp | `0600` |
| `logs/switchboard.log*` | redacted bounded operational history | `0600` in a `0700` directory |
| Kiro work directories | data created by Kiro CLI | application-owned `0700` directories |
| Cursor CLI work directory | local CLI session/runtime data | application-owned `0700` directory |

Switchboard mapping files do not store prompt text. Provider products may keep
their own session history independently.

## Adding a provider

1. Add a transport in `infrastructure/` if one is needed.
2. Implement `ResponsesProvider` in `providers/`.
3. Give the provider a distinct session-cache namespace.
4. Register it in `runtime.py` and extend validated provider IDs/configuration.
5. Add mocked unit and HTTP integration tests.
6. Update both READMEs, configuration docs, the dashboard, and the changelog.

Do not add provider branches to FastAPI routes.
