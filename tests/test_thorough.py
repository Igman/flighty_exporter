"""Thorough runs: after extraction, `run` audits the bands below the threshold
and, within a budget, lowers the session's effective threshold and extracts
the newly included messages. Driven through the command line against the fake
Ollama. Every email, person, booking and flight here is invented."""

from __future__ import annotations

import re
import signal
import threading

from fake_ollama import Chat, Decision
from mailbox_fixtures import FLIGHT_BODY, email, write_mbox
from test_audit import AUDIT_HEADER, NO_FLIGHT, flight_record
from test_extract import FLIGHT_RECORD, FLIGHT_SUBJECT, header, read_csv

NEWSLETTER = "Garden club newsletter"
RECEIPT = "Your bakery receipt"
CHECK = "  check for missed flight emails: "


def configure(workspace, extra: str = "") -> bytes:
    """Write the config and return its bytes."""
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n{extra}')
    return workspace.config_path.read_bytes()


def scripted(workspace, messages: dict[str, tuple[float, dict]]):
    """An mbox of ``messages`` (subject -> gate, extraction record)."""
    for subject, (gate, record) in messages.items():
        workspace.ollama.decisions[subject] = Decision(gate, "other")
        workspace.ollama.chats[subject] = Chat(record)
    return write_mbox(workspace.root / "mail.mbox", [email(s, FLIGHT_BODY) for s in messages])


def run(workspace, mbox, *args: str):
    result = workspace.run("run", "--mailbox", str(mbox), *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def report(result) -> list[str]:
    return result.stdout[result.stdout.index("Run finished."):].splitlines()


def check_lines(result) -> list[str]:
    """The report's lines about the check for missed flight emails."""
    lines = report(result)
    start = next(i for i, line in enumerate(lines) if line.startswith(CHECK))
    end = next(i for i in range(start + 1, len(lines)) if not lines[i].startswith("    "))
    return lines[start:end]


def chat_counts(workspace) -> dict[str, int]:
    counts: dict[str, int] = {}
    for subject in workspace.ollama.extracted_subjects():
        counts[subject] = counts.get(subject, 0) + 1
    return counts


# -- quick and thorough ------------------------------------------------------------------


def test_quick_makes_no_audit_request_and_says_the_check_was_skipped(workspace):
    configure(workspace)
    mbox = scripted(workspace, {FLIGHT_SUBJECT: (0.97, FLIGHT_RECORD), NEWSLETTER: (0.01, NO_FLIGHT)})

    result = run(workspace, mbox)

    assert workspace.ollama.extracted_subjects() == [FLIGHT_SUBJECT]
    assert f"{CHECK}skipped (quick)" in report(result)
    assert "Starting audit." not in result.stdout
    assert not (workspace.root / "audit.csv").exists()


def test_thorough_with_nothing_below_the_threshold_audits_and_changes_nothing(workspace):
    configure(workspace)
    mbox = scripted(workspace, {
        FLIGHT_SUBJECT: (0.97, FLIGHT_RECORD), NEWSLETTER: (0.01, NO_FLIGHT), RECEIPT: (0.002, NO_FLIGHT),
    })

    result = run(workspace, mbox, "--thoroughness", "thorough")

    # Extract sends the flight email; the audit sends the two below the threshold, once each.
    assert workspace.ollama.extracted_subjects()[0] == FLIGHT_SUBJECT
    assert chat_counts(workspace) == {FLIGHT_SUBJECT: 1, NEWSLETTER: 1, RECEIPT: 1}
    out = result.stdout
    assert out.index("Extract pass finished.") < out.index("Starting audit.") < out.index("Audit finished.")
    assert out.index("Audit finished.") < out.index("Starting review.")
    assert "Include skipped: no flight emails found below the threshold." in out
    assert check_lines(result) == [
        f"{CHECK}ran (sample of up to 25 per band, seed 1)",
        "    sampled below the threshold 0.05, with flights: [0.01, 0.02) 0 of 1, [0.001, 0.005) 0 of 1",
        "    no flight emails found below the threshold",
    ]
    audit_csv = workspace.root / "audit.csv"
    assert header(audit_csv) == AUDIT_HEADER
    assert f"  audit report: {audit_csv}" in report(result)
    assert [r["flight_number"] for r in read_csv(workspace.root / "review.csv")] == ["ZZ1234"]


# -- lowering the threshold within the budget ----------------------------------------------


LOW = {
    f"Your documents {n}": (0.03, flight_record(n, "OSL", "KEF", "2025-06-02")) for n in ("ZZ5678", "ZZ6001", "ZZ6002")
}
ALL_FLIGHTS = ["ZZ1234", "ZZ5678", "ZZ6001", "ZZ6002"]


def low_band_mailbox(workspace):
    """A flight email above the threshold, three below it in band [0.02, 0.05)
    (one flight each) and a newsletter far below. With a sample of one per
    band, the audit extracts one of the three; the include extracts the rest."""
    return scripted(workspace, {FLIGHT_SUBJECT: (0.97, FLIGHT_RECORD), **LOW, NEWSLETTER: (0.002, NO_FLIGHT)})


def flight_numbers(path) -> list[str]:
    return sorted(r["flight_number"] for r in read_csv(path))


def test_below_threshold_flight_emails_within_the_budget_lower_the_threshold_for_the_session(workspace):
    config = configure(workspace, '\n[audit]\nsample_size = 1\n\n[run]\nthoroughness = "thorough"\n')
    mbox = low_band_mailbox(workspace)

    result = run(workspace, mbox)

    # Every message is sent at most once: the one the audit sampled is not sent again.
    assert chat_counts(workspace) == {FLIGHT_SUBJECT: 1, **{s: 1 for s in LOW}, NEWSLETTER: 1}
    assert flight_numbers(workspace.root / "review.csv") == ALL_FLIGHTS
    assert check_lines(result) == [
        f"{CHECK}ran (sample of up to 1 per band, seed 1)",
        "    sampled below the threshold 0.05, with flights: [0.02, 0.05) 1 of 1, [0.001, 0.005) 0 of 1",
        "    threshold lowered from 0.05 to 0.02 for this session; 3 more emails extracted, 3 flight emails among "
        "them (1 of them already extracted by the audit)",
    ]
    assert "  flights found:              4" in report(result)
    out = result.stdout
    assert out.index("Audit finished.") < out.index("Starting include.") < out.index("Include finished:")
    assert out.index("Include finished:") < out.index("Starting review.")
    assert "include: 2/2" in result.stderr
    assert workspace.config_path.read_bytes() == config

    # A second Thorough run audits against the lowered threshold and sends nothing new.
    before = len(workspace.ollama.chat_requests())
    again = run(workspace, mbox)
    assert len(workspace.ollama.chat_requests()) == before
    assert len(workspace.ollama.decision_requests()) == 5  # nothing classified again
    assert check_lines(again)[1:] == [
        "    sampled below the threshold 0.02, with flights: [0.001, 0.005) 0 of 1",
        "    no flight emails found below the threshold",
    ]
    assert workspace.config_path.read_bytes() == config


def test_later_commands_on_the_session_use_the_lowered_threshold(workspace):
    configure(workspace, "\n[audit]\nsample_size = 1\n")
    mbox = low_band_mailbox(workspace)
    run(workspace, mbox, "--thoroughness", "thorough")
    before = len(workspace.ollama.chat_requests())

    extract = workspace.run("extract", "--mailbox", str(mbox))
    assert extract.returncode == 0, extract.stderr
    assert extract.count("selected (gate >= 0.02)") == 4
    assert extract.count("already extracted") == 4
    assert len(workspace.ollama.chat_requests()) == before

    (workspace.root / "review.csv").unlink()
    review = workspace.run("review")
    assert review.returncode == 0, review.stderr
    assert flight_numbers(workspace.root / "review.csv") == ALL_FLIGHTS

    (workspace.root / "review.csv").unlink()
    export = workspace.run("export", "--from-store", "--output-dir", "out")
    assert export.returncode == 0, export.stderr
    assert "flights exported:           4" in export.stdout

    sources = workspace.run("sources", "--no-text")
    assert sources.returncode == 0, sources.stderr
    assert sum(1 for line in sources.stdout.splitlines() if line and not line.startswith(" ")) == 4

    audit = workspace.run("audit", "--mailbox", str(mbox), "--sample-size", "1")
    assert audit.returncode == 0, audit.stderr
    assert "(threshold 0.02 for this session; configured 0.05)" in audit.stdout
    assert "Reading: no sampled message below the threshold 0.02 contained flight segments" in audit.stdout
    assert len(workspace.ollama.chat_requests()) == before

    classify = workspace.run("classify", "--mailbox", str(mbox))
    assert classify.returncode == 0, classify.stderr
    assert classify.count("at or above threshold 0.02") == 4


def test_a_configured_threshold_below_the_sessions_wins_and_one_above_it_does_not_raise_it(workspace):
    configure(workspace, "\n[audit]\nsample_size = 1\n")
    mbox = low_band_mailbox(workspace)
    run(workspace, mbox, "--thoroughness", "thorough")

    configure(workspace, "\n[classify]\nthreshold = 0.5\n")
    higher = workspace.run("extract", "--mailbox", str(mbox))
    assert higher.count("selected (gate >= 0.02)") == 4

    configure(workspace, "\n[classify]\nthreshold = 0.001\n")
    lower = workspace.run("extract", "--mailbox", str(mbox))
    assert lower.returncode == 0, lower.stderr
    assert lower.count("selected (gate >= 0.001)") == 5
    assert lower.count("already extracted") == 5  # the newsletter was extracted by the audit


# -- over the budget ------------------------------------------------------------------------


def over_budget_lines(would_add: int, budget: int) -> list[str]:
    return [
        f"{CHECK}ran (sample of up to 1 per band, seed 1)",
        "    sampled below the threshold 0.05, with flights: [0.02, 0.05) 1 of 1, [0.001, 0.005) 0 of 1",
        f"    found flight emails below the threshold in band [0.02, 0.05), but including them would add {would_add} "
        f"emails, over the budget of {budget}; to include them, set [classify] threshold = 0.02 or raise "
        f"[audit] auto_include_budget to at least {would_add}, then run again",
    ]


def test_over_the_budget_nothing_changes_and_the_report_says_how_to_include_them(workspace):
    config = configure(workspace, "\n[audit]\nsample_size = 1\nauto_include_budget = 1\n")
    mbox = low_band_mailbox(workspace)

    result = run(workspace, mbox, "--thoroughness", "thorough")

    assert check_lines(result) == over_budget_lines(2, 1)
    assert "Include skipped: found flight emails below the threshold in band [0.02, 0.05)" in result.stdout
    assert "Starting include." in result.stdout and "include: " not in result.stderr
    # Only the audit's one sampled message from the band was sent; the review table is unchanged.
    assert sum(chat_counts(workspace).get(s, 0) for s in LOW) == 1
    assert flight_numbers(workspace.root / "review.csv") == ["ZZ1234"]
    assert workspace.config_path.read_bytes() == config
    extract = workspace.run("extract", "--mailbox", str(mbox))
    assert extract.count("selected (gate >= 0.05)") == 1

    # Following the report's advice includes them.
    configure(workspace, "\n[audit]\nsample_size = 1\nauto_include_budget = 2\n")
    included = run(workspace, mbox, "--thoroughness", "thorough")
    assert check_lines(included)[-1].startswith("    threshold lowered from 0.05 to 0.02 for this session")
    assert chat_counts(workspace) == {FLIGHT_SUBJECT: 1, **{s: 1 for s in LOW}, NEWSLETTER: 1}
    assert flight_numbers(workspace.root / "review.csv") == ALL_FLIGHTS


def test_a_budget_of_zero_lowers_the_threshold_only_when_nothing_new_must_be_extracted(workspace):
    configure(workspace, "\n[audit]\nsample_size = 1\nauto_include_budget = 0\n")
    mbox = low_band_mailbox(workspace)

    over = run(workspace, mbox, "--thoroughness", "thorough")

    assert check_lines(over) == over_budget_lines(2, 0)

    # With a sample that covers the whole band, the audit extracted every message in it: nothing to add.
    configure(workspace, "\n[audit]\nsample_size = 25\nauto_include_budget = 0\n")
    free = run(workspace, mbox, "--thoroughness", "thorough")
    assert check_lines(free)[-1] == (
        "    threshold lowered from 0.05 to 0.02 for this session; 3 more emails extracted, 3 flight emails among "
        "them (3 of them already extracted by the audit)"
    )
    assert "include: " not in free.stderr
    assert chat_counts(workspace) == {FLIGHT_SUBJECT: 1, **{s: 1 for s in LOW}, NEWSLETTER: 1}
    assert flight_numbers(workspace.root / "review.csv") == ALL_FLIGHTS


def test_a_negative_budget_is_rejected(workspace):
    configure(workspace, "\n[audit]\nauto_include_budget = -1\n")
    mbox = low_band_mailbox(workspace)

    result = workspace.run("run", "--mailbox", str(mbox), "--thoroughness", "thorough")

    assert result.returncode == 1
    assert "[audit] auto_include_budget must be at least 0" in result.stderr
    assert workspace.ollama.requests == []


# -- stopping, model-server problems and resuming -------------------------------------------


SPREAD = {f"Invented notice {n}": (gate, NO_FLIGHT) for n, gate in enumerate((0.03, 0.015, 0.007, 0.002))}


def spread_mailbox(workspace):
    """A flight email above the threshold and four messages without flights in
    four bands below it: extract sends one request, the audit four."""
    return scripted(workspace, {FLIGHT_SUBJECT: (0.97, FLIGHT_RECORD), **SPREAD})


def stop_on_chat(workspace, mbox, number: int, *args: str):
    """Start a Thorough run, send SIGTERM while its ``number``th extraction
    request is in progress, then let that request be answered."""
    reached = threading.Event()
    release = threading.Event()

    def block(request):
        if request.path == "/api/chat" and len(workspace.ollama.chat_requests()) == number:
            reached.set()
            release.wait(timeout=30)

    workspace.ollama.on_request = block
    proc = workspace.start("run", "--mailbox", str(mbox), "--thoroughness", "thorough", *args)
    try:
        assert reached.wait(timeout=30), f"run never sent extraction request {number}"
        proc.send_signal(signal.SIGTERM)
        release.set()
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        release.set()
        if proc.poll() is None:
            proc.kill()
    workspace.ollama.on_request = None
    return proc.returncode, stdout, stderr


def test_a_run_stopped_during_the_audit_resumes_it_without_repeating_requests(workspace):
    configure(workspace)
    mbox = spread_mailbox(workspace)

    code, stdout, stderr = stop_on_chat(workspace, mbox, 3)

    assert code == 143
    assert "Audit stopped. Rerun with the same seed to resume." in stdout
    assert "Run finished." not in stdout and "Starting include." not in stdout
    assert "Stopped. Progress so far is saved; rerun run to resume." in stderr
    assert len(workspace.ollama.chat_requests()) == 3
    assert not (workspace.root / "audit.csv").exists()

    result = run(workspace, mbox, "--thoroughness", "thorough")

    assert chat_counts(workspace) == {FLIGHT_SUBJECT: 1, **{s: 1 for s in SPREAD}}
    assert check_lines(result)[-1] == "    no flight emails found below the threshold"
    assert (workspace.root / "audit.csv").exists()


def test_a_run_stopped_during_the_include_keeps_the_lowered_threshold_and_extract_finishes_it(workspace):
    config = configure(workspace, "\n[audit]\nsample_size = 1\n")
    mbox = low_band_mailbox(workspace)

    # Extract sends one request, the audit two (one per band), the include two.
    code, stdout, stderr = stop_on_chat(workspace, mbox, 4)

    assert code == 143
    assert "Include stopped. The threshold stays lowered from 0.05 to 0.02 for this session" in stdout
    assert "Run finished." not in stdout and "Starting review." not in stdout
    assert "Stopped. Progress so far is saved; rerun run to resume." in stderr
    assert len(workspace.ollama.chat_requests()) == 4

    extract = workspace.run("extract", "--mailbox", str(mbox))
    assert extract.returncode == 0, extract.stderr
    assert extract.count("selected (gate >= 0.02)") == 4
    assert extract.count("extracted") == 1
    assert chat_counts(workspace) == {FLIGHT_SUBJECT: 1, **{s: 1 for s in LOW}, NEWSLETTER: 1}

    result = run(workspace, mbox, "--thoroughness", "thorough")
    assert len(workspace.ollama.chat_requests()) == 5
    assert check_lines(result)[-1] == "    no flight emails found below the threshold"
    assert flight_numbers(workspace.root / "review.csv") == ALL_FLIGHTS
    assert workspace.config_path.read_bytes() == config


def test_a_run_stopped_during_the_include_resumes_with_run(workspace):
    configure(workspace, "\n[audit]\nsample_size = 1\n")
    mbox = low_band_mailbox(workspace)
    stop_on_chat(workspace, mbox, 4)

    result = run(workspace, mbox, "--thoroughness", "thorough")

    assert chat_counts(workspace) == {FLIGHT_SUBJECT: 1, **{s: 1 for s in LOW}, NEWSLETTER: 1}
    assert flight_numbers(workspace.root / "review.csv") == ALL_FLIGHTS


def test_the_server_going_away_during_the_audit_stops_the_run_with_the_existing_message(workspace):
    configure(workspace)
    mbox = spread_mailbox(workspace)

    def vanish_on_third(request):
        if request.path == "/api/chat" and len(workspace.ollama.chat_requests()) == 3:
            workspace.ollama.unreachable = True

    workspace.ollama.on_request = vanish_on_third
    result = workspace.run("run", "--mailbox", str(mbox), "--thoroughness", "thorough")

    assert result.returncode == 1
    assert "Audit stopped. Rerun with the same seed to resume." in result.stdout
    assert "Run finished." not in result.stdout and "Starting include." not in result.stdout
    assert f"error: cannot reach Ollama at {workspace.ollama.url}" in result.stderr
    assert "Progress so far is saved; rerun run to resume." in result.stderr

    workspace.ollama.on_request = None
    workspace.ollama.unreachable = False
    answered = workspace.ollama.extracted_subjects()[:2]
    resumed = run(workspace, mbox, "--thoroughness", "thorough")
    assert not set(answered) & set(workspace.ollama.extracted_subjects()[3:])
    assert sorted(set(workspace.ollama.extracted_subjects())) == sorted([FLIGHT_SUBJECT, *SPREAD])
    assert "Run finished." in resumed.stdout


# -- with sessions ----------------------------------------------------------------------------


def session_value(text: str, label: str) -> str:
    match = re.search(rf"^\s*{re.escape(label)}:\s+(.*?)\s*$", text, re.MULTILINE)
    assert match, f"no '{label}' line in:\n{text}"
    return match.group(1)


def session(workspace) -> str:
    result = workspace.run("session")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_audit_report_is_recorded_and_listed_with_the_files_written(workspace):
    configure(workspace, "\n[audit]\nsample_size = 1\n")
    mbox = low_band_mailbox(workspace)

    result = run(workspace, mbox, "--thoroughness", "thorough")

    audit_csv = (workspace.root / "audit.csv").resolve()
    files = report(result)[report(result).index("Files written:") + 1:]
    assert f"  audit report: {audit_csv}" in files
    assert f"  audit report: {audit_csv}" in session(workspace)
    preview = workspace.run("session", "--fresh-preview").stdout
    assert f"Would delete audit report: {audit_csv}" in preview


def test_session_shows_the_lowered_threshold_with_both_numbers(workspace):
    configure(workspace, "\n[audit]\nsample_size = 1\n")
    mbox = low_band_mailbox(workspace)
    run(workspace, mbox)
    assert session_value(session(workspace), "threshold in use") == "0.05"

    run(workspace, mbox, "--thoroughness", "thorough")

    text = session(workspace)
    assert session_value(text, "threshold") == "0.05"  # what the session was started with
    assert session_value(text, "threshold in use") == "0.02 (lowered for this session from the configured 0.05)"
    assert session_value(text, "at or above the threshold") == "4"
    assert session_value(text, "state") == "complete"


def test_a_session_whose_include_was_stopped_is_unfinished_at_the_lowered_threshold(workspace):
    configure(workspace, "\n[audit]\nsample_size = 1\n")
    mbox = low_band_mailbox(workspace)
    code, _, _ = stop_on_chat(workspace, mbox, 4)
    assert code == 143

    text = session(workspace)

    assert session_value(text, "threshold in use") == "0.02 (lowered for this session from the configured 0.05)"
    assert session_value(text, "extracted") == "3 of 4"
    assert session_value(text, "state") == "unfinished: 1 email still to extract"


def test_run_fresh_thorough_clears_the_effective_threshold_and_runs_the_five_stages(workspace):
    configure(workspace, "\n[audit]\nsample_size = 1\n")
    mbox = low_band_mailbox(workspace)
    run(workspace, mbox, "--thoroughness", "thorough")
    audit_csv = (workspace.root / "audit.csv").resolve()
    workspace.ollama.requests.clear()

    result = run(workspace, mbox, "--fresh", "--thoroughness", "thorough")

    out = result.stdout
    head = out[:out.index("Starting classify.")]
    assert f"Deleted audit report: {audit_csv}" in head and "Deleted the store:" in head
    stages = ["Starting classify.", "Starting extract.", "Starting audit.", "Starting include.", "Starting review."]
    assert [out.index(s) for s in stages] == sorted(out.index(s) for s in stages)
    # A fresh store has no effective threshold: the audit reads against 0.05 again and lowers it again.
    assert "Extract pass finished.\n  selected (gate >= 0.05): 1" in out
    assert check_lines(result)[-1].startswith("    threshold lowered from 0.05 to 0.02 for this session")
    assert len(workspace.ollama.chat_requests()) == 5
    assert flight_numbers(workspace.root / "review.csv") == ALL_FLIGHTS


def test_the_audit_inside_run_leaves_out_the_advice_the_audit_command_gives(workspace):
    configure(workspace, "\n[audit]\nsample_size = 1\n")
    mbox = low_band_mailbox(workspace)
    advice = "The config was not changed. After lowering [classify] threshold, run extract"

    result = run(workspace, mbox, "--thoroughness", "thorough")

    assert "Audit finished." in result.stdout and "Reading: the lowest band with a hit" in result.stdout
    assert advice not in result.stdout
    audit = workspace.run("audit", "--mailbox", str(mbox))
    assert audit.returncode == 0, audit.stderr
    assert advice in audit.stdout
