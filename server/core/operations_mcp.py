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
  * ``datastream_publication.reconcile_execution`` (execution-state
    reconciliation). It DID mint a candidate through ``create_execution`` for
    ``retry`` / ``refetch``; story 63.7 removed that mint, because nothing in
    this build advances such a candidate and a non-terminal execution holds
    ``uq_datastream_executions_active`` -- 409 on every later publication, and
    ``None`` from ``open_collection_run`` every following night. Both verbs now
    answer ``core.run_origins.refusal_message``;
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

# Story 63.7. A leaf registry -- a frozen table and four pure functions, with no
# `core` import of its own -- so it cannot join the `core.main` import cycle the
# lazy-import convention above exists to avoid.
from core import run_origins

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



def _guard_datastream(
    datastream_id: str,
    identity: str,
    *,
    minimum_capability: str,
    conn=None,
    not_found_message: str = "Datastream not found.",
):
    """Return an ``AccessDecision`` or raise not_found (existence-hiding).

    Wraps ``project_access.resolve_strict_resource_access`` on the Datastream scope.
    Reads require ``view``; bounded recovery (write effect) requires at least
    ``edit``. Denial for ANY reason (not a member, no grant, foreign resource,
    guard-evaluation failure / DB down) surfaces a single ``not_found`` -- we NEVER
    disclose whether the Datastream exists (E36-NFR02). Fail-closed: the operation
    must NOT proceed unguarded on a guard failure.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        if conn is None:
            with request_connection(identity) as owned_conn:
                decision = resolve_strict_resource_access(
                    identity,
                    owned_conn,
                    datastream_id=datastream_id,
                    minimum_capability=minimum_capability,
                    hold_access=True,
                )
        else:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                datastream_id=datastream_id,
                minimum_capability=minimum_capability,
                hold_access=True,
            )
    except Exception as exc:  # noqa: BLE001 -- fail-closed (never unguarded).
        logger.error("operations_mcp: access guard failed ds=%s: %s", datastream_id, exc)
        raise _tool_error("not_found", not_found_message) from exc
    if not decision.allowed or not decision.org_id:
        raise _tool_error("not_found", not_found_message)
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
        raise _tool_error("forbidden_interval", "Invalid refetch interval.")
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
        return {
            "platform_known": bool(platform),
            "estimated_points": points,
            "verdict": "budget_exhausted",
            "can_proceed": False,
        }
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
    from core.datastream_publication import reconcile_execution  # noqa: PLC0415
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
                outcome="failed",
                before_hash=None,
                after_hash=None,
                result={"reason": "missing_execution_reference"},
                outbox_payload={"kind": kind, "datastream_id": datastream_id},
            )
        try:
            outcome = reconcile_execution(execution_id, project_id, conn)
        except Exception as exc:  # noqa: BLE001 -- outcome unknown, never a duplicate.
            logger.error("operations_mcp: reconcile failed exec=%s: %s", execution_id, exc)
            return MutationResult(
                outcome="outcome_unknown",
                before_hash=None,
                after_hash=None,
                result={"reason": "reconcile_uncertain"},
                outbox_payload={"kind": kind, "execution_id": execution_id},
            )
        result = {
            "kind": kind,
            "execution_id": execution_id,
            "final_state": outcome.get("final_state"),
            "action_taken": outcome.get("action_taken"),
        }
        return MutationResult(
            outcome="succeeded" if outcome.get("resolved") else "failed",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload=result,
        )

    if kind == "refetch":
        # LA PARITE, RENDUE 2026-08-22 (story 67.23) -- ET PAR APPEL, PAS PAR COPIE.
        #
        # Ce qui etait ici refusait `refetch` avec `run_origins.NO_ENGINE`. Le
        # refus etait juste EN 63.7 et faux depuis : ce que la story 63.7 a retire,
        # c'est un moteur qui mintait une execution et n'enfilait RIEN -- elle
        # restait en `created`, un etat ACTIF, donc
        # `uq_datastream_executions_active` repondait 409 a toute publication
        # suivante et le flux perdait sa collecte recurrente pour de bon, d'un
        # seul appel d'outil, en silence. Retirer CE moteur etait la reparation.
        #
        # Mais le registre declare `refetch` avec `has_engine=True`
        # (`run_origins.py:93`), des deux cotes du fil (`runOrigins.ts:39`), et la
        # porte REST en a un vrai depuis : il enfile ses fenetres et referme son
        # run. Refuser ici laissait un modele incapable de re-collecter un jour
        # qu'une personne re-collecte d'un clic -- l'audit du 2026-08-17 l'appelle
        # << le trou de parite le plus net entre les portes >> (`05-datastream.md`
        # :385-389).
        #
        # ALORS C'EST LE MOTEUR DE L'AUTRE PORTE QUI EST APPELE. Un second ici
        # serait exactement la faute de 63.7 recommencee : deux chemins vers une
        # execution, dont un seul referme la sienne. Les pre-conditions sont deja
        # toutes verifiees plus haut (:800-832) -- versions, policy, fenetre
        # bornee, exposition de compte, quota, verrou -- et `run_refetch` refait
        # les gates de l'horloge par-dessus.
        from core.datastream_collection_api import (  # noqa: PLC0415
            RefetchRefused,
            expand_refetch_days,
            run_refetch,
        )

        interval = prep.get("interval") or {}
        try:
            days = expand_refetch_days(
                date_from=interval.get("from"), date_to=interval.get("to")
            )
            answer = run_refetch(
                datastream_id=datastream_id,
                project_id=project_id,
                days=days,
                actor=prep.get("actor") or "mcp",
            )
        except RefetchRefused as refused:
            # UN REFUS DU MOTEUR N'EST PAS UNE PANNE : il porte le code et la
            # phrase que la porte REST rend, donc les deux portes refusent avec
            # les memes mots.
            logger.warning(
                "operations_mcp: refetch refused ds=%s code=%s",
                datastream_id,
                refused.code,
            )
            result = {
                "kind": kind,
                "execution_id": None,
                "reason": refused.code,
                "message": refused.message,
            }
            return MutationResult(
                outcome="failed",
                before_hash=None,
                after_hash=None,
                result=result,
                outbox_payload={"kind": kind, "datastream_id": datastream_id,
                                "reason": refused.code},
            )
        except Exception as exc:  # noqa: BLE001
            # L'ISSUE EST INCONNUE, PAS ECHOUEE. Des fenetres ont pu partir en
            # file avant l'incident ; annoncer `failed` inviterait un rejeu qui
            # les doublerait.
            logger.error(
                "operations_mcp: refetch uncertain ds=%s: %s", datastream_id, exc
            )
            return MutationResult(
                outcome="outcome_unknown",
                before_hash=None,
                after_hash=None,
                result={"kind": kind, "reason": "refetch_uncertain"},
                outbox_payload={"kind": kind, "datastream_id": datastream_id},
            )

        result = {
            "kind": kind,
            "execution_id": answer.get("execution_id"),
            "jobs": answer.get("jobs", []),
            "windows": len(answer.get("jobs", [])),
        }
        if answer.get("active_run"):
            # UN RUN DEJA EN VOL EST UN FAIT, JAMAIS UN REFUS (Jean, 2026-08-06) :
            # les fenetres sont enfilees et atterrissent, elles n'ont simplement
            # pas de ligne d'execution a elles.
            result["active_run"] = answer["active_run"]
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload=result,
        )

    # retry: REFUSED, and nothing is minted -- story 63.7.
    #
    # `retry` figure dans AUCUNE entree de `run_origins.RUN_ORIGINS` : le registre
    # ne lui connait pas de moteur, et ce n'est pas un oubli -- rejouer une
    # execution echouee telle quelle est precisement ce qui mintait un candidat
    # non-live contre des versions epinglees sans rien enfiler. Le refus reste,
    # et il reste pour la raison que le registre porte, pas pour une regle ecrite
    # ici. `reconcile` plus haut est intact : il resout une execution qui existe
    # deja et ne minte rien.
    logger.warning(
        "operations_mcp: recovery refused kind=%s ds=%s -- no engine advances this run",
        kind,
        datastream_id,
    )
    result = {
        "kind": kind,
        "execution_id": None,
        "reason": run_origins.NO_ENGINE,
        "message": run_origins.refusal_message(None),
    }
    return MutationResult(
        outcome="failed",
        before_hash=None,
        after_hash=None,
        result=result,
        outbox_payload={"kind": kind, "datastream_id": datastream_id,
                        "reason": run_origins.NO_ENGINE},
    )


# ---------------------------------------------------------------------------
# register(mcp): define each handler locally and register it via register_profiled.
# ---------------------------------------------------------------------------


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
        raise _tool_error("missing_param", "datastream_id is required.")
    identity = _identity()
    _guard_datastream(datastream_id, identity, minimum_capability="view")
    from core.datastream_diagnosis import (  # noqa: PLC0415
        _assemble_timeline,
        _bound_offset,
        _bound_page_size,
        _collect_events,
    )
    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            events, context = _collect_events(conn, datastream_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("operations_mcp: runs failed ds=%s: %s", datastream_id, exc)
        raise _tool_error("server_error", "Erreur serveur.") from exc
    timeline = _assemble_timeline(
        events, cursor=_bound_offset(cursor), page_size=_bound_page_size(page_size)
    )
    summary = (
        f"Executions of datastream {context.get('datastream_id')!r}: "
        f"{timeline['page']['size']} of {timeline['total']}."
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
        raise _tool_error("missing_param", "datastream_id is required.")
    identity = _identity()
    _guard_datastream(datastream_id, identity, minimum_capability="view")
    from core.datastream_diagnosis import _collect_events, _diagnose  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
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
        f"Readiness of datastream {context.get('datastream_id')!r}: "
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
        raise _tool_error("missing_param", "datastream_id is required.")
    if kind not in _RECOVERY_KINDS:
        raise _tool_error("invalid_kind", "Recovery type not allowed.")
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
            raise _tool_error("missing_param", "execution_id is required for reconcile.")
        interval = {"execution_id": execution_id}

    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            target = _load_target(conn, datastream_id)
            if target is None:
                raise _tool_error("not_found", "Datastream not found.")
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
        raise _tool_error("server_error", "Preparation unavailable.") from exc

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
        f"Recovery proposal {kind!r} prepared for datastream "
        f"{datastream_id!r} (immutable, pending confirmation)."
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
        raise _tool_error("missing_param", "preparation_id is required.")
    identity = _identity()

    from core.db import request_connection  # noqa: PLC0415
    from core.operations import (  # noqa: PLC0415
        OperationSpec,
        _sha256,
        execute_operation,
    )

    try:
        with request_connection(identity) as conn:
            prep = _load_preparation(conn, preparation_id)
            if prep is None:
                raise _tool_error("not_found", "Proposal not found.")
            # Re-authorize the resource at CALL time (fail closed, edit floor).
            decision = _guard_datastream(
                prep["datastream_id"], identity, minimum_capability="edit"
            )
            if str(decision.org_id) != prep["org_id"]:
                raise _tool_error("not_found", "Proposal not found.")
            # Stale / consumed / expired proposal -> refuse (no dispatch).
            if prep["state"] != "prepared" or prep["expired"]:
                raise _tool_error("stale_preparation", "Proposal stale or already handled.")

            target = _load_target(conn, prep["datastream_id"])
            if target is None:
                raise _tool_error("not_found", "Datastream not found.")

            # (a) stale plan/mapping versions -> refuse.
            if not _versions_match(prep["target_versions"], target):
                raise _tool_error("stale_versions", "Versions plan/mapping perimees.")
            # (b) changed policy -> refuse.
            if prep["target_versions"].get("policy_version") != _current_policy_version():
                raise _tool_error("policy_changed", "Policy changed since the preparation.")
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
                raise _tool_error("lock_conflict", "An execution is already active.")

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
        raise _tool_error("server_error", "Confirmation unavailable.") from exc

    data = {
        "preparation_id": preparation_id,
        "operation_id": op_result.operation_id,
        "outcome": op_result.outcome,
        "replayed": op_result.replayed,
        "result": op_result.result,
    }
    replayed = " (replay of the original operation)" if op_result.replayed else ""
    summary = (
        f"Recovery {prep['kind']!r} confirmed: operation "
        f"{op_result.operation_id} ({op_result.outcome}){replayed}."
    )
    return _result(summary, data)


# ---- Read: connector installation status (Story 38.2, AC2) -----------------
def get_connector_installation_status(connector_name: str):
    """Read platform connector installation evidence (platform admins only).

    The Operations capability profile controls discovery. Call-time
    authorization separately enforces the platform-administrator audience,
    matching the full REST projection.
    """
    connector_name = (connector_name or "").strip() or None
    if connector_name is None:
        raise _tool_error("missing_param", "connector_name is required.")

    identity = _identity()
    # THE one resolution (audit 12, P1-2): `_identity()` returns the token
    # `sub`, a `person_<ULID>` in canonical mode, and the allow-list is keyed
    # by email -- the raw comparison that stood here could never pass.
    from core.super_admin import identity_is_super_admin  # noqa: PLC0415

    if not identity_is_super_admin(identity):
        raise _tool_error("not_found", "Resource not found.")

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
        raise _tool_error("server_error", "Installation state is unavailable.") from exc

    if read_model is None:
        data = {
            "connector_name": connector_name,
            "environment": environment,
            "state": "NOT_INSTALLED",
            "safe_next_action": (
                "platform_admin: apply installation to advance to DOMAIN_PENDING"
            ),
            "responsible_actor": "platform_admin",
            "blocking_cause": "installation_not_applied",
            "last_verified_at": None,
            "catalog_availability": "unavailable",
        }
    else:
        state = read_model["state"]
        data = {
            "connector_name": connector_name,
            "environment": environment,
            **read_model,
            "catalog_availability": ("selectable" if state == "READY" else "unavailable"),
        }

    summary = (
        f"Connector {connector_name!r} installation: "
        f"{data['state']} / {data['catalog_availability']}."
    )
    return _result(summary, data)


# ---- Read: connector domain config (Story 38.3, AC2) -------------------------
def get_connector_domain_config(connector_name: str):
    """Read the secret-free platform connector domain configuration."""
    connector_name = (connector_name or "").strip() or None
    if connector_name is None:
        raise _tool_error("missing_param", "connector_name is required.")

    identity = _identity()
    # THE one resolution (audit 12, P1-2): `_identity()` returns the token
    # `sub`, a `person_<ULID>` in canonical mode, and the allow-list is keyed
    # by email -- the raw comparison that stood here could never pass.
    from core.super_admin import identity_is_super_admin  # noqa: PLC0415

    if not identity_is_super_admin(identity):
        raise _tool_error("not_found", "Resource not found.")

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
        raise _tool_error("server_error", "Domain configuration is unavailable.") from exc

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
        f"Connector domain configuration for {connector_name!r}: "
        f"{'configured' if configured else 'not configured'} (environment={environment})."
    )
    return _result(summary, data)


# ---- Read: connector verification status (Story 38.4, AC4) ------------------
def get_connector_verification_status(connector_name: str):
    """Read secret-free platform connector verification evidence.

    The Operations profile controls discovery. Call-time authorization
    separately enforces the platform-administrator audience, matching REST.
    """
    connector_name = (connector_name or "").strip() or None
    if connector_name is None:
        raise _tool_error("missing_param", "connector_name is required.")

    identity = _identity()
    # THE one resolution (audit 12, P1-2): `_identity()` returns the token
    # `sub`, a `person_<ULID>` in canonical mode, and the allow-list is keyed
    # by email -- the raw comparison that stood here could never pass.
    from core.super_admin import identity_is_super_admin  # noqa: PLC0415

    if not identity_is_super_admin(identity):
        raise _tool_error("not_found", "Resource not found.")

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
        raise _tool_error("server_error", "Verification state is unavailable.") from exc

    if read_model is None:
        data = {
            "connector_name": connector_name,
            "environment": environment,
            "verified": False,
            "safe_next_action": ("platform_admin: POST /verify to run the first verification"),
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
        f"Connector {connector_name!r} verification: {outcome} (environment={environment})."
    )
    return _result(summary, data)


# ---- Read: connector activation status (Story 38.5, AC2) -------------------
def get_connector_activation_status(connector_name: str, org_id: str):
    """Read one org-scoped connector activation state."""

    connector_name = (connector_name or "").strip() or None
    org_id = (org_id or "").strip() or None
    if connector_name is None:
        raise _tool_error("missing_param", "connector_name is required.")
    if org_id is None:
        raise _tool_error("missing_param", "org_id is required.")

    import os as _os  # noqa: PLC0415

    environment = _os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"
    identity = _identity()
    denied = False
    try:
        from core.connector_activation import get_activation  # noqa: PLC0415
        from core.db import request_connection  # noqa: PLC0415
        from core.project_access import identity_has_org_access  # noqa: PLC0415

        with request_connection(identity) as conn:
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
        raise _tool_error("server_error", "Activation state is unavailable.") from exc

    if denied:
        raise _tool_error("not_found", "Activation state not found.")

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
        f"Connector {connector_name!r} activation for org {org_id!r}: "
        f"{state} ({'active' if activated else 'inactive'})."
    )
    return _result(summary, data)


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
        raise _tool_error("server_error", "Template catalogue unavailable.") from exc

    summary = f"Import template catalogue: {len(templates)} template(s) available."
    return _result(summary, {"templates": templates, "count": len(templates)})


# ---- Read: inbound credential status (Story 38.7, AC2/Task 4) ---------------
def get_inbound_credential_status(datastream_id: str, credential_id: str):
    """Return the secret-free status of one inbound delivery credential.

    The Operations profile and Datastream view capability are required.
    The access lock and credential read share one transaction. The result
    never contains a raw token or token hash, and absence is intentionally
    indistinguishable from cross-tenant denial.
    """
    datastream_id = (datastream_id or "").strip() or None
    credential_id = (credential_id or "").strip() or None
    if datastream_id is None:
        raise _tool_error("missing_param", "datastream_id is required.")
    if credential_id is None:
        raise _tool_error("missing_param", "credential_id is required.")

    identity = _identity()
    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.inbound_credentials import get_credential_state  # noqa: PLC0415

        with request_connection(identity) as conn:
            _guard_datastream(
                datastream_id,
                identity,
                minimum_capability="view",
                conn=conn,
                not_found_message="Credential not found.",
            )
            read_model = get_credential_state(
                conn,
                credential_id=credential_id,
                datastream_id=datastream_id,
            )
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error(
            "operations_mcp: get_inbound_credential_status ds=%s cred=%s: %s",
            datastream_id,
            credential_id,
            exc,
        )
        raise _tool_error("server_error", "Credential status is unavailable.") from exc

    if read_model is None:
        raise _tool_error("not_found", "Credential not found.")

    data = {"datastream_id": datastream_id, **read_model}
    state = data.get("state", "unknown")
    summary = (
        f"Credential {credential_id!r} for Datastream {datastream_id!r}: "
        f"state {state}, version {data.get('version')}."
    )
    return _result(summary, data)


def register(mcp) -> None:
    """Register the Operations-profile MCP tools on *mcp*.

    Called once from core.main (the ONLY hook 36.13 adds to main.py), AFTER the
    datastream_diagnosis registration and BEFORE ``validate_catalog()`` so the
    boot-time validator sees these operations tools. Every tool is registered via
    ``mcp_profiles.register_profiled`` so the capability middleware filters it:
    reads are ``effect="read"``/``confirmation_mode="none"``; the write-effect
    ``confirm_datastream_recovery`` is ``effect="confirmed_write"``/``confirmation_mode="human"``.
    None is ever ``profile="insights"`` (that stays Insights read-only).
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    # ---- Register every tool under profile="operations" -------------------------
    register_profiled(
        mcp,
        list_datastream_runs,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_datastream_readiness,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    # prepare writes an immutable proposal row (app.operation_preparations) -- it is a
    # preparation, not a read (review H2). The consequential dispatch stays on the
    # separate human-confirmed `confirm` tool, so prepare itself authorizes nothing.
    register_profiled(
        mcp,
        prepare_datastream_recovery,
        profile="operations",
        effect="prepare",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        confirm_datastream_recovery,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    # Story 38.2: connector installation status read (same safe model as REST GET).
    register_profiled(
        mcp,
        get_connector_installation_status,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )

    # Story 38.3: connector domain config read (same safe model as REST GET).
    register_profiled(
        mcp,
        get_connector_domain_config,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )

    # Story 38.4: connector verification status read (same safe model as REST GET).
    register_profiled(
        mcp,
        get_connector_verification_status,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )

    # Story 38.5: connector activation status read (same safe model as REST GET).
    register_profiled(
        mcp,
        get_connector_activation_status,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )

    # Story 38.6: import template catalog read (same safe model as REST GET).
    register_profiled(
        mcp,
        list_inbound_templates,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )

    # Story 38.7: inbound credential status read (safe model only, no secret).
    register_profiled(
        mcp,
        get_inbound_credential_status,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
