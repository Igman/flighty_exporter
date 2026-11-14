"""The export command, driven through the command line.

Most tests write a review CSV straight into the workspace as the user's
edited input (that file is user-visible), run ``export`` and assert on the
import file, the manual-add list, the exit status and the printed summary.
One test runs the whole path classify -> extract -> review -> edit -> export
against the fake Ollama. OpenFlights reference data comes from tiny fixture
files; nothing touches the network. Every person, booking and flight here is
invented, except the public airport and airline reference rows.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from fake_ollama import Chat, Decision
from mailbox_fixtures import FLIGHT_BODY, email, write_mbox
from openflights_fixtures import serve_reference_data, unreachable_url, write_reference_data
from test_extract import CONNECTING_RECORD, FLIGHT_RECORD, FLIGHT_SUBJECT, read_csv

HEADER = (
    "Date,From,To,Flight_Number,Airline,Distance,Duration,Seat,Seat_Type,Class,Reason,"
    "Plane,Registration,Trip,Note,From_OID,To_OID,Airline_OID,Plane_OID"
)
MANUAL_ADD_COLUMNS = [
    "date", "origin", "destination", "flight_number", "reason", "message_date", "message_subject",
]
REVIEW_COLUMNS = [
    "keep", "needs_attention", "attention_reason", "date", "flight_number", "airline", "origin", "destination", "booking_reference", "passengers",
    "confidence", "evidence", "email_kind", "source", "source_count", "message_id", "message_date",
    "message_subject", "source_messages",
]

# The regression anchor: this exact output was accepted by Flighty.
ANCHOR_ROWS = [
    '2026-09-15,NBO,LHR,KQ100,Kenya Airways,4248,08:59,,,,,,,,"",4059,507,3126,',
    '2025-03-04,MAD,LHR,IB711,Iberia Airlines,773,02:02,,,,,,,,"",1229,507,2822,',
    '2024-03-12,LHR,JNB,BA55,British Airways,5635,11:46,,,,,,,,"",507,813,1355,',
]
LIS_OSL = '2025-05-12,LIS,OSL,ZZ1234,Fictair,1714,03:55,,,,,,,,"",1638,644,90001,'


# -- helpers --------------------------------------------------------------------


def configure(workspace, data_dir: Path | None = None, data_url: str | None = None, extra: str = "") -> Path:
    """Config pointing at the fake Ollama and at fixture reference data."""
    if data_dir is None:
        data_dir = write_reference_data(workspace.root / "openflights")
    workspace.write_config(
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n'
        f'[export]\ndata_dir = "{data_dir}"\ndata_url = "{data_url or unreachable_url()}"\n{extra}'
    )
    return data_dir


def row(date: str, flight_number: str, origin: str, destination: str, keep: str = "yes", **extra: str) -> dict:
    return {
        "keep": keep, "needs_attention": "", "attention_reason": "", "date": date, "flight_number": flight_number, "airline": "Someair", "origin": origin,
        "destination": destination, "booking_reference": "QZX7PL", "passengers": "Robin Quill",
        "confidence": "0.95", "evidence": "booked only", "email_kind": "booking", "source": "model",
        "source_count": "1", "message_id": f"<{flight_number or 'none'}@fixtures.example>",
        "message_date": "2025-03-04 09:15", "message_subject": f"Booking {flight_number}",
        "source_messages": f"2025-03-04 Booking {flight_number}", **extra,
    }


def write_review(path: Path, rows: list[dict], columns: list[str] | None = None, *, lineterminator: str = "\r\n",
                 quoting: int = csv.QUOTE_MINIMAL, bom: bool = True, trailer: str = "",
                 delimiter: str = ",") -> Path:
    columns = columns or REVIEW_COLUMNS
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator=lineterminator, quoting=quoting,
                                extrasaction="ignore", delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)
        handle.write(trailer)
    return path


def import_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("openflights-backup-*.csv"))


def the_import_file(directory: Path) -> Path:
    [path] = import_files(directory)
    return path


def data_rows(path: Path) -> list[str]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    lines = raw[3:].decode("utf-8").split("\r\n")
    assert lines[0] == HEADER
    return lines[1:]


def export(workspace, rows: list[dict] | None = None, *args: str):
    if rows is not None:
        write_review(workspace.root / "review.csv", rows)
    return workspace.run("export", *args)


# -- the import file format ---------------------------------------------------------


def test_regression_anchor_is_byte_identical(workspace):
    configure(workspace)
    before = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # Deliberately out of order: the file is newest first.
    result = export(workspace, [
        row("2024-03-12", "BA55", "LHR", "JNB"),
        row("2025-03-04", "IB711", "MAD", "LHR"),
        row("2026-09-15", "KQ100", "NBO", "LHR"),
    ])
    after = datetime.now(timezone.utc)

    assert result.returncode == 0, result.stderr
    path = the_import_file(workspace.root)
    expected = "﻿" + HEADER + "\r\n" + "\r\n".join(ANCHOR_ROWS)
    assert path.read_bytes() == expected.encode("utf-8")
    assert not path.read_bytes().endswith(b"\r\n")

    match = re.fullmatch(r"openflights-backup-(\d{4}-\d{2}-\d{2}-\d{4})\.csv", path.name)
    assert match
    stamp = datetime.strptime(match.group(1), "%Y-%m-%d-%H%M").replace(tzinfo=timezone.utc)
    assert before <= stamp <= after
    assert result.count("flights exported") == 3
    assert str(path) in result.stdout
    assert not list(workspace.root.glob("*.tmp"))


def test_airport_without_iata_uses_icao_and_airline_name_comes_from_openflights(workspace):
    configure(workspace)

    result = export(workspace, [
        # "Someair" in the review table is ignored: the airline is OpenFlights' name.
        row("2025-06-01", "IB3", "LIS", "MAD"),  # an inactive IB airline sorts first by name
        row("2025-06-02", "QQ77", "KEF", "BOS"),  # two active QQ airlines: alphabetical
        row("2025-06-03", "WW5", "KEF", "biqq"),  # only an inactive WW airline; ICAO-only airport
    ])

    assert result.returncode == 0, result.stderr
    assert data_rows(the_import_file(workspace.root)) == [
        '2025-06-03,KEF,BIQQ,WW5,Defunct Skyways,22,00:32,,,,,,,,"",16,90001,90005,',
        '2025-06-02,KEF,BOS,QQ77,Imaginair,2405,05:18,,,,,,,,"",16,3448,90004,',
        '2025-06-01,LIS,MAD,IB3,Iberia Airlines,318,01:08,,,,,,,,"",1638,1229,2822,',
    ]


def test_ordering_is_deterministic_for_flights_on_the_same_day(workspace):
    configure(workspace)
    rows = [
        row("2025-05-12", "ZZ1234", "LIS", "OSL"),
        row("2025-05-12", "QQ410", "AMS", "KEF"),
        row("2025-05-12", "QQ77", "KEF", "BOS"),
        row("2025-05-13", "BA55", "LHR", "JNB"),
    ]
    first = export(workspace, rows, "--output-dir", "first")
    second = export(workspace, list(reversed(rows)), "--output-dir", "second")

    assert first.returncode == 0 and second.returncode == 0, first.stderr + second.stderr
    a = the_import_file(workspace.root / "first").read_bytes()
    b = the_import_file(workspace.root / "second").read_bytes()
    assert a == b
    assert [line.split(",")[3] for line in data_rows(the_import_file(workspace.root / "first"))] == [
        "BA55", "QQ410", "QQ77", "ZZ1234",
    ]


# -- reading the edited review table --------------------------------------------------


def test_only_rows_marked_keep_are_exported(workspace):
    configure(workspace)
    kept = ["yes", "Y", "TRUE", "1", "x", " Yes "]
    not_kept = ["no", "N", "False", "0", ""]
    rows = [row(f"2025-05-{10 + i:02d}", "ZZ1234", "LIS", "OSL", keep=k) for i, k in enumerate(kept)]
    rows += [row(f"2025-06-{10 + i:02d}", "ZZ1234", "LIS", "OSL", keep=k) for i, k in enumerate(not_kept)]

    result = export(workspace, rows)

    assert result.returncode == 0, result.stderr
    exported = data_rows(the_import_file(workspace.root))
    assert [line[:10] for line in exported] == [f"2025-05-{10 + i:02d}" for i in reversed(range(len(kept)))]
    assert result.count("flights exported") == len(kept)
    assert result.count("not kept") == len(not_kept)
    assert result.count("rejected as invalid") == 0
    assert result.count("rows") == len(rows)


def test_invalid_keep_value_is_reported_and_excluded(workspace):
    configure(workspace)

    result = export(workspace, [
        row("2025-05-12", "ZZ1234", "LIS", "OSL"),
        row("2025-05-13", "QQ410", "AMS", "KEF", keep="maybe"),
    ])

    assert result.returncode == 0, result.stderr
    assert data_rows(the_import_file(workspace.root)) == [LIS_OSL]
    assert read_csv(workspace.root / "manual-add.csv") == []
    assert result.count("rejected as invalid") == 1
    assert re.search(r"row 3: keep value 'maybe' is not yes or no", result.stdout)


def test_invalid_edits_are_reported_with_row_numbers_and_excluded(workspace):
    configure(workspace)

    result = export(workspace, [
        row("2025-05-12", "ZZ1234", "LIS", "OSL"),
        row("2025-02-30", "QQ410", "AMS", "KEF"),  # not a real date
        row("12/05/2025", "QQ77", "KEF", "BOS"),  # a spreadsheet's date format
        row("", "BA55", "LHR", "JNB"),
        row("2025-05-14", "", "", "OSL"),  # no origin (and no flight number)
        row("2025-05-15", "IB711", "MAD", " "),
        row("2025-05-16", "IB712", "MAD", "LHR", keep="no"),  # not kept, so never checked
    ])

    assert result.returncode == 0, result.stderr
    assert data_rows(the_import_file(workspace.root)) == [LIS_OSL]
    assert read_csv(workspace.root / "manual-add.csv") == []
    assert result.count("rejected as invalid") == 5
    assert result.count("not kept") == 1
    assert "row 3: date '2025-02-30' is not a real YYYY-MM-DD date" in result.stdout
    assert "row 4: date '12/05/2025' is not a real YYYY-MM-DD date" in result.stdout
    assert "row 5: date '' is not a real YYYY-MM-DD date" in result.stdout
    assert "row 6: origin is empty" in result.stdout
    assert "row 7: destination is empty" in result.stdout


def test_spreadsheet_resave_reads_the_same(workspace):
    """Reordered and extra columns, header and values with stray spaces and
    case, every field quoted, LF line endings, no byte-order mark, blank
    trailing rows: the same flights come out."""
    configure(workspace)
    columns = ["My notes", "destination", " Flight_Number ", "origin", "date", "KEEP", "message_subject",
               "airline"]
    rows = [
        {**row("2025-03-04", "ib 711", "mad", "lhr", keep=" YES "), "My notes": "aisle, please"},
        {**row("2026-09-15", "KQ100", " NBO ", "LHR", keep="y"), "My notes": 'said "great"'},
        {**row("2024-03-12", "Ba 55", "LHR", "JNB", keep="True"), "My notes": "multi\nline"},
    ]
    for r in rows:
        r[" Flight_Number "], r["KEEP"] = r.pop("flight_number"), r.pop("keep")
    write_review(workspace.root / "review.csv", rows, columns, lineterminator="\n", quoting=csv.QUOTE_ALL,
                 bom=False, trailer=",,,,,,,\n,,,,,,,\n\n")

    result = workspace.run("export")

    assert result.returncode == 0, result.stderr
    assert data_rows(the_import_file(workspace.root)) == ANCHOR_ROWS
    assert result.count("rows") == 3
    assert result.count("rejected as invalid") == 0


def test_crlf_resave_with_byte_order_mark_reads_the_same(workspace):
    configure(workspace)
    write_review(workspace.root / "review.csv", [row("2025-05-12", "ZZ1234", "LIS", "OSL")],
                 lineterminator="\r\n", trailer=",,,\r\n")

    result = workspace.run("export")

    assert result.returncode == 0, result.stderr
    assert data_rows(the_import_file(workspace.root)) == [LIS_OSL]


def test_semicolon_separated_resave_reads_the_same(workspace):
    """A spreadsheet in a European locale re-saves the table with ";"."""
    configure(workspace)
    write_review(workspace.root / "review.csv", [
        row("2024-03-12", "BA55", "LHR", "JNB"),
        row("2025-03-04", "IB711", "MAD", "LHR"),
        row("2026-09-15", "KQ100", "NBO", "LHR"),
        row("2025-05-12", "ZZ1234", "LIS", "OSL", keep="no"),
    ], delimiter=";")

    result = workspace.run("export")

    assert result.returncode == 0, result.stderr
    assert data_rows(the_import_file(workspace.root)) == ANCHOR_ROWS
    assert result.count("rows") == 4
    assert result.count("not kept") == 1
    assert result.count("rejected as invalid") == 0


def test_tab_separated_resave_reads_the_same(workspace):
    configure(workspace)
    write_review(workspace.root / "review.csv", [row("2025-05-12", "ZZ1234", "LIS", "OSL")], delimiter="\t",
                 lineterminator="\n", bom=False)

    result = workspace.run("export")

    assert result.returncode == 0, result.stderr
    assert data_rows(the_import_file(workspace.root)) == [LIS_OSL]
    assert result.count("flights exported") == 1


def test_semicolon_separated_resave_with_commas_inside_a_quoted_field(workspace):
    configure(workspace)
    write_review(workspace.root / "review.csv", [
        row("2025-05-12", "ZZ1234", "LIS", "OSL", passengers="Quill, Robin; Ash, Morgan",
            message_subject="Your trip: Lisbon, Oslo, and back"),
        row("2003-05-12", "ZZ1234", "LIS", "OSL", message_subject="Old trip, Lisbon; Oslo"),
    ], delimiter=";")

    result = workspace.run("export")

    assert result.returncode == 0, result.stderr
    assert data_rows(the_import_file(workspace.root)) == [LIS_OSL]
    [manual] = read_csv(workspace.root / "manual-add.csv")
    assert manual["message_subject"] == "Old trip, Lisbon; Oslo"
    assert (manual["date"], manual["origin"], manual["destination"]) == ("2003-05-12", "LIS", "OSL")
    assert result.count("rejected as invalid") == 0


def test_missing_column_in_a_semicolon_separated_file_is_an_error(workspace):
    configure(workspace)
    columns = [c for c in REVIEW_COLUMNS if c != "destination"]
    write_review(workspace.root / "review.csv", [row("2025-05-12", "ZZ1234", "LIS", "OSL")], columns,
                 delimiter=";")

    result = workspace.run("export")

    assert result.returncode == 1
    assert "has no destination column" in result.stderr
    assert import_files(workspace.root) == []
    assert not (workspace.root / "manual-add.csv").exists()


def test_missing_review_file_is_an_error(workspace):
    configure(workspace)

    result = workspace.run("export")

    assert result.returncode == 1
    assert "review table not found" in result.stderr and "review.csv" in result.stderr
    assert import_files(workspace.root) == []
    assert not (workspace.root / "manual-add.csv").exists()


def test_missing_column_is_an_error(workspace):
    configure(workspace)
    columns = [c for c in REVIEW_COLUMNS if c != "destination"]
    write_review(workspace.root / "review.csv", [row("2025-05-12", "ZZ1234", "LIS", "OSL")], columns)

    result = workspace.run("export")

    assert result.returncode == 1
    assert "destination" in result.stderr and "column" in result.stderr
    assert import_files(workspace.root) == []


# -- the manual-add list ---------------------------------------------------------------


def test_flights_flighty_cannot_match_go_to_the_manual_add_list_only(workspace):
    configure(workspace)

    result = export(workspace, [
        row("2025-05-12", "ZZ1234", "LIS", "OSL"),
        row("2025-05-13", "", "OSL", "LIS"),
        row("2005-12-31", "BA55", "LHR", "JNB"),
        row("2006-01-01", "BA54", "JNB", "LHR"),  # the first matchable day: exported
        row("2025-05-14", "ZZ1235", "LIS", "XQX"),
        row("2025-05-15", "Q9 123", "LIS", "OSL"),
        row("2004-02-01", "", "MAD", "LHR"),
    ])

    assert result.returncode == 0, result.stderr
    exported = data_rows(the_import_file(workspace.root))
    assert [line.split(",")[3] for line in exported] == ["ZZ1234", "BA54"]

    manual_path = workspace.root / "manual-add.csv"
    assert manual_path.read_bytes().startswith(b"\xef\xbb\xbf")
    with manual_path.open(encoding="utf-8-sig", newline="") as handle:
        assert next(csv.reader(handle)) == MANUAL_ADD_COLUMNS
    manual = read_csv(manual_path)
    assert [(m["date"], m["origin"], m["destination"], m["flight_number"], m["reason"]) for m in manual] == [
        ("2004-02-01", "MAD", "LHR", "", "no flight number; dated before 2006"),
        ("2005-12-31", "LHR", "JNB", "BA55", "dated before 2006"),
        ("2025-05-13", "OSL", "LIS", "", "no flight number"),
        ("2025-05-14", "LIS", "XQX", "ZZ1235", "airport XQX not in OpenFlights data"),
        ("2025-05-15", "LIS", "OSL", "Q9123", "no OpenFlights airline for prefix Q9"),
    ]
    assert manual[1]["message_date"] == "2025-03-04 09:15"
    assert manual[1]["message_subject"] == "Booking BA55"
    assert result.count("flights exported") == 2
    assert result.count("flights to add by hand") == 5
    assert str(manual_path) in result.stdout


def test_nothing_to_export_writes_no_import_file(workspace):
    # Only manual-add rows: the reference data is never needed, so a missing
    # data directory with no network is fine.
    configure(workspace, data_dir=workspace.root / "absent")

    result = export(workspace, [
        row("2025-05-12", "ZZ1234", "LIS", "OSL", keep="no"),
        row("2003-05-12", "ZZ1234", "LIS", "OSL"),
    ])

    assert result.returncode == 0, result.stderr
    assert import_files(workspace.root) == []
    assert "none written" in result.stdout
    assert result.count("flights exported") == 0
    assert result.count("flights to add by hand") == 1
    assert result.count("not kept") == 1


def test_all_rows_rejected_or_not_kept_writes_no_import_file(workspace):
    configure(workspace)

    result = export(workspace, [row("2025-05-12", "ZZ1234", "LIS", "OSL", keep="no"),
                                row("2025-13-01", "ZZ1234", "LIS", "OSL")])

    assert result.returncode == 0, result.stderr
    assert import_files(workspace.root) == []
    assert "none written" in result.stdout
    assert read_csv(workspace.root / "manual-add.csv") == []


# -- reference data -------------------------------------------------------------------


def test_missing_reference_data_without_network_stops_with_a_clear_message(workspace):
    data_dir = workspace.root / "empty-cache"
    configure(workspace, data_dir=data_dir)

    result = export(workspace, [row("2025-05-12", "ZZ1234", "LIS", "OSL")])

    assert result.returncode == 1
    assert "airports.dat" in result.stderr and "could not be downloaded" in result.stderr
    assert str(data_dir) in result.stderr
    assert import_files(workspace.root) == []


def test_reference_data_is_downloaded_once_and_reused(workspace, tmp_path_factory):
    data_dir = workspace.root / "cache" / "openflights"
    with serve_reference_data(tmp_path_factory.mktemp("server")) as url:
        configure(workspace, data_dir=data_dir, data_url=url)
        first = export(workspace, [row("2025-05-12", "ZZ1234", "LIS", "OSL")], "--output-dir", "first")

    assert first.returncode == 0, first.stderr
    assert sorted(p.name for p in data_dir.iterdir()) == ["airlines.dat", "airports.dat"]
    assert data_rows(the_import_file(workspace.root / "first")) == [LIS_OSL]

    # The server is gone: the cached files are used.
    second = export(workspace, None, "--output-dir", "second")

    assert second.returncode == 0, second.stderr
    assert data_rows(the_import_file(workspace.root / "second")) == [LIS_OSL]


# -- paths ------------------------------------------------------------------------------


def test_paths_come_from_config_and_the_command_line(workspace):
    configure(workspace, extra='output_dir = "exports"\n\n[review]\noutput = "edited/my-review.csv"\n')
    (workspace.root / "edited").mkdir()
    write_review(workspace.root / "edited" / "my-review.csv", [row("2025-05-12", "ZZ1234", "LIS", "OSL")])

    from_config = workspace.run("export")

    assert from_config.returncode == 0, from_config.stderr
    assert data_rows(the_import_file(workspace.root / "exports")) == [LIS_OSL]
    assert (workspace.root / "exports" / "manual-add.csv").is_file()

    other = write_review(workspace.root / "other.csv", [row("2025-03-04", "IB711", "MAD", "LHR")])
    cli = workspace.run("export", "--review", str(other), "--output-dir", str(workspace.root / "cli-out"))

    assert cli.returncode == 0, cli.stderr
    assert data_rows(the_import_file(workspace.root / "cli-out")) == [ANCHOR_ROWS[1]]
    assert (workspace.root / "cli-out" / "manual-add.csv").is_file()


# -- the whole path -------------------------------------------------------------------


def test_classify_extract_review_edit_export(workspace):
    configure(workspace)
    connecting = "Your Imaginair itinerary has changed"
    old = "Your 2004 trip to Lisbon"
    mbox = write_mbox(workspace.root / "mail.mbox", [
        email(FLIGHT_SUBJECT, FLIGHT_BODY),
        email(connecting, FLIGHT_BODY),
        email(old, FLIGHT_BODY),
    ])
    workspace.ollama.default_decision = Decision(0.95, "booking confirmation")
    workspace.ollama.chats[FLIGHT_SUBJECT] = Chat(FLIGHT_RECORD)
    workspace.ollama.chats[connecting] = Chat(CONNECTING_RECORD)
    workspace.ollama.chats[old] = Chat({
        "email_kind": "booking", "booking_reference": "OLD004", "passengers": ["Robin Quill"],
        "segments": [{"date": "2004-08-01", "airline": "Fictair", "flight_number": "ZZ9",
                      "origin": "OSL", "destination": "LIS", "confidence": 0.9}],
    })
    for command in ("classify", "extract"):
        result = workspace.run(command, "--mailbox", str(mbox))
        assert result.returncode == 0, result.stderr
    assert workspace.run("review").returncode == 0

    # The user edits the table in a spreadsheet: drops one flight, corrects a
    # flight number and an airport, and saves.
    review_csv = workspace.root / "review.csv"
    rows = read_csv(review_csv)
    by_number = {r["flight_number"]: r for r in rows}
    assert set(by_number) == {"ZZ1234", "QQ410", "QQ77", "ZZ9"}
    by_number["QQ410"]["keep"] = "no"
    by_number["ZZ1234"]["flight_number"] = "zz 1235"
    by_number["QQ77"]["destination"] = "lhr"
    write_review(review_csv, rows, list(rows[0].keys()))

    result = workspace.run("export")

    assert result.returncode == 0, result.stderr
    assert data_rows(the_import_file(workspace.root)) == [
        '2025-05-12,LIS,OSL,ZZ1235,Fictair,1714,03:55,,,,,,,,"",1638,644,90001,',
        '2024-11-03,KEF,LHR,QQ77,Imaginair,1176,02:51,,,,,,,,"",16,507,90004,',
    ]
    [manual] = read_csv(workspace.root / "manual-add.csv")
    assert (manual["date"], manual["origin"], manual["destination"], manual["flight_number"]) == (
        "2004-08-01", "OSL", "LIS", "ZZ9")
    assert manual["message_subject"] == old
    assert result.count("flights exported") == 2
    assert result.count("flights to add by hand") == 1
    assert result.count("not kept") == 1
    assert result.count("rejected as invalid") == 0
