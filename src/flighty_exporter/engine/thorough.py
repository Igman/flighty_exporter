"""What a Thorough run does after its audit: the automatic include.

The audit (``stages.audit``) reports, per gate band, how many sampled
messages contained flight segments. Its reading here: the lowest band below
the session's current threshold (``threshold.effective_threshold``) with a
hit. If there is none, nothing changes. Otherwise the suggested threshold is
that band's lower edge, and the cost of lowering to it is the number of
messages classified by the configured decision model with a gate at or above
the suggested threshold and below the current one that have no extraction
yet for the configured extraction model (messages resolved from
machine-readable data are never counted: they are never extracted).

- Within ``[audit] auto_include_budget`` (the count may equal it), the
  suggested value is recorded as the session's effective threshold and those
  messages are extracted exactly as extract would (``extract_messages``), so
  a stop leaves the rest pending for the next run or extract.
- Over the budget, nothing changes; the summary carries what was found and
  how many messages including it would add.

The config file is never written. The engine does this once per run; a
later Thorough run audits again against the lowered threshold.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from ..config import Config
from ..ollama.extraction import ExtractionClient
from ..progress import Progress
from ..stages.audit import SECTION_ALL, SECTION_PDF, AuditResult, band_of
from ..stages.extract import ExtractSummary, extract_messages
from ..stages.summary import failure_lines
from ..store import Store
from ..threshold import effective_threshold

# What the check for missed flight emails concluded.
NOTHING_BELOW = "nothing below"  # no sampled message below the threshold had flight segments
OVER_BUDGET = "over budget"  # some had, but including their band would add more messages than the budget
LOWERED = "lowered"  # the session's threshold was lowered and the newly included messages extracted

NOTHING_BELOW_TEXT = "no flight emails found below the threshold"
QUICK_SKIPPED_TEXT = "skipped (quick)"
PROGRESS_LABEL = "include"


@dataclass
class IncludeSummary:
    threshold: float  # the session's threshold the audit was read against
    budget: int
    outcome: str  # NOTHING_BELOW, OVER_BUDGET or LOWERED
    band: str | None = None  # the lowest band below the threshold with a hit
    suggested: float | None = None  # that band's lower edge
    would_add: int = 0  # messages in [suggested, threshold) with no extraction yet
    already_extracted: int = 0  # messages in that range already extracted (by the audit or earlier)
    extraction: ExtractSummary = field(default_factory=ExtractSummary)
    # Once included: messages in that range with a record, and with flight segments.
    with_records: int = 0
    with_flights: int = 0
    messages: list[str] = field(default_factory=list, repr=False)  # the range's messages
    pending: list[str] = field(default_factory=list, repr=False)  # the ones to extract


def plan_include(config: Config, store: Store, audit: AuditResult) -> IncludeSummary:
    """Read the audit and count what including its lowest hit band would cost.
    Changes nothing."""
    threshold = effective_threshold(config, store)
    budget = config.audit_auto_include_budget
    hits = [hit for hit in audit.hits_below if hit.gate < threshold]
    if not hits:
        return IncludeSummary(threshold, budget, NOTHING_BELOW)
    band = band_of(min(hit.gate for hit in hits))
    assert band is not None
    suggested = band.low
    current = set(store.extraction_candidates(config.decision_model, threshold))
    messages = [m for m in store.extraction_candidates(config.decision_model, suggested) if m not in current]
    extracted = store.extracted_ids(config.extraction_model)
    pending = [m for m in messages if m not in extracted]
    return IncludeSummary(
        threshold,
        budget,
        LOWERED if len(pending) <= budget else OVER_BUDGET,
        band=band.label,
        suggested=suggested,
        would_add=len(pending),
        already_extracted=len(messages) - len(pending),
        messages=messages,
        pending=pending,
    )


def include_messages(
    config: Config,
    mailbox_path: Path,
    store: Store,
    client: ExtractionClient,
    summary: IncludeSummary,
    progress_stream: TextIO | None = None,
    on_progress: Callable[[Progress], None] | None = None,
) -> None:
    """Extract the planned messages and count what the range now holds.
    Whatever stops the extraction propagates with ``summary`` attached."""
    try:
        extract_messages(
            mailbox_path, set(summary.pending), store, client, summary.extraction, PROGRESS_LABEL,
            progress_stream, on_progress,
        )
    except BaseException as exc:
        exc.summary = summary  # type: ignore[attr-defined]
        raise
    records = store.extraction_records(config.extraction_model)
    found = [records[m] for m in summary.messages if m in records]
    summary.with_records = len(found)
    summary.with_flights = sum(1 for record in found if record.segments)


# -- text -----------------------------------------------------------------------------


def outcome_text(summary: IncludeSummary) -> str:
    if summary.outcome == NOTHING_BELOW:
        return NOTHING_BELOW_TEXT
    if summary.outcome == OVER_BUDGET:
        return (
            f"found flight emails below the threshold in band {summary.band}, but including them would add "
            f"{summary.would_add} emails, over the budget of {summary.budget}; to include them, set "
            f"[classify] threshold = {summary.suggested:g} or raise [audit] auto_include_budget to at least "
            f"{summary.would_add}, then run again"
        )
    ex = summary.extraction
    read_now = ex.extracted or "none"
    how = [f"{summary.already_extracted} already read by the check", f"{read_now} read now"]
    failed = ex.failed + ex.not_in_mailbox
    if failed:
        how.append(f"{failed} could not be read")
    included = len(summary.messages)
    return (
        f"threshold lowered from {summary.threshold:g} to {summary.suggested:g} for this session; "
        f"{included} email{'' if included == 1 else 's'} newly included by the lower threshold ({', '.join(how)}), "
        f"{summary.with_flights} of them with flights"
    )


def format_include(summary: IncludeSummary, stopped: bool) -> str:
    """What ``run`` prints when the include stage ends."""
    if summary.outcome != LOWERED:
        return f"Include skipped: {outcome_text(summary)}."
    ex = summary.extraction
    if stopped:
        lines = [
            f"Include stopped. The threshold stays lowered from {summary.threshold:g} to {summary.suggested:g} "
            "for this session; rerun run or extract to extract the remaining messages."
        ]
    else:
        lines = [f"Include finished: {outcome_text(summary)}."]
    lines += [
        f"  newly included (gate >= {summary.suggested:g}, below {summary.threshold:g}): {len(summary.messages)}",
        f"  already extracted:    {summary.already_extracted}",
        f"  extracted:            {ex.extracted}",
        f"  failed:               {ex.failed + ex.not_in_mailbox}",
    ]
    lines += failure_lines(ex.failures)
    return "\n".join(lines)


def check_lines(audit: AuditResult | None, include: IncludeSummary | None) -> list[str]:
    """The end-of-run report's lines about the check for missed flight emails
    (Quick: a single line saying it was skipped)."""
    head = "  check for missed flight emails: "
    if audit is None or include is None:
        return [head + QUICK_SKIPPED_TEXT]
    lines = [head + f"ran (sample of up to {audit.sample_size} per band, seed {audit.seed})"]
    for section, title in ((SECTION_ALL, "sampled below the threshold"), (SECTION_PDF, "with a PDF attachment")):
        cells = [
            f"{result.band.label} {result.hits} of {len(result.sampled)}"
            for result in reversed(audit.sections.get(section, []))
            if result.band.low < include.threshold and result.sampled
        ]
        if section == SECTION_ALL:
            lines.append(f"    {title} {include.threshold:g}, with flights: {', '.join(cells) or 'none'}")
        elif cells:
            lines.append(f"    {title}, with flights: {', '.join(cells)}")
    lines.append(f"    {outcome_text(include)}")
    return lines
