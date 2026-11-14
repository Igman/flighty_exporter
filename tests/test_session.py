"""Sessions: the ``session`` command describes a session (one store plus the
files the tool wrote for it), ``session --fresh-preview`` says what starting
fresh would delete, and ``run --fresh`` deletes it and runs from scratch.
Driven through the command line against a fake Ollama; every email, person
and flight here is invented."""

from __future__ import annotations

import re
import sqlite3
import subprocess
import time

from test_extract import FLIGHT_SUBJECT
from test_run import BROKEN_SUBJECT, mailbox, run

STORE = "flighty_exporter.db"


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


# -- what the session was started with, and when it was last active ---------------------------


def test_started_with_values_are_not_overwritten_by_a_later_run_with_other_settings(workspace):
    mbox = mailbox(workspace)
    assert workspace.run("classify", "--mailbox", str(mbox)).returncode == 0
    other = workspace.root / "other.mbox"
    other.write_bytes(mbox.read_bytes())
    workspace.write_config(
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[classify]\nthreshold = 0.85\n\n'
        '[passenger]\nnames = ["Robin Quill"]\n'
    )

    run(workspace, "--mailbox", str(other))
    text = session(workspace)

    assert value(text, "mailbox") == str(mbox.resolve())
    assert value(text, "passenger names") == "none (no passenger filtering)"
    assert value(text, "threshold") == "0.05"
    # Progress is counted with the settings in use now.
    assert value(text, "at or above the threshold") == "2"


def test_last_activity_changes_with_each_command_that_writes_results(workspace):
    mbox = mailbox(workspace)
    assert workspace.run("classify", "--mailbox", str(mbox)).returncode == 0
    first = value(session(workspace), "last active")
    time.sleep(1.1)
    assert value(session(workspace), "last active") == first  # describing is not activity

    for command in (("extract", "--mailbox", str(mbox)), ("review",), ("export",),
                    ("mark-added", "2024-09-20", "--route", "OSL", "LIS")):
        before = value(session(workspace), "last active")
        time.sleep(1.1)
        assert workspace.run(*command).returncode == 0, command
        assert value(session(workspace), "last active") != before, command


# -- the files the tool writes for the session -------------------------------------------------


def files_section(text: str) -> list[str]:
    return text[text.index("Files written:"):text.index("Starting fresh would lose:")].splitlines()[1:]


def test_every_kind_of_file_written_is_recorded_with_its_full_path(workspace):
    mbox = mailbox(workspace)
    run(workspace, "--mailbox", str(mbox), "--output", "out/review.csv", "--failures", "out/failures.csv")
    assert workspace.run("audit", "--mailbox", str(mbox), "--output", "out/audit.csv").returncode == 0
    assert workspace.run("export", "--review", "out/review.csv", "--output-dir", "exported").returncode == 0

    root = workspace.root.resolve()
    [import_file] = (root / "exported").glob("openflights-backup-*.csv")
    assert files_section(session(workspace)) == [
        f"  review table: {root / 'out' / 'review.csv'}",
        f"  failures file: {root / 'out' / 'failures.csv'}",
        f"  audit report: {root / 'out' / 'audit.csv'}",
        f"  import file: {import_file}",
        f"  manual-add list: {root / 'exported' / 'manual-add.csv'}",
    ]


def test_the_report_lists_the_files_the_session_recorded(workspace):
    mbox = mailbox(workspace)

    result = run(workspace, "--mailbox", str(mbox), "--output", "out/review.csv", "--failures", "out/failures.csv")

    report = result.stdout[result.stdout.index("Files written:"):].splitlines()
    assert report[1:3] == ["  review table: out/review.csv", "  failures file: out/failures.csv"]
    root = workspace.root.resolve()
    assert files_section(session(workspace)) == [
        f"  review table: {root / 'out' / 'review.csv'}",
        f"  failures file: {root / 'out' / 'failures.csv'}",
    ]


def test_a_recorded_file_the_user_deleted_is_not_listed(workspace):
    mbox = mailbox(workspace)
    run(workspace, "--mailbox", str(mbox))
    (workspace.root / "failures.csv").unlink()

    text = session(workspace)

    assert files_section(text) == [f"  review table: {workspace.root.resolve() / 'review.csv'}"]
    assert "  1 generated file" in text


# -- starting fresh ------------------------------------------------------------------------------


def finished_session(workspace):
    """A run, an audit and an export: every kind of file the tool writes, plus
    files of the user's beside them. Returns (mailbox, generated files, user files)."""
    mbox = mailbox(workspace)
    run(workspace, "--mailbox", str(mbox))
    assert workspace.run("audit", "--mailbox", str(mbox)).returncode == 0
    assert workspace.run("export").returncode == 0
    root = workspace.root
    generated = [root / name for name in ("review.csv", "failures.csv", "audit.csv", "manual-add.csv")]
    generated += list(root.glob("openflights-backup-*.csv"))
    assert len(generated) == 5 and all(path.exists() for path in generated)
    # An unrelated file, one named like an import file, and another database.
    user_files = [root / "notes.txt", root / "openflights-backup-2001-01-01-0000.csv", root / "trip.db"]
    for path in user_files:
        path.write_text("the user's own file\n")
    return mbox, generated, user_files


def snapshot(paths):
    return {path: path.read_bytes() for path in paths}


def test_fresh_preview_lists_what_would_be_deleted_and_deletes_nothing(workspace):
    mbox, generated, user_files = finished_session(workspace)
    before = snapshot([*generated, *user_files, mbox])

    text = session(workspace, "--fresh-preview")

    assert snapshot(before) == before
    assert (workspace.root / STORE).exists()
    lines = text.splitlines()
    assert lines[0] == "Starting fresh would delete:"
    for path in generated:
        assert any(line.startswith("  Would delete ") and line.endswith(str(path.resolve())) for line in lines), path
    assert any(line.startswith(f"  Would delete the store: {workspace.root.resolve() / STORE}") for line in lines)
    for path in user_files:
        assert path.name not in text
    assert "never deletes the mailbox, the config file" in text


def run_without_input(workspace, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the command with no input at all: a prompt would fail or hang."""
    return subprocess.run(
        workspace.command(*args), cwd=workspace.root, env=workspace.env(), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )


def test_run_fresh_deletes_the_session_and_runs_from_scratch_without_asking(workspace):
    mbox, generated, user_files = finished_session(workspace)
    cache = workspace.home / ".cache" / "flighty_exporter" / "openflights"
    kept = snapshot([*user_files, mbox, workspace.config_path, *cache.iterdir()])
    workspace.ollama.requests.clear()

    result = run_without_input(workspace, "run", "--mailbox", str(mbox), "--fresh")

    assert result.returncode == 0, result.stdout + result.stderr
    head = result.stdout[:result.stdout.index("Starting classify.")]
    root = workspace.root.resolve()
    assert len(head.splitlines()) == 6
    for path in generated:
        assert re.search(rf"^Deleted [a-z -]+: {re.escape(str(path.resolve()))}$", head, re.MULTILINE), path
    assert f"Deleted the store: {root / STORE}" in head
    # Everything the user owns is untouched.
    assert snapshot(kept) == kept
    # The run started from scratch: every message is classified and extracted again.
    assert sorted(workspace.ollama.classified_subjects()) == sorted(workspace.ollama.decisions)
    assert set(workspace.ollama.extracted_subjects()) == {FLIGHT_SUBJECT, "Your return flight", BROKEN_SUBJECT}
    # The run wrote its own files; the old session's audit report, import file and manual-add list are gone.
    assert (root / "review.csv").exists() and (root / "failures.csv").exists()
    assert not (root / "audit.csv").exists() and not (root / "manual-add.csv").exists()
    assert list(root.glob("openflights-backup-*.csv")) == [root / "openflights-backup-2001-01-01-0000.csv"]
    assert files_section(session(workspace)) == [
        f"  review table: {root / 'review.csv'}", f"  failures file: {root / 'failures.csv'}",
    ]


def test_run_fresh_deletes_the_store_side_files(workspace):
    mbox = mailbox(workspace)
    run(workspace, "--mailbox", str(mbox))
    for suffix in ("-wal", "-shm", "-journal"):
        (workspace.root / f"{STORE}{suffix}").write_bytes(b"")

    result = run(workspace, "--mailbox", str(mbox), "--fresh")

    head = result.stdout[:result.stdout.index("Starting classify.")]
    assert f"Deleted the store: {workspace.root.resolve() / STORE} (with {STORE}-wal, {STORE}-shm" in head
    # The run's own store is closed: SQLite leaves no side file behind.
    assert not any((workspace.root / f"{STORE}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def test_run_fresh_reports_a_recorded_file_already_deleted_and_leaves_a_folder_alone(workspace):
    mbox = mailbox(workspace)
    run(workspace, "--mailbox", str(mbox))
    (workspace.root / "failures.csv").unlink()
    # The user put a folder where the review table was.
    (workspace.root / "review.csv").unlink()
    (workspace.root / "review.csv").mkdir()
    (workspace.root / "review.csv" / "keep.txt").write_text("mine\n")

    result = workspace.run("run", "--mailbox", str(mbox), "--fresh", "--output", "new-review.csv")

    assert result.returncode == 0, result.stderr
    root = workspace.root.resolve()
    assert f"Already gone: failures file {root / 'failures.csv'}" in result.stdout
    assert f"Left alone (review table is not a regular file the tool wrote): {root / 'review.csv'}" in result.stdout
    assert (workspace.root / "review.csv" / "keep.txt").exists()


def test_run_fresh_with_no_store_simply_runs(workspace):
    mbox = mailbox(workspace)

    result = run(workspace, "--mailbox", str(mbox), "--fresh")

    assert result.stdout.startswith("Starting classify.")
    assert "Run finished." in result.stdout


def test_run_without_fresh_resumes_and_keeps_the_session(workspace):
    mbox = mailbox(workspace)
    run(workspace, "--mailbox", str(mbox))
    started = value(session(workspace), "started")
    workspace.ollama.requests.clear()

    result = run(workspace, "--mailbox", str(mbox))

    assert "Deleted" not in result.stdout
    assert workspace.ollama.decision_requests() == []
    assert set(workspace.ollama.extracted_subjects()) == {BROKEN_SUBJECT}  # only the failed one is retried
    assert value(session(workspace), "started") == started


# -- a store from before sessions were recorded ------------------------------------------------


def test_a_store_from_before_sessions_is_described_and_cleared(workspace):
    mbox = mailbox(workspace)
    run(workspace, "--mailbox", str(mbox))
    with sqlite3.connect(workspace.root / STORE) as db:
        db.execute("DROP TABLE written_files")
        db.execute("DELETE FROM session_values")

    text = session(workspace)

    for label in ("mailbox", "passenger names", "thoroughness", "decision model", "threshold", "started",
                  "last active"):
        assert value(text, label) == "unknown", label
    assert value(text, "classified") == "5"
    assert value(text, "extracted") == "2 of 3"
    assert files_section(text) == ["  none", "  (files written before this store recorded them are not known)"]
    preview = session(workspace, "--fresh-preview")
    assert "Would delete the store" in preview and "Would delete review" not in preview
    workspace.ollama.requests.clear()

    result = run(workspace, "--mailbox", str(mbox), "--fresh")

    head = result.stdout[:result.stdout.index("Starting classify.")].splitlines()
    assert head[0].startswith(f"Deleted the store: {workspace.root.resolve() / STORE}")
    assert "files it generated earlier are not known and were not deleted" in head[1]
    assert len(workspace.ollama.decision_requests()) == 5


# -- has the run reached its end? (what Resume opens) -------------------------------------------


def complete_without_failures(workspace):
    mbox = mailbox(workspace)
    del workspace.ollama.chats[BROKEN_SUBJECT]
    workspace.ollama.decisions[BROKEN_SUBJECT].gate = 0.001
    return mbox


def test_a_run_that_reached_review_is_finished(workspace):
    run(workspace, "--mailbox", str(complete_without_failures(workspace)))

    assert value(session(workspace), "run finished") == "yes (resuming opens the review)"


def test_every_candidate_extracted_without_a_review_is_not_finished(workspace):
    """Classify and extract with nothing left to extract look complete, but
    the run never reached review (a Thorough run's audit, or the rest of the
    mailbox, may still be to do): resuming runs the engine."""
    mbox = complete_without_failures(workspace)
    for command in ("classify", "extract"):
        assert workspace.run(command, "--mailbox", str(mbox)).returncode == 0

    text = session(workspace)

    assert value(text, "state") == "complete"
    assert value(text, "run finished") == "no (resuming runs the remaining stages)"


def test_a_finished_run_whose_review_table_was_deleted_is_not_finished(workspace):
    run(workspace, "--mailbox", str(complete_without_failures(workspace)))
    (workspace.root / "review.csv").unlink()

    assert value(session(workspace), "run finished") == "no (resuming runs the remaining stages)"
