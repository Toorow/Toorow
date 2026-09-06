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




def _connection(conn=None):
    """The caller's connection when it hands one over, a fresh one otherwise.

    THE LENS OUTLIVED ITS TRANSACTION (found 2026-08-30): the Governance
    `metric-definitions` lens read this store through a connection of its own, so
    a row the caller had just written inside its transaction -- the seeded
    metric definition of `test_governance_object_traversal_pg` -- was invisible,
    and the lens answered an empty list on a store that held the row. A reader
    that opens its own connection cannot see what its caller has not committed;
    threading the caller's connection through is the only honest read.
    """
    from contextlib import nullcontext  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    return nullcontext(conn) if conn is not None else get_connection()


def _project_org_id(project_id: str, conn=None) -> str | None:
    """Read app.projects.org_id for *project_id* (None if the project is unknown)."""
    try:
        with _connection(conn) as connection:
            with connection.cursor() as cur:
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


def resolve_metric_definitions(project_id: str, conn=None) -> dict[str, dict]:
    """Return {canonical_name -> resolved definition} for *project_id* (cascade).

    Funnel merge PLATFORM (base) -> ORG (of the project's org) -> PROJECT: the most
    specific row wins, metric by metric. The project's org is read from
    app.projects.org_id (035). A missing project resolves to PLATFORM defaults only
    (fail-soft, never a crash).

    S-4: ``project_id`` is assumed ALREADY AUTHORIZED -- this read has no guard; the calling
    surface must have checked org access first (see the module TRUST CONTRACT)."""
    org_id = _project_org_id(project_id, conn=conn)
    rows = _load_definition_rows(org_id=org_id, project_id=project_id, conn=conn)
    return reduce_definitions_by_specificity(rows)


def _semantic_model_declares_money(
    canonical_name: str, project_id: str | None, conn=None
) -> bool | None:
    """``True``/``False`` when a PUBLISHED Concept answers for this metric, else None.

    The Semantic Model states a metric's type on its version:
    ``semantic_concept_versions.value_type`` carries ``'money'`` among its ten
    values (migration 142:139-142). That is a DECLARATION a person published, not
    a guess from a name or a unit -- which is exactly why story 48.3 stopped
    classifying money from ``unit IS NOT NULL`` and moved
    ``capability_compilers.MoneyCapability`` onto this same predicate. This is
    that predicate, asked for one metric.

    Only ``published`` versions answer, and platform Concepts (``project_id IS
    NULL``) answer alongside the Project's own -- the same shape
    ``capability_compilers`` reads, so the compiler's monetary set and this
    function cannot disagree about a metric.

    ``project_id`` MAY BE ``None``, and asking is then still the right thing --
    repaired 2026-08-31. ``is_metric_monetary`` used to skip this function
    entirely without a Project, on the stated reason that "a Concept is scoped to
    a Project or to the platform catalogue, and asking without a Project would
    answer from a scope the caller never named". The clause ``OR c.project_id IS
    NULL`` below refutes it: without a Project this query reads the PLATFORM
    catalogue and nothing else -- exactly the scope a caller who named no Project
    is asking about. Measured before the repair, against a published platform
    Concept declaring ``value_type = 'money'``: *semantic model asked: False*.

    ``None`` means "no published Concept carries this name": NOT "not money".
    The caller falls through to the lower store rather than reading silence as a
    verdict.
    """
    with _connection(conn) as connection:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT v.value_type
                  FROM app.semantic_concepts c
                  JOIN app.semantic_concept_versions v ON v.id = c.current_version_id
                 WHERE c.lifecycle_status = 'published'
                   AND (c.project_id = %s OR c.project_id IS NULL)
                   AND v.kind = 'metric'
                   AND c.name = %s
                 ORDER BY c.project_id NULLS LAST
                 LIMIT 1
                """,
                (project_id, canonical_name),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return str(row[0]) == "money"


def is_metric_monetary(canonical_name: str, *, project_id: str | None = None) -> bool:
    """Return whether *canonical_name* is a MONETARY metric (Story 39.1, E39-FR01/AD1).

    The PLATFORM-WIDE entry point every money rule reads instead of re-deciding.

    SEMANTIC MODEL FIRST SINCE 2026-08-25 (story 49.3 AC1), and this is the same
    repair story 60.2 made to additivity in ``resolve_declared_additivity`` --
    ``is_metric_monetary``'s twin, which said so in its own header while reading
    the opposite order. ``app.metric_definitions`` was the ONLY store consulted
    here, and its authoring doors are retired: a project that reclassifies a
    metric now does it in the Concept workbench, and E39-NFR04 ("a project that
    reclassified a custom metric wins") holds through the store that has the
    version, the review and the last word.

    The order, most authoritative first:
      1. a PUBLISHED Concept version's ``value_type = 'money'``
         (:func:`_semantic_model_declares_money`) -- asked WITH or WITHOUT a
         Project, because its scope clause reads the platform catalogue on its
         own when there is no Project to name;
      2. then ``app.metric_definitions`` through the PROJECT > ORG > PLATFORM
         cascade (``resolve_metric_definitions``) and its stored ``monetary``
         flag -- the layer below, for a metric no published Concept carries;
      3. ``project_id`` None and no Concept -> the PLATFORM definition row's
         ``monetary``, then the classifier.

    THE NULL-PROJECT CARVE-OUT IS GONE, 2026-08-31. Until then step 1 was skipped
    entirely without a Project, so a published platform Concept declaring
    ``value_type = 'money'`` was never consulted and the name classifier decided
    -- for every caller that reaches here without a Project, which is
    ``datamodel.get_target_field`` and ``currency_refusal._is_monetary_metric``
    among others. The carve-out's stated reason ("asking without a Project would
    answer from a scope the caller never named") was refuted by its own SQL: the
    query already carries ``OR c.project_id IS NULL``, so with no Project it
    reads the platform catalogue and nothing else. The store with the version,
    the review and the last word now answers first on BOTH paths, which is what
    ``governance.md`` requires -- *a reader classifies a metric as money without
    asking the Semantic Model first* is one of its `Incomplete if` clauses.

    FAIL-SOFT at every step: an unreachable database (or a store that answers
    nothing) falls through to the pure ``_classify_monetary`` classifier -- never
    a crash, mirroring resolve_*'s fail-to-platform posture. An unknown metric
    name classifies to False (no naked-amount false positive)."""
    try:
        try:
            declared = _semantic_model_declares_money(canonical_name, project_id)
        except Exception as exc:  # noqa: BLE001 -- fall through to the lower store
            logger.warning(
                "metric_semantics: semantic model unreadable for %s in project=%s: %s",
                canonical_name,
                project_id,
                exc,
            )
            declared = None
        if declared is not None:
            return declared
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


# ---------------------------------------------------------------------------
# Declared additivity (Story 60.2) -- is_metric_monetary's twin.
# ---------------------------------------------------------------------------
#
# THE DEFECT THIS ENTRY POINT CLOSES, MEASURED. Additivity was declared in three
# places and read at render time in none of them:
#   * app.semantic_concept_versions.additivity_class (142:157-160), shown by
#     governance_read_model.py:756 and consumed by nothing else;
#   * app.metric_definitions.additive (049:83), cascaded but never consulted by a
#     roll-up;
#   * two frozensets of FOUR literal names (cards.py:163, rollup.py:58) plus a
#     name suffix (is_ratio_name), which is what actually decided.
# A client ratio named `efficiency_index` matches no frozenset and no suffix, so
# it was summed across days: 22,25 EUR printed where 11,90 EUR is the number.
#
# The three classes are the SCHEMA's three -- additive / semi_additive /
# non_additive (142:157-160, semantic_expressions.ADDITIVITY_CLASSES). No fourth
# vocabulary is minted here.

#: Mirrors `core.semantic_expressions.ADDITIVITY_CLASSES`. Kept as a literal so
#: this module does not import the expression contract for one frozenset; the
#: equality is asserted by `test_calculated_field_additivity.py`.
ADDITIVITY_ADDITIVE = "additive"
ADDITIVITY_SEMI_ADDITIVE = "semi_additive"
ADDITIVITY_NON_ADDITIVE = "non_additive"
ADDITIVITY_CLASSES = frozenset(
    {ADDITIVITY_ADDITIVE, ADDITIVITY_SEMI_ADDITIVE, ADDITIVITY_NON_ADDITIVE}
)


def _load_declared_additivity_rows(project_id: str, conn=None) -> list[tuple[str, str]]:
    """(metric name, additivity_class) of this Project's PUBLISHED metric versions.

    The Semantic Model is the authority governance.md names ("The Semantic Model
    owns canonical metrics, dimensions, relationships, aggregation behavior"), so
    it is read first and wins over `app.metric_definitions`. Only `published`
    versions answer: a draft is a proposal, and a proposal must not change a
    number that is already on a screen.
    """
    sql = """
        SELECT DISTINCT ON (v.name) v.name, v.additivity_class
        FROM app.semantic_concept_versions v
        WHERE v.project_id = %s
          AND v.kind = 'metric'
          AND v.status = 'published'
          AND v.additivity_class IS NOT NULL
        ORDER BY v.name, v.version_number DESC
    """
    with _connection(conn) as connection:
        with connection.cursor() as cur:
            cur.execute(sql, (project_id,))
            return [(str(name), str(klass)) for name, klass in cur.fetchall()]


def resolve_declared_additivity(project_id: str | None) -> dict[str, str]:
    """Return ``{canonical_name -> additivity class}`` DECLARED for *project_id*.

    Two declaring stores, most authoritative first:

      1. ``app.semantic_concept_versions.additivity_class`` -- the Semantic Model
         class a person chose in the Concept workbench. Migration 237 makes it
         NOT NULL for every metric version, so a row here always answers.
      2. ``app.metric_definitions`` through the PROJECT > ORG > PLATFORM cascade:
         ``additive = FALSE`` reads as ``non_additive``, and an ``additive``
         definition that names ``non_additive_dimensions`` reads as
         ``semi_additive`` -- the same reading `validate_aggregation` enforces on
         the way in (`semantic_expressions.py:746-763`).

    ONLY DECLARATIONS ARE RETURNED. A metric absent from the result is a metric
    NOBODY declared, and the caller keeps its own platform default rather than
    receiving a guess dressed as an answer. This is the whole difference from
    `is_ratio_name`, which answers for every string ever passed to it.

    FAIL-SOFT, deliberately: an unreachable database returns ``{}``, so a render
    degrades to the platform defaults it had before this function existed instead
    of failing. It never returns a *wider* answer than it read.

    S-4: ``project_id`` is assumed ALREADY AUTHORIZED -- this read has no guard;
    the calling surface must have checked access first (module TRUST CONTRACT).
    """
    if not project_id:
        return {}
    declared: dict[str, str] = {}
    try:
        for name, klass in _load_declared_additivity_rows(project_id):
            if klass in ADDITIVITY_CLASSES:
                declared[name] = klass
    except Exception as exc:  # noqa: BLE001 -- fail-soft to platform defaults
        logger.warning(
            "metric_semantics: declared additivity unreadable for project=%s: %s",
            project_id,
            exc,
        )
    try:
        for name, row in resolve_metric_definitions(project_id).items():
            if name in declared:
                continue  # the Semantic Model already answered for this metric.
            additive = row.get("additive")
            if additive is None:
                continue
            if not additive:
                declared[name] = ADDITIVITY_NON_ADDITIVE
            elif list(row.get("non_additive_dimensions") or ()):
                declared[name] = ADDITIVITY_SEMI_ADDITIVE
            else:
                declared[name] = ADDITIVITY_ADDITIVE
    except Exception as exc:  # noqa: BLE001 -- fail-soft, same posture
        logger.warning(
            "metric_semantics: metric definitions unreadable for project=%s: %s",
            project_id,
            exc,
        )
    return declared


def declared_non_additive(declared: dict[str, str]) -> frozenset[str]:
    """The names of *declared* that must NOT be summed. Pure, offline-testable.

    ``semi_additive`` is on this side of the line: it means "summable across SOME
    dimensions", and a roll-up that does not know which ones cannot tell whether
    the one in front of it is allowed. Treating it as summable is the failure the
    class exists to name.
    """
    return frozenset(
        name for name, klass in declared.items() if klass != ADDITIVITY_ADDITIVE
    )






def reference_reconciliation(*, project_id: str | None, metric: str) -> dict | None:
    """The `reconciliation` entry of the reference layer, from the GOVERNED Rule Set.

    ONE resolver for the two read surfaces (REST reference + MCP tool), and the
    same one the runtime uses -- :func:`core.controls_quality.governed_runtime_rule`.
    Until AI-295 both surfaces resolved this through the PLATFORM > ORG > PROJECT
    cascade over ``app.overlap_groups``, the store Story 49.4 retired for the
    runtime. A screen and the engine behind it therefore answered from two
    different models, which `governance.md` forbids in as many words.

    Measured against a live database before the cut, and this is why the cascade
    could not stay: ``_load_reconciliation_rows(project_id="proj_EXAMPLE")`` -- a
    project that exists nowhere -- returned the PLATFORM ``cost-verification``
    group with ``method=KEEP_SEPARATE``, while ``governed_runtime_rule`` returned
    ``None`` for the same pair. A rule served for nobody is worse than an empty
    one: it is a rule a person can act on.

    ``project_id is None`` yields ``None``, and that is the model rather than a
    limitation: a governed rule belongs to the Project that published it, so an
    ORG-scoped read of the reference has no reconciliation to serve. The caller
    keeps its default behaviour instead of borrowing a rule from a scope it
    cannot see.

    Fail-soft, and the reason is the caller rather than the store: both surfaces
    build a WHOLE ``metrics[]`` list, one entry per canonical metric. A policy
    read that raises would take the entire reference down -- every metric, over
    one unreadable rule -- so it degrades to "no rule" here, which is the answer
    that forbids summing rather than the one that permits it. Held by
    `tests/core/test_reference_reconciliation_is_governed.py`, which failed
    before this clause existed.
    """
    if not project_id:
        return None
    from core.controls_quality import governed_runtime_rule  # noqa: PLC0415

    try:
        rule = governed_runtime_rule(project_id, metric)
    except Exception as exc:  # noqa: BLE001 -- an unreadable policy is "no rule"
        logger.warning(
            "metric_semantics: governed reconciliation unreadable project=%s metric=%s: %s",
            project_id,
            metric,
            exc,
        )
        return None
    if rule is None:
        return None
    return {
        "method": rule.get("method"),
        "priority_order": rule.get("priority_order"),
        "join_key": rule.get("join_key"),
        "truth_connector": rule.get("truth_connector"),
        "resolved_scope": rule.get("scope_level"),
        # The published version, so a reader can name the exact policy that
        # answered rather than infer it from a scope label.
        "rule_set_version_id": rule.get("rule_set_version_id"),
    }


# ---------------------------------------------------------------------------
# DB loaders for the cascade (the three scope levels in one SELECT each).
# ---------------------------------------------------------------------------


def _load_definition_rows(
    *, org_id: str | None, project_id: str | None, conn=None
) -> list[dict]:
    """Load PLATFORM + (org's) ORG + (project's) PROJECT definition rows."""

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
    with _connection(conn) as connection:
        with connection.cursor() as cur:
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
            f"Metric {canonical_name!r} is a ratio (non-additive) and cannot "
            "etre declare additive (AD-4) : sommer un ratio sur plusieurs jours ou canaux "
            "is mathematically wrong and would corrupt fact_daily_kpi. Declare it with "
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

    Writes app.metric_definitions (from dim_metric.csv), and NOTHING ELSE.
    IDEMPOTENT: re-running creates no duplicate (UPSERT keyed on scope unicity) and
    emits an 'upserted' audit only on a real change. Returns counts
    {definitions, groups, members, rules} for observability -- the last three are
    kept at 0 rather than dropped, so an existing caller reading them gets the
    honest count instead of a KeyError.

    WHAT IT NO LONGER WRITES, and why (AI-295). It also upserted
    ``app.overlap_groups`` / ``_members`` / ``reconciliation_rules`` from
    ``metric_source_priority.csv``, plus a supplementary cost-verification group.
    Those rows were the SECOND reconciliation model: the runtime stopped reading
    them in Story 49.4, the two read surfaces stopped in the commit before this
    one, and a store that nothing reads but a bootstrap route still fills is a
    trap -- the next reader finds rows and believes them.

    The seed is untouched and still read. ``metric_source_priority.csv`` feeds
    :func:`core.controls_quality._seed_priorities`, which turns it into the
    editable Project draft a governed publication starts from. The seed was never
    the problem; the second LIVE store was.
    """
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

    # AI-295: the groups / members / rules half of this import is GONE. It wrote the
    # second reconciliation model on every org bootstrap, through a route no screen
    # calls, into tables the runtime stopped reading in Story 49.4.

    logger.info(
        "metric_semantics: imported platform defaults defs=%d groups=%d members=%d rules=%d",
        counts["definitions"],
        counts["groups"],
        counts["members"],
        counts["rules"],
    )
    return counts
