"""SQLite persistence with full snapshot history.

Every execution writes a `runs` row and a batch of `price_snapshots` rows.
Nothing is ever overwritten, so run-over-run comparison (drift, gaps) is a
plain SQL query against history rather than a diff held in memory.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from .api_client import PriceRecord
from .utils import get_logger, utc_now_iso

log = get_logger("db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    source            TEXT NOT NULL,          -- 'api' | 'api+injected'
    note              TEXT,
    record_count      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    captured_at           TEXT NOT NULL,
    service_name          TEXT NOT NULL,
    service_family        TEXT,
    arm_region_name       TEXT NOT NULL,
    location              TEXT,
    arm_sku_name          TEXT NOT NULL,
    sku_name              TEXT,
    product_name          TEXT,
    meter_id              TEXT NOT NULL,
    meter_name            TEXT,
    unit_of_measure       TEXT NOT NULL,
    price_type            TEXT NOT NULL,
    retail_price          REAL NOT NULL,
    unit_price            REAL,
    currency_code         TEXT,
    effective_start_date  TEXT,
    effective_end_date    TEXT,
    tier_minimum_units    REAL NOT NULL DEFAULT 0,
    is_primary_meter_region INTEGER DEFAULT 0,
    is_synthetic          INTEGER NOT NULL DEFAULT 0   -- 1 only for injected demo rows
);

-- Natural key: sku + region + meter + price type + unit + pricing tier.
CREATE INDEX IF NOT EXISTS ix_snapshot_key
    ON price_snapshots (arm_sku_name, arm_region_name, meter_id, price_type,
                        unit_of_measure, tier_minimum_units);
CREATE INDEX IF NOT EXISTS ix_snapshot_run ON price_snapshots (run_id);

CREATE TABLE IF NOT EXISTS ui_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    captured_at     TEXT NOT NULL,
    source_page     TEXT NOT NULL,
    search_path     TEXT NOT NULL,
    service_name    TEXT NOT NULL,
    arm_region_name TEXT NOT NULL,
    arm_sku_name    TEXT NOT NULL,
    raw_price_text  TEXT,
    observed_price  REAL,
    currency_code   TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    detected_at   TEXT NOT NULL,
    rule_id       TEXT NOT NULL,
    severity      TEXT NOT NULL,
    arm_sku_name  TEXT,
    arm_region_name TEXT,
    meter_id      TEXT,
    meter_name    TEXT,
    previous_value TEXT,
    current_value  TEXT,
    delta_pct     REAL,
    details       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_findings_run ON findings (run_id);
"""

SNAPSHOT_COLUMNS = (
    "run_id", "captured_at", "service_name", "service_family", "arm_region_name",
    "location", "arm_sku_name", "sku_name", "product_name", "meter_id", "meter_name",
    "unit_of_measure", "price_type", "retail_price", "unit_price", "currency_code",
    "effective_start_date", "effective_end_date", "tier_minimum_units",
    "is_primary_meter_region", "is_synthetic",
)


class PriceDatabase:
    """All SQLite access goes through here."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------------- runs
    def start_run(self, run_id: str, source: str = "api", note: str = "") -> str:
        self.conn.execute(
            "INSERT INTO runs (run_id, started_at, source, note) VALUES (?, ?, ?, ?)",
            (run_id, utc_now_iso(), source, note),
        )
        self.conn.commit()
        log.info("Run %s started (source=%s)", run_id, source)
        return run_id

    def finish_run(self, run_id: str) -> None:
        self.conn.execute(
            """UPDATE runs
               SET finished_at = ?,
                   record_count = (SELECT COUNT(*) FROM price_snapshots WHERE run_id = ?)
               WHERE run_id = ?""",
            (utc_now_iso(), run_id, run_id),
        )
        self.conn.commit()

    def list_runs(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM runs ORDER BY started_at ASC"))

    def previous_run_id(self, run_id: str) -> str | None:
        """The run immediately before this one.

        Ordered by insertion (rowid), not by `started_at`: two runs launched
        within the same second share a timestamp, and a timestamp comparison
        silently returns no baseline - which would make every rule pass.
        """
        row = self.conn.execute(
            """SELECT run_id FROM runs
               WHERE rowid < (SELECT rowid FROM runs WHERE run_id = ?)
               ORDER BY rowid DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        return row["run_id"] if row else None

    # ----------------------------------------------------------- snapshots
    def insert_records(
        self,
        run_id: str,
        records: Iterable[PriceRecord],
        captured_at: str | None = None,
        is_synthetic: bool = False,
    ) -> int:
        stamp = captured_at or utc_now_iso()
        rows = [
            (
                run_id, stamp, r.service_name, r.service_family, r.arm_region_name,
                r.location, r.arm_sku_name, r.sku_name, r.product_name, r.meter_id,
                r.meter_name, r.unit_of_measure, r.price_type, r.retail_price,
                r.unit_price, r.currency_code, r.effective_start_date,
                r.effective_end_date, r.tier_minimum_units,
                r.is_primary_meter_region, int(is_synthetic),
            )
            for r in records
        ]
        placeholders = ", ".join("?" * len(SNAPSHOT_COLUMNS))
        self.conn.executemany(
            f"INSERT INTO price_snapshots ({', '.join(SNAPSHOT_COLUMNS)}) "
            f"VALUES ({placeholders})",
            rows,
        )
        self.conn.commit()
        log.info("Inserted %s snapshot rows for run %s", len(rows), run_id)
        return len(rows)

    def snapshot_count(self, run_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM price_snapshots WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row["n"])

    def adjust_price(
        self, run_id: str, arm_sku_name: str, meter_id: str, factor: float
    ) -> int:
        """Apply an INTENTIONAL, clearly-flagged price change (demo only).

        Rows touched here are marked `is_synthetic = 1` so no report can pass
        them off as data that came from Azure.
        """
        cur = self.conn.execute(
            """UPDATE price_snapshots
               SET retail_price = retail_price * ?,
                   unit_price   = unit_price * ?,
                   is_synthetic = 1
               WHERE run_id = ? AND arm_sku_name = ? AND meter_id = ?""",
            (factor, factor, run_id, arm_sku_name, meter_id),
        )
        self.conn.commit()
        log.warning(
            "INTENTIONAL price change: run=%s sku=%s meter=%s factor=%.3f (%s row(s))",
            run_id, arm_sku_name, meter_id, factor, cur.rowcount,
        )
        return cur.rowcount

    def delete_meter(self, run_id: str, meter_id: str) -> int:
        """Remove a meter from a run to simulate a catalogue gap (demo only)."""
        cur = self.conn.execute(
            "DELETE FROM price_snapshots WHERE run_id = ? AND meter_id = ?",
            (run_id, meter_id),
        )
        self.conn.commit()
        return cur.rowcount

    # -------------------------------------------------------------- UI obs
    def insert_ui_observation(self, run_id: str, observation: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO ui_observations
               (run_id, captured_at, source_page, search_path, service_name,
                arm_region_name, arm_sku_name, raw_price_text, observed_price,
                currency_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, observation.get("captured_at", utc_now_iso()),
                observation.get("source_page", ""), observation.get("search_path", ""),
                observation.get("service_name", ""), observation.get("arm_region_name", ""),
                observation.get("arm_sku_name", ""), observation.get("raw_price_text"),
                observation.get("observed_price"), observation.get("currency_code", "USD"),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------ findings
    def insert_findings(self, findings: Sequence[dict[str, Any]]) -> int:
        if not findings:
            return 0
        self.conn.executemany(
            """INSERT INTO findings
               (run_id, detected_at, rule_id, severity, arm_sku_name, arm_region_name,
                meter_id, meter_name, previous_value, current_value, delta_pct, details)
               VALUES (:run_id, :detected_at, :rule_id, :severity, :arm_sku_name,
                       :arm_region_name, :meter_id, :meter_name, :previous_value,
                       :current_value, :delta_pct, :details)""",
            findings,
        )
        self.conn.commit()
        return len(findings)

    def findings_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM findings WHERE run_id = ? ORDER BY rule_id, arm_sku_name",
                (run_id,),
            )
        )

    def query(self, sql: str, params: dict[str, Any] | tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PriceDatabase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
