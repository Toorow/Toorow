"""Durable managed-file dispatch intent and reconciliation (Story 38.13)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ulid import ULID

_TERMINAL = frozenset({"published", "failed", "rejected", "reconciled"})
_FORWARD = {
    "pending": frozenset({"landing", "reconcile_required", "failed", "rejected"}),
    "landing": frozenset({"landed", "reconcile_required", "failed", "rejected"}),
    "landed": frozenset({"validating", "reconcile_required", "failed", "rejected"}),
    "validating": frozenset({"ready", "reconcile_required", "failed", "rejected"}),
    "ready": frozenset({"promoting", "reconcile_required", "failed", "rejected"}),
    "promoting": frozenset({"published", "reconcile_required", "failed"}),
    "reconcile_required": frozenset({"published", "reconciled", "failed", "rejected"}),
}


class ManagedFileDispatchError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def fingerprint_bundle(bundle: dict[str, Any]) -> str:
    if not isinstance(bundle, dict) or not bundle:
        raise ManagedFileDispatchError("dispatch_bundle_invalid")
    canonical = json.dumps(
        bundle, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


_COLUMNS = """
id, raw_import_id, ledger_id, execution_id, datastream_id, project_id,
bundle, bundle_fingerprint, state, candidate_content_fingerprint,
candidate_schema_fingerprint, landing_relation, row_count, dq_evidence,
error_code, reconciliation_evidence, created_at, updated_at
"""


def _row(cur, value) -> dict[str, Any]:
    """AI-219: dates by TYPE. Only the JSON columns still answer to their names."""
    from core.row_json import row_to_json  # noqa: PLC0415

    record = row_to_json(cur.description, value)
    for key in ("bundle", "dq_evidence", "reconciliation_evidence"):
        if isinstance(record.get(key), str):
            record[key] = json.loads(record[key])
    return record


def get_dispatch(
    conn,
    *,
    dispatch_id: str,
    datastream_id: str,
    project_id: str,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM app.managed_file_dispatches "
            "WHERE id = %s AND datastream_id = %s AND project_id = %s",
            (dispatch_id, datastream_id, project_id),
        )
        value = cur.fetchone()
        if value is None:
            raise ManagedFileDispatchError("managed_file_dispatch_not_found")
        return _row(cur, value)


def record_intent(
    conn,
    *,
    raw_import_id: str,
    ledger_id: str,
    execution_id: str,
    datastream_id: str,
    project_id: str,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    fingerprint = fingerprint_bundle(bundle)
    dispatch_id = f"mfd_{ULID()}"
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp_managed_file_dispatch_intent")
        try:
            cur.execute(
                """
                INSERT INTO app.managed_file_dispatches
                  (id, raw_import_id, ledger_id, execution_id, datastream_id,
                   project_id, bundle, bundle_fingerprint)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (raw_import_id, execution_id) DO NOTHING
                """,
                (
                    dispatch_id,
                    raw_import_id,
                    ledger_id,
                    execution_id,
                    datastream_id,
                    project_id,
                    json.dumps(bundle, sort_keys=True),
                    fingerprint,
                ),
            )
            if cur.rowcount == 0:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM app.managed_file_dispatches "
                    "WHERE raw_import_id = %s AND execution_id = %s "
                    "AND datastream_id = %s AND project_id = %s",
                    (raw_import_id, execution_id, datastream_id, project_id),
                )
                value = cur.fetchone()
                if value is None:
                    raise ManagedFileDispatchError(
                        "managed_file_dispatch_scope_mismatch"
                    )
                existing = _row(cur, value)
                if (
                    existing["ledger_id"] != ledger_id
                    or existing["execution_id"] != execution_id
                    or existing["bundle_fingerprint"] != fingerprint
                ):
                    raise ManagedFileDispatchError(
                        "managed_file_dispatch_conflict"
                    )
                cur.execute("RELEASE SAVEPOINT sp_managed_file_dispatch_intent")
                return existing
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_managed_file_dispatch_intent")
            cur.execute("RELEASE SAVEPOINT sp_managed_file_dispatch_intent")
            raise
        cur.execute("RELEASE SAVEPOINT sp_managed_file_dispatch_intent")
    return get_dispatch(
        conn,
        dispatch_id=dispatch_id,
        datastream_id=datastream_id,
        project_id=project_id,
    )


def advance_dispatch(
    conn,
    *,
    dispatch_id: str,
    datastream_id: str,
    project_id: str,
    state: str,
    candidate_content_fingerprint: str | None = None,
    candidate_schema_fingerprint: str | None = None,
    landing_relation: str | None = None,
    row_count: int | None = None,
    dq_evidence: dict[str, Any] | None = None,
    error_code: str | None = None,
    reconciliation_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM app.managed_file_dispatches "
            "WHERE id = %s AND datastream_id = %s AND project_id = %s FOR UPDATE",
            (dispatch_id, datastream_id, project_id),
        )
        value = cur.fetchone()
        if value is None:
            raise ManagedFileDispatchError("managed_file_dispatch_not_found")
        current = value[0]
        if current == state:
            existing = get_dispatch(
                conn,
                dispatch_id=dispatch_id,
                datastream_id=datastream_id,
                project_id=project_id,
            )
            supplied = {
                "candidate_content_fingerprint": candidate_content_fingerprint,
                "candidate_schema_fingerprint": candidate_schema_fingerprint,
                "landing_relation": landing_relation,
                "row_count": row_count,
                "dq_evidence": dq_evidence,
            }
            if any(
                value is not None and existing.get(key) not in (None, value)
                for key, value in supplied.items()
            ):
                raise ManagedFileDispatchError("managed_file_dispatch_evidence_conflict")
            return existing
        if current in _TERMINAL or state not in _FORWARD.get(current, frozenset()):
            raise ManagedFileDispatchError(
                "managed_file_dispatch_transition_invalid",
                f"{current}->{state}",
            )
        evidence = json.dumps(reconciliation_evidence or {}, sort_keys=True)
        dq = json.dumps(dq_evidence or {}, sort_keys=True)
        cur.execute(
            """
            UPDATE app.managed_file_dispatches
            SET state = %s,
                candidate_content_fingerprint =
                  COALESCE(%s, candidate_content_fingerprint),
                candidate_schema_fingerprint =
                  COALESCE(%s, candidate_schema_fingerprint),
                landing_relation = COALESCE(%s, landing_relation),
                row_count = COALESCE(%s, row_count),
                dq_evidence =
                  CASE WHEN %s::jsonb = '{}'::jsonb
                    THEN dq_evidence ELSE %s::jsonb END,
                error_code = %s,
                reconciliation_evidence =
                  CASE WHEN %s::jsonb = '{}'::jsonb
                    THEN reconciliation_evidence ELSE %s::jsonb END,
                updated_at = NOW()
            WHERE id = %s AND datastream_id = %s AND project_id = %s
              -- Les quatre gardes portent un `::text` / `::bigint` explicite.
              -- Un placeholder IS NULL nu ne dit pas au serveur de quel type est le
              -- parametre : psycopg 3 envoie une str ET un None sans oid, et
              -- l'UPDATE entier tombe en IndeterminateDatatype -- donc
              -- `advance_dispatch` ne pouvait aboutir pour AUCUN dispatch
              -- contre un vrai Postgres. Mesure 2026-08-04 sur cluster jetable
              -- (str -> erreur, None -> erreur, int/float -> OK) ; cf. AI-186.
              AND (%s::text IS NULL OR candidate_content_fingerprint IS NULL
                   OR candidate_content_fingerprint = %s)
              AND (%s::text IS NULL OR candidate_schema_fingerprint IS NULL
                   OR candidate_schema_fingerprint = %s)
              AND (%s::text IS NULL OR landing_relation IS NULL OR landing_relation = %s)
              AND (%s::bigint IS NULL OR row_count IS NULL OR row_count = %s)
              AND (%s::jsonb = '{}'::jsonb OR dq_evidence IS NULL
                   OR dq_evidence = %s::jsonb)
            """,
            (
                state,
                candidate_content_fingerprint,
                candidate_schema_fingerprint,
                landing_relation,
                row_count,
                dq,
                dq,
                error_code,
                evidence,
                evidence,
                dispatch_id,
                datastream_id,
                project_id,
                candidate_content_fingerprint,
                candidate_content_fingerprint,
                candidate_schema_fingerprint,
                candidate_schema_fingerprint,
                landing_relation,
                landing_relation,
                row_count,
                row_count,
                dq,
                dq,
            ),
        )
        if cur.rowcount != 1:
            raise ManagedFileDispatchError("managed_file_dispatch_evidence_conflict")
    return get_dispatch(
        conn,
        dispatch_id=dispatch_id,
        datastream_id=datastream_id,
        project_id=project_id,
    )


def _candidate_columns(dispatch: dict[str, Any]) -> list[tuple[str, str]]:
    raw = (dispatch.get("bundle") or {}).get("candidate_columns") or []
    columns: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ManagedFileDispatchError("dispatch_candidate_columns_invalid")
        name = str(item.get("name") or "")
        kind = str(item.get("type") or "")
        if not name or not kind:
            raise ManagedFileDispatchError("dispatch_candidate_columns_invalid")
        columns.append((name, kind))
    if not columns:
        raise ManagedFileDispatchError("dispatch_candidate_columns_missing")
    return columns


def reconcile_dispatch(
    conn,
    *,
    dispatch_id: str,
    datastream_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Resolve cross-store uncertainty without making the raw import terminal."""
    dispatch = get_dispatch(
        conn,
        dispatch_id=dispatch_id,
        datastream_id=datastream_id,
        project_id=project_id,
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.state, l.outcome,
                   d.current_published_execution_id = e.id AS is_current,
                   EXISTS (
                     SELECT 1 FROM app.datastream_outbox o
                     WHERE o.execution_id = e.id
                       AND o.datastream_id = e.datastream_id
                       AND o.project_id = e.project_id
                       AND o.event_type = 'published'
                   ) AS has_outbox
            FROM app.datastream_executions e
            JOIN app.managed_feed_import_ledger l
              ON l.id = %s AND l.execution_id = e.id
             AND l.datastream_id = e.datastream_id
             AND l.project_id = e.project_id
            JOIN app.datastreams d
              ON d.id = e.datastream_id AND d.project_id = e.project_id
            WHERE e.id = %s AND e.datastream_id = %s AND e.project_id = %s
            """,
            (
                dispatch["ledger_id"],
                dispatch["execution_id"],
                datastream_id,
                project_id,
            ),
        )
        value = cur.fetchone()
    if value is None:
        return advance_dispatch(
            conn,
            dispatch_id=dispatch_id,
            datastream_id=datastream_id,
            project_id=project_id,
            state="reconcile_required",
            reconciliation_evidence={"reason": "scoped_evidence_missing"},
            error_code="dispatch_reconciliation_required",
        )

    execution_state, ledger_outcome, is_current, has_outbox = value
    if execution_state in {"failed", "cancelled"} or ledger_outcome in {
        "failed",
        "rejected",
    }:
        target = "rejected" if ledger_outcome == "rejected" else "failed"
        return advance_dispatch(
            conn,
            dispatch_id=dispatch_id,
            datastream_id=datastream_id,
            project_id=project_id,
            state=target,
            reconciliation_evidence={
                "execution": execution_state,
                "ledger": ledger_outcome,
            },
            error_code=f"dispatch_{target}",
        )

    # Earlier warehouse phases are retryable but not publishable until the
    # candidate and positive DQ proof have both been durably recorded.
    if (
        execution_state not in {"ready", "publishing", "published"}
        or not isinstance(dispatch.get("dq_evidence"), dict)
        or dispatch["dq_evidence"].get("status") != "passed"
    ):
        if dispatch["state"] == "reconcile_required":
            return dispatch
        return advance_dispatch(
            conn,
            dispatch_id=dispatch_id,
            datastream_id=datastream_id,
            project_id=project_id,
            state="reconcile_required",
            reconciliation_evidence={
                "phase": dispatch["state"],
                "execution": execution_state,
                "ledger": ledger_outcome,
                "reason": "prepublication_phase_requires_resume",
            },
            error_code="dispatch_reconciliation_required",
        )

    relation = str(dispatch.get("landing_relation") or "").rsplit(".", 1)[-1]
    suffix = f"__cand_{dispatch['execution_id']}"
    table = relation[: -len(suffix)] if relation.endswith(suffix) else relation
    if not table:
        raise ManagedFileDispatchError("dispatch_landing_relation_invalid")
    evidence = {
        "candidate_content_fingerprint": dispatch[
            "candidate_content_fingerprint"
        ],
        "candidate_schema_fingerprint": dispatch["candidate_schema_fingerprint"],
        "dispatch_bundle_fingerprint": dispatch["bundle_fingerprint"],
        "landing_relation": dispatch["landing_relation"],
    }

    from core.datastream_publication import (  # noqa: PLC0415
        begin_managed_file_promotion,
        commit_publication,
    )
    from core.raw_landing import promote_candidate  # noqa: PLC0415

    try:
        if execution_state in {"ready", "publishing"}:
            begin_managed_file_promotion(
                dispatch["execution_id"],
                project_id,
                "system:managed-file-reconciliation",
                conn,
                dispatch_id=dispatch_id,
                candidate_evidence=evidence,
            )
        promotion = promote_candidate(
            table,
            dispatch["execution_id"],
            columns=_candidate_columns(dispatch),
            project_id=project_id,
            idempotency_column="execution_id",
            idempotency_value=dispatch["execution_id"],
            expected_rows=int(dispatch.get("row_count") or 0),
            expected_content_fingerprint=dispatch[
                "candidate_content_fingerprint"
            ],
            expected_schema_fingerprint=dispatch[
                "candidate_schema_fingerprint"
            ],
        )
        if execution_state == "published":
            if not (ledger_outcome == "published" and is_current and has_outbox):
                raise ManagedFileDispatchError(
                    "published_dispatch_evidence_incomplete"
                )
            reconciled = advance_dispatch(
                conn,
                dispatch_id=dispatch_id,
                datastream_id=datastream_id,
                project_id=project_id,
                state="published",
                reconciliation_evidence={
                    "phase": "legacy_publication_reconciled",
                    "promotion": promotion,
                },
            )
            conn.commit()
            return reconciled

        commit_publication(
            dispatch["execution_id"],
            project_id,
            "system:managed-file-reconciliation",
            conn,
            ledger_id=dispatch["ledger_id"],
            dispatch_id=dispatch_id,
            candidate_evidence={**evidence, "warehouse_promotion": promotion},
        )
        return get_dispatch(
            conn,
            dispatch_id=dispatch_id,
            datastream_id=datastream_id,
            project_id=project_id,
        )
    except Exception as exc:
        try:
            dispatch = advance_dispatch(
                conn,
                dispatch_id=dispatch_id,
                datastream_id=datastream_id,
                project_id=project_id,
                state="reconcile_required",
                reconciliation_evidence={
                    "phase": "promotion_or_final_publication",
                    "error": type(exc).__name__,
                },
                error_code="dispatch_promotion_reconciliation_required",
            )
            conn.commit()
        except Exception:
            conn.rollback()
        raise ManagedFileDispatchError(
            "dispatch_promotion_reconciliation_required"
        ) from exc


def reconcile_pending_dispatches(
    conn,
    *,
    limit: int = 100,
    datastream_id: str | None = None,
) -> dict[str, int]:
    """Sweep retryable dispatch journals; every lookup remains triple scoped."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id,datastream_id,project_id
            FROM app.managed_file_dispatches
            WHERE state IN (
              'pending','landing','landed','validating','ready',
              'promoting','reconcile_required'
            )
              -- `::text` : le balayage passe `datastream_id=None` par defaut,
              -- et un placeholder IS NULL nu rend la requete indeterminee (AI-186).
              AND (%s::text IS NULL OR datastream_id = %s)
            ORDER BY updated_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (datastream_id, datastream_id, limit),
        )
        rows = cur.fetchall() or []
    pending = [row for row in rows if isinstance(row, (tuple, list)) and len(row) == 3]
    result = {"checked": len(pending), "published": 0, "retryable": 0}
    for dispatch_id, scoped_datastream_id, project_id in pending:
        try:
            reconciled = reconcile_dispatch(
                conn,
                dispatch_id=dispatch_id,
                datastream_id=scoped_datastream_id,
                project_id=project_id,
            )
            if reconciled["state"] == "published":
                result["published"] += 1
            else:
                result["retryable"] += 1
        except ManagedFileDispatchError:
            result["retryable"] += 1
    return result

