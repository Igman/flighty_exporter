"""Sources listing: each flight review would list, with the emails it came
from and their saved text, read from the store alone (no mailbox, no model).

The flights are review's (``review.flight_list``: same threshold,
consolidation, passenger filtering and checks), in review table order. Each
flight is a line with its date, flight number and route, then one line per
source message (date and subject, oldest first), then that message's saved
text with every line prefixed by ``    | `` (``    |`` for a blank line). A
record extracted before text was saved shows ``UNAVAILABLE`` instead.

A flight can be picked by its date and its flight number (compared ignoring
case and spaces), or its route ``ORIGIN-DESTINATION`` when it has no flight
number.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..flights.consolidate import Flight
from ..store import Store
from .review import flight_list

UNAVAILABLE = "source text unavailable (extracted before text was saved)"
NO_FLIGHT_NUMBER = "(no flight number)"
TEXT_PREFIX = "    | "


@dataclass(frozen=True)
class FlightChoice:
    date: str
    flight: str  # a flight number, or ORIGIN-DESTINATION

    def matches(self, flight: Flight) -> bool:
        segment = flight.segment
        wanted = _compact(self.flight)
        return segment.date == self.date and wanted in (
            _compact(segment.flight_number),
            _compact(f"{segment.origin}-{segment.destination}") if not segment.flight_number else None,
        )


class NoSuchFlight(Exception):
    pass


def format_sources(config: Config, store: Store, choice: FlightChoice | None = None, with_text: bool = True) -> str:
    flights = flight_list(config, store).flights
    if choice is not None:
        flights = [flight for flight in flights if choice.matches(flight)]
        if not flights:
            raise NoSuchFlight(
                f"no flight on {choice.date} with flight number or route {choice.flight} in the review list"
            )
    if not flights:
        return "No flights to list. Run classify and extract first."
    return "\n\n".join(_flight_block(flight, store, with_text) for flight in flights)


def _flight_block(flight: Flight, store: Store, with_text: bool) -> str:
    segment = flight.segment
    lines = [f"{segment.date}  {segment.flight_number or NO_FLIGHT_NUMBER}  {segment.origin}-{segment.destination}"]
    for source in flight.sources:
        lines.append(f"  {source.message_date}  {source.subject}")
        if not with_text:
            continue
        text = store.message_text(source.message_id)
        if text is None:
            lines.append(f"    {UNAVAILABLE}")
        else:
            lines += [TEXT_PREFIX + line if line else TEXT_PREFIX.rstrip() for line in text.splitlines()]
    return "\n".join(lines)


def _compact(text: str) -> str:
    return "".join(text.split()).upper()
