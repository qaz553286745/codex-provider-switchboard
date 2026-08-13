# Configuration

Runtime settings come from environment variables. Provider choice, model
selection, and optional file-backed keys are stored in a per-user JSON file
managed by the control panel.

## Service environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `SWITCHBOARD_HOST` | `127.0.0.1` | HTTP bind address |
| `SWITCHBOARD_PORT` | `8787` | HTTP bind port |
| `SWITCHBOARD_TOKEN` | unset | Bearer token; required for non-loopback bind |
| `SWITCHBOARD_CONFIG` | platform path | Override JSON configuration path |
| `SWITCHBOARD_MAX_REQUEST_BYTES` | `8388608` | Maximum wire and decoded JSON request body |
| `SWITCHBOARD_DEBUG_REQUESTS` | `0` | Log content-free request metadata |
| `SWITCHBOARD_CODEX_CONFIG` | `~/.codex/config.toml` | Test/automation override for Codex config management |
| `SWITCHBOARD_LOG_PATH` | per-user application data | Redacted rotating log file |
| `SWITCHBOARD_LOG_MAX_BYTES` | `5242880` | Bytes per log file before rotation |
| `SWITCHBOARD_LOG_BACKUP_COUNT` | `4` | Rotated history files to retain |

The earlier `KIRO_PROXY_HOST`, `KIRO_PROXY_PORT`, `KIRO_PROXY_TOKEN`,
`KIRO_PROXY_CONFIG`, and `KIRO_DEBUG_REQUESTS` names are accepted as migration
aliases. New deployments should use `SWITCHBOARD_*`.

## Kiro environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `KIRO_CLI` | `kiro-cli` | CLI executable name or path |
| `KIRO_MODEL` | `gpt-5.6-sol` | Initial Kiro model before dashboard selection |
| `KIRO_WORKDIR` | app runtime directory | Root for isolated task workdirs |
| `KIRO_TIMEOUT_SECONDS` | `300` | Whole invocation deadline |
| `KIRO_MAX_CONCURRENCY` | `4` | Maximum simultaneous Kiro generation processes |
| `KIRO_QUEUE_TIMEOUT_SECONDS` | `60` | Maximum FIFO wait for an available Kiro generation slot |
| `KIRO_MAX_PROMPT_BYTES` | `4194304` | Rendered prompt limit after safe history compaction |
| `KIRO_CONTEXT_RECOVERY_PROMPT_BYTES` | `786432` | Fresh-session prompt cap for one automatic Kiro overflow/truncation recovery |
| `KIRO_MAX_OUTPUT_BYTES` | `8388608` | Stdout limit |
| `KIRO_ALLOW_REQUESTED_MODEL` | `0` | Allow request `model` to override `KIRO_MODEL` |
| `KIRO_SESSION_REUSE` | `1` | Enable task-to-session affinity for Kiro and Cursor CLI |
| `KIRO_SESSION_TTL_SECONDS` | `604800` | Mapping TTL; `0` disables expiry |

Reasoning effort is taken from `reasoning.effort` or `reasoning_effort`.
`none`/`minimal` map to Kiro `low`; `extra_high` maps to `xhigh`; and
`max`/`ultra` map to `max`.

Different Codex tasks and subagents have separate session locks and workdirs.
The default `KIRO_MAX_CONCURRENCY=4` allows up to four expensive CLI generations
to run concurrently. Requests beyond that limit wait in FIFO order for up to
`KIRO_QUEUE_TIMEOUT_SECONDS`; turns within the same task remain serialized to protect
session order. Apply the same rule to `CURSOR_CLI_MAX_CONCURRENCY` for local
Cursor.

## Cursor environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `CURSOR_API_KEY` | unset | Overrides the key stored in JSON |
| `CURSOR_AGENT_CLI` | `cursor-agent` | Local Cursor Agent executable name or path |
| `CURSOR_CLI` | unset | Compatibility alias for `CURSOR_AGENT_CLI` |
| `CURSOR_CLI_WORKDIR` | app runtime directory | Controlled working directory for local CLI sessions |
| `CURSOR_CLI_MAX_CONCURRENCY` | `1` | Maximum simultaneous Cursor CLI processes |
| `CURSOR_CLI_MAX_PROMPT_BYTES` | `4194304` | Absolute rendered stdin ceiling; the selected model's advertised 272K/1M context applies a lower, conservative limit automatically |
| `CURSOR_CLI_MAX_OUTPUT_BYTES` | `8388608` | NDJSON stdout limit |
| `CURSOR_CLI_FIRST_OUTPUT_TIMEOUT_SECONDS` | `120` | Terminal timeout when the CLI emits no assistant/result event; returned as `invalid_prompt` instead of a disconnected stream |

`cursor.backend` defaults to `cli`. The CLI receives the key through its
documented `CURSOR_API_KEY` environment variable, is pinned to the official
`https://api2.cursor.sh` endpoint, receives prompts only on stdin, runs in
the CLI's default Agent generation mode, and emits bounded `stream-json`
events. Switchboard places that Agent in a dedicated sandboxed bridge workspace
whose project permissions deny native Shell, Read, Write, and MCP calls. Cursor
therefore generates inert outer-Codex tool envelopes without executing those
actions itself; users do not need to select a Cursor mode.
Set the backend to `cloud_api` to use the optional durable Agent/run adapter.
For that backend, the Cursor API origin is fixed to `https://api.cursor.com`
and has no environment override, preventing a configuration mistake from
sending the key to another host.

## Custom provider environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `THIRD_PARTY_API_KEY` | unset | Preferred override for the custom provider key |
| `CUSTOM_API_KEY` | unset | Compatibility alias for the same key |

The custom Base URL is user-selected, but remote URLs require HTTPS and cannot
contain userinfo, query strings, or fragments. Model and quota endpoints must
be same-origin paths beginning with `/`; HTTP redirects are not followed.

## Native direct-provider environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | unset | OpenAI API key; overrides the direct credential file |
| `ANTHROPIC_API_KEY` | unset | Anthropic API key override |
| `XAI_API_KEY` | unset | xAI API key override |
| `OPENROUTER_API_KEY` | unset | OpenRouter API key override |
| `SWITCHBOARD_DIRECT_MAX_CONCURRENCY` | `8` | Maximum simultaneous direct HTTPS requests |
| `SWITCHBOARD_DIRECT_MAX_OUTPUT_BYTES` | `67108864` | Per-response stream byte ceiling |
| `SWITCHBOARD_OAUTH_TIMEOUT_SECONDS` | `900` | Whole interactive login deadline |
| `SWITCHBOARD_OAUTH_CALLBACK_HOST` | `127.0.0.1` | Loopback-only OAuth callback listener |

The direct catalog fixes every provider origin and protocol; Base URLs cannot
be changed through JSON or the dashboard. API keys and OAuth refresh state are
stored in a separate `credentials.json`, not in `config.json`, and are never
returned by the API. Switchboard does not read `~/.codex/auth.json`. The file
uses `0600` permissions but is not encrypted by an OS keychain.

The only local credential import is an explicit dashboard/API action for
experimental direct Kiro. Source `pi` maps to the fixed
`~/.pi/agent/auth.json` file; callers cannot supply a path. The importer uses
bounded, symlink-resistant reads, validates the Pi Kiro OAuth schema, separates
the packed refresh token from its client metadata, and writes through the same
atomic `0600` credential store. Kiro CLI does not expose a supported
credential-export contract, so Switchboard does not guess at or scrape its
private storage.

API-key access for OpenAI, Anthropic, xAI, and OpenRouter is the preferred
stable path. ChatGPT Codex, subscription OAuth, GitHub Copilot, and direct Kiro
carry explicit experimental labels. Kiro CLI remains the recommended Kiro
compatibility route. Cursor remains on the official `cursor-agent` or optional
Cloud Agents API path.

## Configuration file

Default locations:

- macOS: `~/Library/Application Support/codex-provider-switchboard/config.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/codex-provider-switchboard/config.json`
- Windows: `%APPDATA%\codex-provider-switchboard\config.json`

The managed shape is shown in [`examples/config.example.json`](../examples/config.example.json).
The dashboard creates the parent directory with `0700` permissions and writes
the file atomically with `0600` permissions. If you copy an example manually,
apply restrictive permissions yourself:

```bash
chmod 700 "$(dirname "$SWITCHBOARD_CONFIG")"
chmod 600 "$SWITCHBOARD_CONFIG"
```

The file contains:

- `active_provider`: `kiro`, `cursor`, `custom`, or `direct`;
- optional Cursor and custom keys;
- the Cursor backend (`cli` by default or optional `cloud_api`);
- the selected Kiro model;
- a model ID and the exact advertised parameter list; CLI mode resolves saved
  effort/fast parameters to a concrete model ID from `cursor-agent --list-models`;
- a display label;
- whether incoming Codex reasoning should update advertised effort/max fields;
- the Cursor run timeout.
- the custom Base URL, model endpoint, and optional quota endpoint/field map.
- the selected fixed direct platform, model, timeout, and effort-following flag.

The sibling `credentials.json` contains only direct-provider secrets and refresh
metadata. Do not commit either file. Prefer environment injection or an OS
secret manager on shared systems.

Unknown control fields are rejected. Cursor model parameters are bounded,
deduplicated, and restricted to JSON scalar values.

## Codex provider configuration

Use the dashboard's confirmed backup/restore workflow, or manually merge
[`demo_config.toml`](../demo_config.toml) into user-level
`~/.codex/config.toml`. Do not put provider routing in a repository's
`.codex/config.toml`; current Codex versions ignore those keys at the project
layer.

The automatic workflow never runs at application startup. Enabling requires
the exact confirmation `ENABLE`; restoring requires `RESTORE`. Every enable is
derived from the config file as it exists at that moment. A complete `0600`
backup is created, but disable validates and restores only the top-level keys,
individual table keys, and provider entry recorded as Switchboard-managed.
Changes to plugins, marketplaces, desktop settings, MCP configuration, and
other unrelated TOML are preserved. Managed-field drift does not block disable:
the verified backup wins only for the recorded managed fields. If that backup
is missing or invalid, confirmed disable removes only routing values that still
match the takeover record and leaves the current model in place. If those
routing values were already removed externally, status automatically clears the
stale active state so another enable can proceed.

When Codex is using its default built-in `openai` provider, the manager selects
the dedicated `codex-provider-switchboard` provider. Current Codex builds infer
remote-compaction support from the provider display name: a provider named
exactly `OpenAI` is expected to return an opaque `compaction` output item for a
`compaction_trigger` request. Switchboard does not claim that native capability,
so retaining the built-in identity would make long tasks fail during automatic
compaction. Existing non-reserved custom provider IDs are retained by
temporarily replacing only their connection entry.

The manager does not change `features.enable_request_compression`. HTTP fallback
accepts bounded `identity`, `gzip`, `deflate`, and `zstd` request bodies, so the
user's compression preference remains intact. Both compressed wire bytes and
decoded JSON are limited by `SWITCHBOARD_MAX_REQUEST_BYTES`.

Kiro and local Cursor prompts are compacted before their byte limit is checked.
Redundant transport metadata is removed and `additional_tools` is deduplicated
into the tool catalog. On a resumed upstream session, that catalog is sourced
from the full logical request even though only the new input suffix is rendered.
If necessary, only the oldest complete user-turn groups are dropped. The newest
user turn and all following tool/result items are never partially truncated. A
request that still cannot fit ends with an explicit non-retryable
`invalid_prompt` failure.

If Kiro returns a context-overflow status, the same status followed by its
output-truncation sentinel, or a partial Bridge prefix marked as truncated, the
adapter withholds the complete control message. Detection works for plain output
and for a valid current Bridge message envelope. The adapter discards any old
mapping and retries once in a fresh session using
`KIRO_CONTEXT_RECOVERY_PROMPT_BYTES`. If the bounded retry still cannot fit or
returns another control status, Codex receives a terminal `invalid_prompt`
instead of a fake assistant answer or disconnected stream. Kiro Responses set
`usage` to `null` because Kiro CLI does not provide authoritative per-run token
counts; bridge prompt character counts are not reported as model tokens.

Process history is written to a permission-restricted rotating file. The file
contains content-free action/status metadata and redacts credential patterns,
bridge markers/nonces, and session-like identifiers. Prompt text, tool payloads,
provider bodies, auth headers, and raw session IDs must never be logged. Kiro
timing entries expose only queue, startup, first-byte/first-visible, total
duration, and byte-count measurements.

Set `SWITCHBOARD_DEBUG_REQUESTS=1` to add content-free `request_metadata`
records. They show body keys, input item types, byte/character counts, and both
`top_level_tool_count` and `effective_tool_count`; they still never contain
prompt text, tool payloads, raw provider bodies, authorization, or session IDs.

Retries are set to zero because retrying a failed HTTP request can create a
second Kiro or Cursor session/run. Session recovery is handled inside the
provider adapter where the upstream ID is known.

## Non-loopback deployments

The CLI refuses to bind a non-loopback address without `SWITCHBOARD_TOKEN`.
Codex can read the token from the environment using `env_key`:

```toml
[model_providers.codex-provider-switchboard]
base_url = "https://switchboard.example/v1"
wire_api = "responses"
env_key = "SWITCHBOARD_TOKEN"
```

This is only an authentication primitive. A remote deployment still needs TLS,
host/firewall policy, a reviewed reverse proxy, monitoring, and an explicit
decision about exposing the control panel. The project is designed primarily
for one trusted user on loopback.
