"""Runs the run engine off the screen's thread and turns what it reports into
Textual messages.

``run_pipeline`` blocks for hours, so it runs on a thread of its own. Its
events arrive on that thread; each is posted to the screen as an
``EngineEvent`` message (``post_message`` is thread-safe), so widgets are
only ever touched on the screen's own thread. When the engine returns (or
raises), a ``RunEnded`` message follows.

Stopping is a flag the screen sets and the engine reads between messages:
the engine finishes the message in progress, commits it and returns.
Continuing after a pause, or retrying after a problem, is simply a new run:
the engine skips whatever is already in the store.

The thread is a daemon thread rather than a Textual thread worker: those
run on the event loop's default executor, which Python joins at exit, so
"quit now" would still wait for the model's answer. A daemon thread ends
with the process; every result is committed as it arrives, so nothing is
left half-written.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from textual.message import Message
from textual.message_pump import MessagePump

from ..config import Config
from ..engine.events import Event
from ..engine.pipeline import RunResult, run_pipeline


@dataclass(frozen=True)
class RunInputs:
    """What a run needs: the same inputs the ``run`` command uses."""

    config: Config
    mailbox: Path
    store: Path
    thoroughness: str
    review_path: Path
    failures_path: Path

    @classmethod
    def from_config(cls, config: Config, mailbox: Path) -> RunInputs:
        return cls(
            config, mailbox, config.store, config.thoroughness, config.review_output, config.failures_output
        )


class EngineEvent(Message):
    """One event from the run engine, delivered on the screen's thread."""

    def __init__(self, event: Event) -> None:
        super().__init__()
        self.event = event


class RunEnded(Message):
    """The engine returned (``result``) or raised before or during the run (``error``)."""

    def __init__(self, result: RunResult | None, error: Exception | None) -> None:
        super().__init__()
        self.result = result
        self.error = error


class _Sink:
    """Swallows the passes' own progress lines, which would draw over the screen."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class EngineRunner:
    """Starts runs of the engine for ``inputs`` and asks them to stop; posts
    their events and their end to ``target``. One run at a time."""

    def __init__(self, inputs: RunInputs, target: MessagePump) -> None:
        self.inputs = inputs
        self.target = target
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="run engine", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """Ask the run to end after the message in progress."""
        self._stop.set()

    def _run(self) -> None:
        inputs = self.inputs
        try:
            result = run_pipeline(
                inputs.config,
                inputs.mailbox,
                inputs.store,
                thoroughness=inputs.thoroughness,
                review_path=inputs.review_path,
                failures_path=inputs.failures_path,
                subscriber=lambda event: self.target.post_message(EngineEvent(event)),
                should_stop=self._stop.is_set,
                progress_stream=_Sink(),  # type: ignore[arg-type]
                command="flighty-exporter",
            )
        except Exception as exc:  # shown on the screen; the app must not crash with the run
            self.target.post_message(RunEnded(None, exc))
        else:
            self.target.post_message(RunEnded(result, None))
