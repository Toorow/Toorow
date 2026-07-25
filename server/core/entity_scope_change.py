"""toorow -- Governed add-scope previews + confirmation for the brand registry (Story 40.4).

The ADD-SCOPE GATE of Epic 40 (competitor / brand registry). It SPECIALISES -- does NOT
refork (E40-NFR05 / E40-AD3) -- the Epic 37 geographic add-scope machinery
(``core.geographic_change`` + migration 058): an immutable, idempotent, no-side-effect
PREVIEW computed from a dependency FINGERPRINT, listing per bound source the extra outbound
queries/pulls implied (quota cost, rate-limit / provider backfill window, estimated volume)
and a CARDINALITY estimate, then an explicit BACKFILL DECISION on confirmation.

Where Epic 37 previewed "add a tracked COUNTRY to the joint grain", 40.4 previews "add a
tracked ENTITY / source BINDING to the collection set". The SHAPE is identical
(preview -> fingerprint -> per-item impact rows -> confirm with backfill decision -> audit);
only the DELTA being scoped changes (a brand's entity_source_bindings query drivers, not a
country dimension). Adding 50 competitors surfaces "+N outbound queries, quota X" per bound
source BEFORE activation instead of silently multiplying quota usage.

SIBLING of ``geographic_change.py`` (NOT a modification of it): the pure Epic 37 primitives
(``_canonical_json`` / ``_sha256`` / ``_estimate_volume`` / ``validate_confirmation_dependencies``)
are IMPORTED here, never re-derived -- copy-pasting the fingerprint / volume / stale-guard
logic would fork the single source of truth (E40-NFR05). ``dependency_fingerprint`` in 37.4 is
posture-shaped, so 40.4 provides ``dependency_fingerprint_for_add`` built on the imported
``_sha256`` (a thin adapter over the same hash, NOT a fork). If a shared helper needs
generalising, that is an orchestrator note -- this module NEVER edits ``geographic_change.py``.

BACKFILL EXECUTION is the Epic 12 candidate-publication contract, REUSED not re-implemented
(E40-NFR05). On ``backfill_decision="request"`` each backfill action is built ONLY from the
impact's ``backfill_contract`` (the refetch / reprocess / candidate-publication paths the
impact already carries) and marked ``candidate_only`` / ``touches_published_pointer=False``:
a candidate execution is created, the live pointer is NOT moved, and a failed/empty/partial
candidate stays UNPUBLISHED (bounded_recovery invariant). 40.4 mints NO pull, NO publication,
NO rollback engine.

NO-BACKFILL HONESTY (E40-NFR06 / AD-9) -- the load-bearing guarantee. On
``backfill_decision="defer"`` the binding activates for FORWARD collection only:
``confirm`` read-modifies NO existing figure (existing figures are byte-unchanged, drift =
CRITICAL) and past periods carry an EXPLICIT TYPED absence
(``coverage_state="forward_only"`` / ``brand_absent_historical=True``) via the pure
``historical_absence_marker`` -- NEVER a fabricated zero, a back-projected row or a silent
join. This is the geo twin of 37.4's "already-published history remains queryable; retained
history is neither rewritten nor deleted".

AD-2 (ZERO provider vocabulary): this module contains NO connector name, NO brand name and
NO hardcoded cost/query literal. The per-source cost/cardinality comes from the source's
declared capability envelope (``report.quota_cost``, pagination-derived cardinality); the
backfill from the existing contract; the preview shape from ``geographic_change``. The ONLY
vocabulary is the coverage/backfill-state product constants mirroring the impact shape
(tolerated by the AD-2 grep exactly like geographic_change's status literals and
tracked_entity_registry's ROLE_* constants). 40.4 sizes cost/backfill only; it NEVER licenses
a cross-source metric comparison (that stays Epic 27 / 40.6).

Design mirrors ``geographic_change.py`` / ``tracked_entity_registry.py``: ``from __future__
import annotations``, ``hashlib`` / ``json``, prefixed-ULID mint helper, lazy ``core.*``
imports inside the DB / dispatch functions, pure functions kept separate from I/O so the
invariants are testable without Postgres. All log / message strings are ASCII-safe.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any, Callable, Iterable, Mapping

from ulid import ULID

from core.geographic_change import (  # noqa: F401 -- REUSE the 37.4 add-scope primitives (E40-NFR05)
    _canonical_json,
    _estimate_volume,
    _sha256,
    validate_confirmation_dependencies,  # fingerprint-equality + blocking-gap refusal
)

# ---------------------------------------------------------------------------
# Coverage / backfill state product constants (mirror the impact shape). These are the ONLY
# vocabulary in this module; the AD-2 grep tolerates them exactly like geographic_change's
# status literals -- no provider/brand name, no hardcoded cost/query number lives here.
# ---------------------------------------------------------------------------

COVERAGE_CONSOLIDATED = "consolidated"
COVERAGE_FORWARD_ONLY = "forward_only"
COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_UNAVAILABLE = "unavailable"

# 40.4-local audit action verbs -- defined HERE (not in the high-contention core/audit.py) to keep
# this story strictly additive; passed to core.audit.insert_audit_row as the action string.
ACTION_ENTITY_SCOPE_CHANGE_CONFIRMED = "entity_scope_change.confirmed"
ACTION_ENTITY_SCOPE_CHANGE_BLOCKED = "entity_scope_change.blocked"

# proposed_add.kind literals (product data shape, NOT a provider allow-list).
ADD_KIND_ENTITY = "entity"
ADD_KIND_BINDING = "binding"

_PREVIEW_PREFIX = "escprev_"


# ---------------------------------------------------------------------------
# Typed errors (callers/endpoints map these; core never raises HTTP here). Mirror the
# geographic_change error family -- same ``code`` attribute convention.
# ---------------------------------------------------------------------------


class EntityScopeChangeError(RuntimeError):
    code = "entity_scope_change_error"


class EntityScopePreviewNotFound(EntityScopeChangeError):
    code = "entity_scope_preview_not_found"


class EntityScopePreviewStale(EntityScopeChangeError):
    code = "entity_scope_preview_stale"


class EntityScopePreviewBlocked(EntityScopeChangeError):
    code = "entity_scope_preview_blocked"


class EntityScopePreviewConflict(EntityScopeChangeError):
    code = "entity_scope_preview_conflict"


# ---------------------------------------------------------------------------
# Loader seams (Callable). Default readers hit app.entity_source_bindings (40.2) +
# source_capabilities; offline tests inject fakes. If 40.2's table/module has not landed at
# dev time the preview still computes from the injected loader -- 40.4 does NOT hard-block on
# 40.2's DDL and does NOT fork the binding schema (Completion Notes seam).
# ---------------------------------------------------------------------------

# BindingLoader(entity_id) -> list of binding rows {id, source, external_id, query_spec, ...}.
BindingLoader = Callable[[str], list[dict[str, Any]]]
# CapabilityLoader(binding) -> the source capability envelope dict (or None if unavailable).
CapabilityLoader = Callable[[Mapping[str, Any]], dict[str, Any] | None]


def _default_binding_loader(entity_id: str) -> list[dict[str, Any]]:
    """Default: read the entity's per-source bindings via the 40.2 store (E40-FR07)."""
    from core.entity_source_bindings import list_bindings_for_entity  # noqa: PLC0415

    return list_bindings_for_entity(entity_id)


def _load_entity(entity_id: str) -> dict[str, Any]:
    """Read the 40.1 entity master for metadata, fail-soft to a minimal dict.

    The entity is METADATA only here (its id is the load-bearing field and is also carried
    on each binding row) -- the add-scope cost/cardinality is sized entirely off the bindings
    + capability envelopes. A missing entity master (40.1 not yet applied) or an unreachable
    DB therefore degrades to ``{'id': entity_id}`` rather than blocking the preview: the
    binding loader is the injectable seam that keeps the offline path DB-free."""
    try:
        from core.tracked_entity_registry import get_tracked_entity  # noqa: PLC0415

        return get_tracked_entity(entity_id) or {"id": entity_id}
    except Exception:  # noqa: BLE001 - metadata read, never the load-bearing input.
        return {"id": entity_id}


def _default_capability_loader(binding: Mapping[str, Any]) -> dict[str, Any] | None:
    """Default: resolve the bound source's declared capability envelope.

    Source-agnostic: the binding carries the OPAQUE source handle (a module name); the
    capability envelope (reports / quota_cost / pagination / incremental) is read from the
    source_capabilities layer, never interpreted here (AD-2). Returns None when the source
    has no declared envelope (surfaced as a blocking gap by build_binding_scope_impact)."""
    source = str(binding.get("source") or "")
    if not source:
        return None
    # RUNTIME WIRING SEAM (40.5): resolving a capability envelope from an OPAQUE source handle
    # ALONE has no existing accessor (get_scoped_source_capabilities needs project + connection
    # ref -- a different contract). Until 40.5 wires a real source-handle -> envelope resolver,
    # the default returns None -> build_binding_scope_impact surfaces a blocking gap
    # ('envelope unavailable'), never a crash. Callers (and every test) inject an explicit
    # capability_loader; this is the honest not-yet-wired fallback (no bogus import).
    return None


# ---------------------------------------------------------------------------
# Report envelope helpers -- source-agnostic reads off the declared capability envelope.
# ---------------------------------------------------------------------------


def _find_report(capabilities: Mapping[str, Any] | None, report_id: str) -> dict[str, Any]:
    reports = (capabilities or {}).get("reports")
    if not isinstance(reports, list):
        return {}
    if report_id:
        for item in reports:
            if isinstance(item, dict) and item.get("id") == report_id:
                return dict(item)
    # No report pinned by the binding -> the first declared report is the driver envelope.
    for item in reports:
        if isinstance(item, dict):
            return dict(item)
    return {}


def _report_id_from_binding(binding: Mapping[str, Any]) -> str:
    query_spec = binding.get("query_spec")
    if isinstance(query_spec, Mapping):
        report_id = query_spec.get("report_id")
        if isinstance(report_id, str):
            return report_id
    return ""


def _cardinality_estimate(report: Mapping[str, Any]) -> dict[str, Any]:
    """Cardinality estimate off the declared envelope (pagination + supported grains).

    Source-agnostic: NO hardcoded number. When pagination bounds the rows we surface the
    provider bound; the joint-grain width (max supported_grains length) qualifies the
    dimensional cardinality. An envelope with neither yields an explicit 'unavailable'."""
    volume = _estimate_volume(report)
    grains = report.get("supported_grains")
    grain_width = 0
    if isinstance(grains, list):
        for grain in grains:
            if isinstance(grain, list):
                grain_width = max(grain_width, len(grain))
    return {
        "upper_bound_rows": volume.get("upper_bound_rows"),
        "confidence": volume.get("confidence"),
        "grain_width": grain_width or None,
        "basis": "provider_capability_envelope",
    }


def _supports_historical_extraction(report: Mapping[str, Any]) -> bool:
    """True when the source can re-pull past periods (a windowed / cursor incremental mode).

    Source-agnostic historical-extraction signal off the declared envelope: a full-refresh
    report cannot re-pull a bounded past window, so backfill is impossible (a blocking gap on
    a 'request'); a date_window / cursor report can (E40-FR07 provider backfill window)."""
    incremental = report.get("incremental")
    if not isinstance(incremental, Mapping):
        return False
    return incremental.get("mode") in {"date_window", "cursor"}


# ---------------------------------------------------------------------------
# B.2 Impact builder (per bound source) -- the twin of geographic_change.build_datastream_impact.
# ---------------------------------------------------------------------------


def build_binding_scope_impact(
    entity: Mapping[str, Any],
    binding: Mapping[str, Any],
    capabilities: Mapping[str, Any] | None,
    *,
    earliest_pull_date: str | None,
) -> dict[str, Any]:
    """Build one deterministic, source-agnostic add-scope impact row without writes.

    The per-source twin of ``geographic_change.build_datastream_impact``: computes
    ``estimated_volume`` (``_estimate_volume`` on the source report envelope),
    ``estimated_cost`` (``report.quota_cost``), ``cardinality_estimate``,
    ``max_provider_backfill_days``, ``backfill_required`` / ``provider_pull_required``,
    ``coverage_state``, ``blocking_gaps`` (source has no declared envelope, no query driver,
    or no historical extraction) and the reused ``backfill_contract`` (refetch / reprocess /
    candidate-publication paths). NO writes, NO provider vocabulary, NO hardcoded cost."""

    binding_id = str(binding.get("id") or "")
    source = str(binding.get("source") or "")
    entity_id = str(entity.get("id") or binding.get("entity_id") or "")
    report_id = _report_id_from_binding(binding)
    report = _find_report(capabilities, report_id)

    query_spec = binding.get("query_spec")
    has_query_driver = bool(binding.get("external_id")) or bool(
        isinstance(query_spec, Mapping) and query_spec
    )
    has_envelope = bool(report)
    supports_historical = _supports_historical_extraction(report)

    blocking_gaps: list[dict[str, Any]] = []
    if not has_query_driver:
        blocking_gaps.append(
            {
                "code": "binding_query_driver_missing",
                "message": "This binding carries no query driver (no external_id / query_spec).",
                "repair": {"action": "set_binding_external_id_or_query_spec"},
            }
        )
    if not has_envelope:
        blocking_gaps.append(
            {
                "code": "source_capability_envelope_unavailable",
                "message": "The bound source declares no capability envelope for this add.",
                "repair": {"action": "declare_or_select_source_report_envelope"},
            }
        )

    quota = report.get("quota_cost") if isinstance(report.get("quota_cost"), Mapping) else {}
    provider_limit = report.get("max_provider_backfill_days")
    max_backfill_days = provider_limit if isinstance(provider_limit, int) else None

    # Backfill is required only when the source CAN extract history and no such history was
    # ever pulled for this brand (earliest_pull_date is None). If the source cannot extract
    # history at all, backfill is impossible -> a blocking gap, never a silent forward-only.
    retained_history = earliest_pull_date is not None
    backfill_required = supports_historical and not retained_history
    if not supports_historical:
        blocking_gaps.append(
            {
                "code": "source_historical_extraction_unavailable",
                "message": "The bound source cannot re-pull past periods (no windowed extraction).",
                "repair": {"action": "defer_backfill_forward_only_or_select_windowed_report"},
            }
        )

    if blocking_gaps and not has_envelope:
        coverage_state = COVERAGE_UNAVAILABLE
    elif retained_history:
        coverage_state = COVERAGE_COMPLETE
    elif backfill_required:
        coverage_state = COVERAGE_PARTIAL
    else:
        # No backfill possible / needed and no retained history -> forward-only collection.
        coverage_state = COVERAGE_FORWARD_ONLY

    # One outbound driver per bound source per collection window (the "50 competitors"
    # multiply is the aggregate over per-source rows, computed in the preview).
    extra_query_count = 1 if has_query_driver else 0

    return {
        "binding_id": binding_id,
        "entity_id": entity_id,
        "source": source,
        "report_id": report_id or None,
        "compatibility": "compatible" if not blocking_gaps else "blocked",
        "has_query_driver": has_query_driver,
        "has_capability_envelope": has_envelope,
        "supports_historical_extraction": supports_historical,
        "earliest_available_pull_date": earliest_pull_date,
        "max_provider_backfill_days": max_backfill_days,
        "backfill_bound_evidence": (
            "provider_capability" if max_backfill_days is not None else "unavailable"
        ),
        "extra_query_count": extra_query_count,
        "estimated_volume": _estimate_volume(report),
        "estimated_cost": dict(quota),
        "cardinality_estimate": _cardinality_estimate(report),
        "blocking_gaps": blocking_gaps,
        "backfill_required": backfill_required,
        "provider_pull_required": backfill_required,
        "retained_history_preserved": True,
        "coverage_state": coverage_state,
        "backfill_contract": {
            "refetch_path": f"/api/datastreams/bindings/{binding_id}/refetch",
            "reprocess_path": f"/api/datastreams/bindings/{binding_id}/executions",
            "publication_path": f"/api/datastreams/bindings/{binding_id}/executions",
            "candidate_publication_required": True,
            "touches_published_pointer": False,
            "candidate_only": True,
        },
    }


# ---------------------------------------------------------------------------
# B.1 Fingerprint adapter (thin adapter over the imported _sha256, NOT a fork).
# ---------------------------------------------------------------------------


def dependency_fingerprint_for_add(
    proposed_add: Mapping[str, Any],
    bound_source_deps: Iterable[Mapping[str, Any]],
) -> str:
    """Fingerprint the add + its bound-source dependency set (twin of
    ``geographic_change.dependency_fingerprint``, built on the IMPORTED ``_sha256``).

    Order-stable over the sorted (binding_id, source, capability_fingerprint, query_driver)
    bundle so a re-fingerprint of the same add is byte-identical and any drift (a binding
    gained/lost, a source's capability envelope changed, a query driver moved) flips it --
    the stale-preview guard (``validate_confirmation_dependencies``) then refuses a stale
    confirm. NOT a re-derivation of the hash: ``_sha256`` is the single source of truth."""
    normalized = [
        {
            "binding_id": item.get("binding_id"),
            "source": item.get("source"),
            "capability_fingerprint": item.get("capability_fingerprint"),
            "query_driver": item.get("query_driver"),
        }
        for item in bound_source_deps
    ]
    normalized.sort(key=lambda item: (str(item["binding_id"]), str(item["source"])))
    return _sha256({"proposed_add": dict(proposed_add), "bound_sources": normalized})


# ---------------------------------------------------------------------------
# B.3 No-backfill honesty helper (PURE) -- the E40-NFR06 / AD-9 seam.
# ---------------------------------------------------------------------------


def historical_absence_marker(entity_id: str, *, project_id: str) -> dict[str, Any]:
    """The explicit typed past-absence coverage state for a ``defer`` add (PURE).

    Past periods honestly show the brand as ABSENT: ``coverage_state="forward_only"`` +
    ``brand_absent_historical=True``. This is a MARKER, not a fact -- it NEVER writes a
    fabricated zero, a back-projected row or a silent join (E40-NFR06, AD-9). The confirm
    stamps it onto the confirmation payload; the warehouse is not read or written."""
    return {
        "entity_id": entity_id,
        "project_id": project_id,
        "coverage_state": COVERAGE_FORWARD_ONLY,
        "brand_absent_historical": True,
        "fabricated_history": False,
        "back_projected": False,
        "basis": "no_backfill_forward_only",
    }


# ---------------------------------------------------------------------------
# Row <-> dict helper + dependency assembly.
# ---------------------------------------------------------------------------


def _mint_preview_id() -> str:
    return f"{_PREVIEW_PREFIX}{ULID()}"


def _preview_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = (
        "id",
        "project_id",
        "entity_id",
        "proposed_add",
        "impact",
        "dependency_fingerprint",
        "status",
        "confirmation",
        "requested_by",
        "created_at",
        "confirmed_at",
    )
    result = dict(zip(keys, row))
    for key in ("created_at", "confirmed_at"):
        if isinstance(result.get(key), (date, datetime)):
            result[key] = result[key].isoformat()
    return result


def _current_add_dependencies(
    entity_id: str,
    proposed_add: Mapping[str, Any],
    binding_loader: BindingLoader,
    capability_loader: CapabilityLoader,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, dict[str, Any] | None],
    str,
]:
    """Resolve the entity, its bound sources, their capability envelopes + the fingerprint.

    Reads-only: the entity master (40.1), the per-source bindings (40.2 via binding_loader)
    and each source's declared capability envelope. When ``proposed_add`` scopes a SINGLE
    binding (kind='binding'), only that binding is sized; when it scopes the whole entity
    (kind='entity'), every bound source is sized -- this is the "50 competitors" aggregate."""
    entity = _load_entity(entity_id)
    bindings = binding_loader(entity_id)
    add_kind = proposed_add.get("kind")
    if add_kind == ADD_KIND_BINDING:
        target_source = proposed_add.get("source")
        target_scope = proposed_add.get("account_scope")
        bindings = [
            binding
            for binding in bindings
            if binding.get("source") == target_source
            and (target_scope is None or binding.get("account_scope") == target_scope)
        ]

    capabilities_by_binding: dict[str, dict[str, Any] | None] = {}
    deps: list[dict[str, Any]] = []
    for binding in bindings:
        binding_id = str(binding.get("id") or "")
        capabilities = capability_loader(binding)
        capabilities_by_binding[binding_id] = capabilities
        query_driver = binding.get("external_id") or binding.get("query_spec")
        deps.append(
            {
                "binding_id": binding_id,
                "source": binding.get("source"),
                "capability_fingerprint": _sha256(capabilities) if capabilities else None,
                "query_driver": _sha256(query_driver) if query_driver else None,
            }
        )
    fingerprint = dependency_fingerprint_for_add(proposed_add, deps)
    return entity, bindings, capabilities_by_binding, fingerprint


def _build_impact(
    entity: Mapping[str, Any],
    bindings: list[dict[str, Any]],
    capabilities_by_binding: Mapping[str, dict[str, Any] | None],
) -> dict[str, Any]:
    """Aggregate the per-source impact rows into the preview ``impact`` (the '50 competitors'
    cost/cardinality multiply). PURE over already-loaded rows -- no I/O."""
    rows = [
        build_binding_scope_impact(
            entity,
            binding,
            capabilities_by_binding.get(str(binding.get("id") or "")),
            earliest_pull_date=binding.get("earliest_pull_date"),
        )
        for binding in bindings
    ]

    def _row_upper_bound(row: Mapping[str, Any]) -> int:
        estimate = row.get("estimated_cost")
        points = estimate.get("read_points") if isinstance(estimate, Mapping) else None
        return (
            int(points) * int(row.get("extra_query_count") or 0) if isinstance(points, int) else 0
        )

    def _card_total(row: Mapping[str, Any]) -> int:
        card = row.get("cardinality_estimate")
        rows_bound = card.get("upper_bound_rows") if isinstance(card, Mapping) else None
        return int(rows_bound) if isinstance(rows_bound, int) else 0

    return {
        "sources": rows,
        "affected_source_count": len(rows),
        "extra_query_count": sum(int(row.get("extra_query_count") or 0) for row in rows),
        "total_estimated_cost": {
            "read_points_upper_bound": sum(_row_upper_bound(row) for row in rows),
            "unit": "provider_read_points",
        },
        "cardinality_total": {
            "upper_bound_rows": sum(_card_total(row) for row in rows),
            "basis": "provider_capability_envelope",
        },
        "blocking_gap_count": sum(len(row["blocking_gaps"]) for row in rows),
        "backfill_required": any(row["backfill_required"] for row in rows),
        "provider_pull_required": any(row["provider_pull_required"] for row in rows),
    }


# ---------------------------------------------------------------------------
# B.4 Preview (immutable, idempotent, no-write) -- twin of create_geographic_change_preview.
# ---------------------------------------------------------------------------


def create_entity_scope_change_preview(
    *,
    project_id: str,
    entity_id: str,
    proposed_add: dict[str, Any],
    identity: str,
    idempotency_key: str,
    conn: object,
    binding_loader: BindingLoader | None = None,
    capability_loader: CapabilityLoader | None = None,
) -> dict[str, Any]:
    """Persist an immutable, no-side-effect add-scope preview (twin of
    ``create_geographic_change_preview``).

    Idempotency-key replay (same key + same ``proposed_add`` -> the SAME row,
    ``idempotent_replay=True``, no duplicate INSERT) + conflict (same key, different add ->
    ``EntityScopePreviewConflict``). Aggregate ``impact`` (``extra_query_count``,
    ``total_estimated_cost``, ``cardinality_total``, ``backfill_required``,
    ``blocking_gap_count``, per-source rows). Reads the entity (40.1) + bindings (40.2 via
    ``binding_loader``) + capabilities; writes ONLY the preview row -- NO registry, binding or
    warehouse write."""

    if not idempotency_key.strip():
        raise ValueError("Idempotency-Key is required")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()

    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            SELECT id, project_id, entity_id, proposed_add, impact,
                   dependency_fingerprint, status, confirmation,
                   requested_by, created_at, confirmed_at
            FROM app.entity_scope_change_previews
            WHERE project_id = %s AND idempotency_key_hash = %s
            """,
            (project_id, key_hash),
        )
        existing = cur.fetchone()
    if existing is not None:
        preview = _preview_dict(existing)
        if preview["proposed_add"] != proposed_add:
            raise EntityScopePreviewConflict("idempotency key belongs to another proposed add")
        preview["idempotent_replay"] = True
        return preview

    loader = binding_loader or _default_binding_loader
    cap_loader = capability_loader or _default_capability_loader
    entity, bindings, capabilities_by_binding, fingerprint = _current_add_dependencies(
        entity_id, proposed_add, loader, cap_loader
    )
    impact = _build_impact(entity, bindings, capabilities_by_binding)

    preview_id = _mint_preview_id()
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            INSERT INTO app.entity_scope_change_previews
                (id, project_id, entity_id, proposed_add, impact,
                 dependency_fingerprint, status, confirmation,
                 idempotency_key_hash, requested_by)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, 'pending', NULL, %s, %s)
            RETURNING id, project_id, entity_id, proposed_add, impact,
                      dependency_fingerprint, status, confirmation,
                      requested_by, created_at, confirmed_at
            """,
            (
                preview_id,
                project_id,
                entity_id,
                _canonical_json(proposed_add),
                _canonical_json(impact),
                fingerprint,
                key_hash,
                identity,
            ),
        )
        inserted = cur.fetchone()
    conn.commit()  # type: ignore[attr-defined]
    result = _preview_dict(inserted)
    result["idempotent_replay"] = False
    return result


def _fetch_preview_for_update(preview_id: str, project_id: str, conn: object) -> dict[str, Any]:
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            SELECT id, project_id, entity_id, proposed_add, impact,
                   dependency_fingerprint, status, confirmation,
                   requested_by, created_at, confirmed_at
            FROM app.entity_scope_change_previews
            WHERE id = %s AND project_id = %s
            FOR UPDATE
            """,
            (preview_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise EntityScopePreviewNotFound("entity scope preview not found")
    return _preview_dict(row)


# ---------------------------------------------------------------------------
# B.5 Confirmation (atomic, audited) -- twin of confirm_geographic_change.
# ---------------------------------------------------------------------------


def confirm_entity_scope_change(
    *,
    preview_id: str,
    project_id: str,
    identity: str,
    backfill_decision: str,
    conn: object,
    binding_loader: BindingLoader | None = None,
    capability_loader: CapabilityLoader | None = None,
) -> dict[str, Any]:
    """Confirm a fresh preview atomically (twin of ``confirm_geographic_change``).

    ``backfill_decision in {'defer','request'}``. ``FOR UPDATE``; a ``confirmed`` preview
    replays idempotently; a non-pending one is stale; a fresh-recompute whose fingerprint
    drifted (or whose per-source ``proposed_content`` hash changed) is Stale; blocking gaps
    refuse the confirm (audited ``.blocked``).

    On ``request``: ``backfill_actions`` are built ONLY from each impact's
    ``backfill_contract`` (the EXISTING refetch / reprocess / candidate-publication paths) --
    candidate-only, ``touches_published_pointer=False``, dispatched through the Epic 12
    ``bounded_recovery`` / ``refetch`` path (lazy import, ``commit=False``, caller-owned txn).
    A failed/empty/partial candidate stays UNPUBLISHED; 40.4 mints no pull/publication/rollback.

    On ``defer``: the binding activates forward-only and the confirmation stamps
    ``historical_absence_marker`` -- NO existing figure is read-modified, NO history is
    fabricated (E40-NFR06, AD-9).

    ONE ``insert_audit_row(ACTION_ENTITY_SCOPE_CHANGE_CONFIRMED)`` (or ``.blocked`` on refusal);
    ``UPDATE ... WHERE status='pending'`` rowcount==1 guard; ``conn.commit()``;
    ``except: conn.rollback(); raise``."""

    if backfill_decision not in {"defer", "request"}:
        raise ValueError("backfill_decision must be 'defer' or 'request'")

    preview = _fetch_preview_for_update(preview_id, project_id, conn)
    if preview["status"] == "confirmed":
        return {**(preview.get("confirmation") or {}), "idempotent_replay": True}
    if preview["status"] != "pending":
        raise EntityScopePreviewStale("preview is no longer pending")

    loader = binding_loader or _default_binding_loader
    cap_loader = capability_loader or _default_capability_loader
    entity, bindings, capabilities_by_binding, fingerprint = _current_add_dependencies(
        preview["entity_id"], preview["proposed_add"], loader, cap_loader
    )

    # Fresh-recompute equality: fingerprint drift OR a blocking gap -> refuse (audited).
    fresh_impact = _build_impact(entity, bindings, capabilities_by_binding)
    try:
        validate_confirmation_dependencies(
            {
                "dependency_fingerprint": preview["dependency_fingerprint"],
                # reuse the 37.4 guard's blocking-gap key by mapping our 'sources' onto it.
                "impact": {"datastreams": fresh_impact["sources"]},
            },
            fingerprint,
        )
    except Exception as exc:  # noqa: BLE001 - map the geographic guard's types onto ours.
        exc_type = type(exc).__name__
        if exc_type == "GeographicPreviewStale":
            raise EntityScopePreviewStale(
                "preview dependencies changed; create a fresh preview"
            ) from exc
        if exc_type == "GeographicPreviewBlocked":
            _audit_blocked(conn, preview, identity, backfill_decision, fresh_impact)
            raise EntityScopePreviewBlocked("preview contains blocking add-scope gaps") from exc
        raise

    fresh_sources = {row["binding_id"]: row for row in fresh_impact["sources"]}
    preview_sources = {
        row["binding_id"]: row
        for row in preview["impact"].get("sources", [])
        if isinstance(row, Mapping)
    }
    if set(fresh_sources) != set(preview_sources):
        raise EntityScopePreviewStale("bound source set changed; create a fresh preview")

    from core.audit import insert_audit_row  # noqa: PLC0415

    try:
        if backfill_decision == "request":
            backfill_actions = [
                {
                    "binding_id": row["binding_id"],
                    "source": row["source"],
                    **row["backfill_contract"],
                }
                for row in fresh_sources.values()
                if row["backfill_required"]
            ]
            _dispatch_backfill_candidates(conn, project_id, backfill_actions, identity)
            coverage_marker = None
            resulting_coverage = _aggregate_coverage(fresh_sources.values())
        else:  # defer -> forward-only + honest past absence, NO fabricated history.
            backfill_actions = []
            coverage_marker = historical_absence_marker(preview["entity_id"], project_id=project_id)
            resulting_coverage = COVERAGE_FORWARD_ONLY

        confirmation = {
            "preview_id": preview_id,
            "project_id": project_id,
            "entity_id": preview["entity_id"],
            "proposed_add": preview["proposed_add"],
            "affected_sources": [row["source"] for row in fresh_sources.values()],
            "cost_summary": {
                "extra_query_count": fresh_impact["extra_query_count"],
                "total_estimated_cost": fresh_impact["total_estimated_cost"],
                "cardinality_total": fresh_impact["cardinality_total"],
            },
            "backfill_decision": backfill_decision,
            "backfill_actions": backfill_actions,
            "coverage_state": resulting_coverage,
            "historical_absence_marker": coverage_marker,
            "idempotent_replay": False,
        }
        insert_audit_row(
            conn,
            identity=identity,
            action=ACTION_ENTITY_SCOPE_CHANGE_CONFIRMED,
            provider_account="",
            connection_ref="",
            metadata=confirmation,
        )
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                """
                UPDATE app.entity_scope_change_previews
                SET status = 'confirmed', confirmation = %s::jsonb,
                    confirmed_by = %s, confirmed_at = NOW()
                WHERE id = %s AND project_id = %s AND status = 'pending'
                """,
                (_canonical_json(confirmation), identity, preview_id, project_id),
            )
            if cur.rowcount != 1:
                raise EntityScopePreviewStale("preview was confirmed concurrently")
        conn.commit()  # type: ignore[attr-defined]
        return confirmation
    except Exception:
        conn.rollback()  # type: ignore[attr-defined]
        raise


def _aggregate_coverage(rows: Iterable[Mapping[str, Any]]) -> str:
    states = [row["coverage_state"] for row in rows]
    if not states:
        return COVERAGE_FORWARD_ONLY
    if COVERAGE_UNAVAILABLE in states:
        return COVERAGE_UNAVAILABLE
    if COVERAGE_PARTIAL in states:
        return COVERAGE_PARTIAL
    if COVERAGE_FORWARD_ONLY in states:
        return COVERAGE_FORWARD_ONLY
    return COVERAGE_COMPLETE


def _dispatch_backfill_candidates(
    conn: object,
    project_id: str,
    backfill_actions: list[dict[str, Any]],
    identity: str,
) -> None:
    """Dispatch each backfill action through the EXISTING Epic 12 candidate-publication
    contract (bounded_recovery / refetch) -- candidate-only, NO live-pointer move.

    40.4 re-implements NO pull, NO publication, NO rollback: it passes the ``backfill_contract``
    paths the impact already carries to the shared dispatch seam, which mints a CANDIDATE
    execution (``touches_published_pointer=False``). An invalid candidate stays UNPUBLISHED
    (the bounded_recovery invariant, unchanged by 40.4). ``commit=False``: the caller owns the
    transaction so the dispatch commits atomically with the audit + status update."""
    if not backfill_actions:
        return
    from core import bounded_recovery  # noqa: F401, PLC0415 -- REUSE the Epic 12 candidate contract

    dispatch = getattr(bounded_recovery, "dispatch_backfill_candidate", None)
    if dispatch is None:
        # The concrete dispatch entrypoint is wired in 40.5 (governed MCP/REST surface); the
        # contract paths + candidate-only flags are already carried on each action. When the
        # seam is absent we assert the candidate-only invariant and defer the live dispatch to
        # the 40.5 surface -- NEVER a bespoke pull here (E40-NFR05).
        for action in backfill_actions:
            if action.get("touches_published_pointer") is not False or not action.get(
                "candidate_only"
            ):
                raise EntityScopeChangeError(
                    "backfill action is not candidate-only (would move the live pointer)"
                )
        return
    for action in backfill_actions:
        dispatch(
            conn,
            project_id=project_id,
            action=action,
            identity=identity,
            commit=False,
        )


def _audit_blocked(
    conn: object,
    preview: Mapping[str, Any],
    identity: str,
    backfill_decision: str,
    fresh_impact: Mapping[str, Any],
) -> None:
    """Audit a refused (blocking-gap) confirmation on the caller-owned transaction."""
    from core.audit import insert_audit_row  # noqa: PLC0415

    insert_audit_row(
        conn,
        identity=identity,
        action=ACTION_ENTITY_SCOPE_CHANGE_BLOCKED,
        provider_account="",
        connection_ref="",
        metadata={
            "preview_id": preview.get("id"),
            "project_id": preview.get("project_id"),
            "entity_id": preview.get("entity_id"),
            "backfill_decision": backfill_decision,
            "blocking_gap_count": fresh_impact.get("blocking_gap_count"),
            "reason": "blocking_add_scope_gaps",
        },
    )
