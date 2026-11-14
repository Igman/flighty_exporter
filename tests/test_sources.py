"""Saved source email text and the `sources` command, driven through the
command line against the fake Ollama: what `sources` prints (with the mailbox
file gone), and what the store file holds. Every email, person and flight
here is invented."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fake_ollama import Chat, Decision
from mailbox_fixtures import FLIGHT_BODY, email, text_pdf, write_mbox
from test_extract import CONNECTING_RECORD, FLIGHT_RECORD, FLIGHT_SUBJECT
from test_markup import flight_reservation, json_ld_html, markup_email

SENDER = "Fictair <noreply@fictair.example>"
DATE = "Tue, 04 Mar 2025 09:15:00 +0000"
UNAVAILABLE = "source text unavailable (extracted before text was saved)"

ETICKET = """FICTAIR E-TICKET
Passenger: Robin Quill
Flight ZZ1234  12 May 2025  LIS-OSL
Booking reference QZX7PL"""


def run_ok(workspace, *args: str):
    result = workspace.run(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def saved_text(stdout: str) -> list[str]:
    """The saved-text lines of `sources` output, without their prefix."""
    return [line[6:] for line in stdout.splitlines() if line.startswith("    |")]


def pipeline(workspace, messages, config: str = "") -> Path:
    """Classify and extract ``messages``, then delete the mailbox file."""
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n{config}')
    mbox = write_mbox(workspace.root / "mail.mbox", messages)
    run_ok(workspace, "classify", "--mailbox", str(mbox))
    run_ok(workspace, "extract", "--mailbox", str(mbox))
    mbox.unlink()
    return mbox


def test_sources_shows_each_flight_with_the_saved_text_of_its_emails(workspace):
    body = FLIGHT_BODY + "".join(f"Fare rule {i}.\n" for i in range(200)) + "END-OF-EMAIL-MARKER\n"
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    # The model reads a cut input; the saved text is the whole email.
    pipeline(workspace, [
        email(FLIGHT_SUBJECT, body, sender=SENDER,
              attachments=[("QZX7PL.pdf", text_pdf(ETICKET), "application/pdf")]),
    ], config="[extract]\nmax_input_chars = 400\n")

    result = run_ok(workspace, "sources")

    lines = result.stdout.splitlines()
    assert lines[0] == "2025-05-12  ZZ1234  LIS-OSL"
    assert lines[1] == f"  2025-03-04 09:15  {FLIGHT_SUBJECT}"
    text = saved_text(result.stdout)
    assert text[:4] == [f"From: {SENDER}", f"Subject: {FLIGHT_SUBJECT}", f"Date: {DATE}", ""]
    assert "Your booking is confirmed. Booking reference: QZX7PL" in text
    assert "END-OF-EMAIL-MARKER" in text
    pdf = text.index("----- PDF attachment: QZX7PL.pdf -----")
    assert pdf > text.index("END-OF-EMAIL-MARKER")
    assert "Flight ZZ1234 12 May 2025 LIS-OSL" in " ".join(text[pdf:]).replace("  ", " ")


def test_a_message_resolved_from_markup_has_its_text_saved_with_its_pdf_text(workspace):
    html = json_ld_html(flight_reservation("1234", "LIS", "OSL", "2025-06-01T07:40:00+01:00"),
                        visible="Thank you for booking with Fictair. MARKUP-BODY-MARKER")
    pipeline(workspace, [
        markup_email("Your Fictair booking QZX7PL", html, sender=SENDER,
                     attachments=[("QZX7PL.pdf", text_pdf(ETICKET), "application/pdf")]),
    ])
    assert workspace.ollama.pass_requests() == []

    result = run_ok(workspace, "sources")

    lines = result.stdout.splitlines()
    assert lines[:2] == ["2025-06-01  ZZ1234  LIS-OSL", "  2025-03-01 10:00  Your Fictair booking QZX7PL"]
    text = saved_text(result.stdout)
    assert text[:3] == [f"From: {SENDER}", "Subject: Your Fictair booking QZX7PL", "Date: Sat, 01 Mar 2025 10:00:00 +0000"]
    assert any("MARKUP-BODY-MARKER" in line for line in text)
    assert "----- PDF attachment: QZX7PL.pdf -----" in text


def store_bytes(workspace) -> bytes:
    return b"".join(
        path.read_bytes() for path in workspace.root.glob("flighty_exporter.db*") if path.is_file()
    )


def test_no_text_is_saved_for_messages_that_produced_no_record(workspace):
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.decisions["Garden club newsletter"] = Decision(0.01, "marketing")
    workspace.ollama.decisions["Your Imaginair itinerary"] = Decision(0.9, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    workspace.ollama.chats["Your Imaginair itinerary"] = Chat(content="not json")
    pipeline(workspace, [
        email(FLIGHT_SUBJECT, FLIGHT_BODY + "SAVED-MARKER-ONE\n"),
        email("Garden club newsletter", "UNSELECTED-MARKER-TWO\n"),
        email("Your Imaginair itinerary", "FAILED-MARKER-THREE\n"),
    ])

    saved = store_bytes(workspace)

    assert b"SAVED-MARKER-ONE" in saved
    assert b"UNSELECTED-MARKER-TWO" not in saved
    assert b"FAILED-MARKER-THREE" not in saved


def test_extracting_a_message_again_replaces_its_saved_text(workspace):
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    message_id = "<oslo-booking@fixtures.example>"
    pipeline(workspace, [email(FLIGHT_SUBJECT, FLIGHT_BODY + "FIRST-VERSION\n", message_id=message_id)])
    assert "FIRST-VERSION" in saved_text(run_ok(workspace, "sources").stdout)

    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[models]\nextraction = "other-extractor"\n')
    mbox = write_mbox(workspace.root / "mail.mbox",
                      [email(FLIGHT_SUBJECT, FLIGHT_BODY + "SECOND-VERSION\n", message_id=message_id)])
    run_ok(workspace, "extract", "--mailbox", str(mbox))

    text = saved_text(run_ok(workspace, "sources").stdout)
    assert "SECOND-VERSION" in text
    assert "FIRST-VERSION" not in text


def test_sources_lists_review_flights_and_can_be_limited_to_one_flight(workspace):
    other = "Booking for your colleague"
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")
    workspace.ollama.decisions["Old newsletter"] = Decision(0.01, "marketing")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    workspace.ollama.chats["Reminder: your Oslo flight"] = Chat(FLIGHT_RECORD)
    workspace.ollama.chats["Schedule change for WVB3RT"] = Chat(CONNECTING_RECORD)
    workspace.ollama.chats[other] = Chat({**FLIGHT_RECORD, "booking_reference": "OTH999",
                                          "passengers": ["Casey Marsh"]})
    pipeline(workspace, [
        email(FLIGHT_SUBJECT, FLIGHT_BODY),
        email("Reminder: your Oslo flight", "See you on board.\n", date="Wed, 05 Mar 2025 09:15:00 +0000"),
        email("Schedule change for WVB3RT", "Your Imaginair flights have changed.\n"),
        email(other, "Casey's trip.\n"),
        email("Old newsletter", "Nothing here.\n"),
    ], config='\n[passenger]\nnames = ["Robin Quill"]\n')

    everything = run_ok(workspace, "sources").stdout
    flight_lines = [line for line in everything.splitlines() if line and not line.startswith(" ")]
    assert flight_lines == ["2024-11-02  QQ410  AMS-KEF", "2024-11-03  QQ77  KEF-BOS", "2025-05-12  ZZ1234  LIS-OSL"]
    assert other not in everything and "Old newsletter" not in everything

    one = run_ok(workspace, "sources", "--flight", "2025-05-12", "zz 1234").stdout

    assert one.splitlines()[0] == "2025-05-12  ZZ1234  LIS-OSL"
    assert [line for line in one.splitlines() if line.startswith("  ") and not line.startswith("    ")] == [
        f"  2025-03-04 09:15  {FLIGHT_SUBJECT}", "  2025-03-05 09:15  Reminder: your Oslo flight",
    ]
    assert "See you on board." in saved_text(one)
    assert "QQ410" not in one

    listing = run_ok(workspace, "sources", "--flight", "2025-05-12", "ZZ1234", "--no-text").stdout

    assert listing.splitlines() == [
        "2025-05-12  ZZ1234  LIS-OSL",
        f"  2025-03-04 09:15  {FLIGHT_SUBJECT}",
        "  2025-03-05 09:15  Reminder: your Oslo flight",
    ]

    missing = workspace.run("sources", "--flight", "2025-05-13", "ZZ1234")
    assert missing.returncode == 1
    assert "no flight on 2025-05-13" in missing.stderr


def test_records_extracted_before_text_was_saved_show_a_note(workspace):
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    pipeline(workspace, [email(FLIGHT_SUBJECT, FLIGHT_BODY)])
    # Turn the store into one written before text was saved.
    with sqlite3.connect(workspace.root / "flighty_exporter.db") as db:
        db.execute("DROP TABLE message_texts")
        db.execute("PRAGMA user_version=5")

    result = run_ok(workspace, "sources")

    assert result.stdout.splitlines() == [
        "2025-05-12  ZZ1234  LIS-OSL",
        f"  2025-03-04 09:15  {FLIGHT_SUBJECT}",
        f"    {UNAVAILABLE}",
    ]


def test_text_saved_by_the_audit_is_shown_once_the_threshold_includes_the_message(workspace):
    low = "Your documents"
    workspace.ollama.decisions[low] = Decision(0.03, "other")
    workspace.ollama.chats[low] = Chat(FLIGHT_RECORD)
    mbox = write_mbox(workspace.root / "mail.mbox", [email(low, FLIGHT_BODY + "AUDITED-MARKER\n")])
    run_ok(workspace, "classify", "--mailbox", str(mbox))
    run_ok(workspace, "audit", "--mailbox", str(mbox))
    mbox.unlink()
    assert run_ok(workspace, "sources").stdout.strip() == "No flights to list. Run classify and extract first."

    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[classify]\nthreshold = 0.02\n')
    result = run_ok(workspace, "sources")

    assert result.stdout.splitlines()[:2] == ["2025-05-12  ZZ1234  LIS-OSL", f"  2025-03-04 09:15  {low}"]
    assert "AUDITED-MARKER" in saved_text(result.stdout)


def test_sources_without_a_store_is_a_clear_error(workspace):
    result = workspace.run("sources")

    assert result.returncode == 1
    assert "store not found" in result.stderr
