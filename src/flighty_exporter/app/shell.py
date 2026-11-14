"""The application: picks the first screen and moves between screens.

Screens are top-level views shown one at a time with ``show_screen``
(replacing the current one); a later screen is added by writing a ``Screen``
subclass in ``screens/`` and calling ``show_screen`` with it from wherever
the user moves on (a key on a screen, or a message a screen posts that the
app handles here). Screens hold only view state; whatever they show comes
from the engine, the store or the config.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen

from ..config import Config
from ..engine.report import RunReport
from .runner import RunInputs
from .screens.progress import ProgressScreen
from .screens.setup_unavailable import SetupUnavailableScreen


class FlightyApp(App[int]):
    TITLE = "Flighty exporter"
    ENABLE_COMMAND_PALETTE = False

    # Ctrl-Q quits like q: through the current screen, so a run stops cleanly.
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", show=False, priority=True)]

    def __init__(self, config: Config, config_path: Path) -> None:
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.report: RunReport | None = None  # the last finished run's report

    def on_mount(self) -> None:
        self.push_screen(self.first_screen())

    def first_screen(self) -> Screen:
        """The progress screen when the config names a mailbox that exists;
        otherwise the placeholder that says how to set one (guided setup,
        a later ticket, goes here)."""
        mailbox = self.config.mailbox
        if mailbox is None or not mailbox.is_file():
            return SetupUnavailableScreen(self.config_path, mailbox)
        return ProgressScreen(RunInputs.from_config(self.config, mailbox))

    def show_screen(self, screen: Screen) -> None:
        """Replace the current screen with ``screen``."""
        self.switch_screen(screen)

    def on_progress_screen_run_completed(self, message: ProgressScreen.RunCompleted) -> None:
        self.report = message.report
        self.open_review(message.report)

    def open_review(self, report: RunReport) -> None:
        """Hook for moving on to review once a run has finished. The review
        screen does not exist yet, so the progress screen stays, showing the
        report; the review ticket shows its screen here (``show_screen``),
        or from a key on the progress screen that calls this."""

    async def action_quit(self) -> None:
        request_quit = getattr(self.screen, "request_quit", None)
        if request_quit is not None:
            request_quit()
        else:
            self.exit(0)
