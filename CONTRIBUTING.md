# Contributing

Thank you for improving Codex Provider Switchboard. Keep changes small,
testable, and explicit about provider assumptions.

## Development setup

```bash
uv sync --locked --all-groups
uv run pre-commit install
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_repository.py
```

Use Python 3.11 or newer. Do not run live Cursor or Kiro requests from automated
tests. HTTP behavior belongs behind `httpx.MockTransport`; CLI behavior belongs
behind a fake runner.

## Architecture rules

- `domain/` contains pure protocol logic and must not import FastAPI, httpx, or
  subprocess code.
- `application/` owns routing and use-case orchestration, not vendor transports.
- `infrastructure/` owns files, subprocesses, and external HTTP.
- `providers/` adapts one vendor to the common Responses interface.
- `web/` validates and delivers HTTP; it must not implement vendor workflows.
- `runtime.py` is the composition root. Dependencies should remain injectable.

New providers should implement `ResponsesProvider`, receive dependencies in
their constructor, use their own session namespace, and expose no credentials
through status payloads.

## Code quality

- Add type hints to public functions and provider boundaries.
- Prefer bounded input/output and explicit validation at every trust boundary.
- Log metadata, never prompts, tool payloads, API keys, bearer tokens, or raw
  provider responses.
- Preserve the OpenAI Responses event order and monotonically increasing
  `sequence_number` values.
- Add unit tests for mapping/translation and integration tests for HTTP flow.
- Update English and Chinese README sections when user-facing behavior changes.

## Pull requests

A pull request should include:

- the user-visible outcome and motivation;
- security and compatibility impact;
- tests added or changed;
- upstream documentation links for vendor API changes;
- confirmation that formatting, tests, build, and secret scans pass.

Do not include personal paths, captured provider output, runtime state, or real
credentials in fixtures, screenshots, commits, or issue descriptions.

Before opening a pull request, run every command in the verification section of
[`AGENTS.md`](AGENTS.md). The repository hygiene check reports only the rule,
file, and line number; it deliberately never prints a suspected credential.
