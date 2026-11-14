"""Review decisions kept in the store, driven through the command line
(classify, extract, review, export, mark-added) against a fake Ollama and the
tiny OpenFlights fixture data. A decision is the user's keep choice or a
correction to a flight's date, flight number or airports; the review table and
the export both read them. Assertions are on the review CSV, the import file,
the manual-add list and the printed summaries. Every email, person, booking
and flight here is invented.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fake_ollama import Chat, Decision
from mailbox_fixtures import email, write_mbox
from test_consolidation import Mail, record, seg
from test_export import data_rows, import_files, the_import_file, write_review
from test_extract import header, read_csv
from test_validation import booking

REVIEW = "review.csv"
DISCARDED = "decisions discarded because their flight is no longer produced"
RECORDED = "decisions recorded from the review table"

GOOD = booking("Your trip GOOD01", seg("2025-05-12", "ZZ1234", "LIS", "OSL", confidence=0.95))
OTHER = booking("Your trip OTHR02", seg("2025-06-01", "QQ410", "AMS", "KEF", confidence=0.9), day="02")
FLAGGED = booking("Your trip FLAG03", seg("2025-07-01", "ZZ300", "LIS", "XQX", confidence=0.9), day="03")
NO_NUMBER = booking("Your trip NONR04", seg("2025-08-01", "", "OSL", "LIS", confidence=0.9), day="04")
OLD = booking("Your trip OLDY05", seg("2004-08-01", "ZZ9", "OSL", "LIS", confidence=0.9), day="05")


def run_passes(workspace, mails: list[Mail]) -> None:
    """Write ``mails`` to the mailbox, then classify and extract them."""
    mbox = write_mbox(workspace.root / "mail.mbox",
                      [email(m.subject, "Invented email.\n", date=m.header_date) for m in mails])
    for m in mails:
        workspace.ollama.decisions[m.subject] = Decision(0.9, m.email_type)
        workspace.ollama.chats[m.subject] = Chat(m.record)
    for command in ("classify", "extract"):
        result = workspace.run(command, "--mailbox", str(mbox))
        assert result.returncode == 0, result.stderr


def review(workspace, *args: str):
    result = workspace.run("review", *args)
    assert result.returncode == 0, result.stderr
    return result


def rows_by_id(path: Path) -> dict[str, dict[str, str]]:
    return {r["flight_id"]: r for r in read_csv(path)}


def edit_review(workspace, edit, *, reverse: bool = False) -> None:
    """Edit the review table as a spreadsheet would: ``edit`` changes the rows
    (a dict by flight_id) in place; the file is saved with the same columns."""
    path = workspace.root / REVIEW
    rows = read_csv(path)
    edit({r["flight_id"]: r for r in rows})
    if reverse:
        rows.reverse()
    write_review(path, rows, header(path))


def export(workspace, *args: str):
    result = workspace.run("export", *args)
    assert result.returncode == 0, result.stderr
    return result


def view(row: dict[str, str]) -> tuple[str, ...]:
    return (row["keep"], row["date"], row["flight_number"], row["origin"], row["destination"],
            row["attention_reason"])


# -- the flight_id column ----------------------------------------------------------------


def test_review_table_names_each_flight_in_a_last_flight_id_column(workspace):
    run_passes(workspace, [GOOD, NO_NUMBER])

    review(workspace)

    path = workspace.root / REVIEW
    assert header(path)[-1] == "flight_id"
    assert [r["flight_id"] for r in read_csv(path)] == ["2025-05-12 ZZ1234", "2025-08-01 OSL-LIS"]
