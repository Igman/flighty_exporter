# Manual checks

The application's screens are kept free of logic and checked by hand, not by
the test suite (see the spec, issue #15, "Testing Decisions"). Before a
release, or after changing a screen, walk the sections below in a real
terminal, in order: they follow the screens as a user meets them. Each step
says what to do and what you should see. Record each walk in
[Recorded walkthroughs](#recorded-walkthroughs).

## Before you start

**A scratch folder.** Work in an empty folder outside the repository, with a
copy of the synthetic mailbox (invented mail only), and run the tool from
there through the repository's environment:

```
mkdir -p ~/scratch/flighty-check && cd ~/scratch/flighty-check
cp /path/to/flighty_exporter/test.mbox .
alias fe='uv run --project /path/to/flighty_exporter flighty-exporter'
```

`fe` opens the application; `fe session`, `fe flights` and so on run the
commands against the same files. Every file the tool writes lands in this
folder. A config file for the checks, once setup has written one, looks
like:

```toml
mailbox = "test.mbox"

[passenger]
names = ["Jordan Ellis"]

[run]
thoroughness = "quick"
```

To start a check from scratch, delete `flighty_exporter.toml`,
`flighty_exporter.db*`, `review.csv`, `failures.csv`, `audit.csv`,
`manual-add.csv` and `openflights-backup-*.csv` (or `fe run --fresh` with
the config in place, which deletes only what the tool wrote).

**Simulating server problems without touching your models.**

- *Model server not running:* set `[ollama] address = "http://localhost:9"`
  (nothing listens there). Remove the line to bring the server back.
- *A missing model:* set `[models] decision = "fictional-reader:3b"` (or
  `extraction`). The name is not pulled, so the checks report it missing,
  and a download attempt fails without fetching anything. To see a real
  download, use a small model you are happy to have pulled.
- *A non-local address:* set `[ollama] address = "http://192.0.2.10:11434"`
  (a documentation address).
- *An old server* can only be checked with an older Ollama at hand.

Never delete or re-pull your real models for these checks.

**What to expect from `test.mbox`.** With `names = ["Jordan Ellis"]`, a
Quick run with the default models finds 7 flights: 2017-10-05 IB3170
MAD-BCN, 2018-07-22 AC759 YYZ-SFO, 2019-03-14 UA328 SFO-ORD, 2021-09-02
BA286 SFO-LHR (confirmed), 2021-09-16 BA285 LHR-SFO, 2022-05-20 LH455
SFO-FRA and 2022-05-21 LH1238 FRA-VIE. `test.mbox.truth.json` lists the flight
emails behind them. Model output varies a little between versions; note any
difference in the walkthrough record.

## 1. Launching

1. **Not a terminal.** `fe | cat` and `fe < /dev/null` do not open the
   application: each prints the help (the same text as `fe --help`) and
   then, on stderr, "error: the application opens only in a terminal …
   otherwise give a command, such as run", and exits with status `2`.
2. **Help.** `fe --help` lists every command; each `fe <command> --help`
   matches the [command reference](../README.md#command-reference).

## 2. Guided setup

Start from the empty scratch folder (no config, no store).

1. **First screen.** `fe` opens "Setup: three questions", with the config
   file's path on a line of its own (broken after a `/` if long, not in the
   middle of a name), three numbered questions with explanations, a "You
   entered" panel and a "Save and check the model server (Ctrl-S)" button.
   The mailbox field is empty and focused, the names are empty, Quick is
   selected; the footer shows `^s Save and continue`.
2. **Mailbox explanation.** Under "1. Your mailbox file": the Takeout
   `.mbox` file, that dragging it into the terminal pastes its path, and the
   folder a relative path is read from, on its own line.
3. **Bad mailbox paths.** Type each and press Tab; a red message appears
   under the field and nothing is written:
   - a path that does not exist: "There is no file at …";
   - a folder: "… is a folder, not a file. Point at the mailbox (.mbox) file
     itself.";
   - an unzipped `Takeout` folder holding an .mbox: "… is a Google Takeout
     folder, not a file. Point at the .mbox file inside it: …";
   - a `.zip` file: "… is a zip archive, not a mailbox …";
   - an empty file: "… is empty …";
   - a text file: "… does not look like an mbox file … this file begins
     with …".

   Typing in the field clears the message. Ctrl-S with a bad path shows the
   message, focuses the field and stays on setup.
4. **Drag and drop.** Drag `test.mbox` from Finder into the field: the
   pasted path (quoted, or with `\ ` before spaces) is accepted.
5. **Names.** Under "2. Your name variants": why names are asked, one per
   line or comma-separated, that "ELLIS/JORDAN MR" already matches "Jordan
   Ellis", the example, and in bold that empty turns passenger filtering
   off. Type `Jordan Ellis` and `Jordan A. Ellis` on two lines: the summary
   lists both. Add a line `Mr` and press Ctrl-S: "'Mr' is not a name" under
   the field, nothing written. Empty names: the summary reads "none:
   passenger filtering is off, every booking is kept".
6. **Thoroughness.** Under "3. How thorough to be": Quick and Thorough with
   a sentence each (Thorough: roughly 2 to 35 minutes more, and a lowered
   threshold within the budget, shown as a number of emails). Select one
   with the arrow keys and Space or Enter: the summary follows.
7. **Save.** With `test.mbox`, `Jordan Ellis` and Quick, press Ctrl-S. The
   model-server step opens, and `flighty_exporter.toml` exists with
   `mailbox = "test.mbox"` (relative), `[passenger] names = ["Jordan
   Ellis"]` and `[run] thoroughness = "quick"` under a comment saying setup
   wrote it.
8. **An existing config is kept.** With a copy of
   `flighty_exporter.example.toml` as the config (and no store), launch:
   setup is filled in from it and the Save button is focused; press Enter.
   Only the `mailbox` line, `names` and `thoroughness` changed in the file.
9. **Server ready.** With Ollama running and both models pulled: "On this
   machine: yes", "Reachable: yes", the version "(new enough)", each model
   "ready", and "Ready. Press s to start the run." `s` opens the progress
   screen.
10. **Server not running.** With the address pointed at `localhost:9`,
    launch and save. "Reachable: no" and a Problem panel ("… is not
    reachable … Start it with `ollama serve`, or open the Ollama app, then
    rerun the check (press c).", then "Press c to check again once it is
    fixed."); the footer offers `c`, not `s`. Restore the address, press `c`:
    the panel goes and the server is reported.
11. **Non-local address.** With the documentation address: "On this
    machine: no", the refusal ("email content would leave this machine")
    and the guidance that `--allow-remote` does not apply in the
    application. Only `b` and `q` are offered.
12. **Missing model.** With `[models] decision = "fictional-reader:3b"`: its
    line reads "missing", says the size is not known until the download
    starts, and "(press d)" (a missing default model shows "about 9.3 GB" or
    "about 8.0 GB").
    `d` asks "Download fictional-reader:3b?" with the size; `n` closes it and
    nothing downloads (`ollama list` unchanged).
13. **Download error.** `d`, then `y`: the Problem panel shows the server's
    error ("… could not pull fictional-reader:3b: …") and "Press d to try
    again"; `d` and `c` are offered.
14. **Download progress** (only with a model you are happy to pull). `d`,
    `y`: a "Downloading …" panel with a progress bar and "X of Y · pulling
    …", then status texts; when done, a notice and a fresh check showing the
    model "ready". `q` during a download quits at once (status `0`); a later
    download resumes.
15. **Back.** On the model-server step, `b` returns to setup with the saved
    answers filled in.

## 3. The run

From the model-server step press `s` (or, with an unfinished session,
resume from the startup prompt, section 6).

1. **Start.** The screen shows "Mailbox:" with the path on its own line(s)
   and the thoroughness, a status line, and a Stages panel listing classify,
   extract and review (Thorough: classify, extract, audit, include, review),
   each "waiting". Footer: `p Pause`, `q Quit`.
2. **Progress.** The running stage reads "running"; a Current stage panel
   shows a bar, "N of TOTAL messages", the seconds per message ("rate not
   known yet" at first) and the time left in words. Nothing is printed over
   the screen.
3. **Counts.** Classify: classified, resolved from machine-readable data,
   skipped, failed; extract: "extracted N of M" and failed. They are the
   session's totals: after classify, the four add up to the messages in the
   mailbox. `fe run` prints the same figures after each stage as "Session
   totals so far: …".
4. **Pause and continue.** `p`: "Pausing after the current message…", then
   "Paused. Press p to continue."; the stage reads "stopped" and no further
   requests reach Ollama (`ollama ps`). `p` again: finished stages show
   "nothing to do", the stage skips what is done and carries on; no count
   drops back.
5. **Quit and resume.** During extract press `q`: "Stopping after the
   current message… (press q again to quit now)", then the application
   closes with status `0`. The next launch shows the startup prompt (section
   6); resume and extract continues with the remaining messages only.
6. **Quit at once.** `q` twice (or `q` then Ctrl-C) during a model request:
   the application closes immediately, status `0`; the store is intact
   (`fe session` works) and at most one message is redone.
7. **Problem and retry.** With the address at `localhost:9`, relaunch an
   unfinished session and resume: a red Problem panel with the same message
   `run` prints ("cannot reach Ollama at …" or "… is not reachable …"), the
   stage "stopped", footer `r Retry`, `q Quit`. `r` shows the panel again
   (the config is read at launch): quit, restore the address, relaunch. With
   the real server instead, quit the Ollama app during extract: the panel
   appears after the message in progress; start Ollama and press `r`: the
   run carries on, nothing finished is sent again.
8. **Model missing.** With `[models] extraction = "fictional-reader:3b"`,
   after classify the Problem panel shows the "pull it with `ollama pull …`"
   message.
9. **Thorough stages.** A Thorough session lists five stages. During audit
   the bar counts sampled messages; afterwards Counts shows "audit: sampled
   S · extracted E of S · failed F". With `test.mbox` the include stage
   either reads "skipped" with its reason under it, or runs over the newly
   included messages; the report's "check for missed flight emails" line
   then reads like "threshold lowered from 0.05 to 0.02 for this session; N
   emails newly included by the lower threshold (A already read by the
   check, R read now), F of them with flights" (fix d).
10. **Finished.** Every stage "done" or "nothing to do"; a Report panel shows
    the same report `fe run` prints (flights, needing attention, cancelled,
    replaced, dropped, failed, files written) ending with "Next step: press
    enter to review the flights (or q to quit)." and not with "then run
    export" (fix a). Footer: `⏎ Review flights`, `q Quit`.
11. **Narrow terminal.** At about 50 columns, text wraps inside the panels,
    paths break after a `/`, nothing is cut off sideways, and the screen
    scrolls; the footer stays.

## 4. The review

From a finished run, press `enter`. Compare with `fe flights`, which lists the
same flights in the same order.

1. **Only after a complete run.** While paused or with a Problem panel,
   `enter` does nothing and the footer offers no review key.
2. **Table and totals.** Columns date, flight, route, airline, booking,
   evidence, keep, corrected, reasons. Blocking reasons (red) first, then
   advisory (yellow), then the rest, each by date. The first line reads
   "Flights: N · kept K · not kept M · needing attention A" (as `fe
   flights`), the second "Showing: all flights (N of N flights)".
3. **Detail panel.** Moving with the arrows, the panel follows: every field,
   "keep: … (pre-set)" or "(your choice; pre-set …)", each reason with
   "(blocking)" or "(advisory)", "source emails: N" with dates and
   subjects; BA286 shows two or more source emails. When the detail is
   longer than its panel, Tab moves the focus to the panel and the arrows
   scroll it; Tab again returns to the table.
4. **Toggle keep.** Space on a flight: its keep cell and the totals change
   at once, the cursor stays, the detail says "your choice". `k` toggles
   back. `fe flights` in another terminal shows each change.
5. **Correct and correct back.** `c` on a flight: a form with date, flight
   number, origin, destination. Type `2025-02-30` and `enter`: "date
   '2025-02-30' is not a real YYYY-MM-DD date; nothing was saved." Empty the
   origin: "origin is empty". Type a flight number as `ua-328` for UA328:
   saving shows `UA328` and no correction, because it equals what was found.
   Change the destination to an unknown code (`XQY`): the row moves to the
   flagged group with "unknown airport XQY", keep "no", "corrected", and the
   detail lists "corrections (found -> corrected):". `c` again, type the
   original code back, `enter`: the correction disappears and the flight
   returns to its place. `c` then Escape changes nothing.
6. **Clear a decision.** `u` on a toggled flight returns it to the pre-set
   keep; on a corrected flight it asks first (`n` or Escape keeps it, `y`
   clears it).
7. **Filters.** `f`: "Showing: flagged only (A of N flights)". `n`: adds
   "not kept only". `y` steps through each year, oldest first, then back to
   all; a combination matching nothing says "No flights match these
   filters.". With the not-kept filter, Space keeps a flight and it leaves
   the table. The totals line never changes with the filters.
8. **Source emails.** `enter` on BA286: "[1/2]" and "[2/2]" (or more) with
   each email's date and subject and its saved text (From, Subject, Date,
   body, PDF text); it scrolls; Escape returns to the same flight. Rename
   `test.mbox` away first: the text still shows. The heading matches the
   flight's line in `fe sources`; a flight without a number reads "(no
   flight number)".
9. **Lists.** `d`: "Bookings dropped as other passengers': N", one line per
   booking (reference, passengers, flights, first date); Escape returns.
   `x`: "Emails that could not be read: N", one line per email; Escape
   returns.
10. **Manual-add marker** (fix f). Keep a flight with no flight number (or
    one before 2006, or with an unknown airport): its detail shows "added by
    hand in Flighty: no (on the manual-add list: …)". After ticking it on
    the export screen (section 5) and coming back, it reads "yes". A flight
    that goes in the import file shows no such line. `fe decide` on it
    prints the same line.
11. **Saved across launches.** Make a keep change and a correction, `q`
    (status `0`), relaunch, resume: the same decisions show. `fe review
    --force` writes a table with them and `fe export --from-store` exports
    them. A plain `fe export` exports them too, even with an untouched
    `review.csv` written before the decisions: its summary says the store
    was the source and that the review table is as review wrote it.
12. **No flights.** With a mailbox without flight emails: "No flights
    found.", where to look, the count of unreadable emails (`x`), and for a
    Quick run that Thorough exists. The footer offers only `d`, `x`, `e`,
    `q`.

## 5. The export

From the review press `e`. Compare with `fe export --from-store --dry-run`.

1. **Plan.** The status line says nothing is sent anywhere and to press
   `e`. The Plan panel gives the flights for the import file, to add by hand
   and not kept: the dry run's three numbers. No file is written yet. The
   footer shows `e Export`, `space Tick` (only with manual-add flights), `b
   Back to review`, `q Quit`, not `o`.
2. **Checklist.** "Add by hand in Flighty" lists each manual-add flight with
   `[ ]`/`[x]`, date, route (`OSL → LIS`), flight number (`—` if none) and
   reason, with "Ticked N of M" above; with none it says "No flight needs
   adding by hand: every kept flight goes in the import file."
3. **Export.** `e` (or Enter): "Writing the files…", then "Export written".
   The "Import file" panel shows the full path on its own line(s), broken
   after a `/` (`\` on Windows) (fix h), "Press o to show it in Finder."
   ("in File Explorer." on Windows), and "Manual-add
   list:" with its path on the next line. The "Importing into Flighty"
   panel lists the steps and the caveat. The import file holds as many
   flights as the plan said; `fe session` lists both files.
4. **Reveal.** `o` opens Finder (macOS) or File Explorer (Windows) with the
   import file selected.
5. **Tick.** Space on a manual-add flight: `[x]`, the count goes up, and
   `manual-add.csv` has `done` = `yes` at once, without a new export (the
   import file's time is unchanged). Space again unticks.
6. **Back keeps the review's place** (fix e). In the review, set the
   flagged filter and a year, select a flight that is not the first row,
   press `e`, then `b`: the review shows the same filters ("Showing: …") and
   the same flight selected. Change a decision, `e` again: the plan shows
   the new numbers. Wait for the next minute and export: a second import
   file appears (an export in the same minute replaces the first).
7. **Reference data missing.** With `[export] data_dir` an empty folder and
   `data_url = "http://127.0.0.1:9/data/"`: a red Problem panel with the
   message `export` prints and "Nothing was exported"; footer `r`, `b`,
   `q`; `e` does nothing.
8. **Nothing kept.** Drop every flight, open the export: "No flight is
   kept …", plan 0, 0 and the not-kept number; only `b` and `q`.

## 6. Resume or start fresh (later launches)

1. **Unfinished session.** Quit during the run, relaunch: "A previous
   session was found." and a panel titled "Session" (no path in the title,
   nothing cut off: fix h) with the store and mailbox each on their own
   line(s), started and last active, names, thoroughness, then "How far it
   got": "classify N emails read" with the line "(messages under Spam,
   Trash, Drafts and Chat are not counted)" under it (fix c), extract "N of
   M likely flight emails extracted", review, threshold and files; then "The
   run is unfinished" with what is left. Footer `r Resume`, `f Start fresh`,
   `q Quit`. The figures match `fe session`, and the sentence about the run
   is its "where it stands" line.
2. **Resume unfinished.** `r`: the progress screen carries on.
3. **Finished session.** After a finished run, relaunch: "The run is
   complete. Resuming opens its review." `r` opens the review at once with
   the saved decisions, no model request. If an email could not be read,
   the run is still complete: a line under it says "1 likely flight email
   could not be read; the review lists it (x) …", and `fe session` shows
   "state: finished; 1 likely flight email could not be read …" with "run
   finished: yes", never "unfinished".
4. **Threshold lowered.** After a Thorough run that lowered it, the
   threshold line reads like "0.02 (lowered for this session from the
   configured 0.05)".
5. **Mailbox moved.** Rename `test.mbox` and relaunch: the Mailbox label
   says "(no longer there)" in red. Finished: a note says it does not matter
   for the review, and `r` opens it. Unfinished: a paragraph says resuming
   would stop with "mailbox file not found".
6. **Start fresh, cancelled.** `f`: "Start fresh?" listing what would be
   lost and what is kept. After a Thorough run the extraction line separates
   the two counts, e.g. "extraction of 16 emails, and 77 more read by the
   check for missed flight emails" (fix b); it matches `fe session`'s
   "Starting fresh would lose". `n` or Escape closes it; nothing is deleted.
7. **Start fresh, confirmed.** `f`, `y`: setup opens with "Started fresh."
   and one line per deleted file (review table, failures file, audit report,
   import files, manual-add list, the store). On disk those files and
   `flighty_exporter.db*` are gone; `test.mbox`, `flighty_exporter.toml` and
   any file of your own are still there, as are your models (`ollama list`)
   and `~/.cache/flighty_exporter/openflights`. The answers are filled in,
   the mailbox as typed in setup (`test.mbox`, not a full path), as
   `fe session --fresh-preview` listed them.
8. **Quit.** `q` exits with status `0`.

## Recorded walkthroughs

Add a row each time the list is walked. "Automated" means driven with keys
in a pseudo-terminal and the screen read back; steps that need a person
(Finder, drag and drop, mouse, a real download) are listed as not walked.

| Date | Who | Tool version (commit) | Ollama, models | How | Sections walked | Result | Defects found |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-14 | agent (Claude), for the owner | `b52ba94` (start-fresh notice re-checked on `0452572`) | Ollama 0.40.16, `nimble`, `gemma4:12b-it-q4_K_M` | Automated: keys sent in a 100x40 pseudo-terminal, screen read with pyte | 1; 2.1, 2.3 (missing file), 2.5, 2.7, 2.9; 3.1-3.4, 3.10; 4.2-4.10 (4.5 with `ua-328` and `XQY`, then back); 5.1-5.3, 5.5, 5.6; 6.1, 6.3, 6.6, 6.7 (Quick session, then a Thorough one made with `run --thoroughness thorough`). Not walked: 2.4, 2.8, 2.10-2.15, 3.5-3.9, 3.11, 4.1, 4.11, 4.12, 5.4 (Finder), 5.7, 5.8, 6.2, 6.4, 6.5 | Pass: 7 flights, as in the truth file; export wrote 7 rows; start fresh deleted the recorded files and the store and kept `test.mbox`, the config, a file of the walker's own, the models and the reference-data cache | The review footer at 100 columns ends at `x Unreadable`, so `e Export` and `q Quit` are not visible; the reasons column is cut at 100 columns; the setup mailbox error and the report's file paths broke mid-name (the latter fixed in the same pass) |
