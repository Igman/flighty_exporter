"""The bare command: with no subcommand it opens the terminal application,
but only when both input and output are a terminal. Anywhere else (a pipe, a
script, a log) it prints the help (the same text as ``--help``) and a line
saying a command is needed there, and exits with status 2, so automation
never hangs and a script that forgot its command still fails. The
application's screens themselves are verified by hand (see docs/manual-checks.md)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading

import pytest

if sys.platform != "win32":
    import pty  # POSIX only: Windows has no pseudo-terminals to open this way

needs_pty = pytest.mark.skipif(sys.platform == "win32", reason="opening a pseudo-terminal needs POSIX")

NOT_A_TERMINAL = (
    "error: the application opens only in a terminal (input and output both a terminal); "
    "otherwise give a command, such as run\n"
)


def test_the_bare_command_without_a_terminal_prints_help_and_fails(workspace):
    result = workspace.run()
    assert result.returncode == 2
    assert result.stdout == workspace.run("--help").stdout
    assert result.stdout.startswith("usage: flighty-exporter")
    assert "pull-models" in result.stdout
    assert result.stderr == NOT_A_TERMINAL


def _run_with_one_terminal_end(workspace, *, terminal: str) -> tuple[int, str]:
    """Run the bare command with only stdin or only stdout on a terminal.
    The terminal end is read while the command runs (the help it prints
    would otherwise fill the terminal's buffer)."""
    controller, terminal_end = pty.openpty()
    try:
        proc = subprocess.Popen(
            workspace.command(),
            cwd=workspace.root,
            env=workspace.env(),
            stdin=terminal_end if terminal == "stdin" else subprocess.DEVNULL,
            stdout=terminal_end if terminal == "stdout" else subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        os.close(terminal_end)
        terminal_end = -1
        reader = threading.Thread(target=_drain, args=(controller,), daemon=True)
        reader.start()
        _, stderr = proc.communicate(timeout=30)
    finally:
        if terminal_end != -1:
            os.close(terminal_end)
        os.close(controller)
    return proc.returncode, stderr


def _drain(descriptor: int) -> None:
    try:
        while os.read(descriptor, 4096):
            pass
    except OSError:
        pass


@needs_pty
def test_the_bare_command_needs_input_to_be_a_terminal_too(workspace):
    assert _run_with_one_terminal_end(workspace, terminal="stdout") == (2, NOT_A_TERMINAL)


@needs_pty
def test_the_bare_command_needs_output_to_be_a_terminal_too(workspace):
    assert _run_with_one_terminal_end(workspace, terminal="stdin") == (2, NOT_A_TERMINAL)


def test_the_subcommands_do_not_load_the_application(workspace):
    # The application's library is loaded only when the application opens,
    # so the single commands start as quickly as before.
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", *workspace.command()[1:], "review"],
        cwd=workspace.root,
        env=workspace.env(),
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert "store not found" in proc.stderr
    assert "textual" not in proc.stderr
    assert "flighty_exporter.app" not in proc.stderr
