# Security Policy

Codex Provider Switchboard handles model prompts, local CLI execution, provider
session identifiers, optional provider keys, and the user's Codex config.
Security changes are
treated as product changes, not incidental cleanup.

## Supported versions

The project is pre-1.0. Only the latest release and current `main` branch
receive security fixes.

## Reporting a vulnerability

Use the repository's private GitHub vulnerability-reporting flow or Security
Advisories. Do not open a public issue containing credentials, prompts, local
paths, exploit details, or unredacted logs. Include:

- the affected version or commit;
- the smallest reproducible request;
- expected and observed trust-boundary behavior;
- whether a credential, provider account, or local file was exposed.

If private reporting is not enabled yet, contact the repository owner through
a private channel and wait for a coordinated disclosure path.

Never submit a live API key. Revoke any credential that was included in a bug
report, terminal transcript, screenshot, commit, or chat message.

## Trust boundaries

The default deployment assumes:

- the host user account and Python environment are trusted;
- the service listens on loopback;
- Codex is an authorized local client;
- Kiro CLI is an authorized local executable;
- Cursor Agent CLI is an authorized local executable;
- optional Cursor Cloud Agents or an explicitly configured third-party endpoint
  receives requests only when its provider is active.
- a selected direct provider receives requests only through its fixed official
  HTTPS origin and built-in protocol adapter.
- the optional Pi credential import is initiated by the trusted local user and
  reads only that user's fixed `~/.pi/agent/auth.json` source.

File-backed provider keys are protected with `0600` permissions and an
application-owned `0700` directory. It is not encrypted with macOS Keychain or
another hardware-backed store. Anyone who can act as the host user can read it.
Use `CURSOR_API_KEY` or `THIRD_PARTY_API_KEY` from a trusted process environment
if that fits your secret management model better.

Direct-provider credentials use a separate atomic `credentials.json` file with
the same `0600`/`0700` boundary. Environment variables take precedence. This
file is not an encrypted vault; on shared hosts, use a trusted environment or
OS secret manager. Switchboard never imports or copies `~/.codex/auth.json`.
It can copy an allow-listed subset of credentials from Pi only after an
explicit preview/import action; Pi remains unchanged and is never executed.

## Implemented controls

- Loopback is the default bind; non-loopback startup requires
  `SWITCHBOARD_TOKEN`.
- Bearer-token comparisons use constant-time comparison.
- Loopback mode rejects non-loopback Host headers to reduce DNS rebinding risk.
- Control mutations require same-origin browser requests and no CORS policy is
  installed.
- Cursor CLI receives its key only through `CURSOR_API_KEY` and is pinned to its
  official `https://api2.cursor.sh` endpoint; inherited endpoint overrides are
  removed. The optional Cloud backend is pinned to `https://api.cursor.com`.
- Custom remote origins require HTTPS. Userinfo, query/fragment components,
  cross-origin auxiliary endpoints, and redirects are rejected.
- Direct-provider origins and paths are compiled into an allow-listed catalog.
  OAuth/device flows use fixed endpoints, bounded JSON, no redirects,
  per-platform refresh locks, loopback callbacks, PKCE where supported,
  unguessable login IDs, and redacted status responses.
- Pi import accepts no caller-provided path, uses a bounded no-follow regular
  file read, requires no group/world permission bits, validates provider and
  credential schemas, skips duplicate target mappings and environment
  overrides, and returns no secret values. Existing Switchboard values are not
  replaced unless the local user explicitly confirms overwrite.
- Direct Responses and Anthropic streams release only complete JSON SSE frames.
  Kiro direct mode validates AWS event-stream lengths and both CRC checks before
  decoding payload JSON. Tool arguments are emitted only after complete JSON
  object validation.
- Codex config takeover requires exact typed confirmation, creates a complete
  timestamped backup, and records only the fields it changes. Restore verifies
  and restores those fields into the latest file, preserving unrelated Codex
  automatic changes. Managed-field conflicts are never overwritten.
- Configuration writes are atomic and permission-restricted.
- Kiro and Cursor CLI prompts are sent over stdin, not command-line arguments.
- Cursor CLI uses default Agent generation mode without `--force`/`--yolo`, in
  a dedicated sandboxed bridge workspace whose project permissions deny native
  Shell, Read, Write, and MCP calls. Outer Codex remains the tool executor.
- HTTP bodies, decoded gzip/deflate/zstd payloads, WebSocket request frames, CLI
  stdout/NDJSON, JSON responses, and SSE/WebSocket events have byte and
  per-line limits. Decompression stops at the configured decoded-size bound.
- Rotating logs contain request shapes and hashes, not prompt text, tool
  payloads, provider bodies, auth headers, session IDs, or credentials. A final
  formatter redacts credential patterns, bridge nonces, and UUID-like values;
  files are `0600` inside an application-owned `0700` directory.
- Session paths use SHA-256 task-key digests. Mapping files store item
  fingerprints instead of prompt text.
- Returned tool calls are checked against the request's tool catalog, and
  function arguments must be JSON objects.
- Resumed provider prompts materialize that catalog from the full logical
  request, so trimming an incremental input prefix cannot silently remove the
  authorization boundary carried by Codex `additional_tools` metadata.
- Bridge control markers are never legal assistant text. Streaming parsing
  withholds split marker prefixes, rejects stale/nested/malformed envelopes,
  and emits tool calls only after complete validation. Contaminated resumed
  Kiro mappings are deleted before one full-context fresh-session retry.
- Overlong bridge history is trimmed only at complete user-turn boundaries.
  The active turn is never partially cut; an unfit active turn returns the
  terminal `invalid_prompt` code before a CLI is started.
- An upstream Kiro context-overflow status is not accepted as assistant text.
  One recovery retry uses a fresh session and a smaller complete-turn history
  bound; a second overflow fails terminally.
- Token usage is emitted only when an upstream reports it. Character-count
  guesses are not exposed as model usage because Codex uses that field for
  context and compaction decisions.
- Native remote compaction triggers are rejected before an upstream invocation;
  opaque OpenAI compaction items are never forged.
- WebSocket output is produced only from complete, validated JSON SSE records;
  partial upstream records are never copied into client text frames.
- WebSocket previous-response state is bounded by the normal body limits, keeps
  only the latest response in connection-local memory, and is replaced after
  each completed continuation. Uncached IDs fail instead of silently losing
  context.
- The dashboard uses local static assets, `textContent` for upstream labels,
  Content Security Policy, frame denial, no-referrer, and no-store controls.
- Quota responses expose only normalized scalar fields; arbitrary upstream JSON
  is not returned to the dashboard.

## Operational guidance

- Keep the service on loopback unless a remote deployment has a reviewed TLS,
  authentication, firewall, and reverse-proxy design.
- Do not expose the control panel directly to an untrusted network.
- Run one process. Multiple processes can race on provider choice and session
  mapping even though individual writes are atomic.
- Rotate provider keys periodically and immediately after suspected exposure.
- Review upstream provider retention, code-access, sandbox, billing, and tool
  execution policies. Both Cursor CLI and Cloud Agents are agent products, not
  only text-generation endpoints.
- Pin and review dependency updates. Run the test suite and secret scan before
  every release.

## Security non-goals

This project does not:

- sandbox Kiro or Cursor CLI beyond the controls those products implement;
- guarantee stability of experimental subscription OAuth or editor-internal
  provider endpoints such as GitHub Copilot and direct Kiro;
- audit or guarantee an upstream model's compliance with the bridge prompt;
- replace provider authentication, billing, authorization, or retention rules;
- encrypt secrets against the host user;
- provide multi-tenant isolation;
- make a non-loopback HTTP deployment safe without additional infrastructure.
