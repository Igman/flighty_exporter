"""The run command: classify, extract and review in one go, driven through
the command line against a fake Ollama. Every email, person and flight here
is invented."""

from __future__ import annotations

from fake_ollama import Chat, Decision
from mailbox_fixtures import FLIGHT_BODY, email, write_mbox
from test_extract import FLIGHT_RECORD, FLIGHT_SUBJECT
from test_markup import flight_reservation, json_ld_html, markup_email

MARKUP_SUBJECT = "Your Fictair e-ticket QZX7PL"
BROKEN_SUBJECT = "Your itinerary from Brightwing"
NEWSLETTER = "Weekly garden newsletter"
LOW_FLIGHT = "Seat upgrade offer for your trip"

MARKUP_FLIGHT = flight_reservation(
    "88", "OSL", "LIS", "2025-07-14T09:00:00+02:00", reference="MKP4RT", status="http://schema.org/ReservationConfirmed"
)

NO_NUMBER_RECORD = {
    "email_kind": "booking",
    "booking_reference": "HJK4MN",
    "passengers": ["Robin Quill"],
    "segments": [
        {"date": "2024-09-20", "airline": "Fictair", "flight_number": "",
         "origin": "OSL", "destination": "LIS", "confidence": 0.9},
    ],
}


def mailbox(workspace, name: str = "mail.mbox"):
    """A flight read from markup, two flights for the extraction model (one
    without a flight number), one message whose extraction fails, a
    newsletter, and a flight email scored below the threshold."""
    ollama = workspace.ollama
    ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    ollama.decisions["Your return flight"] = Decision(0.9, "booking confirmation")
    ollama.decisions[BROKEN_SUBJECT] = Decision(0.8, "booking confirmation")
    ollama.decisions[NEWSLETTER] = Decision(0.01, "marketing")
    ollama.decisions[LOW_FLIGHT] = Decision(0.003, "other")
    ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    ollama.chats["Your return flight"] = Chat(NO_NUMBER_RECORD)
    ollama.chats[BROKEN_SUBJECT] = Chat(content="not json at all")
    ollama.chats[LOW_FLIGHT] = Chat(FLIGHT_RECORD)
    return write_mbox(
        workspace.root / name,
        [
            markup_email(MARKUP_SUBJECT, json_ld_html(MARKUP_FLIGHT)),
            email(FLIGHT_SUBJECT, FLIGHT_BODY),
            email("Your return flight", "Your flight back from Oslo on 20 September.\n"),
            email(BROKEN_SUBJECT, "Brightwing itinerary attached.\n"),
            email(NEWSLETTER),
            email(LOW_FLIGHT, FLIGHT_BODY),
        ],
    )


def run(workspace, *args: str):
    result = workspace.run("run", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_run_writes_the_same_files_as_the_three_commands_run_separately(workspace):
    mbox = mailbox(workspace)
    separate = ("--store", "separate.db")
    for command in (("classify", "--mailbox", str(mbox)), ("extract", "--mailbox", str(mbox))):
        assert workspace.run(*command, *separate).returncode == 0
    assert workspace.run(
        "review", *separate, "--output", "separate/review.csv", "--failures", "separate/failures.csv"
    ).returncode == 0
    workspace.ollama.requests.clear()

    result = run(
        workspace, "--mailbox", str(mbox), "--store", "together.db",
        "--output", "together/review.csv", "--failures", "together/failures.csv",
    )

    for name in ("review.csv", "failures.csv"):
        together = (workspace.root / "together" / name).read_bytes()
        assert together == (workspace.root / "separate" / name).read_bytes(), name
    assert b"ZZ1234" in (workspace.root / "together" / "review.csv").read_bytes()
    assert BROKEN_SUBJECT.encode() in (workspace.root / "together" / "failures.csv").read_bytes()
    # Quick runs no audit: the below-threshold flight email is never extracted.
    assert LOW_FLIGHT not in workspace.ollama.extracted_subjects()
    assert not (workspace.root / "audit.csv").exists()
    assert "Run finished." in result.stdout
