"""Shared by everything that writes a table for the user (review, failures,
audit and manual-add files): the CSV writer and how a message's date is shown.

At the top level because both a stage (audit) and the output group (review,
export) use it, and those two groups may not import each other.
"""

from __future__ import annotations

import csv
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


class FileInUse(PermissionError):
    """A table could not be replaced because another program holds it open:
    on Windows, a spreadsheet application locks the file it has open."""


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    """Write ``rows`` atomically as UTF-8 with a byte-order mark, so
    spreadsheets detect the encoding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    try:
        tmp.replace(path)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise FileInUse(
            f"cannot write {path}: it may be open in another program, such as a spreadsheet. "
            "Close it there and try again."
        ) from exc


def format_message_date(raw: str) -> tuple[str, str]:
    """The message's Date header as shown ('YYYY-MM-DD HH:MM' in the sender's
    time zone, or as written if it cannot be parsed) and as a sortable UTC
    timestamp ('' if it cannot be parsed, so undated messages sort first)."""
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return raw, ""
    utc = parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M"), utc.strftime("%Y-%m-%dT%H:%M:%S")
