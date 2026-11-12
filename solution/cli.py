import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from solution.db import DEFAULT_DB_PATH, get_connection
from solution.ingest_events import ingest_event_file
from solution.ingest_leads import ingest_lead_file
from solution.reporting import generate_reports
from solution.schemas import load_dispositions, load_workflow_schemas


def apply_schema_defaults(conn: sqlite3.Connection, schemas: dict[str, dict[str, Any]]) -> None:
    """Applies schema defaults to fields that are still null after merging across batches."""
    for wf_id, schema in schemas.items():
        defaults = {
            f["name"]: f["default"]
            for f in schema["fields"]
            if "default" in f and f["name"] != "phone"
        }
        if not defaults:
            continue

        rows = conn.execute(
            "SELECT phone_hash, fields_json FROM leads WHERE workflow_id = ?", (wf_id,)
        ).fetchall()
        for row in rows:
            fields = json.loads(row["fields_json"])
            changed = False
            for k, default_val in defaults.items():
                if fields.get(k) is None:
                    fields[k] = default_val
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE leads SET fields_json = ? WHERE workflow_id = ? AND phone_hash = ?",
                    (json.dumps(fields, ensure_ascii=False), wf_id, row["phone_hash"]),
                )
    conn.commit()


def run_ingest(drop_dir: Path, db_path: Path) -> None:
    if not drop_dir.exists():
        raise FileNotFoundError(f"Drop directory not found: {drop_dir}")

    schemas = load_workflow_schemas()
    dispositions = load_dispositions()
    conn = get_connection(db_path)

    try:
        leads_root = drop_dir / "leads"
        if leads_root.exists():
            for wf_dir in sorted(p for p in leads_root.iterdir() if p.is_dir()):
                wf_id = wf_dir.name
                if wf_id not in schemas:
                    continue
                for csv_file in sorted(wf_dir.glob("*.csv")):
                    file_key = f"{drop_dir.resolve()}/leads/{wf_id}/{csv_file.name}"
                    ingest_lead_file(conn, csv_file, wf_id, schemas[wf_id], file_key)

        events_root = drop_dir / "events"
        if events_root.exists():
            for jsonl_file in sorted(events_root.glob("*.jsonl")):
                file_key = f"{drop_dir.resolve()}/events/{jsonl_file.name}"
                ingest_event_file(conn, jsonl_file, dispositions, file_key)

        apply_schema_defaults(conn, schemas)
    finally:
        conn.close()


def run_report(out_dir: Path, db_path: Path) -> None:
    dispositions = load_dispositions()
    conn = get_connection(db_path)
    try:
        generate_reports(conn, out_dir, dispositions)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Interaction Ledger & Reporting Pipeline")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Path to SQLite database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest a drop directory")
    ingest_parser.add_argument("drop_path", type=Path, help="Path to drop folder (e.g., data/drop_1)")

    report_parser = subparsers.add_parser("report", help="Generate output reports")
    report_parser.add_argument("--out", type=Path, required=True, help="Output directory for reports")

    args = parser.parse_args()
    if args.command == "ingest":
        run_ingest(args.drop_path, args.db)
    elif args.command == "report":
        run_report(args.out, args.db)


if __name__ == "__main__":
    main()