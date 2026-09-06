"""Derive the connector metric-rename map that dbt reads, from the manifests.

    python scripts/generate_metric_name_map.py            # print what would change
    python scripts/generate_metric_name_map.py --write    # rewrite the seed

WHY THIS EXISTS (AI-270). Five connectors land their metric names RAW, so the
canonical dictionary never reaches the warehouse:

    x-ads            billed_charge_local_micro   where the fact expects  cost
    amazon-dsp       totalCost / sales / purchases
    sa360            metrics.clicks / metrics.cost_micros   (the API prefix)
    adobe-analytics  visits
    brevo            sent / delivered            where it expects  messages_*

One cause, not five accidents: each of them lands LONG, so the metric name is a
column VALUE, while their `transform()` renames row KEYS. A dict comprehension
over `row.items()` cannot touch a name that travels as data.

WHY A SEED RATHER THAN A FIX AT EACH LANDING. Repairing the connectors is
correct and does not go far enough on its own: rows ALREADY landed keep their raw
names, so the mart would read a mixture and a total would silently split in two
across the date the fix shipped. A map applied at staging covers the old rows and
the new ones with one rule.

WHY IT IS DERIVED AND NOT WRITTEN. The manifests already hold this map --
`canonical_metric_mapping` is the connector's own declaration of what its raw
names mean. A hand-written seed would be a second copy of it, and two copies of
one fact is the shape this repository keeps paying for. This script projects it;
`test_metric_name_map_is_current` refuses any drift between the two.

WHAT IS DELIBERATELY ABSENT. A row where the raw name EQUALS the canonical one:
it renames nothing, and a map full of identities hides the twenty entries that
actually do something. The staging join is a LEFT JOIN with COALESCE, so an
absent pair means "keep the name as landed" -- which is exactly right.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ROOT / "server" / "modules"
SEED = ROOT / "dbt" / "seeds" / "connector_metric_names.csv"

HEADER = ("connector", "landed_metric", "canonical_metric")


def _canonical(value: object) -> str | None:
    """A mapping entry is either a plain name or a dict carrying `canonical`."""
    if isinstance(value, dict):
        canon = value.get("canonical")
        return str(canon) if canon else None
    return str(value) if value else None


def derive() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for manifest_path in sorted(MODULES.glob("*/manifest.json")):
        connector = manifest_path.parent.name
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        mapping = manifest.get("canonical_metric_mapping") or {}
        if not isinstance(mapping, dict):
            continue
        for landed, raw_value in mapping.items():
            canonical = _canonical(raw_value)
            # Identity pairs are dropped -- see the module docstring.
            if canonical and canonical != landed:
                rows.append((connector, str(landed), canonical))
    return sorted(rows)


def render(rows: list[tuple[str, str, str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="rewrite the seed in place")
    args = parser.parse_args()

    rows = derive()
    rendered = render(rows)
    current = SEED.read_text(encoding="utf-8") if SEED.exists() else ""

    if rendered == current:
        print(f"seed is current: {len(rows)} renames")
        return 0

    if args.write:
        SEED.write_text(rendered, encoding="utf-8")
        print(f"wrote {SEED.relative_to(ROOT)}: {len(rows)} renames")
        return 0

    print(f"seed is STALE: derived {len(rows)} renames, seed holds "
          f"{max(0, len(current.splitlines()) - 1)}")
    print("run: python scripts/generate_metric_name_map.py --write")
    return 1


if __name__ == "__main__":
    sys.exit(main())
