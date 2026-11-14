"""The classify pass: every readable message outside the skipped labels is sent
to the decision model, and its gate probability and email type are stored.

First, before any model request, each message is checked for machine-readable
flight data (schema.org FlightReservation markup or a flight calendar file,
see ``markup``). A message that yields a record is stored as an extraction
from ``markup``, gets no classification, and is never sent to either model.
The check is cheap, so it runs for every message without a markup record yet,
including ones a model classified in an earlier run; from then on the markup
record takes precedence over any model extraction for that message.

The threshold is never applied here; it only selects messages for extraction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from ..config import Config
from ..mail.markup import parse_markup
from ..mail.reader import Mailbox, Message, Skipped, Unreadable
from ..ollama.decision import EMAIL_TYPES, DecisionClient, DecisionError
from ..progress import Progress
from ..store import CLASSIFY_PASS, Store
from .summary import failure_lines

PASS_NAME = CLASSIFY_PASS
GATE_BANDS = (0.5, 0.2, 0.05)


@dataclass
class ClassifySummary:
    total: int = 0
    classified: int = 0
    already_classified: int = 0
    resolved_from_markup: int = 0
    already_resolved_from_markup: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    interrupted: bool = False
    # Ended early for another reason (server unreachable, a stop requested).
    stopped: bool = False


def run_classify(
    config: Config,
    mailbox_path: Path,
    store: Store,
    client: DecisionClient,
    progress_stream: TextIO | None = None,
) -> ClassifySummary:
    """Run the pass. KeyboardInterrupt and OllamaUnreachable propagate after
    the summary so far is attached to the exception as ``summary``."""
    mbox = Mailbox(mailbox_path)
    summary = ClassifySummary(total=len(mbox))
    done = store.classified_ids(config.decision_model)
    resolved = store.markup_ids()
    progress = Progress(summary.total, "classify", progress_stream)
    try:
        for entry in mbox:
            if isinstance(entry, Skipped):
                summary.skipped += 1
                progress.advance()
            elif isinstance(entry, Unreadable):
                store.record_failure(entry.identifier, PASS_NAME, f"unreadable message: {entry.reason}")
                summary.failed += 1
                summary.failures.append((entry.identifier, f"unreadable message: {entry.reason}"))
                progress.advance()
            elif entry.identifier in resolved:
                summary.already_resolved_from_markup += 1
                progress.advance()
            elif _resolve_from_markup(entry, store):
                resolved.add(entry.identifier)
                summary.resolved_from_markup += 1
                progress.advance()
            elif entry.identifier in done:
                summary.already_classified += 1
                progress.advance()
            else:
                started = time.monotonic()
                _classify_one(entry, store, client, summary)
                done.add(entry.identifier)
                progress.advance(time.monotonic() - started)
    except BaseException as exc:
        summary.interrupted = isinstance(exc, KeyboardInterrupt)
        summary.stopped = not summary.interrupted
        exc.summary = summary  # type: ignore[attr-defined]
        raise
    finally:
        progress.finish()
        mbox.close()
    return summary


def _record_message(message: Message, store: Store) -> None:
    store.record_message(
        message.identifier, message.sender, message.subject, message.date, message.labels, message.has_pdf_attachment
    )


def _resolve_from_markup(message: Message, store: Store) -> bool:
    record = parse_markup(message.html_parts, message.calendar_texts)
    if record is None:
        return False
    _record_message(message, store)
    store.save_markup(message.identifier, record)
    return True


def _classify_one(message: Message, store: Store, client: DecisionClient, summary: ClassifySummary) -> None:
    _record_message(message, store)
    try:
        decision = client.decide(message)
    except DecisionError as exc:
        store.record_failure(message.identifier, PASS_NAME, str(exc))
        summary.failed += 1
        summary.failures.append((message.identifier, str(exc)))
        return
    store.save_classification(message.identifier, decision)
    summary.classified += 1


def format_summary(summary: ClassifySummary, config: Config, store: Store) -> str:
    """The printed summary: this run's counts, then what the store holds for
    the configured decision model (so a rerun still shows the results)."""
    if summary.interrupted:
        lines = ["Classify pass interrupted. Rerun to resume."]
    elif summary.stopped:
        lines = ["Classify pass stopped. Rerun to resume."]
    else:
        lines = ["Classify pass finished."]
    lines += [
        f"  messages in mailbox:  {summary.total}",
        f"  classified:           {summary.classified}",
        f"  already classified:   {summary.already_classified}",
        f"  resolved from machine-readable data: {summary.resolved_from_markup}",
        f"  already resolved from machine-readable data: {summary.already_resolved_from_markup}",
        f"  skipped:              {summary.skipped}",
        f"  failed:               {summary.failed}",
    ]
    lines += failure_lines(summary.failures)

    gates = store.gate_probabilities(config.decision_model)
    types = store.email_type_counts(config.decision_model)
    lines.append("")
    lines.append(f"Stored results for {config.decision_model} ({len(gates)} messages):")
    above = sum(1 for g in gates if g >= config.threshold)
    lines.append(f"  at or above threshold {config.threshold:g}: {above}")
    for band in GATE_BANDS:
        lines.append(f"  gate >= {band:g}: {sum(1 for g in gates if g >= band)}")
    for label in EMAIL_TYPES:
        if types.get(label):
            lines.append(f"  {label}: {types[label]}")
    return "\n".join(lines)
