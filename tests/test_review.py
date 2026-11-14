"""The review command's output files, driven through the command line.
Every email, person and flight here is invented."""

from __future__ import annotations

import sys

import pytest

from fake_ollama import Chat, Decision
from mailbox_fixtures import FLIGHT_BODY, email, write_mbox
from test_extract import FLIGHT_RECORD, FLIGHT_SUBJECT, read_csv


def extracted_workspace(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email(FLIGHT_SUBJECT, FLIGHT_BODY)])
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    assert workspace.run("classify", "--mailbox", str(mbox)).returncode == 0
    assert workspace.run("extract", "--mailbox", str(mbox)).returncode == 0
    return mbox


def test_unedited_review_table_is_regenerated_silently(workspace):
    mbox = extracted_workspace(workspace)
    review_csv = workspace.root / "review.csv"
    assert workspace.run("review").returncode == 0
    assert [r["flight_number"] for r in read_csv(review_csv)] == ["ZZ1234"]

    # A new extraction model produces a corrected record; the table was never edited.
    workspace.write_config(
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[models]\nextraction = "other-model"\n'
    )
    corrected = {**FLIGHT_RECORD, "segments": [{**FLIGHT_RECORD["segments"][0], "flight_number": "ZZ1243"}]}
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(corrected)
    assert workspace.run("extract", "--mailbox", str(mbox)).returncode == 0

    again = workspace.run("review")

    assert again.returncode == 0, again.stderr
    assert "edited" not in again.stderr
    assert [r["flight_number"] for r in read_csv(review_csv)] == ["ZZ1243"]

    # And again: still unedited, so still no warning.
    assert workspace.run("review").returncode == 0


def test_edited_review_table_is_not_overwritten_without_force(workspace):
    extracted_workspace(workspace)
    review_csv = workspace.root / "review.csv"
    assert workspace.run("review").returncode == 0
    edited = review_csv.read_bytes().replace(b"yes,,,2025-05-12", b"no,,,2025-05-12")
    assert edited != review_csv.read_bytes()
    review_csv.write_bytes(edited)

    refused = workspace.run("review")

    assert refused.returncode == 1
    assert "review.csv" in refused.stderr and "edited" in refused.stderr and "--force" in refused.stderr
    assert review_csv.read_bytes() == edited

    forced = workspace.run("review", "--force")

    assert forced.returncode == 0, forced.stderr
    assert [r["keep"] for r in read_csv(review_csv)] == ["yes"]
    # The forced table is review's own again, so the next run overwrites it silently.
    assert workspace.run("review").returncode == 0


def test_review_table_not_written_by_review_is_not_overwritten_without_force(workspace):
    extracted_workspace(workspace)
    review_csv = workspace.root / "review.csv"
    review_csv.write_text("keep,date\nno,2025-05-12\n", encoding="utf-8")

    refused = workspace.run("review")

    assert refused.returncode == 1
    assert "review.csv" in refused.stderr and "--force" in refused.stderr
    assert review_csv.read_text(encoding="utf-8") == "keep,date\nno,2025-05-12\n"

    forced = workspace.run("review", "--force")

    assert forced.returncode == 0, forced.stderr
    assert [r["flight_number"] for r in read_csv(review_csv)] == ["ZZ1234"]


def test_output_paths_come_from_config_and_the_command_line(workspace):
    extracted_workspace(workspace)
    workspace.write_config(
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n'
        '[review]\noutput = "out/my-review.csv"\nfailures = "out/my-failures.csv"\n'
    )

    from_config = workspace.run("review")

    assert from_config.returncode == 0, from_config.stderr
    assert len(read_csv(workspace.root / "out" / "my-review.csv")) == 1
    assert (workspace.root / "out" / "my-failures.csv").is_file()
    assert "my-review.csv" in from_config.stdout

    cli = workspace.run(
        "review", "--output", str(workspace.root / "cli-review.csv"), "--failures", str(workspace.root / "cli-failures.csv")
    )

    assert cli.returncode == 0, cli.stderr
    assert len(read_csv(workspace.root / "cli-review.csv")) == 1
    assert (workspace.root / "cli-failures.csv").is_file()


def test_selected_but_not_yet_extracted_message_is_listed_as_a_failure(workspace):
    mbox = write_mbox(
        workspace.root / "mail.mbox",
        [email(FLIGHT_SUBJECT, FLIGHT_BODY, sender="Fictair <noreply@fictair.example>")],
    )
    workspace.ollama.decisions[FLIGHT_SUBJECT] = Decision(0.97, "booking confirmation")
    assert workspace.run("classify", "--mailbox", str(mbox)).returncode == 0

    result = workspace.run("review")

    assert result.returncode == 0, result.stderr
    [failure] = read_csv(workspace.root / "failures.csv")
    assert failure["subject"] == FLIGHT_SUBJECT
    assert failure["sender"] == "Fictair <noreply@fictair.example>"
    assert "extract" in failure["reason"]
    assert read_csv(workspace.root / "review.csv") == []


def test_review_without_a_store_is_a_clear_error(workspace):
    result = workspace.run("review")

    assert result.returncode != 0
    assert "store" in result.stderr.lower()
    assert not (workspace.root / "review.csv").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to replace a file another program has open")
def test_review_table_open_in_another_program_is_a_clear_error(workspace):
    # A spreadsheet holding the table open, as Excel does on Windows.
    extracted_workspace(workspace)
    review_csv = workspace.root / "review.csv"
    assert workspace.run("review").returncode == 0
    before = review_csv.read_bytes()

    with review_csv.open("rb"):
        result = workspace.run("review")

    assert result.returncode == 1
    assert f"cannot write {review_csv}: it may be open in another program" in result.stderr
    assert "Traceback" not in result.stderr
    assert review_csv.read_bytes() == before
    assert not (workspace.root / "review.csv.tmp").exists()
    assert workspace.run("review").returncode == 0  # closed again: written as usual
