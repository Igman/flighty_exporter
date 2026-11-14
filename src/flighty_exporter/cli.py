"""Command-line entry point: ``flighty-exporter [--config FILE] <command>``.

With no command, in a terminal, it opens the full-screen application (see
``app``); outside a terminal it fails with the usage line as before.

Each pass is a subcommand registered in ``COMMANDS``: ``classify``,
``extract``, ``review``, ``audit`` and ``export``; ``run`` performs
classify, extract and review in one go through the run engine (see
``engine.pipeline``) and prints its events. ``mark-added`` ticks a flight on
the manual-add list in the store, and ``sources`` lists the saved text of
each flight's source emails.
``session`` describes the session in the store and, with
``--fresh-preview``, what ``run --fresh`` would delete (see
``engine.session``).
``setup`` writes guided setup's three answers to the config file (see
``engine.setup``) and with ``--check`` reports whether the model server is
ready (``engine.readiness``); ``pull-models`` downloads missing models
through the server (``ollama.pull``), only when given ``--yes``.

The model commands (classify, extract, audit) first refuse an Ollama address
that is not on this machine, and check the server just before their first
model request (see ``preflight``). Until that check has passed, their writes
to the store are held, so a failed check leaves the store as it was.
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG_NAME, Config, ConfigError, load_config
from .engine import session
from .engine import setup
from .engine.readiness import check_readiness, format_readiness
from .ollama.pull import KEPT_PARTIAL, PullFailed, PullProgress, format_bytes, pull_model
from .engine.events import CLASSIFY, EXTRACT, UNREACHABLE, Event, Notice, Problem, RunFinished, StageFinished, StageStarted
from .engine.models import address_warning, decision_client, extraction_client, pass_store
from .engine.pipeline import STOPPED_BY_PROBLEM, STOPPED_ON_REQUEST, RunError, run_pipeline
from .engine.report import format_report, run_next_step
from .engine.events import AUDIT, INCLUDE, STOPPED
from .engine.thorough import format_include
from .flights.decisions import InvalidCorrection, is_date, resolve_key
from .mail.reader import MailboxError
from .ollama.connection import OllamaUnreachable, PreflightError
from .ollama.decision import DecisionClient
from .ollama.extraction import ExtractionClient
from .output import decide, export, flight_view, review, sources
from .output.openflights import ReferenceDataError
from .stages import audit, extract
from .stages.classify import format_summary, run_classify
from .store import Store
from .threshold import effective_threshold

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
EXIT_STOPPED = 143  # run asked to stop (SIGTERM) between messages


class UsageError(Exception):
    pass


def _mailbox_path(args: argparse.Namespace, config: Config) -> Path:
    mailbox_path = Path(args.mailbox).expanduser() if args.mailbox else config.mailbox
    if mailbox_path is None:
        raise UsageError(
            "no mailbox given: pass --mailbox PATH or set `mailbox` in the config file"
        )
    if not mailbox_path.is_file():
        raise MailboxError(f"mailbox file not found: {mailbox_path}")
    return mailbox_path


def _store_path(args: argparse.Namespace, config: Config) -> Path:
    return Path(args.store).expanduser() if args.store else config.store


def _warn(text: str) -> None:
    print(text, file=sys.stderr)


def _check_address(args: argparse.Namespace, config: Config) -> None:
    """Refuse a non-local Ollama address before anything is sent to it."""
    warning = address_warning(config, args.allow_remote)
    if warning:
        _warn(warning)


def _pass_store(
    store_path: Path, client: DecisionClient | ExtractionClient, setting: str, command: str, config: Config
) -> AbstractContextManager[Store]:
    """The store for a model pass, with the lazy preflight check (see ``engine.models``)."""
    return pass_store(store_path, client, setting, command, config, warn=_warn)


_extraction_client = extraction_client


def _pass_stopped(exc: OllamaUnreachable, format_partial: Callable[[Any], str], rerun: str) -> int:
    """Report a model pass stopped by an unreachable server: the summary so
    far (if the pass attached one), the error, and how to resume."""
    summary = getattr(exc, "summary", None)
    if summary is not None:
        print(format_partial(summary))
    print(f"error: {exc}", file=sys.stderr)
    print(f"Progress so far is saved; rerun {rerun} to resume.", file=sys.stderr)
    return EXIT_ERROR


def cmd_classify(args: argparse.Namespace, config: Config) -> int:
    _check_address(args, config)
    mailbox_path = _mailbox_path(args, config)
    store_path = _store_path(args, config)
    client = decision_client(config)
    with _pass_store(store_path, client, "[models] decision", "classify", config) as store:
        session.begin(store, config, mailbox_path, config.thoroughness)
        try:
            summary = run_classify(config, mailbox_path, store, client)
        except KeyboardInterrupt as exc:
            print(format_summary(exc.summary, config, store))  # type: ignore[attr-defined]
            return EXIT_INTERRUPTED
        except OllamaUnreachable as exc:
            return _pass_stopped(exc, lambda partial: format_summary(partial, config, store), "classify")
        print(format_summary(summary, config, store))
    return EXIT_OK


def cmd_extract(args: argparse.Namespace, config: Config) -> int:
    _check_address(args, config)
    mailbox_path = _mailbox_path(args, config)
    client = _extraction_client(config)
    with _pass_store(_store_path(args, config), client, "[models] extraction", "extract", config) as store:
        session.begin(store, config, mailbox_path, config.thoroughness)
        try:
            summary = extract.run_extract(config, mailbox_path, store, client)
        except KeyboardInterrupt as exc:
            print(extract.format_summary(exc.summary, config, store))  # type: ignore[attr-defined]
            return EXIT_INTERRUPTED
        except OllamaUnreachable as exc:
            return _pass_stopped(exc, lambda partial: extract.format_summary(partial, config, store), "extract")
        print(extract.format_summary(summary, config, store))
    return EXIT_OK


def cmd_audit(args: argparse.Namespace, config: Config) -> int:
    _check_address(args, config)
    mailbox_path = _mailbox_path(args, config)
    store_path = _store_path(args, config)
    output = Path(args.output).expanduser() if args.output else config.audit_output
    sample_size = args.sample_size if args.sample_size is not None else config.audit_sample_size
    seed = args.seed if args.seed is not None else config.audit_seed
    if sample_size < 1:
        raise UsageError("--sample-size must be at least 1")
    if not store_path.is_file():
        print(f"error: store not found: {store_path}. Run classify first.", file=sys.stderr)
        return EXIT_ERROR
    client = _extraction_client(config)
    with _pass_store(store_path, client, "[models] extraction", "audit", config) as store:
        session.begin(store, config, mailbox_path, config.thoroughness)
        try:
            result = audit.run_audit(config, mailbox_path, store, client, sample_size, seed)
        except KeyboardInterrupt as exc:
            print(audit.format_interrupted(exc.summary))  # type: ignore[attr-defined]
            return EXIT_INTERRUPTED
        except OllamaUnreachable as exc:
            return _pass_stopped(exc, audit.format_interrupted, "audit with the same seed")
        if not result.classified:
            print(f"error: {audit.no_population_message(result, config)}", file=sys.stderr)
            return EXIT_ERROR
        audit.write_audit_csv(output, result, effective_threshold(config, store))
        session.record_written(store, session.AUDIT_REPORT, output)
        print(audit.format_report(result, config, store, output))
    return EXIT_OK


def cmd_review(args: argparse.Namespace, config: Config) -> int:
    review_path = Path(args.output).expanduser() if args.output else config.review_output
    failures_path = Path(args.failures).expanduser() if args.failures else config.failures_output
    store_path = _store_path(args, config)
    if not store_path.is_file():
        print(f"error: store not found: {store_path}. Run classify and extract first.", file=sys.stderr)
        return EXIT_ERROR
    with Store(store_path) as store:
        try:
            summary = review.write_review(config, store, review_path, failures_path, args.force)
        except review.ReviewExists as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        session.record_written(store, session.REVIEW_TABLE, review_path)
        session.record_written(store, session.FAILURES_FILE, failures_path)
        session.touch(store)
    print(review.format_summary(summary))
    for warning in review.format_warnings(summary):
        print(warning, file=sys.stderr)
    return EXIT_OK


def cmd_export(args: argparse.Namespace, config: Config) -> int:
    """Three cases: a review table given or present is exported, after its
    keep values and corrections are recorded as decisions in the store (if
    there is one); with no review table, or with --from-store, the store's
    flight list is exported with the saved decisions applied."""
    review_path = Path(args.review).expanduser() if args.review else config.review_output
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else config.export_output_dir
    store_path = _store_path(args, config)
    if args.from_store and args.review:
        raise UsageError("--from-store exports from the store and ignores the review table; do not pass --review too")
    from_store = args.from_store or (not args.review and not review_path.exists() and store_path.is_file())
    if from_store and not store_path.is_file():
        print(f"error: store not found: {store_path}. Run classify and extract first.", file=sys.stderr)
        return EXIT_ERROR
    if args.dry_run and not from_store:
        raise UsageError("--dry-run shows what exporting from the store would do; pass --from-store with it")
    try:
        if args.dry_run:
            with Store(store_path) as store:
                plan = export.plan_store_export(config, store, config.export_data_dir, config.export_data_url)
            print(export.format_plan(plan))
            return EXIT_OK
        if from_store:
            with Store(store_path) as store:
                summary = export.run_store_export(
                    config, store, output_dir, config.export_data_dir, config.export_data_url
                )
                _record_export(store, summary)
        elif store_path.is_file():
            with Store(store_path) as store:
                summary = export.run_export(
                    review_path, output_dir, config.export_data_dir, config.export_data_url, store=store, config=config
                )
                _record_export(store, summary)
        else:
            summary = export.run_export(review_path, output_dir, config.export_data_dir, config.export_data_url)
    except (export.ExportError, ReferenceDataError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(export.format_summary(summary))
    return EXIT_OK


def _record_export(store: Store, summary: export.ExportSummary) -> None:
    """Record the files an export wrote for the session in ``store``."""
    session.record_export(store, summary.import_path, summary.manual_add_path)


COMMANDS: dict[str, tuple[str, Callable[[argparse.ArgumentParser], None], Callable[..., int]]] = {}


def _allow_remote_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="send email content to the configured Ollama address even if it is not on this machine "
        "(same as [ollama] allow_remote = true)",
    )


def _classify_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mailbox", help="mbox file to read (overrides `mailbox` in config)")
    parser.add_argument("--store", help="SQLite store file (overrides `store` in config)")
    _allow_remote_argument(parser)


COMMANDS["classify"] = (
    "read flights straight from messages that carry machine-readable flight data (schema.org markup or a "
    "flight calendar file), and send every other message to the decision model and store the gate "
    "probability and email type",
    _classify_arguments,
    cmd_classify,
)


def _extract_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mailbox", help="mbox file to read bodies from (overrides `mailbox` in config)")
    parser.add_argument("--store", help="SQLite store file (overrides `store` in config)")
    _allow_remote_argument(parser)


COMMANDS["extract"] = (
    "send each message at or above the threshold to the extraction model and store its flight record",
    _extract_arguments,
    cmd_extract,
)


def _review_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", help="SQLite store file (overrides `store` in config)")
    parser.add_argument("--output", help="review CSV to write (overrides [review] output in config)")
    parser.add_argument("--failures", help="failures CSV to write (overrides [review] failures in config)")
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing review CSV even if it has been edited"
    )


COMMANDS["review"] = (
    "consolidate the extraction records into one row per flight and write the review table and failures file",
    _review_arguments,
    cmd_review,
)


def _audit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mailbox", help="mbox file to read bodies from (overrides `mailbox` in config)")
    parser.add_argument("--store", help="SQLite store file (overrides `store` in config)")
    parser.add_argument(
        "--sample-size", type=int, help="messages sampled per gate-probability band (overrides [audit] sample_size)"
    )
    parser.add_argument("--seed", type=int, help="seed for the reproducible sample (overrides [audit] seed)")
    parser.add_argument("--output", help="audit CSV to write (overrides [audit] output in config)")
    _allow_remote_argument(parser)


COMMANDS["audit"] = (
    "sample messages from every gate-probability band, including below the threshold, send the sampled ones "
    "to the extraction model (stored like extract) and report per band how many contained flight segments",
    _audit_arguments,
    cmd_audit,
)


def _export_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--review", help="edited review CSV to read (overrides [review] output in config)")
    parser.add_argument(
        "--output-dir", help="directory for the import file and manual-add list (overrides [export] output_dir)"
    )
    parser.add_argument(
        "--store", help="SQLite store file to record decisions in or export from (overrides `store` in config)"
    )
    parser.add_argument(
        "--from-store",
        action="store_true",
        help="export the store's flight list with the saved review decisions, even if a review table exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --from-store: print what the export would write (the flights for the import file, and the "
        "manual-add list with reasons and ticks) and write nothing",
    )


COMMANDS["export"] = (
    "turn the kept rows of the edited review table into an import file for Flighty and a manual-add list",
    _export_arguments,
    cmd_export,
)


def cmd_mark_added(args: argparse.Namespace, config: Config) -> int:
    if not is_date(args.date):
        raise UsageError(f"date {args.date!r} is not a real YYYY-MM-DD date")
    if bool(args.flight_number) == bool(args.route):
        raise UsageError("give the flight number, or --route ORIGIN DEST for a flight without one (not both)")
    store_path = _store_path(args, config)
    if not store_path.is_file():
        print(f"error: store not found: {store_path}. Run classify and extract first.", file=sys.stderr)
        return EXIT_ERROR
    origin, destination = args.route or ("", "")
    with Store(store_path) as store:
        key = resolve_key(store.review_decisions(), args.date, args.flight_number or "", origin, destination)
        # A manual-add list an export already wrote follows the tick at once.
        refreshed, problems = session.tick_manual_add(store, key, not args.undo)
    for problem in problems:
        _warn(f"warning: {problem}")
    if args.undo:
        print(f"Flight {key} is no longer marked as added in Flighty.")
    else:
        print(f"Flight {key} is marked as added in Flighty; export shows it as done on the manual-add list.")
    for path in refreshed:
        print(f"Manual-add list updated: {path}")
    return EXIT_OK


def _mark_added_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("date", help="the flight's date, YYYY-MM-DD, as on the manual-add list")
    parser.add_argument("flight_number", nargs="?", help="the flight number, as on the manual-add list")
    parser.add_argument(
        "--route", nargs=2, metavar=("ORIGIN", "DEST"), help="origin and destination, for a flight without a number"
    )
    parser.add_argument("--undo", action="store_true", help="remove the tick instead of setting it")
    parser.add_argument("--store", help="SQLite store file (overrides `store` in config)")


COMMANDS["mark-added"] = (
    "tick a flight on the manual-add list as entered in Flighty by hand (or untick it with --undo)",
    _mark_added_arguments,
    cmd_mark_added,
)


def cmd_sources(args: argparse.Namespace, config: Config) -> int:
    store_path = _store_path(args, config)
    if not store_path.is_file():
        print(f"error: store not found: {store_path}. Run classify and extract first.", file=sys.stderr)
        return EXIT_ERROR
    choice = tuple(args.flight) if args.flight else None
    with Store(store_path) as store:
        try:
            print(sources.format_sources(config, store, choice, with_text=not args.no_text))
        except sources.NoSuchFlight as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
    return EXIT_OK


def _sources_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", help="SQLite store file (overrides `store` in config)")
    parser.add_argument(
        "--flight",
        nargs=2,
        metavar=("DATE", "FLIGHT"),
        help="list only the flight on DATE (YYYY-MM-DD) with flight number FLIGHT "
        "(or route ORIGIN-DESTINATION for a flight without a number)",
    )
    parser.add_argument("--no-text", action="store_true", help="list the source emails without their text")


COMMANDS["sources"] = (
    "list each flight review would list with its source emails and their saved text (needs only the store)",
    _sources_arguments,
    cmd_sources,
)


def cmd_flights(args: argparse.Namespace, config: Config) -> int:
    if args.year is not None and not (len(args.year) == 4 and args.year.isdigit()):
        raise UsageError(f"--year {args.year!r} is not a year (YYYY)")
    store_path = _store_path(args, config)
    if not store_path.is_file():
        print(f"error: store not found: {store_path}. Run classify and extract first.", file=sys.stderr)
        return EXIT_ERROR
    filters = flight_view.Filters(flagged=args.flagged, not_kept=args.not_kept, year=args.year)
    with Store(store_path) as store:
        flight_list = review.build_flight_list(config, store)
    print(flight_view.format_flights(flight_list, filters, config.thoroughness))
    return EXIT_OK


def _flights_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", help="SQLite store file (overrides `store` in config)")
    parser.add_argument("--flagged", action="store_true", help="only flights needing attention")
    parser.add_argument("--not-kept", action="store_true", help="only flights not kept")
    parser.add_argument("--year", metavar="YYYY", help="only flights in this year")


COMMANDS["flights"] = (
    "list the flights as the review screen shows them (flights needing attention first), with the totals, "
    "the bookings dropped as other passengers' and the emails that could not be read (reads only)",
    _flights_arguments,
    cmd_flights,
)


CORRECTABLE = ("date", "flight_number", "origin", "destination")


def cmd_decide(args: argparse.Namespace, config: Config) -> int:
    if bool(args.flight_number) == bool(args.route):
        raise UsageError("give the flight number, or --route ORIGIN DEST for a flight without one (not both)")
    typed: dict[str, str] = {}
    for item in args.set or ():
        name, sep, value = item.partition("=")
        if not sep or name.strip() not in CORRECTABLE:
            raise UsageError(
                f"--set {item!r}: give field=value, the field being date, flight_number, origin or destination"
            )
        typed[name.strip()] = value
    store_path = _store_path(args, config)
    if not store_path.is_file():
        print(f"error: store not found: {store_path}. Run classify and extract first.", file=sys.stderr)
        return EXIT_ERROR
    origin, destination = args.route or ("", "")
    with Store(store_path) as store:
        reviewer = decide.Reviewer.load(config, store)
        try:
            key = reviewer.find(args.date, args.flight_number or "", origin, destination).key
        except decide.FlightNotFound as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if args.keep or args.drop:
            reviewed = reviewer.set_keep(store, key, args.keep)
        elif args.reset:
            reviewed = reviewer.reset(store, key)
        else:
            segment = reviewer.get(key).flight.segment
            # A field not given keeps the value it now shows.
            values = [typed.get(name, getattr(segment, name)) for name in CORRECTABLE]
            try:
                reviewed = reviewer.correct(store, key, *values)
            except InvalidCorrection as exc:
                print(f"error: {exc}; nothing was saved", file=sys.stderr)
                return EXIT_ERROR
        session.touch(store)
    print("\n".join(flight_view.detail_lines(reviewed)))
    return EXIT_OK


def _decide_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("date", help="the flight's date, YYYY-MM-DD, as it now shows (after any correction)")
    parser.add_argument("flight_number", nargs="?", help="the flight number, as it now shows")
    parser.add_argument(
        "--route", nargs=2, metavar=("ORIGIN", "DEST"), help="origin and destination, for a flight without a number"
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--keep", action="store_true", help="keep the flight")
    action.add_argument("--drop", action="store_true", help="do not keep the flight")
    action.add_argument("--reset", action="store_true", help="clear the flight's keep choice and corrections")
    action.add_argument(
        "--set",
        action="append",
        metavar="FIELD=VALUE",
        help="correct date, flight_number, origin or destination (repeat for several fields); "
        "a value equal to what the tool found clears that correction",
    )
    parser.add_argument("--store", help="SQLite store file (overrides `store` in config)")


COMMANDS["decide"] = (
    "make one review decision, as the review screen does: keep or drop a flight, correct its date, flight "
    "number, origin or destination, or clear its decision; saved at once, then the flight's new state is printed",
    _decide_arguments,
    cmd_decide,
)


RUN_RESUME = "Progress so far is saved; rerun run to resume."


class _RunPrinter:
    """Prints the run engine's events the way the single commands print
    their passes: a start line and the pass's own summary for each stage
    (the passes print their progress lines themselves), problems and
    warnings on stderr, then the end-of-run report."""

    def __init__(self, config: Config, store_path: Path):
        self.config = config
        self.store_path = store_path

    def __call__(self, event: Event) -> None:
        if isinstance(event, StageStarted):
            print(f"Starting {event.stage}.")
        elif isinstance(event, StageFinished) and event.summary is not None:
            self._summary(event)
            print()
        elif isinstance(event, Notice):
            _warn(event.message)
        elif isinstance(event, Problem):
            print(f"error: {event.message}", file=sys.stderr)
        elif isinstance(event, RunFinished):
            print(format_report(event.report, run_next_step(event.report)))

    def _summary(self, event: StageFinished) -> None:
        if event.stage == AUDIT:
            self._audit_summary(event)
            return
        if event.stage == INCLUDE:
            print(format_include(event.summary, stopped=event.state == STOPPED))
            return
        if event.stage in (CLASSIFY, EXTRACT):
            formatter = format_summary if event.stage == CLASSIFY else extract.format_summary
            with Store(self.store_path) as store:
                print(formatter(event.summary, self.config, store))
            return
        if event.note:
            print(f"Review table {event.summary.review_path}: {event.note}.")
            print(f"Failures file written to {event.summary.failures_path}")
        else:
            print(review.format_summary(event.summary))
        for warning in review.format_warnings(event.summary):
            _warn(warning)

    def _audit_summary(self, event: StageFinished) -> None:
        result = event.summary
        if event.state == STOPPED:
            print(audit.format_interrupted(result))
        elif not result.classified:
            print(audit.no_population_message(result, self.config))
        else:
            with Store(self.store_path) as store:
                # The include stage acts on the reading at once: no advice to do it by hand.
                print(audit.format_report(result, self.config, store, self.config.audit_output, advice=False))


def cmd_run(args: argparse.Namespace, config: Config) -> int:
    mailbox_path = _mailbox_path(args, config)
    store_path = _store_path(args, config)
    if args.fresh:
        # Never asks: the session is cleared and the run starts from scratch.
        for line in session.format_fresh(session.start_fresh(store_path)):
            print(line)
    stop_requested: list[int] = []
    # SIGTERM asks the run to stop after the message in progress.
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: stop_requested.append(signum))
    try:
        result = run_pipeline(
            config,
            mailbox_path,
            store_path,
            allow_remote=args.allow_remote,
            thoroughness=args.thoroughness,
            review_path=Path(args.output).expanduser() if args.output else None,
            failures_path=Path(args.failures).expanduser() if args.failures else None,
            force=args.force,
            subscriber=_RunPrinter(config, store_path),
            should_stop=lambda: bool(stop_requested),
        )
    except RunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(f"Interrupted. {RUN_RESUME}", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        signal.signal(signal.SIGTERM, previous)
    if result.state == STOPPED_BY_PROBLEM:
        if result.problem is not None and result.problem.kind == UNREACHABLE:
            print(RUN_RESUME, file=sys.stderr)
        return EXIT_ERROR
    if result.state == STOPPED_ON_REQUEST:
        print(f"Stopped. {RUN_RESUME}", file=sys.stderr)
        return EXIT_STOPPED
    return EXIT_OK


def _run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mailbox", help="mbox file to read (overrides `mailbox` in config)")
    parser.add_argument("--store", help="SQLite store file (overrides `store` in config)")
    _allow_remote_argument(parser)
    parser.add_argument(
        "--thoroughness",
        choices=("quick", "thorough"),
        help="how hard to look for flight emails the first filter missed (overrides [run] thoroughness); "
        "quick runs no audit; thorough audits after extract and, within [audit] auto_include_budget, lowers the "
        "threshold for this session",
    )
    parser.add_argument("--output", help="review CSV to write (overrides [review] output in config)")
    parser.add_argument("--failures", help="failures CSV to write (overrides [review] failures in config)")
    parser.add_argument(
        "--force", action="store_true", help="regenerate the review CSV even if it has been edited"
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="start fresh without asking: delete the store, its side files and the files the tool wrote for "
        "it, then run from scratch (the mailbox, config file and reference data are kept)",
    )


COMMANDS["run"] = (
    "run classify, extract and review in one go, resuming where an earlier run stopped, with progress and "
    "an end-of-run report",
    _run_arguments,
    cmd_run,
)


def cmd_session(args: argparse.Namespace, config: Config) -> int:
    store_path = _store_path(args, config)
    if args.fresh_preview:
        result = session.start_fresh(store_path, preview=True)
        if not result.store_existed:
            print("No session yet.")
            return EXIT_OK
        print("Starting fresh would delete:")
        for line in session.format_fresh(result):
            print(f"  {line}")
        print(session.KEPT_WHEN_FRESH)
        return EXIT_OK
    description = session.describe(config, store_path)
    print("No session yet." if description is None else session.format_description(description))
    return EXIT_OK


def _session_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", help="SQLite store file (overrides `store` in config)")
    parser.add_argument(
        "--fresh-preview",
        action="store_true",
        help="list what starting fresh (run --fresh) would delete, without deleting anything",
    )


COMMANDS["session"] = (
    "describe the session in the store: the mailbox and settings it was started with, how far each stage "
    "got, when it was last active, the files it wrote and what starting fresh would lose (reads only)",
    _session_arguments,
    cmd_session,
)


def cmd_setup(args: argparse.Namespace, config: Config | None) -> int:
    """Write guided setup's three answers to the config file. Runs before the
    config is loaded, since the file may not exist yet (see ``main``)."""
    config_path = args.config or Path(DEFAULT_CONFIG_NAME)
    try:
        result = setup.write_answers(
            config_path, setup.mailbox_path(args.mailbox, Path.cwd()), args.name or [], args.thoroughness
        )
    except setup.SetupError as exc:
        print(f"error: {exc} Nothing was written.", file=sys.stderr)
        return EXIT_ERROR
    print(f"{'Created' if result.created else 'Updated'} {result.config_path}:")
    for line in setup.format_written(result):
        print(f"  {line}")
    if not result.created:
        print("Every other setting, comment and section in the file was left as it was.")
    if not args.check:
        return EXIT_OK
    print()
    readiness = check_readiness(load_config(result.config_path), allow_remote=args.allow_remote, command="setup --check")
    for line in format_readiness(readiness):
        print(line)
    return EXIT_OK if readiness.ready else EXIT_ERROR


def _setup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mailbox", required=True, help="the mbox file (from Gmail Takeout) to use")
    parser.add_argument(
        "--name",
        action="append",
        metavar="NAME",
        help="one of your name variants as it appears on bookings; repeat for each variant (none: passenger "
        "filtering is off and every booking is kept)",
    )
    parser.add_argument(
        "--thoroughness",
        choices=("quick", "thorough"),
        default="quick",
        help="quick (the default) runs no audit; thorough also checks for missed flight emails after extract",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="then check that the model server is reachable, new enough and has both configured models",
    )
    _allow_remote_argument(parser)


COMMANDS["setup"] = (
    "write the mailbox, name variants and thoroughness to the config file (creating it if needed and keeping "
    "every other setting), as the application's setup screen does",
    _setup_arguments,
    cmd_setup,
)

def cmd_pull_models(args: argparse.Namespace, config: Config) -> int:
    """List the configured models the server lacks; with --yes, download them."""
    readiness = check_readiness(config, allow_remote=args.allow_remote, command="pull-models")
    for warning in readiness.warnings:
        _warn(warning)
    if readiness.problem is not None:
        print(f"error: {readiness.problem}", file=sys.stderr)
        return EXIT_ERROR
    if not readiness.missing:
        names = " and ".join(model.name for model in readiness.models)
        print(f"Both models are already on the Ollama server at {readiness.address}: {names}.")
        return EXIT_OK
    if not args.yes:
        print(f"Missing on the Ollama server at {readiness.address}:")
        for model in readiness.missing:
            print(f"  {model.name} ({model.setting}): {model.size_text}")
        print("Nothing was downloaded. Run `flighty-exporter pull-models --yes` to download them.")
        return EXIT_OK
    for model in readiness.missing:
        print(f"Downloading {model.name} ({model.setting}, {model.size_text})…")
        printer = _PullPrinter()
        try:
            for progress in pull_model(
                readiness.address,
                model.name,
                allow_remote=config.ollama_allow_remote or args.allow_remote,
                command="pull-models",
                timeout=config.ollama_timeout_seconds,
            ):
                printer(progress)
        except PullFailed as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        except KeyboardInterrupt:
            print(f"Interrupted. {KEPT_PARTIAL}", file=sys.stderr)
            return EXIT_INTERRUPTED
    after = check_readiness(config, allow_remote=args.allow_remote, command="pull-models")
    for model in readiness.missing:
        if any(m.name == model.name and m.present for m in after.models):
            print(f"{model.name} is now on the Ollama server.")
        else:
            print(f"error: the server reported {model.name} as pulled but does not list it.", file=sys.stderr)
            return EXIT_ERROR
    return EXIT_OK


class _PullPrinter:
    """Prints a pull's progress: each new status, and a layer's bytes at
    most every tenth of the way."""

    def __init__(self) -> None:
        self.last: tuple[str, int] | None = None

    def __call__(self, progress: PullProgress) -> None:
        if progress.total:
            done = progress.completed or 0
            percent = done * 100 // progress.total
            if self.last and self.last[0] == progress.status and percent < min(self.last[1] + 10, 100):
                return
            self.last = (progress.status, percent)
            print(f"  {progress.status}: {format_bytes(done)} of {format_bytes(progress.total)} ({percent}%)")
        elif not self.last or self.last[0] != progress.status:
            self.last = (progress.status, 0)
            print(f"  {progress.status}")


def _pull_models_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes", action="store_true", help="download the missing models (without it, only list them with their sizes)"
    )
    _allow_remote_argument(parser)


COMMANDS["pull-models"] = (
    "download the configured models the local Ollama server does not have yet, with progress (lists them and "
    "their approximate sizes unless --yes is given)",
    _pull_models_arguments,
    cmd_pull_models,
)

# Commands that load the config file themselves (``setup`` may create it).
LOADS_OWN_CONFIG = {"setup"}


def _open_app(args: argparse.Namespace, config: Config) -> int:
    """The bare command in a terminal: the full-screen application. Loaded
    only here, so the single commands never import it."""
    from .app import run_app

    return run_app(config, (args.config or Path(DEFAULT_CONFIG_NAME)).resolve())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flighty-exporter",
        description="Backfill historical flights into Flighty from a Gmail Takeout mbox, using local models.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=f"config file (default: ./{DEFAULT_CONFIG_NAME} if present, else built-in defaults)",
    )
    # Not required: the bare command opens the application (see ``main``).
    subparsers = parser.add_subparsers(dest="command", required=False, metavar="command")
    for name, (help_text, add_arguments, handler) in COMMANDS.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        add_arguments(sub)
        sub.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None and not (sys.stdin.isatty() and sys.stdout.isatty()):
        # Outside a terminal the bare command fails as when a command was required.
        parser.error("the following arguments are required: command")
    try:
        if args.command in LOADS_OWN_CONFIG:
            return args.handler(args, None)
        config = load_config(args.config)
        if args.command is None:
            return _open_app(args, config)
        return args.handler(args, config)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (ConfigError, MailboxError, PreflightError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
