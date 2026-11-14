"""Stopping a command started in the background, on every system.

``run`` stops after the message in progress on SIGTERM; Windows cannot send
SIGTERM to a process, so there Ctrl+Break asks instead (the command is
started in its own process group, see ``conftest``). Windows cannot send
Ctrl+C to one process either: the tests that send it are skipped there, as
what they cover (KeyboardInterrupt and resuming) does not depend on the system.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time

import pytest

POSIX_ONLY_CTRL_C = pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot send Ctrl+C to one process")


def ask_to_stop(proc: subprocess.Popen[str]) -> None:
    """Ask ``proc`` to stop after the message in progress."""
    if sys.platform == "win32":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        # Ctrl+Break arrives on a thread the system starts in the process; let
        # it land before the test lets the message in progress finish.
        time.sleep(0.5)
    else:
        proc.send_signal(signal.SIGTERM)
