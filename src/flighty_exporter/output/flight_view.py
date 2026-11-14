"""The flight list as the review screen shows it, and as the ``flights`` and
``decide`` commands print it: what to show, in what order, never how it is
drawn. Reads nothing; works on a ``review.FlightList``.

**Order** (``ordered``): flights with a blocking reason first, then flights
with only advisory reasons, then the rest; within each group by date, then
flight number (then route, for flights without a number).

**Rows** (``flight_row``): each flight's cells as text, in ``COLUMNS`` order:
date, flight number ("(none)" when there is none), route, airline, booking
reference, evidence, keep ("yes" or "no"), corrected ("corrected" when a
correction is saved, else empty) and its attention reasons joined with "; ".

**Filters** (``Filters``): flagged only (any attention reason), not kept
only, one year; they combine (a flight must pass each one that is set).

**Totals** (``totals``): flights, kept, not kept and needing attention, over
every flight whatever the filters show.

**Detail** (``detail_lines``): every field of one flight, its keep and
whether that is the pre-set value or the user's choice, each attention
reason marked blocking or advisory, each correction with the value
consolidation produced next to the corrected one, for a kept flight the
export would put on the manual-add list whether it is ticked as added by
hand in Flighty, and its source emails.

The lists beside the flights: ``dropped_lines`` (bookings dropped as other
passengers') and ``failure_lines`` (emails that passed the first filter but
could not be read), one line each.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import THOROUGH
from ..flights.decisions import ReviewedFlight
from ..flights.validate import is_blocking
from .export import manual_add_reason
from .openflights import OpenFlights
from .review import FlightList, dropped_line

COLUMNS = ("date", "flight", "route", "airline", "booking", "evidence", "keep", "corrected", "reasons")
NO_NUMBER = "(none)"
CORRECTED = "corrected"
CORRECTED_FIELDS = (
    ("date", "date"),
    ("flight number", "flight_number"),
    ("origin", "origin"),
    ("destination", "destination"),
)

# Attention groups, in display order.
BLOCKING = 0
ADVISORY = 1
NO_ATTENTION = 2


@dataclass(frozen=True)
class Filters:
    flagged: bool = False  # only flights needing attention
    not_kept: bool = False  # only flights not kept
    year: str | None = None  # only flights in this year ("2025")

    def matches(self, reviewed: ReviewedFlight) -> bool:
        if self.flagged and not reviewed.flight.attention:
            return False
        if self.not_kept and reviewed.keep:
            return False
        return self.year is None or reviewed.flight.segment.date[:4] == self.year

    def describe(self) -> str:
        parts = []
        if self.flagged:
            parts.append("flagged only")
        if self.not_kept:
            parts.append("not kept only")
        if self.year is not None:
            parts.append(f"year {self.year}")
        return ", ".join(parts) or "all flights"


@dataclass(frozen=True)
class Totals:
    flights: int
    kept: int
    not_kept: int
    needing_attention: int

    def line(self) -> str:
        return (
            f"Flights: {self.flights:,} · kept {self.kept:,} · not kept {self.not_kept:,} · "
            f"needing attention {self.needing_attention:,}"
        )


def attention_group(reviewed: ReviewedFlight) -> int:
    flight = reviewed.flight
    if flight.blocked:
        return BLOCKING
    return ADVISORY if flight.attention else NO_ATTENTION


def sort_key(reviewed: ReviewedFlight) -> tuple[int, str, str, str, str]:
    segment = reviewed.flight.segment
    return (attention_group(reviewed), segment.date, segment.flight_number, segment.origin, segment.destination)


def ordered(flights: list[ReviewedFlight]) -> list[ReviewedFlight]:
    return sorted(flights, key=sort_key)


def visible(flights: list[ReviewedFlight], filters: Filters) -> list[ReviewedFlight]:
    """The flights ``filters`` let through, in display order."""
    return ordered([reviewed for reviewed in flights if filters.matches(reviewed)])


def totals(flights: list[ReviewedFlight]) -> Totals:
    kept = sum(1 for reviewed in flights if reviewed.keep)
    attention = sum(1 for reviewed in flights if reviewed.flight.attention)
    return Totals(len(flights), kept, len(flights) - kept, attention)


def years(flights: list[ReviewedFlight]) -> list[str]:
    """The years the flights are in, oldest first."""
    return sorted({reviewed.flight.segment.date[:4] for reviewed in flights})


def next_year(current: str | None, present: list[str]) -> str | None:
    """The year filter after ``current`` in the cycle: each year present,
    oldest first, then all years (None)."""
    if current not in present:
        return present[0] if present and current is None else None
    index = present.index(current) + 1
    return present[index] if index < len(present) else None


def flight_row(reviewed: ReviewedFlight) -> tuple[str, ...]:
    flight = reviewed.flight
    segment = flight.segment
    return (
        segment.date,
        segment.flight_number or NO_NUMBER,
        f"{segment.origin}-{segment.destination}",
        segment.airline,
        flight.booking_reference,
        flight.evidence,
        "yes" if reviewed.keep else "no",
        CORRECTED if reviewed.decision is not None and reviewed.decision.corrected else "",
        "; ".join(flight.attention),
    )


def keep_line(reviewed: ReviewedFlight) -> str:
    keep = "yes" if reviewed.keep else "no"
    preset = "yes" if reviewed.preset_keep else "no"
    if reviewed.decision is not None and reviewed.decision.keep is not None:
        return f"{keep} (your choice; pre-set {preset})"
    return f"{keep} (pre-set)"


def corrections(reviewed: ReviewedFlight) -> list[tuple[str, str, str]]:
    """(field label, value consolidation produced, corrected value) for each
    corrected field."""
    decision = reviewed.decision
    if decision is None or not decision.corrected:
        return []
    found = []
    for label, name in CORRECTED_FIELDS:
        if getattr(decision, name) is None:
            continue
        before = getattr(reviewed.original, name) or NO_NUMBER
        after = getattr(reviewed.flight.segment, name) or NO_NUMBER
        found.append((label, before, after))
    return found


def detail_lines(reviewed: ReviewedFlight, reference: OpenFlights | None = None) -> list[str]:
    """``reference``: the reference data (``FlightList.reference``), to tell
    whether a kept flight goes on the manual-add list."""
    flight = reviewed.flight
    segment = flight.segment
    fields = [
        ("date", segment.date),
        ("flight number", segment.flight_number or NO_NUMBER),
        ("origin", segment.origin),
        ("destination", segment.destination),
        ("airline", segment.airline),
        ("booking reference", flight.booking_reference),
        ("passengers", "; ".join(flight.passengers)),
        ("evidence", flight.evidence),
        ("confidence", f"{flight.confidence:.2f}"),
        ("keep", keep_line(reviewed)),
    ]
    width = max(len(label) for label, _ in fields) + 1
    lines = [f"{label + ':':<{width}} {value}" for label, value in fields]
    if flight.attention:
        lines.append("attention:")
        lines += [f"  {reason} ({'blocking' if is_blocking(reason) else 'advisory'})" for reason in flight.attention]
    else:
        lines.append(f"{'attention:':<{width}} none")
    found = corrections(reviewed)
    if found:
        lines.append("corrections (found -> corrected):")
        label_width = max(len(label) for label, _, _ in found) + 1
        lines += [f"  {label + ':':<{label_width}} {before} -> {after}" for label, before, after in found]
    manual = manual_add_reason(reviewed, reference)
    if manual is not None:
        added = "yes" if reviewed.added else "no"
        lines.append(f"added by hand in Flighty: {added} (on the manual-add list: {manual})")
    lines.append(f"source emails: {len(flight.sources)}")
    lines += [f"  {source.message_date}  {source.subject}" for source in flight.sources]
    return lines


def dropped_lines(flight_list: FlightList) -> list[str]:
    return [dropped_line(booking) for booking in flight_list.consolidation.dropped_bookings]


def failure_lines(flight_list: FlightList) -> list[str]:
    return [
        f"{row['message_date']}  {row['sender']}  {row['subject']}: {row['reason']}" for row in flight_list.failures
    ]


DROPPED_HEADING = "Bookings dropped as other passengers'"
FAILURES_HEADING = "Emails that could not be read"
NO_FLIGHTS = "No flights found."
NO_MATCH = "No flights match these filters."


def empty_advice(thoroughness: str | None) -> list[str]:
    """Where to look when no flights were found."""
    lines = [
        NO_FLIGHTS,
        "Check the emails that could not be read: their flights must be looked up and added in Flighty by hand.",
    ]
    if thoroughness != THOROUGH:
        lines.append(
            "This was a Quick run. A Thorough run also checks a sample of the emails the first filter "
            "left out (set `thoroughness = \"thorough\"` under [run] and start fresh)."
        )
    return lines


def format_flights(flight_list: FlightList, filters: Filters, thoroughness: str | None = None) -> str:
    """What the ``flights`` command prints."""
    flights = flight_list.flights
    lines = [totals(flights).line(), "Years: " + (", ".join(years(flights)) or "none")]
    shown = visible(flights, filters)
    lines.append(f"Showing: {filters.describe()} ({len(shown):,} of {len(flights):,} flights)")
    lines.append("")
    if not flights:
        lines += empty_advice(thoroughness)
    elif not shown:
        lines.append(NO_MATCH)
    else:
        rows = [COLUMNS, *(flight_row(reviewed) for reviewed in shown)]
        widths = [max(len(r[i]) for r in rows) for i in range(len(COLUMNS))]
        lines += ["  ".join(cell.ljust(width) for cell, width in zip(r, widths)).rstrip() for r in rows]
    for heading, items in (
        (DROPPED_HEADING, dropped_lines(flight_list)),
        (FAILURES_HEADING, failure_lines(flight_list)),
    ):
        lines += ["", f"{heading}: {len(items)}", *(f"  {item}" for item in items)]
    return "\n".join(lines)
