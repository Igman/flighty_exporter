"""Privacy safeguard and preflight check for the model commands.

Shared by classify, extract and audit (review and export make no model
requests and never run either):

- ``check_address``, before anything is sent to the configured Ollama
  address (preflight requests included): the address must be on this machine,
  because the passes send email content to it. Local means the host is
  ``localhost``, an IPv4 loopback address (127.0.0.0/8), ``::1``, or a name
  that resolves only to loopback addresses. The literal checks come first; a
  name is resolved only when it is not literally local, and a name that does
  not resolve counts as not local. A non-local address is refused unless
  ``[ollama] allow_remote = true`` or ``--allow-remote`` is given, in which
  case a warning is printed on every run.
- ``check_server``, just before a pass's first model request (a pass that
  turns out to need none never runs it): the server answers
  ``GET /api/version`` with version 0.35.0 or later (the release that added
  the decision endpoint), and ``GET /api/tags`` lists the model the pass
  uses. These requests use a short timeout, not the model timeout.

Both raise ``PreflightError`` with a message that says what to do.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
from collections.abc import Callable
from typing import Any

from .connection import OLLAMA_OPENER, PreflightError

MIN_VERSION = (0, 35, 0)
# Preflight requests only ask the server about itself: a few seconds is
# plenty, and an address that does not answer fails fast.
PREFLIGHT_TIMEOUT = 5.0

ALLOW_REMOTE_HINT = "set `allow_remote = true` under [ollama] in the config file, or pass --allow-remote"

_VERSION = re.compile(r"^[vV]?(\d+)\.(\d+)(?:\.(\d+))?")


# -- privacy: is the address on this machine? ------------------------------------


def check_address(address: str, allow_remote: bool) -> str | None:
    """Refuse a non-local address unless ``allow_remote``; return the warning
    to print when the override lets a non-local address through."""
    host = urllib.parse.urlsplit(address).hostname or ""
    reason = _not_local_reason(host)
    if reason is None:
        return None
    if allow_remote:
        return (
            f"warning: email content is being sent to the Ollama server at {address}, which is not on this "
            "machine (allow_remote or --allow-remote is set)."
        )
    raise PreflightError(
        f"refusing to use the Ollama server at {address}: {reason}, and email content would leave this machine. "
        f"Point [ollama] address at a server on this machine (e.g. http://localhost:11434), or, to send email "
        f"content to {address} anyway, {ALLOW_REMOTE_HINT}."
    )


def _not_local_reason(host: str) -> str | None:
    """None when ``host`` is on this machine, else why it is not."""
    host = host.lower()
    if host == "localhost":
        return None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return None if _is_loopback(literal) else f"{host} is not a loopback address"
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return f"the host name {host} does not resolve and cannot be confirmed to be on this machine"
    addresses = sorted({info[4][0] for info in infos})
    if addresses and all(_is_loopback(ipaddress.ip_address(a.split("%")[0])) for a in addresses):
        return None
    return f"{host} resolves to {', '.join(addresses) or 'nothing'}, which is not only loopback addresses"


def _is_loopback(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # IPv4: 127.0.0.0/8; IPv6: ::1 only.
    return ip.is_loopback


# -- preflight: is the server ready for this pass? ---------------------------------


def check_server(
    address: str,
    model: str,
    setting: str,
    command: str,
    timeout: float = PREFLIGHT_TIMEOUT,
    warn: Callable[[str], None] | None = None,
) -> None:
    """Check that Ollama at ``address`` is reachable, new enough and has
    ``model`` (configured as ``setting``, e.g. "[models] decision")."""
    found = server_version(address, command, timeout)
    problem = version_problem(address, found, command)
    if problem is not None:
        raise PreflightError(problem)
    if not version_readable(found) and warn is not None:
        warn(unreadable_version_warning(address, found))
    if not is_pulled(model, pulled_models(address, command, timeout)):
        raise PreflightError(model_missing_message(address, model, setting, command))


# The pieces ``check_server`` is made of, also used to report the server's
# readiness as a whole (guided setup).


def server_version(address: str, command: str, timeout: float = PREFLIGHT_TIMEOUT) -> Any:
    """The ``version`` the server reports at ``GET /api/version`` (None if it
    reports none); ``PreflightError`` when the server cannot be reached or
    does not answer like Ollama."""
    version_answer = _get(address, "/api/version", command, timeout)
    return version_answer.get("version") if isinstance(version_answer, dict) else None


def version_problem(address: str, found: Any, command: str) -> str | None:
    """The message for a server older than ``MIN_VERSION``; None when it is
    new enough or its version cannot be read (which only warns)."""
    parsed = parse_version(found) if isinstance(found, str) else None
    if parsed is None or parsed >= MIN_VERSION:
        return None
    return (
        f"the Ollama server at {address} is version {found}, but flighty-exporter needs Ollama {_required()} "
        f"or later (the release that added the decision endpoint). Update Ollama "
        f"(https://ollama.com/download), then rerun {command}."
    )


def version_readable(found: Any) -> bool:
    return isinstance(found, str) and parse_version(found) is not None


def unreadable_version_warning(address: str, found: Any) -> str:
    return (
        f"warning: could not read the Ollama version from {address} (got {found!r}); continuing, but "
        f"flighty-exporter needs Ollama {_required()} or later."
    )


def pulled_models(address: str, command: str, timeout: float = PREFLIGHT_TIMEOUT) -> set[str]:
    """The models the server lists at ``GET /api/tags``, as comparable keys
    (see ``is_pulled``)."""
    tags = _get(address, "/api/tags", command, timeout)
    listed = tags.get("models") if isinstance(tags, dict) else None
    if not isinstance(listed, list):
        raise PreflightError(
            f"the server at {address} did not list its models at /api/tags (got {str(tags)[:200]}); is it an "
            f"Ollama server?"
        )
    return {
        _model_key(entry.get(key))
        for entry in listed
        if isinstance(entry, dict)
        for key in ("name", "model")
        if isinstance(entry.get(key), str)
    }


def is_pulled(model: str, pulled: set[str]) -> bool:
    return _model_key(model) in pulled


def model_missing_message(address: str, model: str, setting: str, command: str) -> str:
    return (
        f"the model {model} ({setting}) is not pulled on the Ollama server at {address}. "
        f"Pull it with `ollama pull {model}`, then rerun {command}."
    )


def unreachable_message(address: str, reason: object, command: str) -> str:
    return (
        f"the Ollama server at {address} is not reachable ({reason}). Start it with `ollama serve`, or open "
        f"the Ollama app, then rerun {command}."
    )


def model_key(name: str) -> str:
    """How model names compare (see ``_model_key``), for keying a table by model."""
    return _model_key(name)


def _required() -> str:
    return ".".join(map(str, MIN_VERSION))


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``"0.40.16"`` -> (0, 40, 16); tolerates a leading ``v`` and a suffix
    such as ``-rc1``; None when there is no version number."""
    match = _VERSION.match(text.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def _model_key(name: str) -> str:
    """Model names compare case-insensitively, and a name without a tag is
    the ``latest`` tag. A colon before a slash is a registry port, not a tag."""
    name = name.strip().lower()
    _, colon, tag = name.rpartition(":")
    if not colon or "/" in tag:
        name += ":latest"
    return name


def _get(address: str, path: str, command: str, timeout: float) -> Any:
    try:
        with OLLAMA_OPENER.open(address + path, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise PreflightError(
            f"the server at {address} answered {path} with HTTP {exc.code}; is it an Ollama server? Check "
            "[ollama] address."
        ) from exc
    except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            reason = f"no answer within {timeout:g}s"
        raise PreflightError(unreachable_message(address, reason, command)) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreflightError(
            f"the server at {address} answered {path} with something that is not JSON ({raw[:100]!r}); is it "
            "an Ollama server? Check [ollama] address."
        ) from exc
