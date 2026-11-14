"""Downloading a model through the local Ollama server's own pull interface.

``pull_model`` asks the server to pull a model (``POST /api/pull`` with
``{"model": NAME, "stream": true}``) and yields its progress as data. The
server answers with JSON lines: ``{"status": "pulling manifest"}``, then for
each layer ``{"status": "pulling <digest>", "digest": ..., "total": BYTES,
"completed": BYTES}`` (``completed`` may be missing at first), then status
lines for verifying and writing, and finally ``{"status": "success"}``. An
error before the stream starts is an HTTP error status with ``{"error":
...}``; an error during the stream is a line ``{"error": ...}``.

The request goes through the same proxy-free opener as every other request
to Ollama, and the address must pass the same locality check first. Ollama
keeps a partial download, so a pull that is stopped resumes where it left
off the next time.

The server reports a model's size only once the pull starts, so
``APPROXIMATE_SIZES`` holds rough sizes for the two default models, to say
before downloading how large they are.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

from .connection import OLLAMA_OPENER, PreflightError
from .preflight import check_address, is_pulled, model_key, pulled_models, unreachable_message

SUCCESS = "success"

# Rough download sizes in bytes of the two default models.
APPROXIMATE_SIZES = {
    model_key("nimble"): 9_300_000_000,
    model_key("gemma4:12b-it-q4_K_M"): 8_000_000_000,
}
SIZE_NOT_KNOWN = "size not known until the download starts"

KEPT_PARTIAL = "Ollama keeps what was downloaded, so the next download of it resumes where this one stopped."


class PullFailed(Exception):
    """The server could not pull the model; the message says why."""


@dataclass(frozen=True)
class PullProgress:
    """One line of the server's progress: the status text and, while a layer
    downloads, its size and how much of it has arrived (bytes)."""

    status: str
    total: int | None = None
    completed: int | None = None
    digest: str | None = None

    @property
    def finished(self) -> bool:
        return self.status == SUCCESS


def approximate_size(model: str) -> int | None:
    return APPROXIMATE_SIZES.get(model_key(model))


def size_text(model: str) -> str:
    """"about 9.3 GB", or that the size is not known yet."""
    size = approximate_size(model)
    return SIZE_NOT_KNOWN if size is None else f"about {format_bytes(size)}"


def format_bytes(count: int) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if count >= scale:
            return f"{count / scale:.1f} {unit}"
    return f"{count} bytes"


def pull_model(
    address: str, model: str, *, allow_remote: bool, command: str, timeout: float = 300.0
) -> Iterator[PullProgress]:
    """Ask the Ollama server at ``address`` to pull ``model``, yielding each
    progress line until the final ``success`` one.

    Refuses a non-local address (``PreflightError``, before any request)
    unless ``allow_remote``. Raises ``PullFailed`` when the server cannot be
    reached, reports an error, sends something that is not a progress line,
    goes quiet for ``timeout`` seconds, or ends the stream before success,
    and when, after success, the server does not list the model. ``command``
    is what the unreachable message tells the user to rerun."""
    check_address(address, allow_remote)
    address = address.rstrip("/")
    request = urllib.request.Request(
        address + "/api/pull",
        data=json.dumps({"model": model, "stream": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = OLLAMA_OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise PullFailed(f"the Ollama server at {address} could not pull {model}: {_error_text(exc)}") from exc
    except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            reason = f"no answer within {timeout:g}s"
        raise PullFailed(unreachable_message(address, reason, command)) from exc
    with response:
        try:
            for raw in response:
                line = raw.strip()
                if not line:
                    continue
                progress = _progress(line, address, model)
                if progress.finished:
                    _check_listed(address, model, command)
                yield progress
                if progress.finished:
                    return
        except (TimeoutError, socket.timeout) as exc:
            raise PullFailed(
                f"the Ollama server at {address} sent no progress for {timeout:g}s while pulling {model}. "
                f"{KEPT_PARTIAL}"
            ) from exc
        except (ConnectionError, OSError) as exc:
            raise PullFailed(f"the connection to the Ollama server at {address} broke while pulling {model} ({exc}). {KEPT_PARTIAL}") from exc
    raise PullFailed(
        f"the Ollama server at {address} stopped answering before {model} finished downloading. {KEPT_PARTIAL}"
    )


def _check_listed(address: str, model: str, command: str) -> None:
    """Raise ``PullFailed`` unless the server now lists ``model``."""
    try:
        listed = is_pulled(model, pulled_models(address, command))
    except PreflightError as exc:
        raise PullFailed(str(exc)) from exc
    if not listed:
        raise PullFailed(f"the server reported {model} as pulled but does not list it.")


def _progress(line: bytes, address: str, model: str) -> PullProgress:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise PullFailed(
            f"the server at {address} answered the pull of {model} with something that is not JSON "
            f"({line[:100]!r}); is it an Ollama server?"
        ) from exc
    if not isinstance(data, dict):
        raise PullFailed(f"the server at {address} answered the pull of {model} with {str(data)[:100]}")
    if "error" in data:
        raise PullFailed(f"the Ollama server at {address} could not pull {model}: {data['error']}")

    def number(key: str) -> int | None:
        value = data.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    digest = data.get("digest")
    return PullProgress(str(data.get("status", "")), number("total"), number("completed"), digest if isinstance(digest, str) else None)


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
        message = json.loads(body).get("error")
    except (OSError, ValueError, AttributeError):
        message = None
    return str(message) if message else f"HTTP {exc.code}"
