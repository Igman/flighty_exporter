"""Sessions: one store plus the files the tool generated from it.

A session exists when its store file exists. The store records, as session
values (see ``Store.session_value``):

- what the session was started with: the resolved mailbox path, the
  passenger name variants, thoroughness, the two model names and the
  threshold, recorded by the first command that brings a mailbox to the
  store (``begin``) and never overwritten;
- when the store was created by this version (``started_at``): only then is
  every file the session wrote known;
- when the session was last active (``touch``), updated by every command
  that writes results.

It also records every file the tool writes for the session
(``record_written``: review table, failures file, audit report, import
files, manual-add list), by resolved absolute path.

``describe`` reads a session for a startup prompt or a start-fresh
confirmation; ``start_fresh`` deletes the recorded files that still exist,
and only those, then the store and its side files. Neither prints or asks
anything. A store from before sessions were recorded has no started-with
values and no record of its files: it is described from what it holds, with
"unknown" for the rest, and starting fresh deletes just the store.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..output.export import ExportError, refresh_manual_add
from ..output.review import build_flight_list
from ..store import EXTRACT_PASS, Store
from ..threshold import effective_threshold

# Session values.
STARTED_MAILBOX = "started_with_mailbox"
STARTED_NAMES = "started_with_names"  # JSON list
STARTED_THOROUGHNESS = "started_with_thoroughness"
STARTED_DECISION_MODEL = "started_with_decision_model"
STARTED_EXTRACTION_MODEL = "started_with_extraction_model"
STARTED_THRESHOLD = "started_with_threshold"  # JSON number
STARTED_AT = "started_at"  # set only when this version created the store
LAST_ACTIVE = "last_active"

# What each written file is.
REVIEW_TABLE = "review table"
FAILURES_FILE = "failures file"
AUDIT_REPORT = "audit report"
IMPORT_FILE = "import file"
MANUAL_ADD_LIST = "manual-add list"

STORE_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")

FILES_NOT_RECORDED = (
    "this session's store was created before the tool recorded the files it writes, "
    "so files it generated earlier are not known and were not deleted"
)


@dataclass(frozen=True)
class WrittenFile:
    kind: str  # REVIEW_TABLE, FAILURES_FILE, AUDIT_REPORT, IMPORT_FILE or MANUAL_ADD_LIST
    path: Path  # resolved, absolute
    written_at: str  # ISO 8601, UTC


@dataclass(frozen=True)
class SessionDescription:
    store_path: Path
    # What the session was started with; None where not recorded (an older store).
    mailbox: Path | None
    mailbox_exists: bool | None  # None when the mailbox is not known
    passenger_names: tuple[str, ...] | None
    thoroughness: str | None
    decision_model: str | None
    extraction_model: str | None
    threshold: float | None
    started_at: str | None  # ISO 8601 UTC; None for a store created before sessions were recorded
    last_active: str | None  # ISO 8601 UTC; None if never recorded
    # Progress, derived from the store with the configured models and the
    # threshold in use (``threshold.effective_threshold``: the configured one,
    # or the session's effective one if lower).
    threshold_used: float
    threshold_configured: float  # the config's [classify] threshold now
    messages: int  # messages recorded
    classified: int  # by the configured decision model
    resolved_from_markup: int  # read from the message's own machine-readable data
    candidates: int  # classified at or above the threshold in use
    extracted: int  # candidates with a record from the configured extraction model
    flights: int  # in the current flight list
    flights_with_decision: int  # of those, with a saved review decision
    complete: bool  # something classified or resolved, and no candidate left without an extraction
    files: tuple[WrittenFile, ...]  # recorded as written and still present
    files_known: bool  # False for an older store: files written before recording began are unknown
    would_lose: tuple[str, ...]  # plain-language lines: what starting fresh would lose
    # Candidates without an extraction whose last extraction attempt failed
    # (retried on every run).
    extraction_failed: int = 0

    @property
    def still_to_extract(self) -> int:
        return self.candidates - self.extracted

    @property
    def finished(self) -> bool:
        """The run reached its end: something was classified, every candidate
        was extracted or failed to be, and the review table the run writes
        last is recorded and still there. Without the review table this
        cannot tell a run stopped early in classify (no candidates yet) or
        during a Thorough audit from a finished one, since the store does not
        know the mailbox's size; resuming a finished session opens its review
        without running the engine, so it must be sure."""
        handled = self.classified + self.resolved_from_markup > 0 and self.still_to_extract == self.extraction_failed
        return handled and any(f.kind == REVIEW_TABLE for f in self.files)


@dataclass
class FreshResult:
    store_path: Path
    store_existed: bool
    preview: bool = False  # nothing was deleted: this is what would be
    deleted: list[WrittenFile] = field(default_factory=list)  # recorded files deleted
    already_gone: list[WrittenFile] = field(default_factory=list)  # recorded, but no longer there
    refused: list[WrittenFile] = field(default_factory=list)  # recorded path is not a regular file: left alone
    store_files: list[Path] = field(default_factory=list)  # the store and side files deleted
    files_known: bool = True  # False: an older store, whose earlier files were not recorded


# -- recording -------------------------------------------------------------------------


def begin(store: Store, config: Config, mailbox_path: Path, thoroughness: str) -> None:
    """Called by every command that brings a mailbox to the store (classify,
    extract, audit, run): records what the session was started with where
    nothing is recorded yet, and the last activity."""
    started = {
        STARTED_MAILBOX: str(mailbox_path.resolve()),
        STARTED_NAMES: json.dumps(list(config.passenger_names)),
        STARTED_THOROUGHNESS: thoroughness,
        STARTED_DECISION_MODEL: config.decision_model,
        STARTED_EXTRACTION_MODEL: config.extraction_model,
        STARTED_THRESHOLD: json.dumps(config.threshold),
    }
    if store.is_new:
        started[STARTED_AT] = _now()
    for key, value in started.items():
        if store.session_value(key) is None:
            store.set_session_value(key, value)
    touch(store)


def touch(store: Store) -> None:
    """Record that a command wrote results to the session now."""
    store.set_session_value(LAST_ACTIVE, _now())


def record_written(store: Store, kind: str, path: Path) -> None:
    """Record that the tool wrote ``path`` (a ``kind`` of file) for the session."""
    store.record_written_file(path, kind)


def record_export(store: Store, import_path: Path | None, manual_add_path: Path | None) -> None:
    """Record the files an export wrote (each import file is kept on record,
    so starting fresh deletes every one) and the activity."""
    if import_path is not None:
        record_written(store, IMPORT_FILE, import_path)
    if manual_add_path is not None:
        record_written(store, MANUAL_ADD_LIST, manual_add_path)
    touch(store)


def written(store: Store, kind: str) -> list[Path]:
    """The files of ``kind`` recorded as written for the session that still exist."""
    return [path for recorded, path, _ in store.written_files() if recorded == kind and path.is_file()]


def tick_manual_add(store: Store, flight_key: str, added: bool) -> tuple[list[Path], list[str]]:
    """Tick (or untick) a flight on the manual-add list, then bring every
    manual-add list an export wrote for the session in step with the ticks
    (``export.refresh_manual_add``; the import files are not touched).
    Returns (lists refreshed, problems with lists that could not be read)."""
    store.set_added(flight_key, added)
    touch(store)
    refreshed, problems = [], []
    for path in written(store, MANUAL_ADD_LIST):
        try:
            refresh_manual_add(store, path)
        except ExportError as exc:
            problems.append(str(exc))
        else:
            refreshed.append(path)
    return refreshed, problems


# -- describing ------------------------------------------------------------------------


def describe(config: Config, store_path: Path) -> SessionDescription | None:
    """The session in ``store_path``, None if there is none. Reads only
    (opening an older store upgrades its tables, as every command does), and
    never downloads the reference data."""
    if not store_path.is_file():
        return None
    with Store(store_path) as store:
        value = store.session_value
        mailbox = Path(value(STARTED_MAILBOX)) if value(STARTED_MAILBOX) else None
        names = value(STARTED_NAMES)
        threshold = value(STARTED_THRESHOLD)
        threshold_used = effective_threshold(config, store)
        candidates = store.extraction_candidates(config.decision_model, threshold_used)
        extracted_ids = store.extracted_ids(config.extraction_model)
        extracted = len(set(candidates) & extracted_ids)
        extraction_failed = sum(
            1 for mid in set(candidates) - extracted_ids if store.failure_reason(mid, EXTRACT_PASS) is not None
        )
        classified = len(store.classified_ids(config.decision_model))
        markup = len(store.markup_ids())
        # An empty download address: the cached reference data is used if
        # present, and nothing is fetched (the code checks do not change the
        # flight list).
        flight_list = build_flight_list(replace(config, export_data_url=""), store)
        files = tuple(WrittenFile(kind, path, at) for kind, path, at in store.written_files() if path.exists())
        counts = store.row_counts()
        files_known = value(STARTED_AT) is not None
        return SessionDescription(
            store_path=store_path,
            mailbox=mailbox,
            mailbox_exists=None if mailbox is None else mailbox.is_file(),
            passenger_names=None if names is None else tuple(json.loads(names)),
            thoroughness=value(STARTED_THOROUGHNESS),
            decision_model=value(STARTED_DECISION_MODEL),
            extraction_model=value(STARTED_EXTRACTION_MODEL),
            threshold=None if threshold is None else float(json.loads(threshold)),
            started_at=value(STARTED_AT),
            last_active=value(LAST_ACTIVE),
            threshold_used=threshold_used,
            threshold_configured=config.threshold,
            messages=counts["messages"],
            classified=classified,
            resolved_from_markup=markup,
            candidates=len(candidates),
            extracted=extracted,
            extraction_failed=extraction_failed,
            flights=len(flight_list.flights),
            flights_with_decision=sum(1 for reviewed in flight_list.flights if reviewed.decision is not None),
            complete=(classified + markup) > 0 and extracted == len(candidates),
            files=files,
            files_known=files_known,
            would_lose=_would_lose(counts, len(files)),
        )


def _would_lose(counts: dict[str, int], files: int) -> tuple[str, ...]:
    lines = []
    if counts["classifications"]:
        lines.append(f"classification of {_emails(counts['classifications'])}")
    if counts["markup"]:
        lines.append(f"flight data read from machine-readable data in {_emails(counts['markup'])}")
    if counts["model_extractions"]:
        lines.append(f"extraction of {_emails(counts['model_extractions'])}")
    if counts["review_decisions"]:
        lines.append(_plural(counts["review_decisions"], "review decision"))
    if files:
        lines.append(_plural(files, "generated file"))
    return tuple(lines)


# -- starting fresh --------------------------------------------------------------------


def start_fresh(store_path: Path, *, preview: bool = False) -> FreshResult:
    """Delete the session in ``store_path``: every file recorded as written
    for it that still exists (and only those; a recorded path that is not a
    regular file, or that is the session's mailbox, is left alone), then the
    store and its side files. With ``preview``, delete nothing and report
    what would be deleted. With no store, does nothing."""
    result = FreshResult(store_path, store_existed=store_path.is_file(), preview=preview)
    if not result.store_existed:
        return result
    store_files = [store_path] + [Path(f"{store_path}{suffix}") for suffix in STORE_SIDE_SUFFIXES]
    # Noted before the store is opened: SQLite may tidy side files away itself.
    present = {path for path in store_files if path.is_file()}
    with Store(store_path) as store:
        recorded = [WrittenFile(kind, path, at) for kind, path, at in store.written_files()]
        result.files_known = store.session_value(STARTED_AT) is not None
        mailbox = store.session_value(STARTED_MAILBOX)
    protected = {Path(mailbox)} if mailbox else set()
    for written in recorded:
        try:
            mode = os.lstat(written.path).st_mode
        except FileNotFoundError:
            result.already_gone.append(written)
            continue
        if not stat.S_ISREG(mode) or written.path in protected or written.path == store_path.resolve():
            result.refused.append(written)
            continue
        if not preview:
            written.path.unlink()
        result.deleted.append(written)
    for path in store_files:
        if path in present or path.is_file():
            if not preview:
                path.unlink(missing_ok=True)
            result.store_files.append(path)
    return result


# -- text ------------------------------------------------------------------------------


def format_description(description: SessionDescription) -> str:
    d = description

    def known(item: object) -> str:
        return "unknown" if item is None else str(item)

    if d.mailbox is None:
        mailbox = "unknown"
    else:
        mailbox = str(d.mailbox) if d.mailbox_exists else f"{d.mailbox} (no longer exists)"
    if d.passenger_names is None:
        names = "unknown"
    else:
        names = ", ".join(d.passenger_names) or "none (no passenger filtering)"
    if d.complete:
        state = "complete"
    elif d.classified + d.resolved_from_markup == 0:
        state = "unfinished: nothing classified yet"
    else:
        state = f"unfinished: {_emails(d.still_to_extract)} still to extract"
    lines = [
        f"Session in {d.store_path}",
        f"  mailbox:                    {mailbox}",
        "  started with:",
        f"    passenger names:          {names}",
        f"    thoroughness:             {known(d.thoroughness)}",
        f"    decision model:           {known(d.decision_model)}",
        f"    extraction model:         {known(d.extraction_model)}",
        f"    threshold:                {known(d.threshold)}",
        f"  started:                    {_time(d.started_at)}",
        f"  last active:                {_time(d.last_active)}",
        "Progress:",
        f"  messages recorded:          {d.messages}",
        f"  classified:                 {d.classified}",
        f"  resolved from machine-readable data: {d.resolved_from_markup}",
        f"  at or above the threshold:  {d.candidates}",
        f"  extracted:                  {d.extracted} of {d.candidates}",
        f"  extraction failed:          {d.extraction_failed}",
        f"  flights:                    {d.flights}",
        f"  flights with a saved decision: {d.flights_with_decision}",
        f"  state:                      {state}",
        f"  run finished:               "
        f"{'yes (resuming opens the review)' if d.finished else 'no (resuming runs the remaining stages)'}",
    ]
    in_use = f"{d.threshold_used:g}"
    if d.threshold_used < d.threshold_configured:
        in_use += f" (lowered for this session from the configured {d.threshold_configured:g})"
    lines.append(f"  threshold in use:           {in_use}")
    lines.append("Files written:")
    lines += [f"  {f.kind}: {f.path}" for f in d.files] or ["  none"]
    if not d.files_known:
        lines.append("  (files written before this store recorded them are not known)")
    lines.append("Starting fresh would lose:")
    lines += [f"  {line}" for line in d.would_lose] or ["  nothing (the store holds no results yet)"]
    return "\n".join(lines)


KEPT_WHEN_FRESH = (
    "Starting fresh never deletes the mailbox, the config file, downloaded models or the cached "
    "reference data, nor any file the tool did not record writing."
)


def format_fresh(result: FreshResult) -> list[str]:
    """One line per file deleted (or, for a preview, that would be), the
    store, and anything left alone."""
    deleted = "Would delete" if result.preview else "Deleted"
    lines = [f"{deleted} {f.kind}: {f.path}" for f in result.deleted]
    lines += [f"Already gone: {f.kind} {f.path}" for f in result.already_gone]
    lines += [f"Left alone ({f.kind} is not a regular file the tool wrote): {f.path}" for f in result.refused]
    if result.store_files:
        sides = [p.name for p in result.store_files[1:]]
        side_text = f" (with {', '.join(sides)})" if sides else ""
        lines.append(f"{deleted} the store: {result.store_files[0]}{side_text}")
    if not result.files_known:
        lines.append(f"Note: {FILES_NOT_RECORDED}.")
    return lines


def _time(value: str | None) -> str:
    if value is None:
        return "unknown"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return value


def _emails(count: int) -> str:
    return _plural(count, "email")


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
