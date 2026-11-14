"""The extract pass and the review table, driven through the command line
against a fake Ollama. Every email, person and flight here is invented."""

from __future__ import annotations

import csv
import signal
import threading
from pathlib import Path

from fake_ollama import Chat, Decision
from mailbox_fixtures import FLIGHT_BODY, email, write_mbox

FLIGHT_SUBJECT = "Your flight to Oslo is confirmed"
FLIGHT_SENDER = "Fictair <noreply@fictair.example>"

FLIGHT_RECORD = {
    "email_kind": "booking",
    "booking_reference": "QZX7PL",
    "passengers": ["Robin Quill"],
    "segments": [
        {
            "date": "2025-05-12",
            "airline": "Fictair",
            "flight_number": "ZZ1234",
            "origin": "LIS",
            "destination": "OSL",
            "confidence": 0.95,
        }
    ],
}

CONNECTING_RECORD = {
    "email_kind": "change",
    "booking_reference": "WVB3RT",
    "passengers": ["Robin Quill", "Sam Ferrow"],
    "segments": [
        {"date": "2024-11-02", "airline": "Imaginair", "flight_number": "QQ410",
         "origin": "AMS", "destination": "KEF", "confidence": 0.9},
        {"date": "2024-11-03", "airline": "Imaginair", "flight_number": "QQ77",
         "origin": "KEF", "destination": "BOS", "confidence": 0.75},
    ],
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle))


def classify(workspace, mbox: Path) -> None:
    result = workspace.run("classify", "--mailbox", str(mbox))
    assert result.returncode == 0, result.stderr


def flight_mailbox(workspace, *extra) -> Path:
    return write_mbox(
        workspace.root / "mail.mbox",
        [email(FLIGHT_SUBJECT, FLIGHT_BODY, sender=FLIGHT_SENDER), *extra],
    )


def test_successful_extraction_lands_in_the_review_table(workspace):
    mbox = flight_mailbox(workspace, email("Weekly garden newsletter"))
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.decisions["Weekly garden newsletter"] = Decision(0.01, "marketing")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    classify(workspace, mbox)

    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("extracted") == 1
    assert result.count("failed") == 0
    assert result.count("segments") == 1
    # The below-threshold newsletter is never sent to the extraction model.
    assert workspace.ollama.extracted_subjects() == [FLIGHT_SUBJECT]

    review = workspace.run("review")

    assert review.returncode == 0, review.stderr
    assert review.count("flights (one row each)") == 1
    rows = read_csv(workspace.root / "review.csv")
    assert rows == [
        {
            "keep": "yes",
            "needs_attention": "",
            "attention_reason": "",
            "date": "2025-05-12",
            "flight_number": "ZZ1234",
            "airline": "Fictair",
            "origin": "LIS",
            "destination": "OSL",
            "booking_reference": "QZX7PL",
            "passengers": "Robin Quill",
            "confidence": "0.95",
            "evidence": "booked only",
            "email_kind": "booking",
            "source": "model",
            "source_count": "1",
            "message_id": "<yourflighttoosloisconfirmed@fixtures.example>",
            "message_date": "2025-03-04 09:15",
            "message_subject": FLIGHT_SUBJECT,
            "source_messages": f"2025-03-04 {FLIGHT_SUBJECT}",
        }
    ]
    assert read_csv(workspace.root / "failures.csv") == []
    assert header(workspace.root / "failures.csv") == ["message_id", "message_date", "sender", "subject", "reason"]


def test_extraction_request_is_schema_constrained_without_thinking(workspace):
    mbox = flight_mailbox(workspace)
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    classify(workspace, mbox)

    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    [request] = workspace.ollama.chat_requests()
    assert request["model"] == "gemma4:12b-it-q4_K_M"
    assert request["stream"] is False
    assert request["think"] is False
    assert request["options"]["temperature"] == 0
    assert request["options"]["num_ctx"] == 16384
    assert request["options"]["num_predict"] == 1500
    schema = request["format"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"email_kind", "booking_reference", "passengers", "segments"}
    segment = schema["properties"]["segments"]["items"]
    assert set(segment["required"]) == {"date", "airline", "flight_number", "origin", "destination", "confidence"}
    # Sender, subject, date and the full body are sent; the schema is described in the prompt.
    text = "\n".join(m["content"] for m in request["messages"])
    assert "noreply@fictair.example" in text
    assert FLIGHT_SUBJECT in text
    assert "04 Mar 2025" in text
    assert FLIGHT_BODY.strip() in text
    assert "flight_number" in text and "YYYY-MM-DD" in text


def test_extraction_settings_come_from_config(workspace):
    mbox = flight_mailbox(workspace)
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.write_config(
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n'
        '[models]\nextraction = "tiny-extractor"\n\n'
        "[extract]\nnum_ctx = 4096\nnum_predict = 300\nretries = 0\n"
    )
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(content="not json")
    classify(workspace, mbox)

    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    [request] = workspace.ollama.chat_requests()  # retries = 0: one attempt only
    assert request["model"] == "tiny-extractor"
    assert request["options"]["num_ctx"] == 4096
    assert request["options"]["num_predict"] == 300
    assert result.count("failed") == 1


def test_long_email_is_sent_in_full(workspace):
    # About 28,000 characters: long, but inside the default extraction input cap.
    body = FLIGHT_BODY + "".join(f"Line {i} of the fare rules.\n" for i in range(1000)) + "END-OF-EMAIL-MARKER\n"
    mbox = write_mbox(workspace.root / "mail.mbox", [email(FLIGHT_SUBJECT, body)])
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    classify(workspace, mbox)

    assert workspace.run("extract", "--mailbox", str(mbox)).returncode == 0

    [request] = workspace.ollama.chat_requests()
    user = request["messages"][-1]["content"]
    assert "Line 999 of the fare rules." in user and "END-OF-EMAIL-MARKER" in user


def test_invalid_answer_is_retried_and_the_retry_succeeds(workspace):
    mbox = flight_mailbox(workspace)
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = [Chat(content='{"email_kind": "booking", '), Chat(FLIGHT_RECORD)]
    classify(workspace, mbox)

    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert workspace.ollama.extracted_subjects() == [FLIGHT_SUBJECT, FLIGHT_SUBJECT]
    assert result.count("extracted") == 1
    assert result.count("retries") == 1
    assert result.count("failed") == 0

    assert workspace.run("review").returncode == 0
    assert [r["flight_number"] for r in read_csv(workspace.root / "review.csv")] == ["ZZ1234"]
    assert read_csv(workspace.root / "failures.csv") == []


BAD_ANSWERS = {
    "Free text instead of JSON": Chat(content="Sure! The flight is ZZ1234 from Lisbon to Oslo."),
    "Wrong structure": Chat({"flights": [{"number": "ZZ1234"}]}),
    "Cut off at the output cap": Chat(content='{"email_kind": "booking", "passengers": ["', done_reason="length"),
    "Valid JSON but cut off": Chat(FLIGHT_RECORD, done_reason="length"),
    "Server error": Chat(status=500),
    "Bad date": Chat({**FLIGHT_RECORD, "segments": [{**FLIGHT_RECORD["segments"][0], "date": "12 May 2025"}]}),
    "Confidence out of range": Chat(
        {**FLIGHT_RECORD, "segments": [{**FLIGHT_RECORD["segments"][0], "confidence": 7}]}
    ),
    "Unknown email kind": Chat({**FLIGHT_RECORD, "email_kind": "itinerary"}),
}


def test_exhausted_retries_land_in_the_failures_file(workspace):
    messages = [email(subject, FLIGHT_BODY, sender="Bookings <bookings@nowhere-air.example>") for subject in BAD_ANSWERS]
    mbox = write_mbox(workspace.root / "mail.mbox", [*messages, email(FLIGHT_SUBJECT, FLIGHT_BODY)])
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")
    workspace.ollama.chats.update(BAD_ANSWERS)
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    classify(workspace, mbox)

    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("extracted") == 1
    assert result.count("failed") == len(BAD_ANSWERS)
    sent = workspace.ollama.extracted_subjects()
    for subject in BAD_ANSWERS:
        assert sent.count(subject) == 3, subject  # one attempt and two retries (the default)
    assert result.count("retries") == 2 * len(BAD_ANSWERS)

    review = workspace.run("review")

    assert review.returncode == 0, review.stderr
    assert review.count("failures") == len(BAD_ANSWERS)
    failures = read_csv(workspace.root / "failures.csv")
    assert sorted(f["subject"] for f in failures) == sorted(BAD_ANSWERS)
    for failure in failures:
        assert failure["sender"] == "Bookings <bookings@nowhere-air.example>"
        assert failure["message_date"] == "2025-03-04 09:15"
        assert failure["reason"]
    reasons = {f["subject"]: f["reason"] for f in failures}
    assert "length" in reasons["Cut off at the output cap"]
    assert "length" in reasons["Valid JSON but cut off"]
    assert "500" in reasons["Server error"]
    # The one good message is in the review table; no bad one is.
    assert [r["message_subject"] for r in read_csv(workspace.root / "review.csv")] == [FLIGHT_SUBJECT]


def test_failed_extraction_is_retried_on_rerun(workspace):
    mbox = flight_mailbox(workspace)
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(status=500)
    classify(workspace, mbox)
    assert workspace.run("extract", "--mailbox", str(mbox)).count("failed") == 1

    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("extracted") == 1
    assert result.count("failed") == 0
    assert workspace.run("review").returncode == 0
    assert read_csv(workspace.root / "failures.csv") == []
    assert len(read_csv(workspace.root / "review.csv")) == 1


def test_below_threshold_message_is_left_alone_until_the_threshold_is_lowered(workspace):
    mbox = flight_mailbox(workspace, email("Probably nothing"))
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.decisions["Probably nothing"] = Decision(0.03, "other")
    classify(workspace, mbox)

    first = workspace.run("extract", "--mailbox", str(mbox))

    assert first.returncode == 0, first.stderr
    assert first.count("selected (gate >= 0.05)") == 1
    assert workspace.ollama.extracted_subjects() == [FLIGHT_SUBJECT]
    assert workspace.run("review").returncode == 0
    assert read_csv(workspace.root / "failures.csv") == []

    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[classify]\nthreshold = 0.02\n')
    decisions_before = len(workspace.ollama.decision_requests())
    second = workspace.run("extract", "--mailbox", str(mbox))

    assert second.returncode == 0, second.stderr
    assert second.count("extracted") == 1
    assert second.count("already extracted") == 1
    assert workspace.ollama.extracted_subjects() == [FLIGHT_SUBJECT, "Probably nothing"]
    assert len(workspace.ollama.decision_requests()) == decisions_before


def test_rerun_sends_nothing_and_a_new_extraction_model_extracts_again(workspace):
    mbox = flight_mailbox(workspace)
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    classify(workspace, mbox)
    assert workspace.run("extract", "--mailbox", str(mbox)).returncode == 0

    rerun = workspace.run("extract", "--mailbox", str(mbox))

    assert rerun.returncode == 0, rerun.stderr
    assert rerun.count("extracted") == 0
    assert rerun.count("already extracted") == 1
    assert rerun.count("segments") == 1  # summarised from the store
    assert len(workspace.ollama.chat_requests()) == 1

    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[models]\nextraction = "other-extractor"\n')
    changed = workspace.run("extract", "--mailbox", str(mbox))

    assert changed.returncode == 0, changed.stderr
    assert changed.count("extracted") == 1
    assert [r["model"] for r in workspace.ollama.chat_requests()] == ["gemma4:12b-it-q4_K_M", "other-extractor"]


def test_classify_and_extract_never_call_each_others_model(workspace):
    mbox = flight_mailbox(workspace)
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")

    classify(workspace, mbox)
    assert workspace.ollama.chat_requests() == []
    decisions = len(workspace.ollama.decision_requests())

    assert workspace.run("extract", "--mailbox", str(mbox)).returncode == 0
    assert len(workspace.ollama.decision_requests()) == decisions
    assert len(workspace.ollama.chat_requests()) == 1


def test_email_with_no_flight_is_a_valid_extraction_with_no_rows(workspace):
    mbox = flight_mailbox(workspace, email("Your train ticket to Bergen"))
    workspace.ollama.default_decision = Decision(0.4, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    workspace.ollama.chats["Your train ticket to Bergen"] = Chat(
        {"email_kind": "other", "booking_reference": "TRN555", "passengers": ["Robin Quill"], "segments": []}
    )
    classify(workspace, mbox)

    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("extracted") == 2
    assert result.count("failed") == 0
    assert result.count("messages with no segments") == 1
    review = workspace.run("review")
    assert review.returncode == 0, review.stderr
    assert review.count("messages with no segments") == 1
    assert [r["message_subject"] for r in read_csv(workspace.root / "review.csv")] == [FLIGHT_SUBJECT]
    assert read_csv(workspace.root / "failures.csv") == []


def test_one_row_per_segment_and_one_row_per_flight(workspace):
    mbox = flight_mailbox(
        workspace,
        email("Reminder: your Oslo flight", FLIGHT_BODY, date="Wed, 05 Mar 2025 09:15:00 +0000"),
        email("Schedule change for WVB3RT", "Your Imaginair flights have changed."),
    )
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    workspace.ollama.chats["Reminder: your Oslo flight"] = Chat(FLIGHT_RECORD)
    workspace.ollama.chats["Schedule change for WVB3RT"] = Chat(CONNECTING_RECORD)
    classify(workspace, mbox)
    assert workspace.run("extract", "--mailbox", str(mbox)).returncode == 0

    result = workspace.run("review")

    assert result.returncode == 0, result.stderr
    rows = read_csv(workspace.root / "review.csv")
    # The connecting itinerary gives one row per segment; the Oslo flight,
    # found in two emails of the same booking, gives one row with both sources.
    assert [(r["date"], r["flight_number"], r["source_count"]) for r in rows] == [
        ("2024-11-02", "QQ410", "1"),
        ("2024-11-03", "QQ77", "1"),
        ("2025-05-12", "ZZ1234", "2"),
    ]
    assert all(r["keep"] == "yes" for r in rows)
    assert rows[0]["passengers"] == "Robin Quill; Sam Ferrow"
    assert rows[1]["email_kind"] == "change"
    assert rows[1]["confidence"] == "0.75"
    assert rows[2]["message_subject"] == "Reminder: your Oslo flight"
    assert header(workspace.root / "review.csv") == [
        "keep", "needs_attention", "attention_reason", "date", "flight_number", "airline", "origin",
        "destination", "booking_reference", "passengers", "confidence", "evidence", "email_kind", "source", "source_count",
        "message_id", "message_date", "message_subject", "source_messages",
    ]


def test_messages_sharing_a_message_id_are_extracted_once(workspace):
    mbox = write_mbox(
        workspace.root / "mail.mbox",
        [
            email(FLIGHT_SUBJECT, FLIGHT_BODY, message_id="<same@fixtures.example>"),
            email(FLIGHT_SUBJECT, FLIGHT_BODY, message_id="<same@fixtures.example>", labels="Inbox,Travel"),
        ],
    )
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    classify(workspace, mbox)

    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert len(workspace.ollama.chat_requests()) == 1
    assert workspace.run("review").returncode == 0
    assert len(read_csv(workspace.root / "review.csv")) == 1


def test_extract_without_a_mailbox_is_a_clear_error(workspace):
    mbox = flight_mailbox(workspace)
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    classify(workspace, mbox)

    result = workspace.run("extract")
    assert result.returncode != 0
    assert "mailbox" in result.stderr.lower()

    result = workspace.run("extract", "--mailbox", str(workspace.root / "gone.mbox"))
    assert result.returncode != 0
    assert "gone.mbox" in result.stderr
    assert workspace.ollama.chat_requests() == []


def test_message_no_longer_in_the_mailbox_is_reported(workspace):
    mbox = flight_mailbox(workspace, email("Vanishing itinerary", FLIGHT_BODY))
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    classify(workspace, mbox)
    smaller = write_mbox(workspace.root / "smaller.mbox", [email(FLIGHT_SUBJECT, FLIGHT_BODY, sender=FLIGHT_SENDER)])

    result = workspace.run("extract", "--mailbox", str(smaller))

    assert result.returncode == 0, result.stderr
    assert result.count("extracted") == 1
    assert result.count("not in mailbox") == 1
    assert "vanishingitinerary@fixtures.example" in result.stdout
    assert "no longer in the mailbox" in result.stdout
    assert workspace.run("review").returncode == 0
    [failure] = read_csv(workspace.root / "failures.csv")
    assert failure["subject"] == "Vanishing itinerary"
    assert "no longer in the mailbox" in failure["reason"]


def test_extract_before_classify_says_what_to_do(workspace):
    mbox = flight_mailbox(workspace)

    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("extracted") == 0
    assert "classify" in result.stdout.lower()
    assert workspace.ollama.chat_requests() == []


def test_interrupted_extract_resumes_without_resending(workspace):
    subjects = [f"Invented itinerary number {i}" for i in range(6)]
    mbox = write_mbox(workspace.root / "mail.mbox", [email(s, FLIGHT_BODY) for s in subjects])
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")
    classify(workspace, mbox)

    third_request = threading.Event()
    release = threading.Event()

    def block_on_third(request):
        if request.path == "/api/chat" and len(workspace.ollama.chat_requests()) == 3:
            third_request.set()
            release.wait(timeout=30)

    workspace.ollama.on_request = block_on_third
    proc = workspace.start("extract", "--mailbox", str(mbox))
    try:
        assert third_request.wait(timeout=30), "extract never sent a third request"
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        release.set()
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode != 0
    assert "interrupt" in (stdout + stderr).lower()
    first_run = workspace.ollama.extracted_subjects()
    assert len(first_run) == 3

    workspace.ollama.on_request = None
    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    second_run = workspace.ollama.extracted_subjects()[len(first_run):]
    assert len(second_run) == 4  # the two finished messages are not sent again
    assert not set(first_run[:2]) & set(second_run)
    assert sorted(set(first_run[:2]) | set(second_run)) == sorted(subjects)
    assert result.count("already extracted") == 2


def test_progress_is_shown_during_extract(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email(f"Itinerary {i}", FLIGHT_BODY) for i in range(3)])
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")
    classify(workspace, mbox)

    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert "extract: 3/3" in result.stderr
    assert "ETA" in result.stderr


def test_unreachable_ollama_stops_extract_with_a_clear_message(workspace):
    mbox = flight_mailbox(workspace)
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    classify(workspace, mbox)
    workspace.write_config('[ollama]\naddress = "http://127.0.0.1:9"\n')

    result = workspace.run("extract", "--mailbox", str(mbox))

    assert result.returncode != 0
    assert "ollama" in result.stderr.lower()
    assert "127.0.0.1:9" in result.stderr
