"""Store: a SQLite database keyed on message identifier.

Holds what each pass produced for each message and which model produced it,
so that every pass is resumable and results are invalidated when the model
changes. Each result is committed as soon as it is known, so an interruption
loses at most the message in flight.

Tables (extended by later passes):

- ``messages``: identifier, sender, subject, date, labels, and whether the
  message has a PDF attachment (``has_pdf``: 1/0, NULL if unknown because the
  message was recorded before this was stored). Enough to list a message in
  the failures file, and to audit it, without rereading the mbox. Also the
  message's position in the mailbox file (``mbox_offset``: the byte offset of
  its separator line, NULL if not known), so extract and audit can read it
  without reading the others; it is checked against the identifier before use.
- ``classifications``: the classify pass result per message.
- ``extractions``: the validated extraction record per message, and whether
  its input was cut at the extraction character budget (``input_truncated``).
  A record parsed from the message's own markup is stored here with model
  ``markup`` (see ``markup``); it replaces any model record for the message
  and takes the message out of the extract pass for good.
- ``attachment_notes``: PDF attachments the extract pass could not read
  (image-only, too large, unreadable), replaced each time the message is
  extracted. Listed in the failures file; not an extraction failure.
- ``failures``: the latest failure per message and pass.
- ``review_tables``: a hash of each review table as review last wrote it, so
  that an edited table is not overwritten.
- ``review_decisions``: the user's review decisions per flight (see
  ``flights.decisions``), keyed by the flight's identity: a keep override
  (1/0, NULL for none), corrections to date, flight number, origin and
  destination (NULL for none), and the manual-add tick (``added``). A row
  with nothing left in it is deleted.
- ``message_texts``: the text a reviewer reads for each message that yielded
  an extraction record (from the model or from markup): sender, subject,
  date, cleaned body and readable PDF text, uncut. Replaced whenever the
  message is extracted or resolved again; never saved for other messages.
- ``session_values``: small named values for the session as a whole.
- ``written_files``: every file the tool has written for this session
  (review table, failures file, audit report, import files, manual-add
  list), by resolved path, with what it is and when it was last written.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .flights.decisions import ReviewDecision
from .flights.records import MARKUP_SOURCE, Decision, Extraction, ExtractionRecord

SCHEMA_VERSION = 7

# The passes that record failures, as named in the ``failures`` table.
CLASSIFY_PASS = "classify"
EXTRACT_PASS = "extract"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '',
    has_pdf INTEGER,
    mbox_offset INTEGER
);
CREATE TABLE IF NOT EXISTS classifications (
    message_id TEXT PRIMARY KEY REFERENCES messages(message_id),
    gate_probability REAL NOT NULL,
    email_type TEXT NOT NULL,
    type_probabilities TEXT NOT NULL,
    type_confidence REAL,
    model TEXT NOT NULL,
    classified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extractions (
    message_id TEXT PRIMARY KEY REFERENCES messages(message_id),
    record TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    model TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    input_truncated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attachment_notes (
    message_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (message_id, position)
);
CREATE TABLE IF NOT EXISTS failures (
    message_id TEXT NOT NULL,
    pass TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (message_id, pass)
);
CREATE TABLE IF NOT EXISTS review_tables (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    written_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_decisions (
    flight_key TEXT PRIMARY KEY,
    keep INTEGER,
    date TEXT,
    flight_number TEXT,
    origin TEXT,
    destination TEXT,
    added INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_texts (
    message_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_values (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS written_files (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    written_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # True when this opening created the store file (a new session).
        self.is_new = not path.exists()
        self._held = False
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()

    def _migrate(self) -> None:
        """Bring a store written by an earlier version up to date.

        The version number is not consulted: two lines of development each
        numbered their change 5 (``review_decisions`` on one, ``mbox_offset``
        then ``message_texts`` as 6 on the other), so a store's number does
        not say which it has. Missing tables are created by the schema
        script (``CREATE TABLE IF NOT EXISTS``) and missing columns are
        detected here, so a store at any earlier version is upgraded."""
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(extractions)")}
        if "input_truncated" not in columns:
            self._db.execute("ALTER TABLE extractions ADD COLUMN input_truncated INTEGER NOT NULL DEFAULT 0")
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(messages)")}
        if "has_pdf" not in columns:
            # Existing rows: unknown (NULL) until the audit's mailbox scan fills them in.
            self._db.execute("ALTER TABLE messages ADD COLUMN has_pdf INTEGER")
        if "mbox_offset" not in columns:
            # Existing rows: unknown (NULL) until extract or audit finds them by scanning.
            self._db.execute("ALTER TABLE messages ADD COLUMN mbox_offset INTEGER")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- messages -------------------------------------------------------------

    def record_message(
        self,
        message_id: str,
        sender: str,
        subject: str,
        date: str,
        labels: tuple[str, ...],
        has_pdf: bool | None = None,
        position: int | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO messages (message_id, sender, subject, date, labels, has_pdf, mbox_offset) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET sender=excluded.sender, subject=excluded.subject, "
            "date=excluded.date, labels=excluded.labels, has_pdf=COALESCE(excluded.has_pdf, messages.has_pdf), "
            "mbox_offset=COALESCE(excluded.mbox_offset, messages.mbox_offset)",
            (
                message_id, sender, subject, date, ",".join(labels), None if has_pdf is None else int(has_pdf),
                position,
            ),
        )

    def set_has_pdf(self, message_id: str, has_pdf: bool) -> None:
        """Fill in whether a message has a PDF attachment (not committed:
        call ``commit``)."""
        self._db.execute("UPDATE messages SET has_pdf = ? WHERE message_id = ?", (int(has_pdf), message_id))

    def message_positions(self, message_ids: set[str]) -> dict[str, int | None]:
        """The recorded mailbox position of each message (None if unknown)."""
        rows = self._db.execute("SELECT message_id, mbox_offset FROM messages")
        return {message_id: offset for message_id, offset in rows if message_id in message_ids}

    def record_positions(self, positions: dict[str, int]) -> None:
        """Record where messages are in the mailbox file; identifiers the
        store does not hold are ignored."""
        with self._write():
            self._db.executemany(
                "UPDATE messages SET mbox_offset = ? WHERE message_id = ?",
                [(offset, message_id) for message_id, offset in positions.items()],
            )

    # -- classify pass --------------------------------------------------------

    def classified_ids(self, model: str) -> set[str]:
        rows = self._db.execute("SELECT message_id FROM classifications WHERE model = ?", (model,))
        return {row[0] for row in rows}

    def save_classification(self, message_id: str, decision: Decision) -> None:
        with self._write():
            self._db.execute(
                "INSERT OR REPLACE INTO classifications (message_id, gate_probability, email_type, "
                "type_probabilities, type_confidence, model, classified_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    decision.gate_probability,
                    decision.email_type,
                    json.dumps(decision.type_probabilities),
                    decision.type_confidence,
                    decision.model,
                    _now(),
                ),
            )
            self._db.execute("DELETE FROM failures WHERE message_id = ? AND pass = ?", (message_id, CLASSIFY_PASS))

    def gate_probabilities(self, model: str) -> list[float]:
        rows = self._db.execute("SELECT gate_probability FROM classifications WHERE model = ?", (model,))
        return [row[0] for row in rows]

    def email_types(self, model: str) -> dict[str, str]:
        """The email type the decision model chose, per message."""
        rows = self._db.execute("SELECT message_id, email_type FROM classifications WHERE model = ?", (model,))
        return dict(rows.fetchall())

    def email_type_counts(self, model: str) -> Counter[str]:
        rows = self._db.execute(
            "SELECT email_type, COUNT(*) FROM classifications WHERE model = ? GROUP BY email_type", (model,)
        )
        return Counter(dict(rows.fetchall()))

    # -- audit ------------------------------------------------------------------

    def audit_population(self, decision_model: str) -> list[tuple[str, float, bool | None]]:
        """(identifier, gate probability, has PDF attachment or None if
        unknown) for every message classified by ``decision_model``, sorted by
        identifier. Messages resolved from markup are left out."""
        rows = self._db.execute(
            "SELECT c.message_id, c.gate_probability, m.has_pdf FROM classifications c "
            "LEFT JOIN messages m ON m.message_id = c.message_id "
            "WHERE c.model = ? AND c.message_id NOT IN (SELECT message_id FROM extractions WHERE model = ?) "
            "ORDER BY c.message_id",
            (decision_model, MARKUP_SOURCE),
        )
        return [(mid, gate, None if pdf is None else bool(pdf)) for mid, gate, pdf in rows]

    def classify_failure_count(self, decision_model: str) -> int:
        """Messages whose classification failed and that have neither a
        classification by ``decision_model`` nor a markup record."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM failures WHERE pass = ? "
            "AND message_id NOT IN (SELECT message_id FROM classifications WHERE model = ?) "
            "AND message_id NOT IN (SELECT message_id FROM extractions WHERE model = ?)",
            (CLASSIFY_PASS, decision_model, MARKUP_SOURCE),
        ).fetchone()
        return row[0]

    # -- extract pass -----------------------------------------------------------

    def extraction_candidates(self, decision_model: str, threshold: float) -> list[str]:
        """Messages classified by ``decision_model`` with gate at or above the
        threshold, highest gate first. Messages resolved from markup are
        never candidates, even if a model classified them in an earlier run."""
        rows = self._db.execute(
            "SELECT message_id FROM classifications WHERE model = ? AND gate_probability >= ? "
            "AND message_id NOT IN (SELECT message_id FROM extractions WHERE model = ?) "
            "ORDER BY gate_probability DESC, message_id",
            (decision_model, threshold, MARKUP_SOURCE),
        )
        return [row[0] for row in rows]

    # -- markup -------------------------------------------------------------------

    def markup_ids(self) -> set[str]:
        """Messages resolved from their own machine-readable flight data."""
        return self.extracted_ids(MARKUP_SOURCE)

    def save_markup(self, message_id: str, record: ExtractionRecord) -> None:
        """Store a markup record, replacing any model record for the message;
        earlier failures of either pass no longer apply."""
        with self._write():
            self._db.execute(
                "INSERT OR REPLACE INTO extractions (message_id, record, attempts, model, extracted_at, "
                "input_truncated) VALUES (?, ?, 0, ?, ?, 0)",
                (message_id, json.dumps(record.to_json()), MARKUP_SOURCE, _now()),
            )
            self._db.execute("DELETE FROM failures WHERE message_id = ?", (message_id,))
            self._db.execute("DELETE FROM attachment_notes WHERE message_id = ?", (message_id,))

    def extracted_ids(self, model: str) -> set[str]:
        rows = self._db.execute("SELECT message_id FROM extractions WHERE model = ?", (model,))
        return {row[0] for row in rows}

    def save_extraction(self, message_id: str, extraction: Extraction) -> None:
        with self._write():
            self._db.execute(
                "INSERT OR REPLACE INTO extractions (message_id, record, attempts, model, extracted_at, "
                "input_truncated) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    json.dumps(extraction.record.to_json()),
                    extraction.attempts,
                    extraction.record.model,
                    _now(),
                    int(extraction.input_truncated),
                ),
            )
            self._db.execute("DELETE FROM failures WHERE message_id = ? AND pass = ?", (message_id, EXTRACT_PASS))

    def truncated_extraction_ids(self, model: str) -> set[str]:
        """Messages whose extraction input was cut at the character budget."""
        rows = self._db.execute(
            "SELECT message_id FROM extractions WHERE model = ? AND input_truncated", (model,)
        )
        return {row[0] for row in rows}

    def replace_attachment_notes(self, message_id: str, reasons: tuple[str, ...]) -> None:
        with self._write():
            self._db.execute("DELETE FROM attachment_notes WHERE message_id = ?", (message_id,))
            now = _now()
            self._db.executemany(
                "INSERT INTO attachment_notes (message_id, position, reason, recorded_at) VALUES (?, ?, ?, ?)",
                [(message_id, i, reason, now) for i, reason in enumerate(reasons)],
            )

    def attachment_notes(self, message_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT reason FROM attachment_notes WHERE message_id = ? ORDER BY position", (message_id,)
        )
        return [row[0] for row in rows]

    def extraction_records(self, model: str) -> dict[str, ExtractionRecord]:
        rows = self._db.execute("SELECT message_id, record FROM extractions WHERE model = ?", (model,))
        return {message_id: ExtractionRecord.from_json(json.loads(record), model) for message_id, record in rows}

    def message_info(self, message_id: str) -> tuple[str, str, str]:
        """(sender, subject, date) as recorded when the message was classified."""
        row = self._db.execute(
            "SELECT sender, subject, date FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return tuple(row) if row else ("", "", "")  # type: ignore[return-value]

    def failure_reason(self, message_id: str, pass_name: str) -> str | None:
        row = self._db.execute(
            "SELECT reason FROM failures WHERE message_id = ? AND pass = ?", (message_id, pass_name)
        ).fetchone()
        return row[0] if row else None

    # -- review table -----------------------------------------------------------

    def record_review_table(self, path: Path, sha256: str) -> None:
        with self._write():
            self._db.execute(
                "INSERT OR REPLACE INTO review_tables (path, sha256, written_at) VALUES (?, ?, ?)",
                (str(path.resolve()), sha256, _now()),
            )

    def review_table_hash(self, path: Path) -> str | None:
        """The hash of the review table at ``path`` as review last wrote it."""
        row = self._db.execute(
            "SELECT sha256 FROM review_tables WHERE path = ?", (str(path.resolve()),)
        ).fetchone()
        return row[0] if row else None

    # -- review decisions -------------------------------------------------------
    # A store from before decisions existed gets the empty table from the
    # schema script (CREATE TABLE IF NOT EXISTS); nothing else changes.

    def review_decisions(self) -> dict[str, ReviewDecision]:
        """Every saved review decision, by flight key."""
        rows = self._db.execute(
            "SELECT flight_key, keep, date, flight_number, origin, destination, added FROM review_decisions"
        )
        return {
            key: ReviewDecision(None if keep is None else bool(keep), day, number, origin, destination, bool(added))
            for key, keep, day, number, origin, destination, added in rows
        }

    def save_keep(self, flight_key: str, keep: bool | None) -> None:
        """Save (or with None, remove) the keep override for a flight."""
        self._update_decision(flight_key, keep=None if keep is None else int(keep))

    def save_corrections(
        self,
        flight_key: str,
        date: str | None = None,
        flight_number: str | None = None,
        origin: str | None = None,
        destination: str | None = None,
    ) -> None:
        """Replace the flight's corrections (None: that field is not corrected)."""
        self._update_decision(
            flight_key, date=date, flight_number=flight_number, origin=origin, destination=destination
        )

    def set_added(self, flight_key: str, added: bool) -> None:
        """Tick (or untick) the flight on the manual-add list."""
        self._update_decision(flight_key, added=int(added))

    def clear_decision(self, flight_key: str) -> None:
        """Remove the flight's keep override and corrections; its manual-add
        tick stays."""
        self._update_decision(flight_key, keep=None, date=None, flight_number=None, origin=None, destination=None)

    def delete_decisions(self, flight_keys: set[str] | list[str]) -> int:
        """Delete the decisions of these flights entirely; the number deleted."""
        with self._write():
            deleted = 0
            for key in sorted(flight_keys):
                deleted += self._db.execute("DELETE FROM review_decisions WHERE flight_key = ?", (key,)).rowcount
        return deleted

    def _update_decision(self, flight_key: str, **values: object) -> None:
        columns = ", ".join(values)
        with self._write():
            self._db.execute(
                f"INSERT INTO review_decisions (flight_key, {columns}, updated_at) "
                f"VALUES (?, {', '.join('?' for _ in values)}, ?) "
                f"ON CONFLICT(flight_key) DO UPDATE SET "
                f"{', '.join(f'{c}=excluded.{c}' for c in values)}, updated_at=excluded.updated_at",
                (flight_key, *values.values(), _now()),
            )
            self._db.execute(
                "DELETE FROM review_decisions WHERE flight_key = ? AND keep IS NULL AND date IS NULL "
                "AND flight_number IS NULL AND origin IS NULL AND destination IS NULL AND NOT added",
                (flight_key,),
            )

    # -- failures ---------------------------------------------------------------

    def record_failure(self, message_id: str, pass_name: str, reason: str) -> None:
        with self._write():
            self._db.execute(
                "INSERT OR REPLACE INTO failures (message_id, pass, reason, recorded_at) VALUES (?, ?, ?, ?)",
                (message_id, pass_name, reason, _now()),
            )

    # -- saved message text -------------------------------------------------------

    def save_message_text(self, message_id: str, text: str) -> None:
        """Save (or replace) the text of a message that yielded a record."""
        with self._write():
            self._db.execute(
                "INSERT OR REPLACE INTO message_texts (message_id, text, saved_at) VALUES (?, ?, ?)",
                (message_id, text, _now()),
            )

    def message_text(self, message_id: str) -> str | None:
        """The saved text of a message, None if none was saved (for example
        a record extracted before text was saved)."""
        row = self._db.execute("SELECT text FROM message_texts WHERE message_id = ?", (message_id,)).fetchone()
        return row[0] if row else None

    def commit(self) -> None:
        if not self._held:
            self._db.commit()

    # -- session values ---------------------------------------------------------
    # Small named values that belong to the session as a whole (for example
    # the effective threshold, or what the session was started with), each
    # stored as text. A store from before this table existed gets it empty.

    def session_value(self, key: str) -> str | None:
        """The session value stored under ``key``, or None."""
        row = self._db.execute("SELECT value FROM session_values WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_session_value(self, key: str, value: str | None) -> None:
        """Store (or with None, remove) the session value under ``key``."""
        with self._write():
            if value is None:
                self._db.execute("DELETE FROM session_values WHERE key = ?", (key,))
            else:
                self._db.execute(
                    "INSERT OR REPLACE INTO session_values (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, _now()),
                )

    # -- written files ----------------------------------------------------------
    # A store from before this table existed gets it empty: the files written
    # before then are not known.

    def record_written_file(self, path: Path, kind: str) -> None:
        """Record that the tool wrote the file at ``path`` (stored resolved)."""
        with self._write():
            self._db.execute(
                "INSERT OR REPLACE INTO written_files (path, kind, written_at) VALUES (?, ?, ?)",
                (str(path.resolve()), kind, _now()),
            )

    def written_files(self) -> list[tuple[str, Path, str]]:
        """(kind, path, written at) for every file recorded as written, oldest first."""
        rows = self._db.execute("SELECT kind, path, written_at FROM written_files ORDER BY written_at, rowid")
        return [(kind, Path(path), written_at) for kind, path, written_at in rows]

    def row_counts(self) -> dict[str, int]:
        """How much the store holds: messages recorded, classifications (by
        any model), extractions by a model, records read from markup, and
        review decisions."""
        queries = {
            "messages": ("SELECT COUNT(*) FROM messages", ()),
            "classifications": ("SELECT COUNT(*) FROM classifications", ()),
            "model_extractions": ("SELECT COUNT(*) FROM extractions WHERE model != ?", (MARKUP_SOURCE,)),
            "markup": ("SELECT COUNT(*) FROM extractions WHERE model = ?", (MARKUP_SOURCE,)),
            "review_decisions": ("SELECT COUNT(*) FROM review_decisions", ()),
        }
        return {name: self._db.execute(query, params).fetchone()[0] for name, (query, params) in queries.items()}

    # -- held writes ------------------------------------------------------------

    def hold_writes(self) -> None:
        """Stop committing: writes from now on stay in one open transaction
        until ``release_writes``. A model pass holds its writes until the
        preflight check has passed, so a failed check leaves the store as it
        was."""
        self._held = True

    def release_writes(self, keep: bool = True) -> None:
        """Commit (``keep``) or roll back the held writes, and commit each
        result as it is known again."""
        if not self._held:
            return
        self._held = False
        if keep:
            self._db.commit()
        else:
            self._db.rollback()

    @contextmanager
    def _write(self) -> Iterator[None]:
        if self._held:
            yield
            return
        with self._db:
            yield


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
