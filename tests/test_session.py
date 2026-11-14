"""Sessions: the ``session`` command describes a session (one store plus the
files the tool wrote for it), ``session --fresh-preview`` says what starting
fresh would delete, and ``run --fresh`` deletes it and runs from scratch.
Driven through the command line against a fake Ollama; every email, person
and flight here is invented."""

from __future__ import annotations

import re

from test_run import BROKEN_SUBJECT, mailbox, run


def session(workspace, *args: str):
    result = workspace.run("session", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def value(text: str, label: str) -> str:
    """The value printed after ``label:`` on its own line."""
    match = re.search(rf"^\s*{re.escape(label)}:\s+(.*?)\s*$", text, re.MULTILINE)
    assert match, f"no '{label}' line in:\n{text}"
    return match.group(1)


# -- no session -------------------------------------------------------------------------


def test_without_a_store_there_is_no_session(workspace):
    result = workspace.run("session")

    assert result.returncode == 0
    assert result.stdout.strip() == "No session yet."
    assert not (workspace.root / "flighty_exporter.db").exists()
    assert session(workspace, "--fresh-preview").strip() == "No session yet."


# -- describing a session after each stage -------------------------------------------------


def test_session_after_classify_describes_the_mailbox_settings_and_progress(workspace):
    mbox = mailbox(workspace)
    assert workspace.run("classify", "--mailbox", str(mbox)).returncode == 0

    text = session(workspace)

    assert value(text, "mailbox") == str(mbox.resolve())
    assert value(text, "passenger names") == "none (no passenger filtering)"
    assert value(text, "thoroughness") == "quick"
    assert value(text, "decision model") == "nimble"
    assert value(text, "extraction model") == "gemma4:12b-it-q4_K_M"
    assert value(text, "threshold") == "0.05"
    assert value(text, "last active") != "unknown"
    assert value(text, "messages recorded") == "6"
    assert value(text, "classified") == "5"
    assert value(text, "resolved from machine-readable data") == "1"
    assert value(text, "at or above the threshold") == "3"
    assert value(text, "extracted") == "0 of 3"
    assert value(text, "flights") == "1"  # the one read from markup
    assert value(text, "flights with a saved decision") == "0"
    assert value(text, "state") == "unfinished: 3 emails still to extract"
    lose = text[text.index("Starting fresh would lose:"):]
    assert "  classification of 5 emails" in lose
    assert "  flight data read from machine-readable data in 1 email" in lose
    assert "extraction" not in lose


def test_session_after_extract_review_and_export(workspace):
    mbox = mailbox(workspace)
    for command in ("classify", "extract"):
        assert workspace.run(command, "--mailbox", str(mbox)).returncode == 0

    text = session(workspace)
    assert value(text, "extracted") == "2 of 3"  # one extraction failed
    assert value(text, "state") == "unfinished: 1 email still to extract"
    assert "  extraction of 2 emails" in text

    assert workspace.run("review").returncode == 0
    assert workspace.run("export").returncode == 0
    assert workspace.run("mark-added", "2024-09-20", "--route", "OSL", "LIS").returncode == 0

    text = session(workspace)
    assert value(text, "flights") == "3"
    assert value(text, "flights with a saved decision") == "1"
    assert "  1 review decision" in text
    files = text[text.index("Files written:"):text.index("Starting fresh would lose:")]
    root = workspace.root.resolve()
    assert f"  review table: {root / 'review.csv'}" in files
    assert f"  failures file: {root / 'failures.csv'}" in files
    assert f"  manual-add list: {root / 'manual-add.csv'}" in files
    assert re.search(r"  import file: .*openflights-backup-.*\.csv", files)
    assert "  4 generated files" in text


def test_a_session_whose_every_candidate_is_extracted_is_complete(workspace):
    mbox = mailbox(workspace)
    del workspace.ollama.chats[BROKEN_SUBJECT]
    workspace.ollama.decisions[BROKEN_SUBJECT].gate = 0.001
    run(workspace, "--mailbox", str(mbox))

    text = session(workspace)

    assert value(text, "state") == "complete"
    assert value(text, "extracted") == "2 of 2"
