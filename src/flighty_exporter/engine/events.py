"""What the run engine reports while it runs, as plain data.

A subscriber receives, in order: ``RunStarted``; for each stage
``StageStarted``, any number of ``StageProgress`` and ``StageCounts`` (one
of each after every message the stage handles), and ``StageFinished``;
``Notice`` and ``Problem`` where they occur; and ``RunFinished`` when the
whole run completed. A run that stops early (a problem, or a stop the caller
asked for) ends with the stopped stage's ``StageFinished`` and no
``RunFinished``.

Extract's progress can come in two runs: when message positions are missing
or stale, it first scans the mailbox's headers to locate its messages
(``StageProgress.total`` is then the number of mailbox entries), then
reports one step per selected message.

Stage summaries are the stages' own summary objects (``ClassifySummary``,
``ExtractSummary``, ``ReviewSummary``). Those in ``StageCounts`` are shallow
copies taken at that moment.

A Thorough run has two more stages between extract and review (the order is
``THOROUGH_STAGES`` in ``pipeline``): ``AUDIT``, whose summary is the
audit's ``AuditResult`` (its ``StageStarted.total`` is the number of sampled
messages to send, known once the sample is drawn; its ``StageCounts`` carry
the audit's ``extraction`` summary as it fills), and ``INCLUDE``, whose
summary is an ``engine.thorough.IncludeSummary`` (its ``StageCounts`` carry
that summary's ``extraction``). When the audit found nothing below the
threshold, or including it would exceed the budget, the include stage starts
with a total of 0 and finishes ``SKIPPED`` with a note saying why.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .report import RunReport

# The stages, in the order a Quick run performs them.
CLASSIFY = "classify"
EXTRACT = "extract"
REVIEW = "review"

# How a stage ended.
DONE = "done"
NOTHING_TO_DO = "nothing to do"  # every message already had its result: no work was needed
STOPPED = "stopped"  # ended early: a problem, a requested stop, or an interruption
SKIPPED = "skipped"  # not needed in this run; the stage's note says why

# Kinds of model-server problem.
ADDRESS = "address"  # the Ollama address is not on this machine
PREFLIGHT = "preflight"  # unreachable, too old or model missing when the stage was about to start
UNREACHABLE = "unreachable"  # the server went away mid-stage; results so far are kept

# The two stages a Thorough run adds between extract and review.
AUDIT = "audit"
INCLUDE = "include"


@dataclass(frozen=True)
class RunStarted:
    stages: tuple[str, ...]
    thoroughness: str
    mailbox: Path
    store: Path


@dataclass(frozen=True)
class StageStarted:
    stage: str
    total: int | None  # messages the stage will handle, None if not known before it reads the mailbox


@dataclass(frozen=True)
class StageProgress:
    stage: str
    done: int
    total: int
    seconds_per_message: float | None  # measured over messages that needed the model; None until one has
    seconds_remaining: float | None


@dataclass(frozen=True)
class StageCounts:
    stage: str
    summary: Any


@dataclass(frozen=True)
class StageFinished:
    stage: str
    state: str  # DONE, NOTHING_TO_DO, STOPPED or (include only) SKIPPED
    summary: Any  # None when nothing the stage did was kept (a failed preflight check rolls back)
    note: str | None = None  # something the user should know, e.g. an edited review table left as it is


@dataclass(frozen=True)
class Notice:
    """A warning that does not stop the run (e.g. a non-local address allowed by the user)."""

    stage: str | None
    message: str


@dataclass(frozen=True)
class Problem:
    """A model-server problem that stopped the run, with the existing message."""

    stage: str | None  # None for the address check, made before any stage
    kind: str  # ADDRESS, PREFLIGHT or UNREACHABLE
    message: str


@dataclass(frozen=True)
class RunFinished:
    report: RunReport


Event = RunStarted | StageStarted | StageProgress | StageCounts | StageFinished | Notice | Problem | RunFinished
