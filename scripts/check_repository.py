"""Fail safely when publishable repository files contain sensitive shapes."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIPPED_PARTS = frozenset(
    {".git", ".venv", ".pytest_cache", ".ruff_cache", "build", "dist"}
)
SENSITIVE_FILENAMES = frozenset(
    {
        ".env",
        "auth.json",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pfx", ".pem"})

TEXT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "personal macOS home path",
        re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    ),
    (
        "personal Windows home path",
        re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._ -]+\\"),
    ),
    (
        "Cursor credential shape",
        re.compile(r"\bcrsr_[A-Za-z0-9_-]{32,}\b"),
    ),
    (
        "OpenAI credential shape",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "provider credential shape",
        re.compile(r"\b(?:su8-|mr_)[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "GitHub credential shape",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    ),
    (
        "AWS access key shape",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "Slack credential shape",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    ),
    (
        "private key block",
        re.compile(r"-{5}BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-{5}"),
    ),
)

SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|token|secret|password|experimental_bearer_token)\b"
    r"\s*(?:=|:)\s*[\"']?([^\"'\s,#}\]]+)"
)
SAFE_ASSIGNMENT_PREFIXES = (
    "$",
    "<",
    "changeme",
    "example",
    "none",
    "null",
    "replace",
    "test",
    "unset",
    "your",
)


def _candidate_files() -> list[Path]:
    git = shutil.which("git")
    if git:
        result = subprocess.run(  # noqa: S603 - fixed executable and arguments
            [git, "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        relative_paths = [
            Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw
        ]
        return sorted(
            ROOT / path
            for path in relative_paths
            if not any(part in SKIPPED_PARTS for part in path.parts)
        )
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not any(part in SKIPPED_PARTS for part in path.parts)
    )


def _safe_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        not normalized
        or normalized.startswith(SAFE_ASSIGNMENT_PREFIXES)
        or any(character in normalized for character in "()[]{}")
    )


def _scan_text(path: Path, text: str) -> list[tuple[str, int]]:
    findings: list[tuple[str, int]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for rule_name, pattern in TEXT_RULES:
            if pattern.search(line):
                findings.append((rule_name, line_number))
        for match in SECRET_ASSIGNMENT.finditer(line):
            value = match.group(1)
            if len(value) >= 16 and not _safe_placeholder(value):
                findings.append(("non-placeholder secret assignment", line_number))
    return findings


def main() -> int:
    findings: list[tuple[str, Path, int | None]] = []
    files = _candidate_files()
    for path in files:
        relative = path.relative_to(ROOT)
        lower_name = path.name.lower()
        if (
            lower_name in SENSITIVE_FILENAMES
            or path.suffix.lower() in SENSITIVE_SUFFIXES
        ):
            findings.append(("sensitive filename", relative, None))
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            findings.append(("unreadable repository file", relative, None))
            continue
        if b"\0" in raw[:8_192]:
            continue
        text = raw.decode("utf-8", errors="replace")
        findings.extend(
            (rule, relative, line_number)
            for rule, line_number in _scan_text(path, text)
        )

    if findings:
        print("Repository hygiene check failed:", file=sys.stderr)
        for rule, path, line_number in findings:
            location = f"{path}:{line_number}" if line_number else str(path)
            print(f"- {rule}: {location}", file=sys.stderr)
        print("Matched values are intentionally not printed.", file=sys.stderr)
        return 1

    print(f"Repository hygiene check passed ({len(files)} files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
