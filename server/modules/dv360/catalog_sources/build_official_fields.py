"""Re-export the curated Bid Manager v2 filter/metric snapshot from the manifest.

NOTE: This script reads ``manifest.json`` and re-emits its fields in the
``official_fields.json`` format.  It is a *re-exporter*, not a mechanical
diff against the live Bid Manager v2 API reference.  It cannot detect upstream
drift (new Google metrics, renamed enums) on its own.

To detect drift, fetch a fresh snapshot of the official Bid Manager v2
filters/metrics reference page (e.g.
https://developers.google.com/bid-manager/reference/rest/v2/filters-metrics)
and compare the ``source_field`` values against this file manually or via a
dedicated drift script that reads a committed ``raw_api_response.json``
snapshot.  See catalog_sources/ROLLOUT_NOTES.md for the rationale.
"""

from __future__ import annotations

import json
from pathlib import Path


def build() -> list[dict]:
    module_dir = Path(__file__).parents[1]
    manifest = json.loads((module_dir / "manifest.json").read_text(encoding="utf-8"))
    fields = []
    for item in manifest["source_capabilities"]["fields"]:
        fields.append(
            {
                "field_id": item["field_id"],
                "source_field": item["source_field"],
                "kind": item["kind"],
                "data_type": item["physical_type"],
                "section": "FILTER" if item["kind"] == "dimension" else "METRIC",
                "description": item["description"],
            }
        )
    ids = [item["field_id"] for item in fields]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate DV360 field id")
    if any(not item["source_field"].startswith(("FILTER_", "METRIC_")) for item in fields):
        raise ValueError("unknown Bid Manager filter or metric enum")
    return fields


if __name__ == "__main__":
    target = Path(__file__).with_name("official_fields.json")
    fields = build()
    target.write_text(json.dumps(fields, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(fields)} DV360 fields to {target}")
