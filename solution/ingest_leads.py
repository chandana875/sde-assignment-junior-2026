import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solution.schemas import normalize_header
from solution.security import encrypt_pii, hash_phone, mask_phone
from solution.validators import parse_field_value


def ingest_lead_file(
    conn: sqlite3.Connection,
    csv_path: Path,
    workflow_id: str,
    schema: dict[str, Any],
    file_key: str,
) -> None:
    """Ingests one lead CSV file idempotently, validating, rejecting, and merging leads."""
    existing = conn.execute(
        "SELECT 1 FROM ingested_files WHERE file_key = ?", (file_key,)
    ).fetchone()
    if existing:
        return

    batch_id = csv_path.stem
    alias_map: dict[str, str] = schema["_alias_map"]
    ref_field: str = schema["_reference_field"]
    fields_spec: list[dict[str, Any]] = schema["fields"]

    stats = {
        "lead_rows_total": 0,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
    }
    rejections: list[tuple[str, str, int, str]] = []

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            raw_headers = next(reader)
        except StopIteration:
            raw_headers = []

        # Map column index -> canonical schema field name (ignore unknown columns)
        col_to_field: dict[int, str] = {}
        for idx, h in enumerate(raw_headers):
            norm = normalize_header(h)
            if norm in alias_map and alias_map[norm] not in col_to_field.values():
                col_to_field[idx] = alias_map[norm]

        row_number = 0
        for raw_row in reader:
            # Skip blank rows before incrementing row_number
            if all(cell.strip() == "" for cell in raw_row):
                continue

            row_number += 1
            stats["lead_rows_total"] += 1

            # Extract trimmed raw values by canonical field name
            raw_values: dict[str, str] = {}
            for idx, fname in col_to_field.items():
                val = raw_row[idx].strip() if idx < len(raw_row) else ""
                raw_values[fname] = val

            # Validate in schema order
            rejection_reason: str | None = None
            row_warnings = 0
            parsed_values: dict[str, Any] = {}

            for fspec in fields_spec:
                fname = fspec["name"]
                is_req = bool(fspec.get("required", False))
                raw_val = raw_values.get(fname, "")

                if raw_val == "":
                    if is_req:
                        rejection_reason = f"missing_required:{fname}"
                        break
                    parsed_values[fname] = None
                    continue

                is_valid, norm_val = parse_field_value(raw_val, fspec)
                if not is_valid:
                    if fspec["type"] == "phone":
                        rejection_reason = "invalid_phone"
                        break
                    if is_req:
                        rejection_reason = f"invalid_{fspec['type']}:{fname}"
                        break
                    # Optional field present but invalid -> store null + 1 warning
                    parsed_values[fname] = None
                    row_warnings += 1
                else:
                    parsed_values[fname] = norm_val

            if rejection_reason is not None:
                rejections.append((workflow_id, batch_id, row_number, rejection_reason))
                continue

            stats["field_warnings"] += row_warnings

            canonical_phone: str = parsed_values["phone"]
            p_hash = hash_phone(canonical_phone)
            p_masked = mask_phone(canonical_phone)

            # Protect PII fields at rest
            stored_fields: dict[str, Any] = {}
            for fspec in fields_spec:
                fname = fspec["name"]
                if fname == "phone":
                    continue
                val = parsed_values.get(fname)
                if val is not None and fspec.get("pii"):
                    stored_fields[fname] = encrypt_pii(str(val))
                else:
                    stored_fields[fname] = val

            ext_ref = str(parsed_values[ref_field])

            existing_lead = conn.execute(
                "SELECT batch_id, external_ref, fields_json FROM leads WHERE workflow_id = ? AND phone_hash = ?",
                (workflow_id, p_hash),
            ).fetchone()

            if existing_lead is None:
                conn.execute(
                    """
                    INSERT INTO leads (workflow_id, phone_hash, batch_id, external_ref, phone_masked, fields_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        p_hash,
                        batch_id,
                        ext_ref,
                        p_masked,
                        json.dumps(stored_fields, ensure_ascii=False),
                    ),
                )
            else:
                stats["duplicate_lead_rows_merged"] += 1
                merged_fields = json.loads(existing_lead["fields_json"])
                for k, v in stored_fields.items():
                    if v is not None:
                        merged_fields[k] = v
                updated_ref = ext_ref if parsed_values.get(ref_field) is not None else existing_lead["external_ref"]
                conn.execute(
                    """
                    UPDATE leads
                    SET external_ref = ?, fields_json = ?
                    WHERE workflow_id = ? AND phone_hash = ?
                    """,
                    (
                        updated_ref,
                        json.dumps(merged_fields, ensure_ascii=False),
                        workflow_id,
                        p_hash,
                    ),
                )

    if rejections:
        conn.executemany(
            "INSERT INTO lead_rejections (workflow_id, batch_id, row_number, reason) VALUES (?, ?, ?, ?)",
            rejections,
        )

    conn.execute(
        """
        INSERT INTO ingested_files (file_key, file_type, stats_json, ingested_at)
        VALUES (?, 'lead', ?, ?)
        """,
        (file_key, json.dumps(stats), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()