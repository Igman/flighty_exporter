# Manual checks

The application's screens are kept thin and are checked by hand, not by the
test suite (see the spec, issue #15, "Testing Decisions"). Before a release,
or after changing a screen, work through that screen's list in a real
terminal. Each step says what to do and what you should see.

Use invented mail for these checks: `test.mbox` (with `[passenger] names =
["Jordan Ellis"]`) and a local Ollama with the configured models pulled.
Start from a folder holding a config file like:

```toml
mailbox = "test.mbox"
store = "check.db"

[passenger]
names = ["Jordan Ellis"]
```

(Setup writes such a file for you: `uv run flighty-exporter setup --mailbox
test.mbox --name "Jordan Ellis"`, then add the `store` line.)

Delete `check.db`, `review.csv` and `failures.csv` to start a check from
scratch.

## Startup prompt

Create a session first: launch, complete setup, let the run classify a few
messages and press `q`.

1. **Unfinished session.** Launch again. The screen says a previous session
   was found and shows, in a Session panel: the mailbox path, when the
   session started and was last active (local time), the names and
   thoroughness it was started with, how far it got (classify: N of M
   recorded emails read; extract: N of M likely flight emails extracted;
   review: flights and saved decisions; the threshold in use; files: how many
   the session wrote that are still there), and "The run is unfinished" with
   how many are still to extract. The footer offers `r Resume`, `f Start
   fresh`, `q Quit`. The figures match `uv run flighty-exporter session`.
2. **Resume an unfinished run.** Press `r`. The progress screen opens and
   carries on: classify's counts start from where the last launch stopped,
   and no finished message is sent to a model again.
3. **Finished session.** Let a run finish, quit, launch again. The prompt
   says "The run is complete. Resuming opens its review." and the `r` line
   reads "open the finished run's review" (`session` prints "run finished:
   yes"). Press `r`: the review screen opens at once, with no progress
   screen and no model request, showing your saved decisions. If some
   likely flight emails could not be read, the extract line says how many
   and a line under "The run is complete." says the review lists them (`x`);
   the session still counts as finished.
4. **Complete but not finished.** Start a Thorough session and quit during
   the audit stage. Launch: the prompt says "The run did not reach its end"
   (`session` prints "state: complete" but "run finished: no"); `r` opens
   the progress screen, which runs the audit and the remaining stages.
5. **Threshold lowered.** After a Thorough run that lowered the threshold,
   the prompt's threshold line reads like "0.02 (lowered for this session
   from the configured 0.05)".
6. **Mailbox moved, unfinished.** With an unfinished session, rename the
   mailbox file and launch. The Mailbox line ends with "(no longer there)"
   in red, and a paragraph explains that resuming reads the mailbox again
   and would stop with "mailbox file not found": put the file back, or start
   fresh. Pressing `r` anyway shows the progress screen with that error and
   `r Retry`; rename the file back and press `r` there to carry on.
7. **Mailbox moved, finished.** With a finished session, rename the
   mailbox file and launch. The Mailbox line ends with "(no longer there)",
   and a note says that does not matter for the review. `r` opens the
   review; the source emails' text (`enter`) still shows.
8. **Start fresh, cancelled.** Press `f`. A dialog asks "Start fresh?",
   lists what would be lost (for example "classification of 200 emails",
   "extraction of 31 emails", "2 generated files") and says that the
   mailbox, the config file, downloaded models and the cached reference data
   are never deleted. Press `n` (or Escape): the dialog closes and nothing is
   deleted (`check.db` is still there).
9. **Start fresh, confirmed.** Press `f`, then `y`. Setup opens with a
   green-bordered notice at the top: "Started fresh." and one line per file
   deleted (review table, failures file, the store). `check.db`,
   `review.csv` and `failures.csv` are gone; the mailbox and config file are
   not. The three answers are filled in with the ones the session was
   started with.
10. **Quit.** On the prompt, `q` quits with status `0`.

## Guided setup

Start from a folder with no store. Delete or rename the config file for the
first check.

1. **No config, no session.** Launch. Setup opens: "Setup: three questions",
   the config file's path, then three numbered questions, each with its
   explanation, a "You entered" summary and a "Save and check the model
   server (Ctrl-S)" button. The mailbox field is empty and focused, the
   names field is empty, Quick is selected. The footer shows `^s Save and
   continue`.
2. **Mailbox explanation.** Under "1. Your mailbox file": what file it is
   (the .mbox from Gmail Takeout, usually `Takeout/Mail/All mail Including
   Spam and Trash.mbox`), that dragging the file into the terminal pastes
   its path, and the folder a relative path is read from.
3. **Bad mailbox paths.** Type each of these and press Tab to leave the
   field; a red message appears under the field each time, and nothing is
   written:
   - a path that does not exist: "There is no file at …";
   - a folder: "… is a folder, not a file. Point at the mailbox (.mbox) file
     itself.";
   - the unzipped `Takeout` folder: "… is a Google Takeout folder, not a
     file. Point at the .mbox file inside it: …/All mail Including Spam and
     Trash.mbox.";
   - the Takeout `.zip`: "… is a zip archive, not a mailbox … unzip it, then
     point at the .mbox file inside";
   - a text file: "… does not look like an mbox file … this file begins with
     …".
   Typing in the field clears the message. Ctrl-S with a bad path shows the
   message, focuses the field and stays on setup.
4. **Drag and drop.** Drag the mbox file from Finder into the terminal: the
   pasted path (quoted, or with `\ ` before spaces) is accepted.
5. **Name variants.** Under "2. Your name variants": why names are asked,
   one per line or comma-separated, that "ELLIS/JORDAN MR" already matches
   "Jordan Ellis", the example "Jordan Ellis, Jordan A. Ellis", and in bold
   that leaving it empty turns passenger filtering off and every booking is
   kept. Type `Jordan Ellis` and `Jordan A. Ellis` on two lines: the summary
   lists both. Type `Mr` as a line and press Ctrl-S: a red message under the
   field says "'Mr' is not a name" and nothing is written.
6. **Empty names.** Leave the names empty: the summary reads "Name variants:
   none: passenger filtering is off, every booking is kept".
7. **Thoroughness.** Under "3. How thorough to be": Quick and Thorough, with
   one sentence each below: Quick is the fastest; Thorough checks for missed
   flight emails after extraction, roughly 2 to 35 minutes more, and can
   lower the threshold for the session within the `[audit]
   auto_include_budget` (shown as a number of emails). Select Thorough with
   the arrow keys and Space or Enter: the summary says "Thoroughness:
   Thorough".
8. **Summary.** The "You entered" panel always shows the mailbox, the names
   and thoroughness as entered, and that changing one later means starting
   fresh.
9. **Save.** With `test.mbox`, `Jordan Ellis` and Thorough, press Ctrl-S.
   The model-server step opens. The config file now exists with `mailbox =
   "test.mbox"` (relative, as the file is in the config file's folder),
   `[passenger] names = ["Jordan Ellis"]` and `[run] thoroughness =
   "thorough"`, under a three-line comment saying setup wrote it.
10. **Existing config kept.** Put back a config file with comments and other
    settings (e.g. a copy of `flighty_exporter.example.toml` with
    `store = "check.db"`), delete the store and launch. Setup is filled in
    from it and the Save button is focused: press Enter. Compare the file
    with the copy: only the `mailbox` line (added under the commented-out
    example), `names` and `thoroughness` changed.
11. **Server ready.** With Ollama running and both models pulled, the
    model-server step shows "On this machine: yes", "Reachable: yes", the
    version with "(new enough)", each model "ready", and "Ready. Press s to
    start the run." Press `s`: the progress screen opens with the
    thoroughness chosen in setup.
12. **Server not running.** Quit Ollama, launch and save. The step shows
    "Reachable: no" and a Problem panel with the commands' message ("… is
    not reachable … Start it with `ollama serve`, or open the Ollama app,
    then rerun the check (press c).") and "Press c to check again once it is
    fixed." The footer offers `c Check again`, not `s`. Start Ollama, press
    `c`: the panel goes away and the step reports the server.
13. **Server too old.** (Only if an older Ollama is at hand.) The Version
    line reads "(too old)" and the Problem panel gives the commands' "needs
    Ollama 0.35.0 or later" message with `c Check again`.
14. **Non-local address.** Set `[ollama] address =
    "http://192.0.2.10:11434"` in the config and launch. "On this machine:
    no", and the Problem panel shows the refusal ("email content would leave
    this machine") followed by the same guidance as the progress screen
    (`--allow-remote` does not apply in the application; change the config
    and start again). Only `b` and `q` are offered.
15. **Missing model.** Configure a small model you do not have (e.g.
    `[models] decision = "qwen3:0.6b"`) and launch. Its line reads "missing,
    size not known until the download starts (press d)"; for a missing
    default model it reads "about 9.3 GB" (`nimble`) or "about 8.0 GB"
    (`gemma4:12b-it-q4_K_M`). Press `d`: a dialog asks "Download …?" with the
    size; press `n` and check that nothing downloads (`ollama list`).
16. **Download with progress.** Press `d`, then `y`. A "Downloading …"
    panel shows a progress bar and "X of Y · pulling …", moving as the
    download runs, then status texts such as "verifying sha256 digest". When
    it finishes, a notice says it is downloaded and the step checks again;
    the model reads "ready".
17. **Download error.** Configure a model name that does not exist (e.g.
    `fictional-reader:3b`), press `d`, `y`. The Problem panel shows the
    server's error ("… could not pull fictional-reader:3b: …") and "Press d
    to try again"; `d` and `c` are offered.
18. **Quit during a download.** Start a large download and press `q`
    while it runs. The application exits at once with status `0`; the
    status line said beforehand that Ollama keeps what was downloaded.
    Launch again and download again: Ollama resumes rather than starting
    over.
19. **Back to the answers.** On the model-server step, `b` returns to setup
    with the saved answers filled in.
20. **Commands.** `uv run flighty-exporter setup --mailbox test.mbox --name
    "Jordan Ellis" --check` prints what it wrote and the server report;
    `uv run flighty-exporter pull-models` lists missing models without
    downloading.

## Progress screen

A launch with no store starts at setup: to reach the progress screen, press
Ctrl-S on the prefilled setup screen and then `s` once the model server is
ready. With an unfinished session, the startup prompt comes first: press `r`
to resume.

1. **Start.** Run `uv run flighty-exporter` in a terminal. The screen fills
   the terminal at once: the mailbox path and the thoroughness at the top, a
   status line saying the run is running, and a Stages panel listing
   classify, extract and review (plus any further stages the engine
   announces, e.g. audit and include in Thorough mode), each "waiting" until
   it starts. The footer shows `p Pause` and `q Quit`.
2. **Progress updates.** While classify runs, its row reads "running" and a
   Current stage panel shows a progress bar, "N of TOTAL messages", the
   seconds per message ("rate not known yet" before the first model answer)
   and the time remaining in words ("about 1 hour 20 minutes", "less than a
   minute"). The numbers move after every message. Nothing else is printed
   over the screen (no stray progress lines).
3. **Counts.** The Counts panel shows, for classify: classified, resolved from
   machine-readable data, skipped, failed; once extract starts, a second line:
   "extracted N of M" (M being the messages selected for extraction) and
   failed. They are the session's totals so far, not just this launch's: when
   classify finishes, classified plus resolved plus skipped plus failed adds
   up to the messages in the mailbox, and when extract finishes N equals M
   less the failed ones.
4. **Thorough stages.** Set `thoroughness = "thorough"` under `[run]` and
   start from scratch. The header says "Thoroughness: thorough" and the
   Stages panel lists five stages: classify, extract, audit, include,
   review. During audit the progress panel shows the sampled messages being
   sent; when audit finishes the Counts panel shows "audit: sampled S ·
   extracted E of S · failed F". With `test.mbox` the audit usually finds no
   flight emails below the threshold: include then reads "skipped", with the
   reason on the line under it ("no flight emails found below the
   threshold"), no progress bar appears for it, and the run goes on to
   review. To see include run, use a mailbox where the audit finds flight
   emails below the threshold (for example set `[classify] threshold` high,
   such as 0.9, in a scratch copy): include shows a progress bar over the
   newly included messages and finishes with "include: extracted N of N ·
   flight emails F · failed 0" (or, if including them would exceed
   `[audit] auto_include_budget`, include is skipped with that reason
   under it). The report's "check for missed flight
   emails" lines say what changed. Press `q` during audit: the application
   closes after the message in progress, and the next launch carries on
   with the audit's remaining messages.
5. **Locating messages.** With a store from an earlier run and a mailbox that
   has changed since (or a store made before message positions existed),
   extract's panel first shows "Locating messages" and "N of TOTAL mailbox
   entries scanned", then switches to one step per selected message.
6. **Pause and continue.** Press `p` while a model stage runs. The status
   line says "Pausing after the current message…", then "Paused. Press p to
   continue."; the running stage reads "stopped" and the progress panel
   disappears; the footer shows `p Continue`. Watch Ollama (e.g. Activity
   Monitor, or `ollama ps`): no further requests are made. Press `p` again:
   the run starts afresh, already-finished stages show "nothing to do" or
   finish quickly, and the stage carries on from where it stopped: messages
   already handled are skipped at once (the done count races past them) and
   only the rest go to the model. The Counts panel keeps its figures while
   paused and, once the stage has walked past the work done before the pause,
   shows the same totals as before and goes on from there (for example
   "classified 8" when paused, then 9, 10 … up to every message): no count
   drops back to what this launch alone did.
7. **Quit mid-run and resume on the next launch.** During extract, press `q`.
   The status line says "Stopping after the current message… (press q again
   to quit now)"; when that message is done the application closes and the
   shell prompt returns. `echo $?` prints `0`. Run `uv run flighty-exporter`
   again: classify shows "nothing to do", and extract continues with the
   remaining messages only.
8. **Quit at once.** During a slow model stage, press `q` and then `q` again
   (or Ctrl-C) before the message finishes. The application closes
   immediately with exit status `0`. Launch again: the run resumes; at most
   the one message in progress is redone, and the store is not damaged
   (`uv run flighty-exporter review --store check.db` still works).
9. **Problem panel and retry.** Quit Ollama (or `ollama stop` / stop the
   server) and launch. A red Problem panel shows the same message the `run`
   command prints for an unreachable server (e.g. "cannot reach Ollama at
   …"), the stage reads "stopped", and the footer offers `r Retry` and
   `q Quit`. Start Ollama again and press `r`: the panel disappears and the
   run continues. Repeat by stopping Ollama in the middle of extract: the
   panel appears after the message in progress fails, and `r` continues once
   Ollama is back, with nothing finished being sent again.
10. **Model missing.** Set `[models] extraction` to a model you have not
   pulled and launch: after classify, the Problem panel shows the existing
   "pull it with `ollama pull …`" message. Pull it (or fix the name and
   relaunch) and press `r`.
11. **Non-local address.** Set `[ollama] address = "http://192.0.2.10:11434"`
    and launch. The Problem panel shows the existing refusal message (email
    content would leave this machine), followed by a line saying that in the
    application `--allow-remote` does not apply and what to set in the config
    file instead. No stage starts, and the footer offers only `q Quit` (no
    retry: change the config and launch again). With
    `allow_remote = true` under `[ollama]`, the run is attempted and the
    warning appears in the Notices panel instead.
12. **Finish and report.** Let a run complete. Every stage reads "done" or
    "nothing to do", the progress panel is gone, and a Report panel shows the
    same end-of-run report `uv run flighty-exporter run` prints (flights
    found, needing attention, cancelled, replaced, dropped, failed messages,
    files written, next step), followed by "Press enter to review the
    flights, or q to quit.". The footer offers `⏎ Review flights` and
    `q Quit`; `q` exits with status `0` (`enter` is covered under "Review
    screen" below).
13. **No mailbox.** Remove `mailbox` from the config (or point it at a file
    that does not exist), delete the store, and launch. Setup opens instead
    of the progress screen (see "Guided setup" above).
14. **Non-terminal fallback.** Run `uv run flighty-exporter | cat` and
    `uv run flighty-exporter < /dev/null`. Neither opens the application;
    both print the usage line and "error: the following arguments are
    required: command" on stderr, and exit with status `2`.
15. **Narrow terminal.** Resize the terminal to about 50 columns by 24 rows
    during a run. Text wraps inside the panels, nothing is cut off sideways,
    and the screen scrolls (mouse wheel, or the keyboard) to reach the panels
    below; the footer stays visible.

## Review screen

Start from a finished run of `test.mbox` (see the progress screen's step
12). With `[passenger] names = ["Jordan Ellis"]` some bookings are dropped as other
passengers'; for an unreadable email, and for a flight with several source
emails, look at the end-of-run report's counts and the `flights` command's
output (`uv run flighty-exporter flights`), which lists the same flights in
the same order.

1. **Opens only after a complete run.** Launch and press `p` during
   classify: the footer offers no review key, and `enter` does nothing.
   Make Ollama unreachable and launch: with the Problem panel showing,
   `enter` does nothing. Let a run finish: the footer offers
   `⏎ Review flights`; press `enter`. The review screen replaces the
   progress screen.
2. **Table, order and totals.** The table has the columns date, flight,
   route, airline, booking, evidence, keep, corrected, reasons. Flights with
   a blocking reason (reasons in red, e.g. "unknown airport", "low
   confidence") come first, then flights with only advisory reasons (in
   yellow, e.g. "no flight number"), then the rest, each group by date. The
   first line shows "Flights: N · kept K · not kept M · needing attention
   A", the same numbers `flights` prints; the second shows "Showing: all
   flights (N of N flights)".
3. **Detail panel.** Move with the arrow keys: the panel below follows the
   cursor and shows every field, "keep: no (pre-set)" or "yes (pre-set)",
   each attention reason with "(blocking)" or "(advisory)", and "source
   emails: N" with each email's date and subject. A flight confirmed by a
   check-in shows two or more source emails.
4. **Toggle keep.** On a flight pre-set to not keep, press space: its keep
   cell reads "yes" at once, the totals' kept count goes up by one, the
   cursor stays on the same flight, and the detail says "yes (your choice;
   pre-set no)". Press `k`: back to "no (pre-set)". In another terminal,
   `uv run flighty-exporter flights` shows the same keep after each press
   (saved immediately).
5. **Source emails.** Press `enter` on a flight with two source emails: a
   view lists "[1/2]" and "[2/2]" with each email's date and subject,
   followed by its saved text (From, Subject, Date, the body, any PDF
   text); it scrolls with the arrow keys or the mouse. A flight extracted by
   an older version shows "source text unavailable (extracted before text
   was saved)" under its subject. `escape` returns to the table with the
   same flight selected. This works with the mbox file moved away.
6. **Correct a flight.** On a flight flagged "unknown airport", press `c`:
   a form shows the date, flight number, origin and destination prefilled.
   Type an impossible date (2025-02-30) and press `enter`: the form stays
   open with "date '2025-02-30' is not a real YYYY-MM-DD date; nothing was
   saved." Empty the origin and press `enter`: "origin is empty". Put back
   a real date and type a known airport in lower case for the wrong code,
   press `enter`: the form closes, the row shows the upper-cased code,
   "corrected", no reasons, keep "yes", and it has moved down out of the
   flagged group with the cursor still on it. The detail lists
   "corrections (found -> corrected):" with the old and new code. Correct
   a good flight's origin to an unknown code (e.g. XQY): it moves up into
   the flagged group with "unknown airport XQY" and keep "no". Press `c`
   and `escape`: nothing changes.
7. **Clear a decision.** On a flight you toggled but did not correct, press
   `u`: it returns to its pre-set keep without asking. On a corrected
   flight, press `u`: a box asks to clear the decision, naming the
   corrections; `n` (or `escape`) keeps it, `y` puts the found values back.
8. **Filters.** Press `f`: only flights with reasons are shown and the
   second line reads "Showing: flagged only (A of N flights)". Press `n`:
   "flagged only, not kept only". Press `y` repeatedly: it steps through
   each year present, oldest first, then back to all years; a combination
   that matches nothing says "No flights match these filters.". With the
   not-kept filter on, press space on a flight: it is kept and leaves the
   table, the cursor moving to the next flight. `f` and `n` again turn the
   filters off. The totals line never changes with the filters.
9. **Dropped bookings and unreadable emails.** Press `d`: a list headed
   "Bookings dropped as other passengers': N", one line per booking
   (reference, passengers, flights, first date); `escape` returns. Press
   `x`: "Emails that could not be read: N", one line per email (date,
   sender, subject, reason); `escape` returns.
10. **Export and back.** Press `e`: the export screen opens (see "Export
    screen" below). Press `b` there: the review comes back with your
    decisions, read afresh from the store, the filters off and the cursor
    on the first flight. This works the same after a relaunch that resumed
    a finished session (no run finished in this launch).
11. **Saved across sessions.** Make a keep change and a correction, press
    `q` (the application exits with status `0`), and launch again: once the
    run has finished (stages read "nothing to do"), press `enter`. The same
    flights show the same keep values and corrections. `uv run
    flighty-exporter review --store check.db --force` writes a table with
    them, and `export --from-store` exports them.
12. **No flights.** Use a mailbox with no flight emails (or one whose only
    flight email cannot be read): the review shows "No flights found.",
    where to look (the emails that could not be read, with their count and
    `x` to list them) and, for a Quick run, that a Thorough run exists,
    instead of an empty table. The footer offers only `d`, `x`, `e` and `q`.
13. **Many flights.** With a store of a few thousand flights, the screen
    opens within a second or so, and space on one flight updates its row
    without a visible pause.

## Export screen

Start from a session whose run has finished (for example after the progress
screen's checks), so the store holds flights. With `test.mbox` the export
usually has several flights for the import file; to see the manual-add list,
keep (in the review) a flight with no flight number, or one dated before 2006,
or one to an airport the OpenFlights data does not know. Compare every
screen with `uv run flighty-exporter export --from-store --dry-run`, which
prints the same plan and writes nothing.

1. **Open it.** Launch; once the run has finished press `enter` for the
   review, then `e` in the review. The status line says nothing is sent
   anywhere and to press `e`. A Plan panel says how many flights will be exported, how many must
   be added by hand and how many are not kept: the same three numbers as the
   dry run's `flights to export`, `flights to add by hand` and `not kept`.
   No file has been written yet (`ls openflights-backup-*.csv manual-add.csv`
   shows nothing new). The footer shows `e Export`, `space Tick` (only when
   there are manual-add flights), `b Back to review` and `q Quit`; not
   `o Show in Finder`.
2. **Checklist before exporting.** An "Add by hand in Flighty" panel lists
   each manual-add flight with Done (`[ ]` or `[x]`), date, route
   (`OSL → LIS`), flight number (`—` when there is none) and reason, the same
   flights and reasons as the dry run's "Manual-add list would hold", and
   "Ticked N of M" above it. With no manual-add flights it says "No flight
   needs adding by hand: every kept flight goes in the import file." instead.
3. **Export.** Press `e` (or Enter). The status line says "Writing the
   files…", then "Export written". An "Import file" panel shows the import
   file's full path, then "Press o to show it in Finder." (on macOS), then
   the manual-add list's path. Both files exist at those paths; the import
   file holds as many flights as the Plan said, and `manual-add.csv` holds
   the checklist's flights in the same order with `done` = `yes` for the
   ticked ones. An "Importing into Flighty" panel shows the steps: Settings,
   Import Flights, OpenFlights; sending the file to the address Flighty gives
   you; Flighty Pro for flights older than a year; manual-add flights entered
   by hand with the two airports and the date; and the note that Flighty
   documents support only for OpenFlights' own exports. `uv run
   flighty-exporter session` lists the import file and the manual-add list
   under "Files written".
4. **Reveal.** Press `o`. Finder opens with the import file selected. (On a
   system other than macOS, the panel instead says which folder the file is
   in, and `o` shows a note with the full path.)
5. **Tick and untick.** Move to a manual-add flight with the arrow keys and
   press space: its Done cell becomes `[x]` and "Ticked" goes up by one. Open
   `manual-add.csv` (or `cat` it): that flight's `done` column is `yes`
   already, without exporting again, and the import file's modification
   time has not changed. Press space again: `[ ]`, the count goes down, and
   `done` is empty again in the file. `uv run flighty-exporter export
   --from-store --dry-run` shows the same ticks.
6. **Ticks persist.** Tick one flight, press `q` (the application closes,
   exit status `0`), launch again, press `enter` once the run shows
   finished, then `e`: the same flight is still ticked, before and after
   exporting again.
7. **Back to the review and export again.** Press `b`: the review screen
   comes back (filters off, cursor on the first flight), with your
   decisions. Change a decision there (for example press space on one of
   the exported flights so it is not kept), then press `e`: the Plan shows
   the new numbers. Wait for the clock to pass the minute and press `e`: a second
   `openflights-backup-...csv` appears next to the first (an export in the
   same minute as the previous one replaces that file), and `session` lists
   both. `uv run flighty-exporter session --fresh-preview` would delete
   both.
8. **Reference data missing.** Point `[export] data_dir` at an empty folder
   and `data_url` at an address that does not answer (for example
   `http://127.0.0.1:9/data/`), launch and open the export. A red Problem
   panel shows the same message `export` prints ("OpenFlights data file
   airports.dat is not in … and could not be downloaded …") and that
   nothing was exported; the footer offers `r Retry`, `b Back to review` and
   `q Quit`, and `e` does nothing. Put the data back (or restore the config
   and relaunch) and press `r`: the Plan appears.
9. **Nothing kept.** Stop keeping every flight in the review, then open the
   export. The status line says no flight is kept and there is nothing to
   export; the Plan reads 0, 0 and the number not kept; the footer offers
   only `b Back to review` and `q Quit`, and `e` writes nothing.
10. **Nothing leaves the machine.** Throughout, nothing is sent to Flighty or
    to any other service: with the network off, every step above except a
    first download of the reference data works the same.
