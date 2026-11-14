"""The review screen: the flight list of a finished run, and the user's
decisions on it.

Thin by design: what to show comes from ``output.flight_view`` (order,
rows, filters, totals, detail, the dropped and unreadable lists), and every
decision goes through ``output.decide.Reviewer``, which saves it in the
store at once and checks that flight again. The screen holds only view
state: the filters and which flight is selected. Each write is followed by
``session.touch``, as the commands do.

Keys: space or ``k`` toggles keep; ``enter`` opens the selected flight's
source emails; ``c`` corrects its date, flight number, origin or
destination; ``u`` clears its decision (asking first if it has a
correction); ``f``, ``n`` and ``y`` set the flagged, not-kept and year
filters; ``d`` and ``x`` show the dropped bookings and the unreadable
emails; ``e`` moves on to export; ``q`` quits. The sub-views close with
``escape``.

Only the changed row is redrawn after a decision, unless the flight moves
(a correction changed its place in the order) or leaves the filtered view;
then the table is refilled and the cursor follows the flight.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Label, Static

from ...config import Config
from ...engine import session
from ...engine.report import RunReport
from ...flights.decisions import InvalidCorrection, ReviewedFlight
from ...output import flight_view
from ...output.decide import Reviewer
from ...output.sources import UNAVAILABLE
from ...store import Store

GROUP_STYLES = {flight_view.BLOCKING: "red", flight_view.ADVISORY: "yellow", flight_view.NO_ATTENTION: ""}
REASONS_COLUMN = flight_view.COLUMNS.index("reasons")
FLIGHT_ACTIONS = {"toggle_keep", "open_emails", "correct", "reset", "toggle_flagged", "toggle_not_kept", "next_year"}


def styled_row(reviewed: ReviewedFlight) -> list[str | Text]:
    cells: list[str | Text] = list(flight_view.flight_row(reviewed))
    style = GROUP_STYLES[flight_view.attention_group(reviewed)]
    cells[REASONS_COLUMN] = Text(str(cells[REASONS_COLUMN]), style=style)
    return cells


@dataclass(frozen=True)
class ReviewView:
    """The review's view state, kept while the export screen is shown so
    that going back restores it: the filters and the selected flight's key."""

    filters: flight_view.Filters
    selected: str | None


class ReviewScreen(Screen):
    BINDINGS = [
        Binding("space", "toggle_keep", "Keep/drop"),
        Binding("k", "toggle_keep", "Keep/drop", show=False),
        Binding("enter", "open_emails", "Emails", show=False),
        Binding("c", "correct", "Correct"),
        Binding("u", "reset", "Undo decision"),
        Binding("f", "toggle_flagged", "Flagged"),
        Binding("n", "toggle_not_kept", "Not kept"),
        Binding("y", "next_year", "Year"),
        Binding("d", "show_dropped", "Dropped"),
        Binding("x", "show_failures", "Unreadable"),
        Binding("e", "export", "Export"),
        Binding("q", "app.quit", "Quit"),
    ]

    DEFAULT_CSS = """
    ReviewScreen { layout: vertical; }
    #totals { text-style: bold; padding: 0 1; }
    #filters { padding: 0 1; color: $text-muted; }
    #flights { height: 1fr; }
    #empty { height: 1fr; padding: 1 2; display: none; }
    #detail-box { height: 40%; min-height: 8; border: round $primary; padding: 0 1; }
    """

    def __init__(self, config: Config, report: RunReport | None, view: ReviewView | None = None) -> None:
        super().__init__()
        self.config = config
        self.store_path: Path = config.store
        self.report = report
        self.filters = view.filters if view is not None else flight_view.Filters()
        self.follow = view.selected if view is not None else None  # the flight to select when the table fills
        self.reviewer: Reviewer | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="totals")
        yield Static("", id="filters")
        yield DataTable(id="flights", cursor_type="row", zebra_stripes=True)
        yield Static("", id="empty")
        with VerticalScroll(id="detail-box"):
            yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#detail-box").border_title = "Selected flight (enter: its emails)"
        with Store(self.store_path) as store:
            self.reviewer = Reviewer.load(self.config, store)
        table = self.query_one("#flights", DataTable)
        table.add_columns(*flight_view.COLUMNS)
        if not self.reviewer.flights:
            self._show_empty()
            self.refresh_bindings()
            return
        self._fill(follow=self.follow)
        table.focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in FLIGHT_ACTIONS:
            # Nothing to act on in an empty flight list: those keys are hidden.
            return self.reviewer is None or bool(self.reviewer.flights)
        return True

    # -- drawing -----------------------------------------------------------------

    @property
    def flights(self) -> list[ReviewedFlight]:
        assert self.reviewer is not None
        return self.reviewer.flights

    def _show_empty(self) -> None:
        self.query_one("#flights").display = False
        self.query_one("#detail-box").display = False
        empty = self.query_one("#empty", Static)
        thoroughness = self.report.thoroughness if self.report is not None else self.config.thoroughness
        lines = flight_view.empty_advice(thoroughness)
        failures = len(self.reviewer.flight_list.failures) if self.reviewer else 0
        lines.append(f"Emails that could not be read: {failures} (press x to list them).")
        empty.update(escape("\n\n".join(lines)))
        empty.display = True
        self._draw_totals()

    def _fill(self, follow: str | None = None) -> None:
        """Refill the table from the filters; the cursor goes to ``follow``
        (a flight key) if it is shown, else stays at the same row number."""
        table = self.query_one("#flights", DataTable)
        row = table.cursor_row
        shown = flight_view.visible(self.flights, self.filters)
        table.clear()
        for reviewed in shown:
            table.add_row(*styled_row(reviewed), key=reviewed.key)
        keys = [reviewed.key for reviewed in shown]
        if follow in keys:
            row = keys.index(follow)
        if shown:
            table.move_cursor(row=min(max(row, 0), len(shown) - 1))
        self._draw_totals(len(shown))
        self._draw_detail()

    def _draw_totals(self, shown: int | None = None) -> None:
        flights = self.flights if self.reviewer else []
        self.query_one("#totals", Static).update(escape(flight_view.totals(flights).line()))
        if shown is None:
            shown = len(flight_view.visible(flights, self.filters))
        self.query_one("#filters", Static).update(
            escape(f"Showing: {self.filters.describe()} ({shown:,} of {len(flights):,} flights)")
            + ("" if shown or not flights else f" · {flight_view.NO_MATCH}")
        )

    def _selected(self) -> ReviewedFlight | None:
        table = self.query_one("#flights", DataTable)
        if self.reviewer is None or not table.display or table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return self.reviewer.get(str(key))

    def _draw_detail(self) -> None:
        reviewed = self._selected()
        reference = self.reviewer.flight_list.reference if self.reviewer else None
        text = "\n".join(flight_view.detail_lines(reviewed, reference)) if reviewed else "No flight selected."
        self.query_one("#detail", Static).update(escape(text))

    def on_data_table_row_highlighted(self, message: DataTable.RowHighlighted) -> None:
        self._draw_detail()

    def on_data_table_row_selected(self, message: DataTable.RowSelected) -> None:
        self.action_open_emails()

    def _changed(self, before: ReviewedFlight, after: ReviewedFlight) -> None:
        """Show a decision on ``before``'s flight: redraw its row in place,
        or refill when it moves or leaves the filtered view."""
        table = self.query_one("#flights", DataTable)
        if flight_view.sort_key(before) == flight_view.sort_key(after) and self.filters.matches(after):
            for column, cell in zip(table.columns, styled_row(after)):
                table.update_cell(after.key, column, cell)
            self._draw_totals(table.row_count)
            self._draw_detail()
        else:
            self._fill(follow=after.key)

    def _decide(self, decision: Callable[[Reviewer, Store], ReviewedFlight]) -> ReviewedFlight:
        """Run a decision against the store, then record the session as active."""
        assert self.reviewer is not None
        with Store(self.store_path) as store:
            reviewed = decision(self.reviewer, store)
            session.touch(store)
        return reviewed

    # -- decisions ---------------------------------------------------------------

    def action_toggle_keep(self) -> None:
        before = self._selected()
        if before is None:
            return
        self._changed(before, self._decide(lambda reviewer, store: reviewer.toggle_keep(store, before.key)))

    def action_correct(self) -> None:
        before = self._selected()
        if before is None:
            return

        def save(values: tuple[str, str, str, str]) -> str | None:
            try:
                after = self._decide(lambda reviewer, store: reviewer.correct(store, before.key, *values))
            except InvalidCorrection as exc:
                return f"{exc}; nothing was saved."
            self._changed(before, after)
            return None

        self.app.push_screen(CorrectionScreen(before, save))

    def action_reset(self) -> None:
        before = self._selected()
        if before is None or before.decision is None:
            return

        def reset() -> None:
            self._changed(before, self._decide(lambda reviewer, store: reviewer.reset(store, before.key)))

        if before.decision.corrected:
            found = "; ".join(f"{label} {old} -> {new}" for label, old, new in flight_view.corrections(before))
            question = f"Clear this flight's decision, including its corrections ({found})?"
            self.app.push_screen(ConfirmScreen(question), lambda yes: reset() if yes else None)
        else:
            reset()

    # -- filters -----------------------------------------------------------------

    def _set_filters(self, **changes: object) -> None:
        if self.reviewer is None or not self.flights:
            return
        selected = self._selected()
        current = self.filters
        self.filters = flight_view.Filters(
            flagged=bool(changes.get("flagged", current.flagged)),
            not_kept=bool(changes.get("not_kept", current.not_kept)),
            year=changes["year"] if "year" in changes else current.year,  # type: ignore[arg-type]
        )
        self._fill(follow=selected.key if selected else None)

    def action_toggle_flagged(self) -> None:
        self._set_filters(flagged=not self.filters.flagged)

    def action_toggle_not_kept(self) -> None:
        self._set_filters(not_kept=not self.filters.not_kept)

    def action_next_year(self) -> None:
        self._set_filters(year=flight_view.next_year(self.filters.year, flight_view.years(self.flights)))

    # -- other views -------------------------------------------------------------

    def action_open_emails(self) -> None:
        reviewed = self._selected()
        if reviewed is not None:
            self.app.push_screen(EmailScreen(reviewed, self.store_path))

    def action_show_dropped(self) -> None:
        assert self.reviewer is not None
        lines = flight_view.dropped_lines(self.reviewer.flight_list)
        self.app.push_screen(ListScreen(f"{flight_view.DROPPED_HEADING}: {len(lines)}", lines))

    def action_show_failures(self) -> None:
        assert self.reviewer is not None
        lines = flight_view.failure_lines(self.reviewer.flight_list)
        self.app.push_screen(ListScreen(f"{flight_view.FAILURES_HEADING}: {len(lines)}", lines))

    def action_export(self) -> None:
        selected = self._selected()
        self.app.open_export(ReviewView(self.filters, selected.key if selected else None))  # type: ignore[attr-defined]


class EmailScreen(Screen):
    """The source emails of one flight: each one's date and subject, then its saved text."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    DEFAULT_CSS = """
    EmailScreen VerticalScroll { padding: 0 1; }
    EmailScreen #heading { text-style: bold; }
    EmailScreen .source { text-style: bold; margin-top: 1; color: $accent; }
    """

    def __init__(self, reviewed: ReviewedFlight, store_path: Path) -> None:
        super().__init__()
        self.reviewed = reviewed
        self.store_path = store_path

    def compose(self) -> ComposeResult:
        segment = self.reviewed.flight.segment
        sources = self.reviewed.flight.sources
        with Store(self.store_path) as store:
            texts = [store.message_text(source.message_id) for source in sources]
        heading = (
            f"{segment.date}  {segment.flight_number or flight_view.NO_NUMBER}  {segment.origin}-{segment.destination}"
            f" · {len(sources)} source email{'s' if len(sources) != 1 else ''}"
        )
        with VerticalScroll():
            yield Static(escape(heading), id="heading")
            for number, (source, text) in enumerate(zip(sources, texts), 1):
                yield Static(
                    escape(f"[{number}/{len(sources)}]  {source.message_date}  {source.subject}"), classes="source"
                )
                yield Static(escape(text if text is not None else UNAVAILABLE))
        yield Footer()


class ListScreen(Screen):
    """A titled list, one line per item (dropped bookings, unreadable emails)."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    DEFAULT_CSS = """
    ListScreen VerticalScroll { padding: 0 1; }
    ListScreen #title { text-style: bold; margin-bottom: 1; }
    """

    def __init__(self, title: str, lines: list[str]) -> None:
        super().__init__()
        self.title_text = title
        self.lines = lines

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(escape(self.title_text), id="title")
            yield Static(escape("\n".join(self.lines) or "None."))
        yield Footer()


class CorrectionScreen(ModalScreen[None]):
    """The four correctable fields, prefilled; ``enter`` saves through
    ``save``, which returns a message when the values are refused."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Cancel")]

    DEFAULT_CSS = """
    CorrectionScreen { align: center middle; }
    CorrectionScreen > Vertical {
        width: 64; height: auto; border: round $primary; padding: 1 2; background: $surface;
    }
    CorrectionScreen Horizontal { height: 3; }
    CorrectionScreen Label { width: 20; padding: 1 0; }
    CorrectionScreen Input { width: 1fr; }
    CorrectionScreen #message { color: $error; margin-top: 1; }
    """

    FIELDS = (("date", "Date (YYYY-MM-DD)"), ("flight_number", "Flight number"), ("origin", "Origin"),
              ("destination", "Destination"))

    def __init__(self, reviewed: ReviewedFlight, save: Callable[[tuple[str, str, str, str]], str | None]) -> None:
        super().__init__()
        self.reviewed = reviewed
        self.save = save

    def compose(self) -> ComposeResult:
        segment = self.reviewed.flight.segment
        with Vertical() as box:
            box.border_title = "Correct this flight (enter saves, escape cancels)"
            for name, label in self.FIELDS:
                with Horizontal():
                    yield Label(label)
                    yield Input(getattr(segment, name), id=name)
            yield Static("", id="message")

    def on_input_submitted(self, message: Input.Submitted) -> None:
        values = tuple(self.query_one(f"#{name}", Input).value for name, _ in self.FIELDS)
        problem = self.save(values)  # type: ignore[arg-type]
        if problem is None:
            self.dismiss(None)
        else:
            self.query_one("#message", Static).update(escape(problem))


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Static { width: 64; height: auto; border: round $warning; padding: 1 2; background: $surface; }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        yield Static(escape(self.question) + "\n\n[b]y[/b] yes · [b]n[/b] no")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)
