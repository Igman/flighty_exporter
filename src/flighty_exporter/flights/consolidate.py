"""Consolidator: a pure step from per-email extraction records to one flight
per segment flown (or booked). No I/O.

Rules, applied to the records in chronological order of message date (ties
broken by message identifier):

- **Grouping.** Records sharing a booking reference (compared ignoring case
  and all whitespace) form one booking. A record with no booking reference
  joins a booking that already holds a matching segment (same date and flight
  number); otherwise it forms its own booking. Records with no segments never
  create a booking or a flight (a zero-segment cancellation still cancels a
  booking it names).
- **Cancellation.** A cancellation record removes every segment the booking
  has accumulated so far, listed or not. A later booking or change record
  under the same reference re-establishes segments (a rebooking).
- **Replacement, per leg.** A segment in a later booking or change record
  replaces earlier segments of the booking with the same origin and
  destination. Legs the later record does not mention are kept. A segment
  identical to an earlier one (same leg, date and flight number) changes
  nothing but adds a source.
- **Check-in and other records** never replace or remove. Their segments add
  a source to a matching segment of the booking, or create the flight when
  nothing matches.
- **Evidence.** "confirmed" when a source of the flight is a check-in record
  or was classified as check-in/boarding pass or post-flight; otherwise
  "booked only". Evidence never filters.
- **Passengers.** When names are configured, a booking whose records name
  at least one passenger (across all of them) and none matching a configured
  name is dropped with all its flights, before duplicates are merged, so a
  flight also found in the user's own booking stays, sourced from that
  booking only. A booking naming several passengers is kept when any one
  matches (see ``passengers``). A kept booking that names no passenger is
  flagged for attention on each of its flights (unless a flight merges with
  one from a booking that does name someone). A flight's passengers are
  those of its bookings. With no names configured nothing is dropped or
  flagged.
- **Duplicates across bookings** are merged on date plus flight number, or
  date plus origin and destination when the flight number is empty. A merged
  flight keeps the fields of its most recent source, the strongest evidence,
  the highest confidence and the union of sources, passengers and booking
  references.

Codes are compared as ``validate.normalise_record`` leaves them; review
normalises every record before consolidating.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ollama.extraction import ExtractionRecord, Segment
from .passengers import matches_any, names_someone

CONFIRMED = "confirmed"
BOOKED_ONLY = "booked only"

# Attention reasons (a flight may carry several).
NO_PASSENGER_NAMED = "no passenger named"

# Classifier email types that show the passenger got as far as the airport.
CONFIRMING_EMAIL_TYPES = frozenset({"check-in or boarding pass", "post-flight"})
CONFIRMING_EMAIL_KINDS = frozenset({"check-in"})
REPLACING_EMAIL_KINDS = frozenset({"booking", "change"})


@dataclass(frozen=True)
class SourceRecord:
    """One message's extraction record with the metadata consolidation needs."""

    message_id: str
    sent_at: str  # sortable timestamp (UTC ISO), "" if the date is unknown
    message_date: str  # as shown in the review table
    subject: str
    email_type: str  # the decision model's email type, "" if unknown
    record: ExtractionRecord
    input_truncated: bool = False  # the extraction input was cut at the character budget

    @property
    def email_kind(self) -> str:
        return self.record.email_kind

    @property
    def order(self) -> tuple[str, str]:
        return (self.sent_at, self.message_id)

    @property
    def confirms(self) -> bool:
        return self.email_kind in CONFIRMING_EMAIL_KINDS or self.email_type in CONFIRMING_EMAIL_TYPES


@dataclass(frozen=True)
class Flight:
    segment: Segment  # fields from the most recent source
    booking_reference: str  # "; "-joined when merged across bookings
    passengers: tuple[str, ...]
    confidence: float
    evidence: str
    sources: tuple[SourceRecord, ...]  # chronological; the last is the most recent
    attention: tuple[str, ...] = ()  # why the user should look at this flight; empty if no reason
    blocked: bool = False  # a reason is serious enough to pre-set keep to no (set by ``validate``)

    @property
    def latest(self) -> SourceRecord:
        return self.sources[-1]


@dataclass(frozen=True)
class DroppedBooking:
    """A booking left out because none of its passengers is the user."""

    booking_reference: str  # as first written, "" if none
    passengers: tuple[str, ...]
    flights: int
    first_date: str


@dataclass(frozen=True)
class Consolidation:
    flights: list[Flight]
    bookings_cancelled: int
    bookings_replaced: int
    dropped_bookings: list[DroppedBooking] = field(default_factory=list)


def consolidate(records: list[SourceRecord], passenger_names: tuple[str, ...] = ()) -> Consolidation:
    """Consolidate ``records`` into flights. ``passenger_names`` are the user's
    name variants; empty turns passenger filtering off."""
    bookings: list[_Booking] = []
    by_reference: dict[str, _Booking] = {}

    for source in sorted(records, key=lambda s: s.order):
        record = source.record
        reference = normalise_reference(record.booking_reference)
        if reference:
            booking = by_reference.get(reference)
        else:
            booking = _booking_with_matching_segment(bookings, record.segments)
        if booking is None:
            if not record.segments:
                continue
            booking = _Booking()
            bookings.append(booking)
            if reference:
                by_reference[reference] = booking
        booking.apply(source)

    kept: list[_Booking] = []
    dropped: list[DroppedBooking] = []
    for booking in bookings:
        if passenger_names and booking.entries and not booking.is_for(passenger_names):
            dropped.append(booking.dropped())
        else:
            kept.append(booking)
    dropped.sort(key=lambda d: (d.first_date, normalise_reference(d.booking_reference)))

    entries = []
    for booking in kept:
        passengers = tuple(booking.passengers())
        for entry in booking.entries:
            entry.booking_passengers = passengers
            entries.append(entry)
    return Consolidation(
        flights=_merge_duplicates(entries, flag_unnamed=bool(passenger_names)),
        bookings_cancelled=sum(1 for b in bookings if b.cancelled),
        bookings_replaced=sum(1 for b in kept if b.replaced),
        dropped_bookings=dropped,
    )


def normalise_reference(reference: str | None) -> str:
    return "".join((reference or "").split()).upper()


def _normalise_flight_number(flight_number: str) -> str:
    return "".join(flight_number.split()).replace("-", "").upper()


def _leg(segment: Segment) -> tuple[str, str]:
    return (segment.origin.strip().upper(), segment.destination.strip().upper())


def _same_flight(a: Segment, b: Segment) -> bool:
    """Same date and flight number; with no flight number, same date and leg."""
    if a.date != b.date:
        return False
    number_a, number_b = _normalise_flight_number(a.flight_number), _normalise_flight_number(b.flight_number)
    if number_a or number_b:
        return number_a == number_b
    return _leg(a) == _leg(b)


@dataclass
class _Entry:
    """One segment of a booking with every message that mentions it."""

    segment: Segment
    sources: list[SourceRecord]
    confidence: float
    booking_passengers: tuple[str, ...] = ()  # every passenger its booking names

    def add(self, segment: Segment, source: SourceRecord) -> None:
        self.segment = segment
        self.confidence = max(self.confidence, segment.confidence)
        if all(s.message_id != source.message_id for s in self.sources):
            self.sources.append(source)


@dataclass
class _Booking:
    entries: list[_Entry] = field(default_factory=list)
    cancelled: bool = False
    replaced: bool = False
    sources: list[SourceRecord] = field(default_factory=list)  # every record applied, in order

    def apply(self, source: SourceRecord) -> None:
        self.sources.append(source)
        kind = source.email_kind
        if kind == "cancellation":
            if self.entries:
                self.cancelled = True
            self.entries = []
        elif kind in REPLACING_EMAIL_KINDS:
            self._replace_legs(source)
        else:
            for segment in source.record.segments:
                self._add(segment, source)

    def _replace_legs(self, source: SourceRecord) -> None:
        segments = source.record.segments
        legs = {_leg(segment) for segment in segments}
        kept: list[_Entry] = []
        for entry in self.entries:
            if _leg(entry.segment) in legs and not any(_same_flight(entry.segment, s) for s in segments):
                self.replaced = True
                continue
            kept.append(entry)
        self.entries = kept
        for segment in segments:
            self._add(segment, source)

    def _add(self, segment: Segment, source: SourceRecord) -> None:
        for entry in self.entries:
            if _same_flight(entry.segment, segment):
                entry.add(segment, source)
                return
        self.entries.append(_Entry(segment, [source], segment.confidence))

    def passengers(self) -> list[str]:
        """Every passenger named by any record of the booking, first spelling kept."""
        names: dict[str, str] = {}
        for source in self.sources:
            for name in source.record.passengers:
                if names_someone(name):
                    names.setdefault(" ".join(name.split()).casefold(), " ".join(name.split()))
        return list(names.values())

    def is_for(self, configured: tuple[str, ...]) -> bool:
        """Kept when it names no passenger, or any passenger matches a configured name."""
        passengers = self.passengers()
        return not passengers or any(matches_any(name, configured) for name in passengers)

    def dropped(self) -> DroppedBooking:
        reference = next((s.record.booking_reference for s in self.sources if s.record.booking_reference), "")
        return DroppedBooking(
            booking_reference=reference or "",
            passengers=tuple(self.passengers()),
            flights=len(self.entries),
            first_date=min(entry.segment.date for entry in self.entries),
        )

    def has_flight(self, segment: Segment) -> bool:
        return any(
            _normalise_flight_number(segment.flight_number) and _same_flight(entry.segment, segment)
            for entry in self.entries
        )


def _booking_with_matching_segment(bookings: list[_Booking], segments: tuple[Segment, ...]) -> _Booking | None:
    """The most recently created booking holding a segment with the same date
    and flight number as one of ``segments``."""
    for booking in reversed(bookings):
        if any(booking.has_flight(segment) for segment in segments):
            return booking
    return None


def _merge_duplicates(entries: list[_Entry], flag_unnamed: bool) -> list[Flight]:
    groups: dict[tuple[str, ...], list[_Entry]] = {}
    for entry in entries:
        segment = entry.segment
        number = _normalise_flight_number(segment.flight_number)
        key = (segment.date, number) if number else (segment.date, "", *_leg(segment))
        groups.setdefault(key, []).append(entry)
    flights = [_flight(group, flag_unnamed) for group in groups.values()]
    flights.sort(
        key=lambda f: (
            f.segment.date,
            _normalise_flight_number(f.segment.flight_number),
            *_leg(f.segment),
            f.latest.order,
        )
    )
    return flights


def _flight(group: list[_Entry], flag_unnamed: bool) -> Flight:
    sources_by_id: dict[str, SourceRecord] = {}
    for entry in group:
        for source in entry.sources:
            sources_by_id.setdefault(source.message_id, source)
    sources = tuple(sorted(sources_by_id.values(), key=lambda s: s.order))
    newest = max(group, key=lambda e: e.sources[-1].order)

    passengers: dict[str, str] = {}
    for entry in group:
        for name in entry.booking_passengers:
            passengers.setdefault(name.casefold(), name)
    references: dict[str, str] = {}
    for source in sources:
        reference = source.record.booking_reference
        if reference:
            references.setdefault(normalise_reference(reference), reference)

    return Flight(
        segment=newest.segment,
        booking_reference="; ".join(references.values()),
        passengers=tuple(passengers.values()),
        confidence=max(entry.confidence for entry in group),
        evidence=CONFIRMED if any(source.confirms for source in sources) else BOOKED_ONLY,
        sources=sources,
        attention=_attention(passengers, flag_unnamed),
    )


def _attention(passengers: dict[str, str], flag_unnamed: bool) -> tuple[str, ...]:
    reasons = []
    if flag_unnamed and not passengers:
        reasons.append(NO_PASSENGER_NAMED)
    return tuple(reasons)
