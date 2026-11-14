"""What the stages' printed summaries share."""

from __future__ import annotations

from dataclasses import dataclass

# How many failed messages a summary lists before "... and N more".
FAILURES_LISTED = 20


@dataclass(frozen=True)
class Total:
    """A labelled figure of a stage's session totals so far (what the
    progress screen's counts show): ``label count``, or ``label count of
    whole`` when the figure is a part of a known whole."""

    label: str
    count: int
    of: int | None = None

    @property
    def text(self) -> str:
        shown = f"{self.label} {self.count:,}"
        return shown if self.of is None else f"{shown} of {self.of:,}"


def failure_lines(failures: list[tuple[str, str]]) -> list[str]:
    """The summary lines for ``(identifier, reason)`` failures."""
    lines = [f"    failed {identifier}: {reason}" for identifier, reason in failures[:FAILURES_LISTED]]
    if len(failures) > FAILURES_LISTED:
        lines.append(f"    ... and {len(failures) - FAILURES_LISTED} more")
    return lines
