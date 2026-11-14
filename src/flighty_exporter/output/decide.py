"""Making review decisions one flight at a time, as the review screen and the
``decide`` command do: toggle or set keep, correct the date, flight number,
origin or destination from typed values, or clear a flight's decision.

``Reviewer`` holds the flight list (``review.build_flight_list``) and, for
each decision, saves it in the store at once and then checks only that
flight again (``flights.decisions.apply_decisions`` on the one flight, with
the reference data the list was checked against), so a list of thousands of
flights is not rebuilt for every key press. The rules are the shared ones
(``flights.decisions``): a keep value is saved as an override only when it
differs from the pre-set keep (``keep_override``), and a correction keeps
only the fields that differ from the flight as consolidation produced it
(``typed_corrections``, which also rejects an unreal date or an empty
airport, saving nothing). A correction leaves a keep override as it is.

A flight is found by the values it now shows (``find``): its date and
flight number, or date and route for a flight without a number, normalised
as flight identities are, so a corrected flight is found under its
corrected values.

The caller records the session as active (``engine.session.touch``) after a
write; this module does not reach the engine.
"""

from __future__ import annotations

from ..config import Config
from ..flights.decisions import (
    ReviewedFlight, apply_decisions, flight_key, keep_override, segment_key, typed_corrections,
)
from ..store import Store
from .review import FlightList, build_flight_list


class FlightNotFound(Exception):
    pass


class Reviewer:
    def __init__(self, config: Config, flight_list: FlightList) -> None:
        self.config = config
        self.flight_list = flight_list
        self._index = {reviewed.key: number for number, reviewed in enumerate(flight_list.flights)}

    @classmethod
    def load(cls, config: Config, store: Store) -> Reviewer:
        return cls(config, build_flight_list(config, store))

    @property
    def flights(self) -> list[ReviewedFlight]:
        return self.flight_list.flights

    def get(self, key: str) -> ReviewedFlight:
        return self.flights[self._index[key]]

    def find(self, day: str, flight_number: str, origin: str = "", destination: str = "") -> ReviewedFlight:
        """The flight now showing these values (a flight number, or with none
        the route)."""
        wanted = flight_key(day, flight_number, origin, destination)
        found = [reviewed for reviewed in self.flights if segment_key(reviewed.flight.segment) == wanted]
        what = flight_number or f"{origin}-{destination}"
        if not found:
            raise FlightNotFound(f"no flight on {day.strip()} with flight number or route {what} in the review list")
        if len(found) > 1:
            raise FlightNotFound(
                f"more than one flight shows {day.strip()} {what}; correct one of them in the review table first"
            )
        return found[0]

    def set_keep(self, store: Store, key: str, keep: bool) -> ReviewedFlight:
        """Keep (or do not keep) the flight; an override is saved only when
        ``keep`` differs from its pre-set keep."""
        store.save_keep(key, keep_override(keep, self.get(key).preset_keep))
        return self._recheck(store, key)

    def toggle_keep(self, store: Store, key: str) -> ReviewedFlight:
        return self.set_keep(store, key, not self.get(key).keep)

    def correct(
        self, store: Store, key: str, day: str, flight_number: str, origin: str, destination: str
    ) -> ReviewedFlight:
        """Save the corrections the typed values make; raises
        ``flights.decisions.InvalidCorrection`` (saving nothing) for an
        unreal date or an empty airport."""
        found = typed_corrections(self.get(key).original, day, flight_number, origin, destination)
        store.save_corrections(key, found.date, found.flight_number, found.origin, found.destination)
        return self._recheck(store, key)

    def reset(self, store: Store, key: str) -> ReviewedFlight:
        """Clear the flight's keep override and corrections (its manual-add
        tick stays)."""
        store.clear_decision(key)
        return self._recheck(store, key)

    def _recheck(self, store: Store, key: str) -> ReviewedFlight:
        number = self._index[key]
        decision = store.review_decision(key)
        flight = self.flight_list.consolidation.flights[number]
        (reviewed,), _ = apply_decisions(
            [flight],
            {} if decision is None else {key: decision},
            self.flight_list.reference,
            self.config.review_min_confidence,
        )
        self.flights[number] = reviewed
        return reviewed
