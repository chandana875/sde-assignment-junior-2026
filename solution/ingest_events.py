import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from solution.security import hash_phone
from solution.validators import validate_phone

IST = ZoneInfo("Asia/Kolkata")


def parse_timestamp(raw: str) -> tuple[str, str, str]:
    """
    Parses ISO timestamps in UTC (...Z), offset (...+05:30), or naive (assumed IST).
    Returns (utc_iso, ist_iso, ist_date_yyyy_mm_dd).
    """
    s = raw.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    dt_utc = dt.astimezone(timezone.utc)
    dt_ist = dt.astimezone(IST)
    return (
        dt_utc.isoformat(),
        dt_ist.strftime("%Y-%m-%dT%H:%M:%S%z")[:-2] + ":30",
        dt_ist.strftime("%Y-%m-%d"),
    )


def ingest_event_file(
    conn: sqlite3.Connection,
    jsonl_path: Path,
    dispositions: dict[str, dict[str, dict[str, bool]]],
    file_key: str,
) -> None:
    """Ingests one JSONL event file into the append-only ledger idempotently."""
    existing = conn.execute(
        "SELECT 1 FROM ingested_files WHERE file_key = ?", (file_key,)
    ).fetchone()
    if existing:
        return

    # Pre-load existing event_ids into memory for O(1) deduplication across drops
    seen_event_ids: set[str] = {
        row[0] for row in conn.execute("SELECT event_id FROM events").fetchall()
    }

    stats = {
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
    }
    rows_to_insert: list[tuple[Any, ...]] = []

    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue

            stats["events_total"] += 1

            # 1. Malformed check
            try:
                evt = json.loads(stripped)
            except json.JSONDecodeError:
                stats["malformed_events"] += 1
                continue

            if not isinstance(evt, dict):
                stats["malformed_events"] += 1
                continue

            event_id = evt.get("event_id")
            if not event_id or not isinstance(event_id, str) or not event_id.strip():
                stats["malformed_events"] += 1
                continue
            event_id = event_id.strip()

            event_type = evt.get("event_type", "")
            workflow_id = evt.get("workflow_id", "")
            call_id = evt.get("call_id", "")
            occurred_at_raw = evt.get("occurred_at", "")
            if not (event_type and workflow_id and call_id and occurred_at_raw):
                stats["malformed_events"] += 1
                continue

            try:
                utc_iso, ist_iso, ist_date = parse_timestamp(str(occurred_at_raw))
            except ValueError:
                stats["malformed_events"] += 1
                continue

            # 2. Duplicate check across all drops
            if event_id in seen_event_ids:
                stats["duplicate_events_dropped"] += 1
                continue
            seen_event_ids.add(event_id)

            # Normalise phone if present
            raw_phone = evt.get("phone")
            p_hash: str | None = None
            if raw_phone and isinstance(raw_phone, str):
                canon = validate_phone(raw_phone)
                if canon:
                    p_hash = hash_phone(canon)

            payload: dict[str, Any] = evt.get("payload") or {}
            disp_code: str | None = None
            is_known_disp: int | None = None

            if event_type in ("disposition.set", "disposition.override"):
                raw_code = payload.get("disposition_code")
                if raw_code is not None:
                    disp_code = str(raw_code).strip()
                    wf_disps = dispositions.get(workflow_id, {})
                    is_known_disp = 1 if disp_code in wf_disps else 0
                else:
                    is_known_disp = 0

            rows_to_insert.append(
                (
                    event_id,
                    workflow_id,
                    call_id,
                    event_type,
                    p_hash,
                    utc_iso,
                    ist_iso,
                    ist_date,
                    disp_code,
                    is_known_disp,
                    json.dumps(payload, ensure_ascii=False),
                )
            )

    if rows_to_insert:
        conn.executemany(
            """
            INSERT INTO events (
                event_id, workflow_id, call_id, event_type, phone_hash,
                occurred_at_utc, occurred_at_ist, occurred_date_ist,
                disposition_code, is_known_disposition, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )

    conn.execute(
        """
        INSERT INTO ingested_files (file_key, file_type, stats_json, ingested_at)
        VALUES (?, 'event', ?, ?)
        """,
        (file_key, json.dumps(stats), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()