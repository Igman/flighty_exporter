"""The audit: is the classify pass discarding real flight emails?

The decision model's gate probabilities are uncalibrated, so the audit
gathers evidence per gate-probability band (``BAND_EDGES``, lower edge
inclusive, upper exclusive, the last band including 1.0), including the bands
below ``[classify] threshold``:

- Population: every message classified by the configured decision model.
  Messages resolved from machine-readable data have no gate and are left out
  (their records are never touched); messages whose classification failed
  are in no band and only counted.
- Sampling: per band, up to ``sample_size`` messages. The band's identifiers
  are sorted and shuffled with a seed derived from ``seed`` and the band, so
  the sample depends on neither mbox nor SQLite order. Messages that already
  have an extraction from the configured extraction model come first (free),
  in shuffled order; unextracted ones fill the rest, in shuffled order. A
  failed message therefore stays at the front of the unextracted ones and is
  retried by the next audit with the same seed, and a rerun after a complete
  audit sends nothing.
- A second sample per band is drawn the same way from the messages with a PDF
  attachment (recorded at classify time from content type and filename),
  because PDF-only e-tickets are the known blind spot: the classifier never
  sees PDF text. Messages in the first sample count as free there too.
- Every sampled message without an extraction is sent to the extraction model
  exactly as the extract pass sends it, and the result is stored exactly as
  extract stores it (records, failures, attachment notes), so a later
  ``extract`` after lowering the threshold never repeats it. ``review`` still
  only shows messages at or above the threshold.

A sampled message is a hit when its extraction is valid and has at least one
segment. A failed extraction is counted per band, and is neither a hit nor a
miss. The estimate per band is the hit rate times the band size, rounded.

Stores written before PDF attachments were recorded have them as unknown;
the audit fills them in with one scan of the mailbox (no PDF is read) before
sampling. Rerunning classify does not.

The config is never changed: the report ends with a one-line reading (the
lowest band with a hit, and the threshold that would include it).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from ..config import Config
from ..flights.records import ExtractionRecord
from ..mail.reader import Mailbox, Message
from ..ollama.extraction import ExtractionClient
from ..progress import Progress
from ..store import Store
from ..tables import format_message_date, write_csv
from .extract import ExtractSummary, extract_messages

BAND_EDGES = (0.0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0)

SECTION_ALL = "all"
SECTION_PDF = "pdf"

AUDIT_COLUMNS = (
    "section",
    "band",
    "band_low",
    "band_high",
    "above_threshold",
    "messages",
    "sampled",
    "failed",
    "with_segments",
    "hit_rate",
    "estimated_flight_emails",
)

# How often the attachment scan commits what it has filled in.
COMMIT_EVERY = 500


@dataclass(frozen=True)
class Band:
    low: float
    high: float
    last: bool = False

    def contains(self, gate: float) -> bool:
        return self.low <= gate < self.high or (self.last and gate == self.high)

    @property
    def label(self) -> str:
        return f"[{self.low:g}, {self.high:g}{']' if self.last else ')'}"

    def position(self, threshold: float) -> str:
        """Whether the extract pass selects this band: yes, no or partly."""
        if self.low >= threshold:
            return "yes"
        if self.high < threshold or (self.high == threshold and not self.last):
            return "no"
        return "partly"


BANDS = tuple(
    Band(low, high, last=i == len(BAND_EDGES) - 2) for i, (low, high) in enumerate(zip(BAND_EDGES, BAND_EDGES[1:]))
)


def band_of(gate: float) -> Band | None:
    return next((band for band in BANDS if band.contains(gate)), None)


@dataclass
class BandResult:
    band: Band
    messages: int
    sampled: list[str]
    failed: int = 0
    hits: int = 0

    @property
    def rate(self) -> float | None:
        valid = len(self.sampled) - self.failed
        return self.hits / valid if valid else None

    @property
    def estimate(self) -> int | None:
        rate = self.rate
        return None if rate is None else math.floor(rate * self.messages + 0.5)


@dataclass
class Hit:
    gate: float
    message_id: str
    record: ExtractionRecord


@dataclass
class AuditResult:
    sample_size: int
    seed: int
    classified: int = 0
    classify_failed: int = 0
    markup: int = 0
    with_pdf: int = 0
    pdf_unknown: int = 0
    pdf_backfilled: int = 0
    sampled: int = 0
    reused: int = 0
    to_extract: int = 0
    extraction: ExtractSummary = field(default_factory=ExtractSummary)
    sections: dict[str, list[BandResult]] = field(default_factory=dict)
    hits_below: list[Hit] = field(default_factory=list)
    lowest_hit: Hit | None = None
    interrupted: bool = False


def run_audit(
    config: Config,
    mailbox_path: Path,
    store: Store,
    client: ExtractionClient,
    sample_size: int,
    seed: int,
    progress_stream: TextIO | None = None,
) -> AuditResult:
    """Run the audit. KeyboardInterrupt and OllamaUnreachable propagate after
    the result so far is attached to the exception as ``summary``."""
    result = AuditResult(sample_size, seed)
    try:
        _run(config, mailbox_path, store, client, result, progress_stream)
    except BaseException as exc:
        result.interrupted = isinstance(exc, KeyboardInterrupt)
        exc.summary = result  # type: ignore[attr-defined]
        raise
    return result


def _run(
    config: Config,
    mailbox_path: Path,
    store: Store,
    client: ExtractionClient,
    result: AuditResult,
    progress_stream: TextIO | None,
) -> None:
    result.classify_failed = store.classify_failure_count(config.decision_model)
    result.markup = len(store.markup_ids())
    population = store.audit_population(config.decision_model)
    result.classified = len(population)
    if not population:
        return
    unknown = {message_id for message_id, _, has_pdf in population if has_pdf is None}
    if unknown:
        result.pdf_backfilled = _record_pdf_attachments(mailbox_path, unknown, store, progress_stream)
        population = store.audit_population(config.decision_model)
    result.with_pdf = sum(1 for _, _, has_pdf in population if has_pdf)
    result.pdf_unknown = sum(1 for _, _, has_pdf in population if has_pdf is None)

    gates = {message_id: gate for message_id, gate, _ in population}
    extracted = store.extracted_ids(config.extraction_model)
    members: dict[Band, list[str]] = {band: [] for band in BANDS}
    pdf_members: dict[Band, list[str]] = {band: [] for band in BANDS}
    for message_id, gate, has_pdf in population:  # sorted by identifier
        band = band_of(gate)
        if band is None:
            continue
        members[band].append(message_id)
        if has_pdf:
            pdf_members[band].append(message_id)

    size, seed = result.sample_size, result.seed
    main = {band: _sample(members[band], extracted, size, f"{seed}:all:{band.low!r}") for band in BANDS}
    in_main = {message_id for sample in main.values() for message_id in sample}
    pdf = {
        band: _sample(pdf_members[band], extracted | in_main, size, f"{seed}:pdf:{band.low!r}") for band in BANDS
    }
    sampled = in_main | {message_id for sample in pdf.values() for message_id in sample}
    pending = {message_id for message_id in sampled if message_id not in extracted}
    result.sampled, result.reused, result.to_extract = len(sampled), len(sampled) - len(pending), len(pending)

    extract_messages(mailbox_path, pending, store, client, result.extraction, "audit", progress_stream)

    records = store.extraction_records(config.extraction_model)
    result.sections = {
        SECTION_ALL: [_band_result(band, len(members[band]), main[band], records) for band in BANDS],
        SECTION_PDF: [_band_result(band, len(pdf_members[band]), pdf[band], records) for band in BANDS],
    }
    hits = sorted(
        (Hit(gates[m], m, records[m]) for m in sampled if m in records and records[m].segments),
        key=lambda hit: (hit.gate, hit.message_id),
    )
    result.lowest_hit = hits[0] if hits else None
    result.hits_below = [hit for hit in hits if hit.gate < config.threshold]


def _sample(identifiers: list[str], free: set[str], size: int, salt: str) -> list[str]:
    """Up to ``size`` identifiers in a seeded order: the free ones first."""
    order = sorted(identifiers)
    random.Random(salt).shuffle(order)
    first = [m for m in order if m in free][:size]
    rest = [m for m in order if m not in free][: size - len(first)]
    return first + rest


def _band_result(band: Band, messages: int, sampled: list[str], records: dict[str, ExtractionRecord]) -> BandResult:
    result = BandResult(band, messages, sampled)
    for message_id in sampled:
        record = records.get(message_id)
        if record is None:
            result.failed += 1
        elif record.segments:
            result.hits += 1
    return result


def _record_pdf_attachments(
    mailbox_path: Path, unknown: set[str], store: Store, progress_stream: TextIO | None
) -> int:
    """Fill in the PDF-attachment flag of messages recorded before it was
    stored, from one scan of the mailbox (filenames and content types only)."""
    unknown = set(unknown)
    mbox = Mailbox(mailbox_path)
    progress = Progress(len(mbox), "audit: noting PDF attachments", progress_stream)
    filled = 0
    try:
        for entry in mbox:
            if isinstance(entry, Message) and entry.identifier in unknown:
                store.set_has_pdf(entry.identifier, entry.has_pdf_attachment)
                unknown.discard(entry.identifier)
                filled += 1
                if filled % COMMIT_EVERY == 0:
                    store.commit()
            progress.advance()
            if not unknown:
                break
    finally:
        store.commit()
        progress.finish()
        mbox.close()
    return filled


# -- report ------------------------------------------------------------------------


def format_report(result: AuditResult, config: Config, store: Store, output: Path) -> str:
    ex = result.extraction
    sent = ex.extracted + ex.failed
    lines = [
        "Audit finished.",
        f"  decision model:       {config.decision_model} (threshold {config.threshold:g})",
        f"  extraction model:     {config.extraction_model}",
        f"  sample size per band: {result.sample_size} (seed {result.seed})",
        f"  messages classified (in bands): {result.classified}",
        f"  failed classification (in no band): {result.classify_failed}",
        f"  resolved from machine-readable data (not audited): {result.markup}",
        f"  sampled messages:     {result.sampled}",
        f"  already extracted (reused): {result.reused}",
        f"  sent to the extraction model: {sent}",
        f"  extraction failures:  {ex.failed + ex.not_in_mailbox}",
        f"  retries:              {ex.retries}",
    ]
    if ex.not_in_mailbox:
        lines.append(f"  not in mailbox:       {ex.not_in_mailbox}")
    if ex.pdfs_not_read:
        lines.append(f"  PDFs not read:        {ex.pdfs_not_read}")
    for identifier, reason in ex.failures[:20]:
        lines.append(f"    failed {identifier}: {reason}")
    if len(ex.failures) > 20:
        lines.append(f"    ... and {len(ex.failures) - 20} more")
    if result.pdf_backfilled:
        lines.append(f"  PDF attachments noted from the mailbox for {result.pdf_backfilled} earlier-classified messages")

    lines += ["", f"All classified messages ({result.classified})"]
    lines += _table(result.sections[SECTION_ALL], config.threshold)
    lines += ["", f"Messages with a PDF attachment ({result.with_pdf} of {result.classified})"]
    if result.pdf_unknown:
        lines.append(
            f"  not known for {result.pdf_unknown} messages: classified before PDF attachments were recorded "
            "and not found in this mailbox, so they are left out of this section"
        )
    lines += _table(result.sections[SECTION_PDF], config.threshold)

    lines += ["", f"Sampled messages below the threshold that contained flight segments: {len(result.hits_below)}"]
    for hit in result.hits_below:
        sender, subject, raw_date = store.message_info(hit.message_id)
        date, _ = format_message_date(raw_date)
        lines.append(f"  gate {hit.gate:.4g}  {date}  {sender}  \"{subject}\"")
        for segment in hit.record.segments:
            lines.append(
                f"      {segment.date}  {segment.flight_number or '(no flight number)'}  "
                f"{segment.origin}-{segment.destination}"
            )

    lines += ["", _reading(result, config.threshold)]
    lines.append(
        "The config was not changed. After lowering [classify] threshold, run extract: messages the audit "
        "already extracted are not sent again."
    )
    lines.append(f"Audit table written to {output}")
    return "\n".join(lines)


_HEADER = ("band", "messages", "sampled", "failed", "with segments", "hit rate", "est. flight emails")
_WIDTHS = (16, 10, 9, 8, 15, 10, 20)


def _table(results: list[BandResult], threshold: float) -> list[str]:
    def row(cells: tuple[str, ...]) -> str:
        first, *rest = cells
        return "  " + first.ljust(_WIDTHS[0]) + "".join(c.rjust(w) for c, w in zip(rest, _WIDTHS[1:]))

    lines = [row(_HEADER)]
    ordered = list(reversed(results))  # highest gate first
    marker_done = False
    for i, band_result in enumerate(ordered):
        band = band_result.band
        if not marker_done and band.position(threshold) == "no":
            lines.append(f"  ----- threshold {threshold:g}: bands above are extracted, bands below are not -----")
            marker_done = True
        rate, estimate = band_result.rate, band_result.estimate
        lines.append(
            row(
                (
                    band.label,
                    str(band_result.messages),
                    str(len(band_result.sampled)),
                    str(band_result.failed),
                    str(band_result.hits),
                    "-" if rate is None else f"{rate * 100:.1f}%",
                    "-" if estimate is None else f"~{estimate}",
                )
            )
        )
        if band.position(threshold) == "partly":
            lines.append(
                f"  ----- threshold {threshold:g} falls inside band {band.label}: only its messages at or above "
                f"{threshold:g} are extracted -----"
            )
            marker_done = True
    if not marker_done:
        lines.append(f"  ----- threshold {threshold:g}: every band is extracted -----")
    return lines


def _reading(result: AuditResult, threshold: float) -> str:
    hit = result.lowest_hit
    if hit is None:
        return "Reading: no sampled message contained flight segments."
    band = band_of(hit.gate)
    assert band is not None
    if hit.gate >= threshold:
        return (
            f"Reading: no sampled message below the threshold {threshold:g} contained flight segments; the lowest "
            f"band with a hit is {band.label}, already at or above the threshold."
        )
    return (
        f"Reading: the lowest band with a hit is {band.label} (gate {hit.gate:.4g}); "
        f"[classify] threshold = {band.low:g} would include it (now {threshold:g})."
    )


def write_audit_csv(path: Path, result: AuditResult, threshold: float) -> None:
    rows = []
    for section in (SECTION_ALL, SECTION_PDF):
        for band_result in reversed(result.sections[section]):
            band, rate, estimate = band_result.band, band_result.rate, band_result.estimate
            rows.append(
                {
                    "section": section,
                    "band": band.label,
                    "band_low": f"{band.low:g}",
                    "band_high": f"{band.high:g}",
                    "above_threshold": band.position(threshold),
                    "messages": str(band_result.messages),
                    "sampled": str(len(band_result.sampled)),
                    "failed": str(band_result.failed),
                    "with_segments": str(band_result.hits),
                    "hit_rate": "" if rate is None else f"{rate:.3f}",
                    "estimated_flight_emails": "" if estimate is None else str(estimate),
                }
            )
    write_csv(path, AUDIT_COLUMNS, rows)


def format_interrupted(result: AuditResult) -> str:
    ex = result.extraction
    return "\n".join(
        [
            "Audit interrupted. Rerun with the same seed to resume.",
            f"  sampled messages:     {result.sampled}",
            f"  already extracted (reused): {result.reused}",
            f"  to send to the extraction model: {result.to_extract}",
            f"  extracted:            {ex.extracted}",
            f"  failed:               {ex.failed}",
        ]
    )


def no_population_message(result: AuditResult, config: Config) -> str:
    text = (
        f"No message has been classified by {config.decision_model} yet, so there is nothing to audit. "
        "Run classify first."
    )
    if result.markup:
        text += f" ({result.markup} messages resolved from machine-readable data are never audited.)"
    return text
