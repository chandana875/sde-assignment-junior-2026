# Solution: Interaction Ledger & Reporting Pipeline (Assignment A)

## Module Overview
- `cli.py`: CLI entrypoint (`ingest` and `report` subcommands) and post-merge schema default application.
- `db.py`: SQLite connection setup (WAL mode) and relational DDL (`ingested_files`, `leads`, `lead_rejections`, `events`).
- `schemas.py`: Loads workflow JSON schemas, builds normalised header alias maps, and parses `dispositions.csv`.
- `validators.py`: Normalisation and validation rules for Indian mobile phones, day-first/ISO dates, currency decimals, and case-insensitive enums.
- `security.py`: Deterministic HMAC-SHA256 phone hashing, phone masking (`+91******XXXX`), and Fernet symmetric encryption for PII attributes at rest.
- `ingest_leads.py`: Idempotent CSV ingestion with header aliasing, row validation, rejection logging, and cross-batch lead merging.
- `ingest_events.py`: Idempotent JSONL event ingestion with UTC/IST timestamp normalisation and O(1) cross-drop deduplication.
- `reporting.py`: Dynamic event-to-lead reconciliation, disposition override resolution, lead bucket classification, and report generation.
- `tests/test_pipeline.py`: Unit and integration tests covering validators, out-of-order events, bucket rules, duplicate merging, PII encryption, and idempotency.

## Quick Commands
```bash
# Ingest drops incrementally
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2

# Generate the 5 contract reports
python -m solution.cli report --out reports/

# Run automated tests
pytest -v