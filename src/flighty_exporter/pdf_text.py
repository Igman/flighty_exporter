"""Read the text layer of a PDF attachment with pypdf (no OCR).

Used by the mailbox reader. Guarded so that one bad attachment never fails a
message: PDFs over ``MAX_PDF_BYTES`` are not opened, at most ``MAX_PAGES``
pages are read, and every pypdf error (corrupt, encrypted, unsupported) is
turned into ``PdfNotRead`` with a reason naming the file. A PDF with fewer
than ``MIN_TEXT_CHARS`` non-whitespace characters of text is treated as
image-only.
"""

from __future__ import annotations

import io
import logging
import warnings

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 20
MIN_TEXT_CHARS = 20

# pypdf reports recoverable problems through logging and warnings; they would
# otherwise be printed over the progress line.
_pypdf_log = logging.getLogger("pypdf")
_pypdf_log.addHandler(logging.NullHandler())
_pypdf_log.propagate = False


class PdfNotRead(Exception):
    """The PDF was skipped; the message is still extracted from its body."""


def read_pdf_text(filename: str, data: bytes) -> str:
    if len(data) > MAX_PDF_BYTES:
        raise PdfNotRead(
            f"PDF too large to read ({len(data) / 1024 / 1024:.1f} MB, limit "
            f"{MAX_PDF_BYTES // 1024 // 1024} MB): {filename}"
        )
    try:
        from pypdf import PdfReader

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise PdfNotRead(f"encrypted PDF not read: {filename}")
            pages = []
            for index, page in enumerate(reader.pages):
                if index >= MAX_PAGES:
                    break
                pages.append(page.extract_text() or "")
    except PdfNotRead:
        raise
    except Exception as exc:  # noqa: BLE001 - any pypdf failure skips this attachment only
        detail = f"{type(exc).__name__}: {exc}"[:150]
        raise PdfNotRead(f"PDF could not be read ({detail}): {filename}") from exc
    text = "\n\n".join(page.strip() for page in pages if page.strip())
    if sum(1 for char in text if not char.isspace()) < MIN_TEXT_CHARS:
        raise PdfNotRead(f"image-only PDF not read: {filename}")
    return text
