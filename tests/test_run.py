"""The run command: classify, extract and review in one go, driven through
the command line against a fake Ollama. Every email, person and flight here
is invented."""

from __future__ import annotations

import signal
import threading

from fake_ollama import Chat, Decision
from mailbox_fixtures import FLIGHT_BODY, email, write_mbox
from test_extract import FLIGHT_RECORD, FLIGHT_SUBJECT
from test_markup import flight_reservation, json_ld_html, markup_email
from test_preflight import dead_address

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


# -- thoroughness -------------------------------------------------------------------


def test_thorough_stops_before_any_work_with_a_not_available_message(workspace):
    mbox = mailbox(workspace)

    for config, args in (
        ('\n[run]\nthoroughness = "thorough"\n', ()),
        ("", ("--thoroughness", "thorough")),
    ):
        workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n{config}')
        result = workspace.run("run", "--mailbox", str(mbox), *args)

        assert result.returncode == 1
        assert result.stderr.strip() == "error: thorough mode is not available yet"
        assert result.stdout == ""
        assert workspace.ollama.requests == []
        assert not (workspace.root / "flighty_exporter.db").exists()


def test_quick_on_the_command_line_overrides_thorough_in_the_config(workspace):
    mbox = mailbox(workspace)
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[run]\nthoroughness = "thorough"\n')

    result = run(workspace, "--mailbox", str(mbox), "--thoroughness", "quick")

    assert "Run finished." in result.stdout
    assert LOW_FLIGHT not in workspace.ollama.extracted_subjects()


def test_an_unknown_thoroughness_is_rejected(workspace):
    mbox = mailbox(workspace)
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[run]\nthoroughness = "fast"\n')

    result = workspace.run("run", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert '[run] thoroughness must be "quick" or "thorough"' in result.stderr
    bad_option = workspace.run("run", "--mailbox", str(mbox), "--thoroughness", "fast")
    assert bad_option.returncode == 2


# -- interrupting, stopping and resuming --------------------------------------------


def itineraries(workspace, count: int = 6):
    subjects = [f"Invented itinerary number {i}" for i in range(count)]
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")
    workspace.ollama.default_chat = Chat(FLIGHT_RECORD)  # the same flight in every message
    return subjects, write_mbox(workspace.root / "mail.mbox", [email(s, FLIGHT_BODY) for s in subjects])


def start_blocked_on_third_chat(workspace, mbox, sig):
    """Start run, wait until it sends its third extraction request, send
    ``sig``, then let that request be answered. Returns (process, stdout, stderr)."""
    third_request = threading.Event()
    release = threading.Event()

    def block_on_third(request):
        if request.path == "/api/chat" and len(workspace.ollama.chat_requests()) == 3:
            third_request.set()
            release.wait(timeout=30)

    workspace.ollama.on_request = block_on_third
    proc = workspace.start("run", "--mailbox", str(mbox))
    try:
        assert third_request.wait(timeout=30), "run never sent a third extraction request"
        proc.send_signal(sig)
        if sig == signal.SIGTERM:
            release.set()  # the message in progress is finished before the run stops
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        release.set()
        if proc.poll() is None:
            proc.kill()
    workspace.ollama.on_request = None
    return proc, stdout, stderr


def test_interrupted_run_resumes_without_repeating_model_requests(workspace):
    subjects, mbox = itineraries(workspace)

    proc, stdout, stderr = start_blocked_on_third_chat(workspace, mbox, signal.SIGINT)

    assert proc.returncode == 130
    assert "Classify pass finished." in stdout
    assert "Extract pass interrupted. Rerun to resume." in stdout
    assert "Run finished." not in stdout
    assert "Interrupted. Progress so far is saved; rerun run to resume." in stderr
    classified = workspace.ollama.classified_subjects()
    first_run = workspace.ollama.extracted_subjects()
    assert sorted(classified) == sorted(subjects)
    assert len(first_run) == 3

    result = run(workspace, "--mailbox", str(mbox))

    assert workspace.ollama.classified_subjects() == classified  # nothing classified again
    second_run = workspace.ollama.extracted_subjects()[3:]
    assert len(second_run) == 4  # the two finished messages are not sent again; the one in flight is
    assert sorted(set(first_run[:2]) | set(second_run)) == sorted(subjects)
    assert result.count("already classified") == 6
    assert result.count("already extracted") == 2
    assert "Run finished." in result.stdout

    # A finished run has nothing left to send.
    workspace.ollama.requests.clear()
    again = run(workspace, "--mailbox", str(mbox))
    assert workspace.ollama.pass_requests() == []
    assert workspace.ollama.preflight_requests() == []
    assert again.count("flights found") == 1


def test_run_asked_to_stop_finishes_the_message_in_progress_and_resumes_after_it(workspace):
    subjects, mbox = itineraries(workspace)

    proc, stdout, stderr = start_blocked_on_third_chat(workspace, mbox, signal.SIGTERM)

    assert proc.returncode == 143
    assert "Extract pass stopped. Rerun to resume." in stdout
    assert "Run finished." not in stdout
    assert "Stopped. Progress so far is saved; rerun run to resume." in stderr
    first_run = workspace.ollama.extracted_subjects()
    assert len(first_run) == 3

    result = run(workspace, "--mailbox", str(mbox))

    # The message in progress when the stop came was finished and kept.
    second_run = workspace.ollama.extracted_subjects()[3:]
    assert len(second_run) == 3
    assert not set(first_run) & set(second_run)
    assert result.count("already extracted") == 3


# -- model-server problems: the existing message, a non-zero exit, no prompt ----------


def test_unreachable_server_stops_run_before_anything_is_stored(workspace):
    mbox = mailbox(workspace)
    address = dead_address()
    workspace.write_config(f'[ollama]\naddress = "{address}"\n')

    result = workspace.run("run", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert f"the Ollama server at {address} is not reachable" in result.stderr
    assert "rerun run" in result.stderr
    assert "Classify pass" not in result.stdout and "Run finished." not in result.stdout
    assert not (workspace.root / "flighty_exporter.db").exists()
    assert not (workspace.root / "review.csv").exists()


def test_non_local_address_is_refused_before_anything_is_sent(workspace):
    mbox = mailbox(workspace)
    workspace.write_config('[ollama]\naddress = "http://192.0.2.10:11434"\n')

    result = workspace.run("run", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert "refusing to use the Ollama server at http://192.0.2.10:11434" in result.stderr
    assert "Starting" not in result.stdout
    assert not (workspace.root / "flighty_exporter.db").exists()


def test_missing_extraction_model_stops_run_after_classify_and_keeps_its_results(workspace):
    mbox = mailbox(workspace)
    workspace.ollama.models = ["nimble:latest"]

    result = workspace.run("run", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert "Classify pass finished." in result.stdout
    assert "Extract pass" not in result.stdout and "Run finished." not in result.stdout
    assert "the model gemma4:12b-it-q4_K_M ([models] extraction) is not pulled" in result.stderr
    assert "`ollama pull gemma4:12b-it-q4_K_M`, then rerun run." in result.stderr
    assert workspace.ollama.chat_requests() == []

    workspace.ollama.models.append("gemma4:12b-it-q4_K_M")
    classified = len(workspace.ollama.decision_requests())
    resumed = run(workspace, "--mailbox", str(mbox))

    assert len(workspace.ollama.decision_requests()) == classified
    assert "Run finished." in resumed.stdout


def test_server_going_away_mid_run_says_the_stage_stopped_and_how_to_resume(workspace):
    subjects, mbox = itineraries(workspace)

    def vanish_on_third(request):
        if len(workspace.ollama.decision_requests()) == 3:
            workspace.ollama.unreachable = True

    workspace.ollama.on_request = vanish_on_third
    result = workspace.run("run", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert "Classify pass stopped. Rerun to resume." in result.stdout
    assert result.count("classified") == 2
    assert "Starting extract." not in result.stdout and "Run finished." not in result.stdout
    assert f"error: cannot reach Ollama at {workspace.ollama.url}" in result.stderr
    assert "Progress so far is saved; rerun run to resume." in result.stderr
    assert workspace.ollama.chat_requests() == []
