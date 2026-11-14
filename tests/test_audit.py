"""The audit command, driven through the command line against the fake Ollama:
the per-band report it prints, the audit CSV, what the fake received, and how
later extract and review runs behave. Every email, person, booking and flight
here is invented."""

from __future__ import annotations

import signal
import sqlite3
import threading
from pathlib import Path

from fake_ollama import Chat, Decision, chat_user_text
from mailbox_fixtures import email, text_pdf, write_mbox
from test_extract import FLIGHT_BODY, header, read_csv
from test_markup import flight_reservation, json_ld_html, markup_email

AUDIT_HEADER = [
    "section", "band", "band_low", "band_high", "above_threshold", "messages", "sampled", "failed",
    "with_segments", "hit_rate", "estimated_flight_emails",
]
LABELS = [
    "[0.5, 1]", "[0.2, 0.5)", "[0.1, 0.2)", "[0.05, 0.1)", "[0.02, 0.05)",
    "[0.01, 0.02)", "[0.005, 0.01)", "[0.001, 0.005)", "[0, 0.001)",
]
ALL = "All classified messages"
PDF = "Messages with a PDF attachment"

NO_FLIGHT = {"email_kind": "other", "booking_reference": None, "passengers": [], "segments": []}


def flight_record(number: str, origin: str = "LIS", destination: str = "OSL", day: str = "2025-05-12") -> dict:
    return {
        "email_kind": "booking",
        "booking_reference": f"REF{number}",
        "passengers": ["Robin Quill"],
        "segments": [{"date": day, "airline": "Fictair", "flight_number": number, "origin": origin,
                      "destination": destination, "confidence": 0.95}],
    }


def configure(workspace, extra: str = "") -> None:
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n{extra}')


def classify(workspace, mbox: Path, *args: str) -> None:
    result = workspace.run("classify", "--mailbox", str(mbox), *args)
    assert result.returncode == 0, result.stderr


def audit(workspace, mbox: Path, *args: str):
    result = workspace.run("audit", "--mailbox", str(mbox), *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def table(stdout: str, title: str) -> dict[str, dict[str, str]]:
    """The rows of one report section, keyed by band label."""
    lines = stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(title))
    rows: dict[str, dict[str, str]] = {}
    for line in lines[start + 1:]:
        if not line.strip():
            break
        parts = line.split()
        if parts[0].startswith("["):
            label = f"{parts[0]} {parts[1]}"
            rows[label] = dict(zip(("messages", "sampled", "failed", "hits", "rate", "estimate"), parts[2:]))
    return rows


def section_lines(stdout: str, title: str) -> list[str]:
    lines = stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(title))
    end = next(i for i in range(start + 1, len(lines)) if not lines[i].strip())
    return lines[start + 1:end]


def chats_since(workspace, before: int) -> list[str]:
    return workspace.ollama.extracted_subjects()[before:]


def flight_numbers(workspace) -> list[str]:
    return [r["flight_number"] for r in read_csv(workspace.root / "review.csv")]


# -- the report and the lowered-threshold rerun --------------------------------------


def test_per_band_report_then_lowering_the_threshold_extracts_only_new_messages(workspace):
    subjects = {
        "Your flight to Oslo is confirmed": (0.97, flight_record("ZZ1234")),
        "Your train to Porto": (0.3, NO_FLIGHT),
        "Your documents": (0.04, flight_record("ZZ5678", "OSL", "KEF", "2025-06-02")),
        "Garden club newsletter": (0.002, NO_FLIGHT),
    }
    mbox = write_mbox(workspace.root / "mail.mbox", [
        *(email(s, FLIGHT_BODY, sender="Fictair <noreply@fictair.example>") for s in subjects),
        email("Unclassifiable note"),
    ])
    for subject, (gate, record) in subjects.items():
        workspace.ollama.decisions[subject] = Decision(gate, "other")
        workspace.ollama.chats[subject] = Chat(record)
    workspace.ollama.decisions["Unclassifiable note"] = Decision(status=500)
    classify(workspace, mbox)
    assert workspace.run("extract", "--mailbox", str(mbox)).returncode == 0
    assert sorted(workspace.ollama.extracted_subjects()) == ["Your flight to Oslo is confirmed", "Your train to Porto"]
    decisions = len(workspace.ollama.decision_requests())

    result = audit(workspace, mbox)

    # Only the sampled messages without an extraction are sent; the two
    # extracted by extract are reused.
    assert sorted(chats_since(workspace, 2)) == ["Garden club newsletter", "Your documents"]
    assert result.count("messages classified (in bands)") == 4
    assert result.count("failed classification (in no band)") == 1
    assert result.count("sampled messages") == 4
    assert result.count("already extracted (reused)") == 2
    assert result.count("sent to the extraction model") == 2
    rows = table(result.stdout, ALL)
    assert list(rows) == LABELS
    assert rows["[0.5, 1]"] == {"messages": "1", "sampled": "1", "failed": "0", "hits": "1", "rate": "100.0%",
                                "estimate": "~1"}
    assert rows["[0.2, 0.5)"] == {"messages": "1", "sampled": "1", "failed": "0", "hits": "0", "rate": "0.0%",
                                  "estimate": "~0"}
    assert rows["[0.02, 0.05)"]["hits"] == "1"
    assert rows["[0.001, 0.005)"]["hits"] == "0"
    assert rows["[0, 0.001)"] == {"messages": "0", "sampled": "0", "failed": "0", "hits": "0", "rate": "-",
                                  "estimate": "-"}
    # The threshold marker sits between the bands it separates.
    lines = section_lines(result.stdout, ALL)
    marker = next(i for i, line in enumerate(lines) if "threshold 0.05" in line)
    assert lines[marker - 1].split()[0:2] == ["[0.05,", "0.1)"]
    assert lines[marker + 1].split()[0:2] == ["[0.02,", "0.05)"]
    # The below-threshold hit is listed with its segments, and the reading names its band.
    assert "Sampled messages below the threshold that contained flight segments: 1" in result.stdout
    assert 'gate 0.04  2025-03-04 09:15  Fictair <noreply@fictair.example>  "Your documents"' in result.stdout
    assert "2025-06-02  ZZ5678  OSL-KEF" in result.stdout
    assert "Reading: the lowest band with a hit is [0.02, 0.05) (gate 0.04); [classify] threshold = 0.02" in result.stdout
    assert len(workspace.ollama.decision_requests()) == decisions  # nothing reclassified

    csv_rows = read_csv(workspace.root / "audit.csv")
    assert header(workspace.root / "audit.csv") == AUDIT_HEADER
    assert [(r["section"], r["band"]) for r in csv_rows] == [("all", b) for b in LABELS] + [("pdf", b) for b in LABELS]
    by_band = {r["band"]: r for r in csv_rows if r["section"] == "all"}
    assert by_band["[0.02, 0.05)"] == {
        "section": "all", "band": "[0.02, 0.05)", "band_low": "0.02", "band_high": "0.05", "above_threshold": "no",
        "messages": "1", "sampled": "1", "failed": "0", "with_segments": "1", "hit_rate": "1.000",
        "estimated_flight_emails": "1",
    }
    assert by_band["[0.5, 1]"]["above_threshold"] == "yes"
    assert by_band["[0, 0.001)"]["hit_rate"] == "" and by_band["[0, 0.001)"]["estimated_flight_emails"] == ""

    # The audit's below-threshold extraction stays out of the review table.
    assert workspace.run("review").returncode == 0
    assert flight_numbers(workspace) == ["ZZ1234"]

    # A rerun sends nothing and reports the same.
    before = len(workspace.ollama.chat_requests())
    rerun = audit(workspace, mbox)
    assert len(workspace.ollama.chat_requests()) == before
    assert rerun.count("sent to the extraction model") == 0
    assert table(rerun.stdout, ALL) == rows

    # After the threshold is lowered, extract has nothing new to send and the
    # audited flight appears in the review table.
    configure(workspace, "[classify]\nthreshold = 0.02\n")
    extract = workspace.run("extract", "--mailbox", str(mbox))
    assert extract.returncode == 0, extract.stderr
    assert extract.count("selected (gate >= 0.02)") == 3
    assert extract.count("extracted") == 0
    assert extract.count("already extracted") == 3
    assert len(workspace.ollama.chat_requests()) == before
    assert len(workspace.ollama.decision_requests()) == decisions
    assert workspace.run("review").returncode == 0
    assert flight_numbers(workspace) == ["ZZ1234", "ZZ5678"]
    after = audit(workspace, mbox)
    assert "Reading: no sampled message below the threshold 0.02 contained flight segments" in after.stdout


def test_messages_exactly_on_a_band_edge_belong_to_the_band_above_it(workspace):
    edges = [0.0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0]
    mbox = write_mbox(workspace.root / "mail.mbox", [email(f"Edge message {gate:g}") for gate in edges])
    for gate in edges:
        workspace.ollama.decisions[f"Edge message {gate:g}"] = Decision(gate, "other")
    classify(workspace, mbox)

    result = audit(workspace, mbox)

    rows = table(result.stdout, ALL)
    assert {label: row["messages"] for label, row in rows.items()} == {
        "[0.5, 1]": "2", "[0.2, 0.5)": "1", "[0.1, 0.2)": "1", "[0.05, 0.1)": "1", "[0.02, 0.05)": "1",
        "[0.01, 0.02)": "1", "[0.005, 0.01)": "1", "[0.001, 0.005)": "1", "[0, 0.001)": "1",
    }
    assert len(workspace.ollama.chat_requests()) == 10


def test_threshold_inside_a_band_is_shown_inside_it(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Somewhere in the middle")])
    workspace.ollama.decisions["Somewhere in the middle"] = Decision(0.03, "other")
    configure(workspace, "[classify]\nthreshold = 0.03\n")
    classify(workspace, mbox)

    result = audit(workspace, mbox)

    assert "threshold 0.03 falls inside band [0.02, 0.05)" in result.stdout
    assert {r["band"]: r["above_threshold"] for r in read_csv(workspace.root / "audit.csv")}["[0.02, 0.05)"] == "partly"


def test_a_band_larger_than_the_sample_is_sampled_and_estimated(workspace):
    subjects = [f"Bulk message number {i:02d}" for i in range(40)]
    flights = set(subjects[:12])
    mbox = write_mbox(workspace.root / "mail.mbox", [email(s) for s in subjects])
    workspace.ollama.default_decision = Decision(0.15, "other")
    for i, subject in enumerate(subjects):
        workspace.ollama.chats[subject] = Chat(flight_record(f"ZZ{100 + i}") if subject in flights else NO_FLIGHT)
    classify(workspace, mbox)

    result = audit(workspace, mbox, "--sample-size", "10")

    sent = workspace.ollama.extracted_subjects()
    assert len(sent) == len(set(sent)) == 10
    hits = len(flights & set(sent))
    row = table(result.stdout, ALL)["[0.1, 0.2)"]
    assert row["messages"] == "40" and row["sampled"] == "10" and row["hits"] == str(hits)
    assert row["rate"] == f"{hits * 10:.1f}%"
    assert row["estimate"] == f"~{hits * 4}"  # hit rate x band size
    csv_row = next(r for r in read_csv(workspace.root / "audit.csv") if r["section"] == "all" and r["band"] == "[0.1, 0.2)")
    assert csv_row["estimated_flight_emails"] == str(hits * 4)


def test_existing_extractions_are_used_before_new_ones_are_sampled(workspace):
    extracted = [f"High in band {i}" for i in range(3)]
    fresh = [f"Low in band {i}" for i in range(5)]
    mbox = write_mbox(workspace.root / "mail.mbox", [email(s) for s in extracted + fresh])
    for subject in extracted:
        workspace.ollama.decisions[subject] = Decision(0.09, "other")
    for subject in fresh:
        workspace.ollama.decisions[subject] = Decision(0.06, "other")
    configure(workspace, "[classify]\nthreshold = 0.08\n")
    classify(workspace, mbox)
    assert workspace.run("extract", "--mailbox", str(mbox)).returncode == 0
    assert sorted(workspace.ollama.extracted_subjects()) == extracted

    result = audit(workspace, mbox, "--sample-size", "4")

    new = chats_since(workspace, 3)
    assert len(new) == 1 and new[0] in fresh
    assert result.count("already extracted (reused)") == 3
    assert table(result.stdout, ALL)["[0.05, 0.1)"]["sampled"] == "4"


# -- reproducibility ---------------------------------------------------------------


def test_sampling_is_reproducible_for_a_seed_independent_of_mailbox_order(workspace):
    subjects = [f"Quiet message {chr(65 + i)}" for i in range(12)]
    messages = [email(s) for s in subjects]
    mbox = write_mbox(workspace.root / "mail.mbox", messages)
    reversed_mbox = write_mbox(workspace.root / "reversed.mbox", list(reversed(messages)))
    workspace.ollama.default_decision = Decision(0.003, "other")
    configure(workspace, "[audit]\nsample_size = 3\nseed = 2\n")

    def sample(store: str, box: Path, *args: str) -> set[str]:
        classify(workspace, box, "--store", store)
        before = len(workspace.ollama.chat_requests())
        audit(workspace, box, "--store", store, *args)
        return set(chats_since(workspace, before))

    first = sample("a.db", mbox, "--seed", "1", "--sample-size", "4")
    again = sample("b.db", mbox, "--seed", "1", "--sample-size", "4")
    reordered = sample("c.db", reversed_mbox, "--seed", "1", "--sample-size", "4")
    other_seed = sample("d.db", mbox, "--seed", "7", "--sample-size", "4")
    assert len(first) == 4
    assert first == again == reordered
    assert other_seed != first and len(other_seed) == 4

    from_config = sample("e.db", mbox)  # sample_size 3, seed 2 from the config
    from_command_line = sample("f.db", mbox, "--seed", "2", "--sample-size", "3")
    assert len(from_config) == 3
    assert from_config == from_command_line

    # A rerun on a finished store sends nothing.
    before = len(workspace.ollama.chat_requests())
    rerun = audit(workspace, mbox, "--store", "a.db", "--seed", "1", "--sample-size", "4")
    assert len(workspace.ollama.chat_requests()) == before
    assert rerun.count("already extracted (reused)") == 4


# -- what is audited ---------------------------------------------------------------


def test_messages_resolved_from_markup_are_never_audited_or_overwritten(workspace):
    subject = "Your Fictair booking QZX7PL"
    html = json_ld_html(flight_reservation("1234", "LIS", "OSL", "2025-06-01T07:40:00+01:00"))
    plain = write_mbox(workspace.root / "plain.mbox", [
        email(subject, "Booking details to follow.", message_id="<booking@fictair.example>"),
        email("Weekly newsletter"),
    ])
    # The same message classified by the model in an earlier run, then
    # resolved from its markup.
    workspace.ollama.decisions[subject] = Decision(0.9, "booking confirmation")
    workspace.ollama.chats[subject] = Chat(flight_record("ZZ9999"))
    classify(workspace, plain)
    mbox = write_mbox(workspace.root / "mail.mbox", [
        markup_email(subject, html, message_id="<booking@fictair.example>"),
        email("Weekly newsletter"),
    ])
    classify(workspace, mbox)

    result = audit(workspace, mbox)

    assert workspace.ollama.extracted_subjects() == ["Weekly newsletter"]
    assert result.count("messages classified (in bands)") == 1
    assert result.count("resolved from machine-readable data (not audited)") == 1
    assert workspace.run("review").returncode == 0
    [row] = read_csv(workspace.root / "review.csv")
    assert (row["flight_number"], row["source"]) == ("ZZ1234", "markup")


def test_failed_audit_extractions_are_reported_and_retried_on_rerun(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Flaky e-ticket"), email("Plain note")])
    workspace.ollama.decisions["Flaky e-ticket"] = Decision(0.012, "other")
    workspace.ollama.decisions["Plain note"] = Decision(0.012, "other")
    workspace.ollama.chats["Flaky e-ticket"] = Chat(status=500)
    classify(workspace, mbox)

    result = audit(workspace, mbox)

    assert workspace.ollama.extracted_subjects().count("Flaky e-ticket") == 3  # attempt and two retries
    assert result.count("extraction failures") == 1
    assert "failed <flakyeticket@fixtures.example>" in result.stdout
    row = table(result.stdout, ALL)["[0.01, 0.02)"]
    assert row == {"messages": "2", "sampled": "2", "failed": "1", "hits": "0", "rate": "0.0%", "estimate": "~0"}

    workspace.ollama.chats["Flaky e-ticket"] = Chat(flight_record("ZZ4321"))
    before = len(workspace.ollama.chat_requests())
    rerun = audit(workspace, mbox)

    assert chats_since(workspace, before) == ["Flaky e-ticket"]
    row = table(rerun.stdout, ALL)["[0.01, 0.02)"]
    assert row == {"messages": "2", "sampled": "2", "failed": "0", "hits": "1", "rate": "50.0%", "estimate": "~1"}
    assert "Reading: the lowest band with a hit is [0.01, 0.02)" in rerun.stdout


ETICKET = """E-TICKET  Booking code KQ7TZR
Passenger: Robin Quill
ZZ2468  12 Aug 2025  Lisbon (LIS) 08:10 -> Reykjavik (KEF) 11:40"""


def test_pdf_section_samples_messages_with_a_pdf_attachment(workspace):
    plain = [f"Unrelated chatter {i}" for i in range(10)]
    pdfs = [f"Your documents {i}" for i in range(3)]
    messages = [email(s) for s in plain] + [
        email(s, "Please find your documents attached.\n",
              attachments=[(f"KQ7TZR-{i}.pdf", text_pdf(ETICKET), "application/pdf")])
        for i, s in enumerate(pdfs)
    ]
    mbox = write_mbox(workspace.root / "mail.mbox", messages)
    workspace.ollama.default_decision = Decision(0.003, "other")
    workspace.ollama.chats[pdfs[0]] = Chat(flight_record("ZZ2468", "LIS", "KEF", "2025-08-12"))
    classify(workspace, mbox)

    result = audit(workspace, mbox, "--sample-size", "2")

    all_row = table(result.stdout, ALL)["[0.001, 0.005)"]
    pdf_rows = table(result.stdout, PDF)
    assert list(pdf_rows) == LABELS
    assert all_row["messages"] == "13" and all_row["sampled"] == "2"
    assert pdf_rows["[0.001, 0.005)"]["messages"] == "3" and pdf_rows["[0.001, 0.005)"]["sampled"] == "2"
    assert "Messages with a PDF attachment (3 of 13)" in result.stdout
    sent = workspace.ollama.extracted_subjects()
    assert len([s for s in sent if s in pdfs]) >= 2
    assert len(sent) == len(set(sent)) <= 4
    # PDF text reaches the extraction model, as in extract.
    for body in workspace.ollama.chat_requests():
        if "Your documents" in chat_user_text(body):
            assert "ZZ2468  12 Aug 2025" in chat_user_text(body)
    pdf_csv = {r["band"]: r for r in read_csv(workspace.root / "audit.csv") if r["section"] == "pdf"}
    assert pdf_csv["[0.001, 0.005)"]["messages"] == "3"
    assert pdf_csv["[0.001, 0.005)"]["sampled"] == "2"
    assert pdf_csv["[0.5, 1]"]["messages"] == "0"


def test_store_from_before_pdf_attachments_were_recorded_still_works(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [
        email("Your documents", "Please find your documents attached.\n",
              attachments=[("KQ7TZR.pdf", text_pdf(ETICKET), "application/pdf")]),
        email("Plain note"),
    ])
    workspace.ollama.default_decision = Decision(0.04, "other")
    classify(workspace, mbox)
    # Turn the store into one written by the previous version.
    with sqlite3.connect(workspace.root / "flighty_exporter.db") as db:
        db.execute("ALTER TABLE messages DROP COLUMN has_pdf")
        db.execute("PRAGMA user_version=3")

    result = audit(workspace, mbox)

    assert "PDF attachments noted from the mailbox for 2 earlier-classified messages" in result.stdout
    assert table(result.stdout, PDF)["[0.02, 0.05)"]["messages"] == "1"
    rerun = audit(workspace, mbox)
    assert "PDF attachments noted" not in rerun.stdout


# -- interruptions and edge cases ----------------------------------------------------


def test_interrupted_audit_resumes_without_resending(workspace):
    subjects = [f"Low-scoring message {i}" for i in range(6)]
    mbox = write_mbox(workspace.root / "mail.mbox", [email(s) for s in subjects])
    workspace.ollama.default_decision = Decision(0.003, "other")
    classify(workspace, mbox)

    third_request = threading.Event()
    release = threading.Event()

    def block_on_third(request):
        if request.path == "/api/chat" and len(workspace.ollama.chat_requests()) == 3:
            third_request.set()
            release.wait(timeout=30)

    workspace.ollama.on_request = block_on_third
    proc = workspace.start("audit", "--mailbox", str(mbox))
    try:
        assert third_request.wait(timeout=30), "audit never sent a third request"
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        release.set()
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == 130
    assert "Audit interrupted" in stdout
    assert not (workspace.root / "audit.csv").exists()
    first_run = workspace.ollama.extracted_subjects()
    assert len(first_run) == 3

    workspace.ollama.on_request = None
    result = audit(workspace, mbox)

    second_run = workspace.ollama.extracted_subjects()[3:]
    assert len(second_run) == 4  # the two finished messages are not sent again
    assert not set(first_run[:2]) & set(second_run)
    assert result.count("already extracted (reused)") == 2
    assert table(result.stdout, ALL)["[0.001, 0.005)"]["sampled"] == "6"


def test_audit_with_nothing_classified_says_what_to_do(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Junk offer", labels="Spam")])

    missing = workspace.run("audit", "--mailbox", str(mbox))
    assert missing.returncode == 1
    assert "classify" in missing.stderr

    classify(workspace, mbox)
    result = workspace.run("audit", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert "No message has been classified by nimble" in result.stderr
    assert "Run classify first" in result.stderr
    assert not (workspace.root / "audit.csv").exists()
    assert workspace.ollama.chat_requests() == []


def test_output_path_and_sample_size_validation(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Lonely note")])
    classify(workspace, mbox)

    result = audit(workspace, mbox, "--output", "reports/my-audit.csv")
    assert (workspace.root / "reports" / "my-audit.csv").exists()
    assert "Audit table written to reports/my-audit.csv" in result.stdout

    configure(workspace, '[audit]\noutput = "elsewhere.csv"\n')
    audit(workspace, mbox)
    assert (workspace.root / "elsewhere.csv").exists()

    bad = workspace.run("audit", "--mailbox", str(mbox), "--sample-size", "0")
    assert bad.returncode == 2 and "sample-size" in bad.stderr
    configure(workspace, "[audit]\nsample_size = 0\n")
    bad = workspace.run("audit", "--mailbox", str(mbox))
    assert bad.returncode == 1 and "[audit] sample_size" in bad.stderr


def test_unreachable_ollama_stops_the_audit_with_a_clear_message(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Lonely note")])
    classify(workspace, mbox)
    workspace.write_config('[ollama]\naddress = "http://127.0.0.1:9"\n')

    result = workspace.run("audit", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert "127.0.0.1:9" in result.stderr
    assert "rerun audit" in result.stderr
