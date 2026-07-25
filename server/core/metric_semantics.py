"""toorow -- Metric-semantics foundation: store, cascade, seed import (Story 27.1).

The stateless core of the metric-semantics epic (Epic 27). Two layers, one scope
model, one audit registry:

  * layer 1 -- metric DEFINITION (app.metric_definitions): canonical name, aggregation
    rule, additive flag, ratio numerator/denominator. Extends dbt/seeds/dim_metric.csv.
  * layer 2 -- multi-source RECONCILIATION topology (app.overlap_groups +
    app.overlap_group_members + app.reconciliation_rules): the connectors that emit the
    SAME metric and how their overlap is resolved. Extends
    dbt/seeds/metric_source_priority.csv (PRIORITY today).
  * the BRIDGE (app.source_metric_mappings): connector field -> metric definition
    (schema only in 27.1; populated from manifests in Story 27.2).

Resolution is a cascade PROJECT > ORG > PLATFORM: the most specific row wins, metric by
metric, deterministically. Every mutation writes one append-only audit row
(app.metric_semantics_audit) in the SAME transaction as the mutation.

STRICTLY ADDITIVE & PASSIVE (27.1): this module is not wired onto the runtime. The dbt
seeds remain the sole execution authority; the routing resolver (Story 27.3,
metric_reconciliation.py) reads this store but changes no consumer. No warehouse row is
read or written here.

Story 27.3 extensions (this module): PRIORITY defaults now carry a ``target_mart`` derived
PURELY from the metric name (_default_target_mart), and import_platform_defaults also
upserts the cost-verification KEEP_SEPARATE pilot (PLATFORM_COST_VERIFICATION_GROUP). Both
extensions preserve the bit-identical seed<->defaults equivalence (invariant 3):
``target_mart`` never enters the CSV comparison and the pilot is a SUPPLEMENTARY group
outside the CSV.

TRUST CONTRACT (S-3, lesson 27.2): this STORE has NO authorization guard BY DESIGN. Its
functions assume their arguments are ALREADY authorized -- authorization is the
responsibility of the SURFACE (REST route / MCP tool), never the store. Any NEW surface
that exposes these functions MUST reproduce the 27.2 guards (org-read for reads, org-manage
for mutations, existence-hiding on foreign ids) before calling in. The store trusts its
caller; the caller must have earned that trust.

AD-2 (ZERO provider vocabulary): this module contains NO connector names. Connector
names come from the seeds / DB rows, never from code. The seed FILE NAMES
(dim_metric.csv, metric_source_priority.csv) are dbt artefacts, not provider names.

Design mirrors account_topology.py: ``from __future__ import annotations``, module
logger, lazy ``core.db`` imports inside the DB functions, prefixed-ULID mint helper,
pure functions kept separate from I/O so the invariants are testable without Postgres.

Windows/CI note: all log/message strings use ASCII-safe characters only.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (named so they are configurable later, per Dev Notes).
# ---------------------------------------------------------------------------

SCOPE_PLATFORM = "PLATFORM"
SCOPE_ORG = "ORG"
SCOPE_PROJECT = "PROJECT"
# Specificity order for the cascade (least -> most specific).
_SCOPE_RANK = {SCOPE_PLATFORM: 0, SCOPE_ORG: 1, SCOPE_PROJECT: 2}

# Reconciliation methods (mirror the CHECK in migration 049).
METHOD_SUM = "SUM"
METHOD_PRIORITY = "PRIORITY"
METHOD_DEDUP_ID = "DEDUP_ID"
METHOD_ESTIMATE = "ESTIMATE"
METHOD_KEEP_SEPARATE = "KEEP_SEPARATE"
_RECONCILIATION_METHODS = frozenset(
    {METHOD_SUM, METHOD_PRIORITY, METHOD_DEDUP_ID, METHOD_ESTIMATE, METHOD_KEEP_SEPARATE}
)

# The default priority for a connector NOT listed in a PRIORITY seed row. Mirrors
# COALESCE(p.priority, 99) in dbt/models/marts/cross_source_revenue.sql so the socle
# reproduces the mart's ORDER BY exactly.
UNLISTED_PRIORITY = 99

# ---------------------------------------------------------------------------
# Ratio circuit-breaker (Story 32.1 / AD-4)
# ---------------------------------------------------------------------------
# A ratio (ROAS, CTR, CPC, *_rate, ...) is NON-ADDITIVE: summing it across days or
# channels is mathematically wrong, so it must never be declared as an additive metric
# that could land in fact_daily_kpi. Ratios stay first-class -- they are recomputed at
# view time from their additive numerator/denominator (semantic layer / ratio_numerator
# + ratio_denominator). is_ratio_name is the SINGLE source of truth for "is this name a
# ratio?", shared by upsert_metric_definition here and by datamodel.create_target_field.

# Bare canonical ratio names (whole-name match).
_RATIO_EXACT_NAMES = frozenset(
    {
        "roas", "roi", "ctr", "cvr", "cpc", "cpm", "cpa", "cpl", "cpi", "cpv",
        "frequency", "conversion_rate", "bounce_rate", "engagement_rate",
        "click_through_rate", "view_rate", "win_rate",
    }
)
# Suffix families: a name ENDING in one of these tokens after an underscore is a ratio,
# e.g. custom_rate, bounce_pct, meta_roas, google_ctr. The leading underscore anchor keeps
# non-ratio names safe: 'separate' (no underscore before 'rate') and 'rate_card_id' (token
# in the wrong position) do NOT match.
_RATIO_SUFFIX_RE = re.compile(
    r"_(rate|ratio|pct|percent|roas|roi|ctr|cvr|cpc|cpm|cpa|cpl|cpi|cpv|frequency)$",
    re.IGNORECASE,
)


def is_ratio_name(name: str | None) -> bool:
    """True if *name* denotes a non-additive ratio metric (AD-4). Pure, case-insensitive."""
    if not name:
        return False
    normalized = name.strip().lower()
    if normalized in _RATIO_EXACT_NAMES:
        return True
    return bool(_RATIO_SUFFIX_RE.search(normalized))


# ---------------------------------------------------------------------------
# Monetary classification (Story 39.1 / Epic 39). is_ratio_name's twin: the SINGLE
# source of truth for "is this canonical metric MONEY?" -- decided ONCE here, then stored
# on app.metric_definitions.monetary and resolved through the same PROJECT > ORG > PLATFORM
# cascade (E39-FR01). Every money rule (micros 39.2, currency provenance/refusal 39.3, FX
# 39.4/39.5, reconciliation 39.6) keys off this instead of each connector re-deciding.
#
# WHY name/token-based and NOT format='currency'/aggregation_type: `format` is NULL for
# every migrated PLATFORM default (parse_dim_metric never sets it); `cost` and `impressions`
# are both (additive, sum) -- money is an ORTHOGONAL semantic axis. See migration 083 header.
#
# AD-2 status: these are metric-DICTIONARY vocabulary (money nouns: revenue/cost/fee/...),
# NOT provider/connector names -- exactly the same tolerated-config status as the ratio name
# sets above (_RATIO_EXACT_NAMES/_RATIO_SUFFIX_RE) and the seed file names below. The AD-2
# grep in the tests tolerates these explicitly-commented dictionary constants.

# Bare canonical monetary names (whole-name match). Grounded against dbt/seeds/dim_metric.csv:
# the 19 seed metrics partition to monetary == {cost, revenue, ad_revenue, all_revenue,
# conversions_value}; the token rule below picks up ad_revenue/all_revenue/conversions_value
# via the _revenue / _value families, but the exact set makes the 5 explicit and diffable.
_MONETARY_EXACT_NAMES = frozenset(
    {
        "cost", "spend", "revenue", "fee", "refund",
        "ad_revenue", "all_revenue", "conversions_value",
    }
)
# Suffix/word families: a name ENDING (after an underscore) in one of these money tokens is
# monetary, e.g. net_media_cost, platform_fee, refund_amount, gross_revenue, conversions_value.
# The leading-underscore anchor keeps non-money names safe (e.g. 'invalue' would need a real
# '_value' boundary; 'costume' has no '_cost' boundary and is not the bare 'cost').
_MONETARY_TOKEN_RE = re.compile(
    r"_(revenue|cost|spend|fee|refund|amount|value)$",
    re.IGNORECASE,
)


def _classify_monetary(
    canonical_name: str,
    *,
    aggregation_type: str | None = None,
    ratio_numerator: str | None = None,
) -> bool:
    """True if *canonical_name* denotes a MONETARY metric (Story 39.1). Pure, offline.

    Rule (name/token-based, no provider vocabulary):
      1. A RATIO is NEVER monetary. eCPM/CPC/CPA/ROAS are ratios rebuilt at view time
         (AD-4 / E39-FR10); their MONETARY content lives in their additive numerators
         (cost/revenue), which are classified on their own. is_ratio_name short-circuits
         first, so `roas` (numerator=revenue) is non-monetary even though it references a
         monetary metric. ``aggregation_type == 'ratio'`` is treated the same way.
      2. Otherwise monetary iff the name is in _MONETARY_EXACT_NAMES OR matches the
         _MONETARY_TOKEN_RE money-token suffix family.

    Grounding against the 19 seed metrics (dbt/seeds/dim_metric.csv) -- verdict per metric:
        cost              -> True   (spend)
        revenue           -> True   (money)
        ad_revenue        -> True   (money; _revenue family + exact)
        all_revenue       -> True   (money; _revenue family + exact)
        conversions_value -> True   (money; _value family + exact)
        sessions          -> False  (count)
        active_users      -> False  (count)
        conversions       -> False  (count -- note: distinct from conversions_value)
        screen_page_views -> False  (count)
        impressions       -> False  (count)
        clicks            -> False  (count)
        installs          -> False  (count)
        unique_reach      -> False  (count)
        average_position  -> False  (non-additive non-money measure)
        average_frequency -> False  (non-additive non-money measure)
        roas              -> False  (ratio -- short-circuited)
        ctr               -> False  (ratio -- short-circuited)
        cpa               -> False  (ratio -- short-circuited)
        viewability_rate  -> False  (ratio -- short-circuited)
    The token rule DISTINGUISHES the pair: `conversions` (count) is non-monetary while
    `conversions_value` (money, _value family) is monetary.
    """
    if not canonical_name:
        return False
    normalized = canonical_name.strip().lower()
    # (1) ratios are never monetary -- their money lives in their additive numerators.
    if aggregation_type == "ratio" or is_ratio_name(normalized):
        return False
    # (2) name / money-token match.
    if normalized in _MONETARY_EXACT_NAMES:
        return True
    return bool(_MONETARY_TOKEN_RE.search(normalized))


# Seed file names (dbt artefacts -- NOT provider names, AD-2).
DIM_METRIC_SEED = "dim_metric.csv"
METRIC_SOURCE_PRIORITY_SEED = "metric_source_priority.csv"

# Mart routing (Story 27.3). A PRIORITY default declares the pre-computed cross-source
# mart it routes to, derived PURELY from the metric name. These mart NAMES are dbt
# artefacts (warehouse vocabulary), NOT provider names -- same AD-2 status as the seed
# file names above. The routing resolver (metric_reconciliation.py) reads this back.
MART_CROSS_SOURCE_PREFIX = "cross_source_"
# The cross-source marts that actually exist as dbt models today (allow-list). A PRIORITY
# metric with no matching cross_source_<metric> gets target_mart=None (never invented).
_KNOWN_CROSS_SOURCE_MARTS = frozenset({"cross_source_conversions", "cross_source_revenue"})


def _default_target_mart(canonical_name: str) -> str | None:
    """Derive the PRIORITY default target mart for *canonical_name*, or None (PURE).

    Rule: PRIORITY -> cross_source_<metric> IF that dbt model exists, else None (never
    invented). Story 27.3 B.1: conversions -> cross_source_conversions,
    revenue -> cross_source_revenue, everything else (e.g. cost) -> None. This mapping is
    a pure, testable function -- NOT a hard-coded table. It never enters the bit-identical
    seed<->defaults comparison (target_mart is absent from the CSV).
    """
    candidate = f"{MART_CROSS_SOURCE_PREFIX}{canonical_name}"
    return candidate if candidate in _KNOWN_CROSS_SOURCE_MARTS else None


# ---------------------------------------------------------------------------
# Cost pilot (Jean's decision, synthese 6.2). A PLATFORM scaffolding group carrying the
# DEFAULT RULE (KEEP_SEPARATE, overlay never in the totals) of the FUTURE
# verifier x ad-server overlap. Members 'doubleverify'/'ias' are the KNOWN verifiers; the
# ad server (e.g. cm360) will land as a member + become truth_connector when its module
# exists. TODAY neither DV nor IAS emits 'cost' (verified on the manifests) -> the 27.3
# gate does NOT fire on cost, which is correct (one/zero emitting source, invariant 5).
# This group exists to carry the rule, not to produce a premature warning.
#
# AD-2 note: 'doubleverify'/'ias' come from a PRODUCT-DECISION constant (synthese 6.2),
# NOT a runtime scan nor an invented name -- same status as the connectors in the seed
# metric_source_priority.csv (configuration data, not business-code vocabulary). The AD-2
# grep in test_metric_semantics.py tolerates this explicitly-commented config constant.
PLATFORM_COST_VERIFICATION_GROUP = {
    "canonical_name": "cost",
    "name": "cost-verification",
    "members": ["doubleverify", "ias"],  # verifiers (config, per synthese 6.2)
    "method": METHOD_KEEP_SEPARATE,
    "truth_connector": None,  # the ad server (cm360) when its module exists -- reserved
    "target_mart": None,  # KEEP_SEPARATE routes to no mart
}

# Audit action verbs.
ACTION_CREATED = "created"
ACTION_UPSERTED = "upserted"
ACTION_DELETED = "deleted"

_ID_PREFIXES = {
    "metric_definition": "metdef_",
    "source_metric_mapping": "smm_",
    "overlap_group": "ovg_",
    "overlap_group_member": "ovgm_",
    "reconciliation_rule": "recrule_",
    "metric_semantics_audit": "msaudit_",
}


# ---------------------------------------------------------------------------
# Typed errors (callers/endpoints map these; core never raises HTTP here).
# ---------------------------------------------------------------------------


class MetricSemanticsError(Exception):
    """Base for metric-semantics validation errors."""


class InvalidScope(MetricSemanticsError):
    """The (scope_level, org_id, project_id) triplet is inconsistent (mirrors the CHECK)."""


class InvalidReconciliationRule(MetricSemanticsError):
    """A reconciliation rule is missing the field its method requires."""


# ---------------------------------------------------------------------------
# ULID helper (prefixed, per the house pattern).
# ---------------------------------------------------------------------------


def _mint_id(prefix: str) -> str:
    """Mint a new prefixed ULID, e.g. ``_mint_id('metdef_') -> 'metdef_01J...'``."""
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}{ULID()}"


# ---------------------------------------------------------------------------
# Scope validation (the CHECK of migration 049, mirrored in Python so the store
# and the offline fake reject the same inconsistent triplets Postgres would).
# ---------------------------------------------------------------------------


def validate_scope(scope_level: str, org_id: str | None, project_id: str | None) -> None:
    """Raise InvalidScope if the scope triplet is inconsistent (mirrors the CHECK).

    PLATFORM => org_id IS NULL AND project_id IS NULL.
    ORG      => org_id NOT NULL AND project_id IS NULL.
    PROJECT  => project_id NOT NULL (org_id optional -- the project carries its own
                org anchor in app.projects, cf. 035).
    """
    if scope_level == SCOPE_PLATFORM:
        if org_id is not None or project_id is not None:
            raise InvalidScope("PLATFORM scope must have org_id and project_id NULL")
    elif scope_level == SCOPE_ORG:
        if org_id is None or project_id is not None:
            raise InvalidScope("ORG scope requires org_id and a NULL project_id")
    elif scope_level == SCOPE_PROJECT:
        if project_id is None:
            raise InvalidScope("PROJECT scope requires project_id")
    else:
        raise InvalidScope(f"unknown scope_level: {scope_level!r}")


def validate_reconciliation_rule(method: str, rule: dict) -> None:
    """Raise InvalidReconciliationRule if *rule* is incomplete for *method*.

    Applicative (not a SQL CHECK -- the story keeps this out of the DDL):
      * PRIORITY  requires a non-empty ``priority_order``.
      * DEDUP_ID  requires a non-null ``join_key``.
    Other methods carry no mandatory shape here.
    """
    if method not in _RECONCILIATION_METHODS:
        raise InvalidReconciliationRule(f"unknown method: {method!r}")
    if method == METHOD_PRIORITY and not rule.get("priority_order"):
        raise InvalidReconciliationRule("PRIORITY method requires a non-empty priority_order")
    if method == METHOD_DEDUP_ID and not rule.get("join_key"):
        raise InvalidReconciliationRule("DEDUP_ID method requires a join_key")


# ---------------------------------------------------------------------------
# Seed parsing + PURE projection to PLATFORM defaults (offline-testable).
#
# These functions NEVER touch a DB. platform_defaults_from_seeds() is the reference
# structure that import_platform_defaults() writes and the equivalence test compares
# against -- the bit-identical invariant (invariant 3) rendered testable.
# ---------------------------------------------------------------------------


def _default_seeds_dir() -> Path:
    """Locate dbt/seeds relative to this file (server/core/metric_semantics.py).

    server/core/metric_semantics.py -> repo root is three parents up; the seeds live
    at <root>/dbt/seeds. Tests override via the seeds_dir argument (fixtures).
    """
    return Path(__file__).resolve().parents[2] / "dbt" / "seeds"


def _read_seed_rows(seeds_dir: str | Path, filename: str) -> list[dict[str, str]]:
    """Read one CSV seed with the stdlib csv.DictReader (no dbt/duckdb dependency).

    Trailing blank lines (the seeds may end with one) yield no DictReader row, so no
    guard is needed; but we skip a row whose first meaningful column is empty defensively.
    """
    path = Path(seeds_dir) / filename
    rows: list[dict[str, str]] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append({(k or ""): (v if v is not None else "") for k, v in row.items()})
    return rows


def _clean(value: str | None) -> str | None:
    """Normalise a CSV cell: strip; empty string -> None."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def parse_dim_metric(seeds_dir: str | Path | None = None) -> list[dict]:
    """Parse dim_metric.csv into canonical definition dicts (PURE, no DB).

    Translation (bit-exact with the seed):
      name -> canonical_name ; additive ('true'/'false') -> bool ;
      aggregation_rule -> aggregation_type ; ratio_numerator/ratio_denominator ->
      homonymous columns (empty -> None). Fields absent from the seed
      (display_name, format, synonyms, ai_context, non_additive_dimensions) get their
      table defaults (None/[]/{}) -- NOT invented.
    """
    seeds_dir = Path(seeds_dir) if seeds_dir is not None else _default_seeds_dir()
    definitions: list[dict] = []
    for row in _read_seed_rows(seeds_dir, DIM_METRIC_SEED):
        canonical_name = _clean(row.get("name"))
        if not canonical_name:
            continue
        aggregation_type = _clean(row.get("aggregation_rule"))
        ratio_numerator = _clean(row.get("ratio_numerator"))
        definitions.append(
            {
                "canonical_name": canonical_name,
                "aggregation_type": aggregation_type,
                "additive": (row.get("additive", "").strip().lower() == "true"),
                "ratio_numerator": ratio_numerator,
                "ratio_denominator": _clean(row.get("ratio_denominator")),
                # Story 39.1: monetary is DERIVED (pure classifier), NOT a CSV column -- so it
                # is EXCLUDED from the bit-identical seed<->defaults equivalence (invariant 3),
                # exactly as target_mart is excluded from the groups comparison.
                "monetary": _classify_monetary(
                    canonical_name,
                    aggregation_type=aggregation_type,
                    ratio_numerator=ratio_numerator,
                ),
            }
        )
    return definitions


def parse_metric_source_priority(seeds_dir: str | Path | None = None) -> list[dict]:
    """Parse metric_source_priority.csv into one overlap group per metric (PURE, no DB).

    Group by ``metric`` -> one PLATFORM overlap group per metric
    (canonical_name=metric, name=f"{metric}-priority") whose members are the listed
    connectors, plus one PRIORITY reconciliation rule whose ``priority_order`` is the
    connectors sorted by (priority ascending, connector alphabetical) -- reproducing the
    ``ORDER BY COALESCE(priority, 99), connector`` of cross_source_revenue.sql.

    Story 27.3: each group also carries ``target_mart`` derived PURELY from the metric
    name (_default_target_mart) -- NOT read from the CSV (the seed has no such column), so
    the bit-identical seed<->defaults equivalence (invariant 3) is unaffected.
    """
    seeds_dir = Path(seeds_dir) if seeds_dir is not None else _default_seeds_dir()
    # Preserve first-seen order of metrics (the seed order) for stable output.
    grouped: dict[str, list[tuple[int, str]]] = {}
    order: list[str] = []
    for row in _read_seed_rows(seeds_dir, METRIC_SOURCE_PRIORITY_SEED):
        metric = _clean(row.get("metric"))
        connector = _clean(row.get("connector"))
        if not metric or not connector:
            continue
        priority_raw = (row.get("priority") or "").strip()
        try:
            priority = int(priority_raw)
        except ValueError:
            priority = UNLISTED_PRIORITY
        if metric not in grouped:
            grouped[metric] = []
            order.append(metric)
        grouped[metric].append((priority, connector))

    groups: list[dict] = []
    for metric in order:
        members = grouped[metric]
        # Reproduce ORDER BY COALESCE(priority, 99), connector exactly.
        priority_order = [
            connector for _, connector in sorted(members, key=lambda m: (m[0], m[1]))
        ]
        groups.append(
            {
                "canonical_name": metric,
                "name": f"{metric}-priority",
                "members": priority_order,
                "method": METHOD_PRIORITY,
                "priority_order": priority_order,
                # Story 27.3 B.1: the PRIORITY default's routed mart, derived PURELY from
                # the metric name. NOT compared against the CSV (which has no such column);
                # exposed so the routing resolver reads a populated target_mart.
                "target_mart": _default_target_mart(metric),
            }
        )
    return groups


def platform_defaults_from_seeds(seeds_dir: str | Path | None = None) -> dict:
    """PURE projection of the seeds into the canonical PLATFORM-defaults structure.

    Returns:
        {
          "definitions": [ {canonical_name, aggregation_type, additive,
                            ratio_numerator, ratio_denominator}, ... ],   # dim_metric order
          "groups":      [ {canonical_name, name, members, method,
                            priority_order, target_mart}, ... ],          # priority seed order
        }

    This is the reference the bit-identical test (invariant 3) compares against: the
    same structure that import_platform_defaults writes to scope=PLATFORM. Comparing
    canonical structures (not formatted strings) makes the equivalence exact.
    """
    return {
        "definitions": parse_dim_metric(seeds_dir),
        "groups": parse_metric_source_priority(seeds_dir),
    }


# ---------------------------------------------------------------------------
# Cascade resolution -- PURE reducers over already-loaded rows (offline-testable),
# plus DB-backed loaders. Splitting the reduce from the I/O lets the fake store and
# the live table share the exact same specificity logic.
# ---------------------------------------------------------------------------


def _scope_rank(row: dict) -> int:
    """Specificity rank of a row's scope (PLATFORM<ORG<PROJECT). Unknown -> -1."""
    return _SCOPE_RANK.get(row.get("scope_level"), -1)


def reduce_definitions_by_specificity(rows) -> dict[str, dict]:
    """Reduce definition rows to {canonical_name -> most specific row}.

    A funnel merge: for each canonical_name keep the row with the highest scope rank
    (PROJECT beats ORG beats PLATFORM). Deterministic -- a strictly-greater comparison
    means equal ranks keep the first seen.

    CALLER CONTRACT: the caller guarantees AT MOST ONE row per (scope_level,
    canonical_name) -- the unique index uq_metric_definitions_scope_name holds it at the
    DB level, so two rows never share the same rank for the same name and this reducer
    does NOT re-verify uniqueness. Ties across DIFFERENT levels cannot arise either
    (strictly-greater comparison, distinct ranks).
    """
    winner: dict[str, dict] = {}
    for row in rows:
        name = row.get("canonical_name")
        if not name:
            continue
        current = winner.get(name)
        if current is None or _scope_rank(row) > _scope_rank(current):
            winner[name] = row
    return winner


def reduce_reconciliation_by_specificity(rows, metric: str) -> dict | None:
    """Return the most specific overlap-group(+rule) row covering *metric*, or None.

    Filters to rows whose canonical_name == metric, then keeps the highest scope rank.
    Each row is expected to carry the group fields and its rule (method, priority_order,
    ...) already joined. None when no group covers the metric at any level.

    CALLER CONTRACT: the caller guarantees AT MOST ONE group per (scope_level, metric)
    -- the unique index uq_overlap_groups_scope_name plus the one-rule-per-group
    constraint hold it at the DB level, so this reducer does NOT re-verify uniqueness.
    """
    best: dict | None = None
    for row in rows:
        if row.get("canonical_name") != metric:
            continue
        if best is None or _scope_rank(row) > _scope_rank(best):
            best = row
    return best


def _project_org_id(project_id: str) -> str | None:
    """Read app.projects.org_id for *project_id* (None if the project is unknown)."""
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.projects WHERE id = %s",
                    (project_id,),
                )
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        # Fail-soft: an unresolvable project falls back to PLATFORM defaults only,
        # never a crash (adapts has_ready_scope's fail-closed to fail-to-platform).
        logger.warning(
            "metric_semantics: project org lookup failed project=%s: %s", project_id, exc
        )
        return None


def resolve_metric_definitions(project_id: str) -> dict[str, dict]:
    """Return {canonical_name -> resolved definition} for *project_id* (cascade).

    Funnel merge PLATFORM (base) -> ORG (of the project's org) -> PROJECT: the most
    specific row wins, metric by metric. The project's org is read from
    app.projects.org_id (035). A missing project resolves to PLATFORM defaults only
    (fail-soft, never a crash).

    S-4: ``project_id`` is assumed ALREADY AUTHORIZED -- this read has no guard; the calling
    surface must have checked org access first (see the module TRUST CONTRACT)."""
    org_id = _project_org_id(project_id)
    rows = _load_definition_rows(org_id=org_id, project_id=project_id)
    return reduce_definitions_by_specificity(rows)


def is_metric_monetary(canonical_name: str, *, project_id: str | None = None) -> bool:
    """Return whether *canonical_name* is a MONETARY metric (Story 39.1, E39-FR01/AD1).

    The PLATFORM-WIDE entry point every money rule reads instead of re-deciding:
      * ``project_id`` given  -> resolve through the PROJECT > ORG > PLATFORM cascade
        (resolve_metric_definitions) and read the stored ``monetary`` flag, so a project
        that reclassified a custom metric wins (E39-NFR04).
      * ``project_id`` None   -> read the PLATFORM default row's ``monetary``.
    FAIL-SOFT: if no stored row exists (or the DB is unreachable), fall back to the pure
    ``_classify_monetary`` classifier -- never crash, mirroring resolve_*'s fail-to-platform
    posture. An unknown metric name classifies to False (no naked-amount false positive)."""
    try:
        if project_id is not None:
            resolved = resolve_metric_definitions(project_id)
            row = resolved.get(canonical_name)
            if row is not None and row.get("monetary") is not None:
                return bool(row["monetary"])
            # not defined at any scope for this project -> pure classifier fallback.
            return _classify_monetary(canonical_name)
        row = get_metric_definition(scope_level=SCOPE_PLATFORM, canonical_name=canonical_name)
        if row is not None and row.get("monetary") is not None:
            return bool(row["monetary"])
        return _classify_monetary(canonical_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "metric_semantics: is_metric_monetary fell back to classifier for %s: %s",
            canonical_name, exc,
        )
        return _classify_monetary(canonical_name)


def resolve_reconciliation(project_id: str, metric: str) -> dict | None:
    """Return the EFFECTIVE reconciliation rule for (*project_id*, *metric*), or None.

    The most specific overlap group (PROJECT > ORG > PLATFORM) covering *metric*, with
    its rule joined. None when no group covers the metric at any level (= no declared
    overlap; summing across sources stays forbidden without a rule, but 27.1 does not
    apply it -- 27.3 does). For method=PRIORITY, ``priority_order`` is ORDERED, ready to
    reproduce the mart's ORDER BY.

    S-4: ``project_id`` is assumed ALREADY AUTHORIZED -- this read has no guard; the calling
    surface must have checked org access first (see the module TRUST CONTRACT)."""
    org_id = _project_org_id(project_id)
    rows = _load_reconciliation_rows(org_id=org_id, project_id=project_id, metric=metric)
    return reduce_reconciliation_by_specificity(rows, metric)


def resolve_reconciliation_platform(metric: str) -> dict | None:
    """Return the PLATFORM reconciliation rule for *metric*, or None (no cascade).

    A convenience read of the platform default without a project context: loads only
    the PLATFORM overlap group (+ joined rule) covering *metric*."""
    rows = _load_reconciliation_rows(org_id=None, project_id=None, metric=metric)
    return reduce_reconciliation_by_specificity(rows, metric)


# ---------------------------------------------------------------------------
# DB loaders for the cascade (the three scope levels in one SELECT each).
# ---------------------------------------------------------------------------


def _load_definition_rows(*, org_id: str | None, project_id: str | None) -> list[dict]:
    """Load PLATFORM + (org's) ORG + (project's) PROJECT definition rows."""
    from core.db import get_connection  # noqa: PLC0415

    clauses = ["scope_level = 'PLATFORM'"]
    params: list = []
    if org_id is not None:
        clauses.append("(scope_level = 'ORG' AND org_id = %s)")
        params.append(org_id)
    if project_id is not None:
        clauses.append("(scope_level = 'PROJECT' AND project_id = %s)")
        params.append(project_id)
    where = " OR ".join(clauses)
    # SQL clauses are HARD-CODED (scope-level literals, column list, ORDER BY); only the
    # scope VALUES flow in as %s params -- no value is interpolated into the query text.
    # ORDER BY gives a stable row order; the reducer only needs determinism, not sort.
    sql = f"""
        SELECT id, canonical_name, display_name, description, aggregation_type,
               additive, ratio_numerator, ratio_denominator, format, unit,
               currency_mode, non_additive_dimensions, synonyms, ai_context,
               certified, monetary, scope_level, org_id, project_id
        FROM app.metric_definitions
        WHERE {where}
        ORDER BY scope_level, canonical_name
    """
    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(dict(zip(cols, row)))
    return rows


def _load_reconciliation_rows(
    *, org_id: str | None, project_id: str | None, metric: str
) -> list[dict]:
    """Load PLATFORM/ORG/PROJECT overlap groups (+ joined rule) covering *metric*."""
    from core.db import get_connection  # noqa: PLC0415

    clauses = ["g.scope_level = 'PLATFORM'"]
    params: list = [metric]  # first %s is the canonical_name filter below
    if org_id is not None:
        clauses.append("(g.scope_level = 'ORG' AND g.org_id = %s)")
        params.append(org_id)
    if project_id is not None:
        clauses.append("(g.scope_level = 'PROJECT' AND g.project_id = %s)")
        params.append(project_id)
    scope_where = " OR ".join(clauses)
    # SQL clauses are HARD-CODED (scope-level literals, column list, ORDER BY); only the
    # metric + scope VALUES flow in as %s params -- no value is interpolated into the SQL.
    # ORDER BY gives a stable row order; the reducer only needs determinism, not sort.
    sql = f"""
        SELECT g.id AS overlap_group_id, g.canonical_name, g.name, g.scope_level,
               g.org_id, g.project_id, r.method, r.priority_order, r.join_key,
               r.truth_connector, r.target_mart
        FROM app.overlap_groups g
        LEFT JOIN app.reconciliation_rules r ON r.overlap_group_id = g.id
        WHERE g.canonical_name = %s AND ({scope_where})
        ORDER BY g.scope_level, g.canonical_name
    """
    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(dict(zip(cols, row)))
    return rows


# ---------------------------------------------------------------------------
# Append-only audit writer (same-transaction, mirrors insert_audit_row).
# ---------------------------------------------------------------------------


def _write_semantics_audit(
    conn,
    *,
    identity: str,
    action: str,
    entity_type: str,
    entity_id: str,
    scope_level: str,
    org_id: str | None,
    project_id: str | None,
    before: dict | None,
    after: dict | None,
) -> str:
    """Insert one metric_semantics_audit row on an EXISTING transaction.

    Written in the SAME transaction as the mutation so the state change and its proof
    commit or roll back together (atomicity mutation+evidence, like insert_audit_row).
    ``action`` is '<entity_type>.<verb>' (e.g. 'metric_definition.created').
    """
    import json  # noqa: PLC0415

    row_id = _mint_id(_ID_PREFIXES["metric_semantics_audit"])
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.metric_semantics_audit
                (id, identity, action, entity_type, entity_id, scope_level,
                 org_id, project_id, before, after, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, now())
            """,
            (
                row_id,
                identity,
                f"{entity_type}.{action}",
                entity_type,
                entity_id,
                scope_level,
                org_id,
                project_id,
                json.dumps(before) if before is not None else None,
                json.dumps(after) if after is not None else None,
            ),
        )
    return row_id


# ---------------------------------------------------------------------------
# Store CRUD -- metric definitions (the pattern the other entities follow).
#
# Each mutation, on ONE transaction: read before -> UPSERT keyed on the unicity index
# -> write one audit row -> commit. The audit verb is 'created' when there was no
# before row, else 'upserted'.
# ---------------------------------------------------------------------------

_METRIC_DEFINITION_COLUMNS = (
    "id, canonical_name, display_name, description, aggregation_type, additive, "
    "ratio_numerator, ratio_denominator, format, unit, currency_mode, "
    "non_additive_dimensions, synonyms, ai_context, certified, monetary, scope_level, "
    "org_id, project_id, created_by, created_at, updated_at"
)


def _row_to_definition(cols, row) -> dict:
    record: dict = {}
    _ts = {"created_at", "updated_at"}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _ts and val is not None else val
    return record


def _select_definition(conn, *, scope_level, org_id, project_id, canonical_name) -> dict | None:
    """SELECT the definition row at (scope, canonical_name), or None."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_METRIC_DEFINITION_COLUMNS}
            FROM app.metric_definitions
            WHERE scope_level = %s
              AND COALESCE(org_id, '') = COALESCE(%s, '')
              AND COALESCE(project_id, '') = COALESCE(%s, '')
              AND canonical_name = %s
            """,
            (scope_level, org_id, project_id, canonical_name),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_definition(cols, row)


def upsert_metric_definition(
    *,
    canonical_name: str,
    aggregation_type: str,
    additive: bool,
    scope_level: str = SCOPE_PLATFORM,
    org_id: str | None = None,
    project_id: str | None = None,
    created_by: str,
    display_name: str | None = None,
    description: str | None = None,
    ratio_numerator: str | None = None,
    ratio_denominator: str | None = None,
    format: str | None = None,  # noqa: A002 -- column name is 'format'
    unit: str | None = None,
    currency_mode: str | None = None,
    non_additive_dimensions: list[str] | None = None,
    synonyms: list | None = None,
    ai_context: str | None = None,
    certified: bool = False,
    monetary: bool | None = None,
) -> dict:
    """UPSERT one metric definition (keyed on the scope+canonical_name unicity) + audit.

    Returns the persisted row. Emits 'created' audit on first write, 'upserted' when an
    existing row changed. Raises InvalidScope on an inconsistent scope triplet and
    (for ratios) relies on the DB CHECK for the numerator/denominator invariant.

    Ratio circuit-breaker (Story 32.1 / AD-4): a ratio-named metric (or one whose
    aggregation_type is 'ratio') can NEVER be declared additive -- summing it across days
    or channels is mathematically wrong and would corrupt fact_daily_kpi. Declare it with
    additive=False and its ratio_numerator/ratio_denominator instead. Raised before any DB
    access so the guard is unit-testable without Postgres.
    """
    if additive and (is_ratio_name(canonical_name) or aggregation_type == "ratio"):
        raise ValueError(
            f"Le metrique {canonical_name!r} est un ratio (non additif) et ne peut pas "
            "etre declare additive (AD-4) : sommer un ratio sur plusieurs jours ou canaux "
            "est mathematiquement faux et corromprait fact_daily_kpi. Declarez-le avec "
            "additive=False et ses ratio_numerator/ratio_denominator ; le ratio est "
            "recalcule a la volee dans la couche semantique."
        )

    import json  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    validate_scope(scope_level, org_id, project_id)
    new_id = _mint_id(_ID_PREFIXES["metric_definition"])
    non_additive_dimensions = list(non_additive_dimensions or [])
    synonyms_json = json.dumps(synonyms if synonyms is not None else [])
    # Story 39.1: the DB column is NOT NULL. When the caller omits `monetary`, fall back to
    # the single-decision classifier so an omitting caller still stores the right verdict --
    # never send NULL.
    if monetary is None:
        monetary = _classify_monetary(
            canonical_name,
            aggregation_type=aggregation_type,
            ratio_numerator=ratio_numerator,
        )

    with get_connection() as conn:
        before = _select_definition(
            conn,
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            canonical_name=canonical_name,
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.metric_definitions
                    (id, canonical_name, display_name, description, aggregation_type,
                     additive, ratio_numerator, ratio_denominator, format, unit,
                     currency_mode, non_additive_dimensions, synonyms, ai_context,
                     certified, monetary, scope_level, org_id, project_id, created_by,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                        %s, %s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (scope_level, COALESCE(org_id, ''),
                             COALESCE(project_id, ''), canonical_name)
                DO UPDATE SET
                    display_name            = EXCLUDED.display_name,
                    description             = EXCLUDED.description,
                    aggregation_type        = EXCLUDED.aggregation_type,
                    additive                = EXCLUDED.additive,
                    ratio_numerator         = EXCLUDED.ratio_numerator,
                    ratio_denominator       = EXCLUDED.ratio_denominator,
                    format                  = EXCLUDED.format,
                    unit                    = EXCLUDED.unit,
                    currency_mode           = EXCLUDED.currency_mode,
                    non_additive_dimensions = EXCLUDED.non_additive_dimensions,
                    synonyms                = EXCLUDED.synonyms,
                    ai_context              = EXCLUDED.ai_context,
                    certified               = EXCLUDED.certified,
                    monetary                = EXCLUDED.monetary,
                    updated_at              = now()
                RETURNING {_METRIC_DEFINITION_COLUMNS}
                """,
                (
                    new_id,
                    canonical_name,
                    display_name,
                    description,
                    aggregation_type,
                    additive,
                    ratio_numerator,
                    ratio_denominator,
                    format,
                    unit,
                    currency_mode,
                    non_additive_dimensions,
                    synonyms_json,
                    ai_context,
                    certified,
                    monetary,
                    scope_level,
                    org_id,
                    project_id,
                    created_by,
                ),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_definition(cols, row)

        if _definition_changed(before, after):
            _write_semantics_audit(
                conn,
                identity=created_by,
                action=ACTION_CREATED if before is None else ACTION_UPSERTED,
                entity_type="metric_definition",
                entity_id=after["id"],
                scope_level=scope_level,
                org_id=org_id,
                project_id=project_id,
                before=before,
                after=after,
            )
        conn.commit()
    return after


# Fields that carry no semantic change (identity/timestamps) -- excluded from the
# idempotency comparison so a re-import with identical content emits NO 'upserted' audit.
_DEFINITION_VOLATILE = frozenset({"id", "created_at", "updated_at"})


def _definition_changed(before: dict | None, after: dict) -> bool:
    """True when the semantic content of a definition changed (idempotency guard).

    On first write ``before`` is None -> changed. Otherwise compare every non-volatile
    field; a re-UPSERT with identical content returns False so no 'upserted' audit is
    written (import idempotency, test §24)."""
    if before is None:
        return True
    for key, value in after.items():
        if key in _DEFINITION_VOLATILE:
            continue
        if before.get(key) != value:
            return True
    return False


def get_metric_definition(
    *,
    scope_level: str,
    canonical_name: str,
    org_id: str | None = None,
    project_id: str | None = None,
) -> dict | None:
    """Return the definition row at (scope, canonical_name), or None."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        return _select_definition(
            conn,
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            canonical_name=canonical_name,
        )


def list_metric_definitions_by_scope(
    *, scope_level: str, org_id: str | None = None, project_id: str | None = None
) -> list[dict]:
    """List every definition at a given scope (ordered by canonical_name)."""
    from core.db import get_connection  # noqa: PLC0415

    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_METRIC_DEFINITION_COLUMNS}
                FROM app.metric_definitions
                WHERE scope_level = %s
                  AND COALESCE(org_id, '') = COALESCE(%s, '')
                  AND COALESCE(project_id, '') = COALESCE(%s, '')
                ORDER BY canonical_name
                """,
                (scope_level, org_id, project_id),
            )
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(_row_to_definition(cols, row))
    return rows


def delete_metric_definition(
    *,
    scope_level: str,
    canonical_name: str,
    org_id: str | None = None,
    project_id: str | None = None,
    identity: str,
) -> bool:
    """Delete the definition at (scope, canonical_name) + audit. Returns True if a row went.

    Emits a 'deleted' audit (after=None) only when a row actually existed."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        before = _select_definition(
            conn,
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            canonical_name=canonical_name,
        )
        if before is None:
            return False
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.metric_definitions WHERE id = %s",
                (before["id"],),
            )
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_DELETED,
            entity_type="metric_definition",
            entity_id=before["id"],
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            before=before,
            after=None,
        )
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# Store CRUD -- overlap groups (+ members) and reconciliation rules.
# ---------------------------------------------------------------------------

_OVERLAP_GROUP_COLUMNS = (
    "id, metric_definition_id, canonical_name, name, description, scope_level, "
    "org_id, project_id, created_by, created_at, updated_at"
)


def _row_to_group(cols, row) -> dict:
    record: dict = {}
    _ts = {"created_at", "updated_at"}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _ts and val is not None else val
    return record


def _select_group(conn, *, scope_level, org_id, project_id, name) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_OVERLAP_GROUP_COLUMNS}
            FROM app.overlap_groups
            WHERE scope_level = %s
              AND COALESCE(org_id, '') = COALESCE(%s, '')
              AND COALESCE(project_id, '') = COALESCE(%s, '')
              AND name = %s
            """,
            (scope_level, org_id, project_id, name),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_group(cols, row)


def upsert_overlap_group(
    *,
    canonical_name: str,
    name: str,
    scope_level: str = SCOPE_PLATFORM,
    org_id: str | None = None,
    project_id: str | None = None,
    created_by: str,
    metric_definition_id: str | None = None,
    description: str | None = None,
) -> dict:
    """UPSERT one overlap group (keyed on scope+name unicity) + audit."""
    from core.db import get_connection  # noqa: PLC0415

    validate_scope(scope_level, org_id, project_id)
    new_id = _mint_id(_ID_PREFIXES["overlap_group"])

    with get_connection() as conn:
        before = _select_group(
            conn, scope_level=scope_level, org_id=org_id, project_id=project_id, name=name
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.overlap_groups
                    (id, metric_definition_id, canonical_name, name, description,
                     scope_level, org_id, project_id, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (scope_level, COALESCE(org_id, ''),
                             COALESCE(project_id, ''), name)
                DO UPDATE SET
                    metric_definition_id = EXCLUDED.metric_definition_id,
                    canonical_name       = EXCLUDED.canonical_name,
                    description          = EXCLUDED.description,
                    updated_at           = now()
                RETURNING {_OVERLAP_GROUP_COLUMNS}
                """,
                (
                    new_id,
                    metric_definition_id,
                    canonical_name,
                    name,
                    description,
                    scope_level,
                    org_id,
                    project_id,
                    created_by,
                ),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_group(cols, row)

        if _group_changed(before, after):
            _write_semantics_audit(
                conn,
                identity=created_by,
                action=ACTION_CREATED if before is None else ACTION_UPSERTED,
                entity_type="overlap_group",
                entity_id=after["id"],
                scope_level=scope_level,
                org_id=org_id,
                project_id=project_id,
                before=before,
                after=after,
            )
        conn.commit()
    return after


_GROUP_VOLATILE = frozenset({"id", "created_at", "updated_at"})


def _group_changed(before: dict | None, after: dict) -> bool:
    if before is None:
        return True
    for key, value in after.items():
        if key in _GROUP_VOLATILE:
            continue
        if before.get(key) != value:
            return True
    return False


def set_overlap_group_members(
    *, overlap_group_id: str, connectors: list[str], created_by: str,
    scope_level: str, org_id: str | None = None, project_id: str | None = None,
) -> list[dict]:
    """Ensure *connectors* are members of a group (ADDITIVE, idempotent) + audit.

    ADDITIVE by design in 27.1: adds any missing member (one audit 'created' each) and
    never re-adds an existing one (idempotency), but NEVER REMOVES a member that is no
    longer in *connectors*. A shrinking call is therefore a no-op for the dropped member
    -- it survives. Member REMOVAL (and whether it should audit / cascade the rule's
    priority_order) is out of scope for 27.1; 27.3 decides the removal semantics.
    account_scope stays NULL in v1. Returns the current member rows."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        existing = _select_group_member_connectors(conn, overlap_group_id)
        for connector in connectors:
            if connector in existing:
                continue
            member_id = _mint_id(_ID_PREFIXES["overlap_group_member"])
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.overlap_group_members
                        (id, overlap_group_id, connector, account_scope, created_at)
                    VALUES (%s, %s, %s, NULL, now())
                    ON CONFLICT (overlap_group_id, connector, account_scope) DO NOTHING
                    RETURNING id
                    """,
                    (member_id, overlap_group_id, connector),
                )
                inserted = cur.fetchone()
            if inserted is not None:
                _write_semantics_audit(
                    conn,
                    identity=created_by,
                    action=ACTION_CREATED,
                    entity_type="overlap_group_member",
                    entity_id=inserted[0],
                    scope_level=scope_level,
                    org_id=org_id,
                    project_id=project_id,
                    before=None,
                    after={"overlap_group_id": overlap_group_id, "connector": connector},
                )
        conn.commit()
        return _list_group_members(conn, overlap_group_id)


def _select_group_member_connectors(conn, overlap_group_id: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT connector FROM app.overlap_group_members WHERE overlap_group_id = %s",
            (overlap_group_id,),
        )
        return {r[0] for r in cur.fetchall()}


def _list_group_members(conn, overlap_group_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, overlap_group_id, connector, account_scope "
            "FROM app.overlap_group_members WHERE overlap_group_id = %s "
            "ORDER BY connector",
            (overlap_group_id,),
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


_RECONCILIATION_RULE_COLUMNS = (
    "id, overlap_group_id, method, priority_order, join_key, truth_connector, "
    "target_mart, created_by, created_at, updated_at"
)


def _row_to_rule(cols, row) -> dict:
    record: dict = {}
    _ts = {"created_at", "updated_at"}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _ts and val is not None else val
    return record


def upsert_reconciliation_rule(
    *,
    overlap_group_id: str,
    method: str,
    created_by: str,
    scope_level: str,
    org_id: str | None = None,
    project_id: str | None = None,
    priority_order: list[str] | None = None,
    join_key: str | None = None,
    truth_connector: str | None = None,
    target_mart: str | None = None,
) -> dict:
    """UPSERT the single living rule of a group (keyed on overlap_group_id) + audit.

    Validates the method's required shape (PRIORITY -> priority_order, DEDUP_ID ->
    join_key) applicatively before writing (InvalidReconciliationRule)."""
    import json  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    validate_reconciliation_rule(
        method, {"priority_order": priority_order, "join_key": join_key}
    )
    new_id = _mint_id(_ID_PREFIXES["reconciliation_rule"])
    priority_json = json.dumps(priority_order) if priority_order is not None else None

    with get_connection() as conn:
        before = _select_rule(conn, overlap_group_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.reconciliation_rules
                    (id, overlap_group_id, method, priority_order, join_key,
                     truth_connector, target_mart, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, now(), now())
                ON CONFLICT (overlap_group_id)
                DO UPDATE SET
                    method          = EXCLUDED.method,
                    priority_order  = EXCLUDED.priority_order,
                    join_key        = EXCLUDED.join_key,
                    truth_connector = EXCLUDED.truth_connector,
                    target_mart     = EXCLUDED.target_mart,
                    updated_at      = now()
                RETURNING {_RECONCILIATION_RULE_COLUMNS}
                """,
                (
                    new_id,
                    overlap_group_id,
                    method,
                    priority_json,
                    join_key,
                    truth_connector,
                    target_mart,
                    created_by,
                ),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_rule(cols, row)

        if _rule_changed(before, after):
            _write_semantics_audit(
                conn,
                identity=created_by,
                action=ACTION_CREATED if before is None else ACTION_UPSERTED,
                entity_type="reconciliation_rule",
                entity_id=after["id"],
                scope_level=scope_level,
                org_id=org_id,
                project_id=project_id,
                before=before,
                after=after,
            )
        conn.commit()
    return after


def _select_rule(conn, overlap_group_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_RECONCILIATION_RULE_COLUMNS} FROM app.reconciliation_rules "
            "WHERE overlap_group_id = %s",
            (overlap_group_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_rule(cols, row)


_RULE_VOLATILE = frozenset({"id", "created_at", "updated_at"})


def _rule_changed(before: dict | None, after: dict) -> bool:
    if before is None:
        return True
    for key, value in after.items():
        if key in _RULE_VOLATILE:
            continue
        if before.get(key) != value:
            return True
    return False


# ---------------------------------------------------------------------------
# Store CRUD -- source metric mappings (schema only in 27.1; here for completeness).
# ---------------------------------------------------------------------------

_SOURCE_MAPPING_COLUMNS = (
    "id, metric_definition_id, connector, source_field_path, extraction_note, "
    "status, scope_level, org_id, project_id, created_by, created_at, updated_at"
)


def _row_to_mapping(cols, row) -> dict:
    record: dict = {}
    _ts = {"created_at", "updated_at"}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _ts and val is not None else val
    return record


def _select_mapping(
    conn, *, scope_level, org_id, project_id, metric_definition_id, connector
) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_SOURCE_MAPPING_COLUMNS}
            FROM app.source_metric_mappings
            WHERE scope_level = %s
              AND COALESCE(org_id, '') = COALESCE(%s, '')
              AND COALESCE(project_id, '') = COALESCE(%s, '')
              AND metric_definition_id = %s
              AND connector = %s
            """,
            (scope_level, org_id, project_id, metric_definition_id, connector),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_mapping(cols, row)


def upsert_source_metric_mapping(
    *,
    metric_definition_id: str,
    connector: str,
    created_by: str,
    scope_level: str = SCOPE_PLATFORM,
    org_id: str | None = None,
    project_id: str | None = None,
    source_field_path: str | None = None,
    extraction_note: str | None = None,
    status: str = "proposed",
) -> dict:
    """UPSERT one source-metric mapping (keyed on scope+definition+connector) + audit."""
    from core.db import get_connection  # noqa: PLC0415

    validate_scope(scope_level, org_id, project_id)
    new_id = _mint_id(_ID_PREFIXES["source_metric_mapping"])

    with get_connection() as conn:
        before = _select_mapping(
            conn,
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            metric_definition_id=metric_definition_id,
            connector=connector,
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.source_metric_mappings
                    (id, metric_definition_id, connector, source_field_path,
                     extraction_note, status, scope_level, org_id, project_id,
                     created_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (scope_level, COALESCE(org_id, ''),
                             COALESCE(project_id, ''), metric_definition_id, connector)
                DO UPDATE SET
                    source_field_path = EXCLUDED.source_field_path,
                    extraction_note   = EXCLUDED.extraction_note,
                    status            = EXCLUDED.status,
                    updated_at        = now()
                RETURNING {_SOURCE_MAPPING_COLUMNS}
                """,
                (
                    new_id,
                    metric_definition_id,
                    connector,
                    source_field_path,
                    extraction_note,
                    status,
                    scope_level,
                    org_id,
                    project_id,
                    created_by,
                ),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_mapping(cols, row)

        if _mapping_changed(before, after):
            _write_semantics_audit(
                conn,
                identity=created_by,
                action=ACTION_CREATED if before is None else ACTION_UPSERTED,
                entity_type="source_metric_mapping",
                entity_id=after["id"],
                scope_level=scope_level,
                org_id=org_id,
                project_id=project_id,
                before=before,
                after=after,
            )
        conn.commit()
    return after


_MAPPING_VOLATILE = frozenset({"id", "created_at", "updated_at"})


def _mapping_changed(before: dict | None, after: dict) -> bool:
    if before is None:
        return True
    for key, value in after.items():
        if key in _MAPPING_VOLATILE:
            continue
        if before.get(key) != value:
            return True
    return False


# ---------------------------------------------------------------------------
# Seed import -- write the PLATFORM defaults from the seeds (idempotent).
# ---------------------------------------------------------------------------


def import_platform_defaults(
    *, seeds_dir: str | Path | None = None, identity: str = "system"
) -> dict:
    """Read the dbt seeds and UPSERT the matching scope=PLATFORM rows (idempotent).

    Writes app.metric_definitions (from dim_metric.csv) + app.overlap_groups /
    _members / reconciliation_rules PRIORITY (from metric_source_priority.csv).
    IDEMPOTENT: re-running creates no duplicate (UPSERT keyed on scope unicity) and
    emits an 'upserted' audit only on a real change. Returns counts
    {definitions, groups, members, rules} for observability."""
    defaults = platform_defaults_from_seeds(seeds_dir)

    counts = {"definitions": 0, "groups": 0, "members": 0, "rules": 0}
    for definition in defaults["definitions"]:
        upsert_metric_definition(
            canonical_name=definition["canonical_name"],
            aggregation_type=definition["aggregation_type"],
            additive=definition["additive"],
            ratio_numerator=definition["ratio_numerator"],
            ratio_denominator=definition["ratio_denominator"],
            monetary=definition["monetary"],  # Story 39.1 (seeded by _classify_monetary)
            scope_level=SCOPE_PLATFORM,
            created_by=identity,
        )
        counts["definitions"] += 1

    for group in defaults["groups"]:
        persisted = upsert_overlap_group(
            canonical_name=group["canonical_name"],
            name=group["name"],
            scope_level=SCOPE_PLATFORM,
            created_by=identity,
        )
        counts["groups"] += 1
        members = set_overlap_group_members(
            overlap_group_id=persisted["id"],
            connectors=group["members"],
            created_by=identity,
            scope_level=SCOPE_PLATFORM,
        )
        counts["members"] += len(members)
        upsert_reconciliation_rule(
            overlap_group_id=persisted["id"],
            method=group["method"],
            priority_order=group["priority_order"],
            target_mart=group.get("target_mart"),  # Story 27.3 B.1 (NULL when not routable)
            scope_level=SCOPE_PLATFORM,
            created_by=identity,
        )
        counts["rules"] += 1

    # Story 27.3 B.2: the cost-verification KEEP_SEPARATE pilot. A SUPPLEMENTARY PLATFORM
    # group (outside the seed CSV) carrying the default rule of the future
    # verifier x ad-server overlap. Idempotent like the others; it adds NO metric_definition
    # (cost is already defined by dim_metric.csv) -- only a group + members + rule.
    _import_cost_verification_pilot(counts, identity=identity)

    logger.info(
        "metric_semantics: imported platform defaults defs=%d groups=%d members=%d rules=%d",
        counts["definitions"],
        counts["groups"],
        counts["members"],
        counts["rules"],
    )
    return counts


def _import_cost_verification_pilot(counts: dict, *, identity: str) -> None:
    """UPSERT the cost-verification KEEP_SEPARATE pilot group + members + rule (idempotent).

    Story 27.3 B.2. A supplementary PLATFORM group outside the seed CSV, sourced from the
    documented product-decision constant PLATFORM_COST_VERIFICATION_GROUP. Idempotent: the
    same _group_changed / _rule_changed guards as the seed groups mean a second import emits
    no 'upserted' audit. Adds NO metric_definition (cost is defined by dim_metric.csv).
    Increments the shared *counts* dict in place (groups/members/rules) for observability.
    """
    pilot = PLATFORM_COST_VERIFICATION_GROUP
    persisted = upsert_overlap_group(
        canonical_name=pilot["canonical_name"],
        name=pilot["name"],
        scope_level=SCOPE_PLATFORM,
        created_by=identity,
    )
    counts["groups"] += 1
    members = set_overlap_group_members(
        overlap_group_id=persisted["id"],
        connectors=pilot["members"],
        created_by=identity,
        scope_level=SCOPE_PLATFORM,
    )
    counts["members"] += len(members)
    upsert_reconciliation_rule(
        overlap_group_id=persisted["id"],
        method=pilot["method"],
        truth_connector=pilot["truth_connector"],
        target_mart=pilot["target_mart"],
        scope_level=SCOPE_PLATFORM,
        created_by=identity,
    )
    counts["rules"] += 1
