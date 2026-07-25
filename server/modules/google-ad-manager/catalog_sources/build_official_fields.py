"""Deterministic builder for google-ad-manager official_fields.json (catalogue port).

This script is the AUDITABLE SOURCE for the committed official snapshot. It is
committed alongside its two outputs (official_fields.json +
report_type_compatibility.json) so a reviewer can see exactly how the ported
catalogue was derived from the upstream gam-native REST reporting catalogue.

SOURCE OF TRUTH (read-only, parsed with ast -- never imported, so gam-native
backend deps are NOT required):
    C:/Users/littl/Programmation/gam-native/backend/src/domain/
        gam_reporting_catalogue_generated.py
    DISCOVERY_REVISION = "20260528"  (GAM REST discovery snapshot)
    Itself generated from _audits/coverage-current/gam_rest_discovery_v1.json by
    scripts/coverage-audit/7_generate_reporting_catalogue.py.

The upstream file exposes (all values are literals or ``frozenset((...))``):
  - ALL_REST_DIMENSIONS: frozenset[str]      (765 REST dimension enum names)
  - ALL_REST_METRICS:    frozenset[str]      (527 REST metric enum names)
  - AVAILABLE_DIMENSIONS: list[dict]  (name, category, api, description; the
        description carries the ``Data format: `X` `` marker + the
        ``Compatible with the following report types: ...`` sentence)
  - AVAILABLE_METRICS:    list[dict]  (same shape)
  - REPORT_TYPE_DIMENSIONS / REPORT_TYPE_METRICS: dict[str, frozenset[str]]
        (report_type -> allowed enum names)
  - USABLE_REPORT_TYPES, DISCOVERY_REVISION, GENERATED_AT

REST-ONLY: GAM SOAP is deprecated/removed. Only fields whose enum name is in
ALL_REST_DIMENSIONS / ALL_REST_METRICS are emitted (in the 20260528 snapshot
AVAILABLE_* is already REST-only, but the guard is kept so a future snapshot that
re-introduces SOAP rows is filtered deterministically).

MONEY / RATIO DOCTRINE (see catalog_sources/ROLLOUT_NOTES.md):
  - The gam-native ``Data format: `X` `` marker is copied VERBATIM into each
    description. Downstream MONEY detection (÷1e6 once, at read) keys off that
    marker -- do NOT strip or rewrite it.
  - ``non_additive`` populates the mart drop-set: provider-computed ratios /
    rates / per-unit averages are dropped at import and reconstructed from
    additive components. See ``_is_non_additive`` for the exact rule and the
    edge cases it resolves (eCPM is MONEY-format but a RATE => non_additive;
    ``*_PERCENT_REVENUE`` end in _REVENUE but are PERCENT shares => non_additive;
    plain MONEY ``*_REVENUE`` stay additive/in-micros).

Determinism: pure stdlib (ast/json/re/pathlib), no network, no clock. Running it
again reproduces both outputs byte-for-byte (fields sorted by field_id;
report_type_compatibility sorted by report_type then name).

Run (orchestrator, local only -- all outputs are committed):
    uv run python server/modules/google-ad-manager/catalog_sources/build_official_fields.py
"""

from __future__ import annotations

import ast
import json

# ---------------------------------------------------------------------------
# Upstream gam-native catalogue location. Absolute path (this is a one-repo port
# on Jean's machine); if the checkout moves, override via GAM_NATIVE_CATALOGUE.
# ---------------------------------------------------------------------------
import os
import re
from pathlib import Path

_DEFAULT_SOURCE = Path(
    r"C:\Users\littl\Programmation\gam-native\backend\src\domain"
    r"\gam_reporting_catalogue_generated.py"
)
SOURCE_PATH = Path(os.environ.get("GAM_NATIVE_CATALOGUE", _DEFAULT_SOURCE))

_DATA_FORMAT_RE = re.compile(r"Data format: `([A-Z_]+)`")


# ---------------------------------------------------------------------------
# Robust, import-free literal extraction. The upstream file assigns module-level
# names to literals OR to ``frozenset((...))`` calls; ast.literal_eval chokes on
# the frozenset Call, so we evaluate a tiny safe subset ourselves.
# ---------------------------------------------------------------------------


def _safe_eval(node: ast.AST):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "frozenset":
            if not node.args:
                return frozenset()
            return frozenset(_safe_eval(node.args[0]))
        raise ValueError(f"unsupported call in catalogue literal: {ast.dump(node)}")
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_safe_eval(e) for e in node.elts]
    if isinstance(node, ast.Set):
        return {_safe_eval(e) for e in node.elts}
    if isinstance(node, ast.Dict):
        return {_safe_eval(k): _safe_eval(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_safe_eval(node.operand)
    raise ValueError(f"unsupported node in catalogue literal: {ast.dump(node)}")


def load_catalogue(path: Path) -> dict:
    """Parse the gam-native catalogue module into a namespace of Python values.

    No import -- avoids pulling gam-native backend dependencies.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    ns: dict = {}
    for node in tree.body:
        target = value = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            target, value = node.target.id, node.value
        if target is None:
            continue
        try:
            ns[target] = _safe_eval(value)
        except ValueError:
            # Non-literal module statement (e.g. a helper) -- irrelevant to us.
            continue
    required = (
        "ALL_REST_DIMENSIONS",
        "ALL_REST_METRICS",
        "AVAILABLE_DIMENSIONS",
        "AVAILABLE_METRICS",
        "REPORT_TYPE_DIMENSIONS",
        "REPORT_TYPE_METRICS",
        "DISCOVERY_REVISION",
    )
    missing = [k for k in required if k not in ns]
    if missing:
        raise ValueError(f"catalogue is missing required symbols: {missing}")
    return ns


# ---------------------------------------------------------------------------
# field_id derivation: lowercase the GAM enum name, collapse any run of
# non-alphanumerics to a single underscore, strip edge underscores. Verified
# collision-free across all 1292 REST fields at revision 20260528 (see design
# note). The _dedupe pass is a deterministic safety net: on collision the second
# and later occurrences (ordered by source enum name) get a numeric suffix.
# ---------------------------------------------------------------------------


def _snake(source_field: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", source_field.lower()).strip("_")


def _parse_data_format(description: str) -> str | None:
    match = _DATA_FORMAT_RE.search(description)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# non_additive rule (money/ratio doctrine, ROLLOUT_NOTES.md).
#
# TRUE  = provider-computed ratio / rate / per-unit average. Dropped at import,
#         reconstructed in the mart from additive numerator + denominator.
# FALSE = additive quantity, INCLUDING every plain MONEY *_REVENUE (kept in
#         micros).
#
# Edge cases resolved (see design note):
#   - eCPM is MONEY-format but a per-mille RATE -> non_additive (name carries
#     ECPM). CPM_REVENUE / CPC_REVENUE are MONEY revenue -> additive.
#   - *_PERCENT_REVENUE end in _REVENUE but their data_format is PERCENT (a
#     revenue *share*, not an amount) -> non_additive via the format rule.
#   - AVERAGE_*/*_AVERAGE_* and *_PER_* are per-unit averages -> non_additive
#     even when MONEY (e.g. AVERAGE_REVENUE_PER_USER, AD_EXCHANGE_REVENUE_PER_AD_VIEWER).
#   - Plain MONEY *_REVENUE (AD_SERVER_REVENUE, CPM_REVENUE, AD_EXCHANGE_REVENUE,
#     ...) stay additive -> kept in micros, never flagged.
# Dimensions never carry this (set false).
# ---------------------------------------------------------------------------

_RATIO_FORMATS = frozenset({"PERCENT", "WHOLE_PERCENT"})


def _is_non_additive(source_field: str, data_format: str | None) -> bool:
    if data_format in _RATIO_FORMATS:
        return True
    name = source_field  # UPPER_SNAKE enum name
    # Per-unit averages (prefix OR embedded, e.g. AD_SERVER_AVERAGE_ECPM).
    if "AVERAGE" in name:
        return True
    if "_PER_" in name:
        return True
    # Rate families.
    if "ECPM" in name or "CTR" in name:
        return True
    if name.endswith("_RATE") or "FILL_RATE" in name:
        return True
    if "VIEWABILITY" in name:
        return True
    if "PERCENTAGE" in name or "PERCENT" in name:
        return True
    if "COVERAGE" in name:
        return True
    return False


# ---------------------------------------------------------------------------
# tier heuristic (catalog_sources.json section_tier_map intent):
#   core     = delivery + revenue + the primary reporting keys
#              (impressions/clicks/revenue, date, ad_unit, line_item, order,
#              advertiser).
#   advanced = programmatic / deals / reach / Nielsen / audience segments /
#              GA-link (semantic-guard families) + off-property / partner-finance
#              / privacy families.
#   standard = everything else (inventory/fill, viewability, video, ...).
# Ordered: advanced markers are checked first (a programmatic *_REVENUE is
# advanced, not core), then core markers, else standard.
# ---------------------------------------------------------------------------

_ADVANCED_MARKERS = (
    "PROGRAMMATIC",
    "DEAL",
    "BIDDER",
    "YIELD",
    "PROTECTED",  # PROTECTED_AUDIENCE etc.
    "REACH",
    "UNIQUE_",
    "FREQUENCY",
    "NIELSEN",
    "AUDIENCE_SEGMENT",
    "AUDIENCE",
    "GOOGLE_ANALYTICS",
    "ANALYTICS_PROPERTY",
    "OFF_PROPERTY",
    "ON_PLATFORM",
    "PARTNER_FINANCE",
    "PARTNER_MANAGEMENT",
    "PRIVACY",
    "USER_MESSAGES",
    "AD_EXCHANGE",
    "ADSENSE",
    "MEDIATION",
)

_CORE_MARKERS = (
    "IMPRESSION",
    "CLICK",
    "REVENUE",
    "DATE",
    "AD_UNIT",
    "LINE_ITEM",
    "ORDER",
    "ADVERTISER",
)


def _tier(source_field: str) -> str:
    name = source_field
    if any(marker in name for marker in _ADVANCED_MARKERS):
        return "advanced"
    if any(marker in name for marker in _CORE_MARKERS):
        return "core"
    return "standard"


# ---------------------------------------------------------------------------
# report_types for a field: every report type whose REPORT_TYPE_* set contains
# the enum name. Sorted for determinism. A field attached to no usable report
# type (custom-field / custom-dimension placeholders) gets [] -- honest: it is a
# valid enum but not selectable under any usable report type.
# ---------------------------------------------------------------------------


def _report_types_for(name: str, report_type_map: dict) -> list[str]:
    return sorted(rt for rt, allowed in report_type_map.items() if name in allowed)


def build_fields(ns: dict) -> list[dict]:
    rest_dims: frozenset = frozenset(ns["ALL_REST_DIMENSIONS"])
    rest_metrics: frozenset = frozenset(ns["ALL_REST_METRICS"])
    rtd: dict = ns["REPORT_TYPE_DIMENSIONS"]
    rtm: dict = ns["REPORT_TYPE_METRICS"]

    rows: list[tuple[str, str]] = []  # (source_field, kind) in source order
    for entry in ns["AVAILABLE_DIMENSIONS"]:
        rows.append((entry["name"], "dimension"))
    for entry in ns["AVAILABLE_METRICS"]:
        rows.append((entry["name"], "metric"))

    # description lookup keyed by (name, kind) -- names are unique within a kind.
    desc_dim = {e["name"]: e["description"] for e in ns["AVAILABLE_DIMENSIONS"]}
    desc_met = {e["name"]: e["description"] for e in ns["AVAILABLE_METRICS"]}

    fields: list[dict] = []
    seen_ids: dict[str, str] = {}  # field_id -> source_field (for dedupe log)
    collisions: list[tuple[str, str, str]] = []

    for source_field, kind in rows:
        # REST-only guard.
        if kind == "dimension" and source_field not in rest_dims:
            continue
        if kind == "metric" and source_field not in rest_metrics:
            continue

        description = (desc_dim if kind == "dimension" else desc_met)[source_field]
        data_format = _parse_data_format(description)

        field_id = _snake(source_field)
        if field_id in seen_ids:
            # Deterministic disambiguation (never expected at rev 20260528).
            base, n = field_id, 2
            while f"{base}_{n}" in seen_ids:
                n += 1
            collisions.append((field_id, seen_ids[field_id], source_field))
            field_id = f"{base}_{n}"
        seen_ids[field_id] = source_field

        report_type_map = rtd if kind == "dimension" else rtm
        non_additive = (
            _is_non_additive(source_field, data_format) if kind == "metric" else False
        )

        fields.append(
            {
                "field_id": field_id,
                "source_field": source_field,
                "kind": kind,
                "data_format": data_format,
                "tier": _tier(source_field),
                "report_types": _report_types_for(source_field, report_type_map),
                "non_additive": non_additive,
                "description": description,
            }
        )

    fields.sort(key=lambda f: f["field_id"])
    if collisions:
        print("field_id collisions disambiguated (base, first_source, second_source):")
        for base, first, second in collisions:
            print(f"  {base}: {first} kept, {second} suffixed")
    return fields


def build_report_type_compatibility(ns: dict) -> dict:
    """report_type -> {"dimensions": [...], "metrics": [...]} (REST names only).

    Sourced from REPORT_TYPE_DIMENSIONS / REPORT_TYPE_METRICS, intersected with
    the ALL_REST_* universe so a future SOAP-carrying snapshot stays REST-only.
    Consumed at request-build time to refuse a field/report_type combo before the
    REST submit.
    """
    rest_dims: frozenset = frozenset(ns["ALL_REST_DIMENSIONS"])
    rest_metrics: frozenset = frozenset(ns["ALL_REST_METRICS"])
    rtd: dict = ns["REPORT_TYPE_DIMENSIONS"]
    rtm: dict = ns["REPORT_TYPE_METRICS"]

    report_types = sorted(set(rtd) | set(rtm))
    out: dict = {}
    for rt in report_types:
        out[rt] = {
            "dimensions": sorted(n for n in rtd.get(rt, ()) if n in rest_dims),
            "metrics": sorted(n for n in rtm.get(rt, ()) if n in rest_metrics),
        }
    return out


def main() -> None:
    ns = load_catalogue(SOURCE_PATH)
    revision = ns["DISCOVERY_REVISION"]

    fields = build_fields(ns)
    metrics = sum(1 for f in fields if f["kind"] == "metric")
    dims = sum(1 for f in fields if f["kind"] == "dimension")
    non_additive = sum(1 for f in fields if f["non_additive"])
    money = sum(1 for f in fields if f["data_format"] == "MONEY")

    fields_doc = {
        "_generated_by": "build_official_fields.py",
        "_source": (
            "gam-native backend/src/domain/gam_reporting_catalogue_generated.py "
            f"(DISCOVERY_REVISION {revision}); REST-only; ported by the toorow "
            "google-ad-manager catalogue port. Descriptions keep the "
            "`Data format: `X`` marker VERBATIM (drives MONEY micros detection)."
        ),
        "_counts": {
            "dimensions": dims,
            "metrics": metrics,
            "non_additive_metrics": non_additive,
            "money_metrics": money,
            "total": len(fields),
        },
        "fields": fields,
    }

    fields_path = Path(__file__).with_name("official_fields.json")
    fields_path.write_text(
        json.dumps(fields_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"Wrote {len(fields)} REST fields ({metrics} metrics / {dims} dimensions; "
        f"{non_additive} non_additive; {money} MONEY) to {fields_path}"
    )

    compat = build_report_type_compatibility(ns)
    compat_doc = {
        "_generated_by": "build_official_fields.py",
        "_source": (
            "gam-native REPORT_TYPE_DIMENSIONS / REPORT_TYPE_METRICS "
            f"(DISCOVERY_REVISION {revision}); REST names only. Consumed at "
            "request-build time: a field selected outside its report_type's "
            "allowed set is refused (invalid_request) before the REST submit."
        ),
        "report_types": compat,
    }
    compat_path = Path(__file__).with_name("report_type_compatibility.json")
    compat_path.write_text(
        json.dumps(compat_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"Wrote report_type_compatibility for {len(compat)} report types to "
        f"{compat_path}"
    )


if __name__ == "__main__":
    main()
