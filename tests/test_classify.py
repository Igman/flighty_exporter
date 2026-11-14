"""The classify pass, driven through the command line against a fake Ollama."""

from __future__ import annotations

import signal
import threading

from fake_ollama import Decision
from mailbox_fixtures import FLIGHT_BODY, email, write_mbox

EMAIL_TYPES = [
    "booking confirmation",
    "change",
    "cancellation",
    "check-in or boarding pass",
    "post-flight",
    "marketing",
    "other",
]


def test_classifies_every_message_and_reports_counts(workspace):
    mbox = write_mbox(
        workspace.root / "mail.mbox",
        [
            email("Your flight to Oslo is confirmed", FLIGHT_BODY, sender="Fictair <noreply@fictair.example>"),
            email("Weekly garden newsletter"),
            email("Lunch on Friday?"),
        ],
    )
    workspace.ollama.decisions["Your flight to Oslo is confirmed"] = Decision(0.97, "booking confirmation")

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("classified") == 3
    assert result.count("skipped") == 0
    assert result.count("failed") == 0
    assert sorted(workspace.ollama.classified_subjects()) == sorted(
        ["Your flight to Oslo is confirmed", "Weekly garden newsletter", "Lunch on Friday?"]
    )


def test_mailbox_can_come_from_config(workspace):
    write_mbox(workspace.root / "takeout.mbox", [email("Hello from config")])
    workspace.write_config(f'mailbox = "takeout.mbox"\n\n[ollama]\naddress = "{workspace.ollama.url}"\n')

    result = workspace.run("classify")

    assert result.returncode == 0, result.stderr
    assert result.count("classified") == 1
    assert workspace.ollama.classified_subjects() == ["Hello from config"]


def test_command_line_mailbox_overrides_config(workspace):
    write_mbox(workspace.root / "a.mbox", [email("From config file")])
    other = write_mbox(workspace.root / "b.mbox", [email("From command line")])
    workspace.write_config(f'mailbox = "a.mbox"\n\n[ollama]\naddress = "{workspace.ollama.url}"\n')

    result = workspace.run("classify", "--mailbox", str(other))

    assert result.returncode == 0, result.stderr
    assert workspace.ollama.classified_subjects() == ["From command line"]


def test_missing_mailbox_is_a_clear_error(workspace):
    result = workspace.run("classify")
    assert result.returncode != 0
    assert "mailbox" in result.stderr.lower()

    result = workspace.run("classify", "--mailbox", str(workspace.root / "nope.mbox"))
    assert result.returncode != 0
    assert "nope.mbox" in result.stderr


def test_spam_trash_drafts_and_chat_are_never_sent_to_the_model(workspace):
    mbox = write_mbox(
        workspace.root / "mail.mbox",
        [
            email("Spam message", labels="Spam"),
            email("Trashed message", labels="Inbox,Trash"),
            email("Draft message", labels="Drafts"),
            email("Chat transcript", labels="Chat,Archived"),
            email("Kept message", labels="Inbox,Category Updates"),
            email("User label that only looks similar", labels="Chats with Kim,Trash talk"),
            email("No labels at all", labels=None),
        ],
    )

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("skipped") == 4
    assert result.count("classified") == 3
    assert sorted(workspace.ollama.classified_subjects()) == sorted(
        ["Kept message", "User label that only looks similar", "No labels at all"]
    )


def test_request_carries_sender_subject_body_and_both_questions(workspace):
    mbox = write_mbox(
        workspace.root / "mail.mbox",
        [email("Your flight to Oslo is confirmed", FLIGHT_BODY, sender="Fictair <noreply@fictair.example>")],
    )

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    [request] = workspace.ollama.decision_requests()
    assert request["model"] == "nimble"
    state_text = "\n".join(str(v) for v in request["state"].values())
    assert "noreply@fictair.example" in state_text
    assert "Your flight to Oslo is confirmed" in state_text
    assert "Flight ZZ1234" in state_text
    questions = list(request["questions"].values())
    gates = [q for q in questions if q["type"] == "noul"]
    choices = [q for q in questions if q["type"] == "choice"]
    assert len(gates) == 1 and gates[0]["instructions"]
    assert len(choices) == 1
    assert list(choices[0]["criteria"]) == EMAIL_TYPES


def test_multipart_message_sends_the_plain_text_part(workspace):
    mbox = write_mbox(
        workspace.root / "mail.mbox",
        [
            email(
                "Itinerary attached",
                "Plain text says flight ZZ88.\n",
                html="<html><body><p>HTML says something else</p></body></html>",
                attachments=[("itinerary.pdf", b"%PDF-1.4 not really", "application/pdf")],
            )
        ],
    )

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    [request] = workspace.ollama.decision_requests()
    state_text = "\n".join(str(v) for v in request["state"].values())
    assert "Plain text says flight ZZ88." in state_text
    assert "<p>" not in state_text
    assert "itinerary.pdf" in state_text


def test_results_are_stored_and_a_rerun_sends_nothing(workspace):
    mbox = write_mbox(
        workspace.root / "mail.mbox",
        [
            email("Your flight to Oslo is confirmed", FLIGHT_BODY),
            email("Seat sale this weekend only"),
            email("Lunch on Friday?"),
        ],
    )
    workspace.ollama.decisions["Your flight to Oslo is confirmed"] = Decision(0.97, "booking confirmation")
    workspace.ollama.decisions["Seat sale this weekend only"] = Decision(0.30, "marketing")

    first = workspace.run("classify", "--mailbox", str(mbox))
    assert first.returncode == 0, first.stderr
    sent_first = len(workspace.ollama.decision_requests())

    second = workspace.run("classify", "--mailbox", str(mbox))

    assert second.returncode == 0, second.stderr
    assert len(workspace.ollama.decision_requests()) == sent_first == 3
    assert second.count("classified") == 0
    assert second.count("already classified") == 3
    # The stored results are summarised from the store, so a rerun that sends
    # nothing still reports what the model said on the first run.
    for result in (first, second):
        assert "booking confirmation: 1" in result.stdout
        assert "marketing: 1" in result.stdout
        assert "other: 1" in result.stdout
        assert "gate >= 0.5: 1" in result.stdout
        assert "gate >= 0.2: 2" in result.stdout


def test_changing_the_decision_model_reclassifies(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Anything at all")])
    assert workspace.run("classify", "--mailbox", str(mbox)).returncode == 0

    workspace.write_config(
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[models]\ndecision = "other-decider"\n'
    )
    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("classified") == 1
    assert [r["model"] for r in workspace.ollama.decision_requests()] == ["nimble", "other-decider"]


def test_message_without_message_id_gets_a_stable_identifier(workspace):
    mbox = write_mbox(
        workspace.root / "mail.mbox",
        [
            email("First message without an id", message_id=None),
            email("Second message without an id", message_id=None),
        ],
    )

    first = workspace.run("classify", "--mailbox", str(mbox))
    second = workspace.run("classify", "--mailbox", str(mbox))

    assert first.returncode == 0 and second.returncode == 0
    assert first.count("classified") == 2
    assert second.count("classified") == 0
    assert second.count("already classified") == 2
    assert len(workspace.ollama.decision_requests()) == 2


UNKNOWN_CHARSET = (
    b"From: Odd Sender <odd@sender.example>\r\n"
    b"Subject: Mystery charset\r\n"
    b"Message-ID: <odd-1@fixtures.example>\r\n"
    b"X-Gmail-Labels: Inbox\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: text/plain; charset="x-no-such-charset"\r\n'
    b"Content-Transfer-Encoding: 8bit\r\n"
    b"\r\n"
    b"Caf\xe9 au lait on flight ZZ55 \x80\x81\r\n"
)

# A message the email package cannot parse at all: an unterminated quoted
# string in From makes Python's header parser raise (CPython 3.12). If a
# future Python parses it, this fixture needs another unparseable message.
UNPARSEABLE = (
    b'From: "\r\n'
    b"Subject: Cannot be parsed\r\n"
    b"Message-ID: <broken-1@fixtures.example>\r\n"
    b"X-Gmail-Labels: Inbox\r\n"
    b"\r\n"
    b"Body.\r\n"
)


def test_unknown_charset_is_decoded_rather_than_unreadable(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Before it"), UNKNOWN_CHARSET])

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("classified") == 2
    assert result.count("failed") == 0
    [request] = [r for r in workspace.ollama.decision_requests() if r["state"]["subject"] == "Mystery charset"]
    # Not UTF-8, so read as windows-1252 / latin-1.
    assert "Café au lait on flight ZZ55" in request["state"]["body"]


def test_unparseable_message_is_recorded_and_skipped_without_stopping(workspace):
    mbox = write_mbox(
        workspace.root / "mail.mbox",
        [email("Before the broken one"), UNPARSEABLE, email("After the broken one")],
    )

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("classified") == 2
    assert result.count("failed") == 1
    assert "broken-1@fixtures.example" in result.stdout + result.stderr
    assert sorted(workspace.ollama.classified_subjects()) == ["After the broken one", "Before the broken one"]


def test_failed_request_is_recorded_and_retried_on_rerun(workspace):
    mbox = write_mbox(
        workspace.root / "mail.mbox",
        [
            email("Server chokes on this one", "word " * 20000),
            email("Malformed answer for this one"),
            email("This one is fine"),
        ],
    )
    workspace.ollama.decisions["Server chokes on this one"] = Decision(status=500)
    workspace.ollama.decisions["Malformed answer for this one"] = Decision(raw_body='{"answers": {}}')

    first = workspace.run("classify", "--mailbox", str(mbox))

    assert first.returncode == 0, first.stderr
    assert first.count("classified") == 1
    assert first.count("failed") == 2

    workspace.ollama.decisions.clear()
    second = workspace.run("classify", "--mailbox", str(mbox))

    assert second.returncode == 0, second.stderr
    assert second.count("classified") == 2
    assert second.count("already classified") == 1
    assert second.count("failed") == 0


def test_interrupted_run_resumes_without_resending(workspace):
    subjects = [f"Invented message number {i}" for i in range(8)]
    mbox = write_mbox(workspace.root / "mail.mbox", [email(s) for s in subjects])

    third_request = threading.Event()
    release = threading.Event()

    def block_on_third(request):
        if len(workspace.ollama.decision_requests()) == 3:
            third_request.set()
            release.wait(timeout=30)

    workspace.ollama.on_request = block_on_third
    proc = workspace.start("classify", "--mailbox", str(mbox))
    try:
        assert third_request.wait(timeout=30), "classify never sent a third request"
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        release.set()
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode != 0
    assert "interrupt" in (stdout + stderr).lower()
    first_run = workspace.ollama.classified_subjects()
    assert len(first_run) == 3

    workspace.ollama.on_request = None
    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    second_run = workspace.ollama.classified_subjects()[len(first_run):]
    # The two finished messages are not sent again; the one in flight is.
    assert len(second_run) == 6
    assert sorted(set(first_run[:2]) | set(second_run)) == sorted(subjects)
    assert not set(first_run[:2]) & set(second_run)
    assert result.count("already classified") == 2


def test_progress_and_estimated_time_remaining_are_shown(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email(f"Message {i}") for i in range(5)])

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert "5/5" in result.stderr
    assert "ETA" in result.stderr


def test_defaults_and_config_settings(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Defaults please")])

    # Only the address is configured: model and threshold take their defaults.
    result = workspace.run("classify", "--mailbox", str(mbox))
    assert result.returncode == 0, result.stderr
    assert workspace.ollama.decision_requests()[-1]["model"] == "nimble"
    assert "threshold 0.05" in result.stdout

    workspace.write_config(
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n'
        '[models]\ndecision = "tiny-decider"\n\n[classify]\nthreshold = 0.3\n'
    )
    result = workspace.run("classify", "--mailbox", str(mbox))
    assert result.returncode == 0, result.stderr
    assert workspace.ollama.decision_requests()[-1]["model"] == "tiny-decider"
    assert "threshold 0.3" in result.stdout


def test_unknown_config_setting_is_rejected(workspace):
    workspace.write_config('[models]\ndecison = "typo"\n')
    result = workspace.run("classify", "--mailbox", "whatever.mbox")
    assert result.returncode != 0
    assert "decison" in result.stderr


def test_unreachable_ollama_stops_the_run_with_a_clear_message(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("One"), email("Two")])
    workspace.write_config('[ollama]\naddress = "http://127.0.0.1:9"\n')

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode != 0
    assert "ollama" in result.stderr.lower()
    assert "127.0.0.1:9" in result.stderr
