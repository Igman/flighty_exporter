"""The extract pass: every message the decision model scored at or above the
threshold, and that has no extraction yet for the configured extraction model,
is sent to the extraction model; the validated record is stored. Messages
resolved from their own machine-readable data at classify time are never
selected (see ``markup``).

A separate command from classify, so the two models are never needed at once.
Each selected message is read directly at the position classify recorded for
it, after checking that the message there still has its identifier; no other
message is read (messages sharing a Message-ID are treated as one: the first
readable one counts). When a position is missing (a store from before
positions were recorded) or no longer matches (the mailbox file changed), the
mailbox is scanned once, reading only headers, and the positions found are
recorded for every message the store holds, so the scan is not repeated.
Messages are extracted in mailbox order. The text layer of PDF attachments is read here (never at classify time). A PDF
that cannot be read is recorded as an attachment note for the failures file;
it does not make the message fail. The text of each message that yields a
record is saved with it (``Message.full_text``), for review.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from ..config import Config
from ..flights.records import EMAIL_KINDS
from ..mail.reader import Mailbox, Message
from ..ollama.extraction import ExtractionClient, ExtractionFailed
from ..progress import Progress
from ..store import EXTRACT_PASS, Store
from .summary import failure_lines

PASS_NAME = EXTRACT_PASS
NOT_IN_MAILBOX = "message is no longer in the mailbox (classified from a different mbox?)"


@dataclass
class ExtractSummary:
    selected: int = 0
    extracted: int = 0
    already_extracted: int = 0
    failed: int = 0
    retries: int = 0
    not_in_mailbox: int = 0
    inputs_truncated: int = 0
    pdfs_not_read: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    interrupted: bool = False


def run_extract(
    config: Config,
    mailbox_path: Path,
    store: Store,
    client: ExtractionClient,
    progress_stream: TextIO | None = None,
) -> ExtractSummary:
    """Run the pass. KeyboardInterrupt and OllamaUnreachable propagate after
    the summary so far is attached to the exception as ``summary``."""
    candidates = store.extraction_candidates(config.decision_model, config.threshold)
    done = store.extracted_ids(config.extraction_model)
    summary = ExtractSummary(selected=len(candidates))
    pending = {identifier for identifier in candidates if identifier not in done}
    summary.already_extracted = len(candidates) - len(pending)
    extract_messages(mailbox_path, pending, store, client, summary, PASS_NAME, progress_stream)
    return summary


def extract_messages(
    mailbox_path: Path,
    pending: set[str],
    store: Store,
    client: ExtractionClient,
    summary: ExtractSummary,
    label: str = PASS_NAME,
    progress_stream: TextIO | None = None,
) -> None:
    """Send the ``pending`` messages to the extraction model and store each
    result as the extract pass does (records, failures, attachment notes), so
    any command that extracts (extract, audit) leaves the same trace.
    Identifiers not found in the mailbox are recorded as failures.
    KeyboardInterrupt and OllamaUnreachable propagate with ``summary``
    attached as ``summary``."""
    pending = set(pending)
    if not pending:
        return
    mbox = Mailbox(mailbox_path, read_pdfs=True)
    progress = Progress(len(pending), label, progress_stream)
    try:
        positions = _locate(mbox, pending, store, label, progress_stream)
        for identifier, position in sorted(positions.items(), key=lambda item: item[1]):
            entry = mbox.entry_at(position)
            if not isinstance(entry, Message) or entry.identifier != identifier:
                continue
            pending.discard(identifier)
            started = time.monotonic()
            _extract_one(entry, store, client, summary)
            progress.advance(time.monotonic() - started)
        for identifier in sorted(pending):
            store.record_failure(identifier, PASS_NAME, NOT_IN_MAILBOX)
            summary.not_in_mailbox += 1
            summary.failures.append((identifier, NOT_IN_MAILBOX))
            progress.advance()
    except BaseException as exc:
        summary.interrupted = isinstance(exc, KeyboardInterrupt)
        exc.summary = summary  # type: ignore[attr-defined]
        raise
    finally:
        progress.finish()
        mbox.close()


def _locate(
    mbox: Mailbox, wanted: set[str], store: Store, label: str, progress_stream: TextIO | None
) -> dict[str, int]:
    """The position of each ``wanted`` message that is in the mailbox. A
    recorded position is used if the message there has the identifier;
    otherwise the mailbox's headers are scanned once and every position
    found is recorded."""
    recorded = store.message_positions(wanted)
    found = {
        identifier: position
        for identifier, position in recorded.items()
        if position is not None and mbox.identifier_at(position) == identifier
    }
    if len(found) == len(wanted):
        return found
    scanned: dict[str, int] = {}
    progress = Progress(len(mbox), f"{label}: locating messages", progress_stream)
    try:
        for position, identifier in mbox.identifiers():
            if identifier is not None:
                scanned.setdefault(identifier, position)
            progress.advance()
    finally:
        progress.finish()
    store.record_positions(scanned)
    found.update({identifier: scanned[identifier] for identifier in wanted - found.keys() if identifier in scanned})
    return found


def _extract_one(message: Message, store: Store, client: ExtractionClient, summary: ExtractSummary) -> None:
    store.replace_attachment_notes(message.identifier, message.attachment_notes)
    summary.pdfs_not_read += len(message.attachment_notes)
    try:
        extraction = client.extract(message)
    except ExtractionFailed as exc:
        store.record_failure(message.identifier, PASS_NAME, str(exc))
        summary.failed += 1
        summary.retries += exc.attempts - 1
        summary.failures.append((message.identifier, str(exc)))
        return
    store.save_extraction(message.identifier, extraction)
    store.save_message_text(message.identifier, message.full_text())
    summary.extracted += 1
    summary.retries += extraction.attempts - 1
    summary.inputs_truncated += extraction.input_truncated


def format_summary(summary: ExtractSummary, config: Config, store: Store) -> str:
    """The printed summary: this run's counts, then what the store holds for
    the configured extraction model among the selected messages."""
    lines = ["Extract pass interrupted. Rerun to resume." if summary.interrupted else "Extract pass finished."]
    lines += [
        f"  selected (gate >= {config.threshold:g}): {summary.selected}",
        f"  extracted:            {summary.extracted}",
        f"  already extracted:    {summary.already_extracted}",
        f"  retries:              {summary.retries}",
        f"  failed:               {summary.failed}",
        f"  not in mailbox:       {summary.not_in_mailbox}",
        f"  inputs truncated:     {summary.inputs_truncated}",
        f"  PDFs not read:        {summary.pdfs_not_read}",
    ]
    lines += failure_lines(summary.failures)
    if summary.selected == 0:
        lines.append(
            f"  No message has a gate probability at or above {config.threshold:g} for "
            f"{config.decision_model}. Run classify first, or lower [classify] threshold."
        )

    selected = set(store.extraction_candidates(config.decision_model, config.threshold))
    records = {k: v for k, v in store.extraction_records(config.extraction_model).items() if k in selected}
    lines.append("")
    lines.append(f"Stored results for {config.extraction_model} ({len(records)} messages):")
    lines.append(f"  segments: {sum(len(r.segments) for r in records.values())}")
    lines.append(f"  messages with no segments: {sum(1 for r in records.values() if not r.segments)}")
    for kind in EMAIL_KINDS:
        count = sum(1 for r in records.values() if r.email_kind == kind)
        if count:
            lines.append(f"  {kind}: {count}")
    return "\n".join(lines)
