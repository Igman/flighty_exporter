"""Mailbox reader: iterate a Gmail Takeout mbox as normalised messages.

Each entry in the mbox becomes exactly one of:

- ``Message``: a readable message, normalised for the passes;
- ``Skipped``: a message under a Gmail label the tool ignores (Spam, Trash,
  Drafts, Chat), identified but not otherwise read;
- ``Unreadable``: a message that cannot be parsed at all, with the reason.

Every entry carries an identifier: the Message-ID, or, when absent, a stable
identifier derived from the raw message content.

This is the only module that knows about MIME. The passes and the model
clients receive already-prepared text:

- ``body``: the message's cleaned text. In a multipart/alternative the
  text/plain part is preferred and the HTML part (reduced to text, see
  ``html_text``) is the fallback when the plain part is empty or a stub: under
  ``STUB_MAX_CHARS`` characters while the HTML text is at least
  ``STUB_RATIO`` times longer ("View this email in your browser"). Nested
  multiparts are followed; a forwarded email attached as message/rfc822 is
  appended after the outer body with its From, Subject and Date. URLs longer
  than ``LONG_URL_CHARS`` are replaced with ``[link]``.
- ``attachment_filenames``: the names of attached and inline files.
- ``has_pdf_attachment``: whether any part is a PDF, judged by content type
  and filename only (the PDF itself is not read), so the classify pass can
  record it cheaply for the audit.
- ``pdf_texts`` and ``attachment_notes``: the text layer of each PDF
  attachment, and a reason for each PDF that was skipped (image-only, too
  large, unreadable). Read only when the mailbox is opened with
  ``read_pdfs=True`` (the extract pass), since the classify pass never sends
  PDF text.
- ``html_parts`` and ``calendar_texts``: for the markup parser, the raw HTML
  of every text/html part (alternative, attachment or inside a forwarded
  message) and the decoded text of every calendar part (text/calendar,
  application/ics, or any part whose filename ends in .ics). Read for every
  message; the markup parser does no MIME work of its own.

Text parts are decoded from quoted-printable or base64 and from their declared
charset. An unknown or mislabelled charset does not make a message
unreadable; see ``decode_bytes``.
"""

from __future__ import annotations

import codecs
import hashlib
import mailbox
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

from .html_text import html_to_text, tidy_lines
from .pdf_text import PdfNotRead, read_pdf_text

SKIPPED_LABELS = frozenset({"spam", "trash", "drafts", "chat"})

# A plain-text part shorter than this, while the HTML text is at least
# STUB_RATIO times longer, is a stub and the HTML text is used instead.
STUB_MAX_CHARS = 200
STUB_RATIO = 5
LONG_URL_CHARS = 60
LINK_TOKEN = "[link]"

_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]]+", re.IGNORECASE)


@dataclass(frozen=True)
class AttachmentText:
    filename: str
    text: str


@dataclass(frozen=True)
class Message:
    identifier: str
    labels: tuple[str, ...]
    sender: str
    subject: str
    date: str
    body: str
    attachment_filenames: tuple[str, ...] = field(default=())
    pdf_texts: tuple[AttachmentText, ...] = field(default=())
    attachment_notes: tuple[str, ...] = field(default=())
    html_parts: tuple[str, ...] = field(default=())
    calendar_texts: tuple[str, ...] = field(default=())
    has_pdf_attachment: bool = False


@dataclass(frozen=True)
class Skipped:
    identifier: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Unreadable:
    identifier: str
    reason: str


Entry = Message | Skipped | Unreadable


class MailboxError(Exception):
    pass


class Mailbox:
    """An mbox file opened for reading. ``len()`` is the number of entries.

    With ``read_pdfs``, the text layer of PDF attachments is read too."""

    def __init__(self, path: Path, *, read_pdfs: bool = False):
        if not path.is_file():
            raise MailboxError(f"mailbox file not found: {path}")
        self.path = path
        self.read_pdfs = read_pdfs
        # mailbox.mbox scans the file once to index message offsets; messages
        # are then read one at a time, so memory stays flat for large exports.
        self._box = mailbox.mbox(str(path), create=False)
        self._keys = list(self._box.keys())

    def __len__(self) -> int:
        return len(self._keys)

    def __iter__(self) -> Iterator[Entry]:
        for key in self._keys:
            yield read_entry(self._box.get_bytes(key), read_pdfs=self.read_pdfs)

    def close(self) -> None:
        self._box.close()


def read_entry(raw: bytes, *, read_pdfs: bool = False) -> Entry:
    identifier = content_identifier(raw)
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        identifier = _message_id(msg) or identifier
        labels = _labels(msg)
        if SKIPPED_LABELS.intersection(label.lower() for label in labels):
            return Skipped(identifier, labels)
        pdf_texts, notes = _pdf_texts(msg) if read_pdfs else ((), ())
        return Message(
            identifier=identifier,
            labels=labels,
            sender=_header(msg, "From"),
            subject=_header(msg, "Subject"),
            date=_header(msg, "Date"),
            body=_message_text(msg),
            attachment_filenames=_attachment_filenames(msg),
            pdf_texts=pdf_texts,
            attachment_notes=notes,
            html_parts=_html_parts(msg),
            calendar_texts=_calendar_texts(msg),
            has_pdf_attachment=any(not part.is_multipart() and _is_pdf(part) for part in msg.walk()),
        )
    except Exception as exc:  # noqa: BLE001 - any parse failure is per-message
        return Unreadable(identifier, f"{type(exc).__name__}: {exc}")


def content_identifier(raw: bytes) -> str:
    """A stable identifier for a message without a Message-ID."""
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:32]


def replace_long_urls(text: str) -> str:
    return _URL.sub(lambda m: LINK_TOKEN if len(m.group(0)) > LONG_URL_CHARS else m.group(0), text)


def decode_bytes(data: bytes, charset: str | None) -> str:
    """Decode a text part's bytes.

    Tried in order, strictly: the declared charset (iso-8859-1 is read as its
    superset windows-1252, as browsers do), then UTF-8. If neither fits, the
    declared charset is used with replacement characters, unless it is
    unknown, UTF-8 or ASCII (then likely mislabelled), in which case
    windows-1252 with replacement characters is used (latin-1 apart from five
    unassigned bytes)."""
    declared = _codec_name(charset)
    for candidate in (declared, "utf-8"):
        if candidate:
            try:
                return data.decode(candidate)
            except UnicodeDecodeError:
                pass
    fallback = declared if declared not in (None, "utf-8", "ascii") else "cp1252"
    return data.decode(fallback, errors="replace")


def _codec_name(charset: str | None) -> str | None:
    if not charset:
        return None
    try:
        name = codecs.lookup(charset.strip().strip('"')).name
    except (LookupError, ValueError):
        return None
    return "cp1252" if name == "iso8859-1" else name


def _message_id(msg: EmailMessage) -> str | None:
    try:
        value = msg.get("Message-ID")
    except Exception:  # noqa: BLE001 - a broken header falls back to content
        return None
    value = str(value).strip() if value is not None else ""
    return value or None


def _labels(msg: EmailMessage) -> tuple[str, ...]:
    value = msg.get("X-Gmail-Labels")
    if value is None:
        return ()
    return tuple(label.strip() for label in str(value).split(",") if label.strip())


def _header(msg: EmailMessage, name: str) -> str:
    value = msg.get(name)
    return " ".join(str(value).split()) if value is not None else ""


# -- body text -----------------------------------------------------------------


def _message_text(msg: EmailMessage) -> str:
    """The body of ``msg`` followed by each forwarded message it carries."""
    forwarded: list[EmailMessage] = []
    sections = [_part_text(msg, forwarded)]
    for inner in forwarded:
        headers = "\n".join(f"{name}: {_header(inner, name)}" for name in ("From", "Subject", "Date"))
        sections.append(f"---------- Forwarded message ----------\n{headers}\n\n{_message_text(inner)}")
    return replace_long_urls("\n\n".join(section for section in sections if section.strip()).strip())


def _part_text(part: EmailMessage, forwarded: list[EmailMessage]) -> str:
    """The readable text of one part; message/rfc822 parts found on the way
    are collected in ``forwarded`` instead."""
    ctype = part.get_content_type()
    if ctype == "message/rfc822":
        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            forwarded.append(payload[0])
        return ""
    if part.is_multipart():
        children = [child for child in part.get_payload() if isinstance(child, EmailMessage)]
        if ctype == "multipart/alternative":
            return _alternative_text(children, forwarded)
        texts = [_part_text(child, forwarded) for child in children]
        return "\n\n".join(text for text in texts if text)
    if part.get_content_disposition() == "attachment":
        return ""
    if ctype == "text/plain":
        return tidy_lines(_decode_text(part))
    if ctype == "text/html":
        return html_to_text(_decode_text(part))
    return ""


def _alternative_text(children: list[EmailMessage], forwarded: list[EmailMessage]) -> str:
    """Prefer the plain-text alternative; use the richest other alternative
    (normally HTML, possibly inside multipart/related) when the plain part is
    empty or a stub."""
    plain_forwarded: list[EmailMessage] = []
    rich_forwarded: list[EmailMessage] = []
    plain = rich = ""
    for child in children:
        if child.get_content_type() == "text/plain":
            if not plain:
                plain = _part_text(child, plain_forwarded)
        else:
            # Alternatives are ordered from least to most preferred.
            found: list[EmailMessage] = []
            text = _part_text(child, found)
            if text:
                rich, rich_forwarded = text, found
    if not plain or (len(plain) < STUB_MAX_CHARS and len(rich) >= STUB_RATIO * len(plain)):
        chosen, chosen_forwarded = (rich, rich_forwarded) if rich else (plain, plain_forwarded)
    else:
        chosen, chosen_forwarded = plain, plain_forwarded
    forwarded.extend(chosen_forwarded)
    return chosen


def _decode_text(part: EmailMessage) -> str:
    payload = part.get_payload(decode=True) or b""
    return decode_bytes(payload, part.get_content_charset())


# -- attachments -----------------------------------------------------------------


def _attachment_filenames(msg: EmailMessage) -> tuple[str, ...]:
    return tuple(
        name
        for part in msg.walk()
        if (part.get_content_disposition() in ("attachment", "inline") or _is_pdf(part))
        and (name := part.get_filename())
    )


def _is_pdf(part: EmailMessage) -> bool:
    ctype = part.get_content_type()
    if ctype == "application/pdf":
        return True
    filename = part.get_filename() or ""
    return ctype == "application/octet-stream" and filename.lower().endswith(".pdf")


def _pdf_texts(msg: EmailMessage) -> tuple[tuple[AttachmentText, ...], tuple[str, ...]]:
    texts: list[AttachmentText] = []
    notes: list[str] = []
    for part in msg.walk():
        if part.is_multipart() or not _is_pdf(part):
            continue
        filename = part.get_filename() or "unnamed.pdf"
        try:
            data = part.get_payload(decode=True) or b""
            text = read_pdf_text(filename, data)
        except PdfNotRead as exc:
            notes.append(str(exc))
            continue
        texts.append(AttachmentText(filename, replace_long_urls(tidy_lines(text))))
    return tuple(texts), tuple(notes)


# -- raw parts for the markup parser ------------------------------------------------

CALENDAR_TYPES = frozenset({"text/calendar", "application/ics"})


def _html_parts(msg: EmailMessage) -> tuple[str, ...]:
    return tuple(
        _decode_text(part)
        for part in msg.walk()
        if not part.is_multipart() and part.get_content_type() == "text/html"
    )


def _calendar_texts(msg: EmailMessage) -> tuple[str, ...]:
    return tuple(
        _decode_text(part)
        for part in msg.walk()
        if not part.is_multipart()
        and (
            part.get_content_type() in CALENDAR_TYPES
            or (part.get_filename() or "").lower().endswith(".ics")
        )
    )
