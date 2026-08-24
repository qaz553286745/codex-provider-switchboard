"""Inspect release archives without extracting or printing sensitive values."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from check_repository import (
    SENSITIVE_FILENAMES,
    SENSITIVE_SUFFIXES,
    _scan_text,
)

MAX_MEMBER_BYTES = 16 * 1_048_576
MAX_ARCHIVE_BYTES = 128 * 1_048_576
REQUIRED_WHEEL_SUFFIXES = (
    "/py.typed",
    "/web/static/app.css",
    "/web/static/app.js",
    "/web/static/index.html",
    ".dist-info/METADATA",
)
REQUIRED_SDIST_SUFFIXES = (
    "/LICENSE",
    "/README.md",
    "/README.zh-CN.md",
    "/RELEASING.md",
    "/RELEASING.zh-CN.md",
    "/demo_config.toml",
    "/pyproject.toml",
)
FORBIDDEN_ARCHIVE_SUFFIXES = ("/AGENTS.md",)


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
    )


def _scan_member(
    artifact: Path, name: str, raw: bytes
) -> list[tuple[str, str, int | None]]:
    display = f"{artifact.name}!{name}"
    findings: list[tuple[str, str, int | None]] = []
    if not _safe_member_name(name):
        return [("unsafe archive member path", display, None)]
    member_path = PurePosixPath(name)
    lower_name = member_path.name.lower()
    if (
        lower_name in SENSITIVE_FILENAMES
        or member_path.suffix.lower() in SENSITIVE_SUFFIXES
    ):
        return [("sensitive filename", display, None)]
    if b"\0" in raw[:8_192]:
        return findings
    text = raw.decode("utf-8", errors="replace")
    findings.extend(
        (rule, display, line_number)
        for rule, line_number in _scan_text(Path(member_path.name), text)
    )
    return findings


def _scan_wheel(path: Path) -> tuple[list[tuple[str, str, int | None]], set[str]]:
    findings: list[tuple[str, str, int | None]] = []
    names: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                names.add(member.filename)
                total += member.file_size
                if member.file_size > MAX_MEMBER_BYTES or total > MAX_ARCHIVE_BYTES:
                    findings.append(
                        (
                            "archive size limit exceeded",
                            f"{path.name}!{member.filename}",
                            None,
                        )
                    )
                    continue
                findings.extend(
                    _scan_member(path, member.filename, archive.read(member))
                )
    except (OSError, zipfile.BadZipFile):
        findings.append(("invalid wheel archive", path.name, None))
    return findings, names


def _scan_sdist(path: Path) -> tuple[list[tuple[str, str, int | None]], set[str]]:
    findings: list[tuple[str, str, int | None]] = []
    names: set[str] = set()
    total = 0
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.isdir():
                    continue
                names.add(member.name)
                display = f"{path.name}!{member.name}"
                if member.issym() or member.islnk():
                    findings.append(("archive link is not allowed", display, None))
                    continue
                if not member.isfile():
                    findings.append(("unexpected archive member type", display, None))
                    continue
                total += member.size
                if member.size > MAX_MEMBER_BYTES or total > MAX_ARCHIVE_BYTES:
                    findings.append(("archive size limit exceeded", display, None))
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    findings.append(("unreadable archive member", display, None))
                    continue
                findings.extend(_scan_member(path, member.name, extracted.read()))
    except (OSError, tarfile.TarError):
        findings.append(("invalid source archive", path.name, None))
    return findings, names


def _missing_suffixes(names: set[str], required: tuple[str, ...]) -> list[str]:
    return [
        suffix
        for suffix in required
        if not any(name.endswith(suffix) for name in names)
    ]


def _forbidden_suffixes(names: set[str]) -> list[str]:
    return [
        suffix
        for suffix in FORBIDDEN_ARCHIVE_SUFFIXES
        if any(name.endswith(suffix) for name in names)
    ]


def main() -> int:
    directory = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("dist")
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    findings: list[tuple[str, str, int | None]] = []
    scanned_members = 0
    if len(wheels) != 1:
        findings.append(("expected exactly one wheel", str(directory), None))
    if len(sdists) != 1:
        findings.append(("expected exactly one source archive", str(directory), None))

    for wheel in wheels:
        wheel_findings, names = _scan_wheel(wheel)
        findings.extend(wheel_findings)
        scanned_members += len(names)
        findings.extend(
            ("required wheel member missing", f"{wheel.name}!{suffix}", None)
            for suffix in _missing_suffixes(names, REQUIRED_WHEEL_SUFFIXES)
        )
    for sdist in sdists:
        sdist_findings, names = _scan_sdist(sdist)
        findings.extend(sdist_findings)
        scanned_members += len(names)
        findings.extend(
            ("required source member missing", f"{sdist.name}!{suffix}", None)
            for suffix in _missing_suffixes(names, REQUIRED_SDIST_SUFFIXES)
        )
        findings.extend(
            ("forbidden source member present", f"{sdist.name}!{suffix}", None)
            for suffix in _forbidden_suffixes(names)
        )

    if findings:
        print("Release artifact check failed:", file=sys.stderr)
        for rule, location, line_number in findings:
            suffix = f":{line_number}" if line_number else ""
            print(f"- {rule}: {location}{suffix}", file=sys.stderr)
        print("Matched values are intentionally not printed.", file=sys.stderr)
        return 1

    print(f"Release artifact check passed ({scanned_members} members scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
