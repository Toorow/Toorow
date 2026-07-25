"""Build deterministic X Ads API v12 entity/metric field snapshot."""

from __future__ import annotations

import json
from pathlib import Path


def build() -> list[dict]:
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text())
    fields = [
        {
            "field_id": i["field_id"],
            "source_field": i["source_field"],
            "kind": i["kind"],
            "data_type": i["physical_type"],
            "section": "DIMENSION" if i["kind"] == "dimension" else "METRIC",
            "description": i["description"],
        }
        for i in manifest["source_capabilities"]["fields"]
    ]
    if len({i["field_id"] for i in fields}) != len(fields):
        raise ValueError("duplicate X Ads field")
    return fields


if __name__ == "__main__":
    target = Path(__file__).with_name("official_fields.json")
    fields = build()
    target.write_text(json.dumps(fields, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(fields)} X Ads fields to {target}")
