import csv
import json
import sqlite3
from pathlib import Path
from typing import Any


def generate_reports(
    conn: sqlite3.Connection,
    out_dir: Path,
    dispositions: dict[str, dict[str, dict[str, bool]]],
) -> None:
    """Computes all derived metrics and writes the 5 exact contract files."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load all valid leads
    lead_rows = conn.execute(
        "SELECT workflow_id, phone_hash, batch_id, external_ref, phone_masked FROM leads"
    ).fetchall()
    leads: dict[tuple[str, str], dict[str, Any]] = {}
    for r in lead_rows:
        key = (r["workflow_id"], r["phone_hash"])
        leads[key] = {
            "workflow_id": r["workflow_id"],
            "phone_hash": r["phone_hash"],
            "batch_id": r["batch_id"],
            "external_ref": r["external_ref"],
            "phone_masked": r["phone_masked"],
            "calls": [],
        }

    # 2. Load all events in chronological order
    event_rows = conn.execute(
        """
        SELECT event_id, workflow_id, call_id, event_type, phone_hash,
               occurred_at_utc, occurred_at_ist, occurred_date_ist,
               disposition_code, is_known_disposition
        FROM events
        ORDER BY occurred_at_utc ASC, rowid ASC
        """
    ).fetchall()

    # Resolve (workflow_id, call_id) -> phone_hash from any event in that call that has one
    call_to_phone: dict[tuple[str, str], str] = {}
    for ev in event_rows:
        if ev["phone_hash"]:
            call_to_phone[(ev["workflow_id"], ev["call_id"])] = ev["phone_hash"]

    # Filter linked events vs orphans & count unknown dispositions
    orphan_events = 0
    unknown_disposition_events = 0
    events_by_call: dict[tuple[str, str], list[sqlite3.Row]] = {}

    for ev in event_rows:
        wf_id = ev["workflow_id"]
        c_key = (wf_id, ev["call_id"])
        p_hash = ev["phone_hash"] or call_to_phone.get(c_key)

        if not p_hash or (wf_id, p_hash) not in leads:
            orphan_events += 1
            continue

        if ev["event_type"] in ("disposition.set", "disposition.override") and ev["is_known_disposition"] == 0:
            unknown_disposition_events += 1
            continue

        events_by_call.setdefault(c_key, []).append(ev)

    # 3. Build call-level derived facts (only for calls with call.initiated)
    # daily_stats[(workflow_id, date_ist)] -> {'total_calls': int, 'connected_calls': int, 'success_leads': set}
    daily_stats: dict[tuple[str, str], dict[str, Any]] = {}

    for (wf_id, call_id), evs in events_by_call.items():
        initiated_evs = [e for e in evs if e["event_type"] == "call.initiated"]
        if not initiated_evs:
            continue

        init_ev = initiated_evs[0]
        call_date = init_ev["occurred_date_ist"]
        call_initiated_utc = init_ev["occurred_at_utc"]
        call_initiated_ist = init_ev["occurred_at_ist"]

        is_connected = any(e["event_type"] == "call.connected" for e in evs)
        has_telephony_failed = any(e["event_type"] == "call.telephony_failed" for e in evs)

        overrides = [e for e in evs if e["event_type"] == "disposition.override" and e["disposition_code"]]
        sets = [e for e in evs if e["event_type"] == "disposition.set" and e["disposition_code"]]

        if overrides:
            effective_disp = overrides[-1]["disposition_code"]
        elif sets:
            effective_disp = sets[-1]["disposition_code"]
        else:
            effective_disp = None

        disp_meta = dispositions.get(wf_id, {}).get(effective_disp or "", {})
        is_terminal = bool(disp_meta.get("is_terminal", False))
        is_success = bool(disp_meta.get("is_success", False))

        p_hash = call_to_phone[(wf_id, call_id)]
        lead = leads[(wf_id, p_hash)]
        lead["calls"].append(
            {
                "call_id": call_id,
                "initiated_utc": call_initiated_utc,
                "initiated_ist": call_initiated_ist,
                "date_ist": call_date,
                "is_connected": is_connected,
                "has_telephony_failed": has_telephony_failed,
                "effective_disp": effective_disp,
                "is_terminal": is_terminal,
                "is_success": is_success,
            }
        )

        d_key = (wf_id, call_date)
        d_entry = daily_stats.setdefault(
            d_key, {"total_calls": 0, "connected_calls": 0, "success_leads": set()}
        )
        d_entry["total_calls"] += 1
        if is_connected:
            d_entry["connected_calls"] += 1
        if is_success:
            d_entry["success_leads"].add(p_hash)

    # 4. Compute lead-level summaries and batch summaries
    lead_report_rows: list[list[str]] = []
    batch_counts: dict[tuple[str, str], dict[str, int]] = {}

    for (wf_id, _), lead in leads.items():
        calls = sorted(lead["calls"], key=lambda c: c["initiated_utc"])
        total_calls = len(calls)
        connected_calls = sum(1 for c in calls if c["is_connected"])

        if total_calls == 0:
            bucket = "yet_to_reach_out"
        elif any(c["is_terminal"] for c in calls):
            bucket = "completed"
        elif all(c["has_telephony_failed"] for c in calls):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"

        last_call_at = calls[-1]["initiated_ist"] if total_calls > 0 else ""

        # Effective disposition of the most recent call (by initiated time) that has one
        current_disp = ""
        for c in reversed(calls):
            if c["effective_disp"]:
                current_disp = c["effective_disp"]
                break

        ever_success = any(c["is_success"] for c in calls)

        lead_report_rows.append(
            [
                wf_id,
                lead["external_ref"],
                lead["batch_id"],
                lead["phone_masked"],
                bucket,
                str(total_calls),
                str(connected_calls),
                last_call_at,
                current_disp,
                "true" if ever_success else "false",
            ]
        )

        b_key = (wf_id, lead["batch_id"])
        b_entry = batch_counts.setdefault(
            b_key,
            {
                "total_leads": 0,
                "yet_to_reach_out": 0,
                "in_progress_telephony_failed": 0,
                "in_progress_dialed": 0,
                "completed": 0,
            },
        )
        b_entry["total_leads"] += 1
        b_entry[bucket] += 1

    # Write 1: batch_summary.csv (sorted by workflow_id, batch_id)
    with (out_dir / "batch_summary.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "workflow_id",
                "batch_id",
                "total_leads",
                "yet_to_reach_out",
                "in_progress_telephony_failed",
                "in_progress_dialed",
                "completed",
            ]
        )
        for wf_id, batch_id in sorted(batch_counts.keys()):
            c = batch_counts[(wf_id, batch_id)]
            writer.writerow(
                [
                    wf_id,
                    batch_id,
                    c["total_leads"],
                    c["yet_to_reach_out"],
                    c["in_progress_telephony_failed"],
                    c["in_progress_dialed"],
                    c["completed"],
                ]
            )

    # Write 2: daily_overview.csv (sorted by workflow_id, date)
    with (out_dir / "daily_overview.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "workflow_id",
                "date",
                "total_calls",
                "connected_calls",
                "connectivity_rate",
                "success_leads",
            ]
        )
        for wf_id, date_str in sorted(daily_stats.keys()):
            d = daily_stats[(wf_id, date_str)]
            rate = d["connected_calls"] / d["total_calls"] if d["total_calls"] else 0.0
            writer.writerow(
                [
                    wf_id,
                    date_str,
                    d["total_calls"],
                    d["connected_calls"],
                    f"{rate:.4f}",
                    len(d["success_leads"]),
                ]
            )

    # Write 3: lead_report.csv (sorted by workflow_id, external_ref)
    lead_report_rows.sort(key=lambda r: (r[0], r[1]))
    with (out_dir / "lead_report.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "workflow_id",
                "external_ref",
                "batch_id",
                "phone_masked",
                "bucket",
                "total_calls",
                "connected_calls",
                "last_call_at",
                "current_disposition",
                "ever_success",
            ]
        )
        writer.writerows(lead_report_rows)

    # Write 4: rejections.csv (sorted by workflow_id, batch_id, row_number)
    rej_rows = conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM lead_rejections
        ORDER BY workflow_id ASC, batch_id ASC, row_number ASC
        """
    ).fetchall()

    rejections_by_reason: dict[str, int] = {}
    with (out_dir / "rejections.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
        for r in rej_rows:
            reason = r["reason"]
            rejections_by_reason[reason] = rejections_by_reason.get(reason, 0) + 1
            writer.writerow([r["workflow_id"], r["batch_id"], r["row_number"], reason])

    # Write 5: data_quality.json
    dq = {
        "lead_rows_total": 0,
        "lead_rows_rejected": len(rej_rows),
        "rejections_by_reason": dict(sorted(rejections_by_reason.items())),
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
        "orphan_events": orphan_events,
        "unknown_disposition_events": unknown_disposition_events,
    }

    for f_row in conn.execute("SELECT file_type, stats_json FROM ingested_files").fetchall():
        st = json.loads(f_row["stats_json"])
        if f_row["file_type"] == "lead":
            dq["lead_rows_total"] += st.get("lead_rows_total", 0)
            dq["duplicate_lead_rows_merged"] += st.get("duplicate_lead_rows_merged", 0)
            dq["field_warnings"] += st.get("field_warnings", 0)
        elif f_row["file_type"] == "event":
            dq["events_total"] += st.get("events_total", 0)
            dq["malformed_events"] += st.get("malformed_events", 0)
            dq["duplicate_events_dropped"] += st.get("duplicate_events_dropped", 0)

    (out_dir / "data_quality.json").write_text(
        json.dumps(dq, indent=2) + "\n", encoding="utf-8"
    )