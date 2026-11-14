"""What the stages' printed summaries share."""

from __future__ import annotations

# How many failed messages a summary lists before "... and N more".
FAILURES_LISTED = 20


def failure_lines(failures: list[tuple[str, str]]) -> list[str]:
    """The summary lines for ``(identifier, reason)`` failures."""
    lines = [f"    failed {identifier}: {reason}" for identifier, reason in failures[:FAILURES_LISTED]]
    if len(failures) > FAILURES_LISTED:
        lines.append(f"    ... and {len(failures) - FAILURES_LISTED} more")
    return lines
