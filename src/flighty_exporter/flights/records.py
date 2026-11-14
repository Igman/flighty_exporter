"""Record types: what the passes produce for one message, and the structural
validation of an extraction record.

- ``Decision``: the classify pass result (gate probability and email type).
- ``ExtractionRecord`` and its ``Segment`` s: the flights one email describes,
  whether read by the extraction model or parsed from the email's own markup.
- ``Extraction``: a stored extraction record with how it was obtained.

``validate_record`` checks a record's structure and types and raises
``ExtractionError`` when it is unusable. The model server's clients, the
markup parser and the store all build or read records through these types.
No I/O.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

# The ``model`` of a record parsed from the email's own machine-readable data
# (see ``mail.markup``) rather than read by the extraction model.
MARKUP_SOURCE = "markup"

EMAIL_KINDS = ("booking", "change", "cancellation", "check-in", "other")

RECORD_FIELDS = ("email_kind", "booking_reference", "passengers", "segments")
SEGMENT_FIELDS = ("date", "airline", "flight_number", "origin", "destination", "confidence")


@dataclass(frozen=True)
class Decision:
    gate_probability: float
    email_type: str
    type_probabilities: dict[str, float]
    type_confidence: float | None
    model: str


@dataclass(frozen=True)
class Segment:
    date: str
    airline: str
    flight_number: str
    origin: str
    destination: str
    confidence: float


@dataclass(frozen=True)
class ExtractionRecord:
    email_kind: str
    booking_reference: str | None
    passengers: tuple[str, ...]
    segments: tuple[Segment, ...]
    model: str

    def to_json(self) -> dict[str, Any]:
        return {
            "email_kind": self.email_kind,
            "booking_reference": self.booking_reference,
            "passengers": list(self.passengers),
            "segments": [segment.__dict__ for segment in self.segments],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], model: str) -> "ExtractionRecord":
        return validate_record(data, model)


@dataclass(frozen=True)
class Extraction:
    record: ExtractionRecord
    attempts: int
    # The input was cut at the character budget.
    input_truncated: bool = False


class ExtractionError(Exception):
    """One answer was unusable; the run can continue."""


def validate_record(data: Any, model: str) -> ExtractionRecord:
    """Check the record's structure and types. Airline and airport codes are
    not checked against reference tables here."""
    if not isinstance(data, dict):
        raise ExtractionError(f"answer is not a JSON object: {preview(json.dumps(data))}")
    missing = [key for key in RECORD_FIELDS if key not in data]
    if missing:
        raise ExtractionError(f"answer is missing {', '.join(missing)}: {preview(json.dumps(data))}")

    kind = data["email_kind"]
    if kind not in EMAIL_KINDS:
        raise ExtractionError(f"email_kind is not one of {', '.join(EMAIL_KINDS)}: {kind!r}")

    reference = data["booking_reference"]
    if reference is not None and not isinstance(reference, str):
        raise ExtractionError(f"booking_reference is not a string or null: {reference!r}")
    reference = (reference or "").strip() or None

    passengers = data["passengers"]
    if not isinstance(passengers, list) or not all(isinstance(p, str) for p in passengers):
        raise ExtractionError(f"passengers is not a list of strings: {passengers!r}")

    segments = data["segments"]
    if not isinstance(segments, list):
        raise ExtractionError(f"segments is not a list: {segments!r}")
    return ExtractionRecord(
        email_kind=kind,
        booking_reference=reference,
        passengers=tuple(p.strip() for p in passengers if p.strip()),
        segments=tuple(_segment(i, s) for i, s in enumerate(segments, 1)),
        model=model,
    )


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _segment(index: int, data: Any) -> Segment:
    where = f"segment {index}"
    if not isinstance(data, dict):
        raise ExtractionError(f"{where} is not an object: {data!r}")
    missing = [key for key in SEGMENT_FIELDS if key not in data]
    if missing:
        raise ExtractionError(f"{where} is missing {', '.join(missing)}")
    for key in ("date", "airline", "flight_number", "origin", "destination"):
        if not isinstance(data[key], str):
            raise ExtractionError(f"{where} {key} is not a string: {data[key]!r}")
    day = data["date"].strip()
    try:
        if not _DATE.match(day):
            raise ValueError
        date.fromisoformat(day)
    except ValueError:
        raise ExtractionError(f"{where} date is not a valid YYYY-MM-DD date: {data['date']!r}") from None
    confidence = data["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
    ):
        raise ExtractionError(f"{where} confidence is not a number from 0 to 1: {confidence!r}")
    return Segment(
        date=day,
        airline=data["airline"].strip(),
        flight_number=data["flight_number"].strip(),
        origin=data["origin"].strip(),
        destination=data["destination"].strip(),
        confidence=float(confidence),
    )


def preview(text: str) -> str:
    """``text`` on one line, cut to 150 characters, quoted for an error message."""
    text = " ".join(text.split())
    return repr(text[:150] + ("..." if len(text) > 150 else ""))
