# Manual checks

The application's screens are kept thin and are checked by hand, not by the
test suite (see the spec, issue #15, "Testing Decisions"). Before a release,
or after changing a screen, work through that screen's list in a real
terminal. Each step says what to do and what you should see.

Use invented mail for these checks: `test.mbox` (with `names = ["Jordan
Ellis"]`) and a local Ollama with the configured models pulled. Start from a
folder holding a config file like:

```toml
mailbox = "test.mbox"
names = ["Jordan Ellis"]
store = "check.db"
```

Delete `check.db`, `review.csv` and `failures.csv` to start a check from
scratch.

## Progress screen

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
    files written, next step), followed by a line saying review happens in
    the next screen once it exists. The footer offers only `q Quit`; `q`
    exits with status `0`.
13. **No mailbox.** Remove `mailbox` from the config (or point it at a file
    that does not exist) and launch. A screen says guided setup is not
    available yet, why (no mailbox set, or the file not found), the config
    file's path, and the line to add; `q` quits with status `0`.
14. **Non-terminal fallback.** Run `uv run flighty-exporter | cat` and
    `uv run flighty-exporter < /dev/null`. Neither opens the application;
    both print the usage line and "error: the following arguments are
    required: command" on stderr, and exit with status `2`.
15. **Narrow terminal.** Resize the terminal to about 50 columns by 24 rows
    during a run. Text wraps inside the panels, nothing is cut off sideways,
    and the screen scrolls (mouse wheel, or the keyboard) to reach the panels
    below; the footer stays visible.

## Export screen

Start from a session whose run has finished (for example after the progress
screen's checks), so the store holds flights. With `test.mbox` the export
usually has several flights for the import file; to see the manual-add list,
keep (in the review) a flight with no flight number, or one dated before 2006,
or one to an airport the OpenFlights data does not know. Compare every
screen with `uv run flighty-exporter export --from-store --dry-run`, which
prints the same plan and writes nothing.

1. **Open it.** From the review, move on to the export (once the review
   screen exists; until then there is no way to reach it in the running
   application). The status line says nothing is sent anywhere and to press
   `e`. A Plan panel says how many flights will be exported, how many must
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
   exit status `0`), launch again and come back to the export screen: the
   same flight is still ticked, before and after exporting again.
7. **Back to the review and export again.** Press `b`: the review screen
   comes back. Change a decision there (for example stop keeping one of the
   exported flights), then go to the export again: the Plan shows the new
   numbers. Wait for the clock to pass the minute and press `e`: a second
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
