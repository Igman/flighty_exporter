"""Showing a file path on a screen: on lines of its own, broken after a
path separator rather than in the middle of a name."""

from __future__ import annotations

import os
from pathlib import Path

WIDTH = 60


def path_lines(path: Path | str, width: int = WIDTH) -> list[str]:
    """``path`` as lines of at most ``width`` characters where it can be,
    each but the last ending with a separator. A single name longer than
    ``width`` stays whole (the terminal then wraps it)."""
    text = str(path)
    if len(text) <= width:
        return [text]
    names = text.split(os.sep)
    pieces = [name + os.sep for name in names[:-1]] + [names[-1]]
    lines = [""]
    for piece in pieces:
        if lines[-1] and len(lines[-1]) + len(piece) > width:
            lines.append("")
        lines[-1] += piece
    return lines


def path_text(path: Path | str, indent: str = "") -> str:
    """``path_lines`` joined, each line after ``indent``."""
    return "\n".join(indent + line for line in path_lines(path))
