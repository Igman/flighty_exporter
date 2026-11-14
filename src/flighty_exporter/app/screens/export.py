"""The export screen: writes the import file for Flighty and shows the
manual-add checklist.

It holds no export logic: the plan comes from ``export.plan_store_export``,
the files from ``session.export_from_store`` (which records them with the
session, as the ``export`` command does), ticks go through
``session.tick_manual_add`` (which refreshes the manual-add list on disk),
and the import steps are ``export.FLIGHTY_IMPORT_STEPS``. What the screen
owns is view state: what it is doing, the checklist it last drew and the
selected row. It writes only local files; nothing is sent anywhere.

Keys: ``e`` (or Enter) exports; ``o`` shows the import file in Finder
(macOS); space ticks or unticks the selected manual-add flight; ``r``
retries after the reference data could not be loaded; ``b`` goes back to
the review; ``q`` quits.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from ...config import Config
from ...engine import session
from ...output import export
from ...output.openflights import ReferenceDataError
from ...store import Store
from ..paths import path_text

# What the screen is doing.
PLANNING = "planning"
READY = "ready"  # the plan is shown; nothing exported yet
NOTHING_KEPT = "nothing kept"
EXPORTING = "exporting"
EXPORTED = "exported"
FAILED = "failed"  # the plan or the export could not be made (reference data, store)

STATUS = {
    PLANNING: "Working out what the export would write…",
    READY: "Press e to write the import file for Flighty. Nothing is sent anywhere; the files stay on this computer.",
    NOTHING_KEPT: "No flight is kept, so there is nothing to export. Press b to go back to the review.",
    EXPORTING: "Writing the files…",
    EXPORTED: "Export written. Press e to write it again after a change, b to go back to the review.",
    FAILED: "The export could not be prepared: see below.",
}

CHECK_HELP = "Space ticks the selected flight once you have entered it in Flighty; ticks are saved at once."


class ExportScreen(Screen):
    """The export for the store and settings in ``config``; plans on mount."""

    BINDINGS = [
        Binding("e", "export", "Export"),
        Binding("enter", "export", "Export", show=False, priority=True),
        Binding("o", "reveal", "Show in Finder"),
        Binding("space", "toggle", "Tick"),
        Binding("r", "retry", "Retry"),
        Binding("b", "back", "Back to review"),
        Binding("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    ExportScreen { layout: vertical; }
    #body { height: 1fr; padding: 0 1; }
    #status { margin: 1 0 0 0; }
    .panel { border: round $primary; height: auto; padding: 0 1; }
    #plan { margin-top: 1; display: none; }
    #problem { border: round $error; display: none; }
    #written { border: round $success; display: none; }
    #steps { display: none; }
    #checklist-panel { height: auto; display: none; }
    #checklist { height: auto; max-height: 16; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.view = PLANNING
        self.entries: list[export.ManualAddEntry] = []
        self.import_path: Path | None = None
        self.manual_add_path: Path | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="body"):
            yield Static(STATUS[PLANNING], id="status")
            yield Static("", id="plan", classes="panel")
            yield Static("", id="problem", classes="panel")
            yield Static("", id="written", classes="panel")
            yield Static(escape(export.FLIGHTY_IMPORT_STEPS), id="steps", classes="panel")
            with VerticalScroll(id="checklist-panel", classes="panel"):
                yield Static("", id="checklist-count")
                yield DataTable(id="checklist", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#plan").border_title = "Plan"
        self.query_one("#problem").border_title = "Problem"
        self.query_one("#written").border_title = "Import file"
        self.query_one("#steps").border_title = "Importing into Flighty"
        self.query_one("#checklist-panel").border_title = "Add by hand in Flighty"
        table = self.query_one("#checklist", DataTable)
        table.add_column("Done", key="done")
        table.add_column("Date", key="date")
        table.add_column("Route", key="route")
        table.add_column("Flight", key="flight")
        table.add_column("Reason", key="reason")
        self.plan()

    # -- work (on a thread: the reference data may need downloading) ------------------

    def plan(self) -> None:
        self._set_view(PLANNING)
        for panel in ("#plan", "#problem", "#written", "#steps"):
            self.query_one(panel).display = False
        self.import_path = None
        self._plan()

    @work(thread=True, exclusive=True, group="export")
    def _plan(self) -> None:
        try:
            with Store(self.config.store) as store:
                plan = export.plan_store_export(
                    self.config, store, self.config.export_data_dir, self.config.export_data_url
                )
        except (ReferenceDataError, export.ExportError, OSError) as exc:
            self.app.call_from_thread(self._failed, str(exc))
            return
        self.app.call_from_thread(self._show_plan, plan)

    @work(thread=True, exclusive=True, group="export")
    def _export(self) -> None:
        try:
            with Store(self.config.store) as store:
                summary = session.export_from_store(self.config, store, self.config.export_output_dir)
        except (ReferenceDataError, export.ExportError, OSError) as exc:
            self.app.call_from_thread(self._failed, str(exc))
            return
        self.app.call_from_thread(self._show_export, summary)

    # -- drawing ------------------------------------------------------------------------

    def _show_plan(self, plan: export.ExportPlan) -> None:
        kept = len(plan.exported) + len(plan.manual_add)
        self.query_one("#plan", Static).update(escape("\n".join(export.plan_sentences(plan))))
        self.query_one("#plan").display = True
        self.entries = list(plan.manual_add)
        if kept == 0:
            self._set_view(NOTHING_KEPT)
            self.query_one("#checklist-panel").display = False
            return
        self._draw_checklist()
        self._set_view(READY)

    def _show_export(self, summary: export.ExportSummary) -> None:
        self.import_path = summary.import_path
        self.manual_add_path = summary.manual_add_path
        self.entries = list(summary.manual_add_entries)
        self.query_one("#plan", Static).update(escape(export.export_sentence(summary)))
        self.query_one("#problem").display = False
        written = self.query_one("#written", Static)
        if summary.import_path is None:
            text = "No flight qualifies for the import file, so none was written."
        else:
            text = f"Written to:\n{path_text(summary.import_path.resolve())}\n\n"
            if export.reveal_command(summary.import_path) is not None:
                text += "Press o to show it in Finder."
            else:
                text += f"Open this folder in your file manager to find it:\n{path_text(summary.import_path.resolve().parent)}"
        if summary.manual_add_path is not None:
            text += f"\n\nManual-add list:\n{path_text(summary.manual_add_path.resolve())}"
        written.update(escape(text))
        written.display = True
        self.query_one("#steps").display = summary.import_path is not None
        self._draw_checklist()
        self._set_view(EXPORTED)
        written.scroll_visible()

    def _failed(self, message: str) -> None:
        panel = self.query_one("#problem", Static)
        panel.update(escape(f"error: {message}\n\nNothing was exported. Press r to retry once it is fixed, "
                            "b to go back to the review, or q to quit."))
        panel.display = True
        self._set_view(FAILED)

    def _draw_checklist(self, keep_row: int | None = None) -> None:
        panel = self.query_one("#checklist-panel")
        panel.display = True
        table = self.query_one("#checklist", DataTable)
        table.clear()
        count = self.query_one("#checklist-count", Static)
        if not self.entries:
            table.display = False
            count.update("No flight needs adding by hand: every kept flight goes in the import file.")
            return
        table.display = True
        for index, entry in enumerate(self.entries):
            table.add_row(
                Text("[x]" if entry.ticked else "[ ]", style="bold green" if entry.ticked else ""),
                Text(entry.date.isoformat()),
                Text(f"{entry.origin} → {entry.destination}"),
                Text(entry.flight_number or "—"),
                Text(entry.reason),
                key=str(index),
            )
        ticked = sum(1 for entry in self.entries if entry.ticked)
        count.update(escape(f"Ticked {ticked} of {len(self.entries)}. {CHECK_HELP}"))
        if keep_row is not None:
            table.move_cursor(row=keep_row)
        table.focus()

    def _set_view(self, view: str) -> None:
        self.view = view
        self.query_one("#status", Static).update(escape(STATUS[view]))
        self.refresh_bindings()

    # -- keys -----------------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "export":
            return self.view in (READY, EXPORTED)
        if action == "reveal":
            return self.view == EXPORTED and self.import_path is not None
        if action == "toggle":
            return self.view in (READY, EXPORTED) and bool(self.entries)
        if action == "retry":
            return self.view == FAILED
        return True

    def action_export(self) -> None:
        self._set_view(EXPORTING)
        self._export()

    def action_reveal(self) -> None:
        if self.import_path is None:
            return
        if export.reveal(self.import_path):
            self.notify("Shown in Finder.")
        else:
            self.notify(f"Cannot show files on this system; the file is at {self.import_path.resolve()}",
                        severity="warning", timeout=10)

    def action_toggle(self) -> None:
        table = self.query_one("#checklist", DataTable)
        row = table.cursor_row
        if not 0 <= row < len(self.entries):
            return
        entry = self.entries[row]
        with Store(self.config.store) as store:
            _, problems = session.tick_manual_add(store, entry.flight_key, not entry.ticked)
        self.entries[row] = replace(entry, ticked=not entry.ticked)
        self._draw_checklist(keep_row=row)
        for problem in problems:
            self.notify(problem, severity="warning", timeout=10)

    def action_retry(self) -> None:
        self.plan()

    def action_back(self) -> None:
        self.app.back_to_review()  # type: ignore[attr-defined]

    def action_quit(self) -> None:
        self.app.exit(0)
