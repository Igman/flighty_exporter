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

from ..tables import write_csv
from . import openflights as import_format

MANUAL_ADD_NAME = "manual-add.csv"
MANUAL_ADD_COLUMNS = (
    "date", "origin", "destination", "flight_number", "reason", "message_date", "message_subject",
)
REQUIRED_COLUMNS = ("keep", "date", "flight_number", "origin", "destination")
OPTIONAL_COLUMNS = ("message_date", "message_subject")

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


@dataclass
class KeptRow:
    row_number: int
    flight: import_format.Flight
    message_date: str
    message_subject: str


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


def run_export(
    review_path: Path,
    output_dir: Path,
    data_dir: Path,
    data_url: str,
    now: datetime | None = None,
) -> ExportSummary:
    summary = ExportSummary(review_path)
    kept = _read_review(review_path, summary)

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
            }
            for row, reason in manual
        ],
    )
    summary.manual_add = len(manual)
    return summary


def _read_review(path: Path, summary: ExportSummary) -> list[KeptRow]:
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

    kept: list[KeptRow] = []
    for row_number, record in enumerate(records[1:], 2):
        if not any(cell.strip() for cell in record):
            continue
        summary.rows += 1

        def cell(name: str) -> str:
            position = index.get(name)
            return record[position].strip() if position is not None and position < len(record) else ""

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
    lines.append(f"Review table read from {summary.review_path}")
    lines.append(f"  rows:                       {summary.rows}")
    lines.append(f"  not kept:                   {summary.not_kept}")
    lines.append(f"  rejected as invalid:        {len(summary.invalid)}")
    for row_number, reason in summary.invalid:
        lines.append(f"    row {row_number}: {reason}")
    if summary.invalid:
        lines.append("Fix the rejected rows in the review table and run export again.")
    return "\n".join(lines)
