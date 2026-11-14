"""Review table: write the review CSV and the failures file from the store.

The extraction records of every message selected for extraction (gate at or
above the threshold) that has a record from the configured extraction model,
and of every message resolved from its own machine-readable data (markup
records have no gate and are always included), are normalised and checked (see ``validate``) and consolidated (see
``consolidate``) into one row per flight, keeping only bookings for the
configured passenger names. Each row names its most recent source message and
lists every source message. Its source column is "markup" when every source
record came from markup, "model" when every one came from the extraction
model, and "markup; model" when both contributed.

needs_attention is "yes" when the row has a reason to be checked, and
attention_reason gives every reason, joined with "; ", blocking reasons first.
Keep is "no" for a row with a blocking reason (an unknown code, a malformed
flight number, the same origin and destination, an implausible date, or
confidence below ``[review] min_confidence``) and "yes" otherwise, including
rows with only advisory reasons (no flight number, a truncated email, no
passenger named). Codes are checked against the OpenFlights reference data
export uses (``[export] data_dir``, downloaded on first use); if it cannot be
loaded, the table is still written without the code checks and the summary
says why. The printed summary also lists the bookings dropped as being for
other passengers, so the user can check them without the table.

The saved review decisions (see ``flights.decisions``) are applied on top:
corrections before the checks, so reasons and pre-set keep are recomputed
from the corrected values, and a keep override after them. ``build_flight_list``
builds this flight list for review, export, sources and the terminal
application. The
last column, flight_id, names the flight as consolidation produced it, so an
edited row can be matched to its flight. Writing the table discards the
decisions whose flight is no longer produced and counts them in the summary.

The failures file lists selected messages with no valid extraction, and PDF
attachments of selected messages that the extract pass could not read
(image-only, too large or unreadable) with the message they came from.

The review table is the user's to edit, so review records a hash of the file
it wrote in the store and overwrites an existing table only if it is
unchanged since then (or with ``--force``).

Both files are UTF-8 with a byte-order mark so spreadsheets detect the encoding.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..flights.consolidate import (
    BOOKED_ONLY, CONFIRMED, NO_PASSENGER_NAMED, Consolidation, DroppedBooking, Flight, SourceRecord,
    consolidate,
)
from ..flights.decisions import ReviewedFlight, apply_decisions
from ..flights.records import MARKUP_SOURCE
from ..flights.validate import REASON_KINDS, UNKNOWN_AIRLINE, UNKNOWN_AIRPORT, normalise_record, reason_kind
from ..store import EXTRACT_PASS, Store
from ..tables import format_message_date, write_csv
from .openflights import OpenFlights, ReferenceDataError

REVIEW_COLUMNS = (
    "keep",
    "needs_attention",
    "attention_reason",
    "date",
    "flight_number",
    "airline",
    "origin",
    "destination",
    "booking_reference",
    "passengers",
    "confidence",
    "evidence",
    "email_kind",
    "source",
    "source_count",
    "message_id",
    "message_date",
    "message_subject",
    "source_messages",
    "flight_id",
)

FAILURE_COLUMNS = ("message_id", "message_date", "sender", "subject", "reason")

NOT_EXTRACTED = "not extracted yet (run extract)"

# Where a row's flight data came from.
SOURCE_MARKUP = "markup"
SOURCE_MODEL = "model"

CODES_NOT_CHECKED = (
    "airport and airline codes were not checked, so no row is flagged for an unknown code. "
    "The reference data could not be loaded:"
)

DECISIONS_DISCARDED = "decisions discarded because their flight is no longer produced"

# How many dropped bookings the printed summary lists before "and N more".
DROPPED_LISTED = 20


class ReviewExists(Exception):
    pass


@dataclass
class ReviewSummary:
    review_path: Path
    failures_path: Path
    flights: int
    confirmed: int
    booked_only: int
    bookings_cancelled: int
    bookings_replaced: int
    messages: int
    messages_without_segments: int
    failures: int
    unread_attachments: int
    passenger_filtering: bool = False
    needs_attention: int = 0
    not_kept: int = 0
    # Flights carrying each reason kind, and the codes named by unknown-code reasons.
    reasons: Counter[str] = field(default_factory=Counter)
    unknown_codes: dict[str, list[str]] = field(default_factory=dict)
    # Why the codes were not checked, None if they were (or there was nothing to check).
    codes_not_checked: str | None = None
    dropped_bookings: list[DroppedBooking] = field(default_factory=list)
    decisions_discarded: int = 0


@dataclass
class FlightList:
    """The flights for review, export and sources, with what consolidation left beside them."""

    flights: list[ReviewedFlight]  # decisions applied; in review table order
    consolidation: Consolidation
    sources: list[SourceRecord]
    failures: list[dict[str, str]]  # rows of the failures file
    unread_attachments: int
    codes_not_checked: str | None  # why the codes were not checked, None if they were
    stale_decisions: set[str]  # keys of saved decisions that name no flight


def write_review(config: Config, store: Store, review_path: Path, failures_path: Path, force: bool) -> ReviewSummary:
    if review_path.exists() and not force:
        recorded = store.review_table_hash(review_path)
        if recorded is None or recorded != file_hash(review_path):
            raise ReviewExists(
                f"review table {review_path} has been edited since review wrote it (or was not written by "
                "review with this store). Overwriting it would lose your edits; pass --force to overwrite "
                "it anyway, or --output to write elsewhere."
            )
    built = build_flight_list(config, store)
    discarded = discard_stale_decisions(store, built)
    write_csv(review_path, REVIEW_COLUMNS, [_row(reviewed) for reviewed in built.flights])
    store.record_review_table(review_path, file_hash(review_path))
    write_csv(failures_path, FAILURE_COLUMNS, built.failures)
    return review_summary(config, built, review_path, failures_path, discarded)


def write_review_without_table(config: Config, store: Store, review_path: Path, failures_path: Path) -> ReviewSummary:
    """For a review table at ``review_path`` that is left as it is (it was
    edited): what ``write_review`` does, except that no review table is
    written or recorded. The failures file is written, stale decisions are
    discarded, and the summary is the one ``write_review`` would return."""
    built = build_flight_list(config, store)
    discarded = discard_stale_decisions(store, built)
    write_csv(failures_path, FAILURE_COLUMNS, built.failures)
    return review_summary(config, built, review_path, failures_path, discarded)


def review_summary(
    config: Config, built: FlightList, review_path: Path, failures_path: Path, decisions_discarded: int = 0
) -> ReviewSummary:
    """The review summary of a flight list; writes nothing."""
    result, sources, failures = built.consolidation, built.sources, built.failures
    unread_attachments = built.unread_attachments
    flights = [reviewed.flight for reviewed in built.flights]

    reasons: Counter[str] = Counter()
    unknown_codes: dict[str, set[str]] = {}
    for flight in flights:
        reasons.update({reason_kind(reason) for reason in flight.attention})
        for reason in flight.attention:
            kind = reason_kind(reason)
            if kind in (UNKNOWN_AIRPORT, UNKNOWN_AIRLINE):
                unknown_codes.setdefault(kind, set()).add(reason[len(kind) + 1:])

    return ReviewSummary(
        review_path,
        failures_path,
        flights=len(flights),
        confirmed=sum(1 for f in flights if f.evidence == CONFIRMED),
        booked_only=sum(1 for f in flights if f.evidence == BOOKED_ONLY),
        bookings_cancelled=result.bookings_cancelled,
        bookings_replaced=result.bookings_replaced,
        messages=len(sources),
        messages_without_segments=sum(1 for s in sources if not s.record.segments),
        failures=len(failures) - unread_attachments,
        unread_attachments=unread_attachments,
        passenger_filtering=bool(config.passenger_names),
        needs_attention=sum(1 for f in flights if f.attention),
        not_kept=sum(1 for f in flights if f.blocked),
        reasons=reasons,
        unknown_codes={kind: sorted(codes) for kind, codes in unknown_codes.items()},
        codes_not_checked=built.codes_not_checked,
        dropped_bookings=result.dropped_bookings,
        decisions_discarded=decisions_discarded,
    )


def build_flight_list(config: Config, store: Store) -> FlightList:
    """The flight list from the store: consolidation, then the saved
    decisions' corrections, then the checks, then their keep overrides (see
    ``flights.decisions``). Reads only; ``discard_stale_decisions`` removes
    the decisions it found no flight for."""
    selected = store.extraction_candidates(config.decision_model, config.threshold)
    records = store.extraction_records(config.extraction_model)
    markup_records = store.extraction_records(MARKUP_SOURCE)
    selected += sorted(markup_records)
    records.update(markup_records)
    # A markup record has no classification (an earlier run's is ignored).
    email_types = {k: v for k, v in store.email_types(config.decision_model).items() if k not in markup_records}
    truncated = store.truncated_extraction_ids(config.extraction_model)

    sources: list[SourceRecord] = []
    failures: list[dict[str, str]] = []
    unread_attachments = 0
    for message_id in selected:
        sender, subject, raw_date = store.message_info(message_id)
        message_date, sent_at = format_message_date(raw_date)
        listed = {"message_id": message_id, "message_date": message_date, "sender": sender, "subject": subject}
        for reason in store.attachment_notes(message_id):
            unread_attachments += 1
            failures.append({**listed, "reason": reason})
        record = records.get(message_id)
        if record is None:
            reason = store.failure_reason(message_id, EXTRACT_PASS) or NOT_EXTRACTED
            failures.append({**listed, "reason": reason})
            continue
        sources.append(
            SourceRecord(
                message_id, sent_at, message_date, subject, email_types.get(message_id, ""),
                normalise_record(record), input_truncated=message_id in truncated,
            )
        )
    failures.sort(key=lambda r: (r["message_date"], r["message_id"]))

    result = consolidate(sources, config.passenger_names)
    reference, codes_not_checked = None, None
    if result.flights:
        try:
            reference = OpenFlights.load(config.export_data_dir, config.export_data_url)
        except ReferenceDataError as exc:
            codes_not_checked = str(exc)
    flights, stale = apply_decisions(
        result.flights, store.review_decisions(), reference, config.review_min_confidence
    )
    return FlightList(flights, result, sources, failures, unread_attachments, codes_not_checked, stale)


def discard_stale_decisions(store: Store, flight_list: FlightList) -> int:
    """Delete the saved decisions whose flight is no longer produced; how many."""
    return store.delete_decisions(flight_list.stale_decisions) if flight_list.stale_decisions else 0


def _row(reviewed: ReviewedFlight) -> dict[str, str]:
    flight = reviewed.flight
    segment, latest = flight.segment, flight.latest
    return {
        "keep": "yes" if reviewed.keep else "no",
        "needs_attention": "yes" if flight.attention else "",
        "attention_reason": "; ".join(flight.attention),
        "date": segment.date,
        "flight_number": segment.flight_number,
        "airline": segment.airline,
        "origin": segment.origin,
        "destination": segment.destination,
        "booking_reference": flight.booking_reference,
        "passengers": "; ".join(flight.passengers),
        "confidence": f"{flight.confidence:.2f}",
        "evidence": flight.evidence,
        "email_kind": latest.email_kind,
        "source": _source(flight),
        "source_count": str(len(flight.sources)),
        "message_id": latest.message_id,
        "message_date": latest.message_date,
        "message_subject": latest.subject,
        "source_messages": " | ".join(f"{s.message_date[:10]} {s.subject}" for s in flight.sources),
        "flight_id": reviewed.key,
    }


def _source(flight: Flight) -> str:
    kinds = {SOURCE_MARKUP if s.record.model == MARKUP_SOURCE else SOURCE_MODEL for s in flight.sources}
    return "; ".join(kind for kind in (SOURCE_MARKUP, SOURCE_MODEL) if kind in kinds)


def format_summary(summary: ReviewSummary) -> str:
    dropped = summary.dropped_bookings
    lines = [
        f"Review table written to {summary.review_path}",
        f"  flights (one row each):     {summary.flights}",
        f"  confirmed:                  {summary.confirmed}",
        f"  booked only:                {summary.booked_only}",
        f"  flights needing attention:  {summary.needs_attention}",
        f"  flights pre-set to not keep: {summary.not_kept}",
    ]
    if summary.reasons:
        lines.append("  flights per attention reason:")
    for kind in (*REASON_KINDS, NO_PASSENGER_NAMED):
        count = summary.reasons.get(kind, 0)
        if count:
            codes = summary.unknown_codes.get(kind)
            label = f"{kind} ({', '.join(codes)})" if codes else kind
            lines.append(f"    {label}: {count}")
    lines += [
        f"  bookings cancelled:         {summary.bookings_cancelled}",
        f"  bookings with replaced segments: {summary.bookings_replaced}",
        f"  bookings for other passengers (dropped): {len(dropped)}",
    ]
    for booking in dropped[:DROPPED_LISTED]:
        lines.append(f"    {_dropped_line(booking)}")
    if len(dropped) > DROPPED_LISTED:
        lines.append(f"    and {len(dropped) - DROPPED_LISTED} more")
    lines += [
        f"  messages extracted:         {summary.messages}",
        f"  messages with no segments:  {summary.messages_without_segments}",
        f"  {DECISIONS_DISCARDED}: {summary.decisions_discarded}",
        f"Failures file written to {summary.failures_path}",
        f"  failures:                   {summary.failures}",
        f"  PDF attachments not read:   {summary.unread_attachments}",
    ]
    if not summary.passenger_filtering:
        lines.append(
            "Passenger filtering is off: no names are configured, so bookings for other people are kept. "
            "To keep only your own flights, list your name variants in the config file, e.g.\n"
            "  [passenger]\n"
            '  names = ["Jordan Ellis", "Jordan A. Ellis"]'
        )
    return "\n".join(lines)


def format_warnings(summary: ReviewSummary) -> list[str]:
    if summary.codes_not_checked is None:
        return []
    return [f"warning: {CODES_NOT_CHECKED} {summary.codes_not_checked}"]


def _dropped_line(booking: DroppedBooking) -> str:
    flights = f"{booking.flights} flight{'' if booking.flights == 1 else 's'}"
    return (
        f"{booking.booking_reference or '(no booking reference)'}: {'; '.join(booking.passengers)} "
        f"({flights}, first {booking.first_date})"
    )


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

