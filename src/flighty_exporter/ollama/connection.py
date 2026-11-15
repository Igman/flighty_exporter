"""What every part of the model-server group shares: the HTTP opener all
requests to Ollama go through, the errors that stop a model pass, and the
request plumbing both model clients build on (``OllamaClient``).

- ``OllamaUnreachable``: the server could not be reached at all mid-pass.
- ``PreflightError``: the pass cannot start (see ``preflight``).

Errors about one message (``DecisionError``, ``ExtractionFailed``) stay with
their clients: the run continues past them.
"""

from __future__ import annotations

import http.client
import socket
import urllib.error
import urllib.request
from collections.abc import Callable

# The loopback address that answered for ``localhost``, per port.
_LOCALHOST_ANSWERED: dict[int, str] = {}


def _localhost_addresses(port: int) -> list[str]:
    """What ``localhost`` resolves to, IPv4 first (where Ollama listens by default)."""
    found = [info[4][0] for info in socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)]
    ordered = sorted(dict.fromkeys(found), key=lambda address: ":" in address)
    return ordered or ["127.0.0.1"]


def _create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):  # noqa: ANN001
    """``socket.create_connection``, except that ``localhost`` goes straight
    to the loopback address that answered last time. Each request opens a new
    connection, and on Windows ``localhost`` is ::1 first: when Ollama listens
    on 127.0.0.1 only (its default), every connection would first wait about
    two seconds for ::1 to be refused."""
    host, port = address
    if host.lower() != "localhost":
        return socket.create_connection(address, timeout, source_address)
    known = _LOCALHOST_ANSWERED.get(port)
    if known is not None:
        try:
            return socket.create_connection((known, port), timeout, source_address)
        except OSError:
            _LOCALHOST_ANSWERED.pop(port, None)  # the server has moved (restarted elsewhere): look again
    error: OSError | None = None
    for candidate in _localhost_addresses(port):
        try:
            connection = socket.create_connection((candidate, port), timeout, source_address)
        except OSError as exc:
            error = exc
            continue
        _LOCALHOST_ANSWERED[port] = candidate
        return connection
    assert error is not None
    raise error


class _OllamaConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection


class _OllamaHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_OllamaConnection, req)


# Every request to Ollama goes through this opener, which ignores proxy
# settings (HTTP_PROXY and the like): a proxy would carry email content off
# this machine even when the address itself is local. Plain-HTTP connections
# to ``localhost`` go to the loopback address that answers (see
# ``_create_connection``).
OLLAMA_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _OllamaHTTPHandler())


class OllamaUnreachable(Exception):
    """The server could not be reached at all; the run should stop."""


class PreflightError(Exception):
    """The pass cannot start; the message says why and what to do."""


class RequestTimedOut(Exception):
    """A model request got no answer within the client's timeout."""


class OllamaClient:
    """What the decision and extraction clients share: the server address and
    model, the request timeout, the hook run once before the first request,
    and sending a request body through ``OLLAMA_OPENER``."""

    def __init__(self, address: str, model: str, timeout: float):
        self.address = address.rstrip("/")
        self.model = model
        # Called once, just before the first request (the preflight check).
        self.before_first_request: Callable[[], None] | None = None
        self.timeout = timeout

    def send(self, endpoint: str, body: bytes) -> bytes:
        """POST the JSON ``body`` to ``endpoint`` and return the raw answer.

        An HTTP error status is raised as the ``urllib.error.HTTPError`` for
        the client to read; no answer within the timeout raises
        ``RequestTimedOut``; a server that cannot be reached at all raises
        ``OllamaUnreachable``."""
        if self.before_first_request is not None:
            self.before_first_request()
            self.before_first_request = None
        request = urllib.request.Request(
            self.address + endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with OLLAMA_OPENER.open(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise RequestTimedOut from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise RequestTimedOut from exc
            raise OllamaUnreachable(
                f"cannot reach Ollama at {self.address}: {reason}. Is `ollama serve` running?"
            ) from exc
