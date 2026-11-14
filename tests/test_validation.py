"""Code validation and confidence flags, driven through the command line
(classify, extract, review, and export where it matters) against a fake Ollama
that scripts the classifier's answer and the extraction record per message.
Reference data comes from the tiny OpenFlights fixture files (see conftest);
nothing touches the network. Assertions are on the review CSV, the printed
summary and the export's output. Every email, person, booking and flight here
is invented.
"""

from __future__ import annotations

from typing import Any

import pytest

from fake_ollama import Chat, Decision
from mailbox_fixtures import email, write_mbox
from openflights_fixtures import unreachable_url
from test_consolidation import ROBIN, Mail, record, seg
from test_export import data_rows, the_import_file, write_review
from test_extract import read_csv

LOW = "low confidence"


def configure(workspace, extra: str = "") -> None:
    workspace.config_path.write_text(f'[ollama]\naddress = "{workspace.ollama.url}"\n{extra}', encoding="utf-8")


def booking(subject: str, *segments: dict[str, Any], day: str = "01", reference: str | None = None,
            passengers: tuple[str, ...] = (ROBIN,)) -> Mail:
    reference = reference or "".join(c for c in subject.upper() if c.isalnum())[:6]
    return Mail(subject, day, "booking confirmation", record("booking", reference, *segments, passengers=passengers))


def run(workspace, mails: list[Mail], bodies: dict[str, str] | None = None):
    """Classify, extract and review ``mails``; return the review result and rows."""
    bodies = bodies or {}
    mbox = write_mbox(workspace.root / "mail.mbox",
                      [email(m.subject, bodies.get(m.subject, "Invented email.\n"), date=m.header_date)
                       for m in mails])
    for m in mails:
        workspace.ollama.decisions[m.subject] = Decision(0.9, m.email_type)
        workspace.ollama.chats[m.subject] = Chat(m.record)
    for command in ("classify", "extract"):
        result = workspace.run(command, "--mailbox", str(mbox))
        assert result.returncode == 0, result.stderr
    review = workspace.run("review", "--force")
    assert review.returncode == 0, review.stderr
    return review, read_csv(workspace.root / "review.csv")


def by_number(rows: list[dict[str, str]]) -> dict[str, tuple[str, str, str, str]]:
    """flight number -> (keep, needs_attention, attention_reason, confidence)."""
    return {r["flight_number"]: (r["keep"], r["needs_attention"], r["attention_reason"], r["confidence"])
            for r in rows}


# -- the acceptance cases --------------------------------------------------------------


def test_unknown_airport_unknown_airline_malformed_number_and_low_confidence_are_flagged(workspace):
    review, rows = run(workspace, [
        booking("Good flight", seg("2025-06-01", "ZZ100", "LIS", "OSL", confidence=0.9)),
        booking("Unknown airport", seg("2025-06-02", "ZZ200", "LIS", "XQX", confidence=0.9)),
        booking("Unknown airline", seg("2025-06-03", "Q9300", "LIS", "OSL", confidence=0.9)),
        booking("Malformed number", seg("2025-06-04", "ZZ40000", "LIS", "OSL", confidence=0.9)),
        booking("Unsure model", seg("2025-06-05", "ZZ500", "LIS", "OSL", confidence=0.5)),
    ])

    assert by_number(rows) == {
        "ZZ100": ("yes", "", "", "0.90"),
        # A failed check halves the confidence: 0.9 -> 0.45, below the 0.7 default cut-off.
        "ZZ200": ("no", "yes", f"unknown airport XQX; {LOW}", "0.45"),
        "Q9300": ("no", "yes", f"unknown airline Q9; {LOW}", "0.45"),
        "ZZ40000": ("no", "yes", f"malformed flight number; {LOW}", "0.45"),
        "ZZ500": ("no", "yes", LOW, "0.50"),
    }
    assert review.count("flights needing attention") == 4
    assert review.count("flights pre-set to not keep") == 4
    assert review.count("unknown airport (XQX)") == 1
    assert review.count("unknown airline (Q9)") == 1
    assert review.count("malformed flight number") == 1
    assert review.count(LOW) == 4


@pytest.mark.parametrize("number", ["BAW286", "ZZ", "Z1", "11234", "ZZ12345", "ZZ123AB", "ZZ12/3"])
def test_malformed_flight_numbers(workspace, number):
    configure(workspace, "\n[review]\nmin_confidence = 0\n")
    review, rows = run(workspace, [booking("Odd number", seg("2025-06-01", number, "LIS", "OSL", confidence=0.8))])

    [row] = rows
    assert (row["keep"], row["attention_reason"], row["confidence"]) == ("no", "malformed flight number", "0.40")


@pytest.mark.parametrize("number", ["ZZ1", "ZZ1234", "ZZ1234A", "9Q12", "QQ77"])
def test_well_formed_flight_numbers_of_known_airlines_pass(workspace, number):
    review, rows = run(workspace, [booking("Fine number", seg("2025-06-01", number, "LIS", "OSL", confidence=0.8))])

    assert by_number(rows) == {number: ("yes", "", "", "0.80")}


# -- the other reasons ---------------------------------------------------------------


def test_same_origin_and_destination_is_blocking(workspace):
    configure(workspace, "\n[review]\nmin_confidence = 0\n")
    review, rows = run(workspace, [booking("Round trip?", seg("2025-06-01", "ZZ100", "LIS", "LIS", confidence=0.9))])

    assert by_number(rows) == {"ZZ100": ("no", "yes", "origin and destination are the same", "0.45")}
    assert review.count("origin and destination are the same") == 1


def test_implausible_dates(workspace):
    configure(workspace, "\n[review]\nmin_confidence = 0\n")
    # Every message is dated 1 March 2025; 400 days later is 5 April 2026.
    review, rows = run(workspace, [
        booking("Too early", seg("1989-12-31", "ZZ1", "LIS", "OSL", confidence=0.8)),
        booking("Earliest plausible", seg("1990-01-01", "ZZ2", "LIS", "OSL", confidence=0.8)),
        booking("Latest plausible", seg("2026-04-05", "ZZ3", "LIS", "OSL", confidence=0.8)),
        booking("Too far ahead", seg("2026-04-06", "ZZ4", "LIS", "OSL", confidence=0.8)),
    ])

    assert by_number(rows) == {
        "ZZ1": ("no", "yes", "implausible date", "0.40"),
        "ZZ2": ("yes", "", "", "0.80"),
        "ZZ3": ("yes", "", "", "0.80"),
        "ZZ4": ("no", "yes", "implausible date", "0.40"),
    }
    assert review.count("implausible date") == 2


def test_date_is_judged_against_the_flights_most_recent_source_message(workspace):
    # Booked in early 2024, reminded in March 2025: a 2026 flight is plausible.
    review, rows = run(workspace, [
        booking("Early booking", seg("2026-01-10", "ZZ1", "LIS", "OSL"), day="Mon, 01 Jan 2024 10:00:00 +0000",
                reference="EARLY1"),
        booking("Trip reminder", seg("2026-01-10", "ZZ1", "LIS", "OSL"), day="02", reference="EARLY1"),
    ])

    assert by_number(rows) == {"ZZ1": ("yes", "", "", "0.90")}


def test_advisory_reasons_keep_the_row(workspace):
    configure(workspace, '\n[extract]\nmax_input_chars = 400\n\n[passenger]\nnames = ["Robin Quill"]\n')
    long_body = "Itinerary details.\n" + "Fare rules line.\n" * 100
    review, rows = run(workspace, [
        booking("No number given", seg("2025-06-01", "", "LIS", "OSL")),
        booking("Long itinerary", seg("2025-06-02", "ZZ200", "LIS", "OSL")),
        booking("Nameless long itinerary", seg("2025-06-03", "", "OSL", "LIS"), passengers=()),
    ], bodies={"Long itinerary": long_body, "Nameless long itinerary": long_body})

    assert [(r["date"], r["keep"], r["needs_attention"], r["attention_reason"], r["confidence"]) for r in rows] == [
        ("2025-06-01", "yes", "yes", "no flight number", "0.90"),
        ("2025-06-02", "yes", "yes", "email was truncated before extraction", "0.90"),
        ("2025-06-03", "yes", "yes",
         "no flight number; email was truncated before extraction; no passenger named", "0.90"),
    ]
    assert review.count("flights needing attention") == 3
    assert review.count("flights pre-set to not keep") == 0
    assert review.count("no flight number") == 2
    assert review.count("email was truncated before extraction") == 2
    assert review.count("no passenger named") == 1


def test_several_reasons_on_one_row_blocking_first(workspace):
    configure(workspace, '\n[extract]\nmax_input_chars = 400\n\n[passenger]\nnames = ["Robin Quill"]\n')
    review, rows = run(workspace, [
        booking("Garbled ticket", seg("1985-06-01", "q9 123", "xqx", "XQX", confidence=0.8), passengers=()),
    ], bodies={"Garbled ticket": "Ticket.\n" + "Small print.\n" * 100})

    [row] = rows
    assert row["keep"] == "no"
    assert row["attention_reason"] == (
        "unknown airport XQX; unknown airline Q9; origin and destination are the same; implausible date; "
        "low confidence; email was truncated before extraction; no passenger named"
    )
    # Four failed checks: 0.8 x 0.5^4.
    assert row["confidence"] == "0.05"
    assert review.count("flights pre-set to not keep") == 1


# -- normalisation ---------------------------------------------------------------------


def test_codes_are_normalised_and_two_spellings_of_one_flight_merge(workspace):
    review, rows = run(workspace, [
        booking("Agency booking", seg("2025-06-01", "zz 123", " lis", "osl ", confidence=0.8), reference="AGT111"),
        booking("Airline booking", seg("2025-06-01", "ZZ-123", "LIS", "OSL", confidence=0.85), reference="AIR222",
                day="02"),
        booking("Another flight", seg("2025-07-01", "Qq \u2013 4 1 0", "ams", "kef", confidence=0.9)),
    ])

    assert [(r["date"], r["flight_number"], r["origin"], r["destination"], r["source_count"], r["keep"],
             r["attention_reason"]) for r in rows] == [
        ("2025-06-01", "ZZ123", "LIS", "OSL", "2", "yes", ""),
        ("2025-07-01", "QQ410", "AMS", "KEF", "1", "yes", ""),
    ]


# -- confidence and the cut-off -----------------------------------------------------------


def test_each_failed_check_halves_the_confidence(workspace):
    configure(workspace, "\n[review]\nmin_confidence = 0.1\n")
    review, rows = run(workspace, [
        booking("One problem", seg("2025-06-01", "ZZ1", "LIS", "XQX", confidence=0.8)),
        booking("Two problems", seg("2025-06-02", "ZZ2", "XQY", "XQX", confidence=0.8)),
        booking("Three problems", seg("1980-06-03", "ZZ3", "XQX", "XQX", confidence=0.8)),
        booking("Four problems", seg("1980-06-04", "Q94", "XQX", "XQX", confidence=0.8)),
    ])

    assert by_number(rows) == {
        "ZZ1": ("no", "yes", "unknown airport XQX", "0.40"),
        "ZZ2": ("no", "yes", "unknown airport XQY; unknown airport XQX", "0.20"),
        # 0.1 is not below the 0.1 cut-off.
        "ZZ3": ("no", "yes", "unknown airport XQX; origin and destination are the same; implausible date", "0.10"),
        "Q94": ("no", "yes", "unknown airport XQX; unknown airline Q9; origin and destination are the same; "
                "implausible date; low confidence", "0.05"),
    }


def test_cut_off_boundary_with_the_default(workspace):
    review, rows = run(workspace, [
        booking("At the cut-off", seg("2025-06-01", "ZZ1", "LIS", "OSL", confidence=0.7)),
        booking("Just below", seg("2025-06-02", "ZZ2", "LIS", "OSL", confidence=0.69)),
    ])

    assert by_number(rows) == {
        "ZZ1": ("yes", "", "", "0.70"),
        "ZZ2": ("no", "yes", LOW, "0.69"),
    }


def test_cut_off_comes_from_config(workspace):
    configure(workspace, "\n[review]\nmin_confidence = 0.4\n")
    review, rows = run(workspace, [
        booking("Fairly sure", seg("2025-06-01", "ZZ1", "LIS", "OSL", confidence=0.6)),
        booking("Unsure", seg("2025-06-02", "ZZ2", "LIS", "OSL", confidence=0.39)),
        booking("Bad code but sure", seg("2025-06-03", "ZZ3", "LIS", "XQX", confidence=1.0)),
    ])

    assert by_number(rows) == {
        "ZZ1": ("yes", "", "", "0.60"),
        "ZZ2": ("no", "yes", LOW, "0.39"),
        # Still blocked by the code check, though 0.5 is above this cut-off.
        "ZZ3": ("no", "yes", "unknown airport XQX", "0.50"),
    }


@pytest.mark.parametrize("value", ["1.5", "-0.1", '"high"'])
def test_invalid_cut_off_is_a_config_error(workspace, value):
    configure(workspace, f"\n[review]\nmin_confidence = {value}\n")

    result = workspace.run("review")

    assert result.returncode == 1
    assert "min_confidence" in result.stderr


# -- reference data unavailable ---------------------------------------------------------


def test_without_reference_data_the_table_is_written_without_code_checks(workspace):
    absent = workspace.root / "no-openflights"
    configure(workspace, f'\n[export]\ndata_dir = "{absent}"\ndata_url = "{unreachable_url()}"\n')
    review, rows = run(workspace, [
        booking("Unknown airport", seg("2025-06-01", "ZZ1", "LIS", "XQX", confidence=0.9)),
        booking("Unknown airline", seg("2025-06-02", "Q92", "LIS", "OSL", confidence=0.9)),
        booking("Malformed number", seg("2025-06-03", "ZZ30000", "LIS", "OSL", confidence=0.9)),
        booking("Unsure model", seg("2025-06-04", "ZZ4", "LIS", "OSL", confidence=0.5)),
        booking("Round trip?", seg("2025-06-05", "ZZ5", "OSL", "OSL", confidence=0.9)),
    ])

    assert by_number(rows) == {
        "ZZ1": ("yes", "", "", "0.90"),
        "Q92": ("yes", "", "", "0.90"),
        "ZZ30000": ("no", "yes", f"malformed flight number; {LOW}", "0.45"),
        "ZZ4": ("no", "yes", LOW, "0.50"),
        "ZZ5": ("no", "yes", f"origin and destination are the same; {LOW}", "0.45"),
    }
    assert "codes were not checked" in review.stderr
    assert str(absent) in review.stderr
    assert review.count("flights pre-set to not keep") == 3
    assert not absent.exists()


def test_reference_data_is_not_needed_when_there_are_no_flights(workspace):
    configure(workspace, f'\n[export]\ndata_dir = "{workspace.root / "absent"}"\ndata_url = "{unreachable_url()}"\n')
    review, rows = run(workspace, [Mail("Newsletter", "01", "marketing", record("other", None))])

    assert rows == []
    assert "warning" not in review.stderr


# -- the full path to export ---------------------------------------------------------------


def test_blocked_row_is_exported_only_if_the_user_keeps_it(workspace):
    review, rows = run(workspace, [
        booking("Good flight", seg("2025-05-12", "ZZ1234", "LIS", "OSL", confidence=0.95)),
        booking("Unsure flight", seg("2025-06-01", "zz-77", "osl", "lis", confidence=0.5)),
    ])
    assert [(r["flight_number"], r["keep"]) for r in rows] == [("ZZ1234", "yes"), ("ZZ77", "no")]

    first = workspace.run("export")

    assert first.returncode == 0, first.stderr
    assert data_rows(the_import_file(workspace.root)) == [
        '2025-05-12,LIS,OSL,ZZ1234,Fictair,1714,03:55,,,,,,,,"",1638,644,90001,',
    ]
    assert first.count("flights exported") == 1
    assert first.count("not kept") == 1

    # The user approves the doubtful row in a spreadsheet.
    for r in rows:
        r["keep"] = "yes"
    review_csv = workspace.root / "review.csv"
    write_review(review_csv, rows, list(rows[0].keys()))
    the_import_file(workspace.root).unlink()

    second = workspace.run("export")

    assert second.returncode == 0, second.stderr
    assert data_rows(the_import_file(workspace.root)) == [
        '2025-06-01,OSL,LIS,ZZ77,Fictair,1714,03:55,,,,,,,,"",644,1638,90001,',
        '2025-05-12,LIS,OSL,ZZ1234,Fictair,1714,03:55,,,,,,,,"",1638,644,90001,',
    ]
    assert second.count("flights exported") == 2
    assert second.count("not kept") == 0
