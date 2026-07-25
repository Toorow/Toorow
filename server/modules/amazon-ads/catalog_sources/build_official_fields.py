"""Build official_fields.json (+ generated compat blocks) for amazon-ads.

Story 26.5. The Amazon Ads Reporting v3 column reference has NO machine-readable
enum: the CloudFront OpenAPI `Reporting_prod_3p.json` is an EMPTY STUB and the
docs site is client-side rendered. The AUTHORITY is therefore the official
columns dictionary page (guides/reporting/v3/columns, 740 columns) transcribed
verbatim -- one line per column -- into Annex B of the committed research
dossier `_bmad-output/implementation-artifacts/research/amazon-ads-catalog-research.md`
(fetched 2026-07-21 through a JS renderer; fetch commands in catalog_sources.json).

This script is DETERMINISTIC and LOCAL-ONLY (no network). It parses:
  - Annex B  -> the 740-column dictionary (name, Type, report-type availability)
  - Section 3 -> the per-report-type PAGE lists (Base metrics + per-groupBy
                 Additional metrics), used ONLY to detect page-vs-dictionary
                 divergences (probe-to-confirm) -- never as a column source.

and emits, next to itself:
  - official_fields.json        input for scripts/build_api_catalog.py
  - report_type_columns.json    reportTypeId -> sorted column list (post-alias)
  - descriptive_mutable_fields.json  Story 26.6 Part A: entity state/type
                                 attributes the connector lands in attributes_json
                                 (latest-wins, OUT of the supersede grain)
  - and REWRITES two generated blocks inside catalog_sources.json:
      * field_compatibility.rules  (core schema_version "1" selectable_set rules)
      * excluded_fields            (DSP-only / non-daily-only / probe-to-confirm)

Run:  uv run python server/modules/amazon-ads/catalog_sources/build_official_fields.py
Then: uv run python scripts/build_api_catalog.py --module amazon-ads \
          --sources-dir server/modules/amazon-ads/catalog_sources \
          --report server/modules/amazon-ads/catalog_sources/fusion-report.json

ASCII-only stdout (AI-03).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]
_DEFAULT_DOSSIER = (
    _REPO_ROOT
    / "_bmad-output"
    / "implementation-artifacts"
    / "research"
    / "amazon-ads-catalog-research.md"
)

# ---------------------------------------------------------------------------
# Report-type vocabulary (canonical page ids) + dictionary-side aliases.
# The dossier's alias table (draft _derivation.alias_table) + the extra
# dictionary spellings observed in Annex B (sdAdGroups, benchmarks).
# ---------------------------------------------------------------------------

REPORT_TYPE_ALIASES: dict[str, str] = {
    "sbAdGroups": "sbAdGroup",
    "sbPlacement": "sbCampaignPlacement",
    "sdAdGroups": "sdAdGroup",
    "spGlobalAudiences": "spAudiences",
    "spGrossandInvalids": "spGrossAndInvalids",
    "sbGrossandInvalids": "sbGrossAndInvalids",
    "sdGrossandInvalids": "sdGrossAndInvalids",
}

SP_TYPES = (
    "spCampaigns", "spTargeting", "spSearchTerm", "spAdvertisedProduct",
    "spPurchasedProduct", "spGrossAndInvalids", "spPromptAdExtension",
    "spAudiences",
)
SB_TYPES = (
    "sbCampaigns", "sbAdGroup", "sbAds", "sbCampaignPlacement", "sbTargeting",
    "sbSearchTerm", "sbPurchasedProduct", "sbGrossAndInvalids",
    "sbPromptAdExtension", "sbAudiences",
)
SD_TYPES = (
    "sdCampaigns", "sdAdGroup", "sdTargeting", "sdAdvertisedProduct",
    "sdPurchasedProduct", "sdGrossAndInvalids",
)
ST_TYPES = ("stCampaigns", "stTargeting")
DSP_TYPES = (
    "dspCampaign", "dspAudioAndVideo", "dspAudience", "dspBidAdjustment",
    "dspBrandSuitability", "dspGeo", "dspInventory", "dspProduct",
    "dspReachFrequency", "dspTech", "dspBenchmarks",
)
# "benchmarks" is the dictionary's own availability token for the benchmark
# page shapes (dspBenchmarks + crossProgramBenchmarks share it); the dossier's
# per-product DSP count (541) EXCLUDES this bucket, so it stays a distinct
# cross-program type here (product "ALL", non-daily shape).
CROSS_TYPES = ("conversionPath", "crossProgramBenchmarks", "benchmarks")

SPONSORED_TYPES = SP_TYPES + SB_TYPES + SD_TYPES + ST_TYPES
ALL_TYPES = SPONSORED_TYPES + DSP_TYPES + CROSS_TYPES

PRODUCT_BY_TYPE: dict[str, str] = {}
for _t in SP_TYPES:
    PRODUCT_BY_TYPE[_t] = "SPONSORED_PRODUCTS"
for _t in SB_TYPES:
    PRODUCT_BY_TYPE[_t] = "SPONSORED_BRANDS"
for _t in SD_TYPES:
    PRODUCT_BY_TYPE[_t] = "SPONSORED_DISPLAY"
for _t in ST_TYPES:
    PRODUCT_BY_TYPE[_t] = "SPONSORED_TELEVISION"
for _t in DSP_TYPES:
    PRODUCT_BY_TYPE[_t] = "AMAZON_DSP"
for _t in CROSS_TYPES:
    PRODUCT_BY_TYPE[_t] = "ALL"

# Type-typo normalization (dossier section 4: Deciimal / Deciaml / decimal).
_TYPE_NORMALIZE = {
    "integer": "integer",
    "int": "integer",
    "long": "integer",
    "decimal": "decimal",
    "deciimal": "decimal",
    "deciaml": "decimal",
    "string": "string",
}

# Time columns land as dates (physical_type override).
_DATE_FIELDS = {"date", "startDate", "endDate"}

# ---------------------------------------------------------------------------
# Story 26.6 Part A -- descriptive MUTABLE attributes (grain vs. attribute).
#
# Finding F-2 (review 26.5): entity STATE/TYPE dimensions (campaignStatus,
# adStatus, ...) are MUTABLE attributes of a grain entity, NOT row-segmenting
# breakdowns. If they ride inside segments_json (the dbt QUALIFY supersede key)
# a status flip (ENABLED -> PAUSED) on a re-pulled day mints a SECOND grain key
# and the old row SURVIVES the supersede -> SUM(cost) doubles for the whole
# refetched window (the 26.1 ladder makes this systematic). They must land in a
# SEPARATE latest-wins column (attributes_json), OUT of the supersede grain.
#
# DECLARED, NOT GUESSED (contract point 1). This is an explicit allow-list of
# entity-state/type attributes. It deliberately EXCLUDES the *segmenting*
# dimensions whose id also ends in Type/Status but which DO carry distinct
# provider rows (matchType/keywordType/targetingType/segmentType/
# creativeExtensionType -- a keyword's match type, a targeting/audience segment
# type, a creative-extension type each produce their OWN rows; they stay in the
# grain via segments_json). adKeywordStatus is the STATUS of the *ad*-side
# keyword entity (mutable, ENABLED/PAUSED/ARCHIVED) -> attribute.
#
# The DSP entries (lineItem*/creativeType/dealType/contentType/conversionType)
# are currently exposure=excluded (seat-gated), but the prudent default (entity
# state/type = descriptive_mutable) is recorded now so that if a family is ever
# exposed it lands correctly out of the grain from day one.
# ---------------------------------------------------------------------------

DESCRIPTIVE_MUTABLE_FIELDS: frozenset[str] = frozenset({
    # Sponsored (exposed) entity state/type attributes.
    "campaignStatus",       # F-2 anchor case: mutable ENABLED/PAUSED/ARCHIVED
    "adStatus",             # ad-level state (mutable)
    "adKeywordStatus",      # ad-side keyword state (mutable)
    "campaignBudgetType",   # campaign config attribute (mutable)
    "costType",             # billing/cost model of the campaign (mutable)
    # DSP (currently excluded) entity state/type -- prudent default recorded.
    "lineItemStatus",
    "lineItemApprovalStatus",
    "lineItemType",
    "creativeType",
    "dealType",
    "contentType",
    "conversionType",
})

# Segmenting dimensions whose id ALSO looks like a status/type but which carry
# DISTINCT provider rows -> they STAY in the grain (segments_json). Listed for
# reviewer clarity / the FATAL guard below (never silently reclassified).
#
# Review 26.6 F-1 (CRITICAL, silent data loss): attributionType was wrongly
# classed descriptive_mutable. It is an ATTRIBUTION BUCKET that carries ROWS on
# sbPurchasedProduct -- it co-occurs with purchasedAsin + sales14d/orders14d in
# report_type_columns.json, and PROMOTED vs BRAND_HALO are DISTINCT provider
# rows for the SAME purchasedAsin/date. Dropping it out of the grain would
# collapse those two rows at the dbt QUALIFY -> sales14d/orders14d UNDER-COUNTED.
# It is pinned here so the FATAL overlap guard below forbids any future
# reclassification back into the mutable-attribute set.
_SEGMENTING_TYPE_DIMENSIONS: frozenset[str] = frozenset({
    "matchType", "keywordType", "targetingType", "segmentType",
    "creativeExtensionType", "attributionType",
})

# Ratio / provider-derived detection (NON-ADDITIVE note, AD-4). Conversions and
# landing numeric coercion are driven by the catalog physical_type -- NEVER by
# an id suffix -- but the additivity NOTE is naming-derived from the dossier's
# documented families.
_RATIO_MARKERS = (
    "Rate", "Percentage", "Share", "FrequencyAverage", "costPer", "CostPer",
    "eCP", "ROAS", "roas", "acos", "ACOS", "clickThroughRate", "Vcr", "Ctr",
)

# Exclusion reasons (single source for the generator + tests).
REASON_DSP = (
    "dsp-seat-gated: only reachable through dsp* report types, which require a "
    "DSP seat and the Amazon-Ads-AccountId header path (agency profiles). "
    "Excluded until a real DSP account exists (epic-26 open question #2)."
)
REASON_NON_DAILY = (
    "non-daily report shape: only reachable through conversionPath / benchmark "
    "report types (path arrays / benchmark aggregates), which no daily profile "
    "builds. Excluded until a dedicated landing shape exists."
)
REASON_PROBE = (
    "probe-to-confirm: page-vs-dictionary divergence (listed in the official "
    "columns dictionary but absent from every report-type page list). Settled "
    "by the 25.6 live probe (dossier section 5 drift analysis)."
)
REASON_PAGE_ONLY = (
    "probe-to-confirm: page-only column -- documented on the sbCampaigns/"
    "sdCampaigns/stCampaigns report-type pages (groupBy 'campaign' additional "
    "metrics) but ABSENT from the official columns dictionary. Cataloged per "
    "the dossier section-4 rule (a column is cataloged if it appears in "
    "EITHER the dictionary OR its report-type page); type Decimal is "
    "page-context-derived, unconfirmed. Settled by the 25.6 live probe."
)

# ---------------------------------------------------------------------------
# PAGE-ONLY columns (review 26.5 F-3). Dossier section-4 rule: a column is
# cataloged if it appears in EITHER the dictionary OR its report-type page.
# These two appear on the sbCampaigns/sdCampaigns/stCampaigns pages (groupBy
# 'campaign' additional metrics -- dossier sections 3.2/3.3/3.4; dspCampaign
# also lists them in Annex A) but in NO dictionary entry. They are cataloged
# exposure=excluded (REASON_PAGE_ONLY, probe-to-confirm). DSP availability is
# deliberately NOT recorded (dsp columns stay dictionary-scoped; the dsp
# families are seat-gated regardless).
# ---------------------------------------------------------------------------

PAGE_ONLY_COLUMNS: dict[str, dict] = {
    "longTermSales": {
        "raw_type": "Decimal",
        "availability": ("sbCampaigns", "sdCampaigns", "stCampaigns"),
    },
    "longTermROAS": {
        "raw_type": "Decimal",
        "availability": ("sbCampaigns", "sdCampaigns", "stCampaigns"),
    },
}


def _sections(prefix_rules_note: str = "") -> None:  # documentation anchor only
    """Section names transposed from the research draft _section_prefix_rules."""


def assign_section(name: str, availability: set[str]) -> str:
    """Ordered, deterministic section assignment (first match wins).

    Transposed from the draft's _section_prefix_rules. DSP-only fee/delivery
    families are grouped under the DSP sections so the section->tier map stays
    meaningful.
    """
    n = name
    low = n.lower()
    only_dsp = availability and availability <= set(DSP_TYPES)

    if n in _DATE_FIELDS:
        return "TIME"
    if n in {"grossImpressions", "grossClickThroughs"} or low.startswith("invalid"):
        return "GROSS & INVALID"
    if (
        low.startswith("promptadextension")
        or n in {"promptText", "creativeExtensionId", "creativeExtensionType"}
        or low.startswith("rufus")
    ):
        return "PROMPT AD EXTENSION"
    if low.startswith(("3p", "supplycost")) or "fee" in low or "prebid" in low:
        return "DSP FEES"
    if low.startswith(("audio", "companionbanner")):
        return "DSP AUDIO & VIDEO"
    if low.startswith(
        ("offamazon", "combined", "brandstore", "subscribe", "signup",
         "download", "application", "newsubscribe")
    ):
        return "DSP OFF-AMAZON"
    if availability and availability <= (
        set(CROSS_TYPES) | {"dspBenchmarks", "conversionPath"}
    ):
        return "BENCHMARK & PATH"
    if low.startswith("searchterm"):
        return "SEARCH TERM"
    if n == "placementClassification":
        return "PLACEMENT"
    if n in {
        "keyword", "keywordId", "keywordBid", "keywordType", "keywordText",
        "adKeywordStatus", "matchType", "topOfSearchImpressionShare",
        "topOfSearchBidAdjustment",
    } or low.startswith(("targeting", "matchedtarget")):
        return "TARGETING & KEYWORD"
    if low.startswith(
        ("advertisedasin", "advertisedsku", "purchasedasin", "promotedasin",
         "promotedsku", "asinbrandhalo", "variationasin", "parentasin", "product")
    ):
        return "ASIN & PRODUCT"
    if low.startswith("brandedsearch"):
        return "BRANDED SEARCH"
    if low.startswith("newtobrandunitssold"):
        return "UNITS"
    if low.startswith("newtobrand"):
        return "NEW TO BRAND"
    if low.startswith(("addtocart", "ecpaddtocart", "pagevisit")):
        return "CART & PAGE"
    if low.startswith(("kindle", "royalty")):
        return "KINDLE & ROYALTY"
    if low.startswith(
        ("cumulativereach", "impressionsfrequency", "avgimpressionsfrequency",
         "householdreach", "householdfrequency", "frequencyaverage", "reach",
         "uniquereach", "frequencygroup")
    ):
        return "REACH & FREQUENCY"
    if low.startswith(("segment", "audiencesegment", "audience")):
        return "AUDIENCE"
    if (
        low.startswith(("video", "view5seconds", "vcr", "playermode"))
        or low.endswith("quartileviews")
    ):
        return "VIDEO"
    if low.startswith(
        ("viewability", "viewclickthroughrate", "viewableimpressions",
         "measurableimpressions", "measurablerate", "viewthrough")
    ):
        return "VIEWABILITY"
    if low.startswith(("cost", "spend", "ecp")) or n == "totalCost":
        return "COST"
    if low.startswith(("impression", "click", "grossimpressions")) or n in {
        "viewableImpressions", "clickThroughRate",
    }:
        return "TRAFFIC"
    if low.startswith(
        ("campaign", "adgroup", "portfolio", "advertiseraccount", "advertiser")
    ) or n in {
        "adId", "adName", "adStatus", "costType", "bidOptimization",
        "attributionType", "marketplace", "currency", "entityId", "entityName",
        "accountId", "accountName",
    }:
        return "STRUCTURE"
    if low.startswith(
        ("purchases", "sales", "attributedsales", "orders", "conversion",
         "purchaseclickrate")
    ):
        if low.startswith("conversion"):
            return "CONVERSION DETAIL"
        return "SALES & PURCHASES"
    if low.startswith(("unitssold", "units")):
        return "UNITS"
    if low.startswith(("addtolist", "qualifiedborrows", "detailpageview", "leadform", "lead")):
        return "CONVERSION DETAIL"
    if only_dsp:
        return "DSP DELIVERY"
    return "MISC"


_ANNEX_LINE = re.compile(r"^- `([^`]+)` \(([^)]+)\) — (.+)$")


def parse_annex_b(text: str) -> dict[str, dict]:
    """Parse Annex B -> {column: {"raw_type", "availability": set[str]}}."""
    lines = text.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("## Annex B"))
        end = next(i for i, ln in enumerate(lines) if ln.startswith("## Annex C"))
    except StopIteration as exc:  # pragma: no cover - dossier contract
        raise SystemExit("FATAL: Annex B/C markers not found in the dossier") from exc

    columns: dict[str, dict] = {}
    unknown_tokens: set[str] = set()
    for line in lines[start:end]:
        stripped = line.strip()
        m = _ANNEX_LINE.match(stripped)
        if not m:
            if stripped.startswith("- "):
                # Review 26.5 F-5: a list line inside the Annex B range that
                # the parser cannot read is a FATAL, exactly like an unknown
                # report-type token -- never a silent drop of a column.
                raise SystemExit(
                    "FATAL: Annex B list line does not match the column "
                    "parser (fix the dossier line or the regex, never drop "
                    "silently): " + stripped[:160]
                )
            continue
        name, raw_type, availability_raw = m.group(1), m.group(2), m.group(3)
        availability: set[str] = set()
        for token in (t.strip() for t in availability_raw.split(",")):
            if not token:
                continue
            canonical = REPORT_TYPE_ALIASES.get(token, token)
            if canonical not in ALL_TYPES:
                unknown_tokens.add(token)
                continue
            availability.add(canonical)
        if name in columns:
            # Alias-side duplicate dictionary entries merge availability.
            columns[name]["availability"] |= availability
        else:
            columns[name] = {"raw_type": raw_type, "availability": availability}

    if unknown_tokens:
        raise SystemExit(
            "FATAL: unknown report-type token(s) in Annex B (extend the alias "
            "table, never drop silently): " + ", ".join(sorted(unknown_tokens))
        )
    return columns


def parse_page_tokens(text: str) -> set[str]:
    """Collect every backticked token from the section-3 page lists (lowercased).

    Used ONLY for the page-vs-dictionary divergence check: a dictionary column
    that appears on NO report-type page is probe-to-confirm.
    """
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## 3."))
    end = next(i for i, ln in enumerate(lines) if ln.startswith("## 4."))
    tokens: set[str] = set()
    for line in lines[start:end]:
        if ("metrics" in line) or ("Columns (" in line) or ("Additional" in line):
            tokens.update(t.lower() for t in re.findall(r"`([^`]+)`", line))
    return tokens


def column_kind(name: str, physical_type: str) -> str:
    if physical_type in ("string", "date"):
        return "dimension"
    if name.endswith("Id"):
        return "dimension"
    return "metric"


def is_ratio(name: str) -> bool:
    return any(marker in name for marker in _RATIO_MARKERS)


def build(dossier_path: Path) -> None:
    text = dossier_path.read_text(encoding="utf-8")
    columns = parse_annex_b(text)
    page_tokens = parse_page_tokens(text)

    # Review 26.5 F-3: inject the page-only columns (dossier section-4 rule
    # 'dictionary OR page'). FATAL if the dictionary starts listing one --
    # the override must then be dropped (the dictionary becomes authority).
    for name, meta in PAGE_ONLY_COLUMNS.items():
        if name in columns:
            raise SystemExit(
                "FATAL: page-only column now appears in the Annex B "
                f"dictionary -- drop the PAGE_ONLY_COLUMNS override: {name}"
            )
        columns[name] = {
            "raw_type": meta["raw_type"],
            "availability": set(meta["availability"]),
            "page_only": True,
        }

    total = len(columns)
    per_product: dict[str, int] = {}
    for meta in columns.values():
        products = {PRODUCT_BY_TYPE[t] for t in meta["availability"]}
        for p in products:
            per_product[p] = per_product.get(p, 0) + 1

    print(f"annex_b_columns_total={total}")
    for key in ("SPONSORED_PRODUCTS", "SPONSORED_BRANDS", "SPONSORED_DISPLAY",
                "SPONSORED_TELEVISION", "AMAZON_DSP", "ALL"):
        print(f"per_product[{key}]={per_product.get(key, 0)}")

    # ------------------------------------------------------------------ #
    # official_fields.json                                                #
    # ------------------------------------------------------------------ #
    official: list[dict] = []
    excluded_fields: dict[str, str] = {}
    report_type_columns: dict[str, list[str]] = {t: [] for t in ALL_TYPES}
    exposure_counts = {"exposed": 0, "excluded_dsp": 0, "excluded_non_daily": 0,
                       "excluded_probe": 0, "excluded_page_only": 0}

    for name in sorted(columns, key=str.lower):
        meta = columns[name]
        availability: set[str] = meta["availability"]
        raw_type = meta["raw_type"]
        physical = "date" if name in _DATE_FIELDS else _TYPE_NORMALIZE.get(
            raw_type.strip().lower(), "string"
        )
        kind = column_kind(name, physical)
        section = assign_section(name, availability)
        page_only = bool(meta.get("page_only"))
        # Review 26.5 F-3: page-only columns (longTerm*) are NOT dictionary
        # members. They are cataloged (section-4 'dictionary OR page' rule) but
        # deliberately kept OUT of report_type_columns.json -- that file is the
        # DICTIONARY-derived per-type requestable set (Annex B, post-alias) and
        # feeds the per-product completeness counts (SP 95 / SB 152 / SD 91 /
        # ST 60 / DSP 541) + the field_compat selectable_set rules. Adding a
        # page-only column there would inflate those Annex B counts AND make the
        # column field_compat-selectable, contradicting its excluded exposure.
        # (It is refused at the excluded-columns gate before field_compat runs.)
        if not page_only:
            for t in availability:
                report_type_columns[t].append(name)

        sponsored = availability & set(SPONSORED_TYPES)
        dsp = availability & set(DSP_TYPES)
        on_a_page = name.lower() in page_tokens

        desc = (
            f"Amazon Ads Reporting v3 column '{name}' ({raw_type}, official "
            "columns dictionary 2026-07-21). Report types: "
            + ", ".join(sorted(availability)) + "."
        )
        if page_only:
            desc = (
                f"Amazon Ads Reporting v3 column '{name}' ({raw_type}, "
                "PAGE-ONLY -- documented on the report-type page(s) "
                + ", ".join(sorted(availability))
                + " groupBy 'campaign' additional metrics but ABSENT from the "
                "official columns dictionary; type is page-context-derived, "
                "unconfirmed). Cataloged per the dossier section-4 'dictionary "
                "OR page' rule, exposure=excluded probe-to-confirm."
            )
        if is_ratio(name) and kind == "metric":
            desc += (
                " Provider-computed ratio/derived metric: NON-ADDITIVE (AD-4) "
                "-- never SUM across rows; prefer semantic-layer computation "
                "from stored numerators and denominators."
            )

        if page_only:
            # F-3: page-only (longTerm*) -> excluded probe-to-confirm, dedicated
            # reason (distinct from the dictionary-vs-page 'probe' bucket).
            excluded_fields[name] = REASON_PAGE_ONLY
            exposure_counts["excluded_page_only"] += 1
        elif sponsored and not on_a_page:
            excluded_fields[name] = REASON_PROBE
            exposure_counts["excluded_probe"] += 1
        elif not sponsored and dsp:
            excluded_fields[name] = REASON_DSP
            exposure_counts["excluded_dsp"] += 1
        elif not sponsored:
            excluded_fields[name] = REASON_NON_DAILY
            exposure_counts["excluded_non_daily"] += 1
        else:
            exposure_counts["exposed"] += 1

        entry = {
            "field_id": name,
            "source_field": name,
            "kind": kind,
            "data_type": physical,
            "description": desc,
            "section": section,
        }
        # Story 26.6 Part A: tag entity state/type mutable attributes so the
        # provenance is visible in official_fields.json (the runtime split reads
        # the dedicated descriptive_mutable_fields.json emitted below -- the
        # shared scripts/build_api_catalog.py OfficialField dataclass does not
        # carry arbitrary keys through to api_catalog.json).
        if name in DESCRIPTIVE_MUTABLE_FIELDS:
            entry["descriptive_mutable"] = True
        official.append(entry)

    # Story 26.6 Part A guard: every declared mutable attribute MUST be a real
    # dictionary column, and none may collide with a known segmenting dimension
    # (never silently reclassify a grain-bearing breakdown as an attribute).
    missing_mutable = sorted(DESCRIPTIVE_MUTABLE_FIELDS - set(columns))
    if missing_mutable:
        raise SystemExit(
            "FATAL: DESCRIPTIVE_MUTABLE_FIELDS lists column(s) absent from the "
            "dictionary (fix the allow-list, never ship a dangling flag): "
            + ", ".join(missing_mutable)
        )
    overlap = DESCRIPTIVE_MUTABLE_FIELDS & _SEGMENTING_TYPE_DIMENSIONS
    if overlap:
        raise SystemExit(
            "FATAL: a segmenting dimension is flagged descriptive_mutable "
            "(would drop it out of the supersede grain): " + ", ".join(sorted(overlap))
        )

    print("exposure_plan=" + json.dumps(exposure_counts, sort_keys=True))
    print("descriptive_mutable_fields=" + json.dumps(
        sorted(DESCRIPTIVE_MUTABLE_FIELDS)))

    (_HERE / "official_fields.json").write_text(
        json.dumps(official, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    report_type_columns = {t: sorted(cols) for t, cols in report_type_columns.items()}
    (_HERE / "report_type_columns.json").write_text(
        json.dumps(report_type_columns, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    # Story 26.6 Part A: dedicated module-local artifact the connector reads at
    # landing to split entity state/type attributes OUT of segments_json (the
    # supersede grain key) into attributes_json (latest-wins). Kept separate
    # from api_catalog.json because the shared merge pipeline's OfficialField
    # dataclass does not pass arbitrary keys through (and this module must not
    # touch shared catalog_gen). Sorted for a stable, diffable output.
    (_HERE / "descriptive_mutable_fields.json").write_text(
        json.dumps(sorted(DESCRIPTIVE_MUTABLE_FIELDS), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    # ------------------------------------------------------------------ #
    # catalog_sources.json: regenerate the GENERATED blocks in place.      #
    # ------------------------------------------------------------------ #
    sources_path = _HERE / "catalog_sources.json"
    cfg = json.loads(sources_path.read_text(encoding="utf-8"))

    rules: list[dict] = []
    for rt in SPONSORED_TYPES:
        cols = report_type_columns[rt]
        if not cols:
            continue
        rules.append(
            {
                "id": f"selectable_{rt}",
                "kind": "selectable_set",
                "scope": {"report_type": rt},
                "allowed_fields": cols,
                "_source": "official columns dictionary availability (Annex B, post-alias)",
            }
        )
    # Per-groupBy narrowing for the spCampaigns page (base + per-groupBy
    # additional metrics -- dossier section 3.1). Rules INTERSECT: with a
    # {"report_type", "group_by"} context both the report-type rule and the
    # groupBy rule apply.
    sp_campaigns_base = [
        "impressions", "addToList", "qualifiedBorrows", "royaltyQualifiedBorrows",
        "clicks", "cost", "purchases1d", "purchases7d", "purchases14d",
        "purchases30d", "purchasesSameSku1d", "purchasesSameSku7d",
        "purchasesSameSku14d", "purchasesSameSku30d", "unitsSoldClicks1d",
        "unitsSoldClicks7d", "unitsSoldClicks14d", "unitsSoldClicks30d",
        "sales1d", "sales7d", "sales14d", "sales30d", "attributedSalesSameSku1d",
        "attributedSalesSameSku7d", "attributedSalesSameSku14d",
        "attributedSalesSameSku30d", "unitsSoldSameSku1d", "unitsSoldSameSku7d",
        "unitsSoldSameSku14d", "unitsSoldSameSku30d",
        "kindleEditionNormalizedPagesRead14d",
        "kindleEditionNormalizedPagesRoyalties14d", "date", "startDate",
        "endDate", "campaignBiddingStrategy", "costPerClick",
        "clickThroughRate", "spend",
    ]
    sp_campaigns_extras = {
        "campaign": [
            "campaignName", "campaignId", "campaignStatus",
            "campaignBudgetAmount", "campaignBudgetType",
            "campaignRuleBasedBudgetAmount", "campaignApplicableBudgetRuleId",
            "campaignApplicableBudgetRuleName", "campaignBudgetCurrencyCode",
            "topOfSearchImpressionShare",
        ],
        "adGroup": ["adGroupName", "adGroupId", "adStatus"],
        "campaignPlacement": [
            "placementClassification", "campaignName", "campaignId",
            "campaignStatus", "campaignBudgetAmount", "campaignBudgetType",
            "campaignRuleBasedBudgetAmount", "campaignApplicableBudgetRuleId",
            "campaignApplicableBudgetRuleName", "campaignBudgetCurrencyCode",
            "topOfSearchImpressionShare",
        ],
    }
    for group_by, extras in sorted(sp_campaigns_extras.items()):
        rules.append(
            {
                "id": f"selectable_spCampaigns_{group_by}",
                "kind": "selectable_set",
                "scope": {"report_type": "spCampaigns", "group_by": group_by},
                "allowed_fields": sorted(set(sp_campaigns_base) | set(extras)),
                "_source": "spCampaigns page: base metrics + groupBy additional metrics",
            }
        )

    cfg["field_compatibility"]["rules"] = rules
    cfg["excluded_fields"] = excluded_fields

    # Attach the generated column sets to report_type_compatibility (request
    # builder input: selection -> report type + groupBy + columns).
    for rt, spec in cfg["report_type_compatibility"]["report_types"].items():
        spec["columns_ref"] = "report_type_columns.json"

    sources_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("wrote official_fields.json, report_type_columns.json, catalog_sources.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dossier", type=Path, default=_DEFAULT_DOSSIER)
    args = parser.parse_args(argv)
    build(args.dossier)
    return 0


if __name__ == "__main__":
    sys.exit(main())
