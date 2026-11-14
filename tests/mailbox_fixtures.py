"""Build small Gmail-Takeout-style mbox files of invented emails.

Every person, address, booking reference and flight here is fictional.
"""

from __future__ import annotations

import mailbox
from email.message import EmailMessage
from pathlib import Path

OWNER = "Robin Quill <robin.quill@example.test>"


def email(
    subject: str,
    body: str = "Hello.\n",
    *,
    sender: str = "Example Sender <news@sender.example>",
    labels: str | None = "Inbox",
    message_id: str | None = "auto",
    html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    date: str = "Tue, 04 Mar 2025 09:15:00 +0000",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = OWNER
    msg["Subject"] = subject
    msg["Date"] = date
    if message_id == "auto":
        slug = "".join(c for c in subject.lower() if c.isalnum())[:40] or "msg"
        message_id = f"<{slug}@fixtures.example>"
    if message_id is not None:
        msg["Message-ID"] = message_id
    if labels is not None:
        msg["X-Gmail-Labels"] = labels
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    for filename, data, mimetype in attachments or []:
        maintype, subtype = mimetype.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def write_mbox(path: Path, messages: list[EmailMessage | bytes]) -> Path:
    """Write messages (EmailMessage objects or raw RFC 822 bytes) to an mbox."""
    box = mailbox.mbox(str(path), create=True)
    try:
        for message in messages:
            box.add(message)
        box.flush()
    finally:
        box.close()
    return path


# A small invented flight confirmation used across tests.
FLIGHT_BODY = """Dear Robin Quill,

Your booking is confirmed. Booking reference: QZX7PL

Flight ZZ1234  Lisbon (LIS) -> Oslo (OSL)
Departs 12 May 2025 07:40, arrives 12:55

Passenger: Robin Quill
"""


def raw_email(
    subject: str,
    body: bytes,
    *,
    content_type: str = "text/plain; charset=utf-8",
    transfer_encoding: str = "8bit",
    sender: str = "Example Sender <news@sender.example>",
    extra_headers: str = "",
) -> bytes:
    """A single-part message built byte by byte, for charset and
    transfer-encoding cases the email package would normalise away."""
    slug = "".join(c for c in subject.lower() if c.isalnum())[:40] or "msg"
    head = (
        f"From: {sender}\r\n"
        f"To: {OWNER}\r\n"
        f"Subject: {subject}\r\n"
        "Date: Tue, 04 Mar 2025 09:15:00 +0000\r\n"
        f"Message-ID: <{slug}@fixtures.example>\r\n"
        "X-Gmail-Labels: Inbox\r\n"
        "MIME-Version: 1.0\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Transfer-Encoding: {transfer_encoding}\r\n"
        f"{extra_headers}"
        "\r\n"
    )
    return head.encode("ascii") + body


# -- PDF attachments, assembled in memory (no binary files in the repo) ----------


def _pdf(objects: list[bytes], padding: int = 0) -> bytes:
    """A minimal valid PDF: the numbered objects (object 1 is the catalog), a
    cross-reference table and a trailer. ``padding`` bytes of comment are
    added to make a large file."""
    out = bytearray(b"%PDF-1.4\n")
    if padding:
        line = b"%" + b"0" * 1023 + b"\n"
        out += line * (padding // len(line) + 1)
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _page_pdf(content: bytes, padding: int = 0) -> bytes:
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ],
        padding,
    )


def text_pdf(text: str, padding: int = 0) -> bytes:
    """A one-page PDF whose text layer is ``text`` (latin-1, one line per line)."""
    ops = ["BT", "/F1 11 Tf", "14 TL", "50 800 Td"]
    for line in text.splitlines():
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({escaped}) Tj T*")
    ops.append("ET")
    return _page_pdf("\n".join(ops).encode("latin-1"), padding)


def image_only_pdf() -> bytes:
    """A one-page PDF with no text layer: a filled rectangle stands in for a
    scanned image."""
    return _page_pdf(b"0.5 g\n50 400 300 200 re f")
