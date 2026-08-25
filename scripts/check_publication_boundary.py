#!/usr/bin/env python3
"""Fail closed when tracked files cross the repository's public boundary."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


BLOCKED_PREFIXES = (
    ".aws/",
    ".ssh/",
    "artifacts/",
    "data/curated/",
    "data/raw/",
    "private/",
    "secrets/",
)
BLOCKED_BASENAMES = (
    ".netrc",
    ".npmrc",
    ".pypirc",
)
BLOCKED_SUFFIXES = (
    ".jks",
    ".key",
    ".kdbx",
    ".mobileprovision",
    ".p12",
    ".pem",
    ".pfx",
)
REQUIRED_IGNORES = {
    ".env",
    ".env.*",
    "artifacts/",
    "data/curated/",
    "data/raw/",
    "private/",
    "secrets/",
}
SECRET_PATTERNS = (
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def active_ignore_rules(root: Path) -> set[str]:
    rules: set[str] = set()
    for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("!"):
            rules.add(stripped)
    return rules


def path_errors(paths: list[str]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        basename = path.rsplit("/", 1)[-1]
        if any(path.startswith(prefix) for prefix in BLOCKED_PREFIXES):
            errors.append(f"tracked private path: {path}")
        if basename in BLOCKED_BASENAMES:
            errors.append(f"tracked credential configuration: {path}")
        is_private_env = basename == ".env" or (
            basename.startswith(".env.") and basename != ".env.example"
        )
        if is_private_env:
            errors.append(f"tracked environment file: {path}")
        if basename.lower().endswith(BLOCKED_SUFFIXES):
            errors.append(f"tracked credential container: {path}")
    return errors


def content_errors(root: Path, paths: list[str]) -> list[str]:
    errors: list[str] = []
    local_markers = ("/" + "Users/", "/" + "home/", "C:" + "\\Users\\")
    private_key_markers = tuple(
        "-" * 5 + "BEGIN " + key_type + "-" * 5
        for key_type in (
            "PRIVATE KEY",
            "DSA PRIVATE KEY",
            "EC PRIVATE KEY",
            "OPENSSH PRIVATE KEY",
            "RSA PRIVATE KEY",
        )
    )

    for path in paths:
        file_path = root / path
        if file_path.is_symlink():
            errors.append(f"tracked symbolic link: {path}")
            continue
        if not file_path.is_file() or file_path.stat().st_size > 2_000_000:
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(marker in content for marker in local_markers):
            errors.append(f"local absolute path in tracked file: {path}")
        if any(marker in content for marker in private_key_markers):
            errors.append(f"private key material in tracked file: {path}")
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(content):
                errors.append(f"{label} candidate in tracked file: {path}")
    return errors


def check(root: Path) -> list[str]:
    paths = tracked_files(root)
    errors = path_errors(paths)
    errors.extend(content_errors(root, paths))
    missing_ignores = sorted(REQUIRED_IGNORES - active_ignore_rules(root))
    errors.extend(
        f"required .gitignore rule missing: {rule}" for rule in missing_ignores
    )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()

    try:
        errors = check(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"publication boundary: unable to inspect repository: {exc}",
            file=sys.stderr,
        )
        return 2

    if errors:
        for error in errors:
            print(f"publication boundary: {error}", file=sys.stderr)
        return 1

    print("publication boundary: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
