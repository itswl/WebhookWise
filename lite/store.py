"""SQLite persistence for WebhookWise Lite.

Raw SQL over aiosqlite rather than an ORM: the whole point of this edition is
that a reader can see every table and every query in one file. WAL mode keeps
the single writer from blocking dashboard reads.
"""

from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  REAL    NOT NULL,
    source       TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    body         TEXT    NOT NULL,
    alert_hash   TEXT    NOT NULL,
    importance   TEXT,
    summary      TEXT,
    route        TEXT,
    raw          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_hash_time ON events (alert_hash, received_at);
CREATE INDEX IF NOT EXISTS ix_events_time      ON events (received_at);

-- One row per processed alert: the answer to "why did (or didn't) this notify me".
CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL,
    at         REAL    NOT NULL,
    outcome    TEXT    NOT NULL,          -- forwarded | skipped
    skip_code  TEXT    NOT NULL DEFAULT 'none',
    steps      TEXT    NOT NULL,          -- JSON: ordered gate chain
    matched    TEXT                       -- JSON: names of matched rules
);
CREATE INDEX IF NOT EXISTS ix_decisions_time ON decisions (at);
CREATE INDEX IF NOT EXISTS ix_decisions_code ON decisions (skip_code, at);

-- Delivery intent, written in the same transaction as the decision. Delivery is
-- a separate, retryable concern — the decision is never lost to a flaky target.
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL,
    rule_name       TEXT    NOT NULL,
    target_kind     TEXT    NOT NULL,     -- feishu | generic
    target_url      TEXT    NOT NULL,
    payload         TEXT    NOT NULL,     -- JSON
    status          TEXT    NOT NULL DEFAULT 'pending',  -- pending|sent|exhausted
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 4,
    next_attempt_at REAL    NOT NULL,
    last_error      TEXT,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS rules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    match_source     TEXT    NOT NULL DEFAULT '',   -- CSV, empty = any
    match_importance TEXT    NOT NULL DEFAULT '',   -- CSV, empty = any
    target_kind      TEXT    NOT NULL DEFAULT 'feishu',
    target_url       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS silences (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,                -- substring match on "source title body"
    reason  TEXT NOT NULL DEFAULT '',
    until   REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        # WAL lets the dashboard read while the pipeline writes; NORMAL sync is
        # the right durability/throughput trade for alert traffic.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store is not open")
        return self._db

    # ── events ────────────────────────────────────────────────────────────────

    async def insert_event(self, ev: dict[str, Any]) -> int:
        cur = await self.db.execute(
            "INSERT INTO events (received_at, source, title, body, alert_hash, importance, summary, route, raw)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ev["received_at"],
                ev["source"],
                ev["title"],
                ev["body"],
                ev["alert_hash"],
                ev.get("importance"),
                ev.get("summary"),
                ev.get("route"),
                json.dumps(ev.get("raw", {}), ensure_ascii=False),
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def recent_duplicate(self, alert_hash: str, window_seconds: int) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT id, received_at FROM events WHERE alert_hash=? AND received_at > ? ORDER BY id DESC LIMIT 1",
            (alert_hash, time.time() - window_seconds),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def last_forward_at(self, alert_hash: str) -> float | None:
        """When this identity last produced a delivery intent (cooldown input).

        Keyed on the event's arrival, not the outbox row's updated_at: a
        delivery retry must not silently push the cooldown window forward.
        """
        cur = await self.db.execute(
            "SELECT MAX(e.received_at) AS t FROM outbox o JOIN events e ON e.id = o.event_id WHERE e.alert_hash = ?",
            (alert_hash,),
        )
        row = await cur.fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    # ── decisions ─────────────────────────────────────────────────────────────

    async def insert_decision(
        self, event_id: int, outcome: str, skip_code: str, steps: list[dict[str, Any]], matched: list[str]
    ) -> None:
        await self.db.execute(
            "INSERT INTO decisions (event_id, at, outcome, skip_code, steps, matched) VALUES (?,?,?,?,?,?)",
            (
                event_id,
                time.time(),
                outcome,
                skip_code,
                json.dumps(steps, ensure_ascii=False),
                json.dumps(matched, ensure_ascii=False),
            ),
        )
        await self.db.commit()

    async def list_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT d.id, d.at, d.outcome, d.skip_code, d.steps, d.matched,"
            "       e.source, e.title, e.importance, e.summary, e.route"
            " FROM decisions d JOIN events e ON e.id = d.event_id"
            " ORDER BY d.id DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["steps"] = json.loads(item["steps"])
            item["matched"] = json.loads(item["matched"] or "[]")
            out.append(item)
        return out

    async def decision_stats(self, since_seconds: int = 86400) -> dict[str, int]:
        cur = await self.db.execute(
            "SELECT outcome, skip_code, COUNT(*) AS n FROM decisions WHERE at > ? GROUP BY outcome, skip_code",
            (time.time() - since_seconds,),
        )
        stats: dict[str, int] = {}
        for row in await cur.fetchall():
            key = "forwarded" if row["outcome"] == "forwarded" else str(row["skip_code"])
            stats[key] = stats.get(key, 0) + int(row["n"])
        return stats

    # ── outbox ────────────────────────────────────────────────────────────────

    async def enqueue_delivery(
        self, event_id: int, rule_name: str, kind: str, url: str, payload: dict[str, Any]
    ) -> None:
        now = time.time()
        await self.db.execute(
            "INSERT INTO outbox (event_id, rule_name, target_kind, target_url, payload, next_attempt_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (event_id, rule_name, kind, url, json.dumps(payload, ensure_ascii=False), now, now),
        )
        await self.db.commit()

    async def due_deliveries(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM outbox WHERE status='pending' AND next_attempt_at <= ? ORDER BY id LIMIT ?",
            (time.time(), limit),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def mark_sent(self, outbox_id: int) -> None:
        await self.db.execute(
            "UPDATE outbox SET status='sent', attempts=attempts+1, updated_at=? WHERE id=?",
            (time.time(), outbox_id),
        )
        await self.db.commit()

    async def mark_failed(self, outbox_id: int, error: str, backoff_seconds: float) -> str:
        """Bump attempts; exhaust when the budget is spent. Returns the new status."""
        now = time.time()
        cur = await self.db.execute("SELECT attempts, max_attempts FROM outbox WHERE id=?", (outbox_id,))
        row = await cur.fetchone()
        if row is None:
            return "missing"
        attempts = int(row["attempts"]) + 1
        status = "exhausted" if attempts >= int(row["max_attempts"]) else "pending"
        await self.db.execute(
            "UPDATE outbox SET status=?, attempts=?, last_error=?, next_attempt_at=?, updated_at=? WHERE id=?",
            (status, attempts, error[:500], now + backoff_seconds, now, outbox_id),
        )
        await self.db.commit()
        return status

    async def outbox_summary(self) -> dict[str, int]:
        cur = await self.db.execute("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")
        return {str(row["status"]): int(row["n"]) for row in await cur.fetchall()}

    # ── rules & silences ──────────────────────────────────────────────────────

    async def active_rules(self) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM rules WHERE enabled=1 ORDER BY id")
        return [dict(row) for row in await cur.fetchall()]

    async def add_rule(self, rule: dict[str, Any]) -> int:
        cur = await self.db.execute(
            "INSERT INTO rules (name, match_source, match_importance, target_kind, target_url) VALUES (?,?,?,?,?)",
            (
                rule["name"],
                rule.get("match_source", ""),
                rule.get("match_importance", ""),
                rule.get("target_kind", "feishu"),
                rule["target_url"],
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def delete_rule(self, rule_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def active_silences(self) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM silences WHERE until > ? ORDER BY id", (time.time(),))
        return [dict(row) for row in await cur.fetchall()]

    async def add_silence(self, pattern: str, minutes: int, reason: str = "") -> int:
        cur = await self.db.execute(
            "INSERT INTO silences (pattern, reason, until) VALUES (?,?,?)",
            (pattern, reason, time.time() + minutes * 60),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def delete_silence(self, silence_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM silences WHERE id=?", (silence_id,))
        await self.db.commit()
        return cur.rowcount > 0
