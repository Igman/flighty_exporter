"""Thorough runs: after extraction, `run` audits the bands below the threshold
and, within a budget, lowers the session's effective threshold and extracts
the newly included messages. Driven through the command line against the fake
Ollama. Every email, person, booking and flight here is invented."""

from __future__ import annotations

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
