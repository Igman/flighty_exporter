"""Run the whole pipeline for a session: classify, extract, then review.

``run_pipeline`` reuses the passes as they are (``run_classify``,
``run_extract``, ``write_review``) and reports through events (see
``events``). It is resumable by construction, because every pass skips the
work already in the store; whether a stage had anything to do is derived
from the store and the mailbox, never from a stored flag.

- The two models are never needed at once: classify finishes before the
  extraction model is first contacted. Each model stage checks the server
  lazily, just before its first model request, with the store's writes
  held until the check passes (see ``models``).
- ``should_stop`` is consulted between messages and between stages. A stop
  ends the current stage after the message in progress; every result is
  committed as it arrives, so nothing is left half-written.
- A model-server problem (non-local address, server unreachable, too old,
  model missing) is reported as a ``Problem`` event with the existing message
  and ends the run; the engine never asks anything.
- ``KeyboardInterrupt`` ends the current stage (reported as stopped) and is
  re-raised for the caller.

Thoroughness: Quick runs no audit. Thorough is not available yet.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from ..config import QUICK, Config
from ..ollama.connection import OllamaUnreachable, PreflightError
from ..ollama.decision import DecisionClient
from ..ollama.extraction import ExtractionClient
from ..output.review import ReviewExists, ReviewSummary, write_review, write_review_without_table
from ..progress import Progress
from ..stages.classify import ClassifySummary, run_classify
from ..stages.extract import ExtractSummary, run_extract
from ..store import Store
from .events import (
    ADDRESS,
    CLASSIFY,
    DONE,
    EXTRACT,
    NOTHING_TO_DO,
    PREFLIGHT,
    REVIEW,
    STOPPED,
    UNREACHABLE,
    Event,
    Notice,
    Problem,
    RunFinished,
    RunStarted,
    StageCounts,
    StageFinished,
    StageProgress,
    StageStarted,
)
from .models import DECISION_SETTING, EXTRACTION_SETTING, address_warning, decision_client, extraction_client, pass_store
from .report import REVIEW_TABLE_LEFT, RunReport
from . import session

QUICK_STAGES = (CLASSIFY, EXTRACT, REVIEW)
THOROUGH_NOT_AVAILABLE = "thorough mode is not available yet"

# How a run ended.
FINISHED = "finished"
STOPPED_ON_REQUEST = "stopped"
STOPPED_BY_PROBLEM = "problem"


class RunError(Exception):
    """The run cannot start; the message says why. Raised before any work."""


class StopRequested(Exception):
    """Raised between messages when the caller asked the run to stop."""


@dataclass(frozen=True)
class RunResult:
    state: str  # FINISHED, STOPPED_ON_REQUEST or STOPPED_BY_PROBLEM
    report: RunReport | None = None  # set when FINISHED
    problem: Problem | None = None  # set when STOPPED_BY_PROBLEM


class _Halt(Exception):
    """Ends the run early with ``result``."""

    def __init__(self, result: RunResult):
        self.result = result


def run_pipeline(
    config: Config,
    mailbox_path: Path,
    store_path: Path,
    *,
    allow_remote: bool = False,
    thoroughness: str | None = None,
    review_path: Path | None = None,
    failures_path: Path | None = None,
    force: bool = False,
    subscriber: Callable[[Event], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    progress_stream: TextIO | None = None,
    command: str = "run",
) -> RunResult:
    """Run classify, extract and review for the session in ``store_path``.

    ``allow_remote`` is the command line's override of the locality check
    (the config's own setting is always honoured); ``thoroughness``,
    ``review_path`` and ``failures_path`` default to the config's.
    ``force`` regenerates an edited review table; without it an edited
    table is left as it is and the report says so. Each pass's own progress
    lines go to ``progress_stream`` (stderr when None; pass a sink to hide
    them). ``command`` is what the server checks' messages tell the user to
    rerun. Raises ``RunError`` before any work if the run cannot start."""
    thoroughness = thoroughness or config.thoroughness
    if thoroughness != QUICK:
        raise RunError(THOROUGH_NOT_AVAILABLE)
    run = _Run(
        config, mailbox_path, store_path, subscriber or (lambda event: None), should_stop or (lambda: False),
        progress_stream, command,
    )
    run.thoroughness = thoroughness
    run.emit(RunStarted(QUICK_STAGES, thoroughness, mailbox_path, store_path))
    try:
        run.check_address(allow_remote)
        classify = run.classify()
        extract = run.extract()
        review, left = run.review(review_path or config.review_output, failures_path or config.failures_output, force)
    except _Halt as halt:
        return halt.result
    report = RunReport(thoroughness, classify, extract, review, review_table_left=left, files=tuple(run.files_written))
    run.emit(RunFinished(report))
    return RunResult(FINISHED, report=report)


class _Run:
    def __init__(
        self,
        config: Config,
        mailbox_path: Path,
        store_path: Path,
        emit: Callable[[Event], None],
        should_stop: Callable[[], bool],
        progress_stream: TextIO | None,
        command: str,
    ):
        self.config = config
        self.mailbox_path = mailbox_path
        self.store_path = store_path
        self.emit = emit
        self.should_stop = should_stop
        self.progress_stream = progress_stream
        self.command = command
        self.model_requests = 0  # sent by the current stage
        self.thoroughness = config.thoroughness
        self.files_written: list[tuple[str, Path]] = []  # (what, where), as recorded in the session

    def check_address(self, allow_remote: bool) -> None:
        try:
            warning = address_warning(self.config, allow_remote)
        except PreflightError as exc:
            self._problem(None, ADDRESS, str(exc))
        if warning:
            self.emit(Notice(None, warning))

    def classify(self) -> ClassifySummary:
        client = decision_client(self.config)
        with self._model_stage(CLASSIFY, client, DECISION_SETTING) as (store, finish):
            session.begin(store, self.config, self.mailbox_path, self.thoroughness)
            # The mailbox's size is known only once the pass has read it.
            self.emit(StageStarted(CLASSIFY, None))
            summary = run_classify(
                self.config, self.mailbox_path, store, client, self.progress_stream, self._on_progress(CLASSIFY)
            )
            # Nothing to do: every message already had its classification or markup record.
            finish(summary, self.model_requests == 0 and summary.resolved_from_markup == 0)
        return summary

    def extract(self) -> ExtractSummary:
        client = extraction_client(self.config)
        with self._model_stage(EXTRACT, client, EXTRACTION_SETTING) as (store, finish):
            selected = store.extraction_candidates(self.config.decision_model, self.config.threshold)
            pending = len(set(selected) - store.extracted_ids(self.config.extraction_model))
            self.emit(StageStarted(EXTRACT, pending))
            summary = run_extract(
                self.config, self.mailbox_path, store, client, self.progress_stream, self._on_progress(EXTRACT)
            )
            finish(summary, pending == 0)
        return summary

    def review(self, review_path: Path, failures_path: Path, force: bool) -> tuple[ReviewSummary, bool]:
        """Write the review table and failures file. An edited review table
        is left as it is (unless ``force``): the flights are then only
        counted for the report, from the same decision-aware flight list,
        and nothing is recorded for a table that was not written."""
        self._check_stop()
        self.emit(StageStarted(REVIEW, None))
        with Store(self.store_path) as store:
            try:
                summary = write_review(self.config, store, review_path, failures_path, force)
                left = False
            except ReviewExists:
                summary = write_review_without_table(self.config, store, review_path, failures_path)
                left = True
            self.files_written = [] if left else [(session.REVIEW_TABLE, review_path)]
            self.files_written.append((session.FAILURES_FILE, failures_path))
            for kind, path in self.files_written:
                session.record_written(store, kind, path)
            session.touch(store)
        self.emit(StageFinished(REVIEW, DONE, summary, REVIEW_TABLE_LEFT if left else None))
        return summary, left

    # -- shared --------------------------------------------------------------------

    @contextmanager
    def _model_stage(
        self, stage: str, client: DecisionClient | ExtractionClient, setting: str
    ) -> Iterator[tuple[Store, Callable[[object, bool], None]]]:
        """The store for one model pass, and a ``finish(summary, idle)`` to
        call with its result. Ends the run on a stop, a problem or an
        interruption, reporting the stage as stopped."""
        self._check_stop()
        self.model_requests = 0
        finished: list[tuple[object, bool]] = []

        def warn(text: str) -> None:
            self.emit(Notice(stage, text))

        try:
            with pass_store(self.store_path, client, setting, self.command, self.config, warn) as store:
                yield store, lambda summary, idle: finished.append((summary, idle))
        except PreflightError as exc:
            # What the pass did was rolled back: there is no summary to show.
            self.emit(StageFinished(stage, STOPPED, None))
            self._problem(stage, PREFLIGHT, str(exc))
        except OllamaUnreachable as exc:
            self.emit(StageFinished(stage, STOPPED, getattr(exc, "summary", None)))
            self._problem(stage, UNREACHABLE, str(exc))
        except StopRequested as exc:
            self.emit(StageFinished(stage, STOPPED, getattr(exc, "summary", None)))
            raise _Halt(RunResult(STOPPED_ON_REQUEST)) from None
        except KeyboardInterrupt as exc:
            self.emit(StageFinished(stage, STOPPED, getattr(exc, "summary", None)))
            raise
        # Reported once the store is closed, so every result is committed.
        summary, idle = finished[0]
        self.emit(StageFinished(stage, NOTHING_TO_DO if idle else DONE, summary))

    def _on_progress(self, stage: str) -> Callable[[Progress], None]:
        def advance(progress: Progress) -> None:
            self.model_requests = progress.work_done
            self.emit(
                StageProgress(
                    stage, progress.done, progress.total, progress.seconds_per_message, progress.seconds_remaining
                )
            )
            self.emit(StageCounts(stage, copy.copy(progress.counts)))
            if self.should_stop():
                raise StopRequested

        return advance

    def _check_stop(self) -> None:
        if self.should_stop():
            raise _Halt(RunResult(STOPPED_ON_REQUEST))

    def _problem(self, stage: str | None, kind: str, message: str) -> None:
        problem = Problem(stage, kind, message)
        self.emit(problem)
        raise _Halt(RunResult(STOPPED_BY_PROBLEM, problem=problem))
