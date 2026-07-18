"""SQLite time-series store.

SQLite (WAL mode) keeps the project runnable anywhere with zero services.
The `Store` API is deliberately narrow so the backend can be swapped for
TimescaleDB/ClickHouse in production without touching callers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY,
    vin TEXT NOT NULL,
    ts REAL NOT NULL,
    message TEXT NOT NULL,
    signals TEXT NOT NULL              -- JSON: {signal: value}
);
CREATE INDEX IF NOT EXISTS idx_tel_vin_msg_ts ON telemetry (vin, message, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    vin TEXT NOT NULL,
    ts REAL NOT NULL,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_vin_ts ON alerts (vin, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_dedupe ON alerts (vin, ts, code);

CREATE TABLE IF NOT EXISTS vehicle_health (
    vin TEXT PRIMARY KEY,
    soh_pct REAL,
    rul_cycles REAL,
    rul_km REAL,
    rul_days REAL,
    cycles REAL,
    odometer_km REAL,
    last_seen REAL,
    anomaly_rate_pct REAL,
    status TEXT NOT NULL DEFAULT 'unknown'
);
"""


class Store:
    """Thread-safe wrapper around a SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- writes
    def insert_telemetry(self, rows: list[tuple[str, float, str, dict[str, float]]]) -> None:
        """rows: (vin, ts, message_name, signals)"""
        with self._lock:
            self._conn.executemany(
                "INSERT INTO telemetry (vin, ts, message, signals) VALUES (?, ?, ?, ?)",
                [(vin, ts, msg, json.dumps(sig)) for vin, ts, msg, sig in rows],
            )
            self._conn.commit()

    def insert_alerts(self, alerts: list[dict]) -> int:
        """Insert alerts, silently skipping duplicates (vin, ts, code)."""
        inserted = 0
        with self._lock:
            for a in alerts:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO alerts (vin, ts, severity, code, message, value) "
                    "VALUES (:vin, :ts, :severity, :code, :message, :value)",
                    a,
                )
                inserted += cur.rowcount
            self._conn.commit()
        return inserted

    def upsert_health(self, health: dict) -> None:
        cols = (
            "vin, soh_pct, rul_cycles, rul_km, rul_days, cycles, "
            "odometer_km, last_seen, anomaly_rate_pct, status"
        )
        named = ", ".join(f":{c.strip()}" for c in cols.split(","))
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO vehicle_health ({cols}) VALUES ({named})", health
            )
            self._conn.commit()

    # ----------------------------------------------------------------- reads
    def signal_series(
        self, vin: str, message: str, since: float = 0.0, limit: int = 500_000
    ) -> list[tuple[float, dict[str, float]]]:
        cur = self._conn.execute(
            "SELECT ts, signals FROM telemetry WHERE vin=? AND message=? AND ts>=? "
            "ORDER BY ts LIMIT ?",
            (vin, message, since, limit),
        )
        return [(ts, json.loads(sig)) for ts, sig in cur.fetchall()]

    def vins(self) -> list[str]:
        cur = self._conn.execute("SELECT DISTINCT vin FROM telemetry ORDER BY vin")
        return [r[0] for r in cur.fetchall()]

    def frame_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]

    def alerts_for(self, vin: str, limit: int = 200) -> list[dict]:
        cur = self._conn.execute(
            "SELECT vin, ts, severity, code, message, value FROM alerts "
            "WHERE vin=? ORDER BY ts DESC LIMIT ?",
            (vin, limit),
        )
        keys = ["vin", "timestamp", "severity", "code", "message", "value"]
        return [dict(zip(keys, row)) for row in cur.fetchall()]

    def open_alert_count(self, vin: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE vin=?", (vin,)
        ).fetchone()[0]

    def health_all(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM vehicle_health ORDER BY vin")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def health_for(self, vin: str) -> dict | None:
        cur = self._conn.execute("SELECT * FROM vehicle_health WHERE vin=?", (vin,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
