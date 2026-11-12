import json
import sqlite3
from pathlib import Path

import pytest

from solution.cli import run_ingest, run_report
from solution.validators import validate_date, validate_decimal, validate_enum, validate_phone


def test_phone_normalization():
    assert validate_phone("9876543210") == "+919876543210"
    assert validate_phone("+91 98765-43210") == "+919876543210"
    assert validate_phone("919876543210") == "+919876543210"
    assert validate_phone("09876543210") == "+919876543210"
    assert validate_phone("(0) 61234 56789") == "+916123456789"

    assert validate_phone("5876543210") is None
    assert validate_phone("987654321") is None
    assert validate_phone("9198765432100") is None
    assert validate_phone("abcdefghij") is None


def test_date_validation():
    assert validate_date("2026-10-15") == "2026-10-15"
    assert validate_date("15/10/2026") == "2026-10-15"
    assert validate_date("15-10-2026") == "2026-10-15"
    assert validate_date("01-Dec-2026") == "2026-12-01"
    assert validate_date("01-dec-2026") == "2026-12-01"

    assert validate_date("31/02/2026") is None
    assert validate_date("2026-13-01") is None


def test_decimal_and_enum_validation():
    assert validate_decimal("₹ 12,500.50") == "12500.50"
    assert validate_decimal("Rs.1000") == "1000.00"
    assert validate_decimal("INR 99.995") == "100.00"
    assert validate_decimal("-50") is None

    assert validate_enum("HI", ["en", "hi", "ta"]) == "hi"
    assert validate_enum("fr", ["en", "hi", "ta"]) is None


def test_out_of_order_events_overrides_and_buckets(tmp_path: Path):
    db_path = tmp_path / "test.db"

    drop1 = tmp_path / "drop_1"
    (drop1 / "events").mkdir(parents=True)
    events_1 = [
        {
            "event_id": "e_override",
            "event_type": "disposition.override",
            "workflow_id": "motor_renewal",
            "call_id": "c_1",
            "occurred_at": "2026-10-05 11:00:00",
            "payload": {"disposition_code": "renewed", "overridden_by": "sup1", "reason": "verified"},
        },
        {
            "event_id": "e_disp",
            "event_type": "disposition.set",
            "workflow_id": "motor_renewal",
            "call_id": "c_1",
            "occurred_at": "2026-10-05T05:10:00Z",
            "phone": "9876543210",
            "payload": {"disposition_code": "not_interested"},
        },
        {
            "event_id": "e_conn",
            "event_type": "call.connected",
            "workflow_id": "motor_renewal",
            "call_id": "c_1",
            "occurred_at": "2026-10-05T05:05:00Z",
            "phone": "9876543210",
            "payload": {},
        },
        {
            "event_id": "e_init",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "c_1",
            "occurred_at": "2026-10-05T05:00:00Z",
            "phone": "9876543210",
            "payload": {},
        },
    ]
    (drop1 / "events" / "events_0001.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events_1) + "\n", encoding="utf-8"
    )

    run_ingest(drop1, db_path)
    rep1 = tmp_path / "rep1"
    run_report(rep1, db_path)
    dq1 = json.loads((rep1 / "data_quality.json").read_text(encoding="utf-8"))
    assert dq1["orphan_events"] == 4

    drop2 = tmp_path / "drop_2"
    (drop2 / "leads" / "motor_renewal").mkdir(parents=True)
    (drop2 / "leads" / "motor_renewal" / "batch_002.csv").write_text(
        "customer_name,mobile,policy_number,language\n"
        "Asha Devi,+91-98765-43210,POL-100,hi\n",
        encoding="utf-8",
    )

    run_ingest(drop2, db_path)
    rep2 = tmp_path / "rep2"
    run_report(rep2, db_path)

    dq2 = json.loads((rep2 / "data_quality.json").read_text(encoding="utf-8"))
    assert dq2["orphan_events"] == 0

    lead_csv = (rep2 / "lead_report.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(lead_csv) == 2
    assert lead_csv[1] == (
        "motor_renewal,POL-100,batch_002,+91******3210,completed,1,1,2026-10-05T10:30:00+05:30,renewed,true"
    )

    conn = sqlite3.connect(db_path)
    raw_dump = str(conn.execute("SELECT * FROM leads").fetchall())
    conn.close()
    assert "Asha Devi" not in raw_dump
    assert "9876543210" not in raw_dump


def test_duplicate_lead_merging_and_batch_immutability(tmp_path: Path):
    db_path = tmp_path / "merge.db"

    drop1 = tmp_path / "drop_1"
    (drop1 / "leads" / "motor_renewal").mkdir(parents=True)
    (drop1 / "leads" / "motor_renewal" / "batch_001.csv").write_text(
        "customer_name,phone,policy_number,premium_amount,preferred_language\n"
        "Ravi,9876500001,POL-001,5000,hi\n",
        encoding="utf-8",
    )

    drop2 = tmp_path / "drop_2"
    (drop2 / "leads" / "motor_renewal").mkdir(parents=True)
    # Same phone in batch_002 with blank premium & blank language -> must not overwrite batch_001 values
    (drop2 / "leads" / "motor_renewal" / "batch_002.csv").write_text(
        "customer_name,phone,policy_number,premium_amount,preferred_language\n"
        "Ravi Kumar,+919876500001,POL-001-UPDATED,,\n",
        encoding="utf-8",
    )

    run_ingest(drop1, db_path)
    run_ingest(drop2, db_path)
    rep = tmp_path / "rep"
    run_report(rep, db_path)

    dq = json.loads((rep / "data_quality.json").read_text(encoding="utf-8"))
    assert dq["duplicate_lead_rows_merged"] == 1

    # batch_id must remain batch_001 (the batch that first introduced the lead)
    lead_lines = (rep / "lead_report.csv").read_text(encoding="utf-8").strip().splitlines()
    assert "motor_renewal,POL-001-UPDATED,batch_001,+91******0001,yet_to_reach_out,0,0,,,false" == lead_lines[1]

    # Verify non-null fields from batch_001 were preserved in SQLite
    conn = sqlite3.connect(db_path)
    fields = json.loads(conn.execute("SELECT fields_json FROM leads").fetchone()[0])
    conn.close()
    assert fields["premium_amount"] == "5000.00"
    assert fields["preferred_language"] == "hi"


def test_all_four_buckets_and_unknown_dispositions(tmp_path: Path):
    db_path = tmp_path / "buckets.db"
    drop1 = tmp_path / "drop_1"
    (drop1 / "leads" / "motor_renewal").mkdir(parents=True)
    (drop1 / "events").mkdir(parents=True)

    (drop1 / "leads" / "motor_renewal" / "batch_001.csv").write_text(
        "customer_name,phone,policy_number\n"
        "Lead A,9000000001,POL-A\n"
        "Lead B,9000000002,POL-B\n"
        "Lead C,9000000003,POL-C\n"
        "Lead D,9000000004,POL-D\n",
        encoding="utf-8",
    )

    events = [
        # Lead B: telephony failed -> in_progress_telephony_failed
        {"event_id": "b1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "cb1", "occurred_at": "2026-10-05T10:00:00+05:30", "phone": "9000000002", "payload": {}},
        {"event_id": "b2", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "cb1", "occurred_at": "2026-10-05T10:00:05+05:30", "phone": "9000000002", "payload": {}},
        # Lead C: 1 telephony failed + 1 initiated (not all failed) -> in_progress_dialed
        {"event_id": "c1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "cc1", "occurred_at": "2026-10-05T10:00:00+05:30", "phone": "9000000003", "payload": {}},
        {"event_id": "c2", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "cc1", "occurred_at": "2026-10-05T10:00:05+05:30", "phone": "9000000003", "payload": {}},
        {"event_id": "c3", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "cc2", "occurred_at": "2026-10-05T11:00:00+05:30", "phone": "9000000003", "payload": {}},
        # Lead D: unknown disposition ignored + terminal disposition -> completed
        {"event_id": "d1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "cd1", "occurred_at": "2026-10-05T10:00:00+05:30", "phone": "9000000004", "payload": {}},
        {"event_id": "d2", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "cd1", "occurred_at": "2026-10-05T10:01:00+05:30", "phone": "9000000004", "payload": {"disposition_code": "made_up_code"}},
        {"event_id": "d3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "cd1", "occurred_at": "2026-10-05T10:02:00+05:30", "phone": "9000000004", "payload": {"disposition_code": "not_interested"}},
    ]
    (drop1 / "events" / "events_0001.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )

    run_ingest(drop1, db_path)
    rep = tmp_path / "rep"
    run_report(rep, db_path)

    dq = json.loads((rep / "data_quality.json").read_text(encoding="utf-8"))
    assert dq["unknown_disposition_events"] == 1

    rows = {
        line.split(",")[1]: line.split(",")[4]
        for line in (rep / "lead_report.csv").read_text(encoding="utf-8").strip().splitlines()[1:]
    }
    assert rows["POL-A"] == "yet_to_reach_out"
    assert rows["POL-B"] == "in_progress_telephony_failed"
    assert rows["POL-C"] == "in_progress_dialed"
    assert rows["POL-D"] == "completed"


def test_idempotency_and_incremental_equivalence(tmp_path: Path):
    data_dir = Path("data")
    if not data_dir.exists():
        pytest.skip("Generated data/ directory not present")

    db_inc = tmp_path / "inc.db"
    run_ingest(data_dir / "drop_1", db_inc)
    run_ingest(data_dir / "drop_1", db_inc)
    run_ingest(data_dir / "drop_2", db_inc)
    out_inc = tmp_path / "out_inc"
    run_report(out_inc, db_inc)

    db_clean = tmp_path / "clean.db"
    run_ingest(data_dir / "drop_1", db_clean)
    run_ingest(data_dir / "drop_2", db_clean)
    out_clean = tmp_path / "out_clean"
    run_report(out_clean, db_clean)

    for fname in [
        "batch_summary.csv",
        "daily_overview.csv",
        "lead_report.csv",
        "rejections.csv",
        "data_quality.json",
    ]:
        assert (out_inc / fname).read_text(encoding="utf-8") == (out_clean / fname).read_text(
            encoding="utf-8"
        )