"""Sources listing: each flight review would list, with the emails it came
from and their saved text, read from the store alone (no mailbox, no model).

The flights are review's (``review.build_flight_list``: same threshold,
consolidation, passenger filtering and checks, with the saved review
decisions' corrections applied), in review table order, each shown with the
values it currently has. Each flight is a line with its date, flight number
and route, then one line per source message (date and subject, oldest
first), then that message's saved text with every line prefixed by ``    | ``
(``    |`` for a blank line). A record extracted before text was saved shows
``UNAVAILABLE`` instead.

A flight can be picked by its date and its flight number, or its route
``ORIGIN-DESTINATION`` when it has no flight number, as the flight now shows
them (after any correction), normalised as flight identities are (see
``flights.decisions.flight_key``: case, spaces and hyphens in a flight number
do not matter).

Read-only: decisions whose flight is no longer produced are left in the
store (review and export discard them).
"""

from __future__ import annotations

from ..config import Config
from ..flights.decisions import ReviewedFlight, flight_key, segment_key
from ..store import Store
from .review import build_flight_list

UNAVAILABLE = "source text unavailable (extracted before text was saved)"
NO_FLIGHT_NUMBER = "(no flight number)"
TEXT_PREFIX = "    | "


def chosen_keys(day: str, flight: str) -> set[str]:
    """The flight keys a choice of ``day`` and ``flight`` (a flight number,
    or ORIGIN-DESTINATION) can name."""
    keys = {flight_key(day, flight, "", "")}
    origin, sep, destination = flight.partition("-")
    if sep:
        keys.add(flight_key(day, "", origin, destination))
    return keys


class NoSuchFlight(Exception):
    pass


def format_sources(
    config: Config, store: Store, choice: tuple[str, str] | None = None, with_text: bool = True
) -> str:
    """The listing; ``choice`` is (date, flight number or route) to list one flight."""
    flights = build_flight_list(config, store).flights
    if choice is not None:
        wanted = chosen_keys(*choice)
        flights = [reviewed for reviewed in flights if segment_key(reviewed.flight.segment) in wanted]
        if not flights:
            raise NoSuchFlight(
                f"no flight on {choice[0]} with flight number or route {choice[1]} in the review list"
            )
    if not flights:
        return "No flights to list. Run classify and extract first."
    return "\n\n".join(_flight_block(reviewed, store, with_text) for reviewed in flights)


def _flight_block(reviewed: ReviewedFlight, store: Store, with_text: bool) -> str:
    flight = reviewed.flight
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
