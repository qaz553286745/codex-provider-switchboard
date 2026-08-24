# Release guide

[English](https://github.com/qaz553286745/codex-provider-switchboard/blob/main/RELEASING.md) |
[简体中文](https://github.com/qaz553286745/codex-provider-switchboard/blob/main/RELEASING.zh-CN.md)

Releases are built only from a clean, reviewed commit. Never publish from a
working directory containing provider credentials, captured runtime state,
personal paths, or a real Codex configuration.

## One-time package-index setup

Use PyPI Trusted Publishing instead of storing a long-lived upload token in
GitHub. Configure one publisher on both PyPI and TestPyPI with these values:

| Setting | Value |
| --- | --- |
| GitHub owner | `qaz553286745` |
| Repository | `codex-provider-switchboard` |
| Workflow | `release.yml` |
| Environment | `pypi` on PyPI; `testpypi` on TestPyPI |

Create matching GitHub environments. Protect the `pypi` environment with
required reviewers so every production upload has a manual approval gate.
TestPyPI is the default target for manually dispatched workflows. A pushed
`v*` tag targets production PyPI.

The package name appeared unregistered when this workflow was authored, so the
first production release may need a pending trusted publisher. Recheck the name
immediately before publication; an HTTP 404 is not a reservation.

## Prepare a version

1. Update the version in `pyproject.toml` and
   `src/codex_provider_switchboard/__init__.py`.
2. Move relevant `Unreleased` entries into a dated section in `CHANGELOG.md`.
3. Keep user-visible changes consistent across `README.md`,
   `README.zh-CN.md`, and `docs/`.
4. Review `git status --short`, the complete diff, and the final commit.

## Build with a specified Python

The release helper accepts a Python version request or interpreter path. It
refuses a dirty worktree by default, verifies version/tag metadata, clears only
the repository-local output directory, builds one sdist and one universal
wheel, runs Twine and archive hygiene checks, installs the wheel into a fresh
environment using the same Python request, and smoke-tests the CLI.

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_repository.py
uv run pytest --cov
uv run python scripts/build_release.py --python 3.11
```

For an explicitly non-publishable check of current uncommitted work, add
`--allow-dirty`. Do not use that flag for a package-index upload.

## TestPyPI and production

1. Push the reviewed release commit without a tag.
2. In GitHub Actions, dispatch **Release** with the intended Python version and
   the default `testpypi` target.
3. Install from TestPyPI in a clean environment and check the rendered package
   description. Dependencies may still need the production PyPI index.
4. Create the exact tag `v<project-version>` on the already-reviewed commit and
   push it. The workflow rejects a version mismatch.
5. Approve the protected `pypi` environment, then verify the package page and
   immutable file hashes.

The workflow passes only `dist/*` from the isolated build job to the publish
job. The publish job has no source checkout, receives only `id-token: write`,
and uses the package index's short-lived OpenID Connect identity.

## Final checks

- Confirm the source archive includes both language versions and excludes
  `AGENTS.md`, credentials, caches, runtime mappings, and logs.
- Confirm the wheel is `py3-none-any` and declares `Requires-Python: >=3.11`.
- Confirm `codex-provider-switchboard --version` and `--help` work in the clean
  install environment.
- Create a GitHub release from the same tag and attach or reference the exact
  package-index artifacts.
