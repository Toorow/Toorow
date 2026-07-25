# meta-ads — rollout notes

## Story 25.8 — catalog_driven execution + exposure regeneration

Meta Ads is the reference implementation of `selection_mode: "catalog_driven"`
(see the playbook `server/modules/README.md` → "Catalog-driven execution (25.8)").

### What landed

- New report profile `catalog_daily` (`selection_mode: "catalog_driven"`,
  availability `selectable`, dispatch `pull_catalog_daily`) in `manifest.json`
  (both `report_profiles` and `source_capabilities.reports`).
- `pull_catalog_daily(connection_id, date_from, date_to, project_id, pull_id,
  selection=None, ad_account_id=None)` in `connector.py`:
  - builds the Insights `fields` param from the selection's `source_fields`
    (the grain/structure fields are always included);
  - maps selected **breakdown** dimensions (age, gender, country,
    publisher_platform, …) to the `breakdowns` param, validated **before** the
    API call against `catalog_sources/catalog_sources.json → breakdown_compatibility`
    (`incompatible_pairs`, `max_breakdowns`) — an incompatible combo raises
    `core.pull_errors.InvalidRequestError` (no wasted round-trip);
  - **chunks** the `fields` param (~90 fields/request) and **merges** chunk rows
    on the grain+breakdown key so a very wide selection never trips Meta's
    field-count limit;
  - parses **action-family** selections (e.g.
    `actions_offsite_conversion_fb_pixel_purchase`) from the base `actions[]`
    array at parse time (the dotted `action_type` is recovered from the catalog
    field description, since the id's underscore normalisation is lossy);
  - lands via the existing `_insert_raw_rows` — **landing shape unchanged**
    (wide `raw_meta_ads_daily`; dbt UNPIVOTs to the long `fact_daily_kpi`).
- `exact_bundle` profiles (`campaign_daily` / `adset_daily` / `creative_daily`)
  are byte-identical — AD-22 proven on the shared respx fixture.

### Exposure regeneration (ACTION REQUIRED — orchestrator)

The catalog `exposure` values were NOT regenerated in this story (no shell in
the dev agent; the generator is local-only). The generator inputs are prepared:

- `catalog_sources/catalog_sources.json → exposure_regeneration` declares the
  `exposed_when` predicate and the `excluded_families` **with exclusion reasons**
  (CREATIVE ASSET, VIDEO, APP families stay `excluded` because `pull_catalog_daily`
  does not yet build their request shape).
- `breakdown_compatibility` declares the reachable breakdown dimensions.

The orchestrator regenerates the catalog so `exposed` = reachable through
catalog_driven execution (the whole catalog minus the reasoned `excluded`
entries), then re-runs the `CATALOG_GATE_MODE=fail` gate. Command:

```
uv run python scripts/build_api_catalog.py --module meta-ads \
    --sources-dir server/modules/meta-ads/catalog_sources \
    --report /tmp/meta-exposure-report.json
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
```

### Live probe

AI-13 defers the live pass to the 25.6 probe agent; 25.8 is mocked (respx) only.
