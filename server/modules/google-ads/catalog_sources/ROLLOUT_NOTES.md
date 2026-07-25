# Google Ads Catalog Sources — Rollout Notes (Story 26.2, post-review fixes)

## Counts (locked by `test_catalog_google_ads.py`)

| Category | Count |
|---|---|
| Official fields (official_fields.json, v24) | 455 |
| Metrics (metrics.proto Annex A, integral) | 278 |
| Segments (segments.proto Annex B, integral) | 151 |
| Structural dimensions (curated, 5 profile resources) | 26 |
| api_catalog.json exposed | 407 |
| api_catalog.json excluded (AUCTION INSIGHT 7 / EXPERIMENT 28 / SKAN 13, reason per field) | 48 |
| api_catalog.json planned | 0 (epic-26 invariant) |
| money_micros declarations (28 `*_micros` + 11 monetary doubles) | 39 |
| field_compatibility rules (generated, one selectable_set per FROM resource) | 5 |
| manifest error_map entries (`<status>:<ENUM>`) | 59 |

## Generated artifacts — how to regenerate

- `official_fields.json` **and** the `field_compatibility.rules` array of
  `catalog_sources.json` are both written by
  `build_official_fields.py` (offline, deterministic, byte-stable):
  `uv run python server/modules/google-ads/catalog_sources/build_official_fields.py`
- `api_catalog.json` is merged from the official snapshot by the repo engine
  (`scripts/catalog_gen/merge.py`); the Supermetrics enrichment snapshot is
  fetch-only (never committed) and contributes fusion-report stats only —
  official descriptions/sections always win.

## Money-micros declaration (review 26.2 F-2)

`transform()` divides by 1e6 **only** on the `physical_type: "money_micros"`
declaration in `official_fields.json` — never on a name suffix. The explicit
list = every `*_micros` int64 metric **plus** the micros-denominated doubles:
`average_cpc`, `average_cpm`, `average_cost`, `average_cpe`,
`active_view_cpm`, and the `cost_per_*` family (except
`cost_per_conversion_p_value`, a unit-less probability). `value_per_*`
metrics are conversion **values** in currency units and are deliberately NOT
declared.

Unit suspects deliberately left UNDECLARED until the probe rules on them
(landing them raw is safe; dividing them wrongly is not):
`benchmark_average_max_cpc`, `trueview_average_cpv`,
`cost_converted_currency_per_platform_comparable_conversion`,
`biddable_indirect_install_first_in_app_conversion_micros` is declared via
its suffix; `all_new_customer_lifetime_value` / `new_customer_lifetime_value`
are value-family (undeclared).

## PROBE-ONLY verifications (25.6 — these CANNOT be validated offline)

1. **Micros units on the wire, field by field.** Ratify the money_micros
   declaration per field against live responses: every declared field must
   arrive in micros (int64 string or double), every undeclared monetary
   suspect (see list above) must be confirmed currency-units-or-micros and
   the declaration updated + snapshots regenerated accordingly. A wrong call
   here is a silent ×1e6 / ÷1e6 data corruption — this is the gate that
   lifts `verification: blocked`.
2. **Zero semantics.** Confirm which metrics Google OMITS at zero versus
   returns as `"0"` per resource/segment combination: the long landing is
   NULL-honest (absent metric = no row), so DQ volume monitors and the
   staging supersede must not read omission as data loss. Sample: a
   zero-spend day on campaign / ad_group / keyword_view.
3. **Real selection legality.** The static field_compatibility rules refuse
   what is KNOWN illegal; the probe must confirm what static rules cannot:
   (a) the flattened message-typed segment paths
   (`segments.keyword.info.text`, `segments.asset_interaction_target.asset`,
   `segments.sk_ad_network_source_app.sk_ad_network_source_app_id`,
   `segments.budget_campaign_association_status.status`) are selectable as
   emitted; (b) the pruned tier-core DEFAULT combo is accepted as one query
   on each of the 5 profile resources (no PROHIBITED_* residue → any hit
   feeds `fieldservice_compat.json`, which then becomes the authority);
   (c) the topology trio — a pull on a client via composite
   `<cid>@<login_cid>` with `login-customer-id`, a direct client without the
   header, and an MCC selection refused typed
   (`REQUESTED_METRICS_FOR_MANAGER`); (d) the 429 `quotaErrorDetails.
   retryDelay` wire format really parses (`"37s"` assumed — confirm the
   protobuf Duration JSON rendering).

## Topology — known limit (review 26.2 F-9)

- A seed whose expansion fails (e.g. 403 on a foreign manager) is skipped
  with a warning; discovery raises only when EVERY seed fails.
- A client reachable directly AND through an MCC keeps its DIRECT id; the
  composite duplicate is dropped and logged.
- A sub-MCC reachable through TWO root MCCs is placed under the first root
  visited (visited-set guard); its clients carry that root as `login_cid`.
  Any ancestor MCC is a valid `login-customer-id`, so pulls are unaffected —
  only the tree placement is first-root-wins.

## error_map note (review 26.2 F-7)

59 entries `<status>:<ENUM>`. `invalid_grant` (OAuth TOKEN endpoint error,
not a Google Ads API enum) is handled by core token_service on refresh and
surfaces as `auth_expired` — pre-existing core semantics (gsc pattern),
accepted deviation from the story line that said `auth_revoked`.
