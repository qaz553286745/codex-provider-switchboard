"""Build and smoke-test release artifacts with an explicit Python runtime."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_INIT = ROOT / "src" / "codex_provider_switchboard" / "__init__.py"


class ReleaseError(RuntimeError):
    pass


def _run(
    arguments: list[str], *, capture_output: bool = False
) -> subprocess.CompletedProcess[str]:
    print(f"+ {shlex.join(arguments)}", flush=True)
    return subprocess.run(  # noqa: S603 - arguments contain no shell input
        arguments,
        cwd=ROOT,
        check=True,
        capture_output=capture_output,
        text=True,
    )


def _project_metadata() -> tuple[str, str, str]:
    with (ROOT / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source)["project"]
    package_name = str(project["name"])
    project_version = str(project["version"])
    requires_python = str(project["requires-python"])
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
        PACKAGE_INIT.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match is None:
        raise ReleaseError("Could not read __version__ from the package.")
    package_version = match.group(1)
    if project_version != package_version:
        raise ReleaseError(
            "pyproject.toml and package __version__ do not match: "
            f"{project_version!r} != {package_version!r}."
        )
    return package_name, project_version, requires_python


def _require_clean_worktree(*, allow_dirty: bool) -> None:
    git = shutil.which("git")
    if git is None:
        raise ReleaseError("git is required for release builds.")
    result = _run(
        [git, "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
    )
    if result.stdout.strip() and not allow_dirty:
        raise ReleaseError(
            "The Git worktree is not clean. Commit/review the release state or use "
            "--allow-dirty only for local validation."
        )
    if result.stdout.strip():
        print("Warning: building from a dirty worktree for local validation only.")


def _safe_output_directory(value: str) -> Path:
    directory = (
        (ROOT / value).resolve()
        if not Path(value).is_absolute()
        else Path(value).resolve()
    )
    if directory == ROOT or not directory.is_relative_to(ROOT):
        raise ReleaseError("The artifact directory must be a child of the repository.")
    current = ROOT
    for part in directory.relative_to(ROOT).parts:
        current /= part
        if current.is_symlink():
            raise ReleaseError(
                "The artifact directory must not traverse a symbolic link."
            )
    return directory


def _single_artifacts(directory: Path) -> tuple[Path, Path]:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseError(
            "Expected exactly one wheel and one source archive, found "
            f"{len(wheels)} wheel(s) and {len(sdists)} source archive(s)."
        )
    return wheels[0], sdists[0]


def _wheel_metadata(wheel: Path) -> tuple[Message, str]:
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise ReleaseError(f"Wheel is not universal py3-none-any: {wheel.name}")
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        wheel_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
        ]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise ReleaseError("Wheel metadata layout is invalid.")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        wheel_data = archive.read(wheel_names[0]).decode("utf-8")
    return metadata, wheel_data


def _verify_wheel(
    wheel: Path, *, package_name: str, version: str, requires_python: str
) -> None:
    metadata, wheel_data = _wheel_metadata(wheel)
    expected = {
        "Name": package_name,
        "Version": version,
        "Requires-Python": requires_python,
    }
    for field, value in expected.items():
        if metadata.get(field) != value:
            raise ReleaseError(
                f"Wheel {field} is {metadata.get(field)!r}; expected {value!r}."
            )
    if (
        "Root-Is-Purelib: true" not in wheel_data
        or "Tag: py3-none-any" not in wheel_data
    ):
        raise ReleaseError("Wheel does not declare the expected pure-Python tag.")


def _smoke_install(uv: str, python_request: str, wheel: Path, version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="switchboard-release-") as temporary:
        environment = Path(temporary) / "venv"
        _run([uv, "venv", "--no-project", "--python", python_request, str(environment)])
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        command = scripts / (
            "codex-provider-switchboard.exe"
            if os.name == "nt"
            else "codex-provider-switchboard"
        )
        _run([uv, "pip", "install", "--python", str(python), str(wheel)])
        runtime = _run(
            [
                str(python),
                "-c",
                "import platform; print(platform.python_version())",
            ],
            capture_output=True,
        ).stdout.strip()
        installed = _run(
            [str(command), "--version"], capture_output=True
        ).stdout.strip()
        help_text = _run([str(command), "--help"], capture_output=True).stdout
        if installed != f"codex-provider-switchboard {version}":
            raise ReleaseError(
                f"Installed CLI reported an unexpected version: {installed!r}"
            )
        if "usage: codex-provider-switchboard" not in help_text:
            raise ReleaseError("Installed CLI help smoke test failed.")
        print(f"Installed-wheel smoke test passed with Python {runtime}.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        default="3.11",
        help="Python request/path used for PEP 517 build and install smoke test.",
    )
    parser.add_argument(
        "--out-dir", default="dist", help="Artifact directory inside the repository."
    )
    parser.add_argument(
        "--expected-tag",
        help="Optional release tag, which must exactly equal v<project-version>.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow a dirty tree for local validation; never use for publication.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    uv = shutil.which("uv")
    if uv is None:
        raise ReleaseError("uv is required to build release artifacts.")
    package_name, version, requires_python = _project_metadata()
    if args.expected_tag is not None and args.expected_tag != f"v{version}":
        raise ReleaseError(
            f"Release tag {args.expected_tag!r} does not match project version "
            f"v{version}."
        )
    _require_clean_worktree(allow_dirty=args.allow_dirty)
    output = _safe_output_directory(args.out_dir)
    _run(
        [
            uv,
            "build",
            "--clear",
            "--no-create-gitignore",
            "--python",
            args.python,
            "--out-dir",
            str(output),
        ]
    )
    wheel, sdist = _single_artifacts(output)
    _verify_wheel(
        wheel,
        package_name=package_name,
        version=version,
        requires_python=requires_python,
    )
    _run([uv, "run", "twine", "check", str(wheel), str(sdist)])
    _run([uv, "run", "python", "scripts/check_artifacts.py", str(output)])
    _smoke_install(uv, args.python, wheel, version)
    for artifact in (wheel, sdist):
        print(f"sha256  {_sha256(artifact)}  {artifact.relative_to(ROOT)}")
    print(f"Release validation passed for {package_name} {version}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ReleaseError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"Release build failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
