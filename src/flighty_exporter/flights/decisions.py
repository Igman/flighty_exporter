"""Review decisions: the user's keep choices and corrections, applied to the
consolidated flights. A pure step, no I/O; the store keeps the decisions.

**Identity.** A decision belongs to a flight, named by ``flight_key`` from the
flight as consolidation produced it, before any correction: its date and
normalised flight number ("2025-05-12 ZZ1234"), or, with no flight number,
its date, origin and destination ("2025-08-01 OSL-LIS"). Consolidation merges
flights on exactly these fields, so no two flights share a key. A corrected
flight keeps its original key, so a later decision for it still finds it.

**A decision** holds an optional keep override (yes or no), optional
corrections to the date, flight number, origin and destination (None: not
corrected), and the manual-add tick (the user has entered the flight in
Flighty by hand), which does not change the flight.

**Applying** (``apply_decisions``): corrections replace the segment's fields;
the corrected flights are then checked (``validate.check_flights``) so their
attention reasons and pre-set keep are recomputed from the corrected values;
finally a keep override replaces the pre-set keep. Decisions whose key names
no flight are returned so the caller can discard them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from .consolidate import Flight
from .records import Segment
from .validate import CodeLookup, check_flights, normalise_airport, normalise_flight_number


@dataclass(frozen=True)
class ReviewDecision:
    keep: bool | None = None  # None: the pre-set keep stands
    date: str | None = None  # corrections; None: not corrected
    flight_number: str | None = None  # "" is a correction to "no flight number"
    origin: str | None = None
    destination: str | None = None
    added: bool = False  # ticked on the manual-add list

    @property
    def corrected(self) -> bool:
        return any(v is not None for v in (self.date, self.flight_number, self.origin, self.destination))


@dataclass(frozen=True)
class ReviewedFlight:
    key: str  # the identity of the flight as consolidation produced it
    flight: Flight  # corrections applied, then checked
    original: Segment  # the segment as consolidation produced it
    keep: bool  # the keep override if there is one, else the pre-set keep
    decision: ReviewDecision | None = None

    @property
    def preset_keep(self) -> bool:
        return not self.flight.blocked

    @property
    def added(self) -> bool:
        return self.decision is not None and self.decision.added


def flight_key(day: str, flight_number: str, origin: str, destination: str) -> str:
    number = normalise_flight_number(flight_number)
    if number:
        return f"{day.strip()} {number}"
    return f"{day.strip()} {normalise_airport(origin)}-{normalise_airport(destination)}"


def segment_key(segment: Segment) -> str:
    return flight_key(segment.date, segment.flight_number, segment.origin, segment.destination)


def is_date(value: str) -> bool:
    """A real ``YYYY-MM-DD`` date, the only date a correction may hold."""
    if len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def correct(segment: Segment, decision: ReviewDecision | None) -> Segment:
    """``segment`` with the decision's corrections (normalised) in place."""
    if decision is None or not decision.corrected:
        return segment
    changes: dict[str, str] = {}
    if decision.date is not None and is_date(decision.date):
        changes["date"] = decision.date
    if decision.flight_number is not None:
        changes["flight_number"] = normalise_flight_number(decision.flight_number)
    if decision.origin is not None:
        changes["origin"] = normalise_airport(decision.origin)
    if decision.destination is not None:
        changes["destination"] = normalise_airport(decision.destination)
    return replace(segment, **changes)


def apply_decisions(
    flights: list[Flight],
    decisions: dict[str, ReviewDecision],
    reference: CodeLookup | None,
    min_confidence: float,
) -> tuple[list[ReviewedFlight], set[str]]:
    """The consolidated (unchecked) ``flights`` with corrections applied,
    checked, and keep overridden; and the keys of ``decisions`` that name no
    flight."""
    keys = [segment_key(flight.segment) for flight in flights]
    corrected = [
        replace(flight, segment=correct(flight.segment, decisions.get(key))) for flight, key in zip(flights, keys)
    ]
    checked = check_flights(corrected, reference, min_confidence)
    reviewed = []
    for key, original, flight in zip(keys, flights, checked):
        decision = decisions.get(key)
        keep = decision.keep if decision is not None and decision.keep is not None else not flight.blocked
        reviewed.append(ReviewedFlight(key, flight, original.segment, keep, decision))
    return reviewed, set(decisions) - set(keys)


def resolve_key(
    decisions: dict[str, ReviewDecision], day: str, flight_number: str, origin: str, destination: str
) -> str:
    """The key of the flight now showing these values: a flight whose
    corrections produce them, else the key the values themselves make."""
    wanted = flight_key(day, flight_number, origin, destination)
    if wanted in decisions and not decisions[wanted].corrected:
        return wanted
    for key, decision in decisions.items():
        if not decision.corrected:
            continue
        day0, number0, rest = _split_key(key)
        segment = Segment(day0, "", number0, rest[0], rest[1], 0.0)
        if segment_key(correct(segment, decision)) == wanted:
            return key
    return wanted


def _split_key(key: str) -> tuple[str, str, tuple[str, str]]:
    """(date, flight number, (origin, destination)) as far as the key holds
    them: a normalised flight number never contains a hyphen, a route always
    does."""
    day, _, rest = key.partition(" ")
    origin, sep, destination = rest.partition("-")
    if sep:
        return day, "", (origin, destination)
    return day, rest, ("", "")
