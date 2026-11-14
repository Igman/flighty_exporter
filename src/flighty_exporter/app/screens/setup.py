"""Guided setup: the three questions, then the model-server check.

The screen collects the mailbox path, the name variants and thoroughness,
and shows what ``engine.setup`` says about them: a rejected mailbox's
message under the field (checked when the field is left and on continue), a
name that names no one, and a summary of what was entered. Continuing
writes the answers with ``engine.setup.write_answers`` (which keeps every
other setting in the file), reloads the config, and shows the readiness
screen.

Keys: Tab moves between the fields, Ctrl-S (or the button) saves and
continues, Ctrl-Q quits.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, RadioButton, RadioSet, Static, TextArea

from ...config import QUICK, THOROUGH, ConfigError, load_config
from ...engine.setup import (
    SetupAnswers,
    SetupError,
    check_names,
    format_names,
    mailbox_path,
    mailbox_problem,
    parse_name_variants,
    write_answers,
)
from ..paths import path_text
from .readiness import ReadinessScreen

MAILBOX_HELP = (
    "The .mbox file from your Gmail Takeout export, usually "
    "[i]Takeout/Mail/All mail Including Spam and Trash.mbox[/i]. You can drag the file into the terminal "
    "to paste its path. A relative path is read from:\n{cwd}"
)
NAMES_HELP = (
    "Your name as it appears on bookings, so that flights you booked for other people are left out. List "
    "every variant, one per line or separated by commas: with and without a middle name, a maiden name, and "
    "so on. Case, accents, word order, punctuation and titles are ignored, so airline formats such as "
    "\"ELLIS/JORDAN MR\" already match \"Jordan Ellis\".\n"
    "Example: [i]Jordan Ellis, Jordan A. Ellis[/i]\n"
    "[b]Leave it empty to turn passenger filtering off: every booking is kept[/b], including ones made for "
    "other people."
)
QUICK_HELP = "Quick: classifies every email and extracts the flight emails. No extra check; the fastest result."
THOROUGH_HELP = (
    "Thorough: after extraction, also checks for flight emails the first filter missed. Roughly 2 to 35 "
    "minutes more depending on the mailbox, and it can lower the threshold for this session when that adds "
    "at most {budget:,} emails to extract."
)
FIXED_NOTE = (
    "These answers are fixed once the run starts: changing one later means starting fresh, which repeats "
    "the run. Check the names especially."
)


class SetupScreen(Screen):
    BINDINGS = [Binding("ctrl+s", "save", "Save and continue", priority=True)]

    DEFAULT_CSS = """
    SetupScreen VerticalScroll { padding: 0 2; }
    SetupScreen .question { text-style: bold; margin-top: 1; }
    SetupScreen .help { color: $text-muted; }
    SetupScreen .error { color: $error; height: auto; }
    SetupScreen #names { height: 5; }
    SetupScreen #notice { border: round $success; padding: 0 1; height: auto; }
    SetupScreen #summary { border: round $primary; padding: 0 1; margin-top: 1; height: auto; }
    SetupScreen RadioSet { width: auto; }
    SetupScreen Button { margin: 1 0; }
    """

    def __init__(self, answers: SetupAnswers, notice: list[str] | None = None) -> None:
        super().__init__()
        self.answers = answers
        self.notice = notice or []

    def compose(self) -> ComposeResult:
        budget = self.app.config.audit_auto_include_budget  # type: ignore[attr-defined]
        config_path = self.app.config_path  # type: ignore[attr-defined]
        with VerticalScroll():
            if self.notice:
                yield Static(escape("\n".join(self.notice)), id="notice")
            yield Static(
                "[b]Setup[/b]: three questions. The answers are written to the config file below, keeping any "
                f"other settings in it:\n{escape(path_text(config_path, '  '))}",
            )
            yield Static("1. Your mailbox file", classes="question")
            yield Static(MAILBOX_HELP.format(cwd=escape(path_text(Path.cwd(), "  "))), classes="help")
            yield Input(self.answers.mailbox, placeholder="path to the .mbox file", id="mailbox")
            yield Static("", id="mailbox-error", classes="error")
            yield Static("2. Your name variants", classes="question")
            yield Static(NAMES_HELP, classes="help")
            yield TextArea("\n".join(self.answers.names), id="names", placeholder="one name per line; empty: no passenger filtering")
            yield Static("", id="names-error", classes="error")
            yield Static("3. How thorough to be", classes="question")
            with RadioSet(id="thoroughness"):
                yield RadioButton("Quick", value=self.answers.thoroughness != THOROUGH, id="quick")
                yield RadioButton("Thorough", value=self.answers.thoroughness == THOROUGH, id="thorough")
            yield Static(f"{QUICK_HELP}\n{THOROUGH_HELP.format(budget=budget)}", classes="help")
            yield Static("", id="summary")
            yield Button("Save and check the model server (Ctrl-S)", id="save", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#summary").border_title = "You entered"
        self._draw_summary()
        if self.answers.mailbox:
            # Prefilled: one key accepts the answers as they are. The view
            # stays at the top, so the notice and the first question show.
            self.query_one("#save", Button).focus(scroll_visible=False)
        else:
            self.query_one("#mailbox", Input).focus()

    # -- reading the fields ----------------------------------------------------------

    def _mailbox(self) -> Path | None:
        return mailbox_path(self.query_one("#mailbox", Input).value, Path.cwd())

    def _names(self) -> list[str]:
        return parse_name_variants(self.query_one("#names", TextArea).text)

    def _thoroughness(self) -> str:
        return THOROUGH if self.query_one("#thorough", RadioButton).value else QUICK

    # -- checking ----------------------------------------------------------------------

    def _check_mailbox(self) -> bool:
        problem = mailbox_problem(self._mailbox())
        self.query_one("#mailbox-error", Static).update(escape(problem or ""))
        return problem is None

    def _check_names(self) -> bool:
        try:
            check_names(self._names())
        except SetupError as exc:
            self.query_one("#names-error", Static).update(escape(str(exc)))
            return False
        self.query_one("#names-error", Static).update("")
        return True

    def on_input_blurred(self, event: Input.Blurred) -> None:
        if event.input.id == "mailbox" and event.input.value.strip():
            self._check_mailbox()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#mailbox-error", Static).update("")
        self._draw_summary()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._check_mailbox():
            self.query_one("#names", TextArea).focus()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._draw_summary()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._draw_summary()

    def _draw_summary(self) -> None:
        mailbox = self._mailbox()
        names = self._names()
        listed = "\n".join(f"    {escape(name)}" for name in names)
        text = (
            f"Mailbox:       {escape(path_text(mailbox, ' ' * 15).lstrip()) if mailbox else '[i]not entered yet[/i]'}\n"
            f"Name variants: {escape(format_names(names)) if not names else ''}\n"
            + (listed + "\n" if names else "")
            + f"Thoroughness:  {self._thoroughness().capitalize()}\n\n"
            + FIXED_NOTE
        )
        self.query_one("#summary", Static).update(text)

    # -- continuing ----------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()

    def action_save(self) -> None:
        mailbox_ok = self._check_mailbox()
        names_ok = self._check_names()
        if not mailbox_ok:
            self.query_one("#mailbox", Input).focus()
            return
        if not names_ok:
            self.query_one("#names", TextArea).focus()
            return
        config_path = self.app.config_path  # type: ignore[attr-defined]
        try:
            write_answers(config_path, self._mailbox(), self._names(), self._thoroughness())  # type: ignore[arg-type]
            config = load_config(config_path)
        except (SetupError, ConfigError) as exc:
            self.notify(str(exc), title="The config file was not written", severity="error", timeout=15)
            return
        self.app.config = config  # type: ignore[attr-defined]
        self.app.show_screen(ReadinessScreen(config, back=SetupScreen))  # type: ignore[attr-defined]
