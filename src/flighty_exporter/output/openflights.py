"""Import file format: the OpenFlights backup CSV that Flighty accepts.

This is the one module that knows the import file's format; ``export`` decides
which flights go into it. To target another importer, replace this module
behind the same three calls: ``OpenFlights.load``, ``unmatched`` and
``write_backup``.

The file reproduces what OpenFlights' own backup export emits (the layout a
locally generated file was accepted by Flighty with, see the import spike):

- named ``openflights-backup-YYYY-MM-DD-HHMM.csv`` (UTC time of the run);
- UTF-8 byte-order mark, the 19-column header, CRLF between rows and no line
  break after the last row;
- rows newest first;
- Date as ``YYYY-MM-DD`` (no time is known);
- From / To as IATA codes (ICAO when an airport has no IATA code);
- Airline as OpenFlights' own name for the carrier;
- Distance (miles) and Duration (``HH:MM``) from OpenFlights' formulas;
- Note always ``""``; no other field quoted;
- From_OID / To_OID / Airline_OID from OpenFlights' data files.

Reference data: OpenFlights' ``airports.dat`` and ``airlines.dat`` (Open
Database License). ``review`` also uses ``load``, ``airport`` and ``airline``
to check extracted codes, so a code that passes review is one export can
write. They are not bundled: they are downloaded on first use into
a per-user cache directory and reused thereafter. The download sends nothing
about the user.
"""

from __future__ import annotations

import csv
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, NamedTuple

DATA_FILES = ("airports.dat", "airlines.dat")
DOWNLOAD_TIMEOUT_SECONDS = 60

HEADER = (
    "Date,From,To,Flight_Number,Airline,Distance,Duration,Seat,Seat_Type,Class,Reason,"
    "Plane,Registration,Trip,Note,From_OID,To_OID,Airline_OID,Plane_OID"
)
FILE_NAME_FORMAT = "openflights-backup-%Y-%m-%d-%H%M.csv"

_NULLS = ("", "\\N")


class ReferenceDataError(Exception):
    pass


class Flight(NamedTuple):
    """A flight to write: normalised codes, flight number with airline prefix."""

    date: date
    origin: str
    destination: str
    flight_number: str


@dataclass(frozen=True)
class Airport:
    apid: int
    iata: str
    icao: str
    lat: float
    lon: float

    @property
    def code(self) -> str:
        """OpenFlights format_apcode2(): IATA, else ICAO."""
        return self.iata if self.iata not in ("", "\\N", "N/A") else self.icao


@dataclass(frozen=True)
class Airline:
    alid: int
    name: str
    active: bool


class OpenFlights:
    def __init__(self, airports: dict[str, Airport], airlines: dict[str, list[Airline]]):
        self._airports = airports
        self._airlines = airlines

    @classmethod
    def load(cls, data_dir: Path, data_url: str) -> "OpenFlights":
        """Read the reference data from ``data_dir``, downloading any file
        that is missing. Raises ReferenceDataError if that fails."""
        paths = {name: _ensure_file(data_dir, name, data_url) for name in DATA_FILES}
        return cls(_read_airports(paths["airports.dat"]), _read_airlines(paths["airlines.dat"]))

    def airport(self, code: str) -> Airport | None:
        return self._airports.get(code.upper())

    def airline(self, flight_number: str) -> Airline | None:
        """The carrier for the flight number's two-character IATA prefix:
        active airlines first, then alphabetical by name."""
        candidates = self._airlines.get(flight_number[:2].upper(), [])
        active = [a for a in candidates if a.active]
        pool = sorted(active or candidates, key=lambda a: a.name)
        return pool[0] if pool else None

    def unmatched(self, flight: Flight) -> list[str]:
        """Why this flight cannot be written to the import file (empty if it can)."""
        reasons = []
        for code in (flight.origin, flight.destination):
            if self.airport(code) is None:
                reasons.append(f"airport {code} not in OpenFlights data")
        if self.airline(flight.flight_number) is None:
            reasons.append(f"no OpenFlights airline for prefix {flight.flight_number[:2]}")
        return reasons

    def backup_row(self, flight: Flight) -> str:
        origin, destination = self.airport(flight.origin), self.airport(flight.destination)
        airline = self.airline(flight.flight_number)
        if origin is None or destination is None or airline is None:
            raise ValueError(f"flight cannot be written: {self.unmatched(flight)}")
        distance = distance_miles(origin, destination)
        fields = [
            flight.date.isoformat(), origin.code, destination.code, flight.flight_number, airline.name,
            str(distance), duration(distance), "", "", "", "", "", "", "", '""',
            str(origin.apid), str(destination.apid), str(airline.alid), "",
        ]
        return ",".join(fields)

    def write_backup(self, flights: Iterable[Flight], output_dir: Path, now: datetime) -> Path:
        """Write the import file into ``output_dir`` (atomically) and return its
        path. ``now`` is the UTC time of the run, used in the file name."""
        ordered = sorted(flights, key=lambda f: (f.flight_number, f.origin, f.destination))
        ordered.sort(key=lambda f: f.date, reverse=True)  # newest first; stable for ties
        content = "﻿" + HEADER + "\r\n" + "\r\n".join(self.backup_row(f) for f in ordered)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / now.strftime(FILE_NAME_FORMAT)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(content.encode("utf-8"))
        tmp.replace(path)
        return path


def distance_miles(a: Airport, b: Airport) -> int:
    """OpenFlights gcPointDistance(): floor(acos(...) * 6371.2 * 0.621)."""
    if a.lon == b.lon and a.lat == b.lat:
        return 0
    rad = math.pi / 180.0
    lon1, lat1, lon2, lat2 = a.lon * rad, a.lat * rad, b.lon * rad, b.lat * rad
    cosine = math.sin(lat1) * math.sin(lat2) + math.cos(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    dist = math.acos(max(-1.0, min(1.0, cosine)))
    if dist < 0:
        dist += math.pi
    return math.floor(dist * 6371.2 * 0.621)


def duration(distance: int) -> str:
    """OpenFlights gcDuration(): 30 minutes plus a 500 mph cruise, as HH:MM."""
    minutes = math.floor(30 + (distance / 500) * 60)
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def _ensure_file(data_dir: Path, name: str, data_url: str) -> Path:
    path = data_dir / name
    if path.is_file():
        return path
    url = data_url.rstrip("/") + "/" + name
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            content = response.read()
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(name + ".tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ReferenceDataError(
            f"OpenFlights data file {name} is not in {data_dir} and could not be downloaded "
            f"from {url}: {reason}. Download it by hand into that directory, or set "
            "[export] data_dir / data_url in the config file."
        ) from exc
    return path


def _rows(path: Path) -> Iterable[tuple[int, list[str]]]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            for number, row in enumerate(csv.reader(handle), 1):
                if row:
                    yield number, row
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ReferenceDataError(f"cannot read OpenFlights data file {path}: {exc}") from exc


def _read_airports(path: Path) -> dict[str, Airport]:
    by_code: dict[str, Airport] = {}
    for number, row in _rows(path):
        # apid, name, city, country, iata, icao, lat, lon, ...
        try:
            airport = Airport(int(row[0]), row[4], row[5], float(row[6]), float(row[7]))
        except (IndexError, ValueError) as exc:
            raise ReferenceDataError(f"{path} line {number} is not an OpenFlights airport row") from exc
        for code in (airport.iata, airport.icao):
            if code not in _NULLS:
                by_code.setdefault(code.upper(), airport)
    return by_code


def _read_airlines(path: Path) -> dict[str, list[Airline]]:
    by_iata: dict[str, list[Airline]] = {}
    for number, row in _rows(path):
        # alid, name, alias, iata, icao, callsign, country, active
        try:
            alid, name, iata, active = int(row[0]), row[1], row[3], row[7]
        except (IndexError, ValueError) as exc:
            raise ReferenceDataError(f"{path} line {number} is not an OpenFlights airline row") from exc
        if alid < 1 or iata in (*_NULLS, "-"):
            continue
        by_iata.setdefault(iata.upper(), []).append(Airline(alid, name, active == "Y"))
    return by_iata
