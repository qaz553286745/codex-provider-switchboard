# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-08-13

### Added

- Switchboard-native direct providers for OpenAI API, ChatGPT Codex, Anthropic,
  GitHub Copilot, xAI, OpenRouter, and experimental Kiro account access, with no
  Pi/Node/plugin runtime dependency.
- Independent `0600` direct credential storage, environment precedence,
  bounded OAuth/device flows, refresh locking, and loopback-only callbacks;
  Codex's own `auth.json` is never imported.
- Native Responses and Anthropic SSE framing, CRC-validated AWS event-stream
  decoding, complete-JSON tool-call gating, model discovery, and OpenRouter
  quota normalization.
- Dashboard controls for direct platform/model selection, API-key and account
  authentication, stability labels, login progress, logout, and usage status.
- Explicit, fixed-source import of Pi's saved Kiro OAuth credential, including
  packed-refresh conversion, bounded symlink-resistant reads, redacted API
  responses, same-platform login reuse, and waiting-login cancellation.
- Cursor Agent CLI backend with stdin-only prompts, NDJSON streaming, exact
  session resume, model discovery, and normalized per-run token usage.
- Explicit `cursor.backend` selection; local CLI is the default and the
  existing Cloud Agents API remains available as `cloud_api`.
- Generic bearer-authenticated Responses provider with bounded JSON/SSE relay.
- Kiro and Cursor official model discovery plus custom `/models` support.
- Web quota cards for Kiro `/usage`, Cursor's documented API boundary, and
  configurable third-party JSON quota mapping.
- Confirmed Codex config backup with current-file, field-level takeover and
  restore; default takeover uses a dedicated non-OpenAI provider identity so
  Codex does not invoke unsupported remote compaction, while unrelated
  automatic Codex changes survive restoration.
- Deadlock-free Codex config disable: stale externally restored state is
  reconciled automatically, managed-field drift restores from the verified
  backup, and a missing backup falls back to conservative proxy-route cleanup.
- Fail-closed bridge parsing for old nonces, nested/malformed envelopes, and
  arbitrary chunk boundaries; contaminated Kiro resumes discard their mapping
  and retry once in a fresh session, and tool calls wait for complete parsing.
- Permission-restricted, redacted rotating operational history.
- Codex Responses WebSocket transport, Codex-compatible model probing, and
  terminal status propagation for WebSocket validation failures.
- Connection-local Responses WebSocket continuation, including
  `generate: false` prewarm and generated-response chaining, so incremental
  `previous_response_id` turns retain instructions, tools, and provider session
  affinity without duplicate inference.
- Lossless WebSocket receive handoff after `response.completed`, preventing an
  immediately sent tool result or continuation from being cancelled and lost.
- Full-request tool-catalog retention when an upstream session receives only an
  incremental input suffix, preventing earlier `additional_tools` metadata from
  becoming a misleading empty tool list.
- Codex-native progress semantics for bridged providers: pre-tool commentary
  streams as `phase=commentary`, final messages use `phase=final_answer`, real
  `update_plan` calls can drive the native step indicator, and tool payloads
  remain buffered until full validation.
- Codex-style tool scheduling guidance in bridge prompts: batch independent
  nested operations through one exec/Promise.all payload, group independent
  top-level calls only when permitted, and keep dependencies and conflicting
  writes sequential.
- Kiro upstream-status recovery: plain, wrapped, mixed context-overflow, and
  truncated Bridge statuses are withheld across arbitrary stream boundaries;
  the task mapping is reset and one fresh request is retried with a 768-KiB
  retained-history cap. A repeated status ends with a terminal failure.
- Provider-reported token usage only; Kiro and Cursor backends without real
  per-run usage now return `usage: null` instead of a bridge character-count
  estimate that can spuriously trigger Codex compaction.
- Fast rejection of accidental native `compaction_trigger` requests before any
  Kiro/Cursor invocation, complementing the dedicated non-OpenAI provider
  identity.
- Request-shape diagnostics distinguish empty top-level `tools` from the
  effective catalog reconstructed from `additional_tools`.
- Content-free Kiro timing diagnostics for semaphore wait, process startup,
  first stdout, first visible text, and total stream duration.
- Incremental text from validated Kiro resume envelopes while split bridge
  marker prefixes remain withheld, unrelated short text is released
  immediately, and tool calls remain fully buffered.
- Four-MiB Kiro/Cursor prompt defaults with transport-metadata compaction and
  complete-turn history trimming; unfit active turns return `invalid_prompt`.
- Cursor CLI default-model requests now follow the Codex model/effort when an
  advertised variant exists, parse 272K/1M context metadata, trim to a
  model-aware prompt budget, and terminate silent runs with a redacted timing
  record plus an explicit `invalid_prompt` error.
- Cursor CLI no longer pins bridge requests to read-only Ask mode. It uses the
  default Agent generation mode in a managed sandboxed workspace with native
  Shell, Read, Write, and MCP permissions denied; old Ask-mode session mappings
  are invalidated, while all real tools remain owned by outer Codex.
- Bounded identity/gzip/deflate/zstd HTTP request decoding for Codex fallback,
  without modifying the user's request-compression setting.
- Repository hygiene scanner, security-focused Ruff rules, pre-commit hooks,
  package metadata checks, and a documented release checklist.

### Fixed

- Kiro Direct now encodes Codex namespace tool names into deterministic,
  collision-safe 64-character aliases, preserves those aliases across tool
  result continuations, and restores the original wire name and namespace in
  Responses output items.
- Kiro Direct no longer treats an upstream EOF after a progress update as a
  successful final answer. An internal completion tool distinguishes real final
  answers from client-tool rounds, one bounded corrective continuation is
  attempted, and a second incomplete stop fails explicitly with content-free
  terminal-action diagnostics.
- Provider capacity accounting now performs cancellation-sensitive token and
  counter transitions without intermediate awaits, preventing a cancelled
  waiter or lease release from permanently losing a concurrency slot.

## [0.1.0] - 2026-07-17

### Added

- Responses-compatible Kiro CLI and Cursor Cloud Agents adapters.
- Incremental SSE text streaming with validated terminal responses.
- Per-Codex-task Kiro Session and Cursor Agent affinity with subagent isolation.
- Exact Kiro reasoning-effort forwarding and Cursor-advertised model variants.
- Local provider-switching dashboard and content-free upstream inspector.
- Atomic, permission-restricted configuration with environment-key override.
- Request, prompt, upstream output, and SSE payload limits.
- Loopback/Host/origin protections, bearer-token option, and security headers.
- Layered package structure, tests, documentation, CI, and release metadata.
