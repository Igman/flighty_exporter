"""Extraction client: hand-written HTTP client for Ollama's chat endpoint.

POST {address}/api/chat with ``stream: false``, ``think: false``, the
extraction record's JSON schema as ``format``, and explicit options
(temperature 0, context length, output cap). The schema is also described in
the prompt. No SDK.

The user message is the sender, subject, date, the whole cleaned body and
then the text of each PDF attachment under a delimiter line naming the file.
To stay inside the context window the whole is capped at ``max_input_chars``
characters (body first, then PDF text); a cut input is still extracted, and
the extraction says it was truncated.

Ollama has at times ignored ``format`` (HTTP 200 with free text) and let
generation run to the output cap, so an HTTP 200 is never taken as proof:
every answer is parsed and validated here, and an answer that stopped at the
output cap is rejected. Invalid answers are retried a configured number of
times before the message is given up on.
"""

from __future__ import annotations

import json
import math
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..mail.reader import Message
from .decision import OllamaUnreachable
from .preflight import OLLAMA_OPENER

ENDPOINT = "/api/chat"

DEFAULT_MAX_INPUT_CHARS = 36000
TRUNCATION_NOTE = "[... input truncated ...]"

EMAIL_KINDS = ("booking", "change", "cancellation", "check-in", "other")

SEGMENT_FIELDS = ("date", "airline", "flight_number", "origin", "destination", "confidence")

# Kept shallow and flat: nested schemas are less reliable with local models.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email_kind": {"type": "string", "enum": list(EMAIL_KINDS)},
        "booking_reference": {"type": ["string", "null"]},
        "passengers": {"type": "array", "items": {"type": "string"}},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "airline": {"type": "string"},
                    "flight_number": {"type": "string"},
                    "origin": {"type": "string"},
                    "destination": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": list(SEGMENT_FIELDS),
            },
        },
    },
    "required": ["email_kind", "booking_reference", "passengers", "segments"],
}

SYSTEM_PROMPT = """You read one email and extract the flights it describes as JSON.

Answer with a single JSON object and nothing else, with exactly these keys:
- "email_kind": one of "booking" (a new booking, e-ticket or itinerary), "change" (a schedule change or rebooking), "cancellation" (a cancelled or refunded flight or booking), "check-in" (a check-in invitation or boarding pass), "other" (anything else, including receipts, surveys, marketing, and train, bus, hotel or car bookings).
- "booking_reference": the booking reference / record locator / PNR as a string, or null if none is given.
- "passengers": a list of the passenger names exactly as written; an empty list if no passenger is named.
- "segments": a list with one object per flight segment, each with:
  - "date": the local departure date at the origin airport, as YYYY-MM-DD;
  - "airline": the full airline name, e.g. "British Airways";
  - "flight_number": the flight number including the airline's two-character IATA prefix, e.g. "BA286";
  - "origin": the three-letter IATA code of the departure airport, e.g. "LHR";
  - "destination": the three-letter IATA code of the arrival airport, e.g. "SFO";
  - "confidence": a number from 0 to 1 for how sure you are that this segment is correct.

Only include flights (air travel). Trains, buses, ferries, hotels and car hire are not segments.
If the email describes no specific booked flight, use "email_kind": "other" and an empty "segments" list.
Do not invent details that are not in the email; use your best reading of the email text."""


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


class ExtractionFailed(ExtractionError):
    """Every attempt for one message was unusable; the run can continue."""

    def __init__(self, reason: str, attempts: int):
        super().__init__(reason)
        self.attempts = attempts


class ExtractionClient:
    def __init__(
        self,
        address: str,
        model: str,
        *,
        timeout: float = 300.0,
        num_ctx: int = 16384,
        num_predict: int = 1500,
        retries: int = 2,
        max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    ):
        self.address = address.rstrip("/")
        self.model = model
        # Called once, just before the first request (the preflight check).
        self.before_first_request: Callable[[], None] | None = None
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.retries = retries
        self.max_input_chars = max_input_chars

    def extract(self, message: Message) -> Extraction:
        """Ask for the message's record, retrying invalid or cut-off answers.

        Raises ExtractionFailed once the retries are exhausted (or on a
        timeout, which is not retried: at temperature 0 it would just recur),
        and OllamaUnreachable if the server cannot be reached at all."""
        text, truncated = email_text(message, self.max_input_chars)
        payload = build_request(self.model, text, self.num_ctx, self.num_predict)
        errors: list[str] = []
        for attempt in range(1, self.retries + 2):
            try:
                record = parse_response(self._post(payload), self.model)
            except _Timeout as exc:
                errors.append(str(exc))
                break
            except ExtractionError as exc:
                errors.append(str(exc))
                continue
            return Extraction(record, attempt, truncated)
        attempts = len(errors)
        noun = "attempt" if attempts == 1 else "attempts"
        raise ExtractionFailed(f"no valid answer after {attempts} {noun}; last: {errors[-1]}", attempts)

    def _post(self, payload: dict[str, Any]) -> Any:
        if self.before_first_request is not None:
            self.before_first_request()
            self.before_first_request = None
        request = urllib.request.Request(
            self.address + ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with OLLAMA_OPENER.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ExtractionError(f"HTTP {exc.code} from chat endpoint: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise _Timeout(f"chat request timed out after {self.timeout:g}s") from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise _Timeout(f"chat request timed out after {self.timeout:g}s") from exc
            raise OllamaUnreachable(
                f"cannot reach Ollama at {self.address}: {reason}. Is `ollama serve` running?"
            ) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"chat endpoint returned invalid JSON: {raw[:200]!r}") from exc


class _Timeout(ExtractionError):
    pass


def build_request(model: str, text: str, num_ctx: int, num_predict: int) -> dict[str, Any]:
    return {
        "model": model,
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": num_predict},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }


def email_text(message: Message, max_chars: int = DEFAULT_MAX_INPUT_CHARS) -> tuple[str, bool]:
    """The user message and whether it was cut at ``max_chars``: sender,
    subject, date, the whole cleaned body, then each PDF attachment's text."""
    text = f"From: {message.sender}\nSubject: {message.subject}\nDate: {message.date}\n\n{message.body}"
    for attachment in message.pdf_texts:
        text += f"\n\n----- PDF attachment: {attachment.filename} -----\n{attachment.text}"
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + "\n" + TRUNCATION_NOTE, True


def parse_response(response: Any, model: str) -> ExtractionRecord:
    """Validate the answer; a 200 status alone is not proof of a good answer."""
    if not isinstance(response, dict):
        raise ExtractionError(f"chat answer is not an object: {str(response)[:200]}")
    if response.get("error"):
        raise ExtractionError(f"chat endpoint error: {str(response['error'])[:200]}")
    if response.get("done_reason") == "length":
        raise ExtractionError("answer was cut off at the output cap (done_reason: length)")
    message = response.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ExtractionError(f"chat answer has no message content: {str(response)[:200]}")
    try:
        data = json.loads(_strip_code_fence(content))
    except json.JSONDecodeError:
        raise ExtractionError(f"answer is not JSON: {_preview(content)}") from None
    return validate_record(data, model)


def validate_record(data: Any, model: str) -> ExtractionRecord:
    """Check the record's structure and types. Airline and airport codes are
    not checked against reference tables here."""
    if not isinstance(data, dict):
        raise ExtractionError(f"answer is not a JSON object: {_preview(json.dumps(data))}")
    missing = [key for key in SCHEMA["required"] if key not in data]
    if missing:
        raise ExtractionError(f"answer is missing {', '.join(missing)}: {_preview(json.dumps(data))}")

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


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1) if match else text


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return repr(text[:150] + ("..." if len(text) > 150 else ""))
