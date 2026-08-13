# Release checklist

Releases are built only from a clean, reviewed commit. Do not publish directly
from a working directory that contains provider credentials, captured runtime
state, personal paths, or a real Codex configuration.

## Prepare

1. Update the version in `pyproject.toml` and `src/codex_provider_switchboard/__init__.py`.
2. Move the relevant entries from `Unreleased` into a dated section in
   `CHANGELOG.md`.
3. Confirm that user-visible changes are consistent across `README.md`,
   `README.zh-CN.md`, and `docs/`.
4. Review `git status --short` and the complete staged diff.

## Verify

```bash
uv sync --locked --all-groups
uv run pre-commit run --all-files
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_repository.py
uv run pytest --cov
uv build
uv run twine check dist/*
uv run python scripts/check_artifacts.py dist
```

The repository scan is intentionally conservative, but it is not a substitute
for GitHub secret scanning or a dedicated full-history scanner. Enable those on
the public repository before accepting contributions.

## Inspect and publish

- Review the artifact check output; only source, static assets, metadata, and
  intended documentation should be present.
- Install the wheel into a fresh environment and run the CLI help and health
  check.
- Create a signed or otherwise verified Git tag for the exact release commit.
- Publish from CI with a short-lived trusted-publishing identity when possible;
  do not store a long-lived package index token in the repository.
- Verify the GitHub release and package index render the README correctly.
