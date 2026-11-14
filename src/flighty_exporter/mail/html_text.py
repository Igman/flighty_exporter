"""Reduce an HTML email body to readable plain text (standard library only).

Used by the mailbox reader. The aim is text a model can read, not a faithful
rendering:

- script, style, head, title, noscript and template content and comments are
  dropped; character references are decoded;
- block elements, ``<br>`` and table rows start new lines, and table cells in
  a row are separated by `` | `` so that tabular itineraries stay readable
  ("Flight | BA286", not "FlightBA286");
- link text is kept, hrefs are not; image alt text is kept when not empty;
- runs of whitespace collapse to one space and runs of blank lines to one
  blank line.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

CELL_SEPARATOR = " | "

_SKIPPED = frozenset({"script", "style", "head", "title", "noscript", "template"})
_BLOCKS = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "hr", "html", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "tfoot", "thead",
        "tr", "ul",
    }
)
_CELLS = frozenset({"td", "th"})
# Invisible characters marketing emails use to pad preview text.
_INVISIBLE = re.compile("[­͏​‌‍⁠﻿]")
_SPACES = re.compile(r"[ \t\r\n\f\v ]+")


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0
        # One entry per open table row: how many cells it has started.
        self.rows: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "br":
            self.out.append("\n")
        elif tag == "tr":
            self.out.append("\n")
            self.rows.append(0)
        elif tag in _CELLS:
            if self.rows:
                if self.rows[-1]:
                    self.out.append(CELL_SEPARATOR)
                self.rows[-1] += 1
        elif tag in _BLOCKS:
            self.out.append("\n")
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self.out.append(f" {alt} ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            return  # <script/> opens and closes nothing
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "tr":
            if self.rows:
                self.rows.pop()
            self.out.append("\n")
        elif tag == "table":
            # An unclosed row inside a closed table.
            self.out.append("\n")
        elif tag in _BLOCKS:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.out.append(_SPACES.sub(" ", data))


def html_to_text(html: str) -> str:
    converter = _Converter()
    converter.feed(html)
    converter.close()
    return tidy_lines("".join(converter.out), cells=True)


def tidy_lines(text: str, *, cells: bool = False) -> str:
    """Strip each line, drop invisible padding characters and collapse runs of
    blank lines. With ``cells``, also collapse horizontal whitespace and drop
    empty table cells (a line of separators, or one starting or ending with
    one)."""
    text = _INVISIBLE.sub("", text)
    lines: list[str] = []
    for line in text.split("\n"):
        if cells:
            line = _SPACES.sub(" ", line)
            fields = [field.strip() for field in line.split(CELL_SEPARATOR.strip())]
            line = CELL_SEPARATOR.join(field for field in fields if field)
        line = line.strip()
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()
