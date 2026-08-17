"""Durable store: SQLite, append-only, every run recoverable.

The failure this design exists to prevent is a bad scrape silently replacing
good data. So:

  * ``filings`` holds current state and is only ever written inside one
    transaction, at the end of a run that already passed the quality gate.
  * ``filing_history`` is append-only. Every field-level change is recorded with
    the run that caused it, so "when did this become Approved?" is answerable
    and any run can be explained after the fact.
  * ``runs`` and ``source_health`` record what happened even when a run is
    rejected, so failures are diagnosable rather than invisible.

SQLite is used because it is in the standard library, needs no service, and
gives real transactions. There is no ORM: the schema is small and explicit.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .models import Filing, parse_date

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    outcome           TEXT NOT NULL DEFAULT 'running',
    records_fetched   INTEGER NOT NULL DEFAULT 0,
    records_committed INTEGER NOT NULL DEFAULT 0,
    added             INTEGER NOT NULL DEFAULT 0,
    changed           INTEGER NOT NULL DEFAULT 0,
    message           TEXT NOT NULL DEFAULT '',
    detail            TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS filings (
    filing_no      TEXT PRIMARY KEY,
    sro            TEXT NOT NULL,
    sro_family     TEXT NOT NULL,
    filing_year    INTEGER NOT NULL,
    filing_date    TEXT,
    status         TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    release_number TEXT NOT NULL DEFAULT '',
    filing_url     TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    source_url     TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    seen_by        TEXT NOT NULL DEFAULT '',
    content_hash   TEXT NOT NULL,
    first_run_id   INTEGER REFERENCES runs(id),
    last_run_id    INTEGER REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_filings_sro    ON filings(sro);
CREATE INDEX IF NOT EXISTS idx_filings_family ON filings(sro_family);
CREATE INDEX IF NOT EXISTS idx_filings_year   ON filings(filing_year);
CREATE INDEX IF NOT EXISTS idx_filings_date   ON filings(filing_date);
CREATE INDEX IF NOT EXISTS idx_filings_status ON filings(status);

CREATE TABLE IF NOT EXISTS filing_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filing_no   TEXT NOT NULL,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    at          TEXT NOT NULL,
    change_type TEXT NOT NULL,           -- 'added' | 'changed'
    changes     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_history_filing ON filing_history(filing_no);
CREATE INDEX IF NOT EXISTS idx_history_run    ON filing_history(run_id);
CREATE INDEX IF NOT EXISTS idx_history_at     ON filing_history(at);

CREATE TABLE IF NOT EXISTS source_health (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES runs(id),
    source    TEXT NOT NULL,
    tier      TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL,             -- 'ok' | 'degraded' | 'failed'
    records   INTEGER NOT NULL DEFAULT 0,
    duration  REAL NOT NULL DEFAULT 0,
    message   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_health_run ON source_health(run_id);
"""

# Fields compared to decide whether a filing materially changed.
_TRACKED_FIELDS = (
    "sro", "sro_family", "filing_year", "filing_date", "status",
    "summary", "release_number", "filing_url", "source", "source_url", "notes",
)


def _now() -> str:
    return dt.datetime.now().isoformat(sep=" ", timespec="seconds")


class Store:
    """All persistence. Open one per process; it is not thread-shared."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---- runs ----------------------------------------------------------

    def start_run(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs(started_at, outcome) VALUES (?, 'running')", (_now(),)
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        outcome: str,
        records_fetched: int = 0,
        records_committed: int = 0,
        added: int = 0,
        changed: int = 0,
        message: str = "",
        detail: dict[str, object] | None = None,
    ) -> None:
        self._conn.execute(
            """UPDATE runs SET finished_at=?, outcome=?, records_fetched=?,
                   records_committed=?, added=?, changed=?, message=?, detail=?
               WHERE id=?""",
            (_now(), outcome, records_fetched, records_committed, added, changed,
             message, json.dumps(detail or {}), run_id),
        )
        self._conn.commit()

    def record_source_health(self, run_id: int, entries: Iterable[dict[str, object]]) -> None:
        self._conn.executemany(
            """INSERT INTO source_health(run_id, source, tier, status, records, duration, message)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (run_id, e.get("source", ""), e.get("tier", ""), e.get("status", ""),
                 int(e.get("records", 0) or 0), float(e.get("duration", 0) or 0),
                 str(e.get("message", "")))
                for e in entries
            ],
        )
        self._conn.commit()

    def last_run(self, outcome: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM runs"
        args: tuple[object, ...] = ()
        if outcome:
            sql += " WHERE outcome=?"
            args = (outcome,)
        sql += " ORDER BY id DESC LIMIT 1"
        return self._conn.execute(sql, args).fetchone()

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))

    def source_health(self, run_id: int) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM source_health WHERE run_id=? ORDER BY tier, source", (run_id,)))

    # ---- filings -------------------------------------------------------

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0])

    def existing_hashes(self) -> dict[str, str]:
        return {
            row["filing_no"]: row["content_hash"]
            for row in self._conn.execute("SELECT filing_no, content_hash FROM filings")
        }

    def get(self, filing_no: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM filings WHERE filing_no=?", (filing_no,)).fetchone()

    def commit_filings(self, run_id: int, filings: Sequence[Filing]) -> tuple[int, int]:
        """Upsert a batch atomically and append the change log.

        Returns ``(added, changed)``. Records already present and unchanged are
        touched only in ``last_seen``/``seen_by``, so the history stays signal.
        """
        added = changed = 0
        now = _now()

        with self.transaction() as conn:
            for filing in filings:
                prior = conn.execute(
                    "SELECT * FROM filings WHERE filing_no=?", (filing.filing_no,)
                ).fetchone()

                date_str = filing.filing_date.isoformat() if filing.filing_date else None
                seen_by = ", ".join(filing.seen_by)
                new_hash = filing.content_hash

                if prior is None:
                    conn.execute(
                        """INSERT INTO filings(
                               filing_no, sro, sro_family, filing_year, filing_date,
                               status, summary, release_number, filing_url, source,
                               source_url, notes, first_seen, last_seen, seen_by,
                               content_hash, first_run_id, last_run_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (filing.filing_no, filing.sro, filing.sro_family,
                         filing.filing_year, date_str, filing.status, filing.summary,
                         filing.release_number, filing.filing_url, filing.source,
                         filing.source_url, filing.notes, now, now, seen_by,
                         new_hash, run_id, run_id),
                    )
                    conn.execute(
                        """INSERT INTO filing_history(filing_no, run_id, at, change_type, changes)
                           VALUES (?,?,?,'added',?)""",
                        (filing.filing_no, run_id, now,
                         json.dumps({"status": filing.status, "sro": filing.sro})),
                    )
                    added += 1
                    continue

                if prior["content_hash"] == new_hash:
                    conn.execute(
                        "UPDATE filings SET last_seen=?, seen_by=?, last_run_id=? WHERE filing_no=?",
                        (now, seen_by or prior["seen_by"], run_id, filing.filing_no),
                    )
                    continue

                diff: dict[str, list[object]] = {}
                new_values = {
                    "sro": filing.sro,
                    "sro_family": filing.sro_family,
                    "filing_year": filing.filing_year,
                    "filing_date": date_str,
                    "status": filing.status,
                    "summary": filing.summary,
                    "release_number": filing.release_number,
                    "filing_url": filing.filing_url,
                    "source": filing.source,
                    "source_url": filing.source_url,
                    "notes": filing.notes,
                }
                for field in _TRACKED_FIELDS:
                    before, after = prior[field], new_values[field]
                    if (before or "") != (after or ""):
                        diff[field] = [before, after]

                conn.execute(
                    """UPDATE filings SET sro=?, sro_family=?, filing_year=?, filing_date=?,
                           status=?, summary=?, release_number=?, filing_url=?, source=?,
                           source_url=?, notes=?, last_seen=?, seen_by=?, content_hash=?,
                           last_run_id=?
                       WHERE filing_no=?""",
                    (filing.sro, filing.sro_family, filing.filing_year, date_str,
                     filing.status, filing.summary, filing.release_number,
                     filing.filing_url, filing.source, filing.source_url, filing.notes,
                     now, seen_by or prior["seen_by"], new_hash, run_id, filing.filing_no),
                )
                conn.execute(
                    """INSERT INTO filing_history(filing_no, run_id, at, change_type, changes)
                       VALUES (?,?,?,'changed',?)""",
                    (filing.filing_no, run_id, now, json.dumps(diff)),
                )
                changed += 1

        return added, changed

    # ---- queries -------------------------------------------------------

    def query(
        self,
        *,
        search: str = "",
        sro: str = "",
        family: str = "",
        status: str = "",
        year: int | None = None,
        since: dt.date | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        sql = ["SELECT * FROM filings WHERE 1=1"]
        args: list[object] = []
        if search:
            sql.append("AND (filing_no LIKE ? OR summary LIKE ? OR release_number LIKE ? OR sro LIKE ?)")
            like = f"%{search}%"
            args += [like, like, like, like]
        if sro:
            sql.append("AND sro = ?")
            args.append(sro)
        if family:
            sql.append("AND sro_family = ?")
            args.append(family)
        if status:
            sql.append("AND status = ?")
            args.append(status)
        if year:
            sql.append("AND filing_year = ?")
            args.append(year)
        if since:
            sql.append("AND filing_date >= ?")
            args.append(since.isoformat())
        sql.append("ORDER BY filing_date DESC NULLS LAST, filing_no DESC")
        if limit is not None:
            sql.append("LIMIT ? OFFSET ?")
            args += [limit, offset]
        return list(self._conn.execute(" ".join(sql), args))

    def count_matching(self, **kwargs: object) -> int:
        kwargs.pop("limit", None)
        kwargs.pop("offset", None)
        return len(self.query(**kwargs))  # type: ignore[arg-type]

    def distinct(self, column: str) -> list[str]:
        if column not in {"sro", "sro_family", "status", "filing_year", "source"}:
            raise ValueError(f"not a filterable column: {column}")
        rows = self._conn.execute(
            f"SELECT DISTINCT {column} AS v FROM filings WHERE v IS NOT NULL AND v != '' ORDER BY v"
        )
        return [str(r["v"]) for r in rows]

    def summary_by(self, column: str) -> list[tuple[str, int]]:
        if column not in {"sro", "sro_family", "status", "filing_year"}:
            raise ValueError(f"not a groupable column: {column}")
        rows = self._conn.execute(
            f"SELECT {column} AS v, COUNT(*) AS n FROM filings GROUP BY v ORDER BY n DESC"
        )
        return [(str(r["v"]), int(r["n"])) for r in rows]

    def changes_since(self, since: dt.datetime) -> list[sqlite3.Row]:
        """Change-log entries newer than ``since``, joined to current state."""
        return list(self._conn.execute(
            """SELECT h.change_type, h.changes, h.at, f.*
                 FROM filing_history h
                 JOIN filings f ON f.filing_no = h.filing_no
                WHERE h.at >= ?
             ORDER BY h.at DESC, f.filing_date DESC""",
            (since.isoformat(sep=" ", timespec="seconds"),),
        ))

    def latest_filing_date(self) -> dt.date | None:
        row = self._conn.execute(
            "SELECT MAX(filing_date) AS d FROM filings").fetchone()
        return parse_date(row["d"]) if row and row["d"] else None
