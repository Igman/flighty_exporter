"""Booking consolidation, driven through the command line (classify, extract,
review) against a fake Ollama that scripts both the classifier's answer and
the extraction record per message. Assertions are on the review CSV and the
printed summary only. Every email, person, booking and flight here is invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fake_ollama import Chat, Decision
from mailbox_fixtures import email, write_mbox
from test_extract import read_csv

ROBIN = "Robin Quill"
SAM = "Sam Ferrow"


def seg(date: str, flight_number: str, origin: str, destination: str, confidence: float = 0.9,
        airline: str = "Fictair") -> dict[str, Any]:
    return {"date": date, "airline": airline, "flight_number": flight_number,
            "origin": origin, "destination": destination, "confidence": confidence}


def record(kind: str, reference: str | None, *segments: dict[str, Any], passengers: tuple[str, ...] = (ROBIN,)):
    return {"email_kind": kind, "booking_reference": reference, "passengers": list(passengers),
            "segments": list(segments)}


@dataclass
class Mail:
    subject: str
    date: str  # day of the month in March 2025, or a full RFC 2822 date
    email_type: str  # the decision model's answer
    record: dict[str, Any]

    @property
    def header_date(self) -> str:
        return self.date if "," in self.date else f"{self.date} Mar 2025 10:00:00 +0000"


def run(workspace, mails: list[Mail], *, name: str = "mail") -> tuple[Any, list[dict[str, str]]]:
    """Classify, extract and review ``mails``; return the review result and rows."""
    mbox = write_mbox(workspace.root / f"{name}.mbox",
                      [email(m.subject, "Invented email.\n", date=m.header_date) for m in mails])
    for m in mails:
        workspace.ollama.decisions[m.subject] = Decision(0.9, m.email_type)
        workspace.ollama.chats[m.subject] = Chat(m.record)
    store = str(workspace.root / f"{name}.db")
    output = workspace.root / f"{name}-review.csv"
    for command in ("classify", "extract"):
        result = workspace.run(command, "--mailbox", str(mbox), "--store", store)
        assert result.returncode == 0, result.stderr
    review = workspace.run("review", "--store", store, "--output", str(output), "--force",
                           "--failures", str(workspace.root / f"{name}-failures.csv"))
    assert review.returncode == 0, review.stderr
    return review, read_csv(output)


def flights(rows: list[dict[str, str]]) -> list[tuple[str, str, str, str]]:
    return [(r["date"], r["flight_number"], r["origin"], r["destination"]) for r in rows]


# -- cancellation ---------------------------------------------------------------


def test_cancellation_removes_the_bookings_flights_listed_or_not(workspace):
    review, rows = run(workspace, [
        Mail("Booking CXL111 confirmed", "01", "booking confirmation",
             record("booking", "CXL111", seg("2025-06-01", "ZZ500", "LIS", "OSL"))),
        Mail("Booking CXL111 cancelled", "05", "cancellation",
             record("cancellation", "CXL111", seg("2025-06-01", "ZZ500", "LIS", "OSL"))),
        # A two-leg booking whose cancellation lists no segments at all.
        Mail("Booking CXL222 confirmed", "02", "booking confirmation",
             record("booking", "CXL222", seg("2025-07-01", "ZZ600", "OSL", "CPH"),
                    seg("2025-07-08", "ZZ601", "CPH", "OSL"))),
        Mail("Your booking CXL222 has been refunded", "06", "cancellation", record("cancellation", "CXL222")),
        Mail("Booking KEEP33 confirmed", "03", "booking confirmation",
             record("booking", "KEEP33", seg("2025-08-01", "ZZ700", "LIS", "MAD"))),
    ])

    assert flights(rows) == [("2025-08-01", "ZZ700", "LIS", "MAD")]
    assert review.count("flights (one row each)") == 1
    assert review.count("bookings cancelled") == 2


def test_rebooking_after_a_cancellation_reestablishes_the_booking(workspace):
    review, rows = run(workspace, [
        Mail("Booking RBK444 confirmed", "01", "booking confirmation",
             record("booking", "RBK444", seg("2025-06-01", "ZZ500", "LIS", "OSL"))),
        Mail("Booking RBK444 cancelled", "02", "cancellation",
             record("cancellation", "RBK444", seg("2025-06-01", "ZZ500", "LIS", "OSL"))),
        Mail("Booking RBK444 rebooked", "03", "booking confirmation",
             record("booking", "RBK444", seg("2025-06-03", "ZZ502", "LIS", "OSL"))),
    ])

    assert flights(rows) == [("2025-06-03", "ZZ502", "LIS", "OSL")]
    assert rows[0]["source_count"] == "1"
    assert rows[0]["message_subject"] == "Booking RBK444 rebooked"
    assert review.count("bookings cancelled") == 1


# -- replacement --------------------------------------------------------------


def test_schedule_change_replaces_the_earlier_itinerary(workspace):
    # The change is first in the mailbox but dated later: date order decides.
    review, rows = run(workspace, [
        Mail("Schedule change for CHG555", "10", "change",
             record("change", "CHG555", seg("2025-06-01", "ZZ100", "LIS", "FRA"),
                    seg("2025-06-01", "ZZ208", "FRA", "VIE", confidence=0.8))),
        Mail("Your CHG555 itinerary", "01", "booking confirmation",
             record("booking", "CHG555", seg("2025-06-01", "ZZ100", "LIS", "FRA", confidence=0.95),
                    seg("2025-06-01", "ZZ200", "FRA", "VIE"))),
    ])

    assert flights(rows) == [
        ("2025-06-01", "ZZ100", "LIS", "FRA"),
        ("2025-06-01", "ZZ208", "FRA", "VIE"),
    ]
    unchanged, replaced = rows
    assert unchanged["source_count"] == "2"
    assert unchanged["confidence"] == "0.95"  # the highest across its sources
    assert unchanged["message_subject"] == "Schedule change for CHG555"  # the most recent source
    assert unchanged["source_messages"] == "2025-03-01 Your CHG555 itinerary | 2025-03-10 Schedule change for CHG555"
    assert replaced["source_count"] == "1"
    assert replaced["email_kind"] == "change"
    assert review.count("bookings with replaced segments") == 1


def test_change_listing_one_leg_keeps_the_other_leg(workspace):
    review, rows = run(workspace, [
        Mail("Your ONE666 itinerary", "01", "booking confirmation",
             record("booking", "ONE666", seg("2025-06-01", "ZZ300", "LIS", "OSL"),
                    seg("2025-06-09", "ZZ301", "OSL", "LIS"))),
        Mail("Your return flight ONE666 has changed", "04", "change",
             record("change", "ONE666", seg("2025-06-10", "ZZ305", "OSL", "LIS"))),
    ])

    assert flights(rows) == [
        ("2025-06-01", "ZZ300", "LIS", "OSL"),
        ("2025-06-10", "ZZ305", "OSL", "LIS"),
    ]
    assert rows[0]["message_subject"] == "Your ONE666 itinerary"
    assert review.count("bookings with replaced segments") == 1


# -- evidence -------------------------------------------------------------------


def test_check_in_and_post_flight_mail_confirm_a_flight_and_nothing_is_filtered(workspace):
    review, rows = run(workspace, [
        Mail("Your EVD777 trip to Oslo", "01", "booking confirmation",
             record("booking", "EVD777", seg("2025-06-01", "ZZ400", "LIS", "OSL"),
                    seg("2025-06-09", "ZZ401", "OSL", "LIS"))),
        # The extractor calls it a check-in; the classifier mislabels it.
        Mail("Check in now for ZZ400", "02", "other",
             record("check-in", "EVD777", seg("2025-06-01", "ZZ400", "LIS", "OSL"))),
        # A post-flight survey: the extractor says "other", the classifier says post-flight.
        Mail("How was your flight to Madrid", "05", "post-flight",
             record("other", None, seg("2025-06-20", "ZZ410", "LIS", "MAD"))),
    ])

    assert [(r["flight_number"], r["evidence"]) for r in rows] == [
        ("ZZ400", "confirmed"),
        ("ZZ401", "booked only"),
        ("ZZ410", "confirmed"),
    ]
    assert rows[0]["source_count"] == "2"
    assert rows[0]["booking_reference"] == "EVD777"
    assert review.count("confirmed") == 2
    assert review.count("booked only") == 1


def test_check_in_with_no_booking_email_creates_the_flight(workspace):
    review, rows = run(workspace, [
        Mail("Your boarding pass", "01", "check-in or boarding pass",
             record("check-in", "BRD888", seg("2025-06-01", "ZZ900", "OSL", "CPH"))),
    ])

    assert flights(rows) == [("2025-06-01", "ZZ900", "OSL", "CPH")]
    assert rows[0]["evidence"] == "confirmed"
    assert rows[0]["booking_reference"] == "BRD888"


# -- duplicates and grouping ------------------------------------------------------


def test_same_flight_in_two_emails_appears_once(workspace):
    review, rows = run(workspace, [
        Mail("Your DUP999 e-ticket", "01", "booking confirmation",
             record("booking", "DUP999", seg("2025-06-01", "ZZ123", "LIS", "OSL"))),
        Mail("Your DUP999 e-ticket (resent)", "02", "booking confirmation",
             record("booking", "DUP999", seg("2025-06-01", "ZZ123", "LIS", "OSL"))),
    ])

    assert flights(rows) == [("2025-06-01", "ZZ123", "LIS", "OSL")]
    [row] = rows
    assert row["source_count"] == "2"
    assert row["source_messages"] == "2025-03-01 Your DUP999 e-ticket | 2025-03-02 Your DUP999 e-ticket (resent)"
    assert row["message_subject"] == "Your DUP999 e-ticket (resent)"
    assert row["message_date"] == "2025-03-02 10:00"
    assert review.count("bookings with replaced segments") == 0


def test_same_flight_under_two_booking_references_is_merged(workspace):
    review, rows = run(workspace, [
        Mail("Agency itinerary AGT123", "01", "booking confirmation",
             record("booking", "AGT123", seg("2025-06-01", "ZZ 123", "LIS", "OSL", confidence=0.7),
                    passengers=(ROBIN,))),
        Mail("Fictair confirmation AIR456", "02", "check-in or boarding pass",
             record("booking", "AIR456", seg("2025-06-01", "ZZ123", "LIS", "OSL", confidence=0.85),
                    passengers=(ROBIN, SAM))),
    ])

    [row] = rows
    assert row["flight_number"] == "ZZ123"  # fields of the most recent source
    assert row["booking_reference"] == "AGT123; AIR456"
    assert row["passengers"] == f"{ROBIN}; {SAM}"
    assert row["confidence"] == "0.85"
    assert row["evidence"] == "confirmed"
    assert row["source_count"] == "2"


def test_flights_with_no_flight_number_merge_on_date_and_route(workspace):
    review, rows = run(workspace, [
        Mail("Charter booking NFN111", "01", "booking confirmation",
             record("booking", "NFN111", seg("2025-06-01", "", "LIS", "FAO"))),
        Mail("Charter reminder NFN222", "02", "other",
             record("booking", "NFN222", seg("2025-06-01", "", "lis", "fao"))),
        Mail("Charter return NFN111", "03", "booking confirmation",
             record("booking", "NFN333", seg("2025-06-01", "", "FAO", "LIS"))),
    ])

    assert [(r["origin"].upper(), r["destination"].upper(), r["source_count"]) for r in rows] == [
        ("FAO", "LIS", "1"),
        ("LIS", "FAO", "2"),
    ]


def test_booking_references_match_across_case_and_whitespace(workspace):
    review, rows = run(workspace, [
        Mail("Booking WSP123 confirmed", "01", "booking confirmation",
             record("booking", "WSP123", seg("2025-06-01", "ZZ800", "LIS", "OSL"))),
        Mail("Cancellation of wsp 123", "02", "cancellation", record("cancellation", " wsp 123 ")),
    ])

    assert rows == []
    assert review.count("bookings cancelled") == 1


def test_record_without_reference_joins_the_booking_with_a_matching_segment(workspace):
    review, rows = run(workspace, [
        Mail("Booking JNR321 confirmed", "01", "booking confirmation",
             record("booking", "JNR321", seg("2025-06-01", "ZZ810", "LIS", "OSL"),
                    seg("2025-06-08", "ZZ811", "OSL", "LIS"))),
        # No reference, lists only the outbound: it joins JNR321 and cancels both legs.
        Mail("Your trip has been cancelled", "02", "cancellation",
             record("cancellation", None, seg("2025-06-01", "zz 810", "LIS", "OSL"))),
        # No reference and nothing matching: a booking of its own, untouched.
        Mail("Your trip to Madrid", "03", "booking confirmation",
             record("booking", None, seg("2025-06-15", "ZZ820", "LIS", "MAD"))),
    ])

    assert flights(rows) == [("2025-06-15", "ZZ820", "LIS", "MAD")]
    assert rows[0]["booking_reference"] == ""
    assert review.count("bookings cancelled") == 1


def test_records_with_no_segments_create_no_rows(workspace):
    review, rows = run(workspace, [
        Mail("Your train ticket TRN555", "01", "booking confirmation",
             record("other", "TRN555", passengers=(ROBIN,))),
        Mail("Your hotel booking HTL666", "02", "booking confirmation",
             record("booking", "HTL666")),
        Mail("Booking TRN555 flight add-on", "03", "booking confirmation",
             record("booking", "TRN555", seg("2025-06-01", "ZZ830", "OSL", "BGO"))),
    ])

    assert flights(rows) == [("2025-06-01", "ZZ830", "OSL", "BGO")]
    assert rows[0]["source_count"] == "1"  # the train email is not a source of the flight
    assert review.count("messages with no segments") == 2
    assert review.count("bookings cancelled") == 0


def test_output_is_deterministic_whatever_the_mailbox_order(workspace):
    mails = [
        Mail("Itinerary ORD100", "01", "booking confirmation",
             record("booking", "ORD100", seg("2025-07-01", "ZZ20", "LIS", "OSL"),
                    seg("2025-06-01", "ZZ10", "OSL", "LIS"))),
        # Two changes sent at the same moment: the tie is broken by message identifier.
        Mail("Change A for ORD100", "05", "change", record("change", "ORD100", seg("2025-07-02", "ZZ21", "LIS", "OSL"))),
        Mail("Change B for ORD100", "05", "change", record("change", "ORD100", seg("2025-07-03", "ZZ22", "LIS", "OSL"))),
        Mail("Itinerary ORD200", "02", "booking confirmation",
             record("booking", "ORD200", seg("2025-06-01", "ZZ05", "MAD", "LIS"))),
    ]

    _, forward = run(workspace, mails, name="forward")
    _, backward = run(workspace, list(reversed(mails)), name="backward")

    assert forward == backward
    assert flights(forward) == [
        ("2025-06-01", "ZZ05", "MAD", "LIS"),
        ("2025-06-01", "ZZ10", "OSL", "LIS"),
        ("2025-07-03", "ZZ22", "LIS", "OSL"),
    ]
    first = (workspace.root / "forward-review.csv").read_bytes()
    _, again = run(workspace, mails, name="forward")
    assert (workspace.root / "forward-review.csv").read_bytes() == first
