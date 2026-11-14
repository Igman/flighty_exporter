# flighty-exporter

flighty-exporter reads a Google Takeout export of your Gmail (an mbox file),
finds the emails that describe flights you booked, and turns them into a file
you can import into Flighty, so the app knows about flights from before you
started using it. The emails are read by two language models running on your
own machine through Ollama; nothing is sent to a hosted model. You check the
result in a spreadsheet before anything is exported.

What it does not do:

- It is not affiliated with Flighty or endorsed by it.
- It does not talk to Flighty. It writes a file; you send that file to Flighty
  yourself.
- It does not contact any service other than your local Ollama server and,
  once, the two OpenFlights reference-data downloads (`airports.dat` and
  `airlines.dat`, see [Privacy](#privacy)). It does not access your live
  Gmail account.

## Contents

- [Privacy](#privacy)
- [Requirements](#requirements)
- [Getting your mail](#getting-your-mail)
- [Setup](#setup)
- [The workflow](#the-workflow)
- [Importing into Flighty](#importing-into-flighty)
- [Performance](#performance)
- [Configuration reference](#configuration-reference)
- [Known limits](#known-limits)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Licence and credits](#licence-and-credits)

## Privacy

**Email content goes only to a model server on your machine.** The commands
that use a model (`classify`, `extract` and `audit`) send email text to the
Ollama address in the config file, and refuse to start if that address is not
on this machine. Local means `localhost`, an address in 127.0.0.0/8, `::1`, or
a host name that resolves only to those. A name that does not resolve counts
as not local.

**The override.** If you really mean to use an Ollama server on another
machine, set `allow_remote = true` under `[ollama]` or pass `--allow-remote`.
The tool then prints a warning on every run:

```
warning: email content is being sent to the Ollama server at http://..., which is not on this machine (allow_remote or --allow-remote is set).
```

**Proxies are bypassed.** Requests to Ollama ignore `HTTP_PROXY` and similar
settings, because a proxy would carry email content off the machine even when
the address is local.

**The only other network access** is the download of OpenFlights'
`airports.dat` and `airlines.dat` from GitHub the first time `review` or
`export` needs them. They are cached (by default in
`~/.cache/flighty_exporter/openflights`) and reused; the download sends
nothing about you or your mail.

**What is stored on your disk.** These files hold personal data (your
messages' senders, subjects and dates, your names, your flights). Keep them
private and never commit them:

| File | Written by | Default name |
| --- | --- | --- |
| Your config (paths, name variants) | you | `flighty_exporter.toml` |
| The store: every pass's results, the text of the emails flights came from, and your review decisions (SQLite) | `classify`, `extract`, `audit`, `run`, `export`, `mark-added`, `decide`, the application | `flighty_exporter.db` (plus `-wal`/`-shm` side files) |
| The review table | `review`, `run` | `review.csv` |
| The failures file | `review`, `run` | `failures.csv` |
| The audit table | `audit` | `audit.csv` |
| The import file | `export` | `openflights-backup-YYYY-MM-DD-HHMM.csv` |
| The manual-add list | `export` | `manual-add.csv` |

The repository's `.gitignore` covers these default names, mbox files and
Takeout archives. It cannot know about names you choose yourself (with
`--output`, `--store`, `--review`, `--output-dir` or the config): keeping
custom-named files out of version control is up to you.

## Requirements

- macOS or Linux.
- [uv](https://docs.astral.sh/uv/). It installs the right Python (3.12 or
  later) and the one dependency (`pypdf`) for you.
- [Ollama](https://ollama.com/download) 0.35.0 or later (the release that
  added the decision endpoint the classify pass uses), running locally.
- The two models, pulled once:

  ```
  ollama pull nimble                    # decision model, about 9.3 GB
  ollama pull gemma4:12b-it-q4_K_M      # extraction model, about 8 GB
  ```

- Memory: tested on a machine with 24 GB of RAM. Only one model is loaded at a
  time, because classification and extraction are separate commands.
- Flighty Pro, if you want to import flights older than a year (Flighty's
  rule, not this tool's).

## Getting your mail

The tool reads a Gmail mailbox exported with Google Takeout:

1. Open Google Takeout (takeout.google.com) while signed in to the Google
   account whose mail you want.
2. Deselect everything, then select only Mail. Takeout exports Gmail in the
   mbox format. You can leave all mail included: the tool skips messages
   under the Spam, Trash, Drafts and Chat labels itself.
3. Create the export and wait for Google's email saying it is ready (this can
   take hours or days for a large mailbox), then download the archive.
4. Unpack it. The mailbox is a single `.mbox` file inside the `Takeout/Mail/`
   folder (in an English-language account it is usually called
   `All mail Including Spam and Trash.mbox`; the name depends on your
   account's language). It can be several gigabytes.

Google's menus change from time to time; if the wording differs, look for
the Mail (Gmail) product in Takeout's product list and its format options.

## Setup

```
git clone https://github.com/Igman/flighty_exporter.git
cd flighty_exporter
uv sync
cp flighty_exporter.example.toml flighty_exporter.toml
```

Then edit `flighty_exporter.toml`. Two settings matter for everyone: near
the top, uncomment `mailbox` and set it to your mbox file; and under the
existing `[passenger]` section, replace `names = []` with your name variants
(edit the lines in place; do not add a second `[passenger]` section, which is
a TOML error). The result looks like this:

```toml
mailbox = "/path/to/Takeout/Mail/All mail Including Spam and Trash.mbox"

[passenger]
names = ["Jordan Ellis", "Jordan A. Ellis"]
```

Relative paths in the config file are resolved against the file's own
directory. Every command looks for `flighty_exporter.toml` in the current
directory (or takes `--config FILE`), so run them from the clone. The tool
then writes its files next to the config, where `.gitignore` covers them.

**Name variants.** List your name as it appears on bookings, with and
without a middle name, a maiden name, and so on. Matching ignores case,
accents, word order, punctuation and titles (Mr, Mrs, Dr, Herr, Mme, ...), so
`ELLIS/JORDAN MR`, `Ellis, Jordan` and `Jördan Éllis` all match
`Jordan Ellis`. A passenger matches when every word of one of your variants
is in the passenger's name (extra middle names on the ticket are fine), or
when the passenger's name has at least two words and all of them are in your
variant (a ticket that leaves out your middle name). There is no fuzzy or
initial matching. A booking whose passengers include none of your names is
left out of the review table; `review` lists what it dropped. A booking that
names no passenger at all is kept and flagged. With no names configured,
passenger filtering is off and other people's bookings are kept.

One limitation: names are split into words at anything that is not a letter,
so a hyphenated surname written run together on a ticket does not match. If
you are configured as `Ana Garcia-Lopez` and an airline writes
`GARCIALOPEZ/ANA`, add the variant `Ana Garcialopez`.

All other settings have defaults; see the
[configuration reference](#configuration-reference).

## The workflow

Run the commands in this order, from the clone:

```
uv run flighty-exporter classify
uv run flighty-exporter extract
uv run flighty-exporter audit        # optional, but recommended
uv run flighty-exporter review
# edit review.csv in a spreadsheet
uv run flighty-exporter export
```

![Data flow between the five commands: the mbox file feeds classify, extract and audit, which read and write the store; review reads the store and writes review.csv; export reads review.csv and writes the import file for Flighty.](docs/pipeline.svg)

The commands never call each other. Everything passes through two hand-offs:
the store (one SQLite file), which carries results from `classify` to
`extract`, `audit` and `review`; and the review table (`review.csv`), which
carries your decisions from `review` to `export`. Dashed arrows show that
`extract` and `audit` go back to the mbox for message bodies.

`uv run` installs everything on first use, so after installing uv and Ollama
this is the only command you need. Every model command can be stopped with
Ctrl-C and resumed by running it again: each result is saved as soon as it is
known, so an interruption loses at most the message in progress. The model
commands check, just before their first model request, that Ollama is running,
is version 0.35.0 or later and has the model they need, and stop with an
instruction if not.

Progress, with the time per message and an estimate of the time remaining, is
shown on stderr while the model commands run.

### The application

```
uv run flighty-exporter
```

With no command, in a terminal, the tool opens a full-screen application that
runs the same stages as `run` (classify, extract and review; audit and
include too when `[run] thoroughness = "thorough"`) and shows them live: every
stage with its state, a progress bar for the running stage with the messages
done, the time per message and the time remaining, running counts, and the
end-of-run report when it finishes. It reads the same config file (`--config`
works as for the commands) and needs `mailbox` set in it; guided setup is not
available yet. Keys: `p` pauses after the message in progress and continues;
`q` quits after the message in progress (`q` or Ctrl-C again quits at once),
and the next launch carries on where it stopped; `r` retries after a
model-server problem, once you have fixed it. The same rule as the commands
keeps email on this machine: a non-local Ollama address is refused unless the
config file allows it.

Outside a terminal (piped, or run from a script), the bare command prints the
usage line and exits with status 2, as before.

**Reviewing in the application.** When the run has finished, press `enter`
to review the flights (only a complete run is reviewed: not after a pause or
a problem). The review screen shows a table of flights (date, flight number,
route, airline, booking reference, evidence, keep, whether you corrected it,
and its attention reasons), flights needing attention first; the totals
(flights, kept, not kept, needing attention) and the active filters above
it; and below it the selected flight in full: its keep and whether that is
the pre-set value or your choice, each attention reason marked blocking or
advisory, any correction next to the value the tool found, and its source
emails. Keys: space (or `k`) keeps or drops the selected flight; `enter`
shows the saved text of its source emails; `c` corrects its date, flight
number, origin or destination (checked like any other flight; `enter`
saves, `escape` cancels); `u` clears your decision on it; `f` shows only
flagged flights, `n` only flights not kept, `y` steps through the years;
`d` lists the bookings dropped as other passengers', `x` the emails that
could not be read; `e` moves on to export; `q` quits. Every decision is
saved in the store the moment you make it, so it is there when you come
back, and `review` and `export` use it too. The `flights` and `decide`
commands (see [4. review](#4-review)) show and make the same decisions from
the command line.

### All in one go: run

```
uv run flighty-exporter run
```

`run` performs classify, extract and review in that order, printing each
stage's start, its usual progress and summary, and then an end-of-run report:
flights found, how many need attention and why, bookings cancelled, replaced
and dropped as other passengers', saved decisions discarded because their
flight is no longer produced (when there are any), messages that failed in
each stage, PDFs not read, where each file was written, and the next step
(check the review table, then run `export`). Its review step is `review`'s:
the table reflects your saved decisions. It takes `--mailbox`, `--store`, `--allow-remote`,
review's `--output`, `--failures` and `--force`, and `--thoroughness`
(`quick`, the default, runs no audit; `thorough` is described below).

Like the single commands, `run` resumes where it stopped: rerun it after Ctrl-C
(exit status 130) and no finished message is sent to a model again. A
`SIGTERM` (`kill`) asks it to stop after the message in progress (exit status
143). A model-server problem stops it with the same message the single
commands print and exit status 1; it never asks a question. If the review
table exists and you have edited it, `run` leaves it as it is, says so, and
still reports the flights; pass `--force` to regenerate it.

**Thorough mode** (`run --thoroughness thorough`, or `thoroughness =
"thorough"` under `[run]`) adds two stages between extract and review. First
it runs the [audit](#3-audit-recommended) with your `[audit]` sample size and
seed, printing its report and writing `audit.csv`. If a sampled message below
the threshold contained flights, it takes the lower edge of the lowest such
band as the suggested threshold and counts the messages that lowering to it
would add and that have no extraction yet. If that count is at most
`[audit] auto_include_budget` (default 2000), the run lowers the threshold for
this session and extracts those messages (the ones the audit already
extracted are not sent again), so their flights appear in the review table.
Over the budget nothing changes, and the report says what was found, how many
messages including it would add, and how to include them. The end-of-run
report has a "check for missed flight emails" section saying which of these
happened (a Quick run says the check was skipped). This happens once per run;
a later Thorough run audits again against the lowered threshold, which costs
little because the sample is the same and finished messages are reused.

The lowered threshold is the session's **effective threshold**: it is stored
in the store, never written to your config file, and used by every later
`extract`, `audit`, `review`, `export` and `sources` on that store. Whichever
is lower wins: setting `[classify] threshold` below the effective threshold
takes effect, but raising it above the effective threshold does not raise the
session's threshold again. A Thorough run stopped while it extracts the newly
included messages keeps the lowered threshold; the next `run` or `extract`
extracts the rest.

#### Sessions: describing one and starting fresh

A session is the store plus the files the tool wrote from it. The store
records the mailbox and settings the session was started with (by the first
command that brought a mailbox to it; later runs with other settings do not
change them), when it was last active, and every file the tool writes for it:
the review table, the failures file, the audit report, each import file and
the manual-add list.

```
uv run flighty-exporter session
uv run flighty-exporter session --fresh-preview
```

`session` prints the mailbox (and whether it still exists), the settings, when
the session started and was last active, how far each stage got (messages
recorded, classified, resolved from machine-readable data, at or above the
threshold in use, extracted, flights and flights with a saved decision), whether it
looks complete, the files it wrote that still exist, and what starting fresh
would lose. The threshold in use is the effective threshold; when a Thorough
run lowered it, `session` says so and shows the configured value too (for
example `0.02 (lowered for this session from the configured 0.05)`). It reads only the store, and with no store prints
`No session yet.` `--fresh-preview` lists what starting fresh would delete,
without deleting anything.

`run --fresh` starts fresh without asking: it deletes the files recorded as
written for the session that still exist, and only those, then the store and
its SQLite side files (`-wal`, `-shm`, `-journal`), printing a line for each,
and then runs from scratch. It never deletes the mailbox, the config file,
downloaded models, the cached OpenFlights data, or anything the tool did not
record writing, such as your own files in the same folder; a recorded path
that is no longer a regular file is left alone. A store created before
sessions were recorded knows none of the files it wrote, so starting fresh
deletes just the store and says so. Without `--fresh`, `run` resumes. The effective threshold lives in the store,
so starting fresh clears it too: `run --fresh --thoroughness thorough` starts
again from the configured threshold and audits anew.

### 1. classify

```
uv run flighty-exporter classify
```

Reads every message in the mailbox (or the file given with `--mailbox PATH`):

- Messages under the Gmail labels Spam, Trash, Drafts and Chat are skipped.
- A message that carries machine-readable flight data (schema.org
  FlightReservation markup, which many airlines embed for Gmail, or a flight
  calendar `.ics` attachment) is read directly, with no model, and its flights
  go straight to the review table with confidence 1.0. Incomplete markup is
  ignored and the message is classified like any other.
- Every other message is sent to the decision model (`nimble`) with its
  sender, subject, attachment filenames and the start of its body (PDF text is
  not sent at this stage). The model answers two questions: a yes/no gate,
  "does this email contain flight details for a specific booking?", as a
  probability; and the email type, one of `booking confirmation`, `change`,
  `cancellation`, `check-in or boarding pass`, `post-flight`, `marketing` or
  `other`.

Both answers are stored for every message. The gate threshold
(`[classify] threshold`, default 0.05) is not applied here, only when
`extract` selects messages, so changing it never requires classifying again.

At the end it prints a summary: messages in the mailbox, classified, already
classified (from an earlier run), resolved from machine-readable data,
skipped, failed, and then, for everything stored for the decision model, how
many messages are at or above the threshold, how many have a gate of at least
0.5, 0.2 and 0.05, and the count per email type.

Time: about 0.7 seconds per message on the tested machine, so roughly 10 hours
per 50,000 messages. Run it over several sessions if you like.

### 2. extract

```
uv run flighty-exporter extract
```

Sends each message whose gate is at or above the threshold, and that has no
extraction yet, to the extraction model (`gemma4:12b-it-q4_K_M`). The model
reads the whole cleaned body plus the text layer of any PDF attachment (up to
`[extract] max_input_chars` characters in total) and returns the email kind
(`booking`, `change`, `cancellation`, `check-in` or `other`), the booking
reference, the passenger names, and each flight segment: local departure
date, airline, flight number, origin and destination airport codes, and a
confidence value. Every answer is checked against the expected structure; an
invalid or cut-off answer is retried (`[extract] retries`, default 2) and then
recorded as a failure. Failed messages are tried again the next time you run
`extract`.

`extract` (and `audit`) read only the messages they send: `classify` records
where each message sits in the mbox file, and they go straight there instead
of reading the whole mailbox again. If the mbox file has changed since
`classify` (a new export), or the store was made by an earlier version, they
first scan the mailbox's headers once to find the messages again (shown as
`extract: locating messages`) and remember what they found.

The summary lists how many messages were selected (`selected (gate >= 0.05)`),
extracted, already extracted, retried, failed, not found in the mailbox, cut at
the input limit (`inputs truncated`) and PDFs that could not be read, and then
the segments and email kinds stored for the extraction model.

Time: about 3 seconds per message, but only for the small fraction of the
mailbox that passed the gate.

### 3. audit (recommended)

```
uv run flighty-exporter audit
```

The decision model's probabilities are not calibrated, so the audit checks
whether real flight emails are hiding below the threshold. It divides the
classified messages into gate-probability bands (`[0, 0.001)`,
`[0.001, 0.005)`, `[0.005, 0.01)`, `[0.01, 0.02)`, `[0.02, 0.05)`,
`[0.05, 0.1)`, `[0.1, 0.2)`, `[0.2, 0.5)`, `[0.5, 1]`), samples up to
`[audit] sample_size` messages (default 25) from each band, including the bands
below the threshold, sends the sampled ones that have no extraction yet to the
extraction model, and reports per band how many contained flight segments.

Reading the report:

- The table under `All classified messages` has one row per band, highest
  first: `messages` in the band, `sampled`, `failed` extractions,
  `with segments` (sampled messages that contained a flight), the `hit rate`,
  and `est. flight emails` (hit rate times band size). A line marks where the
  threshold falls: bands above it are extracted, bands below are not.
- The table under `Messages with a PDF attachment` repeats this for messages
  with a PDF attachment only. PDF-only e-tickets are the known blind spot,
  because the decision model never sees PDF text.
- `Sampled messages below the threshold that contained flight segments` lists
  each such message with its gate, date, sender, subject and the segments
  found.
- The reading line sums it up, for example
  `Reading: the lowest band with a hit is [0.01, 0.02) (gate 0.0132); [classify] threshold = 0.01 would include it (now 0.05).`
  or `Reading: no sampled message below the threshold 0.05 contained flight segments; ...`.

The audit never changes the config. If the reading suggests it, lower
`threshold` under `[classify]` in your config file yourself and run `extract`
again: only the newly included messages are sent, and messages the audit
already extracted are not sent again. The same seed (`[audit] seed`,
`--seed`) gives the same sample, so rerunning the audit sends nothing new.
The report is also written to `audit.csv`. (A Thorough `run` acts on the
reading for you, within a budget, by lowering the threshold for the session
only; see [All in one go: run](#all-in-one-go-run).)

Time: up to roughly 450 extraction requests at the default sample size (25
messages for each of 9 bands, twice: once for all messages and once for
messages with a PDF), about 3 seconds each. Use `--sample-size` for a quicker
look.

### 4. review

```
uv run flighty-exporter review
```

Takes no model and runs in seconds. It consolidates the extraction records of
every message at or above the threshold, and of every message resolved from
machine-readable data, into one row per flight, and writes the review table
(`review.csv`) and the failures file (`failures.csv`).

How flights are put together:

- **Bookings.** Emails sharing a booking reference are one booking. An email
  with no booking reference joins a booking that already has the same flight
  (date and flight number); otherwise it forms its own booking.
- **Cancelled.** A cancellation email removes every flight the booking has so
  far. A later booking or change email under the same reference brings flights
  back (a rebooking).
- **Replaced, per leg.** A flight in a later booking or change email replaces
  earlier flights of the booking with the same origin and destination. Legs the
  later email does not mention are kept.
- **Filtered by passenger.** A booking whose passengers include none of your
  name variants is dropped, with all its flights; the summary lists each
  dropped booking.
- **Duplicates.** The same flight from several bookings or emails (same date
  and flight number, or same date and route when there is no flight number)
  appears once.
- **Evidence.** A flight is `confirmed` when a check-in, boarding-pass or
  post-flight email exists for it, and `booked only` otherwise. Evidence is
  for your judgement; it never removes a row.

The review table's columns:

| Column | Meaning |
| --- | --- |
| `keep` | `yes` or `no`, pre-filled by the tool. Only `yes` rows are exported. |
| `needs_attention` | `yes` when the row has at least one attention reason. |
| `attention_reason` | Every reason, joined with `; `, blocking reasons first. |
| `date` | Local departure date at the origin, `YYYY-MM-DD`. |
| `flight_number` | With the airline's two-character prefix, e.g. `BA286`. |
| `airline` | As the email names it. |
| `origin`, `destination` | Airport codes. |
| `booking_reference` | Joined with `; ` when merged from several bookings. |
| `passengers` | Every passenger the booking names, joined with `; `. |
| `confidence` | 0 to 1, after the penalties described below. |
| `evidence` | `confirmed` or `booked only`. |
| `email_kind` | The kind of the most recent email about this flight. |
| `source` | `markup`, `model`, or `markup; model`. |
| `source_count` | How many emails mention the flight. |
| `message_id`, `message_date`, `message_subject` | The most recent of those emails, so you can find it in Gmail. |
| `source_messages` | Every one of those emails, as `YYYY-MM-DD subject`, joined with ` \| `. |
| `flight_id` | The flight's identity as the tool first found it: date and flight number (`2025-05-12 ZZ1234`), or date and route when there is no flight number (`2025-08-01 OSL-LIS`). It does not change when you correct the row, so export can match your edits to the flight even if you re-sort the rows. Leave it as it is. |

**Your decisions are saved in the store.** When `export` reads an edited
table, it records your keep choices and corrections in the store (see
[export](#6-export)). The next `review` writes the table from the flights with
those decisions applied: corrected values, your keep choice, and attention
reasons recomputed from the corrected values (a correction to an unknown
airport is flagged like any other). Your keep choice wins over the pre-set
one. A decision follows the latest result: if a later run no longer produces
a flight (say a cancellation email was found), its decision is discarded, and
the summary line `decisions discarded because their flight is no longer
produced` counts them.

There are two kinds of attention reason.

**Blocking** reasons pre-set `keep` to `no`. Turn the row back to `yes` if
you check it and it is right (correcting the field if needed):

- `unknown airport XXX`: the code is not in OpenFlights' airport data.
- `unknown airline XX`: no airline in OpenFlights' data has this prefix.
- `malformed flight number`: not a two-character airline code followed by 1 to
  4 digits and an optional letter.
- `origin and destination are the same`
- `implausible date`: before 1990, or more than 400 days after the latest
  email about the flight.
- `low confidence`: the confidence, halved once for each reason above, is
  below `[review] min_confidence` (default 0.7).

**Advisory** reasons leave `keep` at `yes` but ask you to look:

- `no flight number`: export puts the flight on the manual-add list.
- `email was truncated before extraction`: the email was longer than the
  extraction input limit, so part of it was not read.
- `no passenger named`: the booking names no passenger, so the tool cannot
  tell whether it is yours.

Airport and airline codes are checked against OpenFlights' reference data,
which `review` downloads on first use. If it cannot be loaded, the table is
still written without the code checks and a warning says why.

The summary lists the flights, how many are confirmed and booked only, how
many need attention and are pre-set to not keep, the count per attention
reason, bookings cancelled, bookings with replaced segments, the bookings
dropped as being for other passengers (reference, passengers, number of
flights and first date), and the failures.

**The failures file** lists, with message ID, date, sender, subject and
reason, every message at or above the threshold that produced no valid
extraction (the model's answer was invalid after the retries, or the message
was not extracted yet), and every PDF attachment that could not be read
(image-only, encrypted, too large or unreadable). These are the emails to look
up in Gmail and, if they are flights, add by hand.

**Overwrite protection.** `review` remembers the table it wrote. Run again,
it overwrites the table only if you have not changed it since; if you have
edited it, it stops with an error rather than lose your edits. Use
`--force` to overwrite it anyway, or `--output FILE` to write elsewhere.

**Reading a flight's emails.** `extract` (and `classify`, for messages with
machine-readable flight data) saves in the store the text of every email that
yielded a flight record: sender, subject, date, the cleaned body and the text
of readable PDF attachments. Only those emails, never the whole mailbox. To
check a flight against its emails, without the mbox file:

```
uv run flighty-exporter sources                                 # every flight review lists
uv run flighty-exporter sources --flight 2025-05-12 ZZ1234      # one flight
uv run flighty-exporter sources --flight 2025-05-12 ZZ1234 --no-text   # just its emails' dates and subjects
```

It prints each flight (date, flight number, route), then each source email's
date and subject followed by its saved text. The flights are the ones `review`
would write, with your saved decisions applied, so a corrected flight is shown
(and picked with `--flight`) under its corrected values; `sources` only reads
the store and never discards a decision. For a flight with no flight
number, give its route instead (`--flight 2025-05-12 LIS-OSL`). Flights
extracted by an earlier version of the tool show
`source text unavailable (extracted before text was saved)`; extracting them
again (for example with another extraction model) saves the text.

**The flight list and single decisions from the command line.** `flights`
prints the flights as the application's review screen shows them: the
totals, the years present, a table with flights needing attention first
(blocking reasons before advisory ones, then by date and flight number),
then the bookings dropped as other passengers' and the emails that could not
be read. `decide` makes one decision, saves it in the store at once and
prints the flight's new state. Pick the flight by the date and flight number
it now shows (or `--route ORIGIN DEST` for a flight without a number), as
for `mark-added`:

```
uv run flighty-exporter flights                       # every flight
uv run flighty-exporter flights --flagged --year 2025 # filters combine; also --not-kept
uv run flighty-exporter decide 2025-06-01 QQ410 --drop   # or --keep
uv run flighty-exporter decide 2025-07-01 ZZ300 --set destination=OSL --set date=2025-07-02
uv run flighty-exporter decide 2025-07-02 ZZ300 --reset  # clear the keep choice and corrections
```

A keep choice equal to the pre-set value saves nothing (and removes an
earlier choice). `--set` takes `date`, `flight_number`, `origin` or
`destination`; codes are upper-cased and flight numbers lose spaces and
hyphens, a value equal to what the tool found removes that correction, and
an unreal date or an empty airport is refused without saving anything. A
following `review --force` or `export --from-store` uses what `decide`
saved.

### 5. Edit the review table

Open `review.csv` in a spreadsheet (Numbers, Excel, LibreOffice). For each row:

- Leave `keep` as it is if you agree, or change it. `yes`, `y`, `true`, `1`
  and `x` count as keep; `no`, `n`, `false`, `0` and an empty cell count as do
  not keep (case is ignored). Any other value is reported and the row skipped.
- Correct `date`, `flight_number`, `origin` or `destination` in place if they
  are wrong. The export uses your corrections. Dates must stay `YYYY-MM-DD`;
  spreadsheets like to reformat them, so check the column before saving.

Only `keep`, `date`, `flight_number`, `origin` and `destination` are used by
the export (and `message_date` and `message_subject`, if present, for the
manual-add list). The usual re-save changes are tolerated: reordered or extra
columns, column headers with different case or stray spaces, other quoting
and line endings, a comma, semicolon or tab as the field separator, and blank
rows at the end. Save as CSV in UTF-8
("CSV UTF-8" in Excel).

### 6. export

```
uv run flighty-exporter export
```

Reads the edited review table (or the store, see below) and writes, in the config file's directory
(or `--output-dir DIR`):

- **The import file**, `openflights-backup-YYYY-MM-DD-HHMM.csv` (the time is
  UTC): every kept row Flighty can match, in OpenFlights' backup format.
- **The manual-add list**, `manual-add.csv`: kept rows that cannot go in the
  import file, with `date`, `origin`, `destination`, `flight_number`,
  `reason`, `message_date`, `message_subject` and `done` (see below). The reason is one or more
  of:
  - `no flight number`
  - `dated before 2006` (Flighty has no records before 2006)
  - `airport XXX not in OpenFlights data`
  - `no OpenFlights airline for prefix XX`

It prints a summary: where each file was written, `flights exported`,
`flights to add by hand`, and for the review table the number of `rows`, how
many were `not kept`, and how many were `rejected as invalid`, with the row
number (the header is row 1) and reason for each: a `keep` value that is not
yes or no, a date that is not a real `YYYY-MM-DD` date, or an empty origin or
destination. Rejected rows are left out of both files; fix them and run
`export` again. It takes a second.

Where `export` reads the flights from, in this order:

1. **A review table**, given with `--review FILE` or present at the configured
   path (`review.csv`): read as described above. If the store exists (or
   `--store FILE`), each row's `keep`, `date`, `flight_number`, `origin` and
   `destination` are first recorded in it as decisions, matched by
   `flight_id`, but only where they differ from what the tool produced; the
   summary counts them (`decisions recorded from the review table`). Rows
   without a `flight_id` (a table from an older version, or one you made by
   hand) are exported but record nothing, and with no store the table is
   simply exported.
2. **The store**, when there is no review table: the flights with your saved
   decisions applied, the same rows `review` would write. This gives the same
   import file as exporting the table `review` would write.
3. **`--from-store`** forces the store even when a review table exists.

The summary's `source of the export` line says which was used.

The manual-add list has a last column, `done`: `yes` for a flight you have
marked as entered in Flighty by hand, so you can work through the list over
several sittings. Mark one with its date and flight number as the list shows
them, or `--route ORIGIN DEST` for a flight without a number; `--undo` removes
the mark. Run `export` again to see it in the list.

```
uv run flighty-exporter mark-added 2004-08-01 ZZ9
uv run flighty-exporter mark-added 2025-08-01 --route OSL LIS
uv run flighty-exporter mark-added 2004-08-01 ZZ9 --undo
```

## Importing into Flighty

What was verified (November 2026, with a file generated by this tool's export
format):

- In the Flighty app, go to Settings, Import Flights, and choose OpenFlights.
  Flighty's import works by emailing the file to an address the app gives you;
  attach the `openflights-backup-...csv` file to that email.
- Flighty accepted the generated file and imported its flights, including one
  older than a year (that needs Flighty Pro). The direction of each flight was
  preserved.
- A flight Flighty already had was not imported a second time.

Be aware: Flighty documents support only for files exported directly by
OpenFlights. This tool writes a file in OpenFlights' backup format without
going through OpenFlights. It was accepted when tested, but it may stop
working if Flighty changes its checks.

Flights on the manual-add list are entered by hand in Flighty, which needs
only the two airports and the date.

## Performance

Measured on an Apple M5 Pro with 24 GB of RAM:

- **classify**: about 0.7 seconds per message, so about 10 hours per 50,000
  messages. It is resumable, so it can run over several sessions.
- **extract**: about 3 seconds per message, but only for the small fraction of
  messages that pass the gate.
- **audit**: up to roughly 450 extraction requests at the default sample size.
- **review** and **export**: seconds.

Time per message depends on message length. On the same machine, the
synthetic test mailbox (200 messages, see [Development](#development)) took
4 minutes 19 seconds to classify (1.33 s per message), 1 minute 38 seconds to
extract its 16 selected messages (6.1 s per message) and 6 minutes for the
audit (77 extractions at 4.6 s each).

## Configuration reference

The config file is TOML. Every setting is optional; the defaults below apply
when a setting (or the whole file) is missing. Relative paths are resolved
against the config file's directory (against the current directory when there
is no config file). An unknown setting is an error.

**Top level**

| Setting | Default | Meaning |
| --- | --- | --- |
| `mailbox` | none | The Takeout mbox. Required unless given with `--mailbox` (classify, extract, audit). |
| `store` | `"flighty_exporter.db"` | SQLite store with every pass's results. Overridden by `--store`. |

**`[ollama]`**

| Setting | Default | Meaning |
| --- | --- | --- |
| `address` | `"http://localhost:11434"` | Ollama server. Must be on this machine unless `allow_remote` is set. |
| `timeout_seconds` | `300` | Per-request timeout for model requests (the preflight check uses at most 5 seconds). |
| `allow_remote` | `false` | Allow a non-local address (same as `--allow-remote`). Warns on every run. |

**`[models]`**

| Setting | Default | Meaning |
| --- | --- | --- |
| `decision` | `"nimble"` | Decision model for classify. Results are stored per model; changing it means classifying again. |
| `extraction` | `"gemma4:12b-it-q4_K_M"` | Extraction model for extract and audit. Results are stored per model. |

**`[classify]`**

| Setting | Default | Meaning |
| --- | --- | --- |
| `threshold` | `0.05` | Gate probability (0 to 1) at or above which a message goes to extract. Not applied by classify. A Thorough run may lower it for its session (the effective threshold); the lower of the two applies. |
| `max_body_chars` | `12000` | How much of the cleaned body the decision model sees. A message the server still rejects as too long is retried with the body halved, up to 3 times. |

**`[extract]`**

| Setting | Default | Meaning |
| --- | --- | --- |
| `num_ctx` | `16384` | Context length for each extraction request. |
| `num_predict` | `1500` | Output cap. An answer that stops at the cap is treated as invalid. |
| `retries` | `2` | Retries of an invalid or cut-off answer before the message is recorded as a failure. |
| `max_input_chars` | `36000` | Cap on what the extraction model reads: the body, then PDF text. Cut inputs are flagged in the review table. |

**`[review]`**

| Setting | Default | Meaning |
| --- | --- | --- |
| `output` | `"review.csv"` | Review table written by review and read by export. Overridden by `review --output` and `export --review`. |
| `failures` | `"failures.csv"` | Failures file. Overridden by `review --failures`. |
| `min_confidence` | `0.7` | Confidence (0 to 1) below which a flight is flagged `low confidence`. |

**`[passenger]`**

| Setting | Default | Meaning |
| --- | --- | --- |
| `names` | `[]` | Your name variants. Empty turns passenger filtering off. |

**`[audit]`**

| Setting | Default | Meaning |
| --- | --- | --- |
| `sample_size` | `25` | Messages sampled per band. Overridden by `--sample-size`. |
| `seed` | `1` | Seed for the reproducible sample. Overridden by `--seed`. |
| `output` | `"audit.csv"` | Audit table. Overridden by `audit --output`. |
| `auto_include_budget` | `2000` | Thorough runs only: the most not-yet-extracted messages a lowered threshold may add for the run to lower the session's threshold and extract them by itself. `0` lowers it only when the audit already extracted every message it would add. |

**`[export]`**

| Setting | Default | Meaning |
| --- | --- | --- |
| `output_dir` | the config file's directory | Where the import file and manual-add list go. Overridden by `--output-dir`. |
| `data_dir` | `"~/.cache/flighty_exporter/openflights"` | Cache for OpenFlights' `airports.dat` and `airlines.dat`. |
| `data_url` | `"https://raw.githubusercontent.com/jpatokal/openflights/master/data/"` | Where those files are downloaded from on first use. |

**`[run]`**

| Setting | Default | Meaning |
| --- | --- | --- |
| `thoroughness` | `"quick"` | How hard `run` looks for flight emails the first filter missed: `"quick"` runs no audit; `"thorough"` audits after extract and, within `[audit] auto_include_budget`, lowers the session's threshold (see [All in one go: run](#all-in-one-go-run)). Overridden by `run --thoroughness`. |

Every command also accepts `--config FILE` before the command name, e.g.
`uv run flighty-exporter --config ~/flights/flighty_exporter.toml review`.

## Known limits

- **One Gmail mailbox at a time**, from a Google Takeout mbox. No IMAP, no
  other providers, no merging of several mailboxes.
- **Text-layer PDFs only.** There is no OCR: image-only PDFs, boarding-pass
  images and screenshots are not read (unread PDFs are listed in the failures
  file).
- **No flights before 2006 in the import file.** Flighty has no records before
  2006, so those flights go to the manual-add list. Flighty also says its
  records for 2006 to 2011 are often incomplete.
- **Flighty Pro** is needed to import flights older than a year.
- **Confidence is a weak signal.** The extraction model reports confidence 1.0
  almost always, so a row without attention reasons is not proof it is right.
  The review step matters.
- **PDF-only e-tickets** with generic email wording ("Your e-ticket is
  attached") score low at classification, because the decision model never
  sees PDF text. Run `audit` and look at its PDF section.
- **Calendar events with only UTC times**, and incomplete schema.org markup,
  are not read directly (the local date would be uncertain); those messages go
  through the models instead.
- **Hyphenated or run-together surnames** need an explicit name variant (see
  [Setup](#setup)).
- **Schedule changes are handled per leg.** A change that reroutes you
  through a different airport replaces only legs with the same origin and
  destination, so the old leg can remain; remove it in review.
- **Testing has been English-centred.** The synthetic test mailbox has one
  Spanish email; other languages should work but are less tested.

## Troubleshooting

Messages from the preflight check (classify, extract and audit):

- `refusing to use the Ollama server at ...: ... and email content would leave this machine.`
  The `[ollama] address` is not on this machine. Point it at a local server
  (`http://localhost:11434`), or see the override under [Privacy](#privacy).
- ``the Ollama server at ... is not reachable (...). Start it with `ollama serve`, or open the Ollama app, then rerun <command>.``
  Start Ollama, or fix `[ollama] address`.
- `the Ollama server at ... is version ..., but flighty-exporter needs Ollama 0.35.0 or later ...`
  Update Ollama from https://ollama.com/download.
- ``the model ... ([models] decision) is not pulled on the Ollama server at .... Pull it with `ollama pull ...`, then rerun <command>.``
  (or `[models] extraction`) Run the `ollama pull` command it names.
- `warning: could not read the Ollama version from ...; continuing, ...`
  The server did not report a version it could read. The run continues; make
  sure your Ollama is 0.35.0 or later.
- `the server at ... answered /api/version with HTTP ...; is it an Ollama server?`,
  `... did not list its models at /api/tags ...` or
  `... with something that is not JSON ...`
  Something other than Ollama is answering at `[ollama] address`.

If Ollama stops answering in the middle of a pass, the pass stops with
``cannot reach Ollama at ...: ... Is `ollama serve` running?`` and `Progress so far is saved; rerun <command> to resume.`

Other messages:

- ``no mailbox given: pass --mailbox PATH or set `mailbox` in the config file``
  or `mailbox file not found: ...`: set `mailbox` to the full path of the
  `.mbox` file.
- `store not found: ... Run classify first.` (audit) or
  `... Run classify and extract first.` (review): run the earlier passes, or
  check `store` and that you are in the directory with your config file.
- `review table ... has been edited since review wrote it ...`: see
  overwrite protection under [review](#4-review).
- `review table ... has no ... column ...` (export): a required column header
  was renamed or deleted. Restore it from a freshly written table (for
  example `review --output fresh.csv`).
- `review table ... is not UTF-8 text ...`: save it again as "CSV UTF-8".
- `OpenFlights data file ... is not in ... and could not be downloaded ...`:
  the reference data could not be fetched. Check your connection, or download
  `airports.dat` and `airlines.dat` by hand from the `data_url` into
  `data_dir`.
- `unknown config setting: ...`: a typo in the config file.

**A store from an older version** of the tool is upgraded automatically the
first time a command opens it; nothing needs to be redone. (A store written by
a version that did not record PDF attachments has them filled in by the next
`audit`, with one extra scan of the mailbox. One written before message
positions were recorded has them found by the next `extract` or `audit`, with
one scan of the mailbox's headers.)

**Forcing reclassification.** Results are stored per model name, so changing
`[models] decision` (or `extraction`) makes the next run classify (or extract)
again with the new model. There is no option to reclassify with the same
model: to start over, delete the store file (`flighty_exporter.db` and its
`-wal`/`-shm` files) or point `store` (or `--store`) at a new file. This also
discards the extractions, which then have to be redone. Changing
`[classify] threshold` never needs reclassification.

## Development

```
uv run pytest
```

The tests drive the tool the way a user does: each one builds a small mbox of
invented emails and a config file in a temporary directory, runs the command
line, and checks only the files written, the exit status and the printed
output. The one fake is the Ollama server, replaced at the HTTP boundary with
scripted answers, so the suite needs no model and no network.

`test.mbox` is a synthetic 200-message Gmail-style mailbox for trying the
whole pipeline with the real models, and `test.mbox.truth.json` lists the
flight emails in it and the flights they describe, for comparing with the
review table. Both were generated by `scripts/generate_test_mbox.py`, which
fixes the people, bookings and flights with a seed and has a local Ollama
model write only the subject and body text. To try it, set
`mailbox = "test.mbox"` and `names = ["Jordan Ellis"]` in your config.

The rule for contributions: no real email, name, address, booking reference or
flight ever goes into the repository, including test fixtures. Everything in
the tests and the test mailbox is invented.

### Layout

The package `src/flighty_exporter/` is grouped by responsibility:

| Folder | What lives there |
|---|---|
| `mail/` | Turning an mbox into readable messages: `reader` (MIME), `html_text`, `pdf_text`, and `markup` (machine-readable flight data). The only place that knows about MIME, HTML, PDFs or calendar files. |
| `ollama/` | Everything that talks to the model server: the `decision` and `extraction` clients, `preflight`, and `connection` (the shared HTTP opener and the errors that stop a pass). |
| `flights/` | The pure domain, with no I/O: `records` (the record types and their validation), `consolidate`, `validate`, `passengers`, `decisions` (review decisions and the flight identity they are keyed by). |
| `stages/` | The pipeline passes that tie mail, model server and store together: `classify`, `extract`, `audit`. |
| `output/` | What the user gets at the end: `review`, `sources` (a flight's source emails from the store), `flight_view` (the flight list as the review screen and `flights` show it), `decide` (one review decision at a time), `export`, and `openflights` (the import file format and reference data). |
| `app/` | The full-screen application the bare command opens (Textual): `shell` (the application and moving between screens), `runner` (runs the engine on a thread and turns its events into messages) and `screens/` (one module per screen). Draws what the engine reports; no pipeline logic. |
| `engine/` | The run engine behind `run`: `pipeline` (the stages in order, reported as events), `events`, `report` (the end-of-run report), `session` (describing and starting fresh a session), `thorough` (what a Thorough run does after its audit), and `models` (the locality check, model clients and the store opened with the lazy preflight check, shared with the single commands). Prints nothing. |
| top level | Cross-cutting pieces: `cli` (entry point), `config`, `store`, `progress`, `tables` (CSV writing and message dates shared by audit, review and export), and `threshold` (the session's effective threshold). |

The dependency rule, enforced by `tests/test_layout.py`:

- `flights` imports nothing else from the package.
- `mail` imports nothing from `ollama`, `stages`, `output` or the top level,
  except the record types from `flights.records` (the markup parser produces
  a record).
- `ollama` may import the message type (`mail.reader.Message`) and the record
  types; nothing from `stages`, `output` or the top level.
- `config`, `store`, `progress`, `tables` and `threshold` may import `flights` only.
- `stages` and `output` may import from everything below them, but not from
  each other; neither imports `cli`.
- `engine` may import `stages`, `output` and everything below them; only
  `cli` and `app` import it.
- `app` may import `engine` and everything below it; only `cli` imports it,
  and only when the application opens.
- No module imports a name beginning with an underscore from another module,
  and there are no import cycles. A new module must be given a group in the
  test.

## Licence and credits

MIT, see [LICENSE](LICENSE).

Airport and airline reference data comes from
[OpenFlights](https://openflights.org/data) (`airports.dat` and
`airlines.dat`), made available under the
[Open Database License](https://opendatacommons.org/licenses/odbl/1-0/). It is
not redistributed with this tool: it is downloaded on first use and cached on
your machine. The import file follows OpenFlights' backup CSV format.

Flighty is a trademark of its owner. This project is not affiliated with or
endorsed by Flighty, OpenFlights, Google or Ollama.
