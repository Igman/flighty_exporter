"""Command-line entry point: ``flighty-exporter [--config FILE] <command>``.

Each pass is a subcommand registered in ``COMMANDS``: ``classify``,
``extract``, ``review``, ``audit`` and ``export``.

The model commands (classify, extract, audit) first refuse an Ollama address
that is not on this machine, and check the server just before their first
model request (see ``preflight``). Until that check has passed, their writes
to the store are held, so a failed check leaves the store as it was.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG_NAME, Config, ConfigError, load_config
from .flights.decisions import is_date, resolve_key
from .mail.reader import MailboxError
from .ollama.connection import OllamaUnreachable, PreflightError
from .ollama.decision import DecisionClient
from .ollama.extraction import ExtractionClient
from .ollama.preflight import PREFLIGHT_TIMEOUT, check_address, check_server
from .output import export, review
from .output.openflights import ReferenceDataError
from .stages import audit, extract
from .stages.classify import format_summary, run_classify
from .store import Store

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


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


def _check_address(args: argparse.Namespace, config: Config) -> None:
    """Refuse a non-local Ollama address before anything is sent to it."""
    warning = check_address(config.ollama_address, config.ollama_allow_remote or args.allow_remote)
    if warning:
        print(warning, file=sys.stderr)


@contextmanager
def _pass_store(
    store_path: Path, client: DecisionClient | ExtractionClient, setting: str, command: str, config: Config
) -> Iterator[Store]:
    """Open the store for a model pass, with the preflight check run just
    before the client's first request. Writes are held until the check has
    passed; if it fails they are rolled back, and a store this run created
    is removed again."""
    existed = store_path.exists()
    store = Store(store_path)
    store.hold_writes()

    def preflight() -> None:
        check_server(
            config.ollama_address,
            client.model,
            setting,
            command,
            timeout=min(PREFLIGHT_TIMEOUT, config.ollama_timeout_seconds),
            warn=lambda text: print(text, file=sys.stderr),
        )
        store.release_writes(keep=True)

    client.before_first_request = preflight
    failed = False
    try:
        yield store
    except PreflightError:
        failed = True
        raise
    finally:
        store.release_writes(keep=not failed)
        store.close()
        if failed and not existed:
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(f"{store_path}{suffix}").unlink(missing_ok=True)


def _extraction_client(config: Config) -> ExtractionClient:
    """The extraction client extract and audit both use, so both send the same requests."""
    return ExtractionClient(
        config.ollama_address,
        config.extraction_model,
        timeout=config.ollama_timeout_seconds,
        num_ctx=config.extract_num_ctx,
        num_predict=config.extract_num_predict,
        retries=config.extract_retries,
        max_input_chars=config.extract_max_input_chars,
    )


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
    client = DecisionClient(
        config.ollama_address,
        config.decision_model,
        config.ollama_timeout_seconds,
        max_body_chars=config.classify_max_body_chars,
    )
    with _pass_store(store_path, client, "[models] decision", "classify", config) as store:
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
        audit.write_audit_csv(output, result, config.threshold)
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
    try:
        if from_store:
            with Store(store_path) as store:
                summary = export.run_store_export(
                    config, store, output_dir, config.export_data_dir, config.export_data_url
                )
        elif store_path.is_file():
            with Store(store_path) as store:
                summary = export.run_export(
                    review_path, output_dir, config.export_data_dir, config.export_data_url, store=store, config=config
                )
        else:
            summary = export.run_export(review_path, output_dir, config.export_data_dir, config.export_data_url)
    except (export.ExportError, ReferenceDataError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(export.format_summary(summary))
    return EXIT_OK


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
        store.set_added(key, not args.undo)
    if args.undo:
        print(f"Flight {key} is no longer marked as added in Flighty.")
    else:
        print(f"Flight {key} is marked as added in Flighty; export shows it as done on the manual-add list.")
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
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, (help_text, add_arguments, handler) in COMMANDS.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        add_arguments(sub)
        sub.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
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
