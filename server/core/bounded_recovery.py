"""toorow -- Bounded synchronize / reload / reprocess (Story 12.11, Epic 12).

Story 12.11 promotes THREE bounded-scope recovery verbs to first-class, audited
operations that were previously either implicit or missing as NAMED verbs:

  1. ``synchronize`` -- an on-demand incremental sync of NEW data. States its
     interval (the incremental window ending yesterday), source + mapping
     versions, expected impact and confirmation; audited with actor / reason /
     idempotency key / trace / source version / target candidate.
  2. ``reload`` -- a HALF-OPEN ``[from, to)`` range (or declared partition)
     re-pull that supersedes ONLY that scope, idempotently. Adjacent intervals,
     unrelated dimensions and the prior published version OUTSIDE the scope stay
     unchanged. Reuses the Story 26.1 ``refetch`` windowing (AD-7 append-only +
     QUALIFY supersede is the underlying supersede mechanism).
  3. ``reprocess`` -- reapply a CHOSEN mapping + Ossie version to RETAINED
     raw/source data WITHOUT calling the provider. Missing retention or an
     incompatible schema PREVENTS execution with an actionable explanation.

WHAT THIS MODULE REUSES (E36-NFR05 / the gap analysis: the substrate EXISTS):

  * AD-27 prepare-confirm-commit is the write-effect contract. This module opens
    NO parallel recovery engine and adds NO second store. It writes the SAME
    immutable proposal store the Story 36.13 Operations profile writes
    (``app.operation_preparations``, migration 070) via
    ``operations_mcp._insert_preparation`` / ``_load_preparation`` /
    ``_mark_preparation_confirmed``, and routes EXACTLY ONE durable operation
    through ``operations.execute_operation`` (migration 060: idempotency + audit
    + outbox, idempotent replay). Migration 080 only WIDENS the migration-070
    ``kind`` CHECK so {synchronize, reload, reprocess} are representable.

  * ``refetch.compute_refetch_windows`` / ``REFETCH_WINDOW_DAYS`` -- the bounded
    <=31-day half-open windowing for a reload range (the Story 26.1 mechanism).

  * ``datastream_publication.create_execution`` -- mint ONE non-live CANDIDATE
    execution against the pinned (or chosen, for reprocess) plan/mapping
    versions. This module NEVER calls ``commit_publication`` and NEVER writes
    ``current_published_execution_id``: an invalid candidate simply stays
    UNPUBLISHED, the current version + its freshness stay explicit (publication
    gates are Governance's fail-closed authority, Story 36.18).

  * ``operations_mcp._load_target`` / ``_current_policy_version`` /
    ``_quota_estimate`` / ``_proposal_fingerprint`` /
    ``_recovery_idempotency_key`` / ``_bounded_interval`` -- the SAME target
    read, policy pin, quota pre-check, deterministic fingerprint and idempotency
    derivation the Operations recovery verbs use. No duplication.

WHAT IS GENUINELY NEW (Story 12.11's contribution):

  * The three NAMED verbs as a domain entry point (``prepare_bounded_recovery`` /
    ``confirm_bounded_recovery``) with per-verb interval/partition + versions +
    expected-impact statements.
  * The reload SCOPE model: a half-open ``[from, to)`` range normalized to
    inclusive re-pull windows, carried on the proposal so the candidate re-pull
    supersedes ONLY that scope (proven by adjacent-interval isolation).
  * The reprocess RETENTION + SCHEMA precheck: reprocess reads NO provider; it
    reapplies a CHOSEN mapping + Ossie version to retained raw/source data. When
    retention is missing OR the chosen mapping's schema is incompatible with the
    retained data, it refuses with an ACTIONABLE reason -- never a provider call.

Design mirrors operations_mcp / datastream_recovery: ``from __future__ import
annotations``, module logger, ``core.*`` imports LAZY inside function bodies (no
import cycle with core.main), pure serializers separate from I/O so the invariants
are offline-testable, French user-facing messages, ASCII-only stdout (AI-03). It
does NOT register MCP tools or add admin_api routes itself -- the orchestrator
wires those into the hot shared files (see the story's INTEGRATION SPEC).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)


# The three NAMED bounded-scope verbs Story 12.11 owns. These are DISJOINT from
# the Story 36.13 Operations verbs {retry, refetch, reconcile}: 12.11 adds the
# on-demand + range + reprocess vocabulary. Migration 080 widens the
# migration-070 ``kind`` CHECK to admit exactly these alongside the 36.13 set.
KIND_SYNCHRONIZE = "synchronize"
KIND_RELOAD = "reload"
KIND_REPROCESS = "reprocess"

BOUNDED_KINDS = frozenset({KIND_SYNCHRONIZE, KIND_RELOAD, KIND_REPROCESS})

# A single reload window may not exceed the Story 26.1 window ceiling
# (refetch.REFETCH_WINDOW_DAYS == 31). A wider [from, to) is SPLIT into contiguous
# <=31-day windows (each one queueable). The TOTAL span of one bounded reload is
# capped separately: a whole-history reload is a Governance/backfill concern, not a
# bounded recovery. 92 days (~a quarter) tiles into a few windows while still
# refusing an over-wide (e.g. multi-year) request as forbidden_interval.
MAX_RELOAD_SPAN_DAYS = 92

# The default incremental depth (in days) a synchronize covers when the caller
# states no explicit window: the datastream's own configured refetch depth, read
# from app.datastreams.refetch_days. A documented fallback keeps a missing/absurd
# value safe. Never a platform-wide hardcode of the *effective* window -- this is
# only the floor used when the per-datastream value is unreadable.
DEFAULT_SYNCHRONIZE_DAYS = 3


class BoundedRecoveryError(Exception):
    """A bounded-recovery precondition failure carrying a stable ``code``.

    ``code`` is a stable machine token (e.g. ``forbidden_interval``,
    ``retention_unavailable``, ``incompatible_schema``); ``message`` is the
    actionable French explanation surfaced to the operator.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Pure scope / interval helpers (offline-testable, no I/O).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReloadScope:
    """A normalized half-open ``[from, to)`` reload scope + its re-pull windows.

    ``date_from`` (inclusive) and ``date_to_exclusive`` (EXCLUSIVE) declare the
    half-open range the operator chose; ``windows`` are the contiguous,
    non-overlapping INCLUSIVE ``{date_from, date_to}`` re-pull windows (<=31 days
    each, oldest first) that cover exactly ``[date_from, date_to_exclusive)`` and
    NOTHING outside it -- the isolation guarantee (adjacent intervals untouched).
    ``partition`` is an optional opaque declared-partition label carried verbatim
    for audit/impact when the operator reloads a partition rather than a date
    range.
    """

    date_from: str
    date_to_exclusive: str
    windows: tuple[dict, ...]
    partition: str | None = None

    def as_dict(self) -> dict:
        return {
            "from": self.date_from,
            "to_exclusive": self.date_to_exclusive,
            "windows": [dict(w) for w in self.windows],
            "partition": self.partition,
        }


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def normalize_reload_scope(
    date_from,
    date_to_exclusive,
    partition: str | None = None,
) -> ReloadScope:
    """Normalize a HALF-OPEN ``[from, to)`` reload range into re-pull windows.

    Story 12.11 AC2: a reload uses a half-open ``[from, to)`` range (the ``to``
    bound is EXCLUSIVE -- the first date NOT reloaded) so adjacent reloads tile
    without overlap and never double-supersede a boundary day. This function:

      * validates the range is well-formed and non-empty
        (``from`` < ``to_exclusive``);
      * caps the TOTAL span at ``MAX_RELOAD_SPAN_DAYS`` (a whole-history reload is
        refused as ``forbidden_interval`` -- bounded recovery only);
      * splits ``[from, to_exclusive)`` into contiguous INCLUSIVE windows of at
        most ``refetch.REFETCH_WINDOW_DAYS`` days, oldest first, reusing the
        Story 26.1 windowing (each window is one queueable re-pull job).

    A declared ``partition`` (opaque label) may accompany or replace a date
    range; it is carried verbatim for the impact statement and audit. Raises
    ``BoundedRecoveryError('forbidden_interval', ...)`` on a malformed / inverted
    / empty / over-wide range.
    """
    from core import refetch  # noqa: PLC0415

    d_from = _parse_date(date_from)
    d_to_excl = _parse_date(date_to_exclusive)
    if d_from is None or d_to_excl is None:
        raise BoundedRecoveryError(
            "forbidden_interval",
            "Intervalle de rechargement invalide : dates from/to attendues (ISO).",
        )
    if d_from >= d_to_excl:
        raise BoundedRecoveryError(
            "forbidden_interval",
            "Intervalle de rechargement vide : la borne 'to' est exclusive et "
            "doit etre strictement posterieure a 'from'.",
        )

    # The last INCLUSIVE reloaded day is the exclusive bound minus one day. Cap the
    # TOTAL span (not a single window) so an over-wide / whole-history reload is
    # refused as forbidden_interval; the per-window <=31-day tiling happens below.
    last_inclusive = d_to_excl - timedelta(days=1)
    span_days = (last_inclusive - d_from).days + 1
    if span_days > MAX_RELOAD_SPAN_DAYS:
        raise BoundedRecoveryError(
            "forbidden_interval",
            "Intervalle de rechargement hors bornes "
            f"(max {MAX_RELOAD_SPAN_DAYS} jours par rechargement borne).",
        )

    # Split the INCLUSIVE [from, last_inclusive] span into <=31-day windows,
    # oldest first, reusing the Story 26.1 backfill/refetch windowing so each
    # window supersedes exactly its own days and nothing adjacent.
    windows: list[dict] = []
    cursor = d_from
    while cursor <= last_inclusive:
        window_to = min(
            cursor + timedelta(days=refetch.REFETCH_WINDOW_DAYS - 1), last_inclusive
        )
        windows.append(
            {"date_from": cursor.isoformat(), "date_to": window_to.isoformat()}
        )
        cursor = window_to + timedelta(days=1)

    return ReloadScope(
        date_from=d_from.isoformat(),
        date_to_exclusive=d_to_excl.isoformat(),
        windows=tuple(windows),
        partition=(partition or None),
    )


def synchronize_interval(today: date, refetch_days: int | None) -> dict:
    """Return the incremental ``{from, to}`` window for a synchronize-now run.

    Story 12.11 AC1: synchronize states its interval. The incremental window is
    the last ``refetch_days`` days ENDING YESTERDAY (the nightly convention; a
    DATE grain, never an hour -- date-grain invariant). Falls back to
    ``DEFAULT_SYNCHRONIZE_DAYS`` when ``refetch_days`` is missing/absurd so the
    stated interval is always concrete. Inclusive on both ends.
    """
    days = refetch_days if isinstance(refetch_days, int) and refetch_days >= 1 else None
    if days is None:
        days = DEFAULT_SYNCHRONIZE_DAYS
    to = today - timedelta(days=1)
    frm = to - timedelta(days=days - 1)
    return {"from": frm.isoformat(), "to": to.isoformat()}


# ---------------------------------------------------------------------------
# Reprocess retention + schema compatibility (pure decision + I/O read).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionCheck:
    """The result of the reprocess retention + schema-compatibility precheck.

    ``available`` is True only when RETAINED raw/source data exists for the
    datastream (a prior terminal execution whose landing is reusable) AND the
    chosen mapping's ``source_schema_hash`` is COMPATIBLE with that retained
    data's schema. On failure, ``code`` + ``reason`` carry the actionable
    explanation (``retention_unavailable`` or ``incompatible_schema``).
    ``retained_execution_id`` names the execution whose retained landing would be
    reprocessed (evidence, never a provider call).
    """

    available: bool
    code: str | None = None
    reason: str | None = None
    retained_execution_id: str | None = None
    retained_source_schema_hash: str | None = None


def evaluate_reprocess_retention(
    *,
    retained_execution_id: str | None,
    retained_source_schema_hash: str | None,
    chosen_mapping_source_schema_hash: str | None,
) -> RetentionCheck:
    """PURE decision: may a reprocess reapply the chosen mapping to retained data?

    Story 12.11 AC3: reprocess reapplies a chosen mapping + Ossie version to
    RETAINED raw/source data WITHOUT calling the provider; MISSING retention or
    an INCOMPATIBLE schema PREVENTS execution with an actionable explanation. This
    function fails CLOSED:

      * no retained execution -> ``retention_unavailable`` (there is nothing to
        reprocess; a fresh provider pull -- synchronize/reload -- is required
        first, but reprocess itself never pulls);
      * retained data's schema hash is unknown, or the chosen mapping's
        ``source_schema_hash`` differs from the retained data's -> the chosen
        mapping cannot be safely reapplied to that raw shape ->
        ``incompatible_schema`` (re-classify/re-map, or reload the range under a
        compatible mapping);
      * both present and equal -> ``available``.
    """
    if not retained_execution_id:
        return RetentionCheck(
            available=False,
            code="retention_unavailable",
            reason=(
                "Aucune donnee source retenue a retraiter : lancez d'abord une "
                "synchronisation ou un rechargement (le retraitement n'appelle "
                "jamais la source)."
            ),
        )
    if not retained_source_schema_hash or not chosen_mapping_source_schema_hash:
        return RetentionCheck(
            available=False,
            code="incompatible_schema",
            reason=(
                "Schema de la donnee retenue inconnu : impossible de prouver la "
                "compatibilite avec le mapping choisi. Re-classez la source avant "
                "de retraiter."
            ),
            retained_execution_id=retained_execution_id,
            retained_source_schema_hash=retained_source_schema_hash,
        )
    if retained_source_schema_hash != chosen_mapping_source_schema_hash:
        return RetentionCheck(
            available=False,
            code="incompatible_schema",
            reason=(
                "Le schema source du mapping choisi ne correspond pas a celui de "
                "la donnee retenue : le mapping ne peut pas etre reapplique sans "
                "risque. Rechargez l'intervalle sous un mapping compatible."
            ),
            retained_execution_id=retained_execution_id,
            retained_source_schema_hash=retained_source_schema_hash,
        )
    return RetentionCheck(
        available=True,
        retained_execution_id=retained_execution_id,
        retained_source_schema_hash=retained_source_schema_hash,
    )


def _load_retained_source(conn, datastream_id: str) -> dict | None:
    """Return the most recent RETAINED source execution facts, or None (READ ONLY).

    Retained raw/source data is evidenced by the most recent PUBLISHED execution
    that actually LANDED data: a ``published`` execution with a non-NULL
    ``row_count`` AND ``content_hash`` -- the honest AD-9 markers that its raw
    landing exists in the warehouse from the original run (a ``failed`` execution
    may have aborted at ``loading`` before any row landed, so it is NOT a reliable
    reprocess source; ``projection_plan_ref`` is a NOT-NULL default and proves
    nothing -- H1). We read the execution id and its mapping version's
    ``source_schema_hash`` so the schema-compatibility gate can compare it to the
    chosen mapping. Pure read; never mutated; never a provider call.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.mapping_version_id, e.row_count, e.state, mv.source_schema_hash
            FROM app.datastream_executions e
            LEFT JOIN app.datastream_mapping_versions mv
                   ON mv.id = e.mapping_version_id
            WHERE e.datastream_id = %s
              AND e.state = 'published'
              AND e.row_count IS NOT NULL
              AND e.content_hash IS NOT NULL
            ORDER BY e.state_changed_at DESC
            LIMIT 1
            """,
            (datastream_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "execution_id": row[0],
        "mapping_version_id": row[1],
        "row_count": row[2],
        "state": row[3],
        "source_schema_hash": row[4],
    }


def _load_mapping_source_schema_hash(conn, mapping_version_id: str) -> str | None:
    """Return the ``source_schema_hash`` for one chosen mapping version, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_schema_hash
            FROM app.datastream_mapping_versions
            WHERE id = %s
            """,
            (mapping_version_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Impact statement (pure) -- what each verb states before confirmation (AC1).
# ---------------------------------------------------------------------------


def build_impact(
    kind: str,
    *,
    interval: dict | None,
    reload_scope: ReloadScope | None,
    reprocess: RetentionCheck | None,
) -> dict:
    """Build the human-visible expected-impact statement for a bounded verb (PURE).

    Story 12.11 AC1: the operation states interval/partition + expected impact +
    confirmation. Every verb states ``touches_published_pointer: False`` (a
    candidate is created, never a live-pointer change -- an invalid candidate
    stays UNPUBLISHED, AC4) and ``candidate_only: True``. The scope shape differs
    per verb: synchronize/reload carry an interval (reload also its half-open
    windows + optional partition); reprocess carries the retained-execution
    reference and the provider-free flag.
    """
    impact: dict = {
        "kind": kind,
        "touches_published_pointer": False,
        "candidate_only": True,
    }
    if kind == KIND_SYNCHRONIZE:
        impact["interval"] = interval
        impact["scope"] = "incremental_new_data"
    elif kind == KIND_RELOAD:
        impact["interval"] = interval
        impact["scope"] = "bounded_range_supersede"
        impact["half_open_range"] = (
            reload_scope.as_dict() if reload_scope is not None else None
        )
        impact["supersedes_only_scope"] = True
    elif kind == KIND_REPROCESS:
        impact["scope"] = "reapply_mapping_no_provider"
        impact["calls_provider"] = False
        impact["retained_execution_id"] = (
            reprocess.retained_execution_id if reprocess is not None else None
        )
    return impact


# ---------------------------------------------------------------------------
# Proposal assembly (composes the AD-27 substrate -- no new store).
# ---------------------------------------------------------------------------


@dataclass
class BoundedProposal:
    """An assembled, not-yet-persisted bounded-recovery proposal (AD-27 shape).

    Mirrors the dict ``operations_mcp._insert_preparation`` expects so the SAME
    immutable-proposal store (migration 070) is written -- no bespoke table. The
    ``interval`` field carries the verb-specific scope the confirm step re-reads:
    synchronize/reload -> ``{from, to}`` (reload also stashes ``windows`` +
    ``partition`` + ``to_exclusive``); reprocess -> ``{retained_execution_id,
    chosen_mapping_version_id, ...}``.
    """

    org_id: str
    project_id: str | None
    datastream_id: str
    kind: str
    actor: str
    target_versions: dict
    interval: dict | None
    impact: dict
    quota: dict
    lock_ref: str | None
    rollback_ref: str | None
    reason: str | None = None
    extras: dict = field(default_factory=dict)

    def as_insert_payload(self) -> dict:
        """Return the exact dict ``_insert_preparation`` consumes."""
        return {
            "org_id": self.org_id,
            "project_id": self.project_id,
            "datastream_id": self.datastream_id,
            "kind": self.kind,
            "actor": self.actor,
            "target_versions": self.target_versions,
            "interval": self.interval,
            "impact": self.impact,
            "quota": self.quota,
            "lock_ref": self.lock_ref,
            "rollback_ref": self.rollback_ref,
        }


def _server_estimated_points(
    conn,
    *,
    datastream_id: str,
    project_id: str | None,
    kind: str,
    interval: dict | None,
) -> int:
    """Derive provider quota points from the immutable current plan.

    A client estimate is never authoritative. Reprocess is provider-free and
    therefore has an exact zero cost. Provider-calling verbs require a persisted
    capability quota in the plan snapshot; missing or malformed evidence blocks
    preparation instead of silently accepting zero.
    """
    if kind == KIND_REPROCESS:
        return 0
    if not project_id:
        raise BoundedRecoveryError(
            "quota_estimate_unavailable",
            "Projet du flux introuvable : estimation de quota impossible.",
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT normalized_payload
            FROM app.datastream_plan_versions
            WHERE id = (
                SELECT current_plan_version_id FROM app.datastreams
                WHERE id = %s AND project_id = %s
            )
              AND datastream_id = %s AND project_id = %s
            """,
            (datastream_id, project_id, datastream_id, project_id),
        )
        row = cur.fetchone()
    payload = row[0] if row else None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    quota_cost = (
        (((payload or {}).get("geographic") or {}).get("impact") or {}).get("quota_cost")
        if isinstance(payload, dict)
        else None
    )
    read_points = quota_cost.get("read_points") if isinstance(quota_cost, dict) else None
    if isinstance(read_points, bool) or not isinstance(read_points, int) or read_points < 0:
        raise BoundedRecoveryError(
            "quota_estimate_unavailable",
            "Le plan courant ne contient pas d'estimation de quota fiable.",
        )
    multiplier = 1
    if kind == KIND_RELOAD:
        windows = (interval or {}).get("windows")
        if not isinstance(windows, list) or not windows:
            raise BoundedRecoveryError(
                "quota_estimate_unavailable",
                "Les fenetres du rechargement ne permettent pas d'estimer le quota.",
            )
        multiplier = len(windows)
    return read_points * multiplier

def assemble_proposal(
    conn,
    *,
    org_id: str,
    datastream_id: str,
    kind: str,
    actor: str,
    reason: str | None,
    date_from: str | None,
    date_to_exclusive: str | None,
    partition: str | None,
    chosen_mapping_version_id: str | None,
    today: date | None = None,
) -> BoundedProposal:
    """Assemble ONE bounded-recovery proposal for a named verb (composes seams).

    REUSES the Operations substrate to read the live target, pin versions, run
    the quota pre-check and derive impact -- never a parallel engine. Per verb:

      * ``synchronize`` -> incremental ``{from, to}`` window from the datastream's
        refetch depth; scope = new data.
      * ``reload`` -> normalized half-open ``[from, to)`` scope + <=31-day
        re-pull windows (reuses ``normalize_reload_scope`` / Story 26.1
        windowing). Refuses an over-wide/empty range as ``forbidden_interval``.
      * ``reprocess`` -> retention + schema-compatibility precheck against the
        CHOSEN mapping (``evaluate_reprocess_retention``). Refuses
        ``retention_unavailable`` / ``incompatible_schema`` WITHOUT any provider
        call. On success the proposal pins the CHOSEN mapping version (not
        necessarily the live one) so the confirm reapplies exactly it.

    Raises ``BoundedRecoveryError`` on any precondition breach (the caller maps
    the ``code`` to a stable API/ToolError response). Never dispatches, never
    publishes.
    """
    from core import operations_mcp  # noqa: PLC0415

    if kind not in BOUNDED_KINDS:
        raise BoundedRecoveryError("invalid_kind", "Type de recuperation borne non autorise.")

    today = today or date.today()
    target = operations_mcp._load_target(conn, datastream_id)
    if target is None:
        raise BoundedRecoveryError("not_found", "Flux introuvable.")

    live_plan = target.get("plan_version_id")
    live_mapping = target.get("mapping_version_id")
    platform = target.get("platform")

    interval: dict | None = None
    reload_scope: ReloadScope | None = None
    reprocess_check: RetentionCheck | None = None
    pinned_mapping = live_mapping  # reprocess may override this with a chosen version.

    if kind == KIND_SYNCHRONIZE:
        refetch_days = _load_refetch_days(conn, datastream_id)
        interval = synchronize_interval(today, refetch_days)

    elif kind == KIND_RELOAD:
        reload_scope = normalize_reload_scope(date_from, date_to_exclusive, partition)
        # The proposal's canonical interval is the inclusive [from, last day]
        # envelope; the half-open windows ride in extras/impact for the confirm.
        interval = {
            "from": reload_scope.date_from,
            "to": (
                date.fromisoformat(reload_scope.date_to_exclusive) - timedelta(days=1)
            ).isoformat(),
            "to_exclusive": reload_scope.date_to_exclusive,
            "windows": [dict(w) for w in reload_scope.windows],
            "partition": reload_scope.partition,
        }

    elif kind == KIND_REPROCESS:
        chosen = (chosen_mapping_version_id or "").strip() or live_mapping
        if not chosen:
            raise BoundedRecoveryError(
                "invalid_mapping",
                "Aucun mapping choisi et aucun mapping courant : impossible de "
                "retraiter.",
            )
        retained = _load_retained_source(conn, datastream_id)
        chosen_hash = _load_mapping_source_schema_hash(conn, chosen)
        reprocess_check = evaluate_reprocess_retention(
            retained_execution_id=(retained or {}).get("execution_id"),
            retained_source_schema_hash=(retained or {}).get("source_schema_hash"),
            chosen_mapping_source_schema_hash=chosen_hash,
        )
        if not reprocess_check.available:
            # Actionable refusal -- NO provider call, NO candidate created.
            raise BoundedRecoveryError(reprocess_check.code or "reprocess_blocked",
                                       reprocess_check.reason or "Retraitement impossible.")
        pinned_mapping = chosen
        interval = {
            "retained_execution_id": reprocess_check.retained_execution_id,
            "chosen_mapping_version_id": chosen,
            "calls_provider": False,
        }

    estimated_points = _server_estimated_points(
        conn,
        datastream_id=datastream_id,
        project_id=target.get("project_id"),
        kind=kind,
        interval=interval,
    )
    target_versions = {
        "plan_version_id": live_plan,
        "mapping_version_id": pinned_mapping,
        "policy_version": operations_mcp._current_policy_version(),
    }
    quota = operations_mcp._quota_estimate(platform, estimated_points)
    impact = build_impact(
        kind, interval=interval, reload_scope=reload_scope, reprocess=reprocess_check
    )
    impact["reason"] = reason or None


    return BoundedProposal(
        org_id=org_id,
        project_id=target.get("project_id"),
        datastream_id=datastream_id,
        kind=kind,
        actor=actor,
        target_versions=target_versions,
        interval=interval,
        impact=impact,
        quota=quota,
        lock_ref=target.get("active_execution_id"),
        rollback_ref=target.get("current_published_execution_id"),
        reason=(reason or None),
    )


def _load_refetch_days(conn, datastream_id: str) -> int | None:
    """Return the datastream's configured refetch depth in days, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT refetch_days FROM app.datastreams WHERE id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


# ---------------------------------------------------------------------------
# prepare / confirm domain entry points (AD-27, wrap the existing substrate).
# ---------------------------------------------------------------------------


def prepare_bounded_recovery(
    conn,
    *,
    org_id: str,
    datastream_id: str,
    kind: str,
    actor: str,
    reason: str | None = None,
    date_from: str | None = None,
    date_to_exclusive: str | None = None,
    partition: str | None = None,
    chosen_mapping_version_id: str | None = None,
    today: date | None = None,
) -> dict:
    """Prepare an IMMUTABLE bounded-recovery proposal (AD-27 prepare).

    Assembles the proposal (``assemble_proposal``) and persists it into the SAME
    immutable-proposal store the Operations profile writes
    (``operations_mcp._insert_preparation`` -> ``app.operation_preparations``,
    migration 070). Creates NO durable operation and dispatches NOTHING. Returns
    the ``preparation_id`` + the full stated proposal (interval/partition +
    versions + impact + quota + rollback pointer + idempotency ttl). The caller
    (MCP tool / admin_api route) owns the guard + commit boundary.

    ``reason`` is frozen into the proposal impact and then copied to the durable
    operation at confirm time; confirmation cannot replace that audited reason.
    """
    from core import operations_mcp  # noqa: PLC0415

    proposal = assemble_proposal(
        conn,
        org_id=org_id,
        datastream_id=datastream_id,
        kind=kind,
        actor=actor,
        reason=reason,
        date_from=date_from,
        date_to_exclusive=date_to_exclusive,
        partition=partition,
        chosen_mapping_version_id=chosen_mapping_version_id,
        today=today,
    )

    fingerprint = operations_mcp._proposal_fingerprint(
        {
            "org_id": proposal.org_id,
            "project_id": proposal.project_id,
            "datastream_id": proposal.datastream_id,
            "actor": proposal.actor,
            "kind": proposal.kind,
            "interval": proposal.interval,
            "target_versions": proposal.target_versions,
        }
    )
    idempotency_key = operations_mcp._recovery_idempotency_key(
        proposal.org_id, proposal.datastream_id, proposal.kind, fingerprint
    )
    preparation_id = operations_mcp._insert_preparation(
        conn, proposal.as_insert_payload(), idempotency_key
    )

    return {
        "preparation_id": preparation_id,
        "kind": proposal.kind,
        "target": {
            "datastream_id": proposal.datastream_id,
            "project_id": proposal.project_id,
        },
        "target_versions": proposal.target_versions,
        "interval": proposal.interval,
        "impact": proposal.impact,
        "quota": proposal.quota,
        "lock_ref": proposal.lock_ref,
        "rollback_ref": proposal.rollback_ref,
        "reason": proposal.reason,
        "expires_in_seconds": operations_mcp._PREPARATION_TTL_SECONDS,
    }


def confirm_bounded_recovery(
    conn,
    *,
    preparation_id: str,
    expected_org_id: str,
    expected_project_id: str,
    expected_datastream_id: str,
    actor: str,
    reason: str | None = None,
    trace_id: str,
) -> dict:
    """Confirm or durably replay one exact scoped bounded recovery proposal."""
    from core import operations_mcp  # noqa: PLC0415
    from core.operations import OperationSpec, _sha256, execute_operation  # noqa: PLC0415

    prep = operations_mcp._load_preparation(conn, preparation_id)
    if prep is None or prep.get("org_id") is None:
        raise BoundedRecoveryError("not_found", "Proposition introuvable.")
    exact_scope = (
        prep.get("org_id") == expected_org_id
        and prep.get("project_id") == expected_project_id
        and prep.get("datastream_id") == expected_datastream_id
        and prep.get("actor") == actor
    )
    if not exact_scope:
        raise BoundedRecoveryError("not_found", "Proposition introuvable.")
    if prep["kind"] not in BOUNDED_KINDS:
        raise BoundedRecoveryError("wrong_verb", "Verbe hors du perimetre borne 12.11.")
    if prep["state"] not in {"prepared", "confirmed"}:
        raise BoundedRecoveryError("stale_preparation", "Proposition perimee ou deja traitee.")
    confirmed_replay = prep["state"] == "confirmed"
    if not confirmed_replay and prep["expired"]:
        raise BoundedRecoveryError("stale_preparation", "Proposition perimee ou deja traitee.")
    if confirmed_replay and not prep.get("operation_id"):
        raise BoundedRecoveryError(
            "outcome_unknown", "La proposition est confirmee sans operation durable liee."
        )

    target = operations_mcp._load_target(conn, prep["datastream_id"])
    if target is None or any(
        (
            target.get("org_id") != expected_org_id,
            target.get("project_id") != expected_project_id,
            target.get("datastream_id") != expected_datastream_id,
        )
    ):
        raise BoundedRecoveryError("not_found", "Flux introuvable.")

    prepared_reason = (prep.get("impact") or {}).get("reason")
    if reason is not None and reason != prepared_reason:
        raise BoundedRecoveryError("stale_preparation", "Motif different de la proposition.")

    if not confirmed_replay:
        if prep["kind"] in {KIND_SYNCHRONIZE, KIND_RELOAD}:
            if not operations_mcp._versions_match(prep["target_versions"], target):
                raise BoundedRecoveryError("stale_versions", "Versions plan/mapping perimees.")
        elif prep["target_versions"].get("plan_version_id") != target.get("plan_version_id"):
            raise BoundedRecoveryError("stale_versions", "Version de plan perimee.")

        if (
            prep["target_versions"].get("policy_version")
            != operations_mcp._current_policy_version()
        ):
            raise BoundedRecoveryError(
                "policy_changed", "Politique modifiee depuis la preparation."
            )
        if prep["kind"] == KIND_RELOAD:
            _revalidate_reload_interval(prep.get("interval") or {})
        if prep["kind"] in {KIND_SYNCHRONIZE, KIND_RELOAD} and not target.get("platform"):
            raise BoundedRecoveryError("missing_exposure", "Exposition de compte manquante.")
        if prep["kind"] in {KIND_SYNCHRONIZE, KIND_RELOAD}:
            estimated_points = (prep.get("quota") or {}).get("estimated_points")
            if (
                isinstance(estimated_points, bool)
                or not isinstance(estimated_points, int)
                or estimated_points < 0
            ):
                raise BoundedRecoveryError(
                    "quota_estimate_unavailable", "Estimation de quota absente ou invalide."
                )
            quota_now = operations_mcp._quota_estimate(
                target.get("platform"), estimated_points
            )
            if not quota_now.get("can_proceed"):
                raise BoundedRecoveryError("quota_violation", "Budget quota insuffisant.")
        if target.get("active_execution_id"):
            raise BoundedRecoveryError("lock_conflict", "Une execution est deja active.")

    fingerprint = operations_mcp._proposal_fingerprint(
        {
            "org_id": prep["org_id"],
            "project_id": prep["project_id"],
            "datastream_id": prep["datastream_id"],
            "actor": prep["actor"],
            "kind": prep["kind"],
            "interval": prep["interval"],
            "target_versions": prep["target_versions"],
        }
    )
    idempotency_key = operations_mcp._recovery_idempotency_key(
        prep["org_id"], prep["datastream_id"], prep["kind"], fingerprint
    )
    if _sha256(idempotency_key) != prep["idempotency_key_hash"]:
        raise BoundedRecoveryError("stale_preparation", "Proposition alteree.")

    policy_version = prep["target_versions"].get("policy_version")
    spec = OperationSpec(
        command_type=f"datastream.bounded.{prep['kind']}",
        actor=actor,
        effective_org_id=prep["org_id"],
        resource_path=(
            f"organization:{prep['org_id']}",
            f"project:{prep['project_id']}",
            f"flux:{prep['datastream_id']}",
        ),
        idempotency_key=idempotency_key,
        host_context={},
        versions={
            "policy": policy_version,
            "catalog": policy_version,
            "tool": "confirm_bounded_recovery",
        },
        request_payload={
            "org_id": prep["org_id"],
            "project_id": prep["project_id"],
            "datastream_id": prep["datastream_id"],
            "actor": prep["actor"],
            "kind": prep["kind"],
            "interval": prep["interval"],
            "reason": prepared_reason,
            "plan_version_id": prep["target_versions"].get("plan_version_id"),
            "mapping_version_id": prep["target_versions"].get("mapping_version_id"),
        },
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=preparation_id,
        trace_id=trace_id,
    )

    op_result = execute_operation(
        conn,
        spec,
        mutation=lambda c, op_id: _dispatch_bounded_recovery(c, op_id, prep, target),
    )
    if confirmed_replay and op_result.operation_id != prep["operation_id"]:
        raise BoundedRecoveryError(
            "outcome_unknown", "Le rejeu ne correspond pas a l'operation confirmee."
        )
    if not confirmed_replay:
        operations_mcp._mark_preparation_confirmed(
            conn, preparation_id, op_result.operation_id
        )
    durable_trace_id = operations_mcp._load_operation_trace(conn, op_result.operation_id)
    if not durable_trace_id:
        raise BoundedRecoveryError(
            "outcome_unknown", "L'operation durable ne porte aucune trace serveur."
        )

    return {
        "preparation_id": preparation_id,
        "operation_id": op_result.operation_id,
        "trace_id": durable_trace_id,
        "outcome": op_result.outcome,
        "replayed": op_result.replayed or confirmed_replay,
        "result": op_result.result,
    }


def _revalidate_reload_interval(interval: dict) -> None:
    """Re-validate a persisted reload interval at confirm time (fail-closed).

    Rebuilds the half-open scope from the stored bounds so a proposal that was
    somehow persisted with an over-wide/empty range is refused as
    ``forbidden_interval`` at confirm time too (defence in depth -- the immutable
    proposal was already bounded at prepare, but the confirm never trusts it).
    """
    date_from = interval.get("from")
    to_exclusive = interval.get("to_exclusive")
    if not date_from or not to_exclusive:
        raise BoundedRecoveryError("forbidden_interval", "Intervalle de rechargement absent.")
    normalize_reload_scope(date_from, to_exclusive, interval.get("partition"))


# ---------------------------------------------------------------------------
# The bounded mutation (routed through operations.execute_operation ONCE).
# ---------------------------------------------------------------------------


def _dispatch_bounded_recovery(conn, operation_id: str, prep: dict, target: dict):
    """Perform the bounded recovery for one confirmed proposal (Story 36.2 mutation).

    Runs INSIDE ``operations.execute_operation`` so it is atomic with the durable
    operation's audit + outbox and idempotent on replay. Reuses
    ``datastream_publication.create_execution`` to mint ONE non-live CANDIDATE
    execution against the pinned/chosen plan + mapping versions. It NEVER commits
    a publication and NEVER mutates ``current_published_execution_id`` -- an
    invalid candidate stays UNPUBLISHED (AC4).

    Per verb the ``projection_plan`` carried on the candidate declares the scope:
      * synchronize -> ``recovery_kind=synchronize`` + incremental interval;
      * reload      -> ``recovery_kind=reload`` + half-open windows (supersede
        ONLY that scope, AC2);
      * reprocess   -> ``recovery_kind=reprocess`` + ``calls_provider=False`` +
        the retained-execution reference (NO provider dispatch, AC3).

    Returns a ``MutationResult``. The candidate creation itself may be rejected
    (concurrent execution, invalid reference) -> a ``failed`` outcome with an
    actionable reason; it is NEVER a duplicate durable operation (the operations
    idempotency guard owns that).
    """
    from core.datastream_publication import (  # noqa: PLC0415
        PublicationError,
        create_execution,
    )
    from core.operations import MutationResult, _canonical_hash  # noqa: PLC0415

    kind = prep["kind"]
    project_id = prep["project_id"]
    datastream_id = prep["datastream_id"]
    interval = prep.get("interval") or {}

    projection_plan: dict = {"executable": True, "recovery_kind": kind}
    if kind == KIND_SYNCHRONIZE:
        projection_plan["interval"] = {
            "from": interval.get("from"),
            "to": interval.get("to"),
        }
        projection_plan["scope"] = "incremental_new_data"
    elif kind == KIND_RELOAD:
        # Carry the half-open windows so the re-pull supersedes ONLY that scope.
        projection_plan["half_open_range"] = {
            "from": interval.get("from"),
            "to_exclusive": interval.get("to_exclusive"),
        }
        projection_plan["windows"] = interval.get("windows") or []
        projection_plan["partition"] = interval.get("partition")
        projection_plan["scope"] = "bounded_range_supersede"
    elif kind == KIND_REPROCESS:
        projection_plan["calls_provider"] = False
        projection_plan["retained_execution_id"] = interval.get("retained_execution_id")
        projection_plan["chosen_mapping_version_id"] = interval.get("chosen_mapping_version_id")
        projection_plan["scope"] = "reapply_mapping_no_provider"

    plan_version_id = prep["target_versions"].get("plan_version_id")
    mapping_version_id = prep["target_versions"].get("mapping_version_id")

    try:
        execution = create_execution(
            datastream_id=datastream_id,
            project_id=project_id,
            plan_version_id=plan_version_id,
            mapping_version_id=mapping_version_id,
            projection_plan=projection_plan,
            actor=prep["actor"],
            idempotency_key=f"{operation_id}:candidate",
            conn=conn,
        )
    except PublicationError as exc:
        logger.warning("bounded_recovery: candidate rejected kind=%s: %s", kind, exc)
        return MutationResult(
            outcome="failed", before_hash=None, after_hash=None,
            result={"reason": "candidate_rejected", "kind": kind,
                    "code": getattr(exc, "code", "publication_error")},
            outbox_payload={"kind": kind, "datastream_id": datastream_id},
        )
    except Exception as exc:  # noqa: BLE001 -- concurrency/lock conflict, never a dup.
        logger.warning("bounded_recovery: candidate conflict kind=%s: %s", kind, exc)
        return MutationResult(
            outcome="failed", before_hash=None, after_hash=None,
            result={"reason": "lock_conflict", "kind": kind},
            outbox_payload={"kind": kind, "datastream_id": datastream_id},
        )

    result = {
        "kind": kind,
        "execution_id": execution.get("id"),
        "state": execution.get("state"),
        "scope": projection_plan.get("scope"),
        "calls_provider": projection_plan.get("calls_provider", True),
    }
    return MutationResult(
        outcome="succeeded", before_hash=None, after_hash=_canonical_hash(result),
        result=result, outbox_payload=result,
    )
