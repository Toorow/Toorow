"""toorow -- Metric routing resolver + conditional overlap gate (Story 27.3, Epic 27).

The DECISION brick of the metric-semantics epic. Story 27.1 laid the passive socle (4
tables + append-only audit, the PROJECT > ORG > PLATFORM cascade, the seed import). That
socle CARRIES the rule (method, priority_order, target_mart) but ROUTES nothing.

This module adds ``resolve_route(project_id, metric)`` -> a typed ``RouteDecision``: from
the rule resolved by the 27.1 cascade it yields the method, the target MART (for
PRIORITY/DEDUP_ID/ESTIMATE at PLATFORM scope), the ordered LIST OF PER-SOURCE SERIES (for
KEEP_SEPARATE -- never a combined total), or a structured "combination unavailable" reason
when several sources emit the same sensitive metric WITHOUT a resolved rule (the
conditional gate of invariant 5).

F-1 (AD-9 guard rail): the pre-computed marts (cross_source_*, transaction_reconciliation,
dedup_estimate) LEFT JOIN the PLATFORM seed metric_source_priority -- they are FROZEN on
the PLATFORM priority. An ORG/PROJECT override carries a DIFFERENT priority the mart
ignores, so routing such an override would silently emit a mart figure computed with the
WRONG priority. When a mart-routing rule's scope is not PLATFORM, ``resolve_route`` refuses
to route and returns ``OVERRIDE_NOT_MATERIALIZED`` (per-source series + reason, no
``target_mart``) -- never a wrong number. Org-level mart materialization is Phase B.

STRICTLY PASSIVE (27.3): no existing consumer (rollup.py, cards.py, the dbt marts) is
touched. Like 27.1, NO warehouse row is read or written, and the marts are ROUTED, never
rewritten (invariant 1). ``target_mart`` is a NAME (string) a future consumer (27.6) will
use to read the pre-computed mart; this module validates the name against a KNOWN_MARTS
allow-list but never opens a warehouse cursor.

AD-2 (ZERO provider vocabulary): this module contains NO connector names. Emitters come
from the loaded modules' manifests (core.main._loaded_modules, read exactly like
core.cards_api._canonical_vocabulary) and from the 27.1 DB rows. The MART NAMES
(cross_source_*, transaction_reconciliation, dedup_estimate) are dbt artefacts, not
provider names (same status as the seed file names in metric_semantics.py).

Windows/CI note: all log/message strings use ASCII-safe characters only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Route statuses (str constants, mutually exclusive -- no Enum imposed, matching
# metric_semantics.py's method/scope string constants).
# ---------------------------------------------------------------------------

# The rule routes to a pre-computed mart (PRIORITY/DEDUP_ID/ESTIMATE with target_mart).
ROUTED_TO_MART = "ROUTED_TO_MART"
# The rule is KEEP_SEPARATE: N independent series, one per source, never a total.
KEEP_SEPARATE = "KEEP_SEPARATE"
# No group and the metric is additive (layer 1): a trivial direct SUM of the fact.
DIRECT_SUM = "DIRECT_SUM"
# >=2 sources emit the metric WITHOUT a resolved rule: combination unavailable (gate).
UNRULED_OVERLAP = "UNRULED_OVERLAP"
# No group, non-additive, >=1 emitter: not combinable without a rule (ratio/position).
NOT_COMBINABLE = "NOT_COMBINABLE"
# An ORG/PROJECT override would route to a mart, but the pre-computed marts are FROZEN
# PLATFORM (they LEFT JOIN the seed metric_source_priority, not the 27.1 override tables).
# Routing such an override would apply the WRONG priority silently -> we refuse to route
# and expose the honest per-source view instead (AD-9: never a wrong number). Org-level
# materialization is Phase B.
OVERRIDE_NOT_MATERIALIZED = "OVERRIDE_NOT_MATERIALIZED"


class RouteStatus:
    """Namespace of the six mutually-exclusive route statuses (see the table in A.2).

    ``OVERRIDE_NOT_MATERIALIZED`` is the AD-9 guard rail (F-1): an override rule whose
    scope is not PLATFORM cannot be honoured by the pre-computed marts (they are frozen on
    the PLATFORM seed priority), so we do NOT route it -- we return the per-source series
    (like KEEP_SEPARATE) plus a structured reason, never a mart figure computed with the
    wrong priority."""

    ROUTED_TO_MART = ROUTED_TO_MART
    KEEP_SEPARATE = KEEP_SEPARATE
    DIRECT_SUM = DIRECT_SUM
    UNRULED_OVERLAP = UNRULED_OVERLAP
    NOT_COMBINABLE = NOT_COMBINABLE
    OVERRIDE_NOT_MATERIALIZED = OVERRIDE_NOT_MATERIALIZED


# Reconciliation reason codes (machine-readable, carried by ReconciliationReason).
CODE_UNRULED_OVERLAP = "UNRULED_OVERLAP"
CODE_NON_ADDITIVE_NO_RULE = "NON_ADDITIVE_NO_RULE"
CODE_NO_GROUP_ADDITIVE_SUM = "NO_GROUP_ADDITIVE_SUM"
CODE_OVERRIDE_NOT_MATERIALIZED = "OVERRIDE_NOT_MATERIALIZED"
# D-5 (honesty): KEEP_SEPARATE carries this code so the consumer never sums the series.
CODE_KEEP_SEPARATE = "KEEP_SEPARATE"
# D-2 (honesty): ESTIMATE routes to dedup_estimate, but this module cannot read dim_project
# to verify the project actually designated a source of truth (warehouse read forbidden,
# invariant 1). The routed decision carries this note so the consumer (27.6) knows the
# precondition it -- not us -- must check before trusting the mart figure.
CODE_ESTIMATE_REQUIRES_DESIGNATED_TRUTH = "estimate_requires_designated_truth"
_MSG_ESTIMATE_REQUIRES_DESIGNATED_TRUTH = (
    "valide uniquement si le projet a designe une source de verite (dim_project) ; "
    "sinon le mart est vide"
)


# ---------------------------------------------------------------------------
# Declarative method -> mart routing table (documented + tested, no scattered
# literals). The mart NAMES are warehouse vocabulary (dbt files), NOT provider names
# -- AD-2 tolerates dbt artefacts, cf. metric_semantics.DIM_METRIC_SEED.
# ---------------------------------------------------------------------------

MART_CROSS_SOURCE_PREFIX = "cross_source_"  # PRIORITY -> cross_source_<metric>
MART_TRANSACTION_RECONCILIATION = "transaction_reconciliation"  # DEDUP_ID
MART_DEDUP_ESTIMATE = "dedup_estimate"  # ESTIMATE

# D-1 (honesty): the transaction_reconciliation mart is a two-source join keyed on the
# transaction id -- its CONTRACT is join_key == 'transaction_id'. A DEDUP_ID rule carrying
# a DIFFERENT join_key (e.g. 'order_id') targets data this mart does NOT materialize, so we
# refuse to derive it and let the honest no-route path take over (never a wrong mart).
DEDUP_ID_MART_JOIN_KEY = "transaction_id"

KNOWN_MARTS = frozenset(
    {
        "cross_source_conversions",
        "cross_source_revenue",
        MART_TRANSACTION_RECONCILIATION,
        MART_DEDUP_ESTIMATE,
    }
)

# Reconciliation methods (re-exported from the socle so callers use ONE source of truth).
METHOD_SUM = "SUM"
METHOD_PRIORITY = "PRIORITY"
METHOD_DEDUP_ID = "DEDUP_ID"
METHOD_ESTIMATE = "ESTIMATE"
METHOD_KEEP_SEPARATE = "KEEP_SEPARATE"

# Methods that route to a pre-computed mart (the rest carry per-source series / SUM).
_MART_METHODS = frozenset({METHOD_PRIORITY, METHOD_DEDUP_ID, METHOD_ESTIMATE})

# The ONLY scope the pre-computed marts materialize (F-1). The cross_source_* /
# transaction_reconciliation / dedup_estimate marts LEFT JOIN the seed
# metric_source_priority -- a PLATFORM-frozen priority. An ORG/PROJECT override carries a
# DIFFERENT priority the mart does not know about, so we route to the mart ONLY when the
# resolved rule is itself PLATFORM. (Same string values as metric_semantics.SCOPE_*.)
SCOPE_PLATFORM = "PLATFORM"


def _derive_target_mart(method: str, metric: str, rule: dict | None = None) -> str | None:
    """Derive the pre-computed mart a method implies for *metric*, or None.

    Declarative routing (A.5): the DATA says where to go, the code fabricates no mart.
      * PRIORITY -> cross_source_<metric> IF that name is a KNOWN_MART, else None.
      * DEDUP_ID -> transaction_reconciliation, but ONLY when the rule's ``join_key`` is
        ``transaction_id`` (the mart's actual contract, D-1). A DEDUP_ID rule with any
        other join_key (e.g. 'order_id') targets data the mart does not materialize, so we
        return None -> the honest no-route path (never a wrong number on a wrong join).
      * ESTIMATE -> dedup_estimate.
    A PRIORITY on a metric with no known cross_source_<metric> yields None -> the caller
    falls through to the A.2 guard rail (never a route to an inexistent mart).
    """
    if method == METHOD_PRIORITY:
        candidate = f"{MART_CROSS_SOURCE_PREFIX}{metric}"
        return candidate if candidate in KNOWN_MARTS else None
    if method == METHOD_DEDUP_ID:
        # D-1: read the join_key -- only 'transaction_id' matches the existing mart.
        join_key = (rule or {}).get("join_key")
        return MART_TRANSACTION_RECONCILIATION if join_key == DEDUP_ID_MART_JOIN_KEY else None
    if method == METHOD_ESTIMATE:
        return MART_DEDUP_ESTIMATE
    return None


# ---------------------------------------------------------------------------
# Typed decision objects (frozen dataclasses -- immutable, comparable, hashable).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSeries:
    """One independent per-source series (the contract the gate exposes).

    ``KEEP_SEPARATE`` and ``UNRULED_OVERLAP`` yield a tuple of these: "here are the N
    series, you MUST NOT add them together". Deterministic ordering is enforced by the
    resolver (alphabetical by connector, like the 27.1 tie-break)."""

    connector: str
    canonical_name: str


@dataclass(frozen=True)
class ReconciliationReason:
    """A structured reason a metric is NOT combinable (the honest NULL, AD-9).

    ``code`` is machine-readable (UNRULED_OVERLAP / NON_ADDITIVE_NO_RULE /
    NO_GROUP_ADDITIVE_SUM), ``message`` is a human ASCII string, ``emitters`` are the
    connectors emitting the sensitive metric without a rule. NEVER a total: it is the
    invitation to configure, not a number."""

    code: str
    message: str
    emitters: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteDecision:
    """The typed routing decision (one object describes every outcome).

    A single status class carrying method + target_mart + series + reason (rather than an
    Enum of methods and separate unions): the consumer (27.6) is trivial
    (``if d.status == RouteStatus.ROUTED_TO_MART: read d.target_mart``) and the test is
    exhaustive (one decision = one assertion). Frozen so two equal decisions compare equal
    and the object is hashable (tuples, not lists, for series/priority_order)."""

    metric: str
    status: str
    method: str | None = None
    scope_level: str | None = None
    overlap_group_id: str | None = None
    target_mart: str | None = None
    series: tuple[SourceSeries, ...] = ()
    priority_order: tuple[str, ...] = ()
    reason: ReconciliationReason | None = None


# ---------------------------------------------------------------------------
# Emitter enumeration (the manifest read, mirrors cards_api._canonical_vocabulary).
# ---------------------------------------------------------------------------


def _loaded_modules():
    """Best-effort access to the loaded-module registry (fail-soft in unit contexts).

    Mirrors core.cards_api._loaded_modules: the registry is owned by core.main (Story
    27.2). We read it lazily and never modify it. An unavailable registry (offline unit
    context, server not started) yields an empty list -> the gate is silent, never a
    crash."""
    try:
        from core.main import get_loaded_modules  # noqa: PLC0415

        return get_loaded_modules()
    except Exception:  # pragma: no cover - registry unavailable in some unit contexts
        return []


def _emitters_from_modules(modules) -> dict[str, set[str]]:
    """Build {canonical_metric -> {connectors}} from loaded modules' manifests.

    Read EXACTLY like cards_api._canonical_vocabulary: for each module iterate
    manifest['canonical_metric_mapping'].values(), extract the canonical target (a string,
    or the 'canonical' key when the target is a dict, e.g. average_position). The module's
    own name (LoadedModule.name) is the emitting connector. AD-2: no module name hard-coded
    -- the names come from the registry / manifests.
    """
    index: dict[str, set[str]] = {}
    for mod in modules:
        connector = getattr(mod, "name", None)
        if not connector:
            continue
        manifest = getattr(mod, "manifest", None) or {}
        for target_raw in (manifest.get("canonical_metric_mapping") or {}).values():
            canon = target_raw.get("canonical") if isinstance(target_raw, dict) else target_raw
            if canon:
                index.setdefault(canon, set()).add(connector)
    return index


def _emitters_from_db_members(members_reader=None) -> dict[str, set[str]]:
    """Build {canonical_metric -> {connectors}} from an INJECTED membership reader.

    Story A.4 read ``app.overlap_group_members`` here, so a member declared in the
    reconciliation topology but WITHOUT a loaded module still counted as an
    emitter. AI-295 retired that topology: nothing writes those tables any more,
    so the default source is gone and ``members_reader is None`` yields ``{}``.

    WHY REMOVING IT DOES NOT WIDEN THE GATE, measured rather than argued. The
    membership came from ``metric_source_priority.csv`` (9 pairs) plus the
    cost-verification pilot (2 pairs). Of those 11, NINE are already declared by
    the emitting connector's own manifest, so the union added nothing. The two
    that were not are both the ``cost`` metric claimed for the two verification
    modules of that pilot -- and neither manifest declares ``cost``, because
    neither of them emits one: the topology was making an emitter claim on its
    members' behalf. ``cost`` is declared by 14 manifests, so it stays a detected
    overlap without them. No metric loses an emitter that a connector declares.

    The seam stays injectable: a caller that CAN name declared emitters from
    somewhere else passes them in. What is gone is the default that read a store
    nobody fills -- a query that returns [] forever reads as "no overlap declared"
    and is indistinguishable from a correct empty answer.
    """
    if members_reader is None:
        return {}
    try:
        pairs = members_reader()
    except Exception as exc:  # noqa: BLE001 -- fail-soft: degrade to manifests only.
        logger.warning("metric_reconciliation: injected member reader failed: %s", exc)
        return {}
    index: dict[str, set[str]] = {}
    for canonical_name, connector in pairs:
        if canonical_name and connector:
            index.setdefault(canonical_name, set()).add(connector)
    return index


def _merge_emitter_indexes(*indexes) -> dict[str, set[str]]:
    """Union several {metric -> {connectors}} indexes into one (in a fresh dict)."""
    merged: dict[str, set[str]] = {}
    for index in indexes:
        for metric, connectors in index.items():
            merged.setdefault(metric, set()).update(connectors)
    return merged


def _full_emitter_index(modules, *, members_reader=None) -> dict[str, set[str]]:
    """The complete emitter index: manifests UNION the 27.1 DB membership (A.4).

    Manifests (loaded modules) merged with the declared ``overlap_group_members`` so a
    member declared in the topology WITHOUT a loaded module still counts as an emitter.
    Fail-soft: DB down -> manifests only (never an exception)."""
    return _merge_emitter_indexes(
        _emitters_from_modules(modules),
        _emitters_from_db_members(members_reader),
    )


def emitters_of(metric: str, *, modules=None, members_reader=None) -> tuple[str, ...]:
    """Connectors whose canonical_metric_mapping (or 27.1 DB membership) produces *metric*.

    Source of truth: the manifests of the loaded modules (core.main._loaded_modules),
    read exactly like core.cards_api._canonical_vocabulary, UNIONed with the declared
    ``overlap_group_members`` (A.4) so a member without a loaded module still counts.
    Returns a SORTED, DEDUPLICATED tuple. ``modules`` and ``members_reader`` are injectable
    (a fake list of .name/.manifest objects, a fake returning (canonical_name, connector)
    pairs) so the gate is testable offline without a DB or a running server. AD-2: no module
    name hard-coded."""
    mods = _loaded_modules() if modules is None else modules
    index = _full_emitter_index(mods, members_reader=members_reader)
    return tuple(sorted(index.get(metric, set())))


# ---------------------------------------------------------------------------
# The routing resolver -- resolve_route (PASSIVE: no write, no warehouse read).
# ---------------------------------------------------------------------------


def _series_from_connectors(connectors, metric: str) -> tuple[SourceSeries, ...]:
    """Build the ordered tuple of per-source series (sorted, deduplicated)."""
    return tuple(
        SourceSeries(connector=connector, canonical_name=metric)
        for connector in sorted(set(connectors))
    )


def _keep_separate_decision(metric: str, rule: dict, members) -> RouteDecision:
    """Build a KEEP_SEPARATE decision: one series per member, sorted, NO total.

    D-5: carries an explicit FR reason so the consumer (card/LLM) never mistakes the N
    series for something summable -- the KEEP_SEPARATE contract is 'series per source,
    NEVER add them'."""
    return RouteDecision(
        metric=metric,
        status=RouteStatus.KEEP_SEPARATE,
        method=METHOD_KEEP_SEPARATE,
        scope_level=rule.get("scope_level"),
        overlap_group_id=rule.get("overlap_group_id"),
        target_mart=None,  # KEEP_SEPARATE routes to no mart (invariant 2).
        series=_series_from_connectors(members, metric),
        reason=ReconciliationReason(
            code=CODE_KEEP_SEPARATE,
            message=(
                f"series per source for '{metric}'; NEVER add them together "
                "(regle KEEP_SEPARATE)"
            ),
            emitters=tuple(_series_connectors(members)),
        ),
    )


def _routed_decision(metric: str, rule: dict, method: str, target_mart: str) -> RouteDecision:
    """Build a ROUTED_TO_MART decision (priority_order copied for PRIORITY).

    D-2: an ESTIMATE route carries a structured note in ``reason`` because dedup_estimate is
    only populated when the project designated a source of truth in dim_project -- a
    precondition this module cannot verify (no warehouse read, invariant 1). The note is the
    honest hand-off: the consumer knows exactly what to check before trusting the figure.

    D-6 (assiette caveat): for PRIORITY the cross_source_* mart picks a WINNER PER DAY, so
    the resolved series can change its assiette (which source feeds it) at each daily
    switch-over -- see the mart header. ``priority_order`` here is the rule's DECLARATIVE
    order, NOT a guarantee of a homogeneous assiette across the window."""
    priority_order = rule.get("priority_order") or []
    reason = None
    if method == METHOD_ESTIMATE:
        reason = ReconciliationReason(
            code=CODE_ESTIMATE_REQUIRES_DESIGNATED_TRUTH,
            message=_MSG_ESTIMATE_REQUIRES_DESIGNATED_TRUTH,
        )
    return RouteDecision(
        metric=metric,
        status=RouteStatus.ROUTED_TO_MART,
        method=method,
        scope_level=rule.get("scope_level"),
        overlap_group_id=rule.get("overlap_group_id"),
        target_mart=target_mart,
        priority_order=tuple(priority_order),
        reason=reason,
    )


def _override_not_materialized_decision(
    metric: str, rule: dict, method: str, target_mart: str, members
) -> RouteDecision:
    """Build an OVERRIDE_NOT_MATERIALIZED decision (F-1, the AD-9 guard rail).

    The rule WOULD route to *target_mart*, but its scope is not PLATFORM: the pre-computed
    mart applies the PLATFORM seed priority, NOT this override's priority. Routing would
    emit a mart figure with the WRONG priority silently. Instead we refuse to route and
    hand back the honest per-source series (like KEEP_SEPARATE, so the consumer still has a
    truthful by-source view) plus a structured reason. NO ``target_mart`` -- never a wrong
    number. Org-level materialization is Phase B."""
    return RouteDecision(
        metric=metric,
        status=RouteStatus.OVERRIDE_NOT_MATERIALIZED,
        method=method,
        scope_level=rule.get("scope_level"),
        overlap_group_id=rule.get("overlap_group_id"),
        target_mart=None,  # never route an override through a PLATFORM-frozen mart.
        series=_series_from_connectors(members, metric),
        priority_order=tuple(rule.get("priority_order") or []),
        reason=ReconciliationReason(
            code=CODE_OVERRIDE_NOT_MATERIALIZED,
            message=(
                f"'{metric}' has a {rule.get('scope_level')}-scope {method} override that "
                "the pre-computed mart does not materialize (the mart is frozen on the "
                "PLATFORM priority); per-source series returned instead. Org-level "
                "materialization is Phase B"
            ),
            emitters=tuple(_series_connectors(members)),
        ),
    )


def _series_connectors(members) -> tuple[str, ...]:
    """Sorted, deduplicated connector names from a members iterable (for the reason)."""
    return tuple(sorted(set(members)))


def _no_rule_decision(
    metric: str, definition: dict | None, emitters: tuple[str, ...]
) -> RouteDecision:
    """Decide the outcome when NO rule covers the metric (steps 4/5 of A.3).

    >=2 emitters -> UNRULED_OVERLAP (the conditional gate, invariant 5). Otherwise an
    additive metric (or an unknown one, fail-soft as additive) -> DIRECT_SUM (nothing to
    signal: one source or disjoint groups). A non-additive metric with >=1 emitter ->
    NOT_COMBINABLE (ratio / average_position without a rule).

    Story 53.2 -- ``emitters`` is now the DECLARED emitters UNIONed with the ones the
    caller OBSERVED contributing rows (see ``resolve_route(observed_emitters=...)``).
    Before that union this function could answer DIRECT_SUM by vacuity: the manifest
    registry is fail-soft and yields ``()`` whenever it is unreachable, ``len(()) >= 2``
    is false, and the gate said "go ahead and sum" to a caller that had just counted two
    connectors in the rows. A permission derived from an unreadable registry is not a
    permission."""
    if len(emitters) >= 2:
        return RouteDecision(
            metric=metric,
            status=RouteStatus.UNRULED_OVERLAP,
            series=_series_from_connectors(emitters, metric),
            reason=ReconciliationReason(
                code=CODE_UNRULED_OVERLAP,
                message=(
                    f"combination unavailable: {len(emitters)} sources emit '{metric}' "
                    "without a resolved reconciliation rule; configure a rule to combine"
                ),
                emitters=tuple(emitters),
            ),
        )
    # Fail-soft: an unknown metric (absent from layer 1) is treated as additive.
    additive = True if definition is None else bool(definition.get("additive", True))
    if additive:
        return RouteDecision(
            metric=metric,
            status=RouteStatus.DIRECT_SUM,
            method=METHOD_SUM,
            target_mart=None,
        )
    return RouteDecision(
        metric=metric,
        status=RouteStatus.NOT_COMBINABLE,
        reason=ReconciliationReason(
            code=CODE_NON_ADDITIVE_NO_RULE,
            message=(
                f"'{metric}' is non-additive and has no reconciliation rule; "
                "it cannot be summed across sources"
            ),
            emitters=tuple(emitters),
        ),
    )


def resolve_route(
    project_id: str,
    metric: str,
    *,
    rule_resolver=None,
    definition_resolver=None,
    emitters_source=None,
    members_source=None,
    observed_emitters: tuple[str, ...] | list[str] = (),
) -> RouteDecision:
    """Route *metric* for *project_id* into a typed RouteDecision (PASSIVE, no I/O writes).

    Algorithm (A.3), fail-soft end-to-end like 27.1:
      1. rule = resolve_reconciliation(project_id, metric)  (the 27.1 cascade)
      2. IF rule: route by method (mart / KEEP_SEPARATE / SUM), with the A.2 guard rail
         (a non-routable rule falls through, never a route to an inexistent mart). F-1
         (AD-9): a mart-routing rule whose scope is NOT PLATFORM yields
         OVERRIDE_NOT_MATERIALIZED (the mart is frozen on the PLATFORM priority, so an
         override cannot be honoured by it) -- per-source series + reason, never a wrong
         number.
      3. ELSE (definition = the DECLARED additivity, Semantic Model first then
         `app.metric_definitions` -- see `_resolve_definition`, retargeted 49.3):
         emitters = emitters_of(metric); >=2 -> UNRULED_OVERLAP (gate);
         additive -> DIRECT_SUM; non-additive -> NOT_COMBINABLE.

    ``observed_emitters`` (story 53.2) -- the connectors the CALLER actually saw
    contributing rows for this metric, UNIONed into the declared emitters before step 3
    decides. It exists because the declared enumeration is fail-soft on both of its halves
    (``_loaded_modules`` and ``_read_db_overlap_members`` swallow every exception and
    return empty), so an unreachable registry made the gate answer ``DIRECT_SUM`` -- a
    permission -- to a caller that had just counted two connectors in its rows. Row
    evidence is the strongest emitter evidence there is: a connector that PRODUCED the
    metric emits it, whatever a manifest says. Empty (the default) keeps the previous
    behaviour byte for byte for callers that observe nothing.

    The layer-1 definition is resolved LAZILY (only on the no-rule path, F-5) so a routed
    or override decision never triggers a definition lookup. Every collaborator is
    INJECTABLE (rule_resolver / definition_resolver / emitters_source / members_source) so
    the resolver is fully testable offline with fakes, without a DB or a running server.
    Determinism: two calls yield an EQUAL decision (frozen dataclass)."""
    rule = _resolve_rule(project_id, metric, rule_resolver)

    if rule is not None:
        method = rule.get("method")
        if method == METHOD_KEEP_SEPARATE:
            members = _resolve_members(rule, members_source, metric)
            return _keep_separate_decision(metric, rule, members)
        if method == METHOD_SUM:
            return RouteDecision(
                metric=metric,
                status=RouteStatus.DIRECT_SUM,
                method=METHOD_SUM,
                scope_level=rule.get("scope_level"),
                overlap_group_id=rule.get("overlap_group_id"),
                target_mart=None,
            )
        if method in _MART_METHODS:
            # Prefer the rule's explicit target_mart; else derive declaratively (A.5).
            # D-1: for DEDUP_ID the join_key gate is authoritative -- even an explicit
            # target_mart is only honoured when it agrees with the join-key derivation,
            # so a DEDUP_ID on a non-'transaction_id' key never routes to the mart.
            derived = _derive_target_mart(method, metric, rule)
            if method == METHOD_DEDUP_ID:
                target_mart = derived
            else:
                target_mart = rule.get("target_mart") or derived
            if target_mart and target_mart in KNOWN_MARTS:
                # F-1 (AD-9): the pre-computed marts are FROZEN on the PLATFORM seed
                # priority. An ORG/PROJECT override carries a different priority the mart
                # ignores -- routing it would emit the WRONG number silently. Route to the
                # mart ONLY when the rule is itself PLATFORM; otherwise refuse and expose
                # the honest per-source series (OVERRIDE_NOT_MATERIALIZED).
                if rule.get("scope_level") == SCOPE_PLATFORM:
                    return _routed_decision(metric, rule, method, target_mart)
                members = _resolve_members(rule, members_source, metric)
                return _override_not_materialized_decision(
                    metric, rule, method, target_mart, members
                )
            # A.2 guard rail: a rule that cannot route to a KNOWN mart falls through --
            # never a silent route to an inexistent mart (invariant 2).

    definition = _resolve_definition(project_id, metric, definition_resolver)
    emitters = _resolve_emitters(metric, emitters_source)
    if observed_emitters:
        emitters = tuple(sorted(set(emitters) | {str(c) for c in observed_emitters if c}))
    return _no_rule_decision(metric, definition, emitters)


def route_status_resolver(project_id: str):
    """THE factory that binds the gate to a project -- or None when it cannot bind.

    One factory, every call site. It used to live in ``cards.py`` under
    ``_route_resolver_for``, which meant the four other producers of a cross-source
    total (``get_daily_report``, the two notebook renders, the scheduler briefing) and
    the report renderer had no way to ask short of importing the card module. They did
    not ask, so five of the six paths that publish a combined number published it
    unchecked while the register recorded the gate as wired.

    The returned callable takes ``(metric, observed_sources)`` -- the connectors the
    caller counted in its own rows -- and yields the route STATUS string
    ``rollup._combination_refusal`` compares against ``_COMBINATION_REFUSED``.

    ``None`` when there is no project: a stub answering "allowed" would read as
    "checked", which is the lie this whole story exists to remove. ``compute_rollup``
    records that difference as ``combination_check == "not_requested"``.
    """
    if not project_id:
        return None

    def _resolve(metric: str, observed_sources: tuple[str, ...] = ()):
        return resolve_route(
            project_id, metric, observed_emitters=tuple(observed_sources)
        ).status

    return _resolve


# ---------------------------------------------------------------------------
# Injectable collaborators (real by default, fakeable in tests).
# ---------------------------------------------------------------------------


def _resolve_rule(project_id: str, metric: str, rule_resolver):
    """The effective reconciliation rule, from the GOVERNED Rule Set (Story 49.4).

    This read ``metric_semantics.resolve_reconciliation``, a PROJECT > ORG >
    PLATFORM cascade over mutable ``overlap_groups`` rows whose members were
    CONNECTOR LABELS. Two defects, and neither was visible from here:

    * the cascade meant "no rule in this Project" quietly became "a rule at some
      other scope". It answered for a project that does not exist at all --
      ``resolve_route("no_such_project", "conversions")`` returned
      ``ROUTED_TO_MART cross_source_conversions``, which is a routing decision for
      nobody;
    * a connector label names a provider, not a thing a Project publishes, so a
      rule could not distinguish two Datastreams from the same provider and
      silently followed the second one when it appeared.

    A published Rule Set version pins exact Concept and Datastream versions, and a
    Project reaches one through
    :func:`core.controls_quality.materialize_reconciliation_template` -- the
    template made editable, which is what AC3 requires instead of the cascade.

    ``None`` means what it always meant: no rule governs this metric, so the
    sources are kept separate and never summed. The pure decision logic below is
    unchanged; only where the rule comes from has moved.
    """
    if rule_resolver is not None:
        return rule_resolver(project_id, metric)
    from core.controls_quality import governed_runtime_rule  # noqa: PLC0415

    return governed_runtime_rule(project_id, metric)


def _resolve_definition(project_id: str, metric: str, definition_resolver):
    """The layer-1 declaration this gate reads -- SEMANTIC MODEL FIRST since 49.3.

    THE DEFECT THIS CLOSES, and it is the one story 60.2 already found once in
    ``rollup.py``. This function read ``resolve_metric_definitions`` alone: the
    PROJECT > ORG > PLATFORM cascade over ``app.metric_definitions``. So a metric
    a person had declared ``non_additive`` in the Concept workbench -- the store
    ``governance.md`` gives the last word to -- was invisible here, and step 5 of
    ``resolve_route`` answered ``DIRECT_SUM`` on it: a PERMISSION to add two
    sources together, derived from a store that did not carry the declaration.
    ``governance.md`` names it in its *Incomplete if*: "a render reads
    ``app.metric_definitions`` without going through the reader that puts the
    Semantic Model first".

    The order is the one that document settles, and the reader is the SAME one
    every render already goes through
    (:func:`core.metric_semantics.resolve_declared_additivity`), so what this gate
    applies and what a roll-up applies cannot disagree:

      1. a PUBLISHED Concept version's ``additivity_class``, mapped through
         :func:`core.metric_semantics.declared_non_additive` -- ``semi_additive``
         counts as non-additive here for its reason, "summable across SOME
         dimensions" is not an answer a cross-source sum can use;
      2. otherwise ``app.metric_definitions`` through its cascade -- the SECOND
         layer of that same reader, so the lower store still answers for a metric
         no published Concept carries. It is not consulted here any more; it is
         consulted THERE, once, for every render in the product.

    A metric NEITHER store declares is absent from the result, this returns
    ``None``, and ``_no_rule_decision`` keeps its fail-soft ``additive=True``:
    the move never widens a permission, it only lets a declaration take one away.
    """
    if definition_resolver is not None:
        return definition_resolver(project_id, metric)
    from core import metric_semantics  # noqa: PLC0415

    try:
        declared = metric_semantics.resolve_declared_additivity(project_id)
    except Exception as exc:  # noqa: BLE001 -- fail-soft, same posture as before
        logger.warning(
            "metric_reconciliation: declared additivity unreadable project=%s "
            "metric=%s: %s",
            project_id,
            metric,
            exc,
        )
        return None
    if metric not in declared:
        return None
    # The class is the whole answer this gate needs, and `declared_non_additive`
    # is the mapping that owns it -- `semi_additive` is on the non-summable side
    # because "summable across SOME dimensions" is not an answer a cross-source
    # sum can use. `additivity_class` travels so a caller can name what decided.
    return {
        "additive": metric not in metric_semantics.declared_non_additive(declared),
        "additivity_class": declared[metric],
    }


def _resolve_emitters(metric: str, emitters_source) -> tuple[str, ...]:
    if emitters_source is not None:
        return tuple(sorted(set(emitters_source(metric))))
    return emitters_of(metric)


def _resolve_members(rule: dict, members_source, metric: str) -> list[str]:
    """The members of the rule, from the store that OWNS them and no other.

    A GOVERNED rule carries its own membership and must not touch
    ``app.overlap_group_members`` at all -- that table is the pre-governance
    store, and `governance.md`'s `Incomplete if` forbids a reconciliation rule
    reaching into a parallel one.

    Measured 2026-08-16: it was reaching into it on every single resolution, and
    the only reason nothing was wrong is that the read could never succeed.
    `controls_quality.py:422` sets ``overlap_group_id`` to the
    ``rule_set_version_id`` -- deliberately, "it now names the governed version
    rather than a mutable group row" -- but this function was never told, and
    kept passing it to `_group_members` as if it were a group id. The two
    namespaces cannot collide: a governed version is minted `grsv_`
    (`governance_rule_sets.py:524`) and a group `ovg_`
    (`metric_semantics.py:266`), so the query matched nothing, every time, and
    fell through to the `priority_order` the governed rule already carried.

    A read that works only because it always fails is not a working read. The
    governed branch below skips it, and the answer is unchanged -- which is the
    point: this removes a query, not a behaviour.
    """
    if members_source is not None:
        return list(members_source(rule.get("overlap_group_id")))
    # The published version IS the membership. The legacy branch that read
    # `app.overlap_group_members` is gone with the store (AI-295): its own
    # docstring had already measured that it could never match -- a governed
    # version is minted `grsv_` and a group `ovg_` -- so it fell through to the
    # `priority_order` below on every single call. Same answer, one less query.
    return list(rule.get("priority_order") or [])


# ---------------------------------------------------------------------------
# The conditional overlap gate (invariant 5) -- declarative, never blocking.
# ---------------------------------------------------------------------------


def detect_unruled_overlaps(
    project_id: str,
    *,
    rule_resolver=None,
    emitters_source=None,
    modules=None,
    members_reader=None,
) -> list[ReconciliationReason]:
    """Detect the overlaps WITHOUT a resolved rule (the conditional gate, invariant 5).

    For each canonical metric emitted by >=2 connectors (manifests UNION the 27.1 declared
    ``overlap_group_members``, A.4): if resolve_reconciliation(project_id, metric) is None
    (no rule at ANY level), produce a ReconciliationReason(code=UNRULED_OVERLAP,
    emitters=...). A member declared in the topology WITHOUT a loaded module thus still
    counts as an emitter. NO warning when a single source emits the metric, nor when a rule
    exists (even KEEP_SEPARATE -- the overlap is MANAGED), nor for disjoint summable groups
    (the cascade resolves ONE, so resolve_reconciliation is not None).

    PUBLICATION IS NEVER BLOCKED: this returns a LIST (possibly empty), never raises, has
    no side effect. It feeds an invitation to configure, not a block.

    All collaborators are injectable (rule_resolver / emitters_source / a fake `modules`
    list / a fake `members_reader`) so the gate is testable offline without a DB or a
    running server. The DB member union is fail-soft (DB down -> manifests only)."""
    reasons: list[ReconciliationReason] = []

    # Enumerate {metric -> emitters}. Prefer an injected source; else read the manifests
    # UNIONed with the 27.1 DB membership (A.4). Both paths are fail-soft.
    if emitters_source is not None:
        index = _coerce_emitter_index(emitters_source)
        index = _merge_emitter_indexes(index, _emitters_from_db_members(members_reader))
    else:
        mods = _loaded_modules() if modules is None else modules
        index = _full_emitter_index(mods, members_reader=members_reader)

    for metric in sorted(index):
        emitters = tuple(sorted(index[metric]))
        if len(emitters) < 2:
            continue  # one source -> nothing to signal (invariant 5).
        rule = _resolve_rule(project_id, metric, rule_resolver)
        if rule is not None:
            continue  # a rule exists (even KEEP_SEPARATE) -> overlap managed, no warning.
        reasons.append(
            ReconciliationReason(
                code=CODE_UNRULED_OVERLAP,
                message=(
                    f"combination unavailable: {len(emitters)} sources emit '{metric}' "
                    "without a resolved reconciliation rule; configure a rule to combine"
                ),
                emitters=emitters,
            )
        )
    return reasons


def _coerce_emitter_index(emitters_source) -> dict[str, set[str]]:
    """Normalise an injected emitters source into {metric -> {connectors}}.

    Accepts either a ready-made mapping {metric -> iterable of connectors} (the simplest
    fake) or a callable returning module-like objects (a fake registry). A dict short-cut
    keeps the gate tests trivial while staying faithful to the manifest read for the
    registry case."""
    if isinstance(emitters_source, dict):
        return {metric: set(connectors) for metric, connectors in emitters_source.items()}
    # A callable returning module-like objects -> read like the manifests.
    result = emitters_source() if callable(emitters_source) else emitters_source
    return _emitters_from_modules(result)
