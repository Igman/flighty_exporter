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
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import Config
from ..flights.decisions import ReviewDecision, ReviewedFlight, is_date, resolve_key
from ..flights.validate import normalise_airport, normalise_flight_number
from ..store import Store
from ..tables import write_csv
from . import openflights as import_format
from .review import DECISIONS_DISCARDED, build_flight_list, discard_stale_decisions

MANUAL_ADD_NAME = "manual-add.csv"
MANUAL_ADD_COLUMNS = (
    "date", "origin", "destination", "flight_number", "reason", "message_date", "message_subject", "done",
)
REQUIRED_COLUMNS = ("keep", "date", "flight_number", "origin", "destination")

KEEP_YES = frozenset({"yes", "y", "true", "1", "x"})
KEEP_NO = frozenset({"no", "n", "false", "0", ""})

# Flighty: aviation records before 2006 do not exist, so it cannot match these.
FIRST_MATCHABLE_DATE = date(2006, 1, 1)

# Field separators a spreadsheet may re-save the review table with; the first wins a tie.
_DELIMITERS = (",", ";", "\t")

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_WHITESPACE = re.compile(r"\s+")


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
    decisions_recorded: int = 0
    decisions_discarded: int = 0


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
    its ``config``), the table's keep values and corrections are first
    recorded there as decisions (see ``record_decisions``), and the
    manual-add list shows the store's ticks."""
    summary = ExportSummary(review_path)
    rows = read_review(review_path)
    decisions: dict[str, ReviewDecision] = {}
    if store is not None and config is not None:
        summary.store_path = store.path
        summary.decisions_recorded, summary.decisions_discarded = record_decisions(config, store, rows)
        decisions = store.review_decisions()
    return _export(_kept_rows(rows, summary), summary, output_dir, data_dir, data_url, now, decisions)


def run_store_export(
    config: Config,
    store: Store,
    output_dir: Path,
    data_dir: Path,
    data_url: str,
    now: datetime | None = None,
) -> ExportSummary:
    """Export from the store's flight list with the saved decisions applied:
    the rows the review table would hold, read the same way."""
    flight_list = build_flight_list(config, store)
    summary = ExportSummary(config.review_output, store_path=store.path, from_store=True)
    summary.decisions_discarded = discard_stale_decisions(store, flight_list)
    rows = [(number, _cells(reviewed)) for number, reviewed in enumerate(flight_list.flights, 2)]
    kept = _kept_rows(rows, summary)
    return _export(kept, summary, output_dir, data_dir, data_url, now, store.review_decisions())


def record_decisions(config: Config, store: Store, rows: list[ReviewRow]) -> tuple[int, int]:
    """Record the review table's keep values and corrections as decisions,
    for each row whose ``flight_id`` names a flight of the store's list: a
    correction for each of date, flight number, origin and destination that
    differs from the flight as consolidation produced it, then a keep
    override where the row's keep differs from the corrected flight's pre-set
    keep. A row's values replace any decision recorded before. Rows with no
    known flight_id, an unrecognised keep value, an invalid date or an empty
    airport record nothing. Returns (flights whose decision changed,
    decisions discarded because their flight is no longer produced)."""
    before = store.review_decisions()
    flights = {reviewed.key: reviewed for reviewed in build_flight_list(config, store).flights}
    wanted: dict[str, tuple[bool, str, str, str, str]] = {}
    for _, cells in rows:
        key = cells.get("flight_id", "")
        keep = _keep_value(cells.get("keep", ""))
        day, origin, destination = cells.get("date", ""), cells.get("origin", ""), cells.get("destination", "")
        if key not in flights or keep is None or not is_date(day) or not origin or not destination:
            continue
        wanted[key] = (keep, day, normalise_flight_number(cells.get("flight_number", "")),
                       normalise_airport(origin), normalise_airport(destination))
    for key, (_, day, number, origin, destination) in wanted.items():
        original = flights[key].original
        store.save_corrections(
            key,
            date=day if day != original.date else None,
            flight_number=number if number != original.flight_number else None,
            origin=origin if origin != original.origin else None,
            destination=destination if destination != original.destination else None,
        )
    flight_list = build_flight_list(config, store)
    corrected = {reviewed.key: reviewed for reviewed in flight_list.flights}
    for key, (keep, *_) in wanted.items():
        store.save_keep(key, keep if keep != corrected[key].preset_keep else None)
    after = store.review_decisions()
    recorded = sum(1 for key in wanted if _choices(before.get(key)) != _choices(after.get(key)))
    return recorded, discard_stale_decisions(store, flight_list)


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

    manual: list[tuple[KeptRow, str]] = []
    candidates: list[KeptRow] = []
    for row in kept:
        reasons = []
        if not row.flight.flight_number:
            reasons.append("no flight number")
        if row.flight.date < FIRST_MATCHABLE_DATE:
            reasons.append(f"dated before {FIRST_MATCHABLE_DATE.year}")
        if reasons:
            manual.append((row, "; ".join(reasons)))
        else:
            candidates.append(row)

    exported: list[KeptRow] = []
    if candidates:
        reference = import_format.OpenFlights.load(data_dir, data_url)
        for row in candidates:
            reasons = reference.unmatched(row.flight)
            if reasons:
                manual.append((row, "; ".join(reasons)))
            else:
                exported.append(row)
        if exported:
            now = now or datetime.now(timezone.utc)
            summary.import_path = reference.write_backup([r.flight for r in exported], output_dir, now)
            summary.exported = len(exported)

    manual.sort(key=lambda item: (item[0].flight.date, item[0].row_number))
    summary.manual_add_path = output_dir / MANUAL_ADD_NAME
    write_csv(
        summary.manual_add_path,
        MANUAL_ADD_COLUMNS,
        [
            {
                "date": row.flight.date.isoformat(),
                "origin": row.flight.origin,
                "destination": row.flight.destination,
                "flight_number": row.flight.flight_number,
                "reason": reason,
                "message_date": row.message_date,
                "message_subject": row.message_subject,
                "done": "yes" if _ticked(row, decisions) else "",
            }
            for row, reason in manual
        ],
    )
    summary.manual_add = len(manual)
    return summary


def _ticked(row: KeptRow, decisions: dict[str, ReviewDecision]) -> bool:
    """Whether the user has ticked this manual-add flight (``mark-added``)."""
    flight = row.flight
    key = row.flight_id or resolve_key(
        decisions, flight.date.isoformat(), flight.flight_number, flight.origin, flight.destination
    )
    decision = decisions.get(key)
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
        raw_date, origin, destination = cell("date"), cell("origin").upper(), cell("destination").upper()
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
        flight_number = _WHITESPACE.sub("", cell("flight_number")).upper()
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
    if not _DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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
    else:
        lines.append(f"Review table read from {summary.review_path}")
        lines.append("  source of the export:       review table")
    lines.append(f"  rows:                       {summary.rows}")
    lines.append(f"  not kept:                   {summary.not_kept}")
    lines.append(f"  rejected as invalid:        {len(summary.invalid)}")
    for row_number, reason in summary.invalid:
        lines.append(f"    row {row_number}: {reason}")
    if summary.store_path is not None:
        if not summary.from_store:
            lines.append(f"  decisions recorded from the review table: {summary.decisions_recorded}")
        lines.append(f"  {DECISIONS_DISCARDED}: {summary.decisions_discarded}")
    if summary.invalid:
        lines.append("Fix the rejected rows in the review table and run export again.")
    return "\n".join(lines)
