"""Story 28.2 -- Layer-4 golden transform: raw TTD rows -> canonical rows.

transform() applies canonical_metric_mapping + canonical_dimension_mapping from
the manifest (AD-2). The golden fixtures pin the exact rename contract so a
mapping change is caught. Also asserts the physical_type-driven numeric split
(never id-suffix) and that non-numeric selections are not coerced.
"""

from __future__ import annotations

import json
from pathlib import Path

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "thetradedesk"
_FIXTURES = _MODULE_DIR / "tests" / "fixtures"


def _load(name: str):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_transform_maps_golden_pull_to_expected_facts(connector):
    raw = _load("golden_pull.json")
    expected = _load("expected_facts.json")
    got = connector.transform(raw)
    assert got == expected


def test_numeric_field_ids_driven_by_catalog_physical_type(connector):
    """The numeric split reads the CATALOG physical_type, never an id suffix."""
    ids = ["Impressions", "AdvertiserCostAdvCurrency", "date", "CampaignId",
           "TotalClickConversions"]
    numeric = set(connector._numeric_field_ids(ids))
    assert "Impressions" in numeric
    assert "AdvertiserCostAdvCurrency" in numeric
    assert "TotalClickConversions" in numeric
    # date + CampaignId are date/string -> NOT numeric (land as keys/segments).
    assert "date" not in numeric
    assert "CampaignId" not in numeric


def test_non_additive_ratio_metrics_are_numeric_but_flagged(connector):
    """Cpm/Cpc/Ctr/Cpa* are numeric (land raw at day grain) yet carry the
    NON-ADDITIVE catalog note (AD-4) so the semantic layer never SUMs them."""
    catalog = json.loads((_MODULE_DIR / "api_catalog.json").read_text(encoding="utf-8"))
    by_id = {f["field_id"]: f for f in catalog["fields"]}
    for ratio in ("Cpm", "Cpc", "Ctr", "CpaClick"):
        assert ratio in connector._numeric_field_ids([ratio])
        assert "NON-ADDITIVE" in by_id[ratio]["description"]
