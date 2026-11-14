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
from .screens.export import ExportScreen
from .screens.progress import ProgressScreen
from .screens.review import ReviewScreen, ReviewView
from ..engine import session
from ..engine.setup import answers_from_config
from .screens.setup import SetupScreen
from .screens.startup import StartupScreen


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
        self.review_view: ReviewView | None = None  # the review's filters and selection while exporting

    def on_mount(self) -> None:
        self.push_screen(self.first_screen())

    def first_screen(self) -> Screen:
        """The startup prompt (resume or start fresh) when a session exists;
        otherwise setup, prefilled from the config file, so that a first run
        confirms its three answers (one key accepts them as they are).
        Setup moves on to the model-server check and from there to the
        progress screen; resume goes to the review for a finished session,
        to the progress screen otherwise."""
        description = session.describe(self.config, self.config.store)
        if description is not None:
            return StartupScreen(description)
        return SetupScreen(answers_from_config(self.config))

    def show_screen(self, screen: Screen) -> None:
        """Replace the current screen with ``screen``."""
        self.switch_screen(screen)

    def on_progress_screen_run_completed(self, message: ProgressScreen.RunCompleted) -> None:
        # The progress screen stays, showing the report, until the user
        # presses its key for review (which calls ``open_review``).
        self.report = message.report

    def open_review(self, report: RunReport | None) -> None:
        """Show the review screen for the finished run ``report``. Called
        from the progress screen's review key once the run is complete (the
        key is offered only then) and from the startup prompt; the screen
        reads the flight list afresh from the store."""
        self.show_screen(ReviewScreen(self.config, report))

    def open_export(self, view: ReviewView) -> None:
        """Show the export screen (from the review, whose view state is kept
        for ``back_to_review``). A new screen each time, so it plans afresh
        from the decisions saved since."""
        self.review_view = view
        self.show_screen(ExportScreen(self.config))

    def back_to_review(self) -> None:
        """Back from the export screen: the review, read afresh from the
        store, with the filters and the selected flight it had."""
        self.show_screen(ReviewScreen(self.config, self.report, self.review_view))

    async def action_quit(self) -> None:
        request_quit = getattr(self.screen, "request_quit", None)
        if request_quit is not None:
            request_quit()
        else:
            self.exit(0)
