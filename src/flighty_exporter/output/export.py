"""Export: turn the edited review table into the import file for Flighty and a
manual-add list.

The review CSV is the source of truth, including any field the user corrected
in a spreadsheet. Columns are located by header name, so a file re-saved by a
spreadsheet (other line endings or quoting, a semicolon or tab field
separator, reordered or extra columns, blank trailing rows) still reads. Only keep, date, flight_number, origin and
destination are used; message_date and message_subject, when present, are
copied to the manual-add list as the source reference.

Each data row is one of:

- blank (every cell empty): ignored;
- not kept (keep is no / n / false / 0 / empty);
- invalid: keep is not a recognised value, or a kept row's date is not a real
  ``YYYY-MM-DD`` date, or its origin or destination is empty. Reported with
  its row number and excluded from both outputs; never silently fixed;
- manual-add: kept and valid, but Flighty cannot match it (no flight number,
  dated before 2006) or the import format cannot express it (see
  ``openflights.OpenFlights.unmatched``);
- exported: written to the import file.

Row numbers are spreadsheet row numbers: the header is row 1.

Two sources (the command line picks one, see ``cli.cmd_export``):

- ``run_export``: the review CSV, as above. Given the store, the values the
  user changed in the CSV (compared with what review wrote in each row) are
  first recorded there as decisions, matched by the flight_id column
  (``record_decisions``); rows without one record nothing. Each row that
  names a flight of the store is then exported as the store has that flight
  with its decisions applied, so the table never exports a stale view of a
  flight decided elsewhere. With no store the CSV is exported exactly as
  before.
- ``run_store_export``: the store's flight list with the saved decisions
  applied (``review.build_flight_list``), turned into the rows the review
  table would hold and read the same way, so both give the same files.

The manual-add list's last column, done, shows the store's manual-add tick
for the flight (set by ``mark-added``).

For the export screen and ``export --dry-run``: ``plan_store_export`` says
what ``run_store_export`` would write without writing anything (the latter
writes exactly that plan); ``refresh_manual_add`` rewrites a written
manual-add list with the current ticks only; ``reveal`` shows a file in the
system file manager (macOS only); ``FLIGHTY_IMPORT_STEPS`` is the verified
import procedure.
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import Config
from ..flights.decisions import (
    CORRECTABLE, Corrections, InvalidCorrection, ReviewDecision, ReviewedFlight, is_date, keep_override,
    resolve_key, typed_corrections,
)
from ..flights.validate import normalise_airport, normalise_flight_number
from ..store import Store
from ..tables import write_csv
from . import openflights as import_format
from .review import (
    DECISIONS_DISCARDED, EDITABLE_COLUMNS, FlightList, build_flight_list, discard_stale_decisions, written_values,
)

MANUAL_ADD_NAME = "manual-add.csv"
MANUAL_ADD_COLUMNS = (
    "date", "origin", "destination", "flight_number", "reason", "message_date", "message_subject", "done",
)
REQUIRED_COLUMNS = EDITABLE_COLUMNS

KEEP_YES = frozenset({"yes", "y", "true", "1", "x"})
KEEP_NO = frozenset({"no", "n", "false", "0", ""})

# Flighty: aviation records before 2006 do not exist, so it cannot match these.
FIRST_MATCHABLE_DATE = date(2006, 1, 1)

# Field separators a spreadsheet may re-save the review table with; the first wins a tie.
_DELIMITERS = (",", ";", "\t")



class ExportError(Exception):
    pass


# A data row of the review table: its spreadsheet row number and its cells by
# column name (lowercased, values stripped).
ReviewRow = tuple[int, dict[str, str]]


@dataclass
class KeptRow:
    row_number: int
    flight: import_format.Flight
    message_date: str
    message_subject: str
    flight_id: str = ""


@dataclass(frozen=True)
class ManualAddEntry:
    """A flight of the manual-add list: what the list shows, the flight's key
    (for ticking it with ``Store.set_added``) and whether it is ticked."""

    flight_key: str
    date: date
    origin: str
    destination: str
    flight_number: str
    reason: str
    message_date: str = ""
    message_subject: str = ""
    ticked: bool = False


@dataclass
class ExportPlan:
    """What an export would do, before anything is written: the flights for
    the import file, the manual-add list, and how many rows were not kept or
    rejected as invalid. ``run_store_export`` writes exactly this."""

    store_path: Path | None = None
    rows: int = 0
    not_kept: int = 0
    invalid: list[tuple[int, str]] = field(default_factory=list)
    exported: list[import_format.Flight] = field(default_factory=list)  # in date order
    manual_add: list[ManualAddEntry] = field(default_factory=list)  # in date order
    reference: import_format.OpenFlights | None = field(default=None, repr=False)  # loaded if needed


@dataclass
class ExportSummary:
    review_path: Path
    rows: int = 0
    not_kept: int = 0
    invalid: list[tuple[int, str]] = field(default_factory=list)
    exported: int = 0
    import_path: Path | None = None
    manual_add: int = 0
    manual_add_path: Path | None = None
    store_path: Path | None = None  # the store decisions were read from or recorded in, None if none
    from_store: bool = False  # exported from the store's flight list, not from the review table
    # Exported from the store because the review table at this path is as
    # review wrote it (it holds no edits), None if no such table was looked at.
    unedited_table: Path | None = None
    decisions_recorded: int = 0
    decisions_discarded: int = 0
    # The manual-add list's flights, with their keys and ticks, as written.
    manual_add_entries: list[ManualAddEntry] = field(default_factory=list)


def run_export(
    review_path: Path,
    output_dir: Path,
    data_dir: Path,
    data_url: str,
    now: datetime | None = None,
    store: Store | None = None,
    config: Config | None = None,
) -> ExportSummary:
    """Export from the review table at ``review_path``. With a ``store`` (and
    its ``config``), the values the user changed in the table are first
    recorded there as decisions (see ``record_decisions``), each row naming
    a flight of the store is exported as the store now has that flight, and
    the manual-add list shows the store's ticks."""
    summary = ExportSummary(review_path)
    rows = read_review(review_path)
    decisions: dict[str, ReviewDecision] = {}
    if store is not None and config is not None:
        summary.store_path = store.path
        summary.decisions_recorded, summary.decisions_discarded, rows = record_decisions(
            config, store, review_path, rows
        )
        decisions = store.review_decisions()
    return _export(_kept_rows(rows, summary), summary, output_dir, data_dir, data_url, now, decisions)


def run_store_export(
    config: Config,
    store: Store,
    output_dir: Path,
    data_dir: Path,
    data_url: str,
    now: datetime | None = None,
    unedited_table: Path | None = None,
) -> ExportSummary:
    """Export from the store's flight list with the saved decisions applied:
    the rows the review table would hold, read the same way. Writes what
    ``plan_store_export`` returns. ``unedited_table``: the review table that
    was found as review wrote it, so the store was used (for the summary)."""
    plan, flight_list = _store_plan(config, store, data_dir, data_url)
    summary = ExportSummary(
        config.review_output, store_path=store.path, from_store=True, unedited_table=unedited_table
    )
    summary.decisions_discarded = discard_stale_decisions(store, flight_list)
    summary.rows, summary.not_kept, summary.invalid = plan.rows, plan.not_kept, plan.invalid
    return _write(plan, summary, output_dir, now)


def plan_store_export(config: Config, store: Store, data_dir: Path, data_url: str) -> ExportPlan:
    """What ``run_store_export`` would write, without writing anything (the
    store is not changed either). Loads the reference data when a kept
    flight needs it; raises ReferenceDataError if that fails."""
    return _store_plan(config, store, data_dir, data_url)[0]


def _store_plan(config: Config, store: Store, data_dir: Path, data_url: str) -> tuple[ExportPlan, FlightList]:
    flight_list = build_flight_list(config, store)
    rows = [(number, _cells(reviewed)) for number, reviewed in enumerate(flight_list.flights, 2)]
    counts = ExportSummary(config.review_output)
    kept = _kept_rows(rows, counts)
    plan = _plan(kept, data_dir, data_url, store.review_decisions())
    plan.store_path, plan.rows, plan.not_kept, plan.invalid = store.path, counts.rows, counts.not_kept, counts.invalid
    return plan, flight_list


def record_decisions(
    config: Config, store: Store, review_path: Path, rows: list[ReviewRow]
) -> tuple[int, int, list[ReviewRow]]:
    """Record as decisions what the user changed in the review table at
    ``review_path``, and return the rows to export.

    A row whose ``flight_id`` names a flight of the store's list is compared,
    value by value (keep, date, flight number, origin, destination,
    normalised), with what review wrote in that row (``Store.review_rows``);
    for a row review recorded nothing for (a table written before this was
    recorded, or made by hand), with the flight as the store now shows it.
    Only a value that differs is the user's: it is recorded on top of the
    flight's existing decision (a keep override, or a correction; a value
    typed back to what consolidation produced clears that correction). A
    value equal to what was written leaves the decision as it is, except
    that on a row whose fields the user corrected, the keep they saw and
    left stands even if the correction changes the pre-set keep (unless a
    keep choice was saved for the flight since). The recorded row is then
    remembered as the row's new written values, so a later edit of the same
    table is compared with it. Rows with no known flight_id, an unrecognised
    keep value, an invalid date or an empty airport record nothing.

    The rows returned are ``rows`` with each recordable row's values replaced
    by the flight as the store now has it (decisions applied), so a flight
    the user did not edit is exported as the store has it, not as the table
    showed it when written. Returns (flights whose decision changed,
    decisions discarded because their flight is no longer produced, rows)."""
    before = store.review_decisions()
    flight_list = build_flight_list(config, store)
    flights = {reviewed.key: reviewed for reviewed in flight_list.flights}
    written = store.review_rows(review_path)
    recordable: set[str] = set()
    # key -> (the row's values, the names of the values the user changed, the values written)
    edits: dict[str, tuple[_Values, set[str], _Values]] = {}
    for _, cells in rows:
        key = cells.get("flight_id", "")
        if key not in flights:
            continue
        values = _row_values(flights[key], cells)
        if values is None:
            continue
        recordable.add(key)
        baseline = _written(written[key]) if key in written else _shown(flights[key])
        changed = {name for name, mine, was in zip(EDITABLE_COLUMNS, values, baseline) if mine != was}
        if changed:
            edits[key] = (values, changed, baseline)
        else:
            edits.pop(key, None)  # a later row for the same flight wins
    for key, (values, changed, _) in edits.items():
        reviewed = flights[key]
        found = typed_corrections(reviewed.original, *values[1:])
        existing = reviewed.decision or ReviewDecision()
        store.save_corrections(key, *(
            getattr(found if name in changed else existing, name) for name in CORRECTABLE
        ))
    if edits:
        flight_list = build_flight_list(config, store)
        corrected = {reviewed.key: reviewed for reviewed in flight_list.flights}
        for key, (values, changed, baseline) in edits.items():
            preset = corrected[key].preset_keep
            reviewed = flights[key]
            no_keep_choice = reviewed.decision is None or reviewed.decision.keep is None
            if "keep" in changed:
                store.save_keep(key, keep_override(values[0], preset))
            elif no_keep_choice and reviewed.keep == baseline[0]:
                store.save_keep(key, keep_override(baseline[0], preset))
            store.update_review_row(review_path, key, written_values(_cells_of(values)))
        flight_list = build_flight_list(config, store)
    after = store.review_decisions()
    recorded = sum(1 for key in edits if _choices(before.get(key)) != _choices(after.get(key)))
    discarded = discard_stale_decisions(store, flight_list)
    now = {reviewed.key: reviewed for reviewed in flight_list.flights}
    exported = [
        (number, {**cells, **_cells(now[cells["flight_id"]])}) if cells.get("flight_id", "") in recordable
        else (number, cells)
        for number, cells in rows
    ]
    return recorded, discarded, exported


# A row's keep, date, flight number, origin and destination, normalised.
_Values = tuple[bool, str, str, str, str]


def _row_values(reviewed: ReviewedFlight, cells: dict[str, str]) -> _Values | None:
    """A table row's values, normalised; None if the row cannot be recorded."""
    keep = _keep_value(cells.get("keep", ""))
    if keep is None:
        return None
    try:
        typed_corrections(
            reviewed.original, cells.get("date", ""), cells.get("flight_number", ""),
            cells.get("origin", ""), cells.get("destination", ""),
        )
    except InvalidCorrection:
        return None
    return _normalised(keep, cells.get("date", ""), cells.get("flight_number", ""),
                       cells.get("origin", ""), cells.get("destination", ""))


def _normalised(keep: bool, day: str, flight_number: str, origin: str, destination: str) -> _Values:
    return (keep, day.strip(), normalise_flight_number(flight_number), normalise_airport(origin),
            normalise_airport(destination))


def _written(values: tuple[str, str, str, str, str]) -> _Values:
    """Values recorded as written in a review table row, normalised."""
    return _normalised(_keep_value(values[0]) is True, *values[1:])


def _shown(reviewed: ReviewedFlight) -> _Values:
    """The flight as the store now shows it, normalised."""
    segment = reviewed.flight.segment
    return _normalised(reviewed.keep, segment.date, segment.flight_number, segment.origin, segment.destination)


def _cells_of(values: _Values) -> dict[str, str]:
    """Normalised values as a review table row would hold them."""
    return dict(zip(EDITABLE_COLUMNS, ("yes" if values[0] else "no", *values[1:])))


def _choices(decision: ReviewDecision | None) -> tuple[object, ...]:
    if decision is None:
        return (None,) * 5
    return (decision.keep, decision.date, decision.flight_number, decision.origin, decision.destination)


def _keep_value(value: str) -> bool | None:
    if value.lower() in KEEP_YES:
        return True
    if value.lower() in KEEP_NO:
        return False
    return None


def _cells(reviewed: ReviewedFlight) -> dict[str, str]:
    """A flight of the store's list as the review table's row would hold it."""
    segment, latest = reviewed.flight.segment, reviewed.flight.latest
    return {
        "keep": "yes" if reviewed.keep else "no",
        "date": segment.date,
        "flight_number": segment.flight_number,
        "origin": segment.origin,
        "destination": segment.destination,
        "message_date": latest.message_date,
        "message_subject": latest.subject,
        "flight_id": reviewed.key,
    }


def _export(
    kept: list[KeptRow],
    summary: ExportSummary,
    output_dir: Path,
    data_dir: Path,
    data_url: str,
    now: datetime | None,
    decisions: dict[str, ReviewDecision],
) -> ExportSummary:
    return _write(_plan(kept, data_dir, data_url, decisions), summary, output_dir, now)


def _plan(
    kept: list[KeptRow], data_dir: Path, data_url: str, decisions: dict[str, ReviewDecision]
) -> ExportPlan:
    """Sort the kept rows into the import file and the manual-add list."""
    plan = ExportPlan()
    manual: list[tuple[KeptRow, str]] = []
    candidates: list[KeptRow] = []
    for row in kept:
        reasons = _unmatchable(row.flight)
        if reasons:
            manual.append((row, "; ".join(reasons)))
        else:
            candidates.append(row)

    exported: list[KeptRow] = []
    if candidates:
        plan.reference = import_format.OpenFlights.load(data_dir, data_url)
        for row in candidates:
            reasons = plan.reference.unmatched(row.flight)
            if reasons:
                manual.append((row, "; ".join(reasons)))
            else:
                exported.append(row)
    exported.sort(key=lambda row: (row.flight.date, row.row_number))
    plan.exported = [row.flight for row in exported]

    manual.sort(key=lambda item: (item[0].flight.date, item[0].row_number))
    plan.manual_add = [
        ManualAddEntry(
            _key(row, decisions), row.flight.date, row.flight.origin, row.flight.destination,
            row.flight.flight_number, reason, row.message_date, row.message_subject, _ticked(row, decisions),
        )
        for row, reason in manual
    ]
    return plan


def _unmatchable(flight: import_format.Flight) -> list[str]:
    """Why Flighty cannot match this flight whatever the reference data says."""
    reasons = []
    if not flight.flight_number:
        reasons.append("no flight number")
    if flight.date < FIRST_MATCHABLE_DATE:
        reasons.append(f"dated before {FIRST_MATCHABLE_DATE.year}")
    return reasons


def manual_add_reason(reviewed: ReviewedFlight, reference: import_format.OpenFlights | None) -> str | None:
    """Why the export would put this flight on the manual-add list, None if
    it would not (it is not kept, cannot be exported at all, or goes in the
    import file). Without the reference data, only the reasons that do not
    need it are known."""
    segment = reviewed.flight.segment
    if not reviewed.keep or not is_date(segment.date) or not segment.origin or not segment.destination:
        return None
    flight = import_format.Flight(date.fromisoformat(segment.date), segment.origin, segment.destination,
                                  segment.flight_number)
    reasons = _unmatchable(flight)
    if not reasons and reference is not None:
        reasons = reference.unmatched(flight)
    return "; ".join(reasons) or None


def _write(plan: ExportPlan, summary: ExportSummary, output_dir: Path, now: datetime | None) -> ExportSummary:
    """Write the import file (if any flight goes in it) and the manual-add list of ``plan``."""
    if plan.exported:
        assert plan.reference is not None
        now = now or datetime.now(timezone.utc)
        summary.import_path = plan.reference.write_backup(plan.exported, output_dir, now)
        summary.exported = len(plan.exported)
    summary.manual_add_path = output_dir / MANUAL_ADD_NAME
    write_manual_add(summary.manual_add_path, plan.manual_add)
    summary.manual_add = len(plan.manual_add)
    summary.manual_add_entries = list(plan.manual_add)
    return summary


def write_manual_add(path: Path, entries: list[ManualAddEntry]) -> None:
    """Write the manual-add list at ``path``."""
    write_csv(
        path,
        MANUAL_ADD_COLUMNS,
        [
            {
                "date": entry.date.isoformat(),
                "origin": entry.origin,
                "destination": entry.destination,
                "flight_number": entry.flight_number,
                "reason": entry.reason,
                "message_date": entry.message_date,
                "message_subject": entry.message_subject,
                "done": "yes" if entry.ticked else "",
            }
            for entry in entries
        ],
    )


def refresh_manual_add(store: Store, path: Path) -> list[ManualAddEntry]:
    """Rewrite the manual-add list at ``path`` (written by an earlier export)
    with the store's current ticks, changing nothing else: the import file
    and the flights on the list stay as that export wrote them. Returns the
    list's flights with their keys and ticks. Raises ExportError if the file
    cannot be read as a manual-add list."""
    decisions = store.review_decisions()
    entries = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ExportError(f"cannot read the manual-add list {path}: {exc}") from exc
    for cells in rows:
        day = _parse_date((cells.get("date") or "").strip())
        if day is None:
            raise ExportError(f"the manual-add list {path} has a row without a real date; run export again.")
        row = KeptRow(
            0,
            import_format.Flight(day, cells.get("origin") or "", cells.get("destination") or "",
                                 cells.get("flight_number") or ""),
            cells.get("message_date") or "",
            cells.get("message_subject") or "",
        )
        entries.append(ManualAddEntry(
            _key(row, decisions), day, row.flight.origin, row.flight.destination, row.flight.flight_number,
            cells.get("reason") or "", row.message_date, row.message_subject, _ticked(row, decisions),
        ))
    write_manual_add(path, entries)
    return entries


def _key(row: KeptRow, decisions: dict[str, ReviewDecision]) -> str:
    """The key of the flight this row shows (its flight_id, if it has one)."""
    flight = row.flight
    return row.flight_id or resolve_key(
        decisions, flight.date.isoformat(), flight.flight_number, flight.origin, flight.destination
    )


def _ticked(row: KeptRow, decisions: dict[str, ReviewDecision]) -> bool:
    """Whether the user has ticked this manual-add flight (``mark-added``)."""
    decision = decisions.get(_key(row, decisions))
    return decision is not None and decision.added


def read_review(path: Path) -> list[ReviewRow]:
    """The non-blank data rows of the review table at ``path``."""
    if not path.is_file():
        raise ExportError(f"review table not found: {path}. Run review first, or pass --review PATH.")
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            text = handle.read()
        records = list(csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(text)))
    except UnicodeDecodeError as exc:
        raise ExportError(
            f"review table {path} is not UTF-8 text ({exc.reason}). Save it from the spreadsheet as "
            "\"CSV UTF-8\" and run export again."
        ) from exc
    except (OSError, csv.Error) as exc:
        raise ExportError(f"cannot read review table {path}: {exc}") from exc
    if not records:
        raise ExportError(f"review table {path} is empty: it has no header row.")

    header = [name.strip().lower() for name in records[0]]
    index: dict[str, int] = {}
    for position, name in enumerate(header):
        index.setdefault(name, position)
    missing = [name for name in REQUIRED_COLUMNS if name not in index]
    if missing:
        raise ExportError(
            f"review table {path} has no {', '.join(missing)} column{'s' if len(missing) > 1 else ''} "
            f"(the export needs {', '.join(REQUIRED_COLUMNS)}). Restore the column header from a "
            "freshly written review table."
        )

    rows: list[ReviewRow] = []
    for row_number, record in enumerate(records[1:], 2):
        if not any(cell.strip() for cell in record):
            continue
        rows.append((row_number, {
            name: record[position].strip() if position < len(record) else "" for name, position in index.items()
        }))
    return rows


def _kept_rows(rows: list[ReviewRow], summary: ExportSummary) -> list[KeptRow]:
    """The kept, valid rows; the others are counted in ``summary``."""
    kept: list[KeptRow] = []
    for row_number, cells in rows:
        summary.rows += 1

        def cell(name: str) -> str:
            return cells.get(name, "")

        keep = cell("keep")
        if keep.lower() in KEEP_NO:
            summary.not_kept += 1
            continue
        if keep.lower() not in KEEP_YES:
            summary.invalid.append((row_number, f"keep value {keep!r} is not yes or no"))
            continue

        reasons = []
        raw_date = cell("date")
        origin, destination = normalise_airport(cell("origin")), normalise_airport(cell("destination"))
        flight_date = _parse_date(raw_date)
        if flight_date is None:
            reasons.append(f"date {raw_date!r} is not a real YYYY-MM-DD date")
        if not origin:
            reasons.append("origin is empty")
        if not destination:
            reasons.append("destination is empty")
        if reasons:
            summary.invalid.append((row_number, "; ".join(reasons)))
            continue
        assert flight_date is not None
        flight_number = normalise_flight_number(cell("flight_number"))
        kept.append(
            KeptRow(
                row_number,
                import_format.Flight(flight_date, origin, destination, flight_number),
                cell("message_date"),
                cell("message_subject"),
                cell("flight_id"),
            )
        )
    return kept


def _delimiter(text: str) -> str:
    """The field separator of the review table: the one of comma, semicolon
    and tab under which the header names the most required columns (a
    spreadsheet in a European locale may re-save it with semicolons). Ties,
    including a header that names none, go to the comma, so a genuinely
    missing column is still reported by name."""
    header_line = text.splitlines()[0] if text else ""

    def found(delimiter: str) -> int:
        try:
            header = next(csv.reader([header_line], delimiter=delimiter), [])
        except csv.Error:
            return 0
        names = {name.strip().lower() for name in header}
        return sum(1 for name in REQUIRED_COLUMNS if name in names)

    return max(_DELIMITERS, key=found)


def _parse_date(value: str) -> date | None:
    return date.fromisoformat(value) if is_date(value) else None


def format_summary(summary: ExportSummary) -> str:
    lines = []
    if summary.import_path is not None:
        lines.append(f"Import file written to {summary.import_path}")
    else:
        lines.append("No flight qualifies for the import file; none written.")
    lines.append(f"  flights exported:           {summary.exported}")
    lines.append(f"Manual-add list written to {summary.manual_add_path}")
    lines.append(f"  flights to add by hand:     {summary.manual_add}")
    if summary.from_store:
        lines.append(f"Flight list read from the store {summary.store_path}")
        lines.append("  source of the export:       store (saved review decisions applied)")
        if summary.unedited_table is not None:
            lines.append(f"  review table {summary.unedited_table} is as review wrote it: no edits to record")
    else:
        lines.append(f"Review table read from {summary.review_path}")
        if summary.store_path is not None:
            lines.append("  source of the export:       review table (your edits recorded as decisions; each "
                         "flight the store knows exported with its saved review decisions applied)")
        else:
            lines.append("  source of the export:       review table")
    lines.append(f"  rows:                       {summary.rows}")
    lines.append(f"  not kept:                   {summary.not_kept}")
    lines.append(f"  rejected as invalid:        {len(summary.invalid)}")
    for row_number, reason in summary.invalid:
        lines.append(f"    row {row_number}: {reason}")
    if summary.store_path is not None:
        if not summary.from_store or summary.unedited_table is not None:
            lines.append(f"  decisions recorded from the review table: {summary.decisions_recorded}")
        lines.append(f"  {DECISIONS_DISCARDED}: {summary.decisions_discarded}")
    if summary.invalid:
        lines.append("Fix the rejected rows in the review table and run export again.")
    return "\n".join(lines)


@dataclass(frozen=True)
class PlanFigure:
    """One figure of an export plan, with its wording: ``label`` as
    ``export --dry-run`` prints it, ``sentence`` as the export screen says
    it (None: the screen leaves the figure out)."""

    count: int
    label: str
    sentence: str | None


def plan_figures(plan: ExportPlan) -> list[PlanFigure]:
    """The plan's figures, in order: the one source of the plan's wording."""
    invalid = len(plan.invalid)
    return [
        PlanFigure(len(plan.exported), "flights to export",
                   f"{_plural(len(plan.exported), 'flight')} will be exported to the import file for Flighty."),
        PlanFigure(len(plan.manual_add), "flights to add by hand",
                   f"{_plural(len(plan.manual_add), 'flight')} must be added by hand in Flighty."),
        PlanFigure(plan.not_kept, "not kept",
                   f"{_plural(plan.not_kept, 'flight')} {'is' if plan.not_kept == 1 else 'are'} not kept."),
        PlanFigure(invalid, "rejected as invalid",
                   f"{_plural(invalid, 'flight')} cannot be exported (no date or airport)." if invalid else None),
    ]


def plan_sentences(plan: ExportPlan) -> list[str]:
    """The plan as the export screen says it."""
    return [figure.sentence for figure in plan_figures(plan) if figure.sentence is not None]


def export_sentence(summary: ExportSummary) -> str:
    """What an export wrote, in one sentence (the export screen's)."""
    return (
        f"{_plural(summary.exported, 'flight')} exported; {_plural(summary.manual_add, 'flight')} to add by hand; "
        f"{summary.not_kept} not kept."
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def format_plan(plan: ExportPlan) -> str:
    """What an export from the store would do (``export --from-store
    --dry-run``): the counts, the flights for the import file, and the
    manual-add list with each flight's tick ([x]) and reason."""
    lines = ["Dry run: nothing was written."]
    lines.append(f"Flight list read from the store {plan.store_path}")
    lines += [f"  {figure.label + ':':<28}{figure.count}" for figure in plan_figures(plan)]
    for row_number, reason in plan.invalid:
        lines.append(f"    row {row_number}: {reason}")
    lines.append("Import file would hold:")
    lines += [f"  {_flight_line(f.date, f.flight_number, f.origin, f.destination)}" for f in plan.exported] or [
        "  nothing (no import file would be written)"]
    lines.append("Manual-add list would hold:")
    lines += [
        f"  [{'x' if e.ticked else ' '}] {_flight_line(e.date, e.flight_number, e.origin, e.destination)}  {e.reason}"
        for e in plan.manual_add
    ] or ["  nothing"]
    return "\n".join(lines)


def _flight_line(day: date, flight_number: str, origin: str, destination: str) -> str:
    return f"{day.isoformat()}  {(flight_number or '-').ljust(6)}  {origin}-{destination}"


# The steps for importing into Flighty, saying only what was verified (see
# the README's "Importing into Flighty"); shown on the export screen.
FLIGHTY_IMPORT_STEPS = """\
To import the file into Flighty:
1. In Flighty, go to Settings, Import Flights, and choose OpenFlights.
2. Flighty's import works by sending the file to an address it gives you: send the import file to that address.
3. Flights older than a year need Flighty Pro.
4. Enter each flight on the manual-add list by hand in Flighty: it needs the two airports and the date.

A file generated this way was accepted in November 2026, and a flight Flighty already had was not imported \
twice. Flighty documents support only for files exported by OpenFlights itself, so this could stop working."""


def reveal_command(path: Path, platform: str = sys.platform) -> list[str] | None:
    """The command that shows ``path`` selected in the system file manager,
    or None where that is not supported (only macOS is: ``open -R``)."""
    if platform == "darwin":
        return ["open", "-R", str(path)]
    return None


def reveal(path: Path) -> bool:
    """Show ``path`` in the system file manager. False if this system is not
    supported or the command could not be run: the caller shows the path."""
    command = reveal_command(path)
    if command is None:
        return False
    try:
        completed = subprocess.run(command, check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0
