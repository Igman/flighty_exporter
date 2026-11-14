"""Guided setup's answers, driven through the ``setup`` command.

``setup --mailbox PATH [--name NAME ...] [--thoroughness quick|thorough]``
checks the three answers the application's setup screen asks for and writes
them to the config file, leaving everything else in it as it was. Tests
assert on the file written, what the existing loader reads back from it (via
``session``, which prints the settings it would start with), printed output
and exit status.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from mailbox_fixtures import email, write_mbox


def mbox(workspace, name: str = "mail.mbox") -> Path:
    path = workspace.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_mbox(path, [email("Hello there")])


def remove_config(workspace) -> None:
    workspace.config_path.unlink()


def written(workspace) -> dict:
    return tomllib.loads(workspace.config_path.read_text(encoding="utf-8"))


def loaded_mailbox(workspace) -> Path:
    """The mailbox the tool itself reads from the config file: ``classify``
    names it in its "not found" error once the file is moved away."""
    data = written(workspace)
    path = Path(data["mailbox"]).expanduser()
    return path if path.is_absolute() else workspace.config_path.parent / path


# -- writing a new config ---------------------------------------------------------


def test_setup_creates_the_config_file_with_the_three_answers(workspace):
    remove_config(workspace)
    box = mbox(workspace)

    result = workspace.run(
        "setup", "--mailbox", str(box), "--name", "Robin Quill", "--name", "Robin A. Quill",
        "--thoroughness", "thorough",
    )

    assert result.returncode == 0, result.stderr
    data = written(workspace)
    assert loaded_mailbox(workspace).resolve() == box.resolve()
    assert data["passenger"]["names"] == ["Robin Quill", "Robin A. Quill"]
    assert data["run"]["thoroughness"] == "thorough"
    assert f"Created {workspace.config_path}" in result.stdout
    assert "Robin Quill, Robin A. Quill" in result.stdout
    assert "thorough" in result.stdout


def test_setup_defaults_to_quick_and_no_names(workspace):
    remove_config(workspace)
    box = mbox(workspace)

    result = workspace.run("setup", "--mailbox", str(box))

    assert result.returncode == 0, result.stderr
    data = written(workspace)
    assert data["passenger"]["names"] == []
    assert data["run"]["thoroughness"] == "quick"
    assert "none: passenger filtering is off, every booking is kept" in result.stdout


def test_the_written_config_loads_cleanly_with_the_existing_loader(workspace):
    remove_config(workspace)
    box = mbox(workspace)
    assert workspace.run("setup", "--mailbox", str(box), "--name", "Robin Quill").returncode == 0

    # Any command loads the config first; session reads it without a store.
    result = workspace.run("session")

    assert result.returncode == 0, result.stderr
    assert "No session yet." in result.stdout


# -- an existing config file -------------------------------------------------------


EXISTING = """\
# My settings for the invented mailbox.
store = "trial.db"   # kept next to this file

[ollama]
address = "{address}"
# a comment that must survive
timeout_seconds = 120

[passenger]
# Name variants, as on the tickets.
names = [
    "Old Name",
    "Another Old",
]  # replaced by setup

[classify]
threshold = 0.02

[run]
thoroughness = "quick"  # how hard to look
"""


def test_setup_replaces_the_three_values_in_place_and_keeps_everything_else(workspace):
    workspace.write_config(EXISTING.format(address=workspace.ollama.url))
    box = mbox(workspace)

    result = workspace.run("setup", "--mailbox", str(box), "--name", "Robin Quill", "--thoroughness", "thorough")

    assert result.returncode == 0, result.stderr
    assert workspace.config_path.read_text(encoding="utf-8") == f"""\
# My settings for the invented mailbox.
store = "trial.db"   # kept next to this file
mailbox = "mail.mbox"

[ollama]
address = "{workspace.ollama.url}"
# a comment that must survive
timeout_seconds = 120

[passenger]
# Name variants, as on the tickets.
names = ["Robin Quill"]

[classify]
threshold = 0.02

[run]
thoroughness = "thorough"  # how hard to look
"""
    assert f"Updated {workspace.config_path}" in result.stdout
    assert "Every other setting, comment and section in the file was left as it was." in result.stdout


def test_setup_replaces_an_existing_mailbox_line_keeping_its_comment(workspace):
    other = mbox(workspace, "other.mbox")
    text = f'mailbox = "old.mbox"  # the export\n\n[ollama]\naddress = "{workspace.ollama.url}"\n'
    workspace.write_config(text)

    result = workspace.run("setup", "--mailbox", str(other))

    assert result.returncode == 0, result.stderr
    assert workspace.config_path.read_text(encoding="utf-8") == (
        f'mailbox = "other.mbox"  # the export\n\n[ollama]\naddress = "{workspace.ollama.url}"\n'
        '\n[passenger]\nnames = []\n\n[run]\nthoroughness = "quick"\n'
    )


def test_setup_fills_in_the_example_config(workspace):
    """The example file has a commented-out mailbox line, `names = []` and
    `thoroughness = "quick"`: the mailbox goes under its example, the other
    two are replaced, and nothing else changes."""
    example = (Path(__file__).resolve().parents[1] / "flighty_exporter.example.toml").read_text(encoding="utf-8")
    example = example.replace('address = "http://localhost:11434"', f'address = "{workspace.ollama.url}"')
    workspace.write_config(example)
    box = mbox(workspace)

    result = workspace.run("setup", "--mailbox", str(box), "--name", "Robin Quill")

    assert result.returncode == 0, result.stderr
    expected = example.replace(
        '# mailbox = "Takeout/Mail/All mail Including Spam and Trash.mbox"\n',
        '# mailbox = "Takeout/Mail/All mail Including Spam and Trash.mbox"\nmailbox = "mail.mbox"\n',
    ).replace("\nnames = []\n", '\nnames = ["Robin Quill"]\n')
    assert workspace.config_path.read_text(encoding="utf-8") == expected


def test_setup_adds_missing_sections_at_the_end(workspace):
    text = f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[models]\ndecision = "nimble"\n'
    workspace.write_config(text)
    box = mbox(workspace)

    result = workspace.run("setup", "--mailbox", str(box), "--name", "Robin Quill")

    assert result.returncode == 0, result.stderr
    assert workspace.config_path.read_text(encoding="utf-8") == (
        'mailbox = "mail.mbox"\n\n'
        + text
        + '\n[passenger]\nnames = ["Robin Quill"]\n\n[run]\nthoroughness = "quick"\n'
    )


def test_setup_adds_a_missing_setting_to_an_existing_section(workspace):
    text = (
        f'mailbox = "x.mbox"\n\n[ollama]\naddress = "{workspace.ollama.url}"\n\n'
        "[run]\n# nothing here yet\n\n[passenger]\n"
    )
    workspace.write_config(text)
    box = mbox(workspace)

    result = workspace.run("setup", "--mailbox", str(box), "--thoroughness", "thorough")

    assert result.returncode == 0, result.stderr
    assert workspace.config_path.read_text(encoding="utf-8") == (
        f'mailbox = "mail.mbox"\n\n[ollama]\naddress = "{workspace.ollama.url}"\n\n'
        '[run]\n# nothing here yet\nthoroughness = "thorough"\n\n[passenger]\nnames = []\n'
    )


# -- mailbox paths --------------------------------------------------------------


def test_a_mailbox_inside_the_config_folder_is_written_relative_to_it(workspace):
    box = mbox(workspace, "Takeout/Mail/All mail Including Spam and Trash.mbox")

    # Given relative to the current directory (the workspace).
    result = workspace.run("setup", "--mailbox", "Takeout/Mail/All mail Including Spam and Trash.mbox")

    assert result.returncode == 0, result.stderr
    assert written(workspace)["mailbox"] == "Takeout/Mail/All mail Including Spam and Trash.mbox"
    assert loaded_mailbox(workspace) == box


def test_a_mailbox_outside_the_config_folder_is_written_as_an_absolute_path(workspace, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    box = write_mbox(elsewhere / "mail.mbox", [email("Hello there")])

    result = workspace.run("setup", "--mailbox", str(box))

    assert result.returncode == 0, result.stderr
    assert written(workspace)["mailbox"] == str(box)


def test_a_config_in_another_folder_gets_a_path_that_resolves_to_the_same_file(workspace):
    """--config elsewhere: a mailbox given relative to the current directory
    is written so that, read from the config file's folder, it is the same file."""
    box = mbox(workspace)
    config_dir = workspace.root / "configs"
    config_dir.mkdir()
    workspace.config_path.unlink()
    command = workspace.command("setup", "--mailbox", "mail.mbox")
    command[command.index("--config") + 1] = str(config_dir / "flighty_exporter.toml")

    proc = subprocess.run(command, cwd=workspace.root, env=workspace.env(), capture_output=True, encoding="utf-8")

    assert proc.returncode == 0, proc.stderr
    data = tomllib.loads((config_dir / "flighty_exporter.toml").read_text(encoding="utf-8"))
    assert data["mailbox"] == str(box)
    assert (config_dir / data["mailbox"]).resolve() == box.resolve()


def test_a_config_folder_reached_through_a_symbolic_link_still_gets_a_relative_path(workspace):
    """As on macOS, where /var is a link to /private/var: the mailbox named
    by its real folder is still inside the config file's folder."""
    real = workspace.root / "real"
    real.mkdir()
    (workspace.root / "link").symlink_to(real)
    box = write_mbox(real / "mail.mbox", [email("Hello there")])
    config_path = workspace.root / "link" / "flighty_exporter.toml"
    command = workspace.command("setup", "--mailbox", str(box))
    command[command.index("--config") + 1] = str(config_path)

    proc = subprocess.run(command, cwd=workspace.root, env=workspace.env(), capture_output=True, encoding="utf-8")

    assert proc.returncode == 0, proc.stderr
    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["mailbox"] == "mail.mbox"


def test_the_written_mailbox_is_the_one_the_commands_read(workspace):
    box = mbox(workspace)
    assert workspace.run("setup", "--mailbox", str(box)).returncode == 0
    box.rename(workspace.root / "moved.mbox")

    result = workspace.run("classify")

    assert result.returncode == 1
    assert f"mailbox file not found: {box}" in result.stderr


# -- bad answers ------------------------------------------------------------------


def assert_refused(result, *phrases: str) -> None:
    assert result.returncode == 1
    for phrase in phrases:
        assert phrase in result.stderr, result.stderr
    assert "Nothing was written." in result.stderr


def test_a_missing_mailbox_file_is_refused_and_nothing_is_written(workspace):
    before = workspace.config_path.read_text(encoding="utf-8")

    result = workspace.run("setup", "--mailbox", "nowhere.mbox")

    assert_refused(result, f"There is no file at {workspace.root / 'nowhere.mbox'}.")
    assert workspace.config_path.read_text(encoding="utf-8") == before


def test_a_missing_config_is_not_created_for_a_bad_answer(workspace):
    remove_config(workspace)

    result = workspace.run("setup", "--mailbox", "nowhere.mbox")

    assert result.returncode == 1
    assert not workspace.config_path.exists()


def test_a_folder_is_refused_with_what_to_point_at_instead(workspace):
    (workspace.root / "letters").mkdir()

    result = workspace.run("setup", "--mailbox", "letters")

    assert_refused(result, "letters is a folder, not a file. Point at the mailbox (.mbox) file itself.")


def test_a_takeout_folder_is_refused_with_the_mbox_inside_it_named(workspace):
    box = mbox(workspace, "Takeout/Mail/All mail Including Spam and Trash.mbox")

    result = workspace.run("setup", "--mailbox", "Takeout")

    assert_refused(result, "Takeout is a Google Takeout folder, not a file.", f"Point at the .mbox file inside it: {box}")


def test_a_takeout_folder_without_mail_says_so(workspace):
    (workspace.root / "Takeout" / "Drive").mkdir(parents=True)

    result = workspace.run("setup", "--mailbox", "Takeout")

    assert_refused(result, "is a Google Takeout folder, but it holds no .mbox file")


def test_a_zip_file_is_refused_with_advice_to_unzip_it(workspace):
    archive = workspace.root / "takeout-20260101.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("Takeout/Mail/All mail Including Spam and Trash.mbox", "From x\n")

    result = workspace.run("setup", "--mailbox", str(archive))

    assert_refused(result, "is a zip archive, not a mailbox", "unzip it, then point at the .mbox file inside")


def test_a_file_that_is_not_an_mbox_is_refused(workspace):
    (workspace.root / "notes.txt").write_text("Shopping list\nmilk\n")

    result = workspace.run("setup", "--mailbox", "notes.txt")

    assert_refused(result, "does not look like an mbox file", "this file begins with 'Shopping list'")


def test_an_empty_file_is_refused(workspace):
    (workspace.root / "empty.mbox").write_text("")

    result = workspace.run("setup", "--mailbox", "empty.mbox")

    assert_refused(result, "empty.mbox is empty.")


@pytest.mark.skipif(sys.platform == "win32", reason="chmod cannot make a file unreadable on Windows")
def test_an_unreadable_file_is_refused(workspace):
    box = mbox(workspace)
    box.chmod(0)
    try:
        result = workspace.run("setup", "--mailbox", str(box))
    finally:
        box.chmod(0o644)

    assert_refused(result, f"{box} cannot be read")


def test_a_name_that_is_only_a_title_is_refused(workspace):
    box = mbox(workspace)
    before = workspace.config_path.read_text(encoding="utf-8")

    result = workspace.run("setup", "--mailbox", str(box), "--name", "Robin Quill", "--name", "Mr")

    assert_refused(result, "'Mr' is not a name")
    assert workspace.config_path.read_text(encoding="utf-8") == before


def test_a_config_file_that_cannot_be_read_is_left_alone(workspace):
    workspace.config_path.write_text("[ollama\naddress = \n")
    box = mbox(workspace)

    result = workspace.run("setup", "--mailbox", str(box))

    assert_refused(result, f"cannot read config file {workspace.config_path}")
    assert workspace.config_path.read_text(encoding="utf-8") == "[ollama\naddress = \n"


def test_a_config_the_loader_would_reject_is_not_written(workspace):
    text = f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[classify]\nthreshold = 7\n'
    workspace.write_config(text)
    box = mbox(workspace)

    result = workspace.run("setup", "--mailbox", str(box))

    assert_refused(result, "threshold must be between 0 and 1")
    assert workspace.config_path.read_text(encoding="utf-8") == text
    assert [p.name for p in workspace.root.iterdir() if p.name.startswith(".")] == []


def test_an_unknown_thoroughness_is_a_usage_error(workspace):
    box = mbox(workspace)

    result = workspace.run("setup", "--mailbox", str(box), "--thoroughness", "extreme")

    assert result.returncode == 2
    assert "invalid choice: 'extreme'" in result.stderr


# -- setup --check: is the model server ready? -----------------------------------------


def check(workspace, config_text: str | None = None, *, pulled: bool = True):
    if config_text is not None:
        workspace.write_config(config_text, pulled=pulled)
    box = mbox(workspace)
    return workspace.run("setup", "--mailbox", str(box), "--check")


def test_check_reports_a_ready_server(workspace):
    result = check(workspace)

    assert result.returncode == 0, result.stderr
    assert f"Model server {workspace.ollama.url}:" in result.stdout
    assert "on this machine: yes" in result.stdout
    assert "reachable:       yes" in result.stdout
    assert "version:         0.40.16 (new enough)" in result.stdout
    assert "[models] decision: nimble: pulled" in result.stdout
    assert "[models] extraction: gemma4:12b-it-q4_K_M: pulled" in result.stdout
    assert "Ready: the model server has both models." in result.stdout
    assert workspace.ollama.paths() == ["/api/version", "/api/tags"]


def test_check_reports_an_unreachable_server_with_the_existing_message(workspace):
    from test_preflight import dead_address

    address = dead_address()
    result = check(workspace, f'[ollama]\naddress = "{address}"\n')

    assert result.returncode == 1
    assert "reachable:       no" in result.stdout
    assert f"error: the Ollama server at {address} is not reachable" in result.stdout
    assert "Start it with `ollama serve`, or open the Ollama app, then rerun setup --check." in result.stdout
    # The answers were still written.
    assert written(workspace)["mailbox"] == "mail.mbox"


def test_check_reports_a_server_that_is_too_old(workspace):
    workspace.ollama.version = "0.30.1"

    result = check(workspace)

    assert result.returncode == 1
    assert "version:         0.30.1 (too old)" in result.stdout
    assert "is version 0.30.1, but flighty-exporter needs Ollama 0.35.0 or later" in result.stdout
    assert "/api/tags" not in workspace.ollama.paths()


def test_check_lists_a_missing_decision_model_with_its_size(workspace):
    workspace.ollama.models = ["gemma4:12b-it-q4_K_M"]

    result = check(workspace)

    assert result.returncode == 1
    assert "[models] decision: nimble: missing (about 9.3 GB)" in result.stdout
    assert "[models] extraction: gemma4:12b-it-q4_K_M: pulled" in result.stdout
    assert "the model nimble ([models] decision) is not pulled" in result.stdout
    assert "Or download the missing models with `flighty-exporter pull-models --yes`." in result.stdout


def test_check_lists_a_missing_extraction_model_of_unknown_size(workspace):
    result = check(
        workspace,
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[models]\nextraction = "fictional-reader:3b"\n',
        pulled=False,
    )

    assert result.returncode == 1
    assert "[models] decision: nimble: pulled" in result.stdout
    assert (
        "[models] extraction: fictional-reader:3b: missing (size not known until the download starts)"
        in result.stdout
    )


def test_check_refuses_a_non_local_address_before_any_request(workspace):
    from test_preflight import NON_LOCAL

    result = check(workspace, f'[ollama]\naddress = "{NON_LOCAL}"\n')

    assert result.returncode == 1
    assert "on this machine: no" in result.stdout
    assert f"error: refusing to use the Ollama server at {NON_LOCAL}" in result.stdout
    assert "email content would leave this machine" in result.stdout
    assert workspace.ollama.paths() == []
