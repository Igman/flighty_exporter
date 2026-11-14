"""Command-line entry point: ``flighty-exporter [--config FILE] <command>``.

Each pass is a subcommand registered in ``COMMANDS``: ``classify``,
``extract``, ``review``, ``audit`` and ``export``; ``run`` performs
classify, extract and review in one go through the run engine (see
``engine.pipeline``) and prints its events.

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
from .engine.events import CLASSIFY, EXTRACT, UNREACHABLE, Event, Notice, Problem, RunFinished, StageFinished, StageStarted
from .engine.models import address_warning, decision_client, extraction_client, pass_store
from .engine.pipeline import STOPPED_BY_PROBLEM, STOPPED_ON_REQUEST, RunError, run_pipeline
from .engine.report import format_report
from .mail.reader import MailboxError
from .ollama.connection import OllamaUnreachable, PreflightError
from .ollama.decision import DecisionClient
from .ollama.extraction import ExtractionClient
from .output import export, review
from .output.openflights import ReferenceDataError
from .stages import audit, extract
from .stages.classify import format_summary, run_classify
from .store import Store

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
    review_path = Path(args.review).expanduser() if args.review else config.review_output
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else config.export_output_dir
    try:
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


COMMANDS["export"] = (
    "turn the kept rows of the edited review table into an import file for Flighty and a manual-add list",
    _export_arguments,
    cmd_export,
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
            print(format_report(event.report))

    def _summary(self, event: StageFinished) -> None:
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


def cmd_run(args: argparse.Namespace, config: Config) -> int:
    mailbox_path = _mailbox_path(args, config)
    store_path = _store_path(args, config)
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
        "quick runs no audit",
    )
    parser.add_argument("--output", help="review CSV to write (overrides [review] output in config)")
    parser.add_argument("--failures", help="failures CSV to write (overrides [review] failures in config)")
    parser.add_argument(
        "--force", action="store_true", help="regenerate the review CSV even if it has been edited"
    )


COMMANDS["run"] = (
    "run classify, extract and review in one go, resuming where an earlier run stopped, with progress and "
    "an end-of-run report",
    _run_arguments,
    cmd_run,
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
