"""The `flights` command: the flight list as the review screen shows it, in
the screen's order, with its filters, totals, years, and the lists of dropped
bookings and unreadable emails. Driven through the command line against the
fake Ollama and the tiny OpenFlights fixture data. Every email, person,
booking and flight here is invented.
"""

from __future__ import annotations

from test_consolidation import Mail, record, seg
from test_decisions import FLAGGED, GOOD, NO_NUMBER, OLD, OTHER, run_passes
from test_validation import booking

def flights(workspace, *args: str):
    result = workspace.run("flights", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def shown(stdout: str) -> list[str]:
    """'date flight' of each table row, in the order printed."""
    rows = []
    in_table = False
    for line in stdout.splitlines():
        if line.startswith("date "):
            in_table = True
            continue
        if in_table:
            if not line.strip():
                break
            date, number = line.split()[:2]
            rows.append(f"{date} {number}")
    return rows


def row(stdout: str, wanted: str) -> str:
    for line in stdout.splitlines():
        if " ".join(line.split()[:2]) == wanted:
            return line
    raise AssertionError(f"no row {wanted!r} in:\n{stdout}")


def line_starting(stdout: str, prefix: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starting {prefix!r} in:\n{stdout}")


def test_flights_needing_attention_come_first_blocking_before_advisory_then_by_date(workspace):
    run_passes(workspace, [GOOD, OTHER, FLAGGED, NO_NUMBER, OLD])

    result = flights(workspace)

    assert shown(result.stdout) == [
        "2025-07-01 ZZ300",  # blocking: unknown airport
        "2025-08-01 (none)",  # advisory: no flight number
        "2004-08-01 ZZ9",
        "2025-05-12 ZZ1234",
        "2025-06-01 QQ410",
    ]
    flagged = row(result.stdout, "2025-07-01 ZZ300")
    for cell in ("LIS-XQX", "Fictair", "FLAG03", "booked only", "no", "unknown airport XQX; low confidence"):
        assert cell in flagged
    assert "no flight number" in row(result.stdout, "2025-08-01 (none)")
    assert row(result.stdout, "2025-05-12 ZZ1234").split()[-1] == "yes"


def test_totals_and_years_cover_every_flight(workspace):
    run_passes(workspace, [GOOD, OTHER, FLAGGED, NO_NUMBER, OLD])

    result = flights(workspace, "--flagged")

    assert line_starting(result.stdout, "Flights:") == (
        "Flights: 5 · kept 4 · not kept 1 · needing attention 2")
    assert line_starting(result.stdout, "Years:") == "Years: 2004, 2025"


def test_each_filter_and_their_combinations(workspace):
    late = booking("Your trip LATE06", seg("2026-01-15", "ZZ77", "LIS", "XQV", confidence=0.9), day="06",
                   reference="LATE06")
    run_passes(workspace, [GOOD, OTHER, FLAGGED, NO_NUMBER, OLD, late])

    assert shown(flights(workspace, "--flagged").stdout) == ["2025-07-01 ZZ300", "2026-01-15 ZZ77",
                                                             "2025-08-01 (none)"]
    assert shown(flights(workspace, "--not-kept").stdout) == ["2025-07-01 ZZ300", "2026-01-15 ZZ77"]
    assert shown(flights(workspace, "--year", "2004").stdout) == ["2004-08-01 ZZ9"]
    assert shown(flights(workspace, "--flagged", "--year", "2025").stdout) == [
        "2025-07-01 ZZ300", "2025-08-01 (none)"]
    combined = flights(workspace, "--not-kept", "--flagged", "--year", "2026")
    assert shown(combined.stdout) == ["2026-01-15 ZZ77"]
    assert line_starting(combined.stdout, "Showing:") == (
        "Showing: flagged only, not kept only, year 2026 (1 of 6 flights)")
    assert line_starting(flights(workspace).stdout, "Showing:") == "Showing: all flights (6 of 6 flights)"


def test_a_filter_that_matches_nothing_says_so(workspace):
    run_passes(workspace, [GOOD, OTHER])

    result = flights(workspace, "--not-kept")

    assert shown(result.stdout) == []
    assert "No flights match these filters." in result.stdout


def test_a_bad_year_is_a_usage_error(workspace):
    run_passes(workspace, [GOOD])

    result = workspace.run("flights", "--year", "25")

    assert result.returncode == 2
    assert "YYYY" in result.stderr


def test_no_store_is_a_clear_error(workspace):
    result = workspace.run("flights")

    assert result.returncode == 1
    assert "store not found" in result.stderr


def test_dropped_bookings_and_unreadable_emails_are_listed_under_their_own_headings(workspace):
    workspace.write_config(
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[passenger]\nnames = ["Robin Quill"]\n')
    other = Mail("Your trip ELSE07", "07", "booking confirmation",
                 record("booking", "ELSE07", seg("2025-09-09", "ZZ55", "LIS", "OSL"),
                        seg("2025-09-12", "ZZ56", "OSL", "LIS"), passengers=("Casey Marlow",)))
    unreadable = Mail("Your trip BROK08", "08", "booking confirmation", {"not": "a record"})
    run_passes(workspace, [GOOD, other, unreadable])

    result = flights(workspace)

    lines = result.stdout.splitlines()
    dropped = lines.index("Bookings dropped as other passengers': 1")
    assert lines[dropped + 1] == "  ELSE07: Casey Marlow (2 flights, first 2025-09-09)"
    failed = lines.index("Emails that could not be read: 1")
    assert lines[failed + 1].startswith("  2025-03-08 10:00  ")
    assert "Your trip BROK08" in lines[failed + 1]
    assert shown(result.stdout) == ["2025-05-12 ZZ1234"]


def test_an_empty_flight_list_says_where_to_look(workspace):
    unreadable = Mail("Your trip BROK08", "08", "booking confirmation", {"not": "a record"})
    run_passes(workspace, [unreadable])

    result = flights(workspace)

    assert "No flights found." in result.stdout
    assert "Emails that could not be read: 1" in result.stdout
