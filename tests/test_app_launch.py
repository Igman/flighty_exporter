"""The bare command: with no subcommand it opens the terminal application,
but only when both input and output are a terminal. Anywhere else (a pipe, a
script, a log) it prints the same usage error as before the application
existed, so automation never hangs. The application's screens themselves
are verified by hand (see docs/manual-checks.md)."""

from __future__ import annotations

import os
import pty
import subprocess
import sys

USAGE_ERROR = (
    "usage: flighty-exporter [-h] [--config CONFIG] command ...\n"
    "flighty-exporter: error: the following arguments are required: command\n"
)


def test_the_bare_command_without_a_terminal_prints_usage_as_before(workspace):
    result = workspace.run()
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == USAGE_ERROR


def _run_with_one_terminal_end(workspace, *, terminal: str) -> tuple[int, str]:
    """Run the bare command with only stdin or only stdout on a terminal."""
    controller, terminal_end = pty.openpty()
    try:
        proc = subprocess.run(
            workspace.command(),
            cwd=workspace.root,
            env=workspace.env(),
            stdin=terminal_end if terminal == "stdin" else subprocess.DEVNULL,
            stdout=terminal_end if terminal == "stdout" else subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    finally:
        os.close(terminal_end)
        os.close(controller)
    return proc.returncode, proc.stderr


def test_the_bare_command_needs_input_to_be_a_terminal_too(workspace):
    assert _run_with_one_terminal_end(workspace, terminal="stdout") == (2, USAGE_ERROR)


def test_the_bare_command_needs_output_to_be_a_terminal_too(workspace):
    assert _run_with_one_terminal_end(workspace, terminal="stdin") == (2, USAGE_ERROR)


def test_the_subcommands_do_not_load_the_application(workspace):
    # The application's library is loaded only when the application opens,
    # so the single commands start as quickly as before.
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", *workspace.command()[1:], "review"],
        cwd=workspace.root,
        env=workspace.env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "store not found" in proc.stderr
    assert "textual" not in proc.stderr
    assert "flighty_exporter.app" not in proc.stderr
