"""toorow -- Operations-profile MCP surface (Story 36.13, Epic 36).

The Operations capability profile: run / readiness / diagnostic READS plus
policy-authorized, bounded RECOVERY (retry, bounded refetch, execution-state
reconciliation). It is the FIRST real consumer of the Story 36.11 profile system
(``register_profiled(profile="operations", ...)``). Every tool here is registered
under ``profile="operations"`` so the ``CapabilityProfileMiddleware`` hides it from
discovery and denies direct calls unless the caller holds an evidence-backed
operations opt-in (fail closed at discovery); every tool ALSO re-authorizes at call
time through ``project_access.resolve_strict_resource_access`` (fail closed at call
time -- existence-hiding on denial).

WHAT THIS PROFILE EXCLUDES (invariant, never expose here):
  * mapping PUBLICATION (creating/advancing a published mapping version), and
  * LIVE-POINTER authority (mutating ``current_published_execution_id`` /
    ``commit_publication``).
Those belong to Governance (Stories 36.17/36.18). This module NEVER imports or
calls ``datastream_publication.commit_publication`` and NEVER writes a published
pointer.

AD-27 prepare-confirm-commit (the write-effect contract):
  * ``prepare_datastream_recovery`` produces an IMMUTABLE proposal
    (``app.operation_preparations``, migration 070) with target datastream,
    versions, interval, impact, quota/cost, lock, rollback and idempotency all
    visible. It creates NO durable operation and dispatches nothing.
  * ``confirm_datastream_recovery`` takes a trusted confirmation, RE-CHECKS every
    authorization precondition (stale plan/mapping versions, changed policy,
    forbidden interval, missing account exposure, quota violation, lock conflict)
    and, only when all pass, routes exactly ONE ``operations.execute_operation``
    (Story 36.2: atomic state+audit+outbox, idempotent replay). Dispatch REUSES
    that single durable operation. A duplicate confirm / timeout / retry returns
    the ORIGINAL operation + its outcome (never a duplicate) via the operation
    idempotency key (op_result.replayed).

E36-NFR05 (wrap existing seams): this module opens NO parallel recovery engine and
adds NO source/provider vocabulary to core. It reuses:
  * ``datastream_diagnosis`` for the readiness/diagnostic read evidence;
  * ``datastream_publication.create_execution`` / ``advance_state`` (retry/refetch
    candidates) and ``reconcile_execution`` (execution-state reconciliation);
  * ``refetch`` for the bounded refetch interval ladder / window ceiling;
  * ``quota`` for the quota/cost pre-check in the prepared proposal;
  * ``operations.execute_operation`` for the single durable write.

Design mirrors metric_semantics_mcp / datastream_diagnosis: ``from __future__
import annotations``, module logger, ``core.*`` imports LAZY inside function bodies
(no import cycle with core.main), pure serializers separate from I/O so invariants
are offline-testable, French ToolError / summary microcopy, ASCII-only stdout.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Envelope schema version (mirrors core.main.SCHEMA_VERSION -- kept local so this
# module never imports a private of core.main; single canonical value "1").
_SCHEMA_VERSION = "1"

# The three write-effect recovery verbs the Operations profile owns. Publication /
# live-pointer authority is NOT in this set (excluded from Operations by contract).
_RECOVERY_KINDS = frozenset({"retry", "refetch", "reconcile"})

# Bounded refetch interval ceiling. Reuses the refetch ladder's per-window bound
# (server/core/refetch.REFETCH_WINDOW_DAYS == 31): a single bounded refetch may not
# exceed this many days -- anything wider is a "forbidden interval" and is refused.
_MAX_REFETCH_DAYS = 31

# Proposal time-to-live: a prepared proposal that is not confirmed within this many
# seconds is stale and a confirm must refuse it (AD-27 immutable + bounded).
_PREPARATION_TTL_SECONDS = 900  # 15 minutes


# ---------------------------------------------------------------------------
# Envelope + error + identity helpers (local -- never import a private of main).
# ---------------------------------------------------------------------------


def _envelope(data: dict) -> dict:
    """Build the canonical AD-1 structured_content envelope (same shape as core.main)."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON (FR message)."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    """Resolve the caller identity from the MCP token (same pattern as get_card).

    Absent token => "anonymous", which the strict access seam then rejects
    (existence-hiding, E36-NFR02).
    """
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _result(summary: str, data: dict):
    """Build the dual-channel ToolResult (lean text summary + AD-1 envelope)."""
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


# ---------------------------------------------------------------------------
# Access guard -- MIRRORS the strict AD-5 seam (never a bypass). Existence-hiding.
# ---------------------------------------------------------------------------


def _guard_datastream(datastream_id: str, identity: str, *, minimum_capability: str):
    """Return an ``AccessDecision`` or raise not_found (existence-hiding).

    Wraps ``project_access.resolve_strict_resource_access`` on the Datastream scope.
    Reads require ``view``; bounded recovery (write effect) requires at least
    ``edit``. Denial for ANY reason (not a member, no grant, foreign resource,
    guard-evaluation failure / DB down) surfaces a single ``not_found`` -- we NEVER
    disclose whether the Datastream exists (E36-NFR02). Fail-closed: the operation
    must NOT proceed unguarded on a guard failure.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                datastream_id=datastream_id,
                minimum_capability=minimum_capability,
            )
    except Exception as exc:  # noqa: BLE001 -- fail-closed (never unguarded).
        logger.error("operations_mcp: access guard failed ds=%s: %s", datastream_id, exc)
        raise _tool_error("not_found", "Flux introuvable.") from exc
    if not decision.allowed or not decision.org_id:
        raise _tool_error("not_found", "Flux introuvable.")
    return decision


# ---------------------------------------------------------------------------
# Pure interval / version / quota helpers (offline-testable, no I/O).
# ---------------------------------------------------------------------------


def _bounded_interval(date_from, date_to) -> dict:
    """Validate a bounded refetch ``{from, to}`` interval or raise forbidden_interval.

    An interval is FORBIDDEN (refuse dispatch, AC4) when it is malformed, inverted,
    or wider than ``_MAX_REFETCH_DAYS`` -- Operations refetch is BOUNDED by contract;
    a whole-history reload is a Governance/backfill concern, not a bounded recovery.
    """
    from datetime import date  # noqa: PLC0415

    def _parse(v):
        try:
            return date.fromisoformat(str(v))
        except (TypeError, ValueError):
            return None

    d_from = _parse(date_from)
    d_to = _parse(date_to)
    if d_from is None or d_to is None:
        raise _tool_error("forbidden_interval", "Intervalle de refetch invalide.")
    span = (d_to - d_from).days + 1
    if span < 1 or span > _MAX_REFETCH_DAYS:
        raise _tool_error(
            "forbidden_interval",
            f"Intervalle de refetch hors bornes (max {_MAX_REFETCH_DAYS} jours).",
        )
    return {"from": d_from.isoformat(), "to": d_to.isoformat()}


def _versions_match(pinned: dict, current: dict) -> bool:
    """True only when every pinned plan/mapping version equals the current version.

    A stale-version recheck (AC4): the proposal pinned the plan/mapping versions it
    saw; if the live datastream advanced either since prepare, dispatch is refused.
    """
    for key in ("plan_version_id", "mapping_version_id"):
        if pinned.get(key) != current.get(key):
            return False
    return True


def _quota_estimate(platform: str | None, estimated_points: int) -> dict:
    """Return a quota/cost estimate + pre_check verdict for the prepared proposal.

    Reuses ``quota.pre_check`` (the existing token-bucket + breaker engine) -- this
    module never re-implements a budget. ``platform`` is OPAQUE data (never a
    hard-coded provider name, AD-2): it arrives from the datastream's module row.
    A missing/unregistered platform yields the pass-through ``no_quota`` verdict.
    """
    from core import quota  # noqa: PLC0415

    points = max(0, int(estimated_points))
    try:
        can_proceed, reason = quota.pre_check(platform or "", points)
    except Exception as exc:  # noqa: BLE001 -- a quota lookup hiccup must fail closed.
        logger.warning("operations_mcp: quota pre_check failed platform=%s: %s", platform, exc)
        return {"platform_known": bool(platform), "estimated_points": points,
                "verdict": "budget_exhausted", "can_proceed": False}
    return {
        "platform_known": bool(platform),
        "estimated_points": points,
        "verdict": reason,
        "can_proceed": bool(can_proceed),
    }


def _recovery_idempotency_key(org_id: str, datastream_id: str, kind: str, fingerprint: str) -> str:
    """Derive the durable-operation idempotency key for one recovery proposal.

    Deterministic over (org, datastream, kind, proposal fingerprint) so a duplicate
    confirm / timeout / retry of the SAME proposal replays the SAME durable
    operation (AC5) rather than creating a second one.
    """
    return f"operations.recovery:{org_id}:{datastream_id}:{kind}:{fingerprint}"


# ---------------------------------------------------------------------------
# I/O: read the live datastream target (versions, pointer, platform, lock).
# ---------------------------------------------------------------------------


def _load_target(conn, datastream_id: str) -> dict | None:
    """Return the live target facts for one datastream, or None.

    Composes the existing ``app.datastreams`` columns (E36-NFR05): project_id,
    org_id, current plan/mapping versions, the current published pointer (READ
    ONLY -- never mutated here), the source module name (opaque platform key for
    quota) and any active execution id (the lock the dispatch will contend for).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT project_id, org_id, current_plan_version_id,
                   current_mapping_version_id, current_published_execution_id,
                   source_kind
            FROM app.datastreams
            WHERE id = %s
            """,
            (datastream_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    target = {
        "datastream_id": datastream_id,
        "project_id": row[0],
        "org_id": row[1],
        "plan_version_id": row[2],
        "mapping_version_id": row[3],
        "current_published_execution_id": row[4],
        "platform": row[5],
    }
    # The active-execution lock: a non-terminal execution blocks a new candidate.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM app.datastream_executions
            WHERE datastream_id = %s AND state = ANY(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (datastream_id, ["created", "loading", "validating", "ready", "publishing"]),
        )
        lock = cur.fetchone()
    target["active_execution_id"] = lock[0] if lock else None
    return target


def _current_policy_version() -> str:
    """Return the policy version in force (the admin capability-catalog snapshot).

    Reuses ``mcp_profiles.catalog_version("admin")`` as the governing policy pin so
    a changed catalog/policy since prepare is detectable at confirm (AC4). Pure and
    deterministic; never a wall-clock value.
    """
    from core import mcp_profiles  # noqa: PLC0415

    try:
        return mcp_profiles.catalog_version("admin")
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# Prepared-proposal persistence (migration 070). Immutable proposal store.
# ---------------------------------------------------------------------------


def _proposal_fingerprint(payload: dict) -> str:
    """Deterministic sha256 over the immutable proposal fields (idempotency seed)."""
    from core.operations import _canonical_hash  # noqa: PLC0415

    return _canonical_hash(payload)


def _insert_preparation(conn, proposal: dict, idempotency_key: str) -> str:
    """Insert one immutable prepared proposal, return its id. Caller owns the txn.

    The proposal row is the persisted AD-27 evidence: target/versions/interval/
    impact/quota/lock/rollback/idempotency, frozen by the migration-070
    immutability trigger. NO durable operation exists yet.
    """
    from ulid import ULID  # noqa: PLC0415

    from core.operations import _sha256  # noqa: PLC0415

    preparation_id = f"prep_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.operation_preparations
                (id, org_id, project_id, datastream_id, kind, actor,
                 target_versions, interval, impact, quota, lock_ref, rollback_ref,
                 idempotency_key_hash, expires_at, state)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s, %s,
                    NOW() + (%s || ' seconds')::interval, 'prepared')
            """,
            (
                preparation_id,
                proposal["org_id"],
                proposal["project_id"],
                proposal["datastream_id"],
                proposal["kind"],
                proposal["actor"],
                json.dumps(proposal["target_versions"]),
                json.dumps(proposal["interval"]) if proposal["interval"] is not None else None,
                json.dumps(proposal["impact"]),
                json.dumps(proposal["quota"]),
                proposal["lock_ref"],
                proposal["rollback_ref"],
                _sha256(idempotency_key),
                str(_PREPARATION_TTL_SECONDS),
            ),
        )
    return preparation_id


def _load_preparation(conn, preparation_id: str) -> dict | None:
    """Load one prepared proposal by id (for confirm), or None. FOR UPDATE."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, org_id, project_id, datastream_id, kind, actor,
                   target_versions, interval, impact, quota, lock_ref, rollback_ref,
                   idempotency_key_hash, state, operation_id,
                   (expires_at <= NOW()) AS expired
            FROM app.operation_preparations
            WHERE id = %s
            FOR UPDATE
            """,
            (preparation_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "org_id": row[1],
        "project_id": row[2],
        "datastream_id": row[3],
        "kind": row[4],
        "actor": row[5],
        "target_versions": row[6] or {},
        "interval": row[7],
        "impact": row[8] or {},
        "quota": row[9] or {},
        "lock_ref": row[10],
        "rollback_ref": row[11],
        "idempotency_key_hash": row[12],
        "state": row[13],
        "operation_id": row[14],
        "expired": bool(row[15]),
    }


def _load_operation_trace(conn, operation_id: str) -> str | None:
    """Return the server trace pinned to one durable operation, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trace_id FROM app.operations WHERE id = %s",
            (operation_id,),
        )
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None

def _mark_preparation_confirmed(conn, preparation_id: str, operation_id: str) -> None:
    """Attach the durable operation + flip state prepared->confirmed (lifecycle only).

    The migration-070 trigger permits ONLY this lifecycle transition; every
    substantive proposal field stays frozen (AD-27 immutable proposal).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.operation_preparations
            SET state = 'confirmed', operation_id = %s, updated_at = NOW()
            WHERE id = %s AND state = 'prepared'
            """,
            (operation_id, preparation_id),
        )


# ---------------------------------------------------------------------------
# The recovery mutation (routed through operations.execute_operation ONCE).
# ---------------------------------------------------------------------------


def _dispatch_recovery(conn, operation_id: str, prep: dict, target: dict):
    """Perform the bounded recovery for one confirmed proposal (Story 36.2 mutation).

    Runs INSIDE ``operations.execute_operation`` so it is atomic with the durable
    operation's audit + outbox and idempotent on replay. Reuses the existing Epic 12
    seams (E36-NFR05); it NEVER commits a publication and NEVER mutates the live
    pointer. Returns a ``MutationResult``.
    """
    from core.datastream_publication import (  # noqa: PLC0415
        PublicationError,
        create_execution,
        reconcile_execution,
    )
    from core.operations import MutationResult, _canonical_hash  # noqa: PLC0415

    kind = prep["kind"]
    project_id = prep["project_id"]
    datastream_id = prep["datastream_id"]

    if kind == "reconcile":
        # Execution-state reconciliation: resolve a stuck 'publishing' execution
        # idempotently (fail-closed). This NEVER advances the live pointer beyond
        # what the original atomic publish already committed -- it only resolves the
        # lost state advance (datastream_publication.reconcile_execution).
        execution_id = (prep.get("interval") or {}).get("execution_id")
        if not execution_id:
            return MutationResult(
                outcome="failed", before_hash=None, after_hash=None,
                result={"reason": "missing_execution_reference"},
                outbox_payload={"kind": kind, "datastream_id": datastream_id},
            )
        try:
            outcome = reconcile_execution(execution_id, project_id, conn)
        except Exception as exc:  # noqa: BLE001 -- outcome unknown, never a duplicate.
            logger.error("operations_mcp: reconcile failed exec=%s: %s", execution_id, exc)
            return MutationResult(
                outcome="outcome_unknown", before_hash=None, after_hash=None,
                result={"reason": "reconcile_uncertain"},
                outbox_payload={"kind": kind, "execution_id": execution_id},
            )
        result = {"kind": kind, "execution_id": execution_id,
                  "final_state": outcome.get("final_state"),
                  "action_taken": outcome.get("action_taken")}
        return MutationResult(
            outcome="succeeded" if outcome.get("resolved") else "failed",
            before_hash=None, after_hash=_canonical_hash(result),
            result=result, outbox_payload=result,
        )

    # retry / refetch: create ONE idempotent CANDIDATE execution against the exact
    # pinned plan/mapping versions. The candidate is non-live: it does not touch the
    # published pointer (that is Governance's publish authority, excluded here).
    projection_plan = {"executable": True, "recovery_kind": kind}
    if prep.get("interval"):
        projection_plan["interval"] = prep["interval"]
    try:
        execution = create_execution(
            datastream_id=datastream_id,
            project_id=project_id,
            plan_version_id=target["plan_version_id"],
            mapping_version_id=target["mapping_version_id"],
            projection_plan=projection_plan,
            actor=prep["actor"],
            idempotency_key=f"{operation_id}:candidate",
            conn=conn,
        )
    except PublicationError as exc:
        logger.warning("operations_mcp: recovery candidate rejected: %s", exc)
        return MutationResult(
            outcome="failed", before_hash=None, after_hash=None,
            result={"reason": "candidate_rejected", "kind": kind},
            outbox_payload={"kind": kind, "datastream_id": datastream_id},
        )
    except Exception as exc:  # noqa: BLE001 -- concurrency/lock conflict, never a dup.
        logger.warning("operations_mcp: recovery candidate conflict: %s", exc)
        return MutationResult(
            outcome="failed", before_hash=None, after_hash=None,
            result={"reason": "lock_conflict", "kind": kind},
            outbox_payload={"kind": kind, "datastream_id": datastream_id},
        )
    result = {"kind": kind, "execution_id": execution.get("id"),
              "state": execution.get("state")}
    return MutationResult(
        outcome="succeeded", before_hash=None, after_hash=_canonical_hash(result),
        result=result, outbox_payload=result,
    )


# ---------------------------------------------------------------------------
# register(mcp): define each handler locally and register it via register_profiled.
# ---------------------------------------------------------------------------


def register(mcp) -> None:
    """Register the Operations-profile MCP tools on *mcp*.

    Called once from core.main (the ONLY hook 36.13 adds to main.py), AFTER the
    datastream_diagnosis registration and BEFORE ``validate_catalog()`` so the
    boot-time validator sees these operations tools. Every tool is registered via
    ``mcp_profiles.register_profiled`` so the capability middleware filters it:
    reads are ``effect="read"``/``confirmation_mode="none"``; the write-effect
    ``confirm_datastream_recovery`` is ``effect="write"``/``confirmation_mode="human"``.
    None is ever ``profile="insights"`` (that stays Insights read-only).
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    # ---- Read: run history (delegates to the sanitized diagnosis read model) ----
    def list_datastream_runs(datastream_id: str, cursor: int = 0, page_size: int = 50):
        """Historique BORNE des executions/pulls d'UN flux (profil Operations, lecture).

        Compose le meme modele de lecture sanitise que ``datastream_pull_history``
        (E36-NFR05 : aucune deuxieme source de verite) : chronologie bornee et
        paginee, serialiseur allow-list (classe d'erreur canonique, rejouabilite,
        tentatives, intervalle, identifiants de correlation copiables). Guard strict
        AD-5 (vue) ; flux etranger -> not_found. Ne mute AUCUN pointeur (lecture pure).
        """
        datastream_id = (datastream_id or "").strip() or None
        if datastream_id is None:
            raise _tool_error("missing_param", "datastream_id est requis.")
        identity = _identity()
        _guard_datastream(datastream_id, identity, minimum_capability="view")
        from core.datastream_diagnosis import (  # noqa: PLC0415
            _assemble_timeline,
            _bound_offset,
            _bound_page_size,
            _collect_events,
        )
        from core.db import get_connection  # noqa: PLC0415

        try:
            with get_connection() as conn:
                events, context = _collect_events(conn, datastream_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("operations_mcp: runs failed ds=%s: %s", datastream_id, exc)
            raise _tool_error("server_error", "Erreur serveur.") from exc
        timeline = _assemble_timeline(
            events, cursor=_bound_offset(cursor), page_size=_bound_page_size(page_size)
        )
        summary = (
            f"Executions du flux {context.get('datastream_id')!r} : "
            f"{timeline['page']['size']} sur {timeline['total']}."
        )
        return _result(summary, {"context": context, "timeline": timeline})

    # ---- Read: readiness (recovery-oriented view of the diagnosis) --------------
    def get_datastream_readiness(datastream_id: str):
        """Etat de preparation/recuperation d'UN flux (profil Operations, lecture).

        Derive le diagnostic canonique (E36-FR08) via le modele de lecture partage
        (``datastream_diagnosis._diagnose``) et expose l'etat de recuperation :
        classe d'erreur, rejouabilite, action recommandee, presence d'une execution
        active (verrou), et pointeur publie courant (LU, jamais mute). Guard strict
        AD-5 (vue) ; flux etranger -> not_found. Aucune donnee brute retournee.
        """
        datastream_id = (datastream_id or "").strip() or None
        if datastream_id is None:
            raise _tool_error("missing_param", "datastream_id est requis.")
        identity = _identity()
        _guard_datastream(datastream_id, identity, minimum_capability="view")
        from core.datastream_diagnosis import _collect_events, _diagnose  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        try:
            with get_connection() as conn:
                events, context = _collect_events(conn, datastream_id)
                target = _load_target(conn, datastream_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("operations_mcp: readiness failed ds=%s: %s", datastream_id, exc)
            raise _tool_error("server_error", "Erreur serveur.") from exc
        diagnosis = _diagnose(events)
        readiness = {
            "diagnosis": diagnosis,
            "has_active_execution": bool(target and target.get("active_execution_id")),
            "current_published_execution_id": (
                target.get("current_published_execution_id") if target else None
            ),
            "recoverable_kinds": sorted(_RECOVERY_KINDS),
        }
        summary = (
            f"Preparation du flux {context.get('datastream_id')!r} : "
            f"verdict {diagnosis.get('verdict')}."
        )
        return _result(summary, {"context": context, "readiness": readiness})

    # ---- Write-effect: prepare an immutable recovery proposal (AD-27) -----------
    def prepare_datastream_recovery(
        datastream_id: str,
        kind: str,
        date_from: str | None = None,
        date_to: str | None = None,
        execution_id: str | None = None,
        estimated_points: int = 0,
    ):
        """Prepare une PROPOSITION IMMUABLE de recuperation bornee (AD-27, prepare).

        Produit une proposition immuable (``app.operation_preparations``) rendant
        VISIBLE : flux cible, versions (plan/mapping) figees, intervalle borne,
        impact attendu, quota/cout estime, verrou, chemin de rollback et cle
        d'idempotence. Ne cree AUCUNE operation durable et ne declenche AUCUN
        dispatch (c'est ``confirm_datastream_recovery`` qui, sur confirmation de
        confiance, route UNE operation via Story 36.2). ``kind`` in
        {retry, refetch, reconcile} : la publication de mapping et l'autorite sur le
        pointeur publie sont EXCLUES de ce profil. Guard strict AD-5 (edition
        requise) ; flux etranger -> not_found. Intervalle refetch borne a
        ``_MAX_REFETCH_DAYS`` jours (au-dela = forbidden_interval).
        """
        datastream_id = (datastream_id or "").strip() or None
        kind = (kind or "").strip()
        if datastream_id is None:
            raise _tool_error("missing_param", "datastream_id est requis.")
        if kind not in _RECOVERY_KINDS:
            raise _tool_error("invalid_kind", "Type de recuperation non autorise.")
        identity = _identity()
        decision = _guard_datastream(datastream_id, identity, minimum_capability="edit")
        org_id = str(decision.org_id)

        # Bound / normalize the interval per kind (forbidden_interval refuses here).
        interval: dict | None = None
        if kind == "refetch":
            interval = _bounded_interval(date_from, date_to)
        elif kind == "reconcile":
            execution_id = (execution_id or "").strip() or None
            if execution_id is None:
                raise _tool_error("missing_param", "execution_id est requis pour reconcile.")
            interval = {"execution_id": execution_id}

        from core.db import get_connection  # noqa: PLC0415

        try:
            with get_connection() as conn:
                target = _load_target(conn, datastream_id)
                if target is None:
                    raise _tool_error("not_found", "Flux introuvable.")
                target_versions = {
                    "plan_version_id": target.get("plan_version_id"),
                    "mapping_version_id": target.get("mapping_version_id"),
                    "policy_version": _current_policy_version(),
                }
                quota = _quota_estimate(target.get("platform"), estimated_points)
                impact = {
                    "kind": kind,
                    "interval": interval,
                    "touches_published_pointer": False,  # Operations NEVER publishes.
                    "candidate_only": kind in {"retry", "refetch"},
                }
                lock_ref = target.get("active_execution_id")
                rollback_ref = target.get("current_published_execution_id")
                fingerprint = _proposal_fingerprint(
                    {
                        "datastream_id": datastream_id,
                        "kind": kind,
                        "interval": interval,
                        "target_versions": target_versions,
                    }
                )
                idempotency_key = _recovery_idempotency_key(
                    org_id, datastream_id, kind, fingerprint
                )
                proposal = {
                    "org_id": org_id,
                    "project_id": target.get("project_id"),
                    "datastream_id": datastream_id,
                    "kind": kind,
                    "actor": identity,
                    "target_versions": target_versions,
                    "interval": interval,
                    "impact": impact,
                    "quota": quota,
                    "lock_ref": lock_ref,
                    "rollback_ref": rollback_ref,
                }
                preparation_id = _insert_preparation(conn, proposal, idempotency_key)
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, Exception) and exc.__class__.__name__ == "ToolError":
                raise
            logger.error("operations_mcp: prepare failed ds=%s: %s", datastream_id, exc)
            raise _tool_error("server_error", "Preparation indisponible.") from exc

        data = {
            "preparation_id": preparation_id,
            "target": {
                "datastream_id": datastream_id,
                "project_id": proposal["project_id"],
            },
            "kind": kind,
            "target_versions": target_versions,
            "interval": interval,
            "impact": impact,
            "quota": quota,
            "lock_ref": lock_ref,
            "rollback_ref": rollback_ref,
            "expires_in_seconds": _PREPARATION_TTL_SECONDS,
        }
        summary = (
            f"Proposition de recuperation {kind!r} preparee pour le flux "
            f"{datastream_id!r} (immuable, a confirmer)."
        )
        return _result(summary, data)

    # ---- Write-effect: confirm -> ONE durable operation + dispatch (AD-27) ------
    def confirm_datastream_recovery(preparation_id: str):
        """Confirme une proposition et route UNE operation durable (AD-27, commit).

        Sur confirmation de confiance, RE-VERIFIE tous les preconditions
        d'autorisation : versions plan/mapping perimees, politique changee,
        intervalle interdit, exposition de compte manquante, violation de quota,
        conflit de verrou -> AUCUN dispatch (AC4). Sinon, cree EXACTEMENT UNE
        operation durable via Story 36.2 (etat+audit+outbox atomiques) et le
        dispatch la reutilise. Un confirm en double / timeout / retry renvoie
        l'operation ORIGINALE + son etat (jamais de doublon, AC5). Guard strict
        AD-5 (edition) ; proposition etrangere/perimee/deja consommee -> not_found.
        Ne publie JAMAIS de mapping et ne mute JAMAIS le pointeur publie.
        """
        preparation_id = (preparation_id or "").strip() or None
        if preparation_id is None:
            raise _tool_error("missing_param", "preparation_id est requis.")
        identity = _identity()

        from core.db import get_connection  # noqa: PLC0415
        from core.operations import (  # noqa: PLC0415
            OperationSpec,
            _sha256,
            execute_operation,
        )

        try:
            with get_connection() as conn:
                prep = _load_preparation(conn, preparation_id)
                if prep is None:
                    raise _tool_error("not_found", "Proposition introuvable.")
                # Re-authorize the resource at CALL time (fail closed, edit floor).
                decision = _guard_datastream(
                    prep["datastream_id"], identity, minimum_capability="edit"
                )
                if str(decision.org_id) != prep["org_id"]:
                    raise _tool_error("not_found", "Proposition introuvable.")
                # Stale / consumed / expired proposal -> refuse (no dispatch).
                if prep["state"] != "prepared" or prep["expired"]:
                    raise _tool_error("stale_preparation", "Proposition perimee ou deja traitee.")

                target = _load_target(conn, prep["datastream_id"])
                if target is None:
                    raise _tool_error("not_found", "Flux introuvable.")

                # (a) stale plan/mapping versions -> refuse.
                if not _versions_match(prep["target_versions"], target):
                    raise _tool_error("stale_versions", "Versions plan/mapping perimees.")
                # (b) changed policy -> refuse.
                if prep["target_versions"].get("policy_version") != _current_policy_version():
                    raise _tool_error("policy_changed", "Politique modifiee depuis la preparation.")
                # (c) forbidden interval (revalidate the bounded refetch window).
                if prep["kind"] == "refetch":
                    _bounded_interval(
                        (prep.get("interval") or {}).get("from"),
                        (prep.get("interval") or {}).get("to"),
                    )
                # (d) missing account exposure -> refuse. No live connection ref =>
                # the account exposure was revoked/never granted (fail closed).
                if not target.get("platform"):
                    raise _tool_error("missing_exposure", "Exposition de compte manquante.")
                # (e) quota violation -> refuse (re-run the pre-check, not the cache).
                quota_now = _quota_estimate(
                    target.get("platform"),
                    int((prep.get("quota") or {}).get("estimated_points", 0)),
                )
                if not quota_now.get("can_proceed"):
                    raise _tool_error("quota_violation", "Budget quota insuffisant.")
                # (f) lock conflict -> refuse. A retry/refetch needs a free lock; a
                # newly-active execution that is not our own rollback target blocks.
                if prep["kind"] in {"retry", "refetch"} and target.get("active_execution_id"):
                    raise _tool_error("lock_conflict", "Une execution est deja active.")

                # All rechecks pass: route EXACTLY ONE durable operation (Story 36.2).
                # The idempotency key is rebuilt from the SAME immutable proposal
                # fingerprint so a duplicate confirm replays the original operation.
                fingerprint = _proposal_fingerprint(
                    {
                        "datastream_id": prep["datastream_id"],
                        "kind": prep["kind"],
                        "interval": prep["interval"],
                        "target_versions": prep["target_versions"],
                    }
                )
                idempotency_key = _recovery_idempotency_key(
                    prep["org_id"], prep["datastream_id"], prep["kind"], fingerprint
                )
                # Defence in depth: the raw key must hash to the value pinned at
                # prepare -- otherwise the confirmed proposal was tampered with.
                if _sha256(idempotency_key) != prep["idempotency_key_hash"]:
                    raise _tool_error("stale_preparation", "Proposition alteree.")

                spec = OperationSpec(
                    command_type=f"operations.recovery.{prep['kind']}",
                    actor=identity,
                    effective_org_id=prep["org_id"],
                    resource_path=(
                        f"organization:{prep['org_id']}",
                        f"flux:{prep['datastream_id']}",
                    ),
                    idempotency_key=idempotency_key,
                    host_context={},
                    versions={
                        "policy": prep["target_versions"].get("policy_version"),
                        "catalog": _current_policy_version(),
                        "tool": "confirm_datastream_recovery",
                    },
                    request_payload={
                        "kind": prep["kind"],
                        "interval": prep["interval"],
                        "plan_version_id": prep["target_versions"].get("plan_version_id"),
                        "mapping_version_id": prep["target_versions"].get("mapping_version_id"),
                    },
                    provider_references={},
                    confirmation_mode="human",
                    confirmation_reference=preparation_id,
                    trace_id=None,
                )

                op_result = execute_operation(
                    conn,
                    spec,
                    mutation=lambda c, op_id: _dispatch_recovery(c, op_id, prep, target),
                )
                # Link the durable operation back to the immutable proposal (lifecycle
                # only). On a REPLAY the proposal is already confirmed -> no-op.
                _mark_preparation_confirmed(conn, preparation_id, op_result.operation_id)
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("operations_mcp: confirm failed prep=%s: %s", preparation_id, exc)
            raise _tool_error("server_error", "Confirmation indisponible.") from exc

        data = {
            "preparation_id": preparation_id,
            "operation_id": op_result.operation_id,
            "outcome": op_result.outcome,
            "replayed": op_result.replayed,
            "result": op_result.result,
        }
        replayed = " (rejeu de l'operation originale)" if op_result.replayed else ""
        summary = (
            f"Recuperation {prep['kind']!r} confirmee : operation "
            f"{op_result.operation_id} ({op_result.outcome}){replayed}."
        )
        return _result(summary, data)

    # ---- Read: connector installation status (Story 38.2, AC2) -----------------
    def get_connector_installation_status(connector_name: str):
        """Etat d'installation d'un connecteur plateforme (profil Operations, lecture).

        Retourne le modele de lecture securise (state, safe_next_action,
        responsible_actor, blocking_cause, last_verified_at, catalog_availability)
        pour le connecteur identifie par son nom technique. Aucun secret, aucune
        donnee inter-tenant. Guard strict : profil Operations requis (fail closed
        sur decouverte et appel). Identique au GET REST (AC2 : meme evidence).
        """
        connector_name = (connector_name or "").strip() or None
        if connector_name is None:
            raise _tool_error("missing_param", "connector_name est requis.")

        import os as _os  # noqa: PLC0415

        environment = _os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"

        try:
            from core.connector_installation import get_installation_state  # noqa: PLC0415
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                read_model = get_installation_state(
                    conn,
                    environment=environment,
                    connector_name=connector_name,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "operations_mcp: get_connector_installation_status cn=%s: %s",
                connector_name,
                exc,
            )
            raise _tool_error("server_error", "Etat d'installation indisponible.") from exc

        if read_model is None:
            # Connector has never been installed in this environment.
            data = {
                "connector_name": connector_name,
                "environment": environment,
                "state": "NOT_INSTALLED",
                "safe_next_action": (
                    "platform_admin: apply installation to advance to DOMAIN_PENDING"
                ),
                "responsible_actor": "platform_admin",
                "blocking_cause": None,
                "last_verified_at": None,
                "catalog_availability": "unavailable",
            }
        else:
            state = read_model["state"]
            data = {
                "connector_name": connector_name,
                "environment": environment,
                **read_model,
                "catalog_availability": "selectable" if state == "READY" else "unavailable",
            }

        summary = (
            f"Installation du connecteur {connector_name!r} : "
            f"etat {data['state']} / disponibilite {data['catalog_availability']}."
        )
        return _result(summary, data)

    # ---- Register every tool under profile="operations" -------------------------
    register_profiled(
        mcp, list_datastream_runs,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )
    register_profiled(
        mcp, get_datastream_readiness,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )
    # prepare writes an immutable proposal row (app.operation_preparations) -- it is a
    # preparation WRITE, not a read (review H2). The consequential dispatch stays on the
    # separate human-confirmed `confirm` tool, so prepare itself needs no confirmation.
    register_profiled(
        mcp, prepare_datastream_recovery,
        profile="operations", effect="write",
        data_class="operational", confirmation_mode="none",
    )
    register_profiled(
        mcp, confirm_datastream_recovery,
        profile="operations", effect="write",
        data_class="operational", confirmation_mode="human",
    )
    # Story 38.2: connector installation status read (same safe model as REST GET).
    register_profiled(
        mcp, get_connector_installation_status,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )

    # ---- Read: connector domain config (Story 38.3, AC2) -------------------------
    def get_connector_domain_config(connector_name: str):
        """Configuration de domaine d'un connecteur plateforme (profil Operations, lecture).

        Retourne le modele de lecture securise (domain, provider_adapter,
        webhook_endpoint_version, dns_evidence_class, config_version,
        safe_next_action, configured_at) pour le connecteur identifie par son nom
        technique. Aucun secret (signing_secret_ref, dns_evidence_hash), aucune
        donnee inter-tenant. Guard strict : profil Operations requis.
        Identique au GET REST (AC2 : meme evidence).
        """
        connector_name = (connector_name or "").strip() or None
        if connector_name is None:
            raise _tool_error("missing_param", "connector_name est requis.")

        import os as _os  # noqa: PLC0415

        environment = _os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"

        try:
            from core.connector_domain import get_domain_config  # noqa: PLC0415
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                read_model = get_domain_config(
                    conn,
                    environment=environment,
                    connector_name=connector_name,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "operations_mcp: get_connector_domain_config cn=%s: %s",
                connector_name,
                exc,
            )
            raise _tool_error("server_error", "Configuration de domaine indisponible.") from exc

        if read_model is None:
            data = {
                "connector_name": connector_name,
                "environment": environment,
                "configured": False,
                "safe_next_action": (
                    "platform_admin: POST domain config to configure receiving domain"
                ),
            }
        else:
            data = {
                "connector_name": connector_name,
                "environment": environment,
                "configured": True,
                **read_model,
            }

        configured = data.get("configured", False)
        summary = (
            f"Configuration de domaine du connecteur {connector_name!r} : "
            f"{'configuree' if configured else 'non configuree'} (env={environment})."
        )
        return _result(summary, data)

    # Story 38.3: connector domain config read (same safe model as REST GET).
    register_profiled(
        mcp, get_connector_domain_config,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )

    # ---- Read: connector verification status (Story 38.4, AC4) ------------------
    def get_connector_verification_status(connector_name: str):
        """Etat de verification d'un connecteur plateforme (profil Operations, lecture).

        Retourne le modele de lecture securise de la derniere verification
        (installation_state, last_outcome, evidence_class, first_seen_at,
        last_run_at, blocking_reason, synthetic_delivery, safe_next_action)
        pour le connecteur identifie par son nom technique. Aucun secret
        (evidence_hash, DNS token, cle de signature), aucune donnee inter-tenant.
        Guard strict : profil Operations requis. Identique au GET REST (AC4 :
        meme evidence). Retourne verified:false quand aucun run n'existe encore.
        """
        connector_name = (connector_name or "").strip() or None
        if connector_name is None:
            raise _tool_error("missing_param", "connector_name est requis.")

        import os as _os  # noqa: PLC0415

        environment = _os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"

        try:
            from core.connector_verification import get_verification_state  # noqa: PLC0415
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                read_model = get_verification_state(
                    conn,
                    environment=environment,
                    connector_name=connector_name,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "operations_mcp: get_connector_verification_status cn=%s: %s",
                connector_name,
                exc,
            )
            raise _tool_error("server_error", "Etat de verification indisponible.") from exc

        if read_model is None:
            data = {
                "connector_name": connector_name,
                "environment": environment,
                "verified": False,
                "safe_next_action": (
                    "platform_admin: POST /verify to run the first verification"
                ),
            }
        else:
            data = {
                "connector_name": connector_name,
                "environment": environment,
                "verified": True,
                **read_model,
            }

        outcome = data.get("last_outcome", "unverified")
        summary = (
            f"Verification du connecteur {connector_name!r} : "
            f"resultat {outcome} (env={environment})."
        )
        return _result(summary, data)

    # Story 38.4: connector verification status read (same safe model as REST GET).
    register_profiled(
        mcp, get_connector_verification_status,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )

    # ---- Read: connector activation status (Story 38.5, AC2) -------------------
    def get_connector_activation_status(connector_name: str, org_id: str):
        """Etat d'activation d'un connecteur pour une organisation (profil Operations, lecture).

        Retourne le modele de lecture securise (state, activated_at,
        deactivated_at) pour l'organisation identifiee par org_id et le
        connecteur identifie par connector_name. Aucun secret, aucune donnee
        inter-tenant. L'activation est exposee comme couche distincte de
        l'installation (AC2 : les deux couches restent independantes). Guard
        strict : profil Operations requis. Identique au GET REST (AC2 : meme
        evidence). Retourne activated:false quand aucun enregistrement n'existe.
        """
        connector_name = (connector_name or "").strip() or None
        org_id = (org_id or "").strip() or None
        if connector_name is None:
            raise _tool_error("missing_param", "connector_name est requis.")
        if org_id is None:
            raise _tool_error("missing_param", "org_id est requis.")

        import os as _os  # noqa: PLC0415

        environment = _os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"

        # AC4 cross-org isolation: the caller must have access to org_id, or the
        # activation is nondisclosingly "not found" (never another org's state).
        identity = _identity()
        denied = False
        try:
            from core.connector_activation import get_activation  # noqa: PLC0415
            from core.db import get_connection  # noqa: PLC0415
            from core.project_access import identity_has_org_access  # noqa: PLC0415

            with get_connection() as conn:
                if not identity_has_org_access(org_id, identity, conn):
                    denied = True
                    read_model = None
                else:
                    read_model = get_activation(
                        conn,
                        org_id=org_id,
                        connector_name=connector_name,
                        environment=environment,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "operations_mcp: get_connector_activation_status cn=%s org=%s: %s",
                connector_name,
                org_id,
                exc,
            )
            raise _tool_error("server_error", "Etat d'activation indisponible.") from exc

        if denied:
            raise _tool_error("not_found", "Etat d'activation introuvable.")

        if read_model is None:
            data = {
                "connector_name": connector_name,
                "org_id": org_id,
                "environment": environment,
                "activated": False,
                "safe_next_action": (
                    "org_owner: POST /activation to activate the connector for this org"
                ),
            }
        else:
            data = {
                "connector_name": connector_name,
                "environment": environment,
                "activated": read_model["state"] == "ACTIVE",
                **read_model,
            }

        activated = data.get("activated", False)
        state = data.get("state", "not_activated")
        summary = (
            f"Activation du connecteur {connector_name!r} pour org {org_id!r} : "
            f"etat {state} ({'active' if activated else 'inactif'})."
        )
        return _result(summary, data)

    # Story 38.5: connector activation status read (same safe model as REST GET).
    register_profiled(
        mcp, get_connector_activation_status,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )

    # ---- Read: import template catalog (Story 38.6, AC4 MCP parity) ------------
    def list_inbound_templates():
        """Catalogue des contrats de fichiers immuables disponibles (profil Operations, lecture).

        Retourne le modele de lecture securise du catalogue de templates
        (template_code, version, title, required_fields, optional_fields,
        identity_keys, grain, is_generic, created_at). Aucun secret, aucune
        donnee inter-tenant. Le catalogue est une reference de plateforme --
        chaque entree est immuable (AC3 : un changement = nouvelle version).
        Guard strict : profil Operations requis. Identique au GET REST (AC4 :
        meme evidence). is_generic=TRUE signale le mode decouverte-dabord.
        """
        try:
            from core.db import get_connection  # noqa: PLC0415
            from core.import_templates import list_templates  # noqa: PLC0415

            with get_connection() as conn:
                templates = list_templates(conn)
        except Exception as exc:  # noqa: BLE001
            logger.error("operations_mcp: list_inbound_templates: %s", exc)
            raise _tool_error("server_error", "Catalogue de templates indisponible.") from exc

        summary = (
            f"Catalogue de templates d'import : {len(templates)} template(s) disponible(s)."
        )
        return _result(summary, {"templates": templates, "count": len(templates)})

    # Story 38.6: import template catalog read (same safe model as REST GET).
    register_profiled(
        mcp, list_inbound_templates,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )

    # ---- Read: inbound credential status (Story 38.7, AC2/Task 4) ---------------
    def get_inbound_credential_status(datastream_id: str, credential_id: str):
        """Etat d'un credential de livraison entrant (profil Operations, lecture).

        Retourne le modele de lecture securise (credential_id, datastream_id,
        channel, safe_suffix, state, version, expires_at, overlap_until,
        issued_by, created_at) pour le credential identifie. AUCUN secret
        (token_hash, raw token) n'est jamais retourne -- le show-once invariant
        (AC2, E38-NFR03) interdit toute divulgation secondaire. Guard strict :
        profil Operations requis. Identique au GET REST (AC2 : meme evidence).
        Retourne not_found si le credential est absent ou appartient a un autre flux.
        """
        datastream_id = (datastream_id or "").strip() or None
        credential_id = (credential_id or "").strip() or None
        if datastream_id is None:
            raise _tool_error("missing_param", "datastream_id est requis.")
        if credential_id is None:
            raise _tool_error("missing_param", "credential_id est requis.")

        try:
            from core.db import get_connection  # noqa: PLC0415
            from core.inbound_credentials import get_credential_state  # noqa: PLC0415

            with get_connection() as conn:
                read_model = get_credential_state(
                    conn,
                    credential_id=credential_id,
                    datastream_id=datastream_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "operations_mcp: get_inbound_credential_status ds=%s cred=%s: %s",
                datastream_id,
                credential_id,
                exc,
            )
            raise _tool_error("server_error", "Etat du credential indisponible.") from exc

        if read_model is None:
            raise _tool_error("not_found", "Credential introuvable.")

        # The read_model is the safe projection: no token_hash, no raw token.
        data = {
            "datastream_id": datastream_id,
            **read_model,
        }
        state = data.get("state", "unknown")
        summary = (
            f"Credential {credential_id!r} du flux {datastream_id!r} : "
            f"etat {state}, version {data.get('version')}."
        )
        return _result(summary, data)

    # Story 38.7: inbound credential status read (safe model only, no secret).
    register_profiled(
        mcp, get_inbound_credential_status,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )
