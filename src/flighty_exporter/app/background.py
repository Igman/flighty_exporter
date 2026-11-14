"""Setup's slow work off the screen's thread: the model-server check and model downloads.

Both talk to the server (a check can wait seconds for an answer, a download
takes many minutes), so each runs on a daemon thread of its own and reports
back with Textual messages (``post_message`` is thread-safe), like the
engine runner. A daemon thread ends with the process, so quitting during a
download exits at once; Ollama keeps the partial download and resumes it
next time.
"""

from __future__ import annotations

import threading

from textual.message import Message
from textual.message_pump import MessagePump

from ..config import Config
from ..engine.readiness import Readiness, check_readiness
from ..ollama.connection import PreflightError
from ..ollama.pull import PullFailed, PullProgress, pull_model

# What the server checks' messages tell the user to do again, in the application.
APP_COMMAND = "the check (press c)"


class ReadinessChecked(Message):
    def __init__(self, readiness: Readiness) -> None:
        super().__init__()
        self.readiness = readiness


class PullUpdate(Message):
    def __init__(self, model: str, progress: PullProgress) -> None:
        super().__init__()
        self.model = model
        self.progress = progress


class PullEnded(Message):
    """The download of ``model`` finished (``error`` None) or failed."""

    def __init__(self, model: str, error: str | None) -> None:
        super().__init__()
        self.model = model
        self.error = error


def check_in_background(config: Config, target: MessagePump) -> None:
    """Check the model server for ``config``; posts ``ReadinessChecked``."""

    def work() -> None:
        target.post_message(ReadinessChecked(check_readiness(config, command=APP_COMMAND)))

    threading.Thread(target=work, name="readiness check", daemon=True).start()


def pull_in_background(config: Config, model: str, target: MessagePump) -> None:
    """Download ``model`` through the server; posts ``PullUpdate`` for each
    progress line and ``PullEnded`` at the end. The application has no
    override of the locality check: only the config's own setting counts."""

    def work() -> None:
        try:
            for progress in pull_model(
                config.ollama_address,
                model,
                allow_remote=config.ollama_allow_remote,
                command=APP_COMMAND,
                timeout=config.ollama_timeout_seconds,
            ):
                target.post_message(PullUpdate(model, progress))
        except (PullFailed, PreflightError) as exc:
            target.post_message(PullEnded(model, str(exc)))
        except Exception as exc:  # shown on the screen; the app must not crash with the download
            target.post_message(PullEnded(model, f"error: {exc}"))
        else:
            target.post_message(PullEnded(model, None))

    threading.Thread(target=work, name=f"pull {model}", daemon=True).start()
