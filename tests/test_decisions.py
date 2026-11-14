"""Review decisions kept in the store, driven through the command line
(classify, extract, review, export, mark-added, sources) against a fake Ollama and the
tiny OpenFlights fixture data. A decision is the user's keep choice or a
correction to a flight's date, flight number or airports; the review table and
the export both read them. Assertions are on the review CSV, the import file,
the manual-add list and the printed summaries. Every email, person, booking
and flight here is invented.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fake_ollama import Chat, Decision
from mailbox_fixtures import email, write_mbox
from test_consolidation import Mail, record, seg
from test_export import data_rows, import_files, the_import_file, write_review
from test_extract import header, read_csv
from test_validation import booking

REVIEW = "review.csv"
DISCARDED = "decisions discarded because their flight is no longer produced"
RECORDED = "decisions recorded from the review table"

GOOD = booking("Your trip GOOD01", seg("2025-05-12", "ZZ1234", "LIS", "OSL", confidence=0.95), reference="GOOD01")
OTHER = booking("Your trip OTHR02", seg("2025-06-01", "QQ410", "AMS", "KEF", confidence=0.9), day="02", reference="OTHR02")
FLAGGED = booking("Your trip FLAG03", seg("2025-07-01", "ZZ300", "LIS", "XQX", confidence=0.9), day="03", reference="FLAG03")
NO_NUMBER = booking("Your trip NONR04", seg("2025-08-01", "", "OSL", "LIS", confidence=0.9), day="04", reference="NONR04")
OLD = booking("Your trip OLDY05", seg("2004-08-01", "ZZ9", "OSL", "LIS", confidence=0.9), day="05", reference="OLDY05")


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


# -- the spreadsheet route records decisions ----------------------------------------------


def test_keep_choice_in_an_edited_table_is_recorded_and_a_forced_review_reflects_it(workspace):
    run_passes(workspace, [GOOD, OTHER])
    review(workspace)
    edit_review(workspace, lambda rows: rows["2025-06-01 QQ410"].update(keep="no"))

    exported = export(workspace)

    assert exported.count(RECORDED) == 1
    assert "Review table read from" in exported.stdout
    assert exported.count("flights exported") == 1
    # The edited table is still the user's: review will not overwrite it...
    refused = workspace.run("review")
    assert refused.returncode == 1 and "--force" in refused.stderr
    # ...but a table written elsewhere, or a forced one, carries the decision.
    review(workspace, "--output", str(workspace.root / "fresh.csv"))
    assert rows_by_id(workspace.root / "fresh.csv")["2025-06-01 QQ410"]["keep"] == "no"
    forced = review(workspace, "--force")
    rows = rows_by_id(workspace.root / REVIEW)
    assert [rows[k]["keep"] for k in ("2025-05-12 ZZ1234", "2025-06-01 QQ410")] == ["yes", "no"]
    assert forced.count(DISCARDED) == 0
    # Exporting the regenerated table records nothing new.
    assert export(workspace).count(RECORDED) == 0


def test_correction_is_validated_and_its_attention_reasons_recomputed(workspace):
    run_passes(workspace, [GOOD, OTHER, FLAGGED])
    review(workspace)
    rows = rows_by_id(workspace.root / REVIEW)
    assert view(rows["2025-07-01 ZZ300"]) == ("no", "2025-07-01", "ZZ300", "LIS", "XQX",
                                              "unknown airport XQX; low confidence")

    def edit(rows):
        # Fixes the flagged flight and approves it.
        rows["2025-07-01 ZZ300"].update(destination="osl", keep="yes")
        # Introduces an unknown airport, and leaves keep as the table showed it.
        rows["2025-05-12 ZZ1234"].update(destination="XQY")
        # Introduces one, and agrees with the check that it should not be kept.
        rows["2025-06-01 QQ410"].update(origin="xqz", flight_number="qq 411", keep="no")

    edit_review(workspace, edit)
    exported = export(workspace)

    assert exported.count(RECORDED) == 3
    review(workspace, "--force")
    rows = rows_by_id(workspace.root / REVIEW)
    assert view(rows["2025-07-01 ZZ300"]) == ("yes", "2025-07-01", "ZZ300", "LIS", "OSL", "")
    assert rows["2025-07-01 ZZ300"]["confidence"] == "0.90"
    # The user's keep (yes, as the table said) wins over the check's pre-set no.
    assert view(rows["2025-05-12 ZZ1234"]) == ("yes", "2025-05-12", "ZZ1234", "LIS", "XQY",
                                               "unknown airport XQY; low confidence")
    assert view(rows["2025-06-01 QQ410"]) == ("no", "2025-06-01", "QQ411", "XQZ", "KEF",
                                              "unknown airport XQZ; low confidence")
    assert rows["2025-06-01 QQ410"]["needs_attention"] == "yes"


def test_rows_sorted_differently_in_the_spreadsheet_still_find_their_flights(workspace):
    run_passes(workspace, [GOOD, OTHER, FLAGGED])
    review(workspace)

    def edit(rows):
        rows["2025-05-12 ZZ1234"].update(keep="no")
        rows["2025-07-01 ZZ300"].update(destination="OSL", keep="yes")

    edit_review(workspace, edit, reverse=True)
    assert export(workspace).count(RECORDED) == 2

    review(workspace, "--force")
    rows = rows_by_id(workspace.root / REVIEW)
    assert [rows[k]["keep"] for k in ("2025-05-12 ZZ1234", "2025-06-01 QQ410", "2025-07-01 ZZ300")] == [
        "no", "yes", "yes"]
    assert rows["2025-07-01 ZZ300"]["destination"] == "OSL"


def test_a_later_edit_replaces_the_recorded_decision(workspace):
    run_passes(workspace, [GOOD, OTHER])
    review(workspace)
    edit_review(workspace, lambda rows: rows["2025-06-01 QQ410"].update(keep="no", destination="LHR"))
    assert export(workspace).count(RECORDED) == 1

    # The user changes their mind in the same table: keep it, and put the airport back.
    edit_review(workspace, lambda rows: rows["2025-06-01 QQ410"].update(keep="yes", destination="KEF"))
    assert export(workspace).count(RECORDED) == 1

    review(workspace, "--force")
    assert view(rows_by_id(workspace.root / REVIEW)["2025-06-01 QQ410"]) == (
        "yes", "2025-06-01", "QQ410", "AMS", "KEF", "")


# -- exporting from the store ----------------------------------------------------------------


def test_export_from_the_store_with_no_review_table_matches_the_spreadsheet_route(workspace):
    run_passes(workspace, [GOOD, OTHER, FLAGGED, NO_NUMBER, OLD])
    review(workspace)

    def edit(rows):
        rows["2025-06-01 QQ410"].update(keep="no")
        rows["2025-05-12 ZZ1234"].update(flight_number="ZZ1235")
        rows["2025-07-01 ZZ300"].update(destination="KEF", keep="yes")

    edit_review(workspace, edit)
    via_table = export(workspace, "--output-dir", "via-table")
    (workspace.root / REVIEW).unlink()

    via_store = export(workspace, "--output-dir", "via-store")

    assert "Flight list read from the store" in via_store.stdout
    assert "store (saved review decisions applied)" in via_store.stdout
    assert RECORDED not in via_store.stdout
    assert via_store.count(DISCARDED) == 0
    table_file = the_import_file(workspace.root / "via-table")
    store_file = the_import_file(workspace.root / "via-store")
    assert store_file.read_bytes() == table_file.read_bytes()
    assert data_rows(store_file) == [
        '2025-07-01,LIS,KEF,ZZ300,Fictair,1825,04:09,,,,,,,,"",1638,16,90001,',
        '2025-05-12,LIS,OSL,ZZ1235,Fictair,1714,03:55,,,,,,,,"",1638,644,90001,',
    ]
    manual = read_csv(workspace.root / "via-store" / "manual-add.csv")
    assert manual == read_csv(workspace.root / "via-table" / "manual-add.csv")
    assert [(m["date"], m["flight_number"], m["reason"]) for m in manual] == [
        ("2004-08-01", "ZZ9", "dated before 2006"),
        ("2025-08-01", "", "no flight number"),
    ]
    for label in ("flights exported", "flights to add by hand", "rows", "not kept", "rejected as invalid"):
        assert via_store.count(label) == via_table.count(label), label


def test_from_store_ignores_a_review_table_that_exists(workspace):
    run_passes(workspace, [GOOD, OTHER])
    review(workspace)
    # A table the store knows nothing about (no flight_id), dropping every flight.
    write_review(workspace.root / REVIEW, [{"keep": "no", "date": "2025-05-12", "flight_number": "ZZ1234",
                                            "origin": "LIS", "destination": "OSL"}],
                 ["keep", "date", "flight_number", "origin", "destination"])

    from_table = export(workspace, "--output-dir", "table")
    from_store = export(workspace, "--from-store", "--output-dir", "store")

    assert import_files(workspace.root / "table") == []
    assert from_table.count(RECORDED) == 0
    assert [line.split(",")[3] for line in data_rows(the_import_file(workspace.root / "store"))] == [
        "QQ410", "ZZ1234"]
    assert from_store.count("flights exported") == 2


def test_from_store_without_a_store_is_a_clear_error(workspace):
    result = workspace.run("export", "--from-store")

    assert result.returncode == 1
    assert "store not found" in result.stderr


def test_review_table_without_a_store_is_exported_as_before(workspace):
    run_passes(workspace, [GOOD, OTHER])
    review(workspace)
    edit_review(workspace, lambda rows: rows["2025-06-01 QQ410"].update(keep="no"))
    (workspace.root / "flighty_exporter.db").unlink()

    result = export(workspace)

    assert data_rows(the_import_file(workspace.root)) == [
        '2025-05-12,LIS,OSL,ZZ1234,Fictair,1714,03:55,,,,,,,,"",1638,644,90001,']
    assert "source of the export:       review table" in result.stdout
    assert RECORDED not in result.stdout and DISCARDED not in result.stdout


def test_rows_without_a_flight_id_are_exported_but_record_nothing(workspace):
    run_passes(workspace, [GOOD, OTHER])
    review(workspace)
    path = workspace.root / REVIEW
    rows = read_csv(path)
    for r in rows:
        r["keep"] = "no" if r["flight_number"] == "QQ410" else r["keep"]
    # A table from before flight_id existed: the column is not there.
    write_review(path, rows, [c for c in header(path) if c != "flight_id"])

    result = export(workspace)

    assert result.count(RECORDED) == 0
    assert result.count("flights exported") == 1
    review(workspace, "--force")
    assert [r["keep"] for r in read_csv(path)] == ["yes", "yes"]


# -- decisions follow the latest result ------------------------------------------------------


def test_decision_whose_flight_disappears_is_discarded_and_counted(workspace):
    run_passes(workspace, [GOOD, OTHER])
    review(workspace)
    edit_review(workspace, lambda rows: rows["2025-06-01 QQ410"].update(keep="no", destination="LHR"))
    assert export(workspace).count(RECORDED) == 1

    # A later cancellation email for that booking arrives; the passes run again.
    cancelled = Mail("Booking OTHR02 cancelled", "20", "cancellation", record("cancellation", "OTHR02"))
    run_passes(workspace, [GOOD, OTHER, cancelled])

    forced = review(workspace, "--force")

    assert forced.count(DISCARDED) == 1
    assert list(rows_by_id(workspace.root / REVIEW)) == ["2025-05-12 ZZ1234"]
    assert review(workspace).count(DISCARDED) == 0


def test_export_from_the_store_discards_and_counts_decisions_too(workspace):
    run_passes(workspace, [GOOD, OTHER])
    review(workspace)
    edit_review(workspace, lambda rows: rows["2025-06-01 QQ410"].update(keep="no"))
    export(workspace)
    cancelled = Mail("Booking OTHR02 cancelled", "20", "cancellation", record("cancellation", "OTHR02"))
    run_passes(workspace, [GOOD, OTHER, cancelled])

    result = export(workspace, "--from-store")

    assert result.count(DISCARDED) == 1
    assert export(workspace, "--from-store").count(DISCARDED) == 0


# -- manual-add ticks --------------------------------------------------------------------------


def done(path: Path) -> dict[tuple[str, str, str, str], str]:
    return {(m["date"], m["flight_number"], m["origin"], m["destination"]): m["done"] for m in read_csv(path)}


def test_mark_added_ticks_a_flight_on_the_manual_add_list(workspace):
    run_passes(workspace, [GOOD, NO_NUMBER, OLD])
    review(workspace)
    export(workspace)
    manual = workspace.root / "manual-add.csv"
    assert header(manual)[-1] == "done"
    assert set(done(manual).values()) == {""}

    by_route = workspace.run("mark-added", "2025-08-01", "--route", "osl", "lis")
    by_number = workspace.run("mark-added", "2004-08-01", "ZZ 9")

    assert by_route.returncode == 0, by_route.stderr
    assert by_number.returncode == 0, by_number.stderr
    assert "2025-08-01 OSL-LIS" in by_route.stdout
    export(workspace)
    assert done(manual) == {("2004-08-01", "ZZ9", "OSL", "LIS"): "yes", ("2025-08-01", "", "OSL", "LIS"): "yes"}
    # The same ticks show when exporting from the store.
    export(workspace, "--from-store")
    assert done(manual) == {("2004-08-01", "ZZ9", "OSL", "LIS"): "yes", ("2025-08-01", "", "OSL", "LIS"): "yes"}

    undone = workspace.run("mark-added", "2004-08-01", "ZZ9", "--undo")

    assert undone.returncode == 0, undone.stderr
    export(workspace)
    assert done(manual) == {("2004-08-01", "ZZ9", "OSL", "LIS"): "", ("2025-08-01", "", "OSL", "LIS"): "yes"}


def test_mark_added_finds_a_corrected_flight_by_the_values_it_now_shows(workspace):
    run_passes(workspace, [GOOD, OLD])
    review(workspace)
    edit_review(workspace, lambda rows: rows["2004-08-01 ZZ9"].update(flight_number="ZZ8"))
    export(workspace)

    assert workspace.run("mark-added", "2004-08-01", "ZZ8").returncode == 0

    (workspace.root / REVIEW).unlink()
    export(workspace)
    assert done(workspace.root / "manual-add.csv") == {("2004-08-01", "ZZ8", "OSL", "LIS"): "yes"}


def test_mark_added_needs_a_flight_number_or_a_route_and_a_store(workspace):
    neither = workspace.run("mark-added", "2004-08-01")
    assert neither.returncode == 2 and "--route" in neither.stderr

    no_store = workspace.run("mark-added", "2004-08-01", "ZZ9")
    assert no_store.returncode == 1 and "store not found" in no_store.stderr

    bad_date = workspace.run("mark-added", "01/08/2004", "ZZ9")
    assert bad_date.returncode == 2 and "YYYY-MM-DD" in bad_date.stderr


# -- stores from before decisions -----------------------------------------------------------------


def test_a_store_from_before_decisions_still_works(workspace):
    run_passes(workspace, [GOOD, OTHER])
    with sqlite3.connect(workspace.root / "flighty_exporter.db") as db:
        db.execute("DROP TABLE review_decisions")
        db.execute("PRAGMA user_version=4")

    review(workspace)
    edit_review(workspace, lambda rows: rows["2025-06-01 QQ410"].update(keep="no"))

    assert export(workspace).count(RECORDED) == 1
    (workspace.root / REVIEW).unlink()
    assert export(workspace).count("flights exported") == 1


# Stores as each earlier version left them: (what to remove, user_version).
# Two lines of development both numbered their change 5, so the number alone
# does not say which tables and columns a store has.
EARLIER_STORES = {
    "4": (("review_decisions", "message_texts", "mbox_offset"), 4),
    "5 with decisions": (("message_texts", "mbox_offset"), 5),
    "5 with positions": (("review_decisions", "message_texts"), 5),
    "6 with positions and text": (("review_decisions",), 6),
}


@pytest.mark.parametrize("earlier", EARLIER_STORES)
def test_a_store_from_each_earlier_version_works_with_decisions_positions_and_saved_text(workspace, earlier):
    removed, version = EARLIER_STORES[earlier]
    run_passes(workspace, [GOOD, OTHER])
    db_path = workspace.root / "flighty_exporter.db"
    with sqlite3.connect(db_path) as db:
        for name in removed:
            if name == "mbox_offset":
                db.execute("ALTER TABLE messages DROP COLUMN mbox_offset")
            else:
                db.execute(f"DROP TABLE {name}")
        db.execute(f"PRAGMA user_version={version}")

    # Decisions: recorded from an edited table, then applied by sources.
    review(workspace)
    edit_review(workspace, lambda rows: rows["2025-06-01 QQ410"].update(flight_number="QQ411"))
    assert export(workspace).count(RECORDED) == 1
    listing = workspace.run("sources", "--flight", "2025-06-01", "QQ411")
    assert listing.returncode == 0, listing.stderr
    text_kept = "message_texts" not in removed
    assert ("Invented email." in listing.stdout) == text_kept
    assert ("source text unavailable" in listing.stdout) == (not text_kept)

    # Positions and saved text: extracting again (another model) finds the
    # messages, scanning only if positions were never recorded, and saves text.
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[models]\nextraction = "other-extractor"\n')
    again = workspace.run("extract", "--mailbox", str(workspace.root / "mail.mbox"))
    assert again.returncode == 0, again.stderr
    assert ("locating messages" in again.stderr) == ("mbox_offset" in removed)
    assert again.count("not in mailbox") == 0
    listing = workspace.run("sources", "--flight", "2025-06-01", "QQ411")
    assert listing.stdout.splitlines()[0] == "2025-06-01  QQ411  AMS-KEF"
    assert "Invented email." in listing.stdout and "source text unavailable" not in listing.stdout

    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert db.execute("SELECT count(*) FROM messages WHERE mbox_offset IS NULL").fetchone()[0] == 0
        assert len(db.execute("SELECT * FROM review_decisions").fetchall()) == 1
