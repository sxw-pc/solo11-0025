"""SQLite persistence for check batches, findings and dictionary snapshots."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .compare import Finding
from .config import CATEGORIES, canonical_json, safe_batch_component

SCHEMA = """
CREATE TABLE IF NOT EXISTS batch (
    batch_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    dict_fingerprint TEXT NOT NULL,
    report_count INTEGER NOT NULL,
    counts_json TEXT NOT NULL,
    parser_mode TEXT NOT NULL DEFAULT 'auto'
);
CREATE TABLE IF NOT EXISTS finding (
    finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batch(batch_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    report TEXT NOT NULL,
    category TEXT NOT NULL,
    metric TEXT NOT NULL,
    parser_source TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    differences_json TEXT NOT NULL,
    UNIQUE(batch_id, ordinal)
);
CREATE TABLE IF NOT EXISTS dict_snapshot (
    fingerprint TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    source_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_finding_batch ON finding(batch_id);
CREATE INDEX IF NOT EXISTS idx_finding_metric ON finding(metric);
CREATE INDEX IF NOT EXISTS idx_batch_created ON batch(created_at);
"""


def utc_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def make_batch_id(fingerprint: str, now: datetime | None = None) -> str:
    """Timestamp plus the first eight dictionary fingerprint characters."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%S%fZ")
    return safe_batch_component(f"{stamp}-{fingerprint[:8]}")


class Store:
    """Thin, connection-per-operation SQLite repository."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._memory_connection = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
            self._memory_connection.row_factory = sqlite3.Row
            self._memory_connection.execute("PRAGMA foreign_keys = ON")
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialise()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self._memory_connection is not None:
            with self._lock:
                try:
                    yield self._memory_connection
                    self._memory_connection.commit()
                except Exception:
                    self._memory_connection.rollback()
                    raise
            return
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialise(self) -> None:
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(batch)")}
            if "parser_mode" not in columns:
                connection.execute(
                    "ALTER TABLE batch ADD COLUMN parser_mode TEXT NOT NULL DEFAULT 'auto'"
                )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

    def save_batch(self, batch: dict[str, Any], findings: Iterable[Finding | dict[str, Any]],
                   dict_content: str, *, source_path: str = "") -> bool:
        """Insert a batch and its findings atomically. Returns False if it existed."""
        safe_batch_component(str(batch["batch_id"]))
        payload = [item.to_dict() if isinstance(item, Finding) else dict(item)
                   for item in findings]
        counts = batch.get("counts") or {}
        normalised_counts = {name: int(counts.get(name, 0)) for name in CATEGORIES}
        with self.connection() as connection:
            try:
                with connection:
                    cursor = connection.execute(
                        """INSERT OR IGNORE INTO batch
                           (batch_id, created_at, dict_fingerprint, report_count,
                            counts_json, parser_mode)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            batch["batch_id"],
                            batch["created_at"],
                            batch["dict_fingerprint"],
                            int(batch.get("report_count", 0)),
                            canonical_json(normalised_counts),
                            str(batch.get("parser_mode", "auto")),
                        ),
                    )
                    inserted = cursor.rowcount > 0
                    if not inserted:
                        return False
                    connection.execute(
                        """INSERT OR IGNORE INTO dict_snapshot
                           (fingerprint, content, source_path, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (batch["dict_fingerprint"], dict_content,
                         source_path, batch["created_at"]),
                    )
                    for ordinal, finding in enumerate(payload):
                        connection.execute(
                            """INSERT OR IGNORE INTO finding
                               (batch_id, ordinal, report, category, metric, parser_source,
                                line_start, line_end, summary, evidence_json, differences_json)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                batch["batch_id"],
                                ordinal,
                                finding["report"],
                                finding["category"],
                                finding["metric"],
                                finding.get("parser_source", "fallback"),
                                int(finding.get("line_start", 1)),
                                int(finding.get("line_end", 1)),
                                finding.get("summary", ""),
                                canonical_json(finding.get("evidence", {})),
                                canonical_json(finding.get("differences", [])),
                            ),
                        )
                return inserted
            except sqlite3.Error:
                raise

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        safe_batch_component(batch_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM batch WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            return None
        counts = {name: 0 for name in CATEGORIES}
        counts.update(json.loads(row["counts_json"]))
        return {
            "batch_id": row["batch_id"],
            "created_at": row["created_at"],
            "dict_fingerprint": row["dict_fingerprint"],
            "report_count": row["report_count"],
            "counts": counts,
            "parser_mode": row["parser_mode"],
        }

    def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT batch_id FROM batch ORDER BY created_at DESC, batch_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [batch for row in rows if (batch := self.get_batch(row["batch_id"])) is not None]

    def get_findings(self, batch_id: str) -> list[dict[str, Any]]:
        safe_batch_component(batch_id)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM finding WHERE batch_id = ? ORDER BY ordinal, finding_id",
                (batch_id,),
            ).fetchall()
        findings: list[dict[str, Any]] = []
        for row in rows:
            evidence = json.loads(row["evidence_json"])
            annotations = evidence.get("annotations", []) if isinstance(evidence, dict) else []
            findings.append({
                "report": row["report"],
                "category": row["category"],
                "metric": row["metric"],
                "parser_source": row["parser_source"],
                "line_start": row["line_start"],
                "line_end": row["line_end"],
                "line_range": [row["line_start"], row["line_end"]],
                "summary": row["summary"],
                "differences": json.loads(row["differences_json"]),
                "annotations": annotations,
                "evidence": evidence,
            })
        return findings

    def findings_by_category(self, batch_id: str, category: str) -> list[dict[str, Any]]:
        return [item for item in self.get_findings(batch_id) if item["category"] == category]

    def category_counts(self, batch_id: str) -> dict[str, int]:
        counts = {name: 0 for name in CATEGORIES}
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT category, COUNT(*) AS amount FROM finding "
                "WHERE batch_id = ? GROUP BY category",
                (batch_id,),
            ).fetchall()
        for row in rows:
            counts[row["category"]] = row["amount"]
        return counts

    def metric_history(self, metric: str, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT b.batch_id, b.created_at, f.report, f.category, f.summary
                   FROM finding f JOIN batch b ON b.batch_id = f.batch_id
                   WHERE f.metric = ?
                   ORDER BY b.created_at DESC, b.batch_id DESC, f.ordinal
                   LIMIT ?""",
                (metric.strip().lower(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_dict_snapshot(self, fingerprint: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM dict_snapshot WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return dict(row) if row is not None else None

    def count_batches(self) -> int:
        with self.connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS amount FROM batch").fetchone()
        return int(row["amount"])
