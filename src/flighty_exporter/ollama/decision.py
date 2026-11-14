"""Decision client: hand-written HTTP client for Ollama's decision endpoint.

POST {address}/v1/systemone with the message as ``state`` and two named
questions: a yes/no ``noul`` gate and a ``choice`` of email type. No SDK.

The state is the sender, subject, attachment filenames and the start of the
cleaned body, cut to ``max_body_chars`` characters so that the whole prompt
stays under the server's limits (8,192 tokens and 64 KiB). The server rejects
rather than truncates, and there is no tokenizer endpoint, so the character
budget is an estimate: when the server still answers that the prompt is too
long (HTTP 400 "... tokens; expected 1–8192 ..." or HTTP 413), the request is
repeated with the body halved, up to ``MAX_HALVINGS`` times. PDF text is never
sent to this pass.
"""

from __future__ import annotations

import json
import math
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..flights.records import Decision
from ..mail.reader import Message
from .preflight import OLLAMA_OPENER

ENDPOINT = "/v1/systemone"

DEFAULT_MAX_BODY_CHARS = 12000
MAX_HALVINGS = 3
# The decision server's answer when the prompt is over its token limit.
_TOO_MANY_TOKENS = re.compile(r"tokens; expected 1\S{1,6}8192")

GATE_QUESTION = "flight_details"
TYPE_QUESTION = "email_type"

GATE_INSTRUCTIONS = (
    "Does this email contain flight details for a specific booking, such as a "
    "flight number, travel date or route for a reservation made for a passenger? "
    "Answer no for promotions, fare sales, newsletters and emails that only "
    "mention travel in general."
)
GATE_CRITERIA = {
    "true": "The email gives details of one or more specific booked flights.",
    "false": "The email has no details of a specific booked flight.",
}

TYPE_INSTRUCTIONS = "Which kind of email is this?"
EMAIL_TYPES: dict[str, str] = {
    "booking confirmation": "Confirms a new flight booking, e-ticket or itinerary.",
    "change": "Reports a schedule change, rebooking or other change to an existing flight booking.",
    "cancellation": "Reports that a flight or flight booking was cancelled or refunded.",
    "check-in or boarding pass": "Invites the passenger to check in, or contains a boarding pass.",
    "post-flight": "Sent after a flight: a receipt, survey, miles credited or a lost-baggage notice.",
    "marketing": "A promotion, fare sale, newsletter or advertisement.",
    "other": "Anything else.",
}


class DecisionError(Exception):
    """The request for one message failed; the run can continue."""


class PromptTooLarge(DecisionError):
    """The server rejected the prompt as over its token or size limit."""


class OllamaUnreachable(Exception):
    """The server could not be reached at all; the run should stop."""


class DecisionClient:
    def __init__(
        self,
        address: str,
        model: str,
        timeout: float = 300.0,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    ):
        self.address = address.rstrip("/")
        self.model = model
        # Called once, just before the first request (the preflight check).
        self.before_first_request: Callable[[], None] | None = None
        self.timeout = timeout
        self.max_body_chars = max_body_chars

    def decide(self, message: Message) -> Decision:
        """Classify one message, halving the body while the server says the
        prompt is too large."""
        budget = self.max_body_chars
        for halvings in range(MAX_HALVINGS + 1):
            try:
                response = self._post(build_request(self.model, message, budget))
            except PromptTooLarge as exc:
                if halvings == MAX_HALVINGS:
                    raise DecisionError(
                        f"prompt still too large with the body cut to {budget} characters "
                        f"after {MAX_HALVINGS} halvings: {exc}"
                    ) from exc
                budget //= 2
                continue
            return parse_response(response, self.model)
        raise AssertionError("unreachable")

    def _post(self, payload: dict[str, Any]) -> Any:
        if self.before_first_request is not None:
            self.before_first_request()
            self.before_first_request = None
        request = urllib.request.Request(
            self.address + ENDPOINT,
            # Non-ASCII text as UTF-8, not \u escapes: smaller against the 64 KiB limit.
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with OLLAMA_OPENER.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 413 or (exc.code == 400 and _TOO_MANY_TOKENS.search(detail)):
                raise PromptTooLarge(f"HTTP {exc.code} from decision endpoint: {detail}") from exc
            raise DecisionError(f"HTTP {exc.code} from decision endpoint: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise DecisionError(f"decision request timed out after {self.timeout:g}s") from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise DecisionError(f"decision request timed out after {self.timeout:g}s") from exc
            raise OllamaUnreachable(
                f"cannot reach Ollama at {self.address}: {reason}. Is `ollama serve` running?"
            ) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DecisionError(f"decision endpoint returned invalid JSON: {raw[:200]!r}") from exc


def build_request(model: str, message: Message, max_body_chars: int = DEFAULT_MAX_BODY_CHARS) -> dict[str, Any]:
    state = {"sender": message.sender, "subject": message.subject}
    if message.attachment_filenames:
        state["attachments"] = ", ".join(message.attachment_filenames)
    state["body"] = message.body[:max_body_chars]
    return {
        "model": model,
        "state": state,
        "questions": {
            GATE_QUESTION: {
                "type": "noul",
                "instructions": GATE_INSTRUCTIONS,
                "criteria": GATE_CRITERIA,
            },
            TYPE_QUESTION: {
                "type": "choice",
                "instructions": TYPE_INSTRUCTIONS,
                "criteria": EMAIL_TYPES,
            },
        },
    }


def parse_response(response: Any, requested_model: str) -> Decision:
    """Validate the answer; a 200 status alone is not proof of a good answer."""
    try:
        answers = response["answers"]
        gate = answers[GATE_QUESTION]["noul"]
        choice = answers[TYPE_QUESTION]
        email_type = choice["choice"]
        probabilities = choice.get("probabilities") or {}
        confidence = choice.get("confidence")
    except (KeyError, TypeError) as exc:
        raise DecisionError(f"decision answer is missing {exc}: {str(response)[:200]}") from exc
    if not _is_probability(gate):
        raise DecisionError(f"gate answer is not a probability: {gate!r}")
    if email_type not in EMAIL_TYPES:
        raise DecisionError(f"email type answer is not one of the labels: {email_type!r}")
    if not isinstance(probabilities, dict) or not all(
        label in EMAIL_TYPES and _is_probability(p) for label, p in probabilities.items()
    ):
        raise DecisionError(f"email type probabilities are invalid: {probabilities!r}")
    if confidence is not None and not _is_probability(confidence):
        raise DecisionError(f"email type confidence is invalid: {confidence!r}")
    return Decision(
        gate_probability=float(gate),
        email_type=email_type,
        type_probabilities={label: float(p) for label, p in probabilities.items()},
        type_confidence=None if confidence is None else float(confidence),
        # The configured name, so a model change in config invalidates results.
        model=requested_model,
    )


def _is_probability(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )
