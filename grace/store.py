"""SQLite persistence (spec 4.3). Thread-safe; truth is segregated from evidence."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from grace.models import Customer, Event, Invoice, Mandate, Truth
from grace.util import ensure_aware, from_iso, to_iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mandates (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    rail TEXT NOT NULL,
    status TEXT NOT NULL,
    holdout INTEGER NOT NULL DEFAULT 0,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS truth (
    mandate_id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    mandate_id TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_mandate ON invoices(mandate_id);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    mandate_id TEXT NOT NULL,
    name TEXT NOT NULL,
    at TEXT NOT NULL,
    invoice_id TEXT,
    payload TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_mandate ON events(mandate_id, at);
CREATE INDEX IF NOT EXISTS idx_events_invoice ON events(invoice_id);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    mandate_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_arm ON decisions(arm, mandate_id);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT,
    decision_id TEXT,
    mandate_id TEXT,
    phase TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_mandate ON audit(mandate_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_decision ON audit(decision_id);
CREATE TABLE IF NOT EXISTS locks (
    key TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    mandate_id TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_state (
    mandate_id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    """One SQLite file per run. All access is serialised by an RLock."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._bulk_depth = 0
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # WAL + NORMAL is the standard durable-enough pairing: it survives an
            # application crash; only an OS crash can lose the last transactions.
            # With one fsync per write, generating a 2,000-mandate cohort spent
            # minutes in fsync alone.
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def bulk(self):
        """Defer commits during a bulk load: one commit at the end, not thousands."""
        with self._lock:
            self._bulk_depth += 1
        try:
            yield self
        finally:
            with self._lock:
                self._bulk_depth -= 1
                if self._bulk_depth == 0:
                    self._conn.commit()

    def _commit(self) -> None:
        if self._bulk_depth == 0:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- meta
    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self._commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # ----------------------------------------------------------- customers
    def upsert_customer(self, c: Customer) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO customers(id,json) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET json=excluded.json",
                (c.id, c.model_dump_json()),
            )
            self._commit()

    def get_customer(self, cid: str) -> Customer | None:
        with self._lock:
            row = self._conn.execute("SELECT json FROM customers WHERE id=?", (cid,)).fetchone()
        return Customer.model_validate_json(row["json"]) if row else None

    # ------------------------------------------------------------ mandates
    def upsert_mandate(self, m: Mandate, holdout: bool | None = None) -> None:
        with self._lock:
            if holdout is None:
                row = self._conn.execute("SELECT holdout FROM mandates WHERE id=?", (m.id,)).fetchone()
                holdout = bool(row["holdout"]) if row else False
            self._conn.execute(
                "INSERT INTO mandates(id,customer_id,rail,status,holdout,json) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET customer_id=excluded.customer_id, rail=excluded.rail, "
                "status=excluded.status, holdout=excluded.holdout, json=excluded.json",
                (m.id, m.customer_id, m.rail.value, m.status.value, int(holdout), m.model_dump_json()),
            )
            self._commit()

    def get_mandate(self, mid: str) -> Mandate | None:
        with self._lock:
            row = self._conn.execute("SELECT json FROM mandates WHERE id=?", (mid,)).fetchone()
        return Mandate.model_validate_json(row["json"]) if row else None

    def all_mandates(self, holdout: bool | None = None) -> list[Mandate]:
        sql = "SELECT json FROM mandates"
        args: tuple = ()
        if holdout is not None:
            sql += " WHERE holdout=?"
            args = (int(holdout),)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [Mandate.model_validate_json(r["json"]) for r in rows]

    def is_holdout(self, mid: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT holdout FROM mandates WHERE id=?", (mid,)).fetchone()
        return bool(row["holdout"]) if row else False

    def holdout_ids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM mandates WHERE holdout=1").fetchall()
        return {r["id"] for r in rows}

    # --------------------------------------------------------------- truth
    def set_truth(self, mandate_id: str, t: Truth) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO truth(mandate_id,json) VALUES(?,?) ON CONFLICT(mandate_id) DO UPDATE SET json=excluded.json",
                (mandate_id, t.model_dump_json()),
            )
            self._commit()

    def get_truth(self, mandate_id: str) -> Truth | None:
        with self._lock:
            row = self._conn.execute("SELECT json FROM truth WHERE mandate_id=?", (mandate_id,)).fetchone()
        return Truth.model_validate_json(row["json"]) if row else None

    # ------------------------------------------------------------ invoices
    def upsert_invoice(self, inv: Invoice) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO invoices(id,mandate_id,json) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET mandate_id=excluded.mandate_id, json=excluded.json",
                (inv.id, inv.mandate_id, inv.model_dump_json()),
            )
            self._commit()

    def get_invoice(self, iid: str) -> Invoice | None:
        with self._lock:
            row = self._conn.execute("SELECT json FROM invoices WHERE id=?", (iid,)).fetchone()
        return Invoice.model_validate_json(row["json"]) if row else None

    def invoices_for(self, mandate_id: str) -> list[Invoice]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT json FROM invoices WHERE mandate_id=? ORDER BY id", (mandate_id,)
            ).fetchall()
        return [Invoice.model_validate_json(r["json"]) for r in rows]

    def open_invoice(self, mandate_id: str) -> Invoice | None:
        """Oldest unpaid invoice, which is what a manual charge would target."""
        for inv in self.invoices_for(mandate_id):
            if inv.status != "paid":
                return inv
        return None

    # -------------------------------------------------------------- events
    def append_event(self, ev: Event) -> bool:
        """Idempotent. Returns True if newly inserted, False if a duplicate."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events(id,mandate_id,name,at,invoice_id,payload,processed) "
                "VALUES(?,?,?,?,?,?,?)",
                (ev.id, ev.mandate_id, ev.name, to_iso(ev.at), ev.invoice_id,
                 json.dumps(ev.payload), int(ev.processed)),
            )
            self._commit()
            return cur.rowcount > 0

    def events_for(self, mandate_id: str, limit: int | None = None) -> list[Event]:
        sql = "SELECT * FROM events WHERE mandate_id=? ORDER BY at, id"
        with self._lock:
            rows = self._conn.execute(sql, (mandate_id,)).fetchall()
        evs = [self._row_to_event(r) for r in rows]
        return evs[-limit:] if limit else evs

    def events_for_invoice(self, invoice_id: str) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE invoice_id=? ORDER BY at, id", (invoice_id,)
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def count_events(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> Event:
        return Event(
            id=r["id"], mandate_id=r["mandate_id"], name=r["name"],
            at=from_iso(r["at"]), payload=json.loads(r["payload"]), processed=bool(r["processed"]),
        )

    # ----------------------------------------------------------- decisions
    def save_decision(self, decision_id: str, mandate_id: str, arm: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions(decision_id,mandate_id,arm,json) VALUES(?,?,?,?) "
                "ON CONFLICT(decision_id) DO UPDATE SET json=excluded.json",
                (decision_id, mandate_id, arm, json.dumps(payload)),
            )
            self._commit()

    def decisions_for_arm(self, arm: str) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mandate_id, json FROM decisions WHERE arm=?", (arm,)
            ).fetchall()
        return {r["mandate_id"]: json.loads(r["json"]) for r in rows}

    def decided_mandate_ids(self, arm: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT mandate_id FROM decisions WHERE arm=?", (arm,)).fetchall()
        return {r["mandate_id"] for r in rows}

    def get_decision(self, decision_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
        return json.loads(row["json"]) if row else None

    # --------------------------------------------------------------- audit
    def append_audit(self, *, phase: str, run_id: str | None = None,
                     decision_id: str | None = None, mandate_id: str | None = None,
                     **fields: Any) -> int:
        rec = {"phase": phase, "decision_id": decision_id, "mandate_id": mandate_id, **fields}
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit(ts,run_id,decision_id,mandate_id,phase,json) VALUES(?,?,?,?,?,?)",
                (to_iso(utcnow()), run_id, decision_id, mandate_id, phase,
                 json.dumps(rec, default=str)),
            )
            self._commit()
            return int(cur.lastrowid)

    def audit_for(self, mandate_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, phase, decision_id, json FROM audit WHERE mandate_id=? ORDER BY seq",
                (mandate_id,),
            ).fetchall()
        out = []
        for r in rows:
            rec = json.loads(r["json"])
            rec.update(seq=r["seq"], ts=r["ts"])
            out.append(rec)
        return out

    def audit_by_phase(self, phase: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, mandate_id, decision_id, json FROM audit WHERE phase=? ORDER BY seq",
                (phase,),
            ).fetchall()
        out = []
        for r in rows:
            rec = json.loads(r["json"])
            rec.update(seq=r["seq"], ts=r["ts"])
            out.append(rec)
        return out

    # --------------------------------------------------------------- locks
    def lock(self, key: str, ttl: timedelta) -> bool:
        """Acquire an invoice-scoped lock. Returns False if already held and unexpired."""
        now = utcnow()
        with self._lock:
            row = self._conn.execute("SELECT expires_at FROM locks WHERE key=?", (key,)).fetchone()
            if row:
                exp = from_iso(row["expires_at"])
                if exp and exp > now:
                    return False
            self._conn.execute(
                "INSERT INTO locks(key,expires_at) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET expires_at=excluded.expires_at",
                (key, to_iso(now + ttl)),
            )
            self._commit()
            return True

    def is_locked(self, key: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT expires_at FROM locks WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        exp = from_iso(row["expires_at"])
        return bool(exp and exp > utcnow())

    def unlock(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM locks WHERE key=?", (key,))
            self._commit()

    def retry_due_within(self, mandate: Mandate, hours: int, now: datetime | None = None) -> bool:
        """True if an automatic retry is already scheduled inside the window."""
        from grace.models import SubStatus

        if mandate.status != SubStatus.PENDING or mandate.charge_at is None:
            return False
        now = ensure_aware(now) or utcnow()
        return now <= mandate.charge_at <= now + timedelta(hours=hours)

    # ----------------------------------------------------------- sim state
    def set_sim_state(self, mandate_id: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sim_state(mandate_id,json) VALUES(?,?) "
                "ON CONFLICT(mandate_id) DO UPDATE SET json=excluded.json",
                (mandate_id, json.dumps(payload, default=str)),
            )
            self._commit()

    def get_sim_state(self, mandate_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM sim_state WHERE mandate_id=?", (mandate_id,)
            ).fetchone()
        return json.loads(row["json"]) if row else {}

    # ------------------------------------------------------------ feedback
    def append_feedback(self, mandate_id: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO feedback(mandate_id,json) VALUES(?,?)",
                (mandate_id, json.dumps(payload, default=str)),
            )
            self._commit()
