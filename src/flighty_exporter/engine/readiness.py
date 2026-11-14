"""Is the model server ready for a run? Guided setup's check, as data.

``check_readiness`` runs the checks the model passes run before their first
request (see ``ollama.preflight``), all at once and without stopping at a
missing model: is the address on this machine, does the server answer, is it
new enough, and is each of the two configured models pulled. The messages
are the passes' own. ``format_readiness`` turns the result into lines for
the ``setup --check`` and ``pull-models`` commands; the application's setup
screen draws the same data.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..ollama.connection import PreflightError
from ..ollama.preflight import (
    PREFLIGHT_TIMEOUT,
    check_address,
    is_pulled,
    model_missing_message,
    pulled_models,
    server_version,
    unreadable_version_warning,
    version_problem,
    version_readable,
)
from ..ollama.pull import approximate_size, size_text
from .events import ADDRESS
from .models import DECISION_SETTING, EXTRACTION_SETTING

SERVER = "server"  # not reachable, not answering like Ollama, or too old


@dataclass(frozen=True)
class ModelReadiness:
    setting: str  # "[models] decision" or "[models] extraction"
    name: str
    present: bool
    approximate_size: int | None  # bytes; None when not known before the download starts
    size_text: str
    message: str | None  # the passes' "not pulled" message when missing


@dataclass(frozen=True)
class Readiness:
    address: str
    local: bool  # the address is on this machine
    problem: str | None  # the message of the check that stopped the others, if any
    problem_kind: str | None  # ADDRESS or SERVER
    warnings: tuple[str, ...]  # a non-local address let through by allow_remote; an unreadable version
    reachable: bool | None  # None: not tried (address refused)
    version: str | None
    version_ok: bool | None  # None: not known
    models: tuple[ModelReadiness, ...]  # empty when the server could not be asked

    @property
    def missing(self) -> tuple[ModelReadiness, ...]:
        return tuple(model for model in self.models if not model.present)

    @property
    def ready(self) -> bool:
        return self.problem is None and bool(self.models) and not self.missing


def check_readiness(config: Config, *, allow_remote: bool = False, command: str) -> Readiness:
    """Check the model server named in ``config``. ``allow_remote`` is the
    command line's override of the locality check (the config's own setting
    is always honoured); ``command`` is what the messages tell the user to
    rerun. Never raises for a server problem: it is in the result."""
    address = config.ollama_address
    warnings: list[str] = []
    try:
        warning = check_address(address, config.ollama_allow_remote or allow_remote)
    except PreflightError as exc:
        return Readiness(address, False, str(exc), ADDRESS, (), None, None, None, ())
    if warning:
        warnings.append(warning)
    local = warning is None
    timeout = min(PREFLIGHT_TIMEOUT, config.ollama_timeout_seconds)
    try:
        found = server_version(address, command, timeout)
    except PreflightError as exc:
        return Readiness(address, local, str(exc), SERVER, tuple(warnings), False, None, None, ())
    version = found if isinstance(found, str) else None
    problem = version_problem(address, found, command)
    if problem is not None:
        return Readiness(address, local, problem, SERVER, tuple(warnings), True, version, False, ())
    if not version_readable(found):
        warnings.append(unreadable_version_warning(address, found))
    try:
        pulled = pulled_models(address, command, timeout)
    except PreflightError as exc:
        return Readiness(
            address, local, str(exc), SERVER, tuple(warnings), True, version, version_readable(found) or None, ()
        )
    models = []
    for setting, name in ((DECISION_SETTING, config.decision_model), (EXTRACTION_SETTING, config.extraction_model)):
        present = is_pulled(name, pulled)
        models.append(
            ModelReadiness(
                setting,
                name,
                present,
                approximate_size(name),
                size_text(name),
                None if present else model_missing_message(address, name, setting, command),
            )
        )
    return Readiness(
        address, local, None, None, tuple(warnings), True, version, True if version_readable(found) else None,
        tuple(models),
    )


def format_readiness(readiness: Readiness) -> list[str]:
    r = readiness
    lines = [f"Model server {r.address}:"]
    lines.append(f"  on this machine: {'yes' if r.local else 'no'}")
    lines += [f"  {warning}" for warning in r.warnings]
    if r.problem_kind == ADDRESS:
        return lines + [f"error: {r.problem}"]
    lines.append(f"  reachable:       {'yes' if r.reachable else 'no'}")
    if r.reachable:
        if r.version_ok is None:
            lines.append(f"  version:         {r.version or 'not reported'} (cannot tell whether it is new enough)")
        else:
            lines.append(f"  version:         {r.version} ({'new enough' if r.version_ok else 'too old'})")
    for model in r.models:
        state = "pulled" if model.present else f"missing ({model.size_text})"
        lines.append(f"  {model.setting}: {model.name}: {state}")
    if r.problem is not None:
        lines.append(f"error: {r.problem}")
    for model in r.missing:
        lines.append(f"error: {model.message}")
    if r.missing:
        lines.append("Or download the missing models with `flighty-exporter pull-models --yes`.")
    if r.ready:
        lines.append("Ready: the model server has both models.")
    return lines
