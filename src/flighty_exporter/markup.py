"""Markup parser: exact flight data that an email already carries.

Given a message's raw HTML parts and the text of its calendar files (both
prepared by the mailbox reader; no MIME work here), ``parse_markup`` returns
an extraction record of the same shape the extraction model produces, with
``model`` set to ``MARKUP_SOURCE`` and every segment at confidence 1.0, or
None. Standard library only.

**schema.org FlightReservation** (Gmail's "email markup"), in JSON-LD
(``<script type="application/ld+json">``: a single object, a list, or an
``@graph``) or microdata (``itemscope`` / ``itemtype`` / ``itemprop``). The
type may be written with or without the ``http(s)://schema.org/`` prefix.
From each reservation: ``reservationFor`` (the Flight) with ``flightNumber``,
``airline`` (or ``provider``) ``iataCode`` and ``name``,
``departureAirport.iataCode``, ``arrivalAirport.iataCode`` and
``departureTime``; ``reservationNumber`` (or ``reservationId``),
``underName`` and ``reservationStatus``.

- A digits-only flight number is prefixed with the airline's IATA code
  ("286" + "BA" -> "BA286"); a number that already starts with the code is
  left alone. Digits only and no airline code: the segment is incomplete.
- The date is the date portion of ``departureTime`` exactly as written (no
  time-zone conversion).
- A segment is complete with a date, flight number, origin and destination.
  If any reservation's segment is incomplete, the markup yields nothing and
  the message goes to the models: a partial markup record is never mixed
  with model output.
- One record per message: every distinct segment and passenger, the first
  booking reference. A reservation whose status ends in "Cancelled" (or
  "Canceled") is cancelled. All cancelled: a cancellation record. Some
  cancelled: those segments (and passengers) are dropped and the record is a
  booking.

**Calendar files** (.ics), used only when the HTML carries no
FlightReservation markup. A VEVENT is a flight when its SUMMARY, LOCATION and
DESCRIPTION together name exactly one flight number (two-character designator
plus 1 to 4 digits) and exactly one pair of distinct airport codes in an
unambiguous arrangement ("SFO-LHR", "SFO to LHR", "SFO → LHR", or exactly two
codes in parentheses), and it has a DTSTART. Other events (a meeting) are
ignored. The parser is conservative and returns nothing when:

- an event names a flight number but no single route, or has no usable
  DTSTART, or names several flight numbers;
- an event's DTSTART is in UTC (ends in Z): the local date is uncertain.

A DTSTART with a TZID, a floating local time or a DATE value gives the date
as written. ``METHOD:CANCEL`` or ``STATUS:CANCELLED`` makes an event
cancelled, combined like reservations above. A booking reference or
passenger is taken only when plainly labelled ("Booking reference: ...",
"Passenger: ...").
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any

from .extraction_client import ExtractionError, ExtractionRecord, validate_record

MARKUP_SOURCE = "markup"
MARKUP_CONFIDENCE = 1.0

FLIGHT_RESERVATION = "FlightReservation"


@dataclass(frozen=True)
class _Leg:
    date: str
    airline: str
    flight_number: str
    origin: str
    destination: str
    reference: str
    passengers: tuple[str, ...]
    cancelled: bool


def parse_markup(html_parts: tuple[str, ...], calendar_texts: tuple[str, ...]) -> ExtractionRecord | None:
    """The message's extraction record from its markup, or None."""
    reservations = [item for part in html_parts for item in _flight_reservations(part)]
    if reservations:
        legs = [_reservation_leg(item) for item in reservations]
    else:
        legs = _calendar_legs(calendar_texts)
    if not legs or any(leg is None for leg in legs):
        return None
    return _record([leg for leg in legs if leg is not None])


def _record(legs: list[_Leg]) -> ExtractionRecord | None:
    cancelled = all(leg.cancelled for leg in legs)
    if not cancelled:
        legs = [leg for leg in legs if not leg.cancelled]
    segments: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    passengers: dict[str, str] = {}
    for leg in legs:
        key = (leg.date, leg.flight_number, leg.origin, leg.destination)
        segments.setdefault(
            key,
            {"date": leg.date, "airline": leg.airline, "flight_number": leg.flight_number,
             "origin": leg.origin, "destination": leg.destination, "confidence": MARKUP_CONFIDENCE},
        )
        for name in leg.passengers:
            passengers.setdefault(" ".join(name.split()).casefold(), " ".join(name.split()))
    reference = next((leg.reference for leg in legs if leg.reference), None)
    data = {
        "email_kind": "cancellation" if cancelled else "booking",
        "booking_reference": reference,
        "passengers": list(passengers.values()),
        "segments": list(segments.values()),
    }
    try:
        return validate_record(data, MARKUP_SOURCE)
    except ExtractionError:
        return None


# -- schema.org items -------------------------------------------------------------


def _type_names(value: Any) -> list[str]:
    """``@type`` / ``itemtype`` as bare names: "http://schema.org/Flight" -> "Flight"."""
    values = value if isinstance(value, list) else [value]
    names = []
    for item in values:
        if isinstance(item, str):
            for token in item.split():
                names.append(token.rstrip("/").rsplit("/", 1)[-1])
    return names


def _is_type(item: Any, name: str) -> bool:
    return isinstance(item, dict) and name in _type_names(item.get("@type"))


def _flight_reservations(html_text: str) -> list[dict[str, Any]]:
    if FLIGHT_RESERVATION.lower() not in html_text.lower():
        return []  # the common case, decided without parsing
    collector = _Collector()
    try:
        collector.feed(html_text)
        collector.close()
    except Exception:  # noqa: BLE001 - unparseable HTML carries no usable markup
        return []
    found: list[dict[str, Any]] = []
    for block in collector.json_blocks:
        for item in _top_level_items(_load_json(block)):
            if _is_type(item, FLIGHT_RESERVATION):
                found.append(item)
    for item in _microdata_items(collector.root):
        if _is_type(item, FLIGHT_RESERVATION):
            found.append(item)
    return found


def _load_json(block: str) -> Any:
    """A JSON-LD block, tolerating comment or CDATA wrapping and HTML escaping;
    None if it is not JSON."""
    text = block.strip()
    for opener, closer in (("<!--", "-->"), ("//<![CDATA[", "//]]>"), ("<![CDATA[", "]]>")):
        if text.startswith(opener) and text.endswith(closer):
            text = text[len(opener):-len(closer)].strip()
    for candidate in (text, html.unescape(text)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _top_level_items(data: Any) -> list[Any]:
    items: list[Any] = []
    for item in data if isinstance(data, list) else [data]:
        if isinstance(item, dict) and isinstance(item.get("@graph"), list):
            items.extend(item["@graph"])
        items.append(item)
    return items


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return next((v for v in value if v not in (None, "", [], {})), None)
    return value


def _text(value: Any) -> str:
    value = _first(value)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return " ".join(str(value).split())
    return ""


def _prop(item: Any, *names: str) -> Any:
    if not isinstance(item, dict):
        return None
    for name in names:
        value = _first(item.get(name))
        if value not in (None, "", [], {}):
            return value
    return None


def _code(value: Any) -> str:
    """An ``iataCode`` of an Airline or Airport object (or a bare code string)."""
    value = _first(value)
    if isinstance(value, dict):
        return _text(value.get("iataCode")).upper()
    return _text(value).upper()


def _names(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    names: list[str] = []
    for person in values:
        if isinstance(person, dict):
            name = _text(person.get("name")) or " ".join(
                filter(None, (_text(person.get("givenName")), _text(person.get("familyName"))))
            )
        else:
            name = _text(person)
        if name:
            names.append(name)
    return names


_DIGITS_ONLY = re.compile(r"\d{1,4}[A-Z]?")
_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_CANCELLED = ("cancelled", "canceled")


def _reservation_leg(reservation: dict[str, Any]) -> _Leg | None:
    flight = _prop(reservation, "reservationFor")
    if not isinstance(flight, dict):
        return None
    airline = _prop(flight, "airline", "provider") or _prop(reservation, "provider")
    airline_code = _code(airline) if isinstance(airline, dict) else ""
    airline_name = _text(airline.get("name")) if isinstance(airline, dict) else _text(airline)

    number = re.sub(r"[\s\-]+", "", _text(_prop(flight, "flightNumber"))).upper()
    if _DIGITS_ONLY.fullmatch(number):
        if len(airline_code) != 2:
            return None
        number = airline_code + number
    # Otherwise the number already carries a designator ("BA286"): never doubled.

    origin = _code(_prop(flight, "departureAirport"))
    destination = _code(_prop(flight, "arrivalAirport"))
    day = _iso_date(_text(_prop(flight, "departureTime")))
    if not (number and origin and destination and day):
        return None
    status = _text(_prop(reservation, "reservationStatus")).lower()
    return _Leg(
        date=day,
        airline=airline_name,
        flight_number=number,
        origin=origin,
        destination=destination,
        reference=_text(_prop(reservation, "reservationNumber", "reservationId")),
        passengers=tuple(_names(reservation.get("underName"))),
        cancelled=status.endswith(_CANCELLED),
    )


def _iso_date(value: str) -> str:
    """The date portion of an ISO 8601 date-time as written, or ""."""
    match = _ISO_DATE.match(value.strip())
    if not match:
        return ""
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return ""
    return match.group(1)


# -- HTML: JSON-LD blocks and a small element tree for microdata ---------------------

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "_Node | None"):
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []
        self.parent = parent

    def text(self) -> str:
        parts: list[str] = []
        stack: list[_Node | str] = [self]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                parts.append(node)
            else:
                stack.extend(reversed(node.children))
        return " ".join("".join(parts).split())


class _Collector(HTMLParser):
    """Collects the text of each JSON-LD script and builds an element tree."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.current = self.root
        self.json_blocks: list[str] = []
        self._script: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: (value if value is not None else "") for name, value in attrs}
        if tag == "script":
            kind = values.get("type", "").split(";")[0].strip().lower()
            self._script = [] if kind == "application/ld+json" else None
        node = _Node(tag, values, self.current)
        self.current.children.append(node)
        if tag not in _VOID:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: (value if value is not None else "") for name, value in attrs}
        self.current.children.append(_Node(tag, values, self.current))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.json_blocks.append("".join(self._script))
            self._script = None
        if tag in _VOID:
            return
        node: _Node | None = self.current
        while node is not None and node is not self.root and node.tag != tag:
            node = node.parent
        if node is not None and node is not self.root:
            self.current = node.parent or self.root

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        elif self.current.tag not in ("script", "style"):
            self.current.children.append(data)


def _microdata_items(root: _Node) -> list[dict[str, Any]]:
    """Every itemscope element in the document as a JSON-LD-like dict."""
    items: list[dict[str, Any]] = []
    stack: list[_Node | str] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            continue
        if "itemscope" in node.attrs:
            items.append(_item(node))
        stack.extend(reversed(node.children))
    return items


def _item(scope: _Node) -> dict[str, Any]:
    item: dict[str, Any] = {"@type": scope.attrs.get("itemtype", "")}
    for prop_node in _properties(scope):
        value: Any = _item(prop_node) if "itemscope" in prop_node.attrs else _value(prop_node)
        for name in prop_node.attrs["itemprop"].split():
            item.setdefault(name, []).append(value)
    return item


def _properties(scope: _Node) -> list[_Node]:
    """Descendants with itemprop whose nearest itemscope ancestor is ``scope``."""
    found: list[_Node] = []
    stack: list[_Node | str] = list(reversed(scope.children))
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            continue
        if "itemprop" in node.attrs:
            found.append(node)
        if "itemscope" not in node.attrs:
            stack.extend(reversed(node.children))
    return found


def _value(node: _Node) -> str:
    attrs = node.attrs
    if node.tag == "meta" or "content" in attrs:
        return attrs.get("content", "")
    if node.tag == "time" and "datetime" in attrs:
        return attrs["datetime"]
    if node.tag in ("link", "a", "area") and "href" in attrs:
        return attrs["href"]
    if node.tag in ("img", "audio", "video", "source", "embed", "iframe") and "src" in attrs:
        return attrs["src"]
    return node.text()


# -- calendar files ---------------------------------------------------------------

_DESIGNATOR = r"(?:[A-Z]{2}|[A-Z][0-9]|[0-9][A-Z])"
_CAL_FLIGHT_NUMBER = re.compile(rf"(?<![A-Za-z0-9])({_DESIGNATOR}) ?([0-9]{{1,4}})(?![A-Za-z0-9])")
_ROUTE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{3})\s*(?:-{1,2}>?|–|—|→|✈️?|>|\bto\b)\s*([A-Z]{3})(?![A-Za-z0-9])"
)
_PAREN_CODE = re.compile(r"\(([A-Z]{3})\)")
_LABELLED_REFERENCE = re.compile(
    r"(?:booking reference|booking ref\.?|confirmation code|confirmation number|record locator|PNR)"
    r"\s*[:#]\s*([A-Z0-9]{5,8})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_LABELLED_PASSENGER = re.compile(r"^\s*passenger(?: name)?\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_DTSTART = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{6})(Z?))?$")


@dataclass
class _Property:
    name: str
    params: dict[str, str]
    value: str


def _calendar_legs(calendar_texts: tuple[str, ...]) -> list[_Leg | None]:
    """One entry per flight event; None marks a flight-like event that is not
    usable (so the whole message goes to the models)."""
    legs: list[_Leg | None] = []
    for text in calendar_texts:
        method, events = _parse_calendar(text)
        for event in events:
            legs.extend(_event_leg(event, method))
    return legs


def _parse_calendar(text: str) -> tuple[str, list[dict[str, list[_Property]]]]:
    unfolded = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))
    method = ""
    events: list[dict[str, list[_Property]]] = []
    stack: list[str] = []
    for line in unfolded.split("\n"):
        prop = _parse_line(line.rstrip("\r"))
        if prop is None:
            continue
        if prop.name == "BEGIN":
            stack.append(prop.value.upper())
            if stack[-1] == "VEVENT":
                events.append({})
        elif prop.name == "END":
            if stack:
                stack.pop()
        elif stack and stack[-1] == "VEVENT" and events:
            events[-1].setdefault(prop.name, []).append(prop)
        elif stack and stack[-1] == "VCALENDAR" and prop.name == "METHOD":
            method = prop.value.strip().upper()
    return method, events


def _parse_line(line: str) -> _Property | None:
    in_quotes = False
    for i, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    name, *raw_params = head.split(";")
    params: dict[str, str] = {}
    for param in raw_params:
        key, _, val = param.partition("=")
        params[key.strip().upper()] = val.strip().strip('"')
    return _Property(name.strip().upper(), params, value)


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _event_leg(event: dict[str, list[_Property]], method: str) -> list[_Leg | None]:
    texts = [_unescape(p.value) for name in ("SUMMARY", "LOCATION", "DESCRIPTION") for p in event.get(name, [])]
    combined = "\n".join(texts)
    numbers = {a + b for a, b in _CAL_FLIGHT_NUMBER.findall(combined)}
    if not numbers:
        return []  # not a flight (a meeting, a hotel stay): ignored
    if len(numbers) > 1:
        return [None]
    route = _route(combined)
    day = _event_date(event)
    if route is None or day is None:
        return [None]
    status = (event.get("STATUS") or [_Property("STATUS", {}, "")])[0].value.strip().upper()
    reference = _LABELLED_REFERENCE.search(combined)
    passengers = tuple(m.strip() for m in _LABELLED_PASSENGER.findall(combined) if m.strip())
    return [
        _Leg(
            date=day,
            airline="",
            flight_number=numbers.pop(),
            origin=route[0],
            destination=route[1],
            reference=reference.group(1).upper() if reference else "",
            passengers=passengers,
            cancelled=method == "CANCEL" or status == "CANCELLED",
        )
    ]


def _route(text: str) -> tuple[str, str] | None:
    pairs = {(a, b) for a, b in _ROUTE.findall(text) if a != b}
    if len(pairs) == 1:
        return pairs.pop()
    if pairs:
        return None
    codes = list(dict.fromkeys(_PAREN_CODE.findall(text)))
    if len(codes) == 2:
        return codes[0], codes[1]
    return None


def _event_date(event: dict[str, list[_Property]]) -> str | None:
    starts = event.get("DTSTART")
    if not starts:
        return None
    match = _DTSTART.match(starts[0].value.strip())
    if not match:
        return None
    year, month, day, time_part, utc = match.groups()
    if time_part and utc:
        return None  # UTC only: the local departure date is uncertain
    text = f"{year}-{month}-{day}"
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text
