"""Validation: pure steps that normalise extracted segments and check each
consolidated flight for plausibility. No I/O.

``normalise_record`` runs on every extraction record before consolidation, so
that the same flight written two ways ("ba 286", "BA-286") merges: airport
codes are stripped and uppercased; flight numbers lose all whitespace and
hyphens and are uppercased.

``check_flights`` runs on the consolidated flights. Each failed check adds a
reason to the flight's attention reasons:

Blocking (the row is pre-set to not keep):

1. ``unknown airport XXX``: the origin or destination is not an airport code
   in the reference data (one reason per unknown code).
2. ``unknown airline XX``: a well-formed flight number whose two-character
   prefix matches no airline in the reference data. Not checked for a
   malformed flight number, which has no reliable prefix.
3. ``malformed flight number``: present but not a two-character airline
   designator (letters or digits, at least one letter), then 1 to 4 digits,
   then an optional single letter.
4. ``origin and destination are the same``.
5. ``implausible date``: before 1990-01-01, or more than 400 days after the
   date of the flight's most recent dated source message.
6. ``low confidence``: the flight's confidence, after halving once for every
   reason from checks 1 to 5, is below the cut-off.

Advisory (the row stays pre-set to keep, but needs attention):

7. ``no flight number`` (export puts it on the manual-add list).
8. ``email was truncated before extraction``: a source message's extraction
   input was cut at the character budget.
9. Consolidation's own reasons (``no passenger named``), kept as they are.

Blocking reasons come first, in the order above. Checks 1 and 2 need the
reference data; with ``reference`` None they are skipped and the others still
run.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, timedelta
from typing import Protocol

from .consolidate import Flight
from .extraction_client import ExtractionRecord, Segment

UNKNOWN_AIRPORT = "unknown airport"
UNKNOWN_AIRLINE = "unknown airline"
MALFORMED_FLIGHT_NUMBER = "malformed flight number"
SAME_ORIGIN_AND_DESTINATION = "origin and destination are the same"
IMPLAUSIBLE_DATE = "implausible date"
LOW_CONFIDENCE = "low confidence"
NO_FLIGHT_NUMBER = "no flight number"
INPUT_TRUNCATED = "email was truncated before extraction"

# Reason kinds in the order they are listed on a row and in the summary.
# "no passenger named" (from consolidation) comes last.
REASON_KINDS = (
    UNKNOWN_AIRPORT,
    UNKNOWN_AIRLINE,
    MALFORMED_FLIGHT_NUMBER,
    SAME_ORIGIN_AND_DESTINATION,
    IMPLAUSIBLE_DATE,
    LOW_CONFIDENCE,
    NO_FLIGHT_NUMBER,
    INPUT_TRUNCATED,
)

EARLIEST_PLAUSIBLE_DATE = date(1990, 1, 1)
MAX_DAYS_AFTER_MESSAGE = 400
CONFIDENCE_PENALTY = 0.5

_FLIGHT_NUMBER = re.compile(r"(?:[A-Z][A-Z0-9]|[0-9][A-Z])[0-9]{1,4}[A-Z]?")
# Whitespace and hyphens, including the typographic dashes and minus sign.
_STRIPPED_FROM_FLIGHT_NUMBERS = re.compile(r"[\s\-\u2010-\u2015\u2212]+")


class CodeLookup(Protocol):
    """The reference data: ``openflights.OpenFlights`` satisfies it."""

    def airport(self, code: str) -> object | None: ...

    def airline(self, flight_number: str) -> object | None: ...


def normalise_flight_number(flight_number: str) -> str:
    return _STRIPPED_FROM_FLIGHT_NUMBERS.sub("", flight_number).upper()


def normalise_airport(code: str) -> str:
    return "".join(code.split()).upper()


def normalise_record(record: ExtractionRecord) -> ExtractionRecord:
    return replace(record, segments=tuple(_normalise_segment(s) for s in record.segments))


def _normalise_segment(segment: Segment) -> Segment:
    return replace(
        segment,
        flight_number=normalise_flight_number(segment.flight_number),
        origin=normalise_airport(segment.origin),
        destination=normalise_airport(segment.destination),
    )


def check_flights(
    flights: list[Flight], reference: CodeLookup | None, min_confidence: float
) -> list[Flight]:
    """The flights with confidence lowered and reasons added (see the module
    docstring); ``blocked`` is set when any reason is blocking."""
    return [_check(flight, reference, min_confidence) for flight in flights]


def reason_kind(reason: str) -> str:
    """The reason without the code it names, e.g. "unknown airport"."""
    for kind in (UNKNOWN_AIRPORT, UNKNOWN_AIRLINE):
        if reason.startswith(kind + " "):
            return kind
    return reason


def _check(flight: Flight, reference: CodeLookup | None, min_confidence: float) -> Flight:
    segment = flight.segment
    number = segment.flight_number
    well_formed = bool(_FLIGHT_NUMBER.fullmatch(number))

    blocking: list[str] = []
    if reference is not None:
        for code in dict.fromkeys((segment.origin, segment.destination)):
            if reference.airport(code) is None:
                blocking.append(f"{UNKNOWN_AIRPORT} {code or '(empty)'}")
        if well_formed and reference.airline(number) is None:
            blocking.append(f"{UNKNOWN_AIRLINE} {number[:2]}")
    if number and not well_formed:
        blocking.append(MALFORMED_FLIGHT_NUMBER)
    if segment.origin and segment.origin == segment.destination:
        blocking.append(SAME_ORIGIN_AND_DESTINATION)
    if _implausible_date(flight):
        blocking.append(IMPLAUSIBLE_DATE)

    confidence = flight.confidence * CONFIDENCE_PENALTY ** len(blocking)
    if confidence < min_confidence:
        blocking.append(LOW_CONFIDENCE)

    advisory: list[str] = []
    if not number:
        advisory.append(NO_FLIGHT_NUMBER)
    if any(source.input_truncated for source in flight.sources):
        advisory.append(INPUT_TRUNCATED)

    return replace(
        flight,
        confidence=confidence,
        attention=(*blocking, *advisory, *flight.attention),
        blocked=bool(blocking),
    )


def _implausible_date(flight: Flight) -> bool:
    day = date.fromisoformat(flight.segment.date)  # validated at extraction
    if day < EARLIEST_PLAUSIBLE_DATE:
        return True
    sent = [s.sent_at[:10] for s in flight.sources if s.sent_at]
    if not sent:
        return False
    latest = date.fromisoformat(max(sent))
    return day > latest + timedelta(days=MAX_DAYS_AFTER_MESSAGE)
