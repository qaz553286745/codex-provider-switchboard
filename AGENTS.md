# Repository instructions

## Scope

This repository is a local Responses compatibility layer. Preserve protocol
correctness, session affinity, and credential safety over convenience.

## Structure

- Pure translation belongs in `domain/`.
- Use-case routing belongs in `application/`.
- Filesystem, subprocess, and HTTP adapters belong in `infrastructure/`.
- Vendor-specific orchestration belongs in `providers/`.
- FastAPI concerns belong in `web/`.
- Dependency construction belongs only in `runtime.py`.

Do not add vendor logic to route handlers or global mutable runtime state.

## Security

- Never add real or realistic high-entropy keys, tokens, cookies, or personal
  filesystem paths.
- Never log prompt text, tool payloads, session IDs, provider bodies, or auth
  headers.
- Keep Cursor credentials pinned to the official HTTPS origin.
- Send prompts to CLIs through stdin.
- Bound all untrusted input and output.
- Status/config APIs must return only redacted views.
- New control mutations need authentication and same-origin checks.

## Verification

Before handing off a change, run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_repository.py
uv run pytest --cov
uv build
uv run twine check dist/*
uv run python scripts/check_artifacts.py dist
```

Use mocks for all provider tests. Keep `README.md`, `README.zh-CN.md`,
`docs/`, and `CHANGELOG.md` consistent with behavior.
