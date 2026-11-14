"""Placeholder shown when the app cannot start a run because no mailbox is
known: guided setup does not exist yet. The setup ticket replaces this
screen (see ``FlightyApp.first_screen``)."""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static


class SetupUnavailableScreen(Screen):
    BINDINGS = [Binding("q", "app.quit", "Quit")]

    DEFAULT_CSS = """
    SetupUnavailableScreen VerticalScroll { padding: 1 2; }
    """

    def __init__(self, config_path: Path, mailbox: Path | None) -> None:
        super().__init__()
        self.config_path = config_path
        self.mailbox = mailbox

    def compose(self) -> ComposeResult:
        if self.mailbox is None:
            why = "No mailbox is set in the config file."
        else:
            why = f"The mailbox file was not found: {escape(str(self.mailbox))}"
        text = (
            "[b]Guided setup is not available yet.[/b]\n\n"
            f"{why}\n\n"
            "To set it, open the config file (create it if it does not exist):\n\n"
            f"    {escape(str(self.config_path))}\n\n"
            "and put this line at the top, before any \\[section] heading, with the path to your Gmail "
            "Takeout mbox:\n\n"
            '    mailbox = "Takeout/Mail/All mail Including Spam and Trash.mbox"\n\n'
            "A relative path is read from the config file's folder. Then start flighty-exporter again.\n\n"
            "Press q to quit."
        )
        with VerticalScroll():
            yield Static(text)
        yield Footer()
