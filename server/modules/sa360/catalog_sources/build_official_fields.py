"""Build deterministic SA360 v0 field metadata from the curated manifest contract."""

from __future__ import annotations

import json
from pathlib import Path


def build() -> list[dict]:
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text())
    fields = []
    for item in manifest["source_capabilities"]["fields"]:
        fields.append(
            {
                "field_id": item["field_id"],
                "source_field": item["source_field"],
                "kind": item["kind"],
                "data_type": item["physical_type"],
                "section": item["field_id"].split(".", 1)[0],
                "description": item["description"],
                "selectable": True,
                "filterable": item["kind"] == "dimension",
                "sortable": True,
                "repeated": False,
            }
        )
    ids = [item["field_id"] for item in fields]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate SA360 field id")
    return fields


if __name__ == "__main__":
    target = Path(__file__).with_name("official_fields.json")
    fields = build()
    target.write_text(json.dumps(fields, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(fields)} SA360 fields to {target}")
