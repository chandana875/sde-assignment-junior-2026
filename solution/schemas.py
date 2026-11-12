import csv
import json
import re
from pathlib import Path
from typing import Any

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


def normalize_header(header: str) -> str:
    """Lower-case, trim, and collapse runs of '_' or whitespace to a single space."""
    return re.sub(r"[_\s]+", " ", header.strip().lower())


def load_workflow_schemas(schemas_dir: Path = SCHEMAS_DIR) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(schemas_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        wf_id = data["workflow_id"]
        alias_map: dict[str, str] = {}
        ref_field = None
        for field in data["fields"]:
            fname = field["name"]
            alias_map[normalize_header(fname)] = fname
            for alias in field.get("aliases", []):
                alias_map[normalize_header(alias)] = fname
            if field.get("reference"):
                ref_field = fname
        data["_alias_map"] = alias_map
        data["_reference_field"] = ref_field
        schemas[wf_id] = data
    return schemas


def load_dispositions(schemas_dir: Path = SCHEMAS_DIR) -> dict[str, dict[str, dict[str, bool]]]:
    """Returns {workflow_id: {code: {'is_terminal': bool, 'is_success': bool}}}."""
    catalogue: dict[str, dict[str, dict[str, bool]]] = {}
    csv_path = schemas_dir / "dispositions.csv"
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            wf_id = row["workflow_id"].strip()
            code = row["code"].strip()
            catalogue.setdefault(wf_id, {})[code] = {
                "is_terminal": row["is_terminal"].strip().lower() == "true",
                "is_success": row["is_success"].strip().lower() == "true",
            }
    return catalogue