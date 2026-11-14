"""The startup prompt, shown when a session exists: resume it or start fresh.

It draws the session as ``engine.session.describe`` reports it and offers
two choices. Resume shows the progress screen, which runs the engine: an
unfinished run carries on, a finished one completes at once and the app
moves on to its results. Start fresh asks once, listing what would be lost
and what is kept, then calls ``engine.session.start_fresh`` and shows setup
with the previous answers prefilled.

Keys: ``r`` resume, ``f`` start fresh (then ``y`` to confirm, ``n`` or
Escape to cancel), ``q`` quit.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Static

from ...engine import session
from ...engine.session import SessionDescription
from ...engine.setup import answers_from_session, format_names
from ..runner import RunInputs
from .progress import ProgressScreen
from .setup import SetupScreen


def when(value: str | None) -> str:
    if value is None:
        return "unknown"
    try:
        moment = datetime.fromisoformat(value).astimezone()
    except ValueError:
        return value
    return moment.strftime("%Y-%m-%d %H:%M")


def describe_lines(d: SessionDescription) -> list[str]:
    """The session, as markup lines for the prompt."""
    lines = []
    if d.mailbox is None:
        lines.append("[b]Mailbox[/b]       unknown (this session was started by an older version)")
    elif d.mailbox_exists:
        lines.append(f"[b]Mailbox[/b]       {escape(str(d.mailbox))}")
    else:
        lines.append(f"[b]Mailbox[/b]       {escape(str(d.mailbox))} [red](no longer there)[/red]")
    lines.append(f"[b]Started[/b]       {when(d.started_at)}")
    lines.append(f"[b]Last active[/b]   {when(d.last_active)}")
    names = "unknown" if d.passenger_names is None else format_names(d.passenger_names)
    lines.append(f"[b]Names[/b]         {escape(names)}")
    lines.append(f"[b]Thoroughness[/b]  {escape(d.thoroughness or 'unknown')}")
    lines.append("")
    lines.append("[b]How far it got[/b]")
    read = d.classified + d.resolved_from_markup
    lines.append(
        f"  classify   {read:,} of {d.messages:,} recorded emails read"
        + (f" ({d.resolved_from_markup:,} from machine-readable flight data)" if d.resolved_from_markup else "")
    )
    lines.append(f"  extract    {d.extracted:,} of {d.candidates:,} likely flight emails extracted")
    lines.append(f"  review     {d.flights:,} flights, {d.flights_with_decision:,} with a saved decision")
    threshold = f"{d.threshold_used:g}"
    if d.threshold_used < d.threshold_configured:
        threshold += f" (lowered for this session from the configured {d.threshold_configured:g})"
    lines.append(f"  threshold  {threshold}")
    lines.append("")
    if d.complete:
        lines.append("[green]The run is complete.[/green] Resuming opens its results.")
    elif read == 0:
        lines.append("[yellow]The run is unfinished:[/yellow] nothing has been classified yet.")
    else:
        lines.append(
            f"[yellow]The run is unfinished:[/yellow] {d.still_to_extract:,} likely flight "
            f"email{'' if d.still_to_extract == 1 else 's'} still to extract. Resuming carries on where it stopped; nothing finished is sent to a model again."
        )
    if d.mailbox is not None and not d.mailbox_exists:
        lines.append("")
        lines.append(
            "[red]The mailbox file is no longer at that path.[/red] The store keeps only what the run has "
            "already read, and resuming reads the mailbox again to check that every email has been handled, "
            "so it would stop with \"mailbox file not found\". Put the file back where it was, or start fresh "
            "and point setup at its new place."
        )
    return lines


class ConfirmFreshScreen(ModalScreen[bool]):
    """One confirmation before starting fresh: what would be lost, what is kept."""

    BINDINGS = [
        Binding("y", "confirm", "Yes, start fresh"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "No", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmFreshScreen { align: center middle; }
    ConfirmFreshScreen > Vertical {
        width: 80%; max-width: 90; height: auto; border: thick $error; background: $surface; padding: 1 2;
    }
    """

    def __init__(self, description: SessionDescription) -> None:
        super().__init__()
        self.description = description

    def compose(self) -> ComposeResult:
        lost = [f"  • {escape(line)}" for line in self.description.would_lose] or [
            "  • nothing yet (the store holds no results)"
        ]
        text = "\n".join(
            [
                "[b]Start fresh?[/b]",
                "",
                "This deletes the session's store and the files the tool wrote for it. You would lose:",
                *lost,
                "",
                escape(session.KEPT_WHEN_FRESH),
                "",
                "Press [b]y[/b] to start fresh, or [b]n[/b] to go back.",
            ]
        )
        with Vertical():
            yield Static(text)
        yield Footer()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class StartupScreen(Screen):
    BINDINGS = [
        Binding("r", "resume", "Resume"),
        Binding("f", "fresh", "Start fresh"),
        Binding("q", "app.quit", "Quit"),
    ]

    DEFAULT_CSS = """
    StartupScreen VerticalScroll { padding: 1 2; }
    StartupScreen #session { border: round $primary; padding: 0 1; height: auto; }
    StartupScreen #choices { margin-top: 1; }
    """

    def __init__(self, description: SessionDescription) -> None:
        super().__init__()
        self.description = description

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("[b]A previous session was found.[/b]\n")
            yield Static("\n".join(describe_lines(self.description)), id="session")
            resume = "open the finished run's results" if self.description.complete else "carry on where it stopped"
            yield Static(
                f"[b]r[/b]  Resume: {resume}\n"
                "[b]f[/b]  Start fresh: delete this session (after one confirmation) and answer setup again, "
                "with your previous answers filled in\n"
                "[b]q[/b]  Quit",
                id="choices",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#session").border_title = f"Session in {self.description.store_path}"

    def action_resume(self) -> None:
        config = self.app.config  # type: ignore[attr-defined]
        mailbox = self.description.mailbox or config.mailbox
        if mailbox is None:
            self.notify("No mailbox is known for this session: start fresh to set one.", severity="error")
            return
        self.app.show_screen(ProgressScreen(RunInputs.from_config(config, mailbox)))  # type: ignore[attr-defined]

    def action_fresh(self) -> None:
        self.app.push_screen(ConfirmFreshScreen(self.description), self._fresh_confirmed)

    def _fresh_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        config = self.app.config  # type: ignore[attr-defined]
        result = session.start_fresh(self.description.store_path)
        answers = answers_from_session(self.description, config)
        self.app.show_screen(SetupScreen(answers, notice=["Started fresh.", *session.format_fresh(result)]))  # type: ignore[attr-defined]
