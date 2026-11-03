"""SQLite persistence with transactional helpers for concurrent bidding."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "prebidding.db"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "photos").mkdir(parents=True, exist_ok=True)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    ensure_data_dir()
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    google_sub TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('Bidder', 'Administrator'))
);

CREATE TABLE IF NOT EXISTS bid_items (
    item_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    photograph_ref TEXT NOT NULL,
    initial_bid_amount REAL NOT NULL CHECK (initial_bid_amount >= 0),
    status TEXT NOT NULL CHECK (status IN ('Upcoming', 'Open', 'Closed')),
    opening_at TEXT,
    closing_at TEXT,
    event_timezone TEXT NOT NULL DEFAULT 'America/Chicago',
    published INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bids (
    bid_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES bid_items(item_id),
    user_id TEXT NOT NULL REFERENCES users(google_sub),
    amount REAL NOT NULL CHECK (amount >= 0),
    recorded_sequence INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('Accepted', 'Corrected', 'Cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_bids_item ON bids(item_id);
CREATE INDEX IF NOT EXISTS idx_bids_item_user ON bids(item_id, user_id);

CREATE TABLE IF NOT EXISTS bid_audits (
    audit_id TEXT PRIMARY KEY,
    bid_id TEXT NOT NULL REFERENCES bids(bid_id),
    action TEXT NOT NULL CHECK (action IN ('Correct', 'Cancel')),
    original_amount REAL,
    revised_amount REAL,
    reason TEXT NOT NULL,
    administrator_id TEXT NOT NULL REFERENCES users(google_sub),
    action_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_schedules (
    item_id TEXT PRIMARY KEY REFERENCES bid_items(item_id),
    opening_at TEXT,
    closing_at TEXT,
    closure_type TEXT,
    closed_by_user_id TEXT,
    closed_at TEXT,
    timezone TEXT NOT NULL DEFAULT 'America/Chicago'
);

CREATE TABLE IF NOT EXISTS item_synchronizations (
    sync_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error_detail TEXT,
    items_imported INTEGER DEFAULT 0,
    items_rejected INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sequence_counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate_bids_allow_rebids(conn: sqlite3.Connection) -> None:
    """Drop legacy UNIQUE(item_id, user_id) so outbid users can place another bid."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bids'"
    ).fetchone()
    if row is None:
        return
    table_sql = row["sql"] if isinstance(row, sqlite3.Row) else row[0]
    if not table_sql or "UNIQUE (item_id, user_id)" not in table_sql:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            CREATE TABLE bids_rebids_mig (
                bid_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL REFERENCES bid_items(item_id),
                user_id TEXT NOT NULL REFERENCES users(google_sub),
                amount REAL NOT NULL CHECK (amount >= 0),
                recorded_sequence INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('Accepted', 'Corrected', 'Cancelled'))
            );
            INSERT INTO bids_rebids_mig (
                bid_id, item_id, user_id, amount, recorded_sequence, recorded_at, status
            )
            SELECT bid_id, item_id, user_id, amount, recorded_sequence, recorded_at, status
            FROM bids;
            DROP TABLE bids;
            ALTER TABLE bids_rebids_mig RENAME TO bids;
            CREATE INDEX IF NOT EXISTS idx_bids_item ON bids(item_id);
            CREATE INDEX IF NOT EXISTS idx_bids_item_user ON bids(item_id, user_id);
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO sequence_counters(name, value) VALUES ('bid_sequence', 0)"
    )
    _migrate_bids_allow_rebids(conn)
