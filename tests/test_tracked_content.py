"""Tracked files hold no real email address and no home-directory path.

The second test that looks at the repository rather than the command line's
output: every text file git tracks (``git ls-files``; binary files and
``uv.lock`` are skipped) must use only reserved example domains in email
addresses, and must not contain a path inside someone's home folder. A
failure names the file and line, so a commit that would publish personal data
is caught before it is pushed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

SKIPPED_FILES = {"uv.lock"}

# Reserved for documentation and testing (RFC 2606, RFC 6761).
RESERVED_TLDS = {"example", "test", "invalid", "localhost"}
RESERVED_DOMAINS = {"example.com", "example.org", "example.net"}

# Placeholders the docs use for the user's folder, e.g. C:\Users\<you>\.
HOME_PLACEHOLDERS = {"<you>"}

# A local part is required, so decorators (@pytest.mark.parametrize) do not match.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
# /Users/<you>/, /home/<you>/ or C:\Users\<you>\ with a real folder name in place of <you>
# (the Windows form also with forward or doubled slashes).
HOME = re.compile(r"(?:/(?:Users|home)/|\b[A-Za-z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/))([^/\\\s\"'`]+)")


def _tracked_text_files() -> list[tuple[str, str]]:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True)
    if proc.returncode != 0:
        pytest.skip(f"git ls-files failed (not a git checkout?): {proc.stderr.decode(errors='replace').strip()}")
    files = []
    for name in proc.stdout.decode().split("\0"):
        if not name or name in SKIPPED_FILES:
            continue
        path = REPO / name
        if not path.is_file():  # deleted in the working tree
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            files.append((name, data.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return files


def _logical_lines(text: str):
    """Lines with quoted-printable soft line breaks (a trailing "=") joined, so an
    address split across lines in test.mbox is seen whole. Yields (first line number, text)."""
    start, parts = None, []
    for number, line in enumerate(text.splitlines(), start=1):
        if start is None:
            start = number
        if line.endswith("="):
            parts.append(line[:-1])
            continue
        parts.append(line)
        yield start, "".join(parts)
        start, parts = None, []
    if parts:
        yield start, "".join(parts)


def _reserved(domain: str) -> bool:
    domain = domain.lower()
    if domain.rsplit(".", 1)[-1] in RESERVED_TLDS:
        return True
    return any(domain == d or domain.endswith("." + d) for d in RESERVED_DOMAINS)


def test_email_addresses_use_reserved_example_domains():
    found = []
    for name, text in _tracked_text_files():
        for number, line in _logical_lines(text):
            for match in EMAIL.finditer(line):
                if not _reserved(match.group(1)):
                    found.append(f"{name}:{number}: {match.group(0)}")
    assert not found, "email addresses outside the reserved example domains:\n" + "\n".join(found)


def test_no_home_directory_paths():
    found = []
    for name, text in _tracked_text_files():
        for number, line in _logical_lines(text):
            for match in HOME.finditer(line):
                if match.group(1) not in HOME_PLACEHOLDERS:
                    found.append(f"{name}:{number}: {match.group(0)}")
    assert not found, "home-directory paths in tracked files:\n" + "\n".join(found)
