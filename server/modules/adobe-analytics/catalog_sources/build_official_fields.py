"""Build the deterministic Adobe Analytics 2.0 static schema baseline."""

from __future__ import annotations

import json
from pathlib import Path


def build() -> list[dict]:
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text())
    fields = [
        {
            "field_id": item["field_id"],
            "source_field": item["source_field"],
            "kind": item["kind"],
            "data_type": item["physical_type"],
            "section": "DIMENSION" if item["kind"] == "dimension" else "METRIC",
            "description": item["description"],
        }
        for item in manifest["source_capabilities"]["fields"]
    ]
    if len(fields) != len({item["field_id"] for item in fields}):
        raise ValueError("duplicate Adobe Analytics baseline field")
    return fields


if __name__ == "__main__":
    target = Path(__file__).with_name("official_fields.json")
    fields = build()
    target.write_text(json.dumps(fields, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(fields)} Adobe Analytics fields to {target}")
