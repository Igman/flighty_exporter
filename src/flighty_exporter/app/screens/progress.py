"""The progress screen: runs the run engine and draws what it reports.

It holds no pipeline logic: the stage list comes from ``RunStarted.stages``,
every number from the engine's events, and the end-of-run report from
``format_report``. What the screen owns is view state: what it last drew
for each stage, and whether it is running, paused, stopping, showing a
problem or finished.

Keys: ``p`` pauses (the engine stops after the message in progress) and
continues (a fresh run, which skips what is done); ``r`` retries after a
model-server problem (not after a refused non-local address: the config is
read at launch, so only relaunching after changing it can help); ``q`` quits, waiting for the message in progress
(``q`` or Ctrl-C again quits at once); ``enter``, once the run has finished,
opens the review (``FlightyApp.open_review``).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Log, ProgressBar, Static

from ...engine.events import (
    ADDRESS,
    DONE,
    NOTHING_TO_DO,
    SKIPPED,
    STOPPED,
    Notice,
    Problem,
    RunFinished,
    RunStarted,
    StageCounts,
    StageFinished,
    StageProgress,
    StageStarted,
)
from ...engine.pipeline import FINISHED, STOPPED_ON_REQUEST
from ...engine.report import RunReport, format_report
from ...engine.thorough import LOWERED, IncludeSummary
from ...stages.audit import AuditResult
from ...stages.classify import ClassifySummary
from ...stages.extract import ExtractSummary
from ..paths import path_text
from ..runner import EngineEvent, EngineRunner, RunEnded, RunInputs

# What the screen is doing.
STARTING = "starting"
RUNNING = "running"
PAUSING = "pausing"
PAUSED = "paused"
QUITTING = "quitting"
PROBLEM = "problem"  # a problem a retry may get past
STUCK = "stuck"  # a problem only a change to the config file fixes
FINISHED_VIEW = "finished"

WAITING = "waiting"
RUNNING_STAGE = "running"

STATE_MARKS = {
    WAITING: ("·", "dim"),
    RUNNING_STAGE: ("▶", "bold yellow"),
    DONE: ("✓", "green"),
    NOTHING_TO_DO: ("✓", "dim green"),
    STOPPED: ("■", "red"),
    SKIPPED: ("–", "dim"),
}

STATUS = {
    STARTING: "Starting the run…",
    RUNNING: "Running. Press p to pause, q to quit; the next launch carries on where this one stops.",
    PAUSING: "Pausing after the current message…",
    PAUSED: "Paused. Press p to continue.",
    QUITTING: "Stopping after the current message… (press q again to quit now)",
    PROBLEM: "The run stopped: see below.",
    STUCK: "The run cannot start: see below.",
    FINISHED_VIEW: "Run finished.",
}

# Summaries whose counts are shown when their stage finishes even if it sent
# nothing (extract on a relaunch, an audit whose sample was extracted before).
FINAL_COUNTS = (ClassifySummary, ExtractSummary, AuditResult, IncludeSummary)

RETRY_HINT = "Press r to retry once it is fixed, or q to quit."
# Under the engine's message, which is the commands' and mentions their --allow-remote option.
ADDRESS_HINT = (
    "In the application, --allow-remote does not apply: point [ollama] address at a server on this "
    "machine, or set `allow_remote = true` under [ollama] in the config file, then start "
    "flighty-exporter again. Press q to quit."
)
REVIEW_NEXT = "Next step: press enter to review the flights (or q to quit)."

def human_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "less than a minute"
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return "about " + " ".join(parts)


def progress_line(event: StageProgress) -> str:
    what = "mailbox entries scanned" if event.phase else "messages"
    parts = [f"{event.done:,} of {event.total:,} {what}"]
    if event.seconds_per_message is None:
        parts.append("rate not known yet")
    else:
        parts.append(f"{event.seconds_per_message:.1f} s per message")
    if event.seconds_remaining is not None:
        parts.append(f"{human_duration(event.seconds_remaining)} left")
    return " · ".join(parts)


def summary_counts(summary: Any) -> list[str]:
    """The counts worth showing from a stage's summary, as text.

    For classify and extract they are the session's totals so far: each
    summary counts this run's work next to what earlier runs had already
    done ("already classified", "already extracted"), so "done so far" is
    their sum. Failures are retried on every run, so the failed count is the
    messages still failing. At the end of a Thorough run's audit and include
    stages, their own summaries give the same totals (messages reused from
    earlier extractions count as extracted); while those stages run, their
    counts are their extraction summaries, which hold only this run's
    extractions. Any other summary is shown generically: every numeric
    field."""
    if isinstance(summary, ClassifySummary):
        return [
            f"classified {summary.classified + summary.already_classified:,}",
            "resolved from machine-readable data "
            f"{summary.resolved_from_markup + summary.already_resolved_from_markup:,}",
            f"skipped {summary.skipped:,}",
            f"failed {summary.failed:,}",
        ]
    if isinstance(summary, ExtractSummary):
        extracted = summary.extracted + summary.already_extracted
        return [
            f"extracted {extracted:,} of {summary.selected:,}" if summary.selected else f"extracted {extracted:,}",
            f"failed {summary.failed + summary.not_in_mailbox:,}",
        ]
    if isinstance(summary, AuditResult):
        return [
            f"sampled {summary.sampled:,}",
            f"extracted {summary.reused + summary.extraction.extracted:,} of {summary.sampled:,}",
            f"failed {summary.extraction.failed + summary.extraction.not_in_mailbox:,}",
        ]
    if isinstance(summary, IncludeSummary):
        if summary.outcome != LOWERED:
            return []
        wanted = summary.would_add + summary.already_extracted
        return [
            f"extracted {summary.already_extracted + summary.extraction.extracted:,} of {wanted:,}",
            f"flight emails {summary.with_flights:,}",
            f"failed {summary.extraction.failed + summary.extraction.not_in_mailbox:,}",
        ]
    if dataclasses.is_dataclass(summary):
        values = {field.name: getattr(summary, field.name) for field in dataclasses.fields(summary)}
    else:
        values = dict(vars(summary)) if hasattr(summary, "__dict__") else {}
    return [
        f"{name.replace('_', ' ')} {value:,}"
        for name, value in values.items()
        if isinstance(value, int) and not isinstance(value, bool)
    ]


class ProgressScreen(Screen):
    """Runs the pipeline for ``inputs`` as soon as it is shown."""

    BINDINGS = [
        Binding("p", "pause", "Pause"),
        Binding("p", "resume", "Continue"),
        Binding("r", "retry", "Retry"),
        Binding("enter", "open_review", "Review flights"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ProgressScreen { layout: vertical; }
    #body { height: 1fr; padding: 0 1; }
    #heading { text-style: bold; }
    #status { margin: 1 0; }
    .panel { border: round $primary; height: auto; padding: 0 1; }
    #current { height: auto; }
    #current ProgressBar { width: 1fr; }
    #current Bar { width: 1fr; }
    #problem { border: round $error; display: none; }
    #report { border: round $success; display: none; }
    #notices { height: 6; border: round $secondary; padding: 0 1; }
    """

    class RunCompleted(Message):
        """The run finished; ``report`` is the engine's end-of-run report.
        The app moves on to review from here (see ``FlightyApp.open_review``)."""

        def __init__(self, report: RunReport) -> None:
            super().__init__()
            self.report = report

    def __init__(self, inputs: RunInputs) -> None:
        super().__init__()
        self.inputs = inputs
        self.runner = EngineRunner(inputs, self)
        self.view = STARTING
        self.stages: dict[str, str] = {}  # stage -> its state, in the engine's order
        self.notes: dict[str, str] = {}  # stage -> the note it finished with
        self.counts: dict[str, list[str]] = {}
        self.report: RunReport | None = None  # the finished run's report, once there is one

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="body"):
            yield Static(
                f"Mailbox:\n{escape(path_text(self.inputs.mailbox, '  '))}\nThoroughness: {self.inputs.thoroughness}",
                id="heading",
            )
            yield Static(STATUS[STARTING], id="status")
            yield Static("", id="stages", classes="panel")
            with Vertical(id="current", classes="panel"):
                yield Static("", id="current-label")
                yield ProgressBar(total=None, show_eta=False, id="bar")
                yield Static("", id="current-detail")
            yield Static("No counts yet.", id="counts", classes="panel")
            yield Static("", id="problem", classes="panel")
            yield Static("", id="report", classes="panel")
            yield Log(id="notices", max_lines=200)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#stages").border_title = "Stages"
        self.query_one("#current").border_title = "Current stage"
        self.query_one("#counts").border_title = "Counts"
        self.query_one("#problem").border_title = "Problem"
        self.query_one("#report").border_title = "Report"
        self.query_one("#notices").border_title = "Notices"
        self.query_one("#current").display = False
        self.start_run()

    # -- the run -----------------------------------------------------------------

    def start_run(self) -> None:
        self.query_one("#problem").display = False
        self._set_view(STARTING)
        self.runner.start()

    def on_engine_event(self, message: EngineEvent) -> None:
        event = message.event
        if isinstance(event, RunStarted):
            self.stages = {stage: WAITING for stage in event.stages}
            self.notes = {}
            # Counts are the session's totals: they stay until the new run's replace them.
            self._draw_stages()
            self._draw_counts()
            if self.view == STARTING:
                self._set_view(RUNNING)
        elif isinstance(event, StageStarted):
            self.stages[event.stage] = RUNNING_STAGE
            self._draw_stages()
            current = self.query_one("#current")
            # A stage with nothing to handle finishes at once: no bar to draw.
            current.display = event.total != 0
            current.border_title = f"Current stage: {event.stage}"
            self.query_one("#current-label").display = False
            self.query_one("#bar", ProgressBar).update(total=event.total or None, progress=0)
            self.query_one("#current-detail", Static).update(
                f"{event.total:,} messages to handle" if event.total is not None else "reading the mailbox…"
            )
        elif isinstance(event, StageProgress):
            label = self.query_one("#current-label", Static)
            label.update(event.phase.capitalize())
            label.display = bool(event.phase)
            self.query_one("#bar", ProgressBar).update(total=event.total, progress=event.done)
            self.query_one("#current-detail", Static).update(progress_line(event))
        elif isinstance(event, StageCounts):
            self.counts[event.stage] = summary_counts(event.summary)
            self._draw_counts()
        elif isinstance(event, StageFinished):
            self.stages[event.stage] = event.state
            if event.note:
                self.notes[event.stage] = event.note
            self._draw_stages()
            self.query_one("#current").display = False
            if event.summary is not None and (event.stage in self.counts or isinstance(event.summary, FINAL_COUNTS)):
                # The final figures of a stage whose counts were showing, or
                # of a known stage that had nothing to send.
                counts = summary_counts(event.summary)
                if counts:
                    self.counts[event.stage] = counts
                    self._draw_counts()
        elif isinstance(event, Notice):
            self._log(event.message)
        elif isinstance(event, Problem):
            # The config is read at launch, so retrying cannot fix an address problem.
            self._show_problem(event.message, retry=event.kind != ADDRESS)
        elif isinstance(event, RunFinished):
            self.report = event.report
            report = self.query_one("#report", Static)
            report.update(escape(format_report(event.report, REVIEW_NEXT)))
            report.display = True
            report.scroll_visible()
            self.post_message(self.RunCompleted(event.report))

    def on_run_ended(self, message: RunEnded) -> None:
        if self.view == QUITTING:
            self.app.exit(0)
            return
        result = message.result
        if message.error is not None:
            self._show_problem(f"error: {message.error}", retry=True)
        elif result is not None and result.state == FINISHED:
            self._set_view(FINISHED_VIEW)
        elif result is not None and result.state == STOPPED_ON_REQUEST:
            self._set_view(PAUSED)

    def _show_problem(self, text: str, *, retry: bool) -> None:
        panel = self.query_one("#problem", Static)
        panel.update(escape(text + "\n\n" + (RETRY_HINT if retry else ADDRESS_HINT)))
        panel.display = True
        self._set_view(PROBLEM if retry else STUCK)
        panel.scroll_visible()

    # -- keys --------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "pause":
            return self.view in (RUNNING, STARTING)
        if action == "resume":
            return self.view == PAUSED
        if action == "retry":
            return self.view == PROBLEM
        if action == "open_review":
            # Only a complete run is reviewed: not after a pause, a stop or a problem.
            return self.view == FINISHED_VIEW and self.report is not None
        return True

    def action_pause(self) -> None:
        self.runner.request_stop()
        self._set_view(PAUSING)

    def action_resume(self) -> None:
        self.start_run()

    def action_retry(self) -> None:
        self.start_run()

    def action_open_review(self) -> None:
        self.app.open_review(self.report)  # type: ignore[attr-defined]

    def action_quit(self) -> None:
        self.request_quit()

    def request_quit(self) -> None:
        """Quit, letting a running engine finish the message in progress; asked
        again while it does, quit at once (every result is already committed)."""
        if not self.runner.running or self.view == QUITTING:
            self.app.exit(0)
            return
        self.runner.request_stop()
        self._set_view(QUITTING)

    # -- drawing -----------------------------------------------------------------

    def _set_view(self, view: str) -> None:
        self.view = view
        self.query_one("#status", Static).update(STATUS[view])
        self.refresh_bindings()

    def _draw_stages(self) -> None:
        width = max((len(stage) for stage in self.stages), default=0)
        lines = []
        for stage, state in self.stages.items():
            mark, style = STATE_MARKS.get(state, ("?", ""))
            lines.append(f"[{style}]{mark}[/] {escape(stage).ljust(width)}  [{style}]{escape(state)}[/]")
            if stage in self.notes:
                lines.append(f"  {' ' * width}  [dim]{escape(self.notes[stage])}[/]")
        self.query_one("#stages", Static).update("\n".join(lines))

    def _draw_counts(self) -> None:
        lines = []
        for stage, counts in self.counts.items():
            shown = " · ".join(escape(count) for count in counts)
            lines.append(f"[b]{escape(stage)}[/b]: {shown}")
        self.query_one("#counts", Static).update("\n".join(lines) or "No counts yet.")

    def _log(self, text: str) -> None:
        self.query_one("#notices", Log).write_line(text)
