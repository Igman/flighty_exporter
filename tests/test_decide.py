"""The `decide` command: one review decision at a time, as the review screen
makes them (toggle keep, correct a field, clear a decision), saved at once in
the store. Driven through the command line against the fake Ollama and the
tiny OpenFlights fixture data; checked through what `decide` prints, the
review table a following `review --force` writes and the import file
`export --from-store` writes. Every email, person, booking and flight here is
invented.
"""

from __future__ import annotations

import pytest
from test_decisions import FLAGGED, GOOD, NO_NUMBER, OLD, OTHER, REVIEW, review, rows_by_id, run_passes, view
from test_export import data_rows, import_files, the_import_file
from test_extract import read_csv


def decide(workspace, *args: str):
    result = workspace.run("decide", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def field(stdout: str, label: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(label + ":"):
            return line[len(label) + 1:].strip()
    raise AssertionError(f"no {label!r} line in:\n{stdout}")


def decision_rows(workspace) -> dict[str, tuple[str, ...]]:
    review(workspace, "--force")
    return {key: view(row) for key, row in rows_by_id(workspace.root / REVIEW).items()}


def exported_numbers(workspace) -> list[str]:
    # The import file is named to the minute, so an earlier export may have
    # left one under another name: remove it, and read only this export's.
    for path in import_files(workspace.root / "out"):
        path.unlink()
    result = workspace.run("export", "--from-store", "--output-dir", "out")
    assert result.returncode == 0, result.stderr
    return sorted(line.split(",")[3] for line in data_rows(the_import_file(workspace.root / "out")))


# -- keep -------------------------------------------------------------------------------------


def test_drop_saves_an_override_and_keep_clears_it_when_it_matches_the_preset_again(workspace):
    run_passes(workspace, [GOOD, OTHER])

    dropped = decide(workspace, "2025-06-01", "QQ410", "--drop")

    assert field(dropped.stdout, "keep") == "no (your choice; pre-set yes)"
    assert decision_rows(workspace)["2025-06-01 QQ410"][0] == "no"
    assert exported_numbers(workspace) == ["ZZ1234"]

    kept = decide(workspace, "2025-06-01", "qq 410", "--keep")

    # Back to the pre-set value: no override is left behind.
    assert field(kept.stdout, "keep") == "yes (pre-set)"
    assert decision_rows(workspace)["2025-06-01 QQ410"][0] == "yes"
    assert exported_numbers(workspace) == ["QQ410", "ZZ1234"]


def test_keep_on_a_flight_already_kept_saves_nothing(workspace):
    run_passes(workspace, [GOOD])

    result = decide(workspace, "2025-05-12", "ZZ1234", "--keep")

    assert field(result.stdout, "keep") == "yes (pre-set)"


def test_keep_overrides_a_blocked_flight_and_the_detail_marks_reasons(workspace):
    run_passes(workspace, [GOOD, FLAGGED])

    result = decide(workspace, "2025-07-01", "ZZ300", "--keep")

    assert field(result.stdout, "keep") == "yes (your choice; pre-set no)"
    assert "  unknown airport XQX (blocking)" in result.stdout.splitlines()
    assert "  low confidence (blocking)" in result.stdout.splitlines()
    assert "  2025-03-03 10:00  Your trip FLAG03" in result.stdout.splitlines()
    assert decision_rows(workspace)["2025-07-01 ZZ300"][0] == "yes"


def test_a_flight_without_a_number_is_picked_by_its_route_and_advisory_reasons_marked(workspace):
    run_passes(workspace, [GOOD, NO_NUMBER])

    result = decide(workspace, "2025-08-01", "--route", "osl", "lis", "--drop")

    assert "  no flight number (advisory)" in result.stdout.splitlines()
    assert decision_rows(workspace)["2025-08-01 OSL-LIS"][0] == "no"


def test_a_kept_flight_for_the_manual_add_list_shows_whether_it_was_added_by_hand(workspace):
    run_passes(workspace, [GOOD, NO_NUMBER])
    added = "added by hand in Flighty"

    shown = decide(workspace, "2025-08-01", "--route", "OSL", "LIS", "--keep")
    assert field(shown.stdout, added) == "no (on the manual-add list: no flight number)"

    assert workspace.run("mark-added", "2025-08-01", "--route", "OSL", "LIS").returncode == 0
    shown = decide(workspace, "2025-08-01", "--route", "OSL", "LIS", "--keep")
    assert field(shown.stdout, added) == "yes (on the manual-add list: no flight number)"

    # A flight that goes in the import file, or one not kept, is not on the list.
    assert added not in decide(workspace, "2025-05-12", "ZZ1234", "--keep").stdout
    assert added not in decide(workspace, "2025-08-01", "--route", "OSL", "LIS", "--drop").stdout


# -- corrections -------------------------------------------------------------------------------


def test_a_correction_that_fixes_a_flagged_flight_recomputes_its_reasons_and_keep(workspace):
    run_passes(workspace, [GOOD, FLAGGED])

    result = decide(workspace, "2025-07-01", "ZZ300", "--set", "destination=osl")

    assert field(result.stdout, "destination") == "OSL"
    assert field(result.stdout, "attention") == "none"
    assert field(result.stdout, "keep") == "yes (pre-set)"
    assert "  destination: XQX -> OSL" in result.stdout.splitlines()
    assert decision_rows(workspace)["2025-07-01 ZZ300"] == ("yes", "2025-07-01", "ZZ300", "LIS", "OSL", "")
    assert exported_numbers(workspace) == ["ZZ1234", "ZZ300"]


def test_a_correction_that_introduces_an_unknown_airport_is_flagged(workspace):
    run_passes(workspace, [GOOD])

    result = decide(workspace, "2025-05-12", "ZZ1234", "--set", "origin=xqy")

    assert "  unknown airport XQY (blocking)" in result.stdout.splitlines()
    assert field(result.stdout, "keep") == "no (pre-set)"
    assert decision_rows(workspace)["2025-05-12 ZZ1234"] == (
        "no", "2025-05-12", "ZZ1234", "XQY", "OSL", "unknown airport XQY; low confidence")


def test_each_field_can_be_corrected_and_is_normalised(workspace):
    run_passes(workspace, [GOOD, OTHER])

    result = decide(workspace, "2025-06-01", "QQ410", "--set", "date=2025-06-02", "--set", "flight_number=qq-411",
                    "--set", "origin= lhr ", "--set", "destination=cph")

    lines = result.stdout.splitlines()
    for line in ("  date:          2025-06-01 -> 2025-06-02", "  flight number: QQ410 -> QQ411",
                 "  origin:        AMS -> LHR", "  destination:   KEF -> CPH"):
        assert line in lines
    assert decision_rows(workspace)["2025-06-01 QQ410"] == ("yes", "2025-06-02", "QQ411", "LHR", "CPH", "")


def test_a_corrected_flight_is_found_by_its_corrected_values(workspace):
    run_passes(workspace, [GOOD, OTHER])
    decide(workspace, "2025-06-01", "QQ410", "--set", "flight_number=QQ411")

    result = decide(workspace, "2025-06-01", "QQ411", "--drop")

    assert field(result.stdout, "flight number") == "QQ411"
    assert decision_rows(workspace)["2025-06-01 QQ410"][:3] == ("no", "2025-06-01", "QQ411")
    missing = workspace.run("decide", "2025-06-01", "QQ410", "--drop")
    assert missing.returncode == 1 and "no flight on 2025-06-01" in missing.stderr


def test_a_value_typed_back_to_the_original_clears_that_correction(workspace):
    run_passes(workspace, [GOOD, OTHER])
    decide(workspace, "2025-06-01", "QQ410", "--set", "origin=LHR", "--set", "destination=CPH")

    result = decide(workspace, "2025-06-01", "QQ410", "--set", "origin=ams")

    assert "  destination: KEF -> CPH" in result.stdout.splitlines()
    assert "origin:" not in result.stdout.split("corrections")[1]
    decide(workspace, "2025-06-01", "QQ410", "--set", "destination=KEF")
    shown = workspace.run("decide", "2025-06-01", "QQ410", "--keep").stdout
    assert "corrections" not in shown


@pytest.mark.parametrize("value, message", [
    ("date=2025-02-30", "not a real YYYY-MM-DD date"),
    ("date=12/05/2025", "not a real YYYY-MM-DD date"),
    # Refused by the spreadsheet route too: the two routes accept the same dates.
    ("date=2025-W20-1", "not a real YYYY-MM-DD date"),
    ("origin=", "origin is empty"),
    ("destination=  ", "destination is empty"),
])
def test_an_unreal_date_or_an_empty_airport_is_rejected_without_saving(workspace, value, message):
    run_passes(workspace, [GOOD])

    result = workspace.run("decide", "2025-05-12", "ZZ1234", "--set", value)

    assert result.returncode == 1
    assert message in result.stderr
    assert decision_rows(workspace)["2025-05-12 ZZ1234"] == ("yes", "2025-05-12", "ZZ1234", "LIS", "OSL", "")


def test_an_empty_flight_number_is_a_correction_to_no_flight_number(workspace):
    run_passes(workspace, [GOOD])

    result = decide(workspace, "2025-05-12", "ZZ1234", "--set", "flight_number=")

    assert "  no flight number (advisory)" in result.stdout.splitlines()
    assert decision_rows(workspace)["2025-05-12 ZZ1234"][2] == ""


# -- reset -------------------------------------------------------------------------------------


def test_reset_clears_the_keep_choice_and_the_corrections_but_not_the_manual_add_tick(workspace):
    run_passes(workspace, [GOOD, OLD])
    decide(workspace, "2004-08-01", "ZZ9", "--set", "flight_number=ZZ8")
    decide(workspace, "2004-08-01", "ZZ8", "--drop")
    assert workspace.run("mark-added", "2004-08-01", "ZZ8").returncode == 0

    result = decide(workspace, "2004-08-01", "ZZ8", "--reset")

    assert field(result.stdout, "flight number") == "ZZ9"
    assert field(result.stdout, "keep") == "yes (pre-set)"
    assert "corrections" not in result.stdout
    assert decision_rows(workspace)["2004-08-01 ZZ9"] == ("yes", "2004-08-01", "ZZ9", "OSL", "LIS", "")
    workspace.run("export", "--from-store", "--output-dir", "out")
    assert [m["done"] for m in read_csv(workspace.root / "out" / "manual-add.csv")] == ["yes"]


# -- usage -------------------------------------------------------------------------------------


def test_decide_needs_one_action_a_flight_and_a_store(workspace):
    no_store = workspace.run("decide", "2025-05-12", "ZZ1234", "--keep")
    assert no_store.returncode == 1 and "store not found" in no_store.stderr

    run_passes(workspace, [GOOD])
    no_action = workspace.run("decide", "2025-05-12", "ZZ1234")
    assert no_action.returncode == 2
    two_actions = workspace.run("decide", "2025-05-12", "ZZ1234", "--keep", "--drop")
    assert two_actions.returncode == 2
    neither = workspace.run("decide", "2025-05-12", "--keep")
    assert neither.returncode == 2 and "--route" in neither.stderr
    bad_field = workspace.run("decide", "2025-05-12", "ZZ1234", "--set", "airline=Fictair")
    assert bad_field.returncode == 2 and "date, flight_number, origin or destination" in bad_field.stderr
    no_equals = workspace.run("decide", "2025-05-12", "ZZ1234", "--set", "origin")
    assert no_equals.returncode == 2
    unknown = workspace.run("decide", "2025-05-13", "ZZ1234", "--keep")
    assert unknown.returncode == 1 and "no flight on 2025-05-13" in unknown.stderr
