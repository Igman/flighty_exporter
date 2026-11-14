"""Files that can hold personal data are ignored by git in this repository.

The one test that looks at the repository rather than the command line's
output: for each default output file name the tool writes (taken from the
config and export defaults), ``git check-ignore`` must say it is ignored.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flighty_exporter.config import DEFAULT_CONFIG_NAME, Config
from flighty_exporter.export import MANUAL_ADD_NAME
from flighty_exporter.openflights import FILE_NAME_FORMAT

REPO = Path(__file__).resolve().parents[1]


def _written_names() -> list[str]:
    defaults = Config()
    store = defaults.store.name
    import_file = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc).strftime(FILE_NAME_FORMAT)
    outputs = [defaults.review_output.name, defaults.failures_output.name, defaults.audit_output.name, import_file,
               MANUAL_ADD_NAME]
    return [
        store, f"{store}-wal", f"{store}-shm", f"{store}-journal",
        DEFAULT_CONFIG_NAME,
        *outputs,
        *(f"{name}.tmp" for name in outputs),  # written first, then renamed
    ]


PERSONAL = [
    *_written_names(),
    # Under a subdirectory too (e.g. [review] output = "out/review.csv").
    *(f"out/{name}" for name in _written_names()),
    # Mailbox files and Takeout archives.
    "mail.mbox",
    "Takeout/Mail/All mail Including Spam and Trash.mbox",
    "archive.mbx",
    "takeout-20260102T030405Z-001.zip",
    "takeout-20260102T030405Z-001.tgz",
]

TRACKED = ["test.mbox", "test.mbox.truth.json", "flighty_exporter.example.toml", "pyproject.toml"]


def _ignored(path: str) -> bool:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    proc = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", path], cwd=REPO, capture_output=True, text=True
    )
    if proc.returncode not in (0, 1):
        pytest.skip(f"git check-ignore failed (not a git checkout?): {proc.stderr.strip()}")
    return proc.returncode == 0


@pytest.mark.parametrize("path", PERSONAL)
def test_personal_data_files_are_ignored(path):
    assert _ignored(path), f"{path} would not be ignored by git"


@pytest.mark.parametrize("path", TRACKED)
def test_synthetic_fixture_and_project_files_stay_trackable(path):
    assert not _ignored(path), f"{path} would be ignored by git"
