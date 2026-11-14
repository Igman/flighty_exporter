"""Showing a file path on a screen: on lines of its own, broken after a
path separator rather than in the middle of a name."""

from __future__ import annotations

import os
import re
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


# "label: " then an absolute path: /a/path, or on Windows C:\a\path or \\server\share.
_LABELLED_PATH = re.compile(r"(.*?): (/.*|[A-Za-z]:\\.*|\\\\.*)$")


def wrap_path_lines(text: str) -> str:
    """``text`` with every over-long line of the form "label: /a/path" put
    as the label, then the path on lines of its own under it (the report's
    "Files written", the lines starting fresh prints)."""
    out = []
    for line in text.split("\n"):
        match = _LABELLED_PATH.match(line)
        if match and len(line) > WIDTH:
            label, path = match.groups()
            indent = " " * (len(label) - len(label.lstrip()) + 2)
            out.append(f"{label}:")
            out.append(path_text(path, indent))
        else:
            out.append(line)
    return "\n".join(out)
