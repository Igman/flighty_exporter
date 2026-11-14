"""What an export from the store would do, and the manual-add list kept in
step with its ticks, driven through the command line (classify, extract,
export, mark-added, run) against a fake Ollama and the tiny OpenFlights
fixture data.

``export --from-store --dry-run`` prints the plan the export screen shows:
the flights that would go in the import file, the ones that would go to the
manual-add list (with reasons and ticks) and how many are not kept, and
writes nothing. Assertions are on printed output, exit status and files.
Every email, person, booking and flight here is invented.
"""

from __future__ import annotations

from pathlib import Path

from test_decisions import FLAGGED, GOOD, NO_NUMBER, OLD, OTHER, done, edit_review, export, review, run_passes
from test_export import configure, data_rows, import_files, the_import_file
from test_extract import read_csv

STORE = "flighty_exporter.db"


def dry_run(workspace, *args: str):
    result = workspace.run("export", "--from-store", "--dry-run", *args)
    assert result.returncode == 0, result.stderr
    return result


def listed(text: str, heading: str) -> list[str]:
    """The indented lines under ``heading`` in the dry run's output."""
    lines = text.splitlines()
    start = lines.index(heading) + 1
    found = []
    for line in lines[start:]:
        if not line.startswith("  "):
            break
        found.append(line.strip())
    return found


def files_in(directory: Path) -> set[Path]:
    return {path for path in directory.rglob("*") if path.is_file()}


def test_dry_run_writes_nothing_and_matches_what_the_export_then_does(workspace):
    run_passes(workspace, [GOOD, OTHER, FLAGGED, NO_NUMBER, OLD])
    before = files_in(workspace.root)

    result = dry_run(workspace)

    assert files_in(workspace.root) == before
    assert "Dry run: nothing was written." in result.stdout
    assert result.count("flights to export") == 2
    assert result.count("flights to add by hand") == 2
    assert result.count("not kept") == 1
    assert listed(result.stdout, "Import file would hold:") == [
        "2025-05-12  ZZ1234  LIS-OSL",
        "2025-06-01  QQ410   AMS-KEF",
    ]
    assert listed(result.stdout, "Manual-add list would hold:") == [
        "[ ] 2004-08-01  ZZ9     OSL-LIS  dated before 2006",
        "[ ] 2025-08-01  -       OSL-LIS  no flight number",
    ]

    exported = export(workspace, "--from-store")

    assert exported.count("flights exported") == 2
    assert exported.count("flights to add by hand") == 2
    assert exported.count("not kept") == 1
    rows = [line.split(",") for line in data_rows(the_import_file(workspace.root))]
    assert sorted((r[0], r[3], f"{r[1]}-{r[2]}") for r in rows) == [
        ("2025-05-12", "ZZ1234", "LIS-OSL"), ("2025-06-01", "QQ410", "AMS-KEF"),
    ]
    manual = read_csv(workspace.root / "manual-add.csv")
    assert [(m["date"], m["flight_number"], m["reason"], m["done"]) for m in manual] == [
        ("2004-08-01", "ZZ9", "dated before 2006", ""),
        ("2025-08-01", "", "no flight number", ""),
    ]


def test_dry_run_shows_reasons_from_the_reference_data_and_saved_keep_choices(workspace):
    run_passes(workspace, [GOOD, OTHER, FLAGGED])
    review(workspace)
    # Keep the flight to an unknown airport, drop another.
    edit_review(workspace, lambda rows: (rows["2025-07-01 ZZ300"].update(keep="yes"),
                                         rows["2025-06-01 QQ410"].update(keep="no")))
    export(workspace)  # records the edits as decisions

    result = dry_run(workspace)

    assert result.count("flights to export") == 1
    assert result.count("not kept") == 1
    assert listed(result.stdout, "Manual-add list would hold:") == [
        "[ ] 2025-07-01  ZZ300   LIS-XQX  airport XQX not in OpenFlights data",
    ]


def test_ticks_show_in_the_dry_run_and_mark_added_refreshes_the_manual_add_list(workspace):
    run_passes(workspace, [GOOD, NO_NUMBER, OLD])
    export(workspace, "--from-store")
    manual = workspace.root / "manual-add.csv"
    import_file = the_import_file(workspace.root)
    import_bytes = import_file.read_bytes()

    ticked = workspace.run("mark-added", "2025-08-01", "--route", "OSL", "LIS")

    assert ticked.returncode == 0, ticked.stderr
    # The list on disk follows the tick at once; the import file is not rewritten.
    assert done(manual) == {("2004-08-01", "ZZ9", "OSL", "LIS"): "", ("2025-08-01", "", "OSL", "LIS"): "yes"}
    assert f"Manual-add list updated: {manual.resolve()}" in ticked.stdout
    assert import_files(workspace.root) == [import_file]
    assert import_file.read_bytes() == import_bytes
    assert listed(dry_run(workspace).stdout, "Manual-add list would hold:") == [
        "[ ] 2004-08-01  ZZ9     OSL-LIS  dated before 2006",
        "[x] 2025-08-01  -       OSL-LIS  no flight number",
    ]

    assert workspace.run("mark-added", "2025-08-01", "--route", "OSL", "LIS", "--undo").returncode == 0

    assert set(done(manual).values()) == {""}


def test_mark_added_before_any_export_writes_no_manual_add_list(workspace):
    run_passes(workspace, [GOOD, NO_NUMBER])

    ticked = workspace.run("mark-added", "2025-08-01", "--route", "OSL", "LIS")

    assert ticked.returncode == 0, ticked.stderr
    assert not (workspace.root / "manual-add.csv").exists()
    assert "Manual-add list updated" not in ticked.stdout
    assert listed(dry_run(workspace).stdout, "Manual-add list would hold:") == [
        "[x] 2025-08-01  -       OSL-LIS  no flight number",
    ]


def test_each_export_is_recorded_and_starting_fresh_deletes_every_import_file(workspace):
    run_passes(workspace, [GOOD, NO_NUMBER])
    export(workspace, "--from-store", "--output-dir", "first")
    export(workspace, "--from-store", "--output-dir", "second")
    written = [the_import_file(workspace.root / "first"), the_import_file(workspace.root / "second")]

    result = workspace.run("run", "--mailbox", str(workspace.root / "mail.mbox"), "--fresh")

    assert result.returncode == 0, result.stderr
    for path in written:
        assert f"Deleted import file: {path.resolve()}" in result.stdout
        assert not path.exists()
    assert not (workspace.root / "first" / "manual-add.csv").exists()
    assert not (workspace.root / "second" / "manual-add.csv").exists()


def test_dry_run_without_reference_data_says_so_and_writes_nothing(workspace):
    run_passes(workspace, [GOOD, NO_NUMBER])
    configure(workspace, data_dir=workspace.root / "empty-cache")
    before = files_in(workspace.root)

    result = workspace.run("export", "--from-store", "--dry-run")

    assert result.returncode == 1
    assert "airports.dat" in result.stderr and "could not be downloaded" in result.stderr
    assert files_in(workspace.root) == before


def test_dry_run_with_nothing_kept(workspace):
    run_passes(workspace, [FLAGGED])

    result = dry_run(workspace)

    assert result.count("flights to export") == 0
    assert result.count("flights to add by hand") == 0
    assert result.count("not kept") == 1
    assert listed(result.stdout, "Import file would hold:") == ["nothing (no import file would be written)"]
    assert listed(result.stdout, "Manual-add list would hold:") == ["nothing"]


def test_dry_run_needs_the_store_route(workspace):
    run_passes(workspace, [GOOD])
    review(workspace)

    result = workspace.run("export", "--dry-run")

    assert result.returncode == 2
    assert "--from-store" in result.stderr
