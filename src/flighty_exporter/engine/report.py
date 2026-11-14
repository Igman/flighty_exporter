"""The end-of-run report: what a completed run found, as data, and the text
the ``run`` command prints for it.

Everything comes from the stage summaries the run produced (nothing is
recomputed): the flight counts from review's summary, the failed messages
from each pass's summary. Failed messages are retried on every run, so this
run's counts are the messages still failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..flights.consolidate import NO_PASSENGER_NAMED
from ..flights.validate import REASON_KINDS
from ..output.review import ReviewSummary
from ..stages.classify import ClassifySummary
from ..stages.extract import ExtractSummary

REVIEW_TABLE_LEFT = "review table was edited and has been left as it is; pass --force to regenerate it"


@dataclass(frozen=True)
class RunReport:
    thoroughness: str
    classify: ClassifySummary
    extract: ExtractSummary
    review: ReviewSummary
    # The review table existed and had been edited, so it was not rewritten;
    # ``review`` still holds the flights as the store has them.
    review_table_left: bool = False

    @property
    def files_written(self) -> list[tuple[str, Path]]:
        """(what, where) for every file this run wrote."""
        written = [] if self.review_table_left else [("review table", self.review.review_path)]
        return written + [("failures file", self.review.failures_path)]


def format_report(report: RunReport) -> str:
    review = report.review
    lines = [
        "Run finished.",
        f"  flights found:              {review.flights}",
        f"    confirmed:                {review.confirmed}",
        f"    booked only:              {review.booked_only}",
        f"  flights needing attention:  {review.needs_attention}",
    ]
    for kind in (*REASON_KINDS, NO_PASSENGER_NAMED):
        count = review.reasons.get(kind, 0)
        if count:
            lines.append(f"    {kind}: {count}")
    lines += [
        f"  flights pre-set to not keep: {review.not_kept}",
        f"  bookings cancelled:         {review.bookings_cancelled}",
        f"  bookings with replaced segments: {review.bookings_replaced}",
        f"  bookings for other passengers (dropped): {len(review.dropped_bookings)}",
        "  messages that failed:",
        f"    classify:                 {report.classify.failed}",
        f"    extract:                  {report.extract.failed + report.extract.not_in_mailbox}",
        f"  PDF attachments not read:   {review.unread_attachments}",
        "Files written:",
    ]
    lines += [f"  {what}: {where}" for what, where in report.files_written]
    if report.review_table_left:
        lines.append(f"Note: {REVIEW_TABLE_LEFT} ({review.review_path}).")
    lines.append(f"Next step: check {review.review_path}, then run export.")
    return "\n".join(lines)
