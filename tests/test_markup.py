"""Machine-readable flight data (schema.org FlightReservation markup in JSON-LD
or microdata, and flight calendar files), driven through the command line
(classify, extract, review) against the fake Ollama. Assertions are on what
the fake received (a message resolved from markup reaches neither model), the
printed summaries and the review CSV. Every email, person, booking and flight
here is invented.
"""

from __future__ import annotations

import json
from typing import Any

from fake_ollama import Chat, Decision, chat_user_text, decision_body
from mailbox_fixtures import email, write_mbox
from test_consolidation import ROBIN, SAM, record, seg
from test_extract import read_csv

SCHEMA = "http://schema.org/"


# -- building invented markup ------------------------------------------------------


def flight_reservation(
    number: str = "1234",
    origin: str | None = "LIS",
    destination: str | None = "OSL",
    departure: str = "2025-06-01T07:40:00+01:00",
    *,
    airline_code: str | None = "ZZ",
    airline_name: str = "Fictair",
    reference: str | None = "QZX7PL",
    passenger: Any = ROBIN,
    status: str = "http://schema.org/ReservationConfirmed",
    type_: Any = "FlightReservation",
    airline_key: str = "airline",
) -> dict[str, Any]:
    flight: dict[str, Any] = {"@type": "Flight", "flightNumber": number, "departureTime": departure}
    airline: dict[str, Any] = {"@type": "Airline", "name": airline_name}
    if airline_code is not None:
        airline["iataCode"] = airline_code
    flight[airline_key] = airline
    if origin is not None:
        flight["departureAirport"] = {"@type": "Airport", "iataCode": origin}
    if destination is not None:
        flight["arrivalAirport"] = {"@type": "Airport", "iataCode": destination}
    item: dict[str, Any] = {"@context": "http://schema.org", "@type": type_, "reservationFor": flight,
                            "reservationStatus": status}
    if reference is not None:
        item["reservationNumber"] = reference
    if passenger is not None:
        item["underName"] = passenger if not isinstance(passenger, str) else {"@type": "Person", "name": passenger}
    return item


def json_ld_html(*blocks: Any, visible: str = "Thank you for booking with Fictair.") -> str:
    scripts = "\n".join(
        f'<script type="application/ld+json">{b if isinstance(b, str) else json.dumps(b)}</script>' for b in blocks
    )
    return f"<html><head>{scripts}</head><body><p>{visible}</p></body></html>"


def markup_email(subject: str, html: str, *, day: str = "01", **kwargs: Any):
    # An empty plain part, so the HTML (reduced to text) is the body the models see.
    return email(subject, "\n", html=html,
                 date=f"{day} Mar 2025 10:00:00 +0000", **kwargs)


def ics(*events: str, method: str = "PUBLISH") -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Fictair//Invented//EN", f"METHOD:{method}"]
    for event in events:
        lines += ["BEGIN:VEVENT", *event.strip().splitlines(), "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


FLIGHT_EVENT = """UID:zz1234-20250601@fictair.example
SUMMARY:Flight ZZ 1234 LIS → OSL
DTSTART;TZID=Europe/Lisbon:20250601T074000
DTEND;TZID=Europe/Oslo:20250601T125500
LOCATION:Lisbon (LIS)
DESCRIPTION:Fictair flight ZZ 1234\\nLisbon (LIS) to Oslo (OSL)\\nBooking ref
 erence: QZX7PL\\nPassenger: Robin Quill"""

MEETING_EVENT = """UID:sync-1@quillco.example
SUMMARY:Quarterly planning sync
DTSTART;TZID=Europe/Lisbon:20250601T100000
LOCATION:Room 4\\, second floor
DESCRIPTION:Agenda: budget\\, hiring."""


def ics_attachment(data: bytes, filename: str = "flight.ics") -> list[tuple[str, bytes, str]]:
    return [(filename, data, "text/calendar")]


# -- running the commands ---------------------------------------------------------


def run_all(workspace, messages, *, name: str = "mail", config: str = ""):
    """Classify, extract and review; return (classify, extract, review, rows)."""
    if config:
        workspace.config_path.write_text(f'[ollama]\naddress = "{workspace.ollama.url}"\n{config}')
    mbox = write_mbox(workspace.root / f"{name}.mbox", messages)
    classify = workspace.run("classify", "--mailbox", str(mbox))
    assert classify.returncode == 0, classify.stderr
    extract = workspace.run("extract", "--mailbox", str(mbox))
    assert extract.returncode == 0, extract.stderr
    review = workspace.run("review", "--force")
    assert review.returncode == 0, review.stderr
    return classify, extract, review, read_csv(workspace.root / "review.csv")


def requested_subjects(workspace) -> set[str]:
    return set(workspace.ollama.classified_subjects()) | set(workspace.ollama.extracted_subjects())


def flights(rows: list[dict[str, str]]) -> list[tuple[str, ...]]:
    return [(r["date"], r["flight_number"], r["origin"], r["destination"], r["source"]) for r in rows]


def model_flight(workspace, subject: str, rec: dict[str, Any], email_type: str = "booking confirmation") -> None:
    workspace.ollama.decisions[subject] = Decision(0.9, email_type)
    workspace.ollama.chats[subject] = Chat(rec)


# -- each markup form, with no model request ---------------------------------------------


def test_json_ld_two_leg_booking_is_read_without_any_model_request(workspace):
    html = json_ld_html([
        flight_reservation("1234", "LIS", "OSL", "2025-06-01T07:40:00+01:00"),
        flight_reservation("1235", "OSL", "LIS", "2025-06-08T18:05:00+02:00"),
    ])
    classify, extract, review, rows = run_all(workspace, [
        markup_email("Your Fictair booking QZX7PL", html),
        email("Weekly newsletter"),
    ])

    assert requested_subjects(workspace) == {"Weekly newsletter"}
    assert workspace.ollama.chat_requests() == []
    assert classify.count("resolved from machine-readable data") == 1
    assert classify.count("classified") == 1
    assert flights(rows) == [
        ("2025-06-01", "ZZ1234", "LIS", "OSL", "markup"),
        ("2025-06-08", "ZZ1235", "OSL", "LIS", "markup"),
    ]
    for row in rows:
        assert (row["keep"], row["needs_attention"], row["confidence"]) == ("yes", "", "1.00")
        assert (row["booking_reference"], row["passengers"], row["airline"]) == ("QZX7PL", ROBIN, "Fictair")
        assert (row["email_kind"], row["evidence"]) == ("booking", "booked only")
        assert row["message_subject"] == "Your Fictair booking QZX7PL"
    assert review.count("messages extracted") == 1


def test_microdata_booking_is_read_without_any_model_request(workspace):
    html = """<html><body>
    <div itemscope itemtype="http://schema.org/FlightReservation">
      <meta itemprop="reservationNumber" content="MDQ123">
      <link itemprop="reservationStatus" href="http://schema.org/ReservationConfirmed">
      <div itemprop="underName" itemscope itemtype="http://schema.org/Person">
        Passenger: <span itemprop="name">Robin Quill</span>
      </div>
      <div itemprop="reservationFor" itemscope itemtype="http://schema.org/Flight">
        <p>Flight <span itemprop="flightNumber">77</span></p>
        <div itemprop="airline" itemscope itemtype="http://schema.org/Airline">
          <span itemprop="name">Imaginair</span><meta itemprop="iataCode" content="QQ">
        </div>
        <div itemprop="departureAirport" itemscope itemtype="http://schema.org/Airport">
          <span itemprop="name">Copenhagen</span> (<span itemprop="iataCode">CPH</span>)
        </div>
        <div itemprop="arrivalAirport" itemscope itemtype="http://schema.org/Airport">
          <meta itemprop="name" content="Keflavik"><meta itemprop="iataCode" content="KEF">
        </div>
        <time itemprop="departureTime" datetime="2025-07-14T23:50:00+02:00">14 July, 23:50</time>
      </div>
    </div></body></html>"""
    classify, extract, review, rows = run_all(workspace, [markup_email("Imaginair e-ticket MDQ123", html)])

    assert workspace.ollama.requests == []
    assert classify.count("resolved from machine-readable data") == 1
    [row] = rows
    assert flights(rows) == [("2025-07-14", "QQ77", "CPH", "KEF", "markup")]
    assert (row["airline"], row["booking_reference"], row["passengers"], row["confidence"], row["keep"]) == (
        "Imaginair", "MDQ123", ROBIN, "1.00", "yes")


def test_flight_calendar_attachment_is_read_without_any_model_request(workspace):
    classify, extract, review, rows = run_all(workspace, [
        email("Your trip to Oslo", "Your itinerary is attached.\n", attachments=ics_attachment(ics(FLIGHT_EVENT)),
              date="01 Mar 2025 10:00:00 +0000"),
    ])

    assert workspace.ollama.requests == []
    assert classify.count("resolved from machine-readable data") == 1
    [row] = rows
    assert flights(rows) == [("2025-06-01", "ZZ1234", "LIS", "OSL", "markup")]
    # A plainly labelled reference and passenger are taken; the airline name is not in the file.
    assert (row["booking_reference"], row["passengers"], row["airline"], row["confidence"], row["keep"]) == (
        "QZX7PL", ROBIN, "", "1.00", "yes")


# -- the fall-through cases ------------------------------------------------------------


def test_incomplete_markup_falls_through_to_both_models_and_the_model_record_is_used(workspace):
    subject = "Booking INC001 confirmed"
    html = json_ld_html(flight_reservation("1234", "LIS", None, reference="INC001"),
                        visible="Flight ZZ1234 Lisbon (LIS) to Oslo (OSL), 1 June 2025.")
    model_flight(workspace, subject, record("booking", "INC001", seg("2025-06-01", "ZZ1234", "LIS", "OSL", 0.85)))
    classify, extract, review, rows = run_all(workspace, [markup_email(subject, html)])

    assert workspace.ollama.classified_subjects() == [subject]
    assert workspace.ollama.extracted_subjects() == [subject]
    assert classify.count("resolved from machine-readable data") == 0
    assert classify.count("classified") == 1
    assert extract.count("extracted") == 1
    [row] = rows
    assert flights(rows) == [("2025-06-01", "ZZ1234", "LIS", "OSL", "model")]
    assert row["confidence"] == "0.85"


def test_markup_with_one_incomplete_leg_yields_nothing_at_all(workspace):
    # Never a partial markup record mixed with model output: the whole message goes to the models.
    subject = "Two legs, one broken"
    html = json_ld_html(flight_reservation("1234", "LIS", "OSL"),
                        flight_reservation("1235", "OSL", "LIS", departure="next Sunday"))
    model_flight(workspace, subject, record("booking", "QZX7PL", seg("2025-06-01", "ZZ1234", "LIS", "OSL"),
                                            seg("2025-06-08", "ZZ1235", "OSL", "LIS")))
    classify, extract, review, rows = run_all(workspace, [markup_email(subject, html)])

    assert classify.count("resolved from machine-readable data") == 0
    assert workspace.ollama.extracted_subjects() == [subject]
    assert {r["source"] for r in rows} == {"model"}
    assert len(rows) == 2


def test_digits_only_flight_number_without_an_airline_code_falls_through(workspace):
    subject = "Number without airline"
    html = json_ld_html(flight_reservation("1234", airline_code=None))
    model_flight(workspace, subject, record("booking", "QZX7PL", seg("2025-06-01", "ZZ1234", "LIS", "OSL")))
    classify, extract, review, rows = run_all(workspace, [markup_email(subject, html)])

    assert classify.count("resolved from machine-readable data") == 0
    assert workspace.ollama.extracted_subjects() == [subject]
    assert flights(rows) == [("2025-06-01", "ZZ1234", "LIS", "OSL", "model")]


def test_utc_only_calendar_event_falls_through(workspace):
    subject = "Trip to Oslo (calendar)"
    event = FLIGHT_EVENT.replace("DTSTART;TZID=Europe/Lisbon:20250601T074000", "DTSTART:20250601T064000Z")
    model_flight(workspace, subject, record("booking", "QZX7PL", seg("2025-06-01", "ZZ1234", "LIS", "OSL")))
    classify, extract, review, rows = run_all(workspace, [
        email(subject, "Itinerary attached.\n", attachments=ics_attachment(ics(event)))])

    assert classify.count("resolved from machine-readable data") == 0
    assert workspace.ollama.classified_subjects() == [subject]
    assert workspace.ollama.extracted_subjects() == [subject]
    assert flights(rows) == [("2025-06-01", "ZZ1234", "LIS", "OSL", "model")]


def test_non_flight_markup_and_meeting_invites_are_ignored_and_scripts_never_reach_the_models(workspace):
    hotel = json_ld_html({
        "@context": "https://schema.org", "@type": "LodgingReservation", "reservationNumber": "HTL555",
        "underName": {"@type": "Person", "name": ROBIN},
        "reservationFor": {"@type": "LodgingBusiness", "name": "Hotel Quillfield",
                           "address": "1 Invented Street"},
        "checkinDate": "2025-06-01T15:00:00+02:00",
    }, {"@context": "https://schema.org", "@type": "Order", "orderNumber": "ORD-9", "merchant": {"name": "Shop"}},
        visible="Your stay at Hotel Quillfield is confirmed.")
    classify, extract, review, rows = run_all(workspace, [
        markup_email("Hotel Quillfield reservation", hotel),
        email("Planning sync invite", "Join us.\n", attachments=ics_attachment(ics(MEETING_EVENT), "invite.ics")),
    ])

    assert classify.count("resolved from machine-readable data") == 0
    assert sorted(workspace.ollama.classified_subjects()) == ["Hotel Quillfield reservation", "Planning sync invite"]
    hotel_body = next(decision_body(b) for b in workspace.ollama.decision_requests()
                      if b["state"]["subject"] == "Hotel Quillfield reservation")
    assert "Your stay at Hotel Quillfield is confirmed." in hotel_body
    assert "LodgingReservation" not in hotel_body and "@type" not in hotel_body
    assert rows == []


def test_html_with_markup_does_not_leak_its_script_into_the_extractors_text(workspace):
    subject = "Booking LEAK01"
    html = json_ld_html(flight_reservation("1234", None, "OSL", reference="LEAK01"),
                        visible="Flight ZZ1234 from Lisbon to Oslo.")
    model_flight(workspace, subject, record("booking", "LEAK01", seg("2025-06-01", "ZZ1234", "LIS", "OSL")))
    run_all(workspace, [markup_email(subject, html)])

    [chat] = workspace.ollama.chat_requests()
    text = chat_user_text(chat)
    assert "Flight ZZ1234 from Lisbon to Oslo." in text
    assert "reservationFor" not in text and "application/ld+json" not in text


# -- JSON-LD variants ------------------------------------------------------------------


def test_list_graph_and_prefixed_types_with_a_malformed_block_beside_them(workspace):
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "name": "Fictair"},
        flight_reservation("ZZ300", "LIS", "MAD", "2025-06-03T09:00:00+01:00", reference="GRAPH1",
                           type_="https://schema.org/FlightReservation"),
    ]}
    as_list = [flight_reservation("400", "MAD", "LIS", "2025-06-04T09:00:00+02:00", reference="LIST01",
                                  type_=["http://schema.org/Reservation", "http://schema.org/FlightReservation"],
                                  airline_key="provider")]
    # Comment-wrapped and HTML-escaped, with whitespace around values.
    escaped = flight_reservation(" ZZ500 ", " LIS ", " OSL ", " 2025-06-05T07:00:00 ", reference=" ESC001 ",
                                 passenger=f"  {ROBIN} ")
    escaped_text = "<!--\n" + json.dumps(escaped).replace('"', "&quot;") + "\n-->"
    html = json_ld_html(graph, "{ this is not JSON", as_list, escaped_text)
    classify, extract, review, rows = run_all(workspace, [markup_email("Three bookings in one email", html)])

    assert workspace.ollama.requests == []
    assert classify.count("resolved from machine-readable data") == 1
    assert [(r["date"], r["flight_number"], r["origin"], r["destination"], r["passengers"]) for r in rows] == [
        ("2025-06-03", "ZZ300", "LIS", "MAD", ROBIN),
        ("2025-06-04", "ZZ400", "MAD", "LIS", ROBIN),
        ("2025-06-05", "ZZ500", "LIS", "OSL", ROBIN),
    ]
    # Reservations disagreeing on the booking reference: the first is used for the whole message.
    assert {r["booking_reference"] for r in rows} == {"GRAPH1"}


def test_two_passengers_on_the_same_flight_make_one_row_naming_both(workspace):
    html = json_ld_html([
        flight_reservation("1234", passenger=ROBIN),
        flight_reservation("1234", passenger=[{"@type": "Person", "name": SAM}]),
    ])
    classify, extract, review, rows = run_all(workspace, [markup_email("Booking for two", html)],
                                              config='[passenger]\nnames = ["Robin Quill"]\n')

    [row] = rows
    assert (row["flight_number"], row["passengers"], row["keep"], row["source"]) == (
        "ZZ1234", f"{ROBIN}; {SAM}", "yes", "markup")


def test_markup_for_another_passenger_is_dropped_like_any_booking(workspace):
    html = json_ld_html(flight_reservation("1234", passenger=SAM, reference="SAMBKG"))
    classify, extract, review, rows = run_all(workspace, [markup_email("Booking for Sam", html)],
                                              config='[passenger]\nnames = ["Robin Quill"]\n')

    assert rows == []
    assert review.count("bookings for other passengers (dropped)") == 1


# -- cancellations -----------------------------------------------------------------------


def test_cancelled_reservation_cancels_the_booking(workspace):
    booked = json_ld_html(flight_reservation("1234", reference="CXL777"))
    cancelled = json_ld_html(flight_reservation("1234", reference="CXL777",
                                                status="http://schema.org/ReservationCancelled"))
    classify, extract, review, rows = run_all(workspace, [
        markup_email("Booking CXL777 confirmed", booked, day="01"),
        markup_email("Booking CXL777 cancelled", cancelled, day="05"),
    ])

    assert workspace.ollama.requests == []
    assert classify.count("resolved from machine-readable data") == 2
    assert rows == []
    assert review.count("bookings cancelled") == 1


def test_partial_cancellation_drops_only_the_cancelled_leg(workspace):
    html = json_ld_html([
        flight_reservation("1234", "LIS", "OSL", reference="PART01"),
        flight_reservation("1235", "OSL", "LIS", "2025-06-08T18:00:00+02:00", reference="PART01",
                           status="https://schema.org/ReservationCanceled"),
    ])
    classify, extract, review, rows = run_all(workspace, [markup_email("Return leg cancelled", html)])

    assert flights(rows) == [("2025-06-01", "ZZ1234", "LIS", "OSL", "markup")]
    assert rows[0]["email_kind"] == "booking"


def test_calendar_cancellation_cancels_an_earlier_booking(workspace):
    booked = "Booking QZX7PL confirmed"
    model_flight(workspace, booked, record("booking", None, seg("2025-06-01", "ZZ1234", "LIS", "OSL")))
    classify, extract, review, rows = run_all(workspace, [
        email(booked, "Flight ZZ1234 LIS-OSL on 1 June.\n", date="01 Mar 2025 10:00:00 +0000"),
        email("Cancelled: Flight ZZ 1234", "This event has been cancelled.\n",
              attachments=ics_attachment(ics(FLIGHT_EVENT.replace("Booking ref\n erence: QZX7PL\\n", "")
                                             + "\nSTATUS:CANCELLED", method="CANCEL")),
              date="05 Mar 2025 10:00:00 +0000"),
    ])

    assert classify.count("resolved from machine-readable data") == 1
    assert "Cancelled: Flight ZZ 1234" not in requested_subjects(workspace)
    assert rows == []
    assert review.count("bookings cancelled") == 1


# -- precedence and mixing ---------------------------------------------------------------


def test_markup_wins_over_a_calendar_file_in_the_same_message(workspace):
    html = json_ld_html(flight_reservation("300", "LIS", "MAD", "2025-06-03T09:00:00+01:00"))
    classify, extract, review, rows = run_all(workspace, [
        markup_email("Booking with markup and calendar", html, attachments=ics_attachment(ics(FLIGHT_EVENT))),
    ])

    assert workspace.ollama.requests == []
    assert flights(rows) == [("2025-06-03", "ZZ300", "LIS", "MAD", "markup")]


def test_markup_and_a_model_record_for_the_same_booking_make_one_flight(workspace):
    reminder = "Your flight ZZ1234 is tomorrow"
    model_flight(workspace, reminder, record("check-in", "QZX7PL", seg("2025-06-01", "ZZ1234", "LIS", "OSL", 0.8)),
                 email_type="check-in or boarding pass")
    classify, extract, review, rows = run_all(workspace, [
        markup_email("Your Fictair booking QZX7PL", json_ld_html(flight_reservation("1234")), day="01"),
        email(reminder, "Check in now for ZZ1234 LIS to OSL.\n", date="31 May 2025 10:00:00 +0000"),
    ])

    [row] = rows
    assert (row["flight_number"], row["source"], row["source_count"]) == ("ZZ1234", "markup; model", "2")
    assert (row["confidence"], row["evidence"], row["keep"]) == ("1.00", "confirmed", "yes")


def test_rerunning_classify_on_an_existing_store_lets_markup_take_precedence(workspace):
    subject = "Your Fictair booking QZX7PL"
    message_id = "<qzx7pl@fixtures.example>"
    # An earlier run classified and extracted the message with the models (here the
    # mbox copy carried no markup); the model misread the flight number.
    model_flight(workspace, subject, record("booking", "QZX7PL", seg("2025-06-01", "ZZ1284", "LIS", "OSL", 0.8)))
    plain = write_mbox(workspace.root / "before.mbox", [
        email(subject, "Flight ZZ1234 LIS-OSL.\n", message_id=message_id)])
    for command in ("classify", "extract"):
        assert workspace.run(command, "--mailbox", str(plain)).returncode == 0
    assert workspace.ollama.extracted_subjects() == [subject]
    workspace.ollama.requests.clear()

    mbox = write_mbox(workspace.root / "after.mbox", [
        markup_email(subject, json_ld_html(flight_reservation("1234")), message_id=message_id)])
    classify = workspace.run("classify", "--mailbox", str(mbox))
    assert classify.returncode == 0, classify.stderr
    assert classify.count("resolved from machine-readable data") == 1
    assert classify.count("already classified") == 0
    extract = workspace.run("extract", "--mailbox", str(mbox))
    assert extract.returncode == 0, extract.stderr
    assert workspace.ollama.requests == []
    review = workspace.run("review", "--force")
    assert review.returncode == 0, review.stderr
    assert flights(read_csv(workspace.root / "review.csv")) == [("2025-06-01", "ZZ1234", "LIS", "OSL", "markup")]

    again = workspace.run("classify", "--mailbox", str(mbox))
    assert again.count("resolved from machine-readable data") == 0
    assert again.count("already resolved from machine-readable data") == 1
    assert workspace.ollama.requests == []


def test_unknown_airport_code_in_markup_is_flagged_and_blocked(workspace):
    html = json_ld_html(flight_reservation("1234", "LIS", "XQX"))
    classify, extract, review, rows = run_all(workspace, [markup_email("Booking to nowhere", html)])

    [row] = rows
    assert (row["keep"], row["needs_attention"], row["source"]) == ("no", "yes", "markup")
    assert row["attention_reason"].startswith("unknown airport XQX")
    assert review.count("unknown airport (XQX)") == 1
