"""What every part of the model-server group shares: the HTTP opener all
requests to Ollama go through, the errors that stop a model pass, and the
request plumbing both model clients build on (``OllamaClient``).

- ``OllamaUnreachable``: the server could not be reached at all mid-pass.
- ``PreflightError``: the pass cannot start (see ``preflight``).

Errors about one message (``DecisionError``, ``ExtractionFailed``) stay with
their clients: the run continues past them.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from collections.abc import Callable

# Every request to Ollama goes through this opener, which ignores proxy
# settings (HTTP_PROXY and the like): a proxy would carry email content off
# this machine even when the address itself is local.
OLLAMA_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
