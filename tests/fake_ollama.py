"""A fake Ollama server, replaced at the HTTP boundary.

The only fake in the test suite. It serves Ollama's endpoints on a local port
with scripted answers keyed by message (the email subject), and records every
request it receives so tests can assert on what the tool sent.

Endpoints are registered in ``FakeOllama.routes`` ((method, path) ->
handler): the decision endpoint, the chat endpoint, and the version and tags
endpoints the preflight check asks. By default the fake reports a recent
version and lists both default models; ``version``, ``models`` and the
``*_status`` / ``*_raw_body`` fields script other answers.
"""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


@dataclass
class Decision:
    """A scripted answer from the decision model for one message."""

    gate: float = 0.01
    email_type: str = "other"
    # Return this HTTP status instead of an answer (simulates a server error).
    status: int = 200
    # Return this raw body instead of a well-formed answer.
    raw_body: str | None = None
    # The error message sent with a non-200 status.
    error: str = "scripted failure"


# The real decision server's answers to an over-long prompt. It rejects rather
# than truncates.
def too_many_tokens_error(tokens: int) -> str:
    return f"prompt 0 has {tokens} tokens; expected 1\u20138192 (input is never truncated)"


TOO_LARGE_ERROR = "text and schema must not exceed 64 KiB"


VALID_RECORD: dict[str, Any] = {
    "email_kind": "other",
    "booking_reference": None,
    "passengers": [],
    "segments": [],
}


@dataclass
class Chat:
    """A scripted answer from the extraction model for one chat request.

    By default the answer content is ``record`` serialised as JSON. Set
    ``content`` to send that text instead (free text, broken JSON),
    ``done_reason`` to e.g. "length" for an answer cut off at the output cap,
    ``status`` for an HTTP error, or ``raw_body`` for a whole raw response.
    """

    record: Any = field(default_factory=lambda: dict(VALID_RECORD))
    content: str | None = None
    done_reason: str = "stop"
    status: int = 200
    raw_body: str | None = None


# What the real server lists for the two default models.
DEFAULT_MODELS = ("nimble:latest", "gemma4:12b-it-q4_K_M")
PREFLIGHT_PATHS = ("/api/version", "/api/tags")
PASS_PATHS = ("/v1/systemone", "/api/chat")


@dataclass
class RecordedRequest:
    method: str
    path: str
    body: Any


@dataclass
class FakeOllama:
    # Scripted decision answers keyed by the email subject sent in the state.
    decisions: dict[str, Decision] = field(default_factory=dict)
    default_decision: Decision = field(default_factory=Decision)
    # Scripted chat answers keyed by the email subject in the user message. A
    # list is a sequence: one answer per request, the last one repeating.
    chats: dict[str, Chat | list[Chat]] = field(default_factory=dict)
    default_chat: Chat = field(default_factory=Chat)
    requests: list[RecordedRequest] = field(default_factory=list)
    # Optional hook called with each request before answering (e.g. to block).
    on_request: Callable[[RecordedRequest], None] | None = None
    # Optional prompt limits for the decision endpoint, enforced like the real
    # server: over ``decision_token_limit`` estimated tokens (one token per
    # ``chars_per_token`` characters of state and questions) is HTTP 400; over
    # ``decision_byte_limit`` UTF-8 bytes is HTTP 413.
    decision_token_limit: int | None = None
    chars_per_token: float = 3.0
    decision_byte_limit: int | None = None
    # Preflight answers: GET /api/version reports ``version`` (None leaves the
    # field out); GET /api/tags lists ``models`` by name. A non-200 status
    # answers with that HTTP error; a raw body is sent as is.
    version: str | None = "0.40.16"
    version_status: int = 200
    version_raw_body: str | None = None
    models: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    tags_status: int = 200

    def __post_init__(self) -> None:
        self.routes: dict[tuple[str, str], Callable[[Any], tuple[int, str]]] = {
            ("POST", "/v1/systemone"): self._systemone,
            ("POST", "/api/chat"): self._chat,
            ("GET", "/api/version"): self._version,
            ("GET", "/api/tags"): self._tags,
        }
        self._chat_counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeOllama":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = raw.decode("utf-8", "replace")
                request = RecordedRequest(method, self.path, body)
                with fake._lock:
                    fake.requests.append(request)
                if fake.on_request is not None:
                    fake.on_request(request)
                route = fake.routes.get((method, self.path))
                if route is None:
                    status, payload = 404, json.dumps({"error": "not found"})
                else:
                    status, payload = route(body)
                data = payload.encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()

    # -- inspection helpers -------------------------------------------------

    def decision_requests(self) -> list[Any]:
        with self._lock:
            return [r.body for r in self.requests if r.path == "/v1/systemone"]

    def classified_subjects(self) -> list[str]:
        return [body["state"].get("subject", "") for body in self.decision_requests()]

    def chat_requests(self) -> list[Any]:
        with self._lock:
            return [r.body for r in self.requests if r.path == "/api/chat"]

    def extracted_subjects(self) -> list[str]:
        """The email subject of every chat request, in order (with repeats)."""
        return [chat_subject(body) for body in self.chat_requests()]

    def paths(self) -> list[str]:
        """The path of every request received, in order."""
        with self._lock:
            return [r.path for r in self.requests]

    def preflight_requests(self) -> list[str]:
        return [path for path in self.paths() if path in PREFLIGHT_PATHS]

    def pass_requests(self) -> list[str]:
        """Requests that carried email content (decision or chat)."""
        return [path for path in self.paths() if path in PASS_PATHS]

    # -- endpoints ------------------------------------------------------------

    def _version(self, body: Any) -> tuple[int, str]:
        if self.version_status != 200:
            return self.version_status, json.dumps({"error": "scripted failure"})
        if self.version_raw_body is not None:
            return 200, self.version_raw_body
        return 200, json.dumps({} if self.version is None else {"version": self.version})

    def _tags(self, body: Any) -> tuple[int, str]:
        if self.tags_status != 200:
            return self.tags_status, json.dumps({"error": "scripted failure"})
        return 200, json.dumps(
            {
                "models": [
                    {"name": name, "model": name, "size": 1, "digest": "0" * 64, "details": {}}
                    for name in self.models
                ]
            }
        )

    def _chat(self, body: Any) -> tuple[int, str]:
        subject = chat_subject(body)
        script = self.chats.get(subject, self.default_chat)
        with self._lock:
            n = self._chat_counts.get(subject, 0)
            self._chat_counts[subject] = n + 1
        answer = script[min(n, len(script) - 1)] if isinstance(script, list) else script
        if answer.status != 200:
            return answer.status, json.dumps({"error": "scripted failure"})
        if answer.raw_body is not None:
            return 200, answer.raw_body
        content = answer.content if answer.content is not None else json.dumps(answer.record)
        return 200, json.dumps(
            {
                "model": body.get("model"),
                "created_at": "2026-01-01T00:00:00Z",
                "message": {"role": "assistant", "content": content},
                "done": True,
                "done_reason": answer.done_reason,
            }
        )

    def _systemone(self, body: Any) -> tuple[int, str]:
        subject = body.get("state", {}).get("subject", "") if isinstance(body, dict) else ""
        prompt = prompt_text(body)
        if self.decision_byte_limit is not None and len(prompt.encode("utf-8")) > self.decision_byte_limit:
            return 413, json.dumps({"error": TOO_LARGE_ERROR}, ensure_ascii=False)
        tokens = math.ceil(len(prompt) / self.chars_per_token)
        if self.decision_token_limit is not None and tokens > self.decision_token_limit:
            return 400, json.dumps({"error": too_many_tokens_error(tokens)}, ensure_ascii=False)
        decision = self.decisions.get(subject, self.default_decision)
        if decision.status != 200:
            return decision.status, json.dumps({"error": decision.error}, ensure_ascii=False)
        if decision.raw_body is not None:
            return 200, decision.raw_body
        questions = body["questions"]
        answers: dict[str, Any] = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": decision.gate}
            elif question["type"] == "choice":
                labels = list(question["criteria"])
                chosen = decision.email_type
                rest = (1 - 0.9) / max(len(labels) - 1, 1)
                answers[name] = {
                    "type": "choice",
                    "choice": chosen,
                    "probabilities": {label: (0.9 if label == chosen else rest) for label in labels},
                    "confidence": 0.9,
                }
        return 200, json.dumps({"model": body.get("model"), "answers": answers, "usage": {}})


def prompt_text(body: Any) -> str:
    """The text of a decision request the server counts: state and questions."""
    if not isinstance(body, dict):
        return ""
    parts = [str(v) for v in (body.get("state") or {}).values()]
    for question in (body.get("questions") or {}).values():
        parts.append(str(question.get("instructions", "")))
        parts.extend(str(v) for v in (question.get("criteria") or {}).values())
    return "\n".join(parts)


def decision_body(body: Any) -> str:
    """The email body a decision request carried."""
    return str(((body or {}).get("state") or {}).get("body", ""))


def chat_user_text(body: Any) -> str:
    """The user message of a chat request (the email text)."""
    for message in reversed((body or {}).get("messages") or []):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


def chat_subject(body: Any) -> str:
    """The Subject line of the email in a chat request's user message."""
    if not isinstance(body, dict):
        return ""
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            match = re.search(r"^Subject: (.*)$", message.get("content", ""), re.MULTILINE)
            return match.group(1).strip() if match else ""
    return ""
