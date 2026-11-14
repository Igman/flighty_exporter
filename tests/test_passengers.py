"""Passenger filtering, driven through the command line (classify, extract,
review, and export where it matters) against a fake Ollama that scripts the
classifier's answer and the extraction record per message. Assertions are on
the review CSV and the printed summary only. Every email, person, booking and
flight here is invented.
"""

from __future__ import annotations

import re

from test_consolidation import Mail, flights, record, run, seg
from openflights_fixtures import unreachable_url, write_reference_data
from test_export import data_rows, the_import_file

ROBIN = "Robin Quill"
SAM = "Sam Ferrow"

FILTERING_OFF = "Passenger filtering is off"


def names_config(workspace, *names: str, extra: str = "") -> None:
    listed = ", ".join(f'"{name}"' for name in names)
    workspace.config_path.write_text(
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[passenger]\nnames = [{listed}]\n{extra}',
        encoding="utf-8",
    )


def booking(subject: str, day: str, reference: str | None, *segments, passengers=(ROBIN,), kind="booking",
            email_type="booking confirmation") -> Mail:
    return Mail(subject, day, email_type, record(kind, reference, *segments, passengers=tuple(passengers)))


def dropped_lines(review) -> list[str]:
    """The lines listing dropped bookings, under the dropped count."""
    lines = review.stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if "bookings for other passengers (dropped)" in line)
    listed = []
    for line in lines[start + 1:]:
        if not line.startswith("    "):
            break
        listed.append(line.strip())
    return listed


def dropped_count(review) -> int:
    match = re.search(r"bookings for other passengers \(dropped\): (\d+)", review.stdout)
    assert match, review.stdout
    return int(match.group(1))


# -- the acceptance cases -----------------------------------------------------------


def test_another_passengers_booking_is_dropped_and_listed_in_the_summary(workspace):
    names_config(workspace, ROBIN)
    review, rows = run(workspace, [
        booking("Your trip OWN111", "01", "OWN111", seg("2025-06-01", "ZZ100", "LIS", "OSL")),
        booking("Booking SAM222 for Sam", "02", "SAM222", seg("2025-07-01", "ZZ200", "OSL", "CPH"),
                seg("2025-07-05", "ZZ201", "CPH", "OSL"), passengers=(SAM,)),
    ])

    assert flights(rows) == [("2025-06-01", "ZZ100", "LIS", "OSL")]
    assert rows[0]["needs_attention"] == "" and rows[0]["attention_reason"] == ""
    assert dropped_count(review) == 1
    assert dropped_lines(review) == ["SAM222: Sam Ferrow (2 flights, first 2025-07-01)"]
    assert review.count("flights needing attention") == 0
    assert FILTERING_OFF not in review.stdout


def test_multi_passenger_booking_is_kept_when_any_passenger_matches(workspace):
    names_config(workspace, ROBIN)
    review, rows = run(workspace, [
        booking("Family trip FAM333", "01", "FAM333", seg("2025-06-01", "ZZ300", "LIS", "MAD"),
                passengers=(SAM, "Alex Ferrow", "QUILL/ROBIN MR")),
        booking("Group trip GRP444", "02", "GRP444", seg("2025-06-10", "ZZ400", "MAD", "LIS"),
                passengers=(SAM, "Alex Ferrow")),
    ])

    assert flights(rows) == [("2025-06-01", "ZZ300", "LIS", "MAD")]
    assert rows[0]["passengers"] == "Sam Ferrow; Alex Ferrow; QUILL/ROBIN MR"
    assert dropped_lines(review) == ["GRP444: Sam Ferrow; Alex Ferrow (1 flight, first 2025-06-10)"]


def test_booking_naming_no_passenger_is_kept_and_flagged(workspace):
    names_config(workspace, ROBIN)
    review, rows = run(workspace, [
        booking("Your itinerary NON555", "01", "NON555", seg("2025-06-01", "ZZ500", "LIS", "OSL"), passengers=()),
        # A "name" that is only a title names nobody either.
        booking("Your itinerary TTL666", "02", "TTL666", seg("2025-06-02", "ZZ600", "OSL", "LIS"),
                passengers=("MR", " ")),
        booking("Your itinerary OWN777", "03", "OWN777", seg("2025-06-03", "ZZ700", "LIS", "MAD")),
    ])

    assert [(r["flight_number"], r["keep"], r["needs_attention"], r["attention_reason"]) for r in rows] == [
        ("ZZ500", "yes", "yes", "no passenger named"),
        ("ZZ600", "yes", "yes", "no passenger named"),
        ("ZZ700", "yes", "", ""),
    ]
    assert review.count("flights needing attention") == 2
    assert dropped_count(review) == 0
    assert dropped_lines(review) == []


def test_matching_ignores_case_accents_word_order_punctuation_and_titles(workspace):
    names_config(workspace, ROBIN)
    formats = [
        "QUILL/ROBIN MR",
        "QUILL/ROBINMR",  # title glued to the given name
        "QUILL/ROBINMRS",
        "Quill, Robin",
        "Mr Robin Quill",
        "Dr. Robin Quill",
        "ROBIN QUILL",
        "Röbin Qüill",
        "QUILL ROBIN MSTR",
        "M. Robin QUILL",
    ]
    mails = [
        booking(f"Format check {i}", f"{i + 1:02d}", f"FMT{i:03d}",
                seg(f"2025-06-{i + 1:02d}", f"ZZ{i + 1}", "LIS", "OSL"), passengers=(name,))
        for i, name in enumerate(formats)
    ]
    mails.append(booking("Near miss", "20", "MISS99", seg("2025-07-01", "ZZ99", "LIS", "OSL"),
                         passengers=("Robin Quillon",)))
    mails.append(booking("Near miss glued", "21", "MISS98", seg("2025-07-02", "ZZ98", "LIS", "OSL"),
                         passengers=("QUILLON/ROBINMR",)))
    review, rows = run(workspace, mails)

    assert [r["passengers"] for r in rows] == formats
    assert all(r["needs_attention"] == "" for r in rows)
    assert dropped_lines(review) == [
        "MISS99: Robin Quillon (1 flight, first 2025-07-01)",
        "MISS98: QUILLON/ROBINMR (1 flight, first 2025-07-02)",
    ]


def test_accented_configured_name_matches_a_plain_ticket(workspace):
    names_config(workspace, "Zoë Ångström-Nuñez")
    review, rows = run(workspace, [
        booking("Ticket ACC111", "01", "ACC111", seg("2025-06-01", "ZZ111", "LIS", "OSL"),
                passengers=("ANGSTROMNUNEZ/ZOE MS",)),
        booking("Ticket ACC222", "02", "ACC222", seg("2025-06-02", "ZZ222", "LIS", "OSL"),
                passengers=("Nunez, Zoe Angstrom",)),
        booking("Ticket ACC333", "03", "ACC333", seg("2025-06-03", "ZZ333", "LIS", "OSL"),
                passengers=("Zoe Nunez",)),
    ])

    # A hyphenated surname written run together is a different token: no fuzzy
    # matching, so the user lists that spelling as a variant if airlines use it.
    assert [r["flight_number"] for r in rows] == ["ZZ222", "ZZ333"]
    assert dropped_lines(review) == ["ACC111: ANGSTROMNUNEZ/ZOE MS (1 flight, first 2025-06-01)"]


# -- name variants ------------------------------------------------------------------


def test_middle_names_match_both_ways(workspace):
    names_config(workspace, ROBIN, "Casey Morrow Lind")
    review, rows = run(workspace, [
        # The ticket has a middle name the configured name lacks, in any position.
        booking("Ticket MID111", "01", "MID111", seg("2025-06-01", "ZZ11", "LIS", "OSL"),
                passengers=("QUILL/ROBIN AVERY MR",)),
        booking("Ticket MID222", "02", "MID222", seg("2025-06-02", "ZZ12", "LIS", "OSL"),
                passengers=("Avery Robin Quill",)),
        # The configured name has a middle name the ticket leaves out.
        booking("Ticket MID333", "03", "MID333", seg("2025-06-03", "ZZ13", "LIS", "OSL"),
                passengers=("LIND/CASEY MS",)),
        # Two tokens of the configured name that are not the person's full name still match:
        # all the ticket's tokens are in the configured name.
        booking("Ticket MID444", "04", "MID444", seg("2025-06-04", "ZZ14", "LIS", "OSL"),
                passengers=("Morrow Lind",)),
        # One extra token not in the configured name, and the configured name incomplete: no match.
        booking("Ticket MID555", "05", "MID555", seg("2025-06-05", "ZZ15", "LIS", "OSL"),
                passengers=("Casey Lind Parker",)),
    ])

    assert [r["flight_number"] for r in rows] == ["ZZ11", "ZZ12", "ZZ13", "ZZ14"]
    assert dropped_lines(review) == ["MID555: Casey Lind Parker (1 flight, first 2025-06-05)"]


def test_single_token_passenger_matches_only_a_single_token_name(workspace):
    mails = [
        booking("Ticket ONE111", "01", "ONE111", seg("2025-06-01", "ZZ21", "LIS", "OSL"), passengers=("Robin",)),
        booking("Ticket ONE222", "02", "ONE222", seg("2025-06-02", "ZZ22", "LIS", "OSL"), passengers=("MR QUILL",)),
    ]
    names_config(workspace, ROBIN)
    review, rows = run(workspace, mails, name="full")

    assert rows == []
    assert dropped_count(review) == 2

    names_config(workspace, ROBIN, "Robin")
    review, rows = run(workspace, mails, name="single")

    assert [r["flight_number"] for r in rows] == ["ZZ21"]
    assert dropped_lines(review) == ["ONE222: MR QUILL (1 flight, first 2025-06-02)"]


# -- passengers across the records of a booking --------------------------------------


def test_passengers_are_the_union_across_a_bookings_records(workspace):
    names_config(workspace, ROBIN)
    review, rows = run(workspace, [
        # The confirmation names nobody; the check-in names the user.
        booking("Confirmation LAT111", "01", "LAT111", seg("2025-06-01", "ZZ31", "LIS", "OSL"),
                seg("2025-06-08", "ZZ32", "OSL", "LIS"), passengers=()),
        booking("Check in for ZZ31", "05", "LAT111", seg("2025-06-01", "ZZ31", "LIS", "OSL"),
                passengers=("QUILL/ROBINMR",), kind="check-in", email_type="check-in or boarding pass"),
        # The confirmation names nobody; the check-in names someone else.
        booking("Confirmation LAT222", "02", "LAT222", seg("2025-07-01", "ZZ41", "LIS", "MAD"), passengers=()),
        booking("Check in for ZZ41", "06", "LAT222", seg("2025-07-01", "ZZ41", "LIS", "MAD"),
                passengers=(SAM,), kind="check-in", email_type="check-in or boarding pass"),
    ])

    assert [(r["flight_number"], r["needs_attention"]) for r in rows] == [("ZZ31", ""), ("ZZ32", "")]
    # ZZ32 is only in the confirmation, but its booking names the user.
    assert [r["passengers"] for r in rows] == ["QUILL/ROBINMR", "QUILL/ROBINMR"]
    assert review.count("flights needing attention") == 0
    assert dropped_lines(review) == ["LAT222: Sam Ferrow (1 flight, first 2025-07-01)"]


def test_another_passengers_booking_stays_dropped_after_a_change(workspace):
    names_config(workspace, ROBIN)
    review, rows = run(workspace, [
        booking("Booking CHG111 for Sam", "01", "CHG111", seg("2025-06-01", "ZZ51", "LIS", "OSL"),
                seg("2025-06-09", "ZZ52", "OSL", "LIS"), passengers=(SAM,)),
        # The schedule change names no passenger; the booking is still Sam's.
        booking("Schedule change CHG111", "04", "CHG111", seg("2025-06-02", "ZZ53", "LIS", "OSL"),
                passengers=(), kind="change", email_type="change"),
    ])

    assert rows == []
    assert review.count("flights needing attention") == 0
    assert dropped_lines(review) == ["CHG111: Sam Ferrow (2 flights, first 2025-06-02)"]
    assert review.count("bookings with replaced segments") == 0


def test_same_flight_in_own_and_another_passengers_booking_stays_sourced_from_own(workspace):
    names_config(workspace, ROBIN)
    review, rows = run(workspace, [
        booking("Robin's ticket SHR111", "01", "SHR111", seg("2025-06-01", "ZZ61", "LIS", "OSL")),
        booking("Sam's ticket SHR222", "02", "SHR222", seg("2025-06-01", "ZZ 61", "LIS", "OSL"),
                passengers=(SAM,), email_type="check-in or boarding pass", kind="check-in"),
    ])

    [row] = rows
    assert row["flight_number"] == "ZZ61"
    assert row["booking_reference"] == "SHR111"
    assert row["passengers"] == ROBIN
    assert row["source_count"] == "1"
    assert row["message_subject"] == "Robin's ticket SHR111"
    assert row["evidence"] == "booked only"  # Sam's check-in does not confirm Robin's flight
    assert dropped_lines(review) == ["SHR222: Sam Ferrow (1 flight, first 2025-06-01)"]


# -- filtering off --------------------------------------------------------------------


def test_with_no_names_configured_nothing_is_filtered_or_flagged_and_the_tool_says_so(workspace):
    review, rows = run(workspace, [
        booking("Booking OFF111 for Sam", "01", "OFF111", seg("2025-06-01", "ZZ71", "LIS", "OSL"), passengers=(SAM,)),
        booking("Your itinerary OFF222", "02", "OFF222", seg("2025-06-02", "ZZ72", "OSL", "LIS"), passengers=()),
    ])

    assert [(r["flight_number"], r["keep"], r["needs_attention"], r["attention_reason"]) for r in rows] == [
        ("ZZ71", "yes", "", ""),
        ("ZZ72", "yes", "", ""),
    ]
    assert dropped_count(review) == 0
    assert review.count("flights needing attention") == 0
    assert FILTERING_OFF in review.stdout
    assert "[passenger]" in review.stdout and "names = [" in review.stdout


def test_an_empty_names_list_also_turns_filtering_off(workspace):
    names_config(workspace)
    review, rows = run(workspace, [
        booking("Booking EMP111 for Sam", "01", "EMP111", seg("2025-06-01", "ZZ81", "LIS", "OSL"), passengers=(SAM,)),
    ])

    assert len(rows) == 1
    assert FILTERING_OFF in review.stdout


def test_names_that_are_not_a_list_of_names_are_a_clear_config_error(workspace):
    for setting in ('names = "Robin Quill"', 'names = ["Robin Quill", 3]', 'names = ["Mr"]'):
        workspace.config_path.write_text(
            f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[passenger]\n{setting}\n', encoding="utf-8"
        )
        result = workspace.run("review")
        assert result.returncode == 1
        assert "[passenger] names" in result.stderr


# -- summary and export ----------------------------------------------------------------


def test_dropped_bookings_list_is_capped_at_twenty_lines(workspace):
    names_config(workspace, ROBIN)
    mails = [
        booking(f"Booking OTH{i:03d} for Sam", f"{i + 1:02d}", f"OTH{i:03d}",
                seg(f"2025-06-{i + 1:02d}", f"ZZ{100 + i}", "LIS", "OSL"), passengers=(SAM,))
        for i in range(22)
    ]
    # No booking reference: the line says so.
    mails.append(booking("Ticket for Sam", "28", None, seg("2025-05-01", "ZZ99", "OSL", "LIS"), passengers=(SAM,)))
    review, rows = run(workspace, mails)

    assert rows == []
    assert dropped_count(review) == 23
    listed = dropped_lines(review)
    assert len(listed) == 21
    assert listed[0] == "(no booking reference): Sam Ferrow (1 flight, first 2025-05-01)"
    assert listed[1] == "OTH000: Sam Ferrow (1 flight, first 2025-06-01)"
    assert listed[19] == "OTH018: Sam Ferrow (1 flight, first 2025-06-19)"
    assert listed[20] == "and 3 more"


def test_export_ignores_the_attention_columns(workspace):
    data_dir = write_reference_data(workspace.root / "openflights")
    names_config(workspace, ROBIN, extra=f'\n[export]\ndata_dir = "{data_dir.as_posix()}"\ndata_url = "{unreachable_url()}"\n')
    review, rows = run(workspace, [
        booking("Your itinerary EXP111", "01", "EXP111", seg("2025-05-12", "ZZ1234", "LIS", "OSL"), passengers=()),
    ], name="review")
    assert [(r["needs_attention"], r["attention_reason"]) for r in rows] == [("yes", "no passenger named")]

    result = workspace.run("export", "--review", str(workspace.root / "review-review.csv"))

    assert result.returncode == 0, result.stderr
    assert data_rows(the_import_file(workspace.root)) == [
        '2025-05-12,LIS,OSL,ZZ1234,Fictair,1714,03:55,,,,,,,,"",1638,644,90001,'
    ]
    assert result.count("rejected as invalid") == 0
