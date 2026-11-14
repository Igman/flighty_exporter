"""Reading selected messages by position: extract and audit go straight to the
messages they need instead of reading the whole mailbox again.

Driven through the command line against the fake Ollama. A "decoy" shows that
a message was not read: after classify, an unselected message earlier in the
mailbox is given a selected message's Message-ID (edited in place, so no
byte moves). A stage that read the mailbox from the start would meet the
decoy first and send its subject to the extraction model; a stage that reads
by position sends the selected message. Every email, person and flight here
is invented.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fake_ollama import Chat, Decision
from mailbox_fixtures import FLIGHT_BODY, email, write_mbox
from test_extract import FLIGHT_RECORD, read_csv

FLIGHT = "Your flight to Oslo is confirmed"
FLIGHT_ID = "<flight-oslo@fixtures.example>"
SECOND = "Your flight to Reykjavik is confirmed"
SECOND_ID = "<flight-kefl@fixtures.example>"
DECOY = "Weekly garden newsletter"
DECOY_ID = "<garden-news@fixtures.example>"  # same length as the flight identifiers

SECOND_RECORD = {
    **FLIGHT_RECORD,
    "booking_reference": "KFX2RT",
    "segments": [{**FLIGHT_RECORD["segments"][0], "flight_number": "ZZ881", "origin": "OSL",
                  "destination": "KEF", "date": "2025-07-01"}],
}


def run_ok(workspace, *args: str):
    result = workspace.run(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def script(workspace) -> None:
    workspace.ollama.decisions[FLIGHT] = Decision(0.97, "booking confirmation")
    workspace.ollama.decisions[SECOND] = Decision(0.9, "booking confirmation")
    workspace.ollama.decisions[DECOY] = Decision(0.01, "marketing")
    workspace.ollama.chats[FLIGHT] = Chat(FLIGHT_RECORD)
    workspace.ollama.chats[SECOND] = Chat(SECOND_RECORD)


def flight_email(subject: str = FLIGHT, message_id: str = FLIGHT_ID):
    return email(subject, FLIGHT_BODY, message_id=message_id, sender="Fictair <noreply@fictair.example>")


def decoy_email(subject: str = DECOY, message_id: str = DECOY_ID):
    return email(subject, "Prune the roses this week.\n", message_id=message_id)


def give_identifier(mbox: Path, old: str, new: str) -> None:
    """Edit one Message-ID in place, keeping every byte where it was."""
    assert len(old) == len(new)
    data = mbox.read_bytes()
    assert data.count(old.encode()) == 1
    mbox.write_bytes(data.replace(old.encode(), new.encode()))


def use_extraction_model(workspace, name: str) -> None:
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[models]\nextraction = "{name}"\n')


def test_extract_reads_only_the_selected_messages(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [decoy_email(), flight_email()])
    script(workspace)
    run_ok(workspace, "classify", "--mailbox", str(mbox))
    give_identifier(mbox, DECOY_ID, FLIGHT_ID)

    result = run_ok(workspace, "extract", "--mailbox", str(mbox))

    assert workspace.ollama.extracted_subjects() == [FLIGHT]
    assert result.count("extracted") == 1
    assert result.count("not in mailbox") == 0


def test_audit_reads_only_the_sampled_messages(workspace):
    # The decoy was extracted by extract, so the audit does not send it; the
    # sampled message below the threshold sits after it in the mailbox.
    low = "Your documents for the trip"
    low_id = "<trip-papers@fixtures.example>"
    mbox = write_mbox(workspace.root / "mail.mbox", [flight_email(), flight_email(low, low_id)])
    script(workspace)
    workspace.ollama.decisions[low] = Decision(0.003, "other")
    run_ok(workspace, "classify", "--mailbox", str(mbox))
    run_ok(workspace, "extract", "--mailbox", str(mbox))
    give_identifier(mbox, FLIGHT_ID, low_id)

    result = run_ok(workspace, "audit", "--mailbox", str(mbox))

    assert workspace.ollama.extracted_subjects() == [FLIGHT, low]
    assert result.count("sent to the extraction model") == 1
    assert "not in mailbox" not in result.stdout


def test_a_mailbox_changed_after_classify_yields_the_same_extractions_and_positions_are_recorded_again(workspace):
    mbox = workspace.root / "mail.mbox"
    write_mbox(mbox, [flight_email(), decoy_email(), flight_email(SECOND, SECOND_ID)])
    script(workspace)
    run_ok(workspace, "classify", "--mailbox", str(mbox))
    # A new export of the mailbox: a new message first, and the rest in another order.
    newcomer = "Your parcel is on its way"
    write_mbox(mbox.with_suffix(".new"), [decoy_email(newcomer, "<parcel-note@fixtures.example>"),
                                          flight_email(SECOND, SECOND_ID), decoy_email(), flight_email()])
    mbox.with_suffix(".new").replace(mbox)

    result = run_ok(workspace, "extract", "--mailbox", str(mbox))

    assert workspace.ollama.extracted_subjects() == [SECOND, FLIGHT]  # mailbox order
    assert result.count("extracted") == 2
    assert result.count("not in mailbox") == 0
    assert "extract: locating messages" in result.stderr
    run_ok(workspace, "review")
    assert [(r["flight_number"], r["message_subject"]) for r in read_csv(workspace.root / "review.csv")] == [
        ("ZZ1234", FLIGHT), ("ZZ881", SECOND),
    ]

    # The positions found by that scan were recorded: a decoy put before the
    # message is not read when it is extracted again by another model.
    give_identifier(mbox, "<parcel-note@fixtures.example>", FLIGHT_ID)
    use_extraction_model(workspace, "other-extractor")
    again = run_ok(workspace, "extract", "--mailbox", str(mbox))

    assert workspace.ollama.extracted_subjects()[2:] == [SECOND, FLIGHT]
    assert "locating messages" not in again.stderr


def test_a_store_without_recorded_positions_still_works_and_scans_only_once(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [decoy_email(), flight_email(), flight_email(SECOND, SECOND_ID)])
    script(workspace)
    run_ok(workspace, "classify", "--mailbox", str(mbox))
    # Turn the store into one written before positions were recorded.
    with sqlite3.connect(workspace.root / "flighty_exporter.db") as db:
        db.execute("ALTER TABLE messages DROP COLUMN mbox_offset")
        db.execute("PRAGMA user_version=4")

    result = run_ok(workspace, "extract", "--mailbox", str(mbox))

    assert workspace.ollama.extracted_subjects() == [FLIGHT, SECOND]
    assert result.count("extracted") == 2
    assert result.count("not in mailbox") == 0

    give_identifier(mbox, DECOY_ID, FLIGHT_ID)
    use_extraction_model(workspace, "other-extractor")
    again = run_ok(workspace, "extract", "--mailbox", str(mbox))

    assert workspace.ollama.extracted_subjects()[2:] == [FLIGHT, SECOND]
    assert "locating messages" not in again.stderr


def test_a_message_gone_from_the_changed_mailbox_is_still_reported(workspace):
    mbox = workspace.root / "mail.mbox"
    write_mbox(mbox, [flight_email(SECOND, SECOND_ID), flight_email()])
    script(workspace)
    run_ok(workspace, "classify", "--mailbox", str(mbox))
    write_mbox(mbox.with_suffix(".new"), [decoy_email(), flight_email()])
    mbox.with_suffix(".new").replace(mbox)

    result = run_ok(workspace, "extract", "--mailbox", str(mbox))

    assert workspace.ollama.extracted_subjects() == [FLIGHT]
    assert result.count("extracted") == 1
    assert result.count("not in mailbox") == 1
    assert "no longer in the mailbox" in result.stdout
