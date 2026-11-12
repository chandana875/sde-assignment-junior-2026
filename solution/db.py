import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get("PIPELINE_DB_PATH", "pipeline.db"))


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingested_files (
            file_key TEXT PRIMARY KEY,
            file_type TEXT NOT NULL,
            stats_json TEXT NOT NULL,
            ingested_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS leads (
            workflow_id TEXT NOT NULL,
            phone_hash TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            external_ref TEXT NOT NULL,
            phone_masked TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            PRIMARY KEY (workflow_id, phone_hash)
        );

        CREATE TABLE IF NOT EXISTS lead_rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            reason TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            phone_hash TEXT,
            occurred_at_utc TEXT NOT NULL,
            occurred_at_ist TEXT NOT NULL,
            occurred_date_ist TEXT NOT NULL,
            disposition_code TEXT,
            is_known_disposition INTEGER,
            payload_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_wf_call ON events (workflow_id, call_id);
        CREATE INDEX IF NOT EXISTS idx_events_wf_phone ON events (workflow_id, phone_hash);
        """
    )
    conn.commit()