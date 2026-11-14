"""Progress line with an estimated time remaining, written to stderr.

On a terminal the line is redrawn in place after every message. Otherwise
(piped, logged) a line is printed every 5% of the mailbox and at the end, so
logs stay readable.

The same figures are available as data: ``on_advance``, if given, is called
with the progress after every message, and ``counts`` carries whatever the
stage passed (its summary so far), so a caller can follow a pass without
reading its text.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, TextIO


class Progress:
    # Names a preliminary pass within a stage (e.g. extract's scan to locate
    # its messages); "" for the stage's main work.
    phase = ""

    def __init__(
        self,
        total: int,
        label: str,
        stream: TextIO | None = None,
        on_advance: Callable[[Progress], None] | None = None,
        counts: Any = None,
    ):
        self.total = total
        self.label = label
        self.stream = stream or sys.stderr
        self.interactive = self.stream.isatty()
        self.done = 0
        self.work_done = 0
        self.work_seconds = 0.0
        self._step = max(1, total // 20)
        self._last_len = 0
        self.on_advance = on_advance
        self.counts = counts

    def advance(self, seconds_of_work: float | None = None) -> None:
        """One mailbox entry handled; pass the time spent if it did real work
        (a model request), so the estimate ignores instant skips."""
        self.done += 1
        if seconds_of_work is not None:
            self.work_done += 1
            self.work_seconds += seconds_of_work
        if self.interactive or self.done % self._step == 0 or self.done == self.total:
            self._draw()
        if self.on_advance is not None:
            self.on_advance(self)

    @property
    def seconds_per_message(self) -> float | None:
        """Measured over the messages that did real work; None until one has."""
        return self.work_seconds / self.work_done if self.work_done else None

    @property
    def seconds_remaining(self) -> float | None:
        per = self.seconds_per_message
        return None if per is None else (self.total - self.done) * per

    def finish(self) -> None:
        if self.interactive and self._last_len:
            self.stream.write("\n")
            self.stream.flush()

    def line(self) -> str:
        pct = 100.0 * self.done / self.total if self.total else 100.0
        text = f"{self.label}: {self.done}/{self.total} ({pct:.0f}%)"
        if self.work_done:
            per = self.work_seconds / self.work_done
            remaining = (self.total - self.done) * per
            text += f", {per:.2f} s/message, ETA {format_duration(remaining)}"
        else:
            text += ", ETA unknown"
        return text

    def _draw(self) -> None:
        text = self.line()
        if self.interactive:
            pad = " " * max(0, self._last_len - len(text))
            self.stream.write("\r" + text + pad)
            self._last_len = len(text)
        else:
            self.stream.write(text + "\n")
        self.stream.flush()


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"
