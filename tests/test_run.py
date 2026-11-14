"""The run command: classify, extract and review in one go, driven through
the command line against a fake Ollama. Every email, person and flight here
is invented."""

from __future__ import annotations

import signal
import sqlite3
import threading

from fake_ollama import Chat, Decision
from mailbox_fixtures import FLIGHT_BODY, email, image_only_pdf, write_mbox
from test_consolidation import ROBIN, SAM, record, seg
from test_decisions import DISCARDED, GOOD, OTHER, edit_review, rows_by_id
from test_decisions import export as export_from_table
from test_export import data_rows, the_import_file
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


# -- the end-of-run report ------------------------------------------------------------


def report_mailbox(workspace):
    """Bookings that exercise every line of the report. Robin is the user."""
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[passenger]\nnames = ["{ROBIN}"]\n')
    mails = [
        # Two legs, the second replaced by a later schedule change: 2 flights, 1 booking replaced.
        ("Your CHG555 itinerary", "01", "booking confirmation",
         record("booking", "CHG555", seg("2025-06-01", "ZZ100", "LIS", "FRA"), seg("2025-06-01", "ZZ200", "FRA", "VIE"))),
        ("Schedule change for CHG555", "10", "change",
         record("change", "CHG555", seg("2025-06-01", "ZZ100", "LIS", "FRA"), seg("2025-06-01", "ZZ208", "FRA", "VIE"))),
        # Booked, then cancelled: no flight, 1 booking cancelled.
        ("Your CXL111 booking", "02", "booking confirmation",
         record("booking", "CXL111", seg("2025-07-01", "ZZ500", "LIS", "OSL"))),
        ("Booking CXL111 cancelled", "05", "cancellation", record("cancellation", "CXL111")),
        # Sam's trip: dropped as another passenger's.
        ("Sam's OTH777 itinerary", "03", "booking confirmation",
         record("booking", "OTH777", seg("2025-08-01", "ZZ600", "OSL", "CPH"), passengers=(SAM,))),
        # No flight number: needs attention, still kept.
        ("Your NON123 booking", "04", "booking confirmation",
         record("booking", "NON123", seg("2025-09-01", "", "OSL", "LIS"))),
        # An unknown airport: needs attention (and its confidence is halved), pre-set to not keep.
        ("Your UNK456 booking", "06", "booking confirmation",
         record("booking", "UNK456", seg("2025-10-01", "ZZ300", "LIS", "QQX"))),
    ]
    messages = []
    for subject, day, email_type, rec in mails:
        workspace.ollama.decisions[subject] = Decision(0.9, email_type)
        workspace.ollama.chats[subject] = Chat(rec)
        messages.append(email(subject, "Invented email.\n", date=f"{day} Mar 2025 10:00:00 +0000"))
    # A scanned ticket whose PDF cannot be read, an extraction that fails, and a classification that fails.
    workspace.ollama.decisions["Your scanned ticket"] = Decision(0.9, "booking confirmation")
    messages.append(email("Your scanned ticket", "Attached.\n",
                          attachments=[("scan.pdf", image_only_pdf(), "application/pdf")]))
    workspace.ollama.decisions[BROKEN_SUBJECT] = Decision(0.9, "booking confirmation")
    workspace.ollama.chats[BROKEN_SUBJECT] = Chat(content="not json at all")
    messages.append(email(BROKEN_SUBJECT, "Attached.\n"))
    workspace.ollama.decisions["Unclassifiable note"] = Decision(status=500)
    messages.append(email("Unclassifiable note"))
    return write_mbox(workspace.root / "mail.mbox", messages)


def test_report_states_what_was_found_what_failed_where_the_files_are_and_what_to_do_next(workspace):
    mbox = report_mailbox(workspace)

    result = run(workspace, "--mailbox", str(mbox), "--output", "out/review.csv", "--failures", "out/failures.csv")

    report = result.stdout[result.stdout.index("Run finished."):]
    assert report.splitlines() == [
        "Run finished.",
        "  flights found:              4",
        "    confirmed:                0",
        "    booked only:              4",
        "  flights needing attention:  2",
        "    unknown airport (QQX): 1",
        "    low confidence: 1",
        "    no flight number: 1",
        "  flights pre-set to not keep: 1",
        "  bookings cancelled:         1",
        "  bookings with replaced segments: 1",
        "  bookings for other passengers (dropped): 1",
        "  messages that failed:",
        "    classify:                 1",
        "    extract:                  1",
        "  PDF attachments not read:   1",
        "Files written:",
        "  review table: out/review.csv",
        "  failures file: out/failures.csv",
        "Next step: check out/review.csv, then run export.",
    ]
    assert (workspace.root / "out" / "review.csv").exists()
    assert (workspace.root / "out" / "failures.csv").exists()
    # Each stage's start line comes before its own summary.
    out = result.stdout
    assert out.index("Starting classify.") < out.index("Classify pass finished.") < out.index("Starting extract.")
    assert out.index("Extract pass finished.") < out.index("Starting review.") < out.index("Review table written to")
    assert "classify: 10/10" in result.stderr and "extract: 9/9" in result.stderr


def test_an_edited_review_table_is_left_as_it_is_and_the_report_still_counts_the_flights(workspace):
    mbox = report_mailbox(workspace)
    run(workspace, "--mailbox", str(mbox))
    review_csv = workspace.root / "review.csv"
    edited = review_csv.read_bytes().replace(b"yes,", b"no,", 1)
    assert edited != review_csv.read_bytes()
    review_csv.write_bytes(edited)
    (workspace.root / "failures.csv").unlink()

    result = run(workspace, "--mailbox", str(mbox))

    assert review_csv.read_bytes() == edited
    assert (
        f"Review table {review_csv}: review table was edited and has been left as it is; "
        "pass --force to regenerate it." in result.stdout
    )
    report = result.stdout[result.stdout.index("Run finished."):]
    assert "  flights found:              4" in report
    assert "  review table:" not in report
    assert f"  failures file: {workspace.root / 'failures.csv'}" in report
    assert (workspace.root / "failures.csv").exists()
    assert "Note: review table was edited and has been left as it is; pass --force to regenerate it" in report

    forced = run(workspace, "--mailbox", str(mbox), "--force")

    assert review_csv.read_bytes() != edited
    assert f"  review table: {review_csv}" in forced.stdout


def test_an_edited_review_table_left_as_it_is_records_no_other_table(workspace):
    mbox = report_mailbox(workspace)
    run(workspace, "--mailbox", str(mbox))
    review_csv = workspace.root / "review.csv"
    review_csv.write_bytes(review_csv.read_bytes().replace(b"yes,", b"no,", 1))

    run(workspace, "--mailbox", str(mbox))

    with sqlite3.connect(workspace.root / "flighty_exporter.db") as db:
        recorded = [path for (path,) in db.execute("SELECT path FROM review_tables")]
    assert recorded == [str(review_csv.resolve())]


# -- review decisions, sources and export after run ------------------------------------------


def decisions_mailbox(workspace):
    """Two invented bookings, as the review decisions tests use them."""
    mails = [GOOD, OTHER]
    for m in mails:
        workspace.ollama.decisions[m.subject] = Decision(0.9, m.email_type)
        workspace.ollama.chats[m.subject] = Chat(m.record)
    return write_mbox(workspace.root / "mail.mbox",
                      [email(m.subject, "Invented email.\n", date=m.header_date) for m in mails])


def test_run_writes_the_table_with_saved_decisions_and_reports_the_ones_discarded(workspace):
    mbox = decisions_mailbox(workspace)
    first = run(workspace, "--mailbox", str(mbox))
    assert DISCARDED not in first.stdout[first.stdout.index("Run finished."):]
    edit_review(workspace, lambda rows: rows["2025-06-01 QQ410"].update(keep="no"))
    export_from_table(workspace)
    # A decision for a flight no run produces.
    assert workspace.run("mark-added", "2001-01-01", "ZZ1").returncode == 0

    result = run(workspace, "--mailbox", str(mbox), "--force")

    rows = rows_by_id(workspace.root / "review.csv")
    assert [rows[k]["keep"] for k in ("2025-05-12 ZZ1234", "2025-06-01 QQ410")] == ["yes", "no"]
    report = result.stdout[result.stdout.index("Run finished."):]
    assert f"  {DISCARDED}: 1" in report.splitlines()
    again = run(workspace, "--mailbox", str(mbox))
    assert DISCARDED not in again.stdout[again.stdout.index("Run finished."):]
    assert rows_by_id(workspace.root / "review.csv")["2025-06-01 QQ410"]["keep"] == "no"


def test_run_then_sources_and_export_from_the_store(workspace):
    mbox = decisions_mailbox(workspace)
    run(workspace, "--mailbox", str(mbox))
    mbox.unlink()

    listing = workspace.run("sources")
    assert listing.returncode == 0, listing.stderr
    assert [line for line in listing.stdout.splitlines() if line and not line.startswith(" ")] == [
        "2025-05-12  ZZ1234  LIS-OSL", "2025-06-01  QQ410  AMS-KEF",
    ]
    assert listing.stdout.count("    | Invented email.") == 2

    from_csv = export_from_table(workspace, "--output-dir", "from-csv")
    (workspace.root / "review.csv").unlink()
    from_store = export_from_table(workspace, "--from-store", "--output-dir", "from-store")

    assert "flights exported:           2" in from_csv.stdout and "flights exported:           2" in from_store.stdout
    assert data_rows(the_import_file(workspace.root / "from-store")) == data_rows(
        the_import_file(workspace.root / "from-csv")
    )


def test_run_locates_messages_when_positions_are_missing_and_reports_progress(workspace):
    subjects, mbox = itineraries(workspace, count=3)
    assert workspace.run("classify", "--mailbox", str(mbox)).returncode == 0
    with sqlite3.connect(workspace.root / "flighty_exporter.db") as db:
        db.execute("UPDATE messages SET mbox_offset = NULL")

    result = run(workspace, "--mailbox", str(mbox))

    assert "extract: locating messages: 3/3 (100%)" in result.stderr
    assert "extract: 3/3" in result.stderr
    assert sorted(workspace.ollama.extracted_subjects()) == sorted(subjects)
    assert "Run finished." in result.stdout
