"""Setup's last step: is the model server ready? Download a missing model.

It draws what ``engine.readiness.check_readiness`` reports (run off the
screen's thread): the address and whether it is on this machine, whether
the server answers and is new enough, and each configured model. A problem
is shown with the passes' own message: a server that is not running or too
old with a check-again key, a non-local address with the same guidance as
the progress screen (the application has no override). A missing model is
shown with its approximate size and downloaded only after a confirmation,
with a progress bar. Once everything is ready, a key starts the run.

Keys: ``c`` checks again, ``d`` downloads the next missing model (after
``y`` to confirm), ``s`` starts the run, ``b`` goes back to the answers,
``q`` quits (at once, even during a download: Ollama keeps the partial
download).
"""

from __future__ import annotations

from collections.abc import Callable

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, ProgressBar, Static

from ...config import Config
from ...engine.events import ADDRESS
from ...engine.readiness import ModelReadiness, Readiness
from ...engine.setup import SetupAnswers, answers_from_config
from ...ollama.pull import KEPT_PARTIAL, format_bytes
from ..background import PullEnded, PullUpdate, ReadinessChecked, check_in_background, pull_in_background
from ..runner import RunInputs
from .progress import ADDRESS_HINT, ProgressScreen

CHECKING = "checking"
PROBLEM = "problem"  # not reachable or too old: check again
STUCK = "stuck"  # non-local address: only a change to the config file helps
MISSING = "missing"
DOWNLOADING = "downloading"
DOWNLOAD_FAILED = "download failed"
READY = "ready"

STATUS = {
    CHECKING: "Checking the model server…",
    PROBLEM: "The model server is not ready: see below. Fix it, then press c to check again.",
    STUCK: "The model server's address is not on this machine: see below.",
    MISSING: "A model is missing. Press d to download it (you are asked first), or c to check again.",
    DOWNLOADING: f"Downloading… Press q to quit at any time: {KEPT_PARTIAL}",
    DOWNLOAD_FAILED: "The download failed: see below. Press d to try again, or c to check again.",
    READY: "Ready. Press s to start the run.",
}


class ConfirmDownloadScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes, download"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "No", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmDownloadScreen { align: center middle; }
    ConfirmDownloadScreen > Vertical {
        width: 80%; max-width: 80; height: auto; border: thick $warning; background: $surface; padding: 1 2;
    }
    """

    def __init__(self, model: ModelReadiness, address: str) -> None:
        super().__init__()
        self.model = model
        self.address = address

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[b]Download {escape(self.model.name)}?[/b]\n\n"
                f"Size: {escape(self.model.size_text)}. The Ollama server at {escape(self.address)} downloads it "
                "from the Ollama library and keeps it for later runs; starting fresh never deletes it.\n\n"
                "Press [b]y[/b] to download, or [b]n[/b] to go back."
            )
        yield Footer()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ReadinessScreen(Screen):
    BINDINGS = [
        Binding("s", "start", "Start the run"),
        Binding("d", "download", "Download"),
        Binding("c", "check", "Check again"),
        Binding("b", "back", "Back to the answers"),
        Binding("q", "app.quit", "Quit"),
    ]

    DEFAULT_CSS = """
    ReadinessScreen VerticalScroll { padding: 0 2; }
    ReadinessScreen #status { margin: 1 0; }
    ReadinessScreen .panel { border: round $primary; padding: 0 1; height: auto; }
    ReadinessScreen #problem { border: round $error; display: none; }
    ReadinessScreen #download { display: none; }
    ReadinessScreen #download ProgressBar { width: 1fr; }
    ReadinessScreen #download Bar { width: 1fr; }
    """

    def __init__(self, config: Config, back: Callable[[SetupAnswers], Screen]) -> None:
        """``back`` makes the setup screen to return to, prefilled with the answers."""
        super().__init__()
        self.config = config
        self.back = back
        self.view = CHECKING
        self.readiness: Readiness | None = None
        self.downloading: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("[b]Model server[/b]: the run needs a local Ollama server with both models.")
            yield Static(STATUS[CHECKING], id="status")
            yield Static("", id="server", classes="panel")
            yield Static("", id="problem", classes="panel")
            with Vertical(id="download", classes="panel"):
                yield ProgressBar(total=None, show_eta=False, id="bar")
                yield Static("", id="download-detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#server").border_title = f"Ollama at {self.config.ollama_address}"
        self.query_one("#problem").border_title = "Problem"
        self.action_check()

    # -- checking ------------------------------------------------------------------

    def action_check(self) -> None:
        self.query_one("#problem").display = False
        self._set_view(CHECKING)
        check_in_background(self.config, self)

    def on_readiness_checked(self, message: ReadinessChecked) -> None:
        if self.view != CHECKING:
            return
        readiness = self.readiness = message.readiness
        self.query_one("#server", Static).update(self._server_text(readiness))
        if readiness.problem is not None:
            hint = ADDRESS_HINT if readiness.problem_kind == ADDRESS else "Press c to check again once it is fixed."
            self._show_problem(f"{readiness.problem}\n\n{hint}")
            self._set_view(STUCK if readiness.problem_kind == ADDRESS else PROBLEM)
        elif readiness.missing:
            self._set_view(MISSING)
        else:
            self._set_view(READY)

    def _server_text(self, r: Readiness) -> str:
        lines = [f"On this machine:  {'yes' if r.local else '[red]no[/red]'}"]
        lines += [f"[yellow]{escape(w)}[/yellow]" for w in r.warnings]
        if r.problem_kind != ADDRESS:
            lines.append(f"Reachable:        {'yes' if r.reachable else '[red]no[/red]'}")
        if r.reachable:
            if r.version_ok is None:
                lines.append(f"Version:          {escape(r.version or 'not reported')} (cannot tell)")
            else:
                verdict = "new enough" if r.version_ok else "[red]too old[/red]"
                lines.append(f"Version:          {escape(r.version or '')} ({verdict})")
        for model in r.models:
            if model.present:
                state = "[green]ready[/green]"
            else:
                state = f"[yellow]missing[/yellow], {escape(model.size_text)} to download (press d)"
            lines.append(f"{escape(model.setting)}: {escape(model.name)}: {state}")
        return "\n".join(lines)

    def _show_problem(self, text: str) -> None:
        panel = self.query_one("#problem", Static)
        panel.update(escape(text))
        panel.display = True
        panel.scroll_visible()

    # -- downloading -----------------------------------------------------------------

    def action_download(self) -> None:
        assert self.readiness is not None
        model = self.readiness.missing[0]
        self.app.push_screen(
            ConfirmDownloadScreen(model, self.readiness.address), lambda ok: self._download_confirmed(model, ok)
        )

    def _download_confirmed(self, model: ModelReadiness, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self.query_one("#problem").display = False
        self.downloading = model.name
        panel = self.query_one("#download")
        panel.border_title = f"Downloading {model.name}"
        panel.display = True
        self.query_one("#bar", ProgressBar).update(total=None, progress=0)
        self.query_one("#download-detail", Static).update("asking the server…")
        self._set_view(DOWNLOADING)
        pull_in_background(self.config, model.name, self)

    def on_pull_update(self, message: PullUpdate) -> None:
        progress = message.progress
        bar = self.query_one("#bar", ProgressBar)
        if progress.total:
            done = progress.completed or 0
            bar.update(total=progress.total, progress=done)
            detail = f"{format_bytes(done)} of {format_bytes(progress.total)} · {progress.status}"
        else:
            detail = progress.status
        self.query_one("#download-detail", Static).update(escape(detail))

    def on_pull_ended(self, message: PullEnded) -> None:
        self.downloading = None
        if message.error is not None:
            self.query_one("#download").display = False
            self._show_problem(f"{message.error}\n\nPress d to try again.")
            self._set_view(DOWNLOAD_FAILED)
            return
        self.query_one("#download").display = False
        self.notify(f"{message.model} is downloaded.")
        self.action_check()

    # -- moving on -------------------------------------------------------------------

    def action_start(self) -> None:
        self.app.config = self.config  # type: ignore[attr-defined]
        assert self.config.mailbox is not None
        self.app.show_screen(ProgressScreen(RunInputs.from_config(self.config, self.config.mailbox)))  # type: ignore[attr-defined]

    def action_back(self) -> None:
        self.app.show_screen(self.back(answers_from_config(self.config)))  # type: ignore[attr-defined]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "start":
            return self.view == READY
        if action == "download":
            return self.view in (MISSING, DOWNLOAD_FAILED)
        if action == "check":
            return self.view in (PROBLEM, MISSING, DOWNLOAD_FAILED)
        if action == "back":
            return self.view != DOWNLOADING
        return True

    def _set_view(self, view: str) -> None:
        self.view = view
        self.query_one("#status", Static).update(STATUS[view])
        self.refresh_bindings()
