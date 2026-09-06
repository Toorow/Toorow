"""Story 36.2 transactional operation/audit/outbox foundation tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _conn(*rows):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(rows)
    cur.fetchall.return_value = []
    conn.cursor.return_value = cur
    return conn, cur


def _spec(**overrides):
    from core.operations import OperationSpec

    values = {
        "command_type": "credential.account.expose",
        "actor": "owner-1",
        "effective_org_id": "org-1",
        "resource_path": ("organization:org-1", "credential:conn-1", "account:acct-1"),
        "idempotency_key": "request-123",
        "host_context": {"host": "console", "workspace_id": "workspace-1"},
        "versions": {"policy": "p1", "catalog": "c1", "tool": "t1"},
        "request_payload": {"grantee_org_id": "org-2"},
        "provider_references": {"connection_ref": "conn-1", "account_id": "acct-1"},
        "confirmation_mode": "server",
        "confirmation_reference": "confirm-1",
        "trace_id": "a" * 32,
    }
    values.update(overrides)
    return OperationSpec(**values)


def test_validate_json_accepts_identifier_keys_but_rejects_secret_material():
    """Regression: `credential_id`/`connection_ref`/`bearer_hash` are foreign
    keys/digests, not secret material, and must not make the operation raise
    (which previously broke the entire account-exposure happy path)."""
    from core.operations import OperationValidationError, _is_secret_key, _validate_json

    # Identifier / reference / digest keys are safe.
    for safe in ("credential_id", "connection_ref", "api_key_id", "bearer_hash", "token_ref"):
        assert _is_secret_key(safe) is False, safe
    _validate_json(
        {"credential_id": "cred-1", "account_id": "acct-1", "grantee_org_id": "org-2"},
        name="outbox_payload",
    )

    # Value-bearing secret keys are still rejected.
    for secret in ("credential", "api_key", "access_token", "credential_secret", "password"):
        assert _is_secret_key(secret) is True, secret
    with pytest.raises(OperationValidationError):
        _validate_json({"credential_secret": "shhh"}, name="outbox_payload")


def test_operation_spec_hashes_raw_idempotency_and_rejects_nested_secrets():
    from core.operations import OperationValidationError, prepare_operation

    prepared = prepare_operation(_spec())
    assert prepared.idempotency_key_hash != "request-123"
    assert len(prepared.idempotency_key_hash) == 64
    assert prepared.request_hash == prepare_operation(_spec()).request_hash
    with pytest.raises(OperationValidationError):
        prepare_operation(_spec(request_payload={"nested": {"access_token": "secret"}}))


def test_execute_operation_runs_mutation_audit_and_outbox_without_committing():
    from core.operations import MutationResult, execute_operation

    conn, cur = _conn(None, ("op-1",))
    mutation = MagicMock(
        return_value=MutationResult(
            outcome="succeeded",
            before_hash="b" * 64,
            after_hash="c" * 64,
            result={"grant_id": "grant-1"},
            outbox_payload={"grant_id": "grant-1"},
        )
    )

    result = execute_operation(conn, _spec(), mutation=mutation)

    assert result.operation_id == "op-1"
    assert result.replayed is False
    assert result.outcome == "succeeded"
    assert result.audit_event_id.startswith("audit_")
    assert result.outbox_event_id.startswith("opout_")
    mutation.assert_called_once_with(conn, "op-1")
    conn.commit.assert_not_called()
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "INSERT INTO app.operations" in sql
    assert "INSERT INTO app.audit_log" in sql
    assert "INSERT INTO app.operation_outbox" in sql
    assert "request-123" not in repr(cur.execute.call_args_list)


def test_an_operation_may_pin_the_plan_and_mapping_versions_it_ran_against():
    """The two names the schema declares and the validator refused.

    Migration 070 states the vocabulary in the column's own comment -- "the
    immutable plan/mapping/policy/catalog/tool versions the proposal was pinned
    to" -- and `_VERSION_KEYS` carried three of the five. Every operation that
    pinned the plan and mapping versions it ran against was refused before any
    SQL, with a message that named a key and not the tool.

    That was BOTH MCP tools of `datastream_first_candidate`, so a Datastream that
    reached a Ready candidate had no way to be published at all.

    Measured on the EFFECT: the operation runs, and the versions it pinned reach
    the INSERT. A test that only asserted the set had grown would stay green over
    a validator that dropped them on the way to SQL.
    """
    from core.operations import MutationResult, execute_operation

    conn, cur = _conn(None, ("op-1",))
    mutation = MagicMock(
        return_value=MutationResult(
            outcome="succeeded", before_hash=None, after_hash="c" * 64,
            result={}, outbox_payload={},
        )
    )

    result = execute_operation(
        conn,
        _spec(versions={"plan": "dsp_1", "mapping": "dmap_1", "tool": "publish"}),
        mutation=mutation,
    )

    assert result.outcome == "succeeded"
    written = repr(cur.execute.call_args_list)
    assert "dsp_1" in written and "dmap_1" in written


def test_execute_operation_replay_returns_original_without_mutation():
    from core.operations import execute_operation, prepare_operation

    conn, cur = _conn(
        (
            "op-existing",
            "succeeded",
            {"grant_id": "grant-1"},
            "audit-existing",
            "opout-existing",
            prepare_operation(_spec()).request_hash,
        )
    )
    mutation = MagicMock()

    result = execute_operation(conn, _spec(), mutation=mutation)

    assert result.replayed is True
    assert result.operation_id == "op-existing"
    assert result.result == {"grant_id": "grant-1"}
    mutation.assert_not_called()
    assert len(cur.execute.call_args_list) == 1


def test_execute_operation_propagates_mutation_failure_and_never_commits():
    from core.operations import execute_operation

    conn, cur = _conn(None, ("op-1",))
    mutation = MagicMock(side_effect=RuntimeError("mutation failed"))
    with pytest.raises(RuntimeError, match="mutation failed"):
        execute_operation(conn, _spec(), mutation=mutation)
    conn.commit.assert_not_called()
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "operation_outbox" not in sql
    assert "audit_log" not in sql


def test_claim_outbox_is_bounded_skip_locked_and_uses_stable_event_id():
    from core.operations import claim_outbox_batch

    conn, cur = _conn()
    cur.fetchall.return_value = [
        ("opout-1", "op-1", "credential.account.expose", {"grant_id": "g1"}, 2)
    ]
    rows = claim_outbox_batch(conn, worker_id="worker-1", limit=200)
    assert rows[0].event_id == "opout-1"
    assert rows[0].consumer_idempotency_key == "opout-1"
    sql = cur.execute.call_args.args[0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert cur.execute.call_args.args[1][0] == 50


def test_uncertain_delivery_sets_outcome_unknown_and_keeps_retryable():
    from core.operations import record_delivery_result

    conn, cur = _conn(("op-1",))
    record_delivery_result(
        conn,
        event_id="opout-1",
        confirmed=False,
        uncertain=True,
        error_class="transport_timeout",
    )
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "outcome_unknown" in sql
    assert "retry_at" in sql
    conn.commit.assert_not_called()


def test_support_response_requires_post_commit_audit_lookup():
    from core.operations import (
        build_support_disclosure_response,
        confirm_support_audit_committed,
    )

    missing_conn, _ = _conn(None)
    with pytest.raises(RuntimeError, match="committed audit"):
        confirm_support_audit_committed(missing_conn, "audit-1")
    with pytest.raises(RuntimeError, match="committed audit"):
        build_support_disclosure_response(
            audit_reference=None, evidence={"class": "log"}
        )

    committed_conn, _ = _conn(("audit-1",))
    reference = confirm_support_audit_committed(committed_conn, "audit-1")
    response = build_support_disclosure_response(
        audit_reference=reference,
        evidence={"class": "redacted_error", "evidence_hash": "d" * 64},
    )
    assert response["audit_event_id"] == "audit-1"
    assert "raw" not in response


def test_support_disclosure_uses_the_same_operation_foundation(monkeypatch):
    from core import operations

    captured = {}

    def execute(conn, spec, *, mutation):
        captured["spec"] = spec
        changed = mutation(conn, "op-support")
        return operations.OperationResult(
            "op-support", "succeeded", changed.result, "audit-support", "outbox-support", False
        )

    monkeypatch.setattr(operations, "execute_operation", execute)
    result = operations.prepare_support_disclosure(
        MagicMock(),
        actor="support-1",
        effective_org_id="org-1",
        resource_path=("organization:org-1", "pull:pull-1"),
        idempotency_key="support-request-1",
        host_context={"host": "console"},
        versions={"policy": "p1", "catalog": "c1", "tool": "support-v1"},
        disclosure_class="redacted_error",
        evidence_hash="d" * 64,
        redaction_hash="e" * 64,
        trace_id=None,
    )
    assert result.audit_event_id == "audit-support"
    assert captured["spec"].command_type == "support.disclosure"


def test_reconciliation_only_resolves_outcome_unknown_once():
    from core.operations import reconcile_operation_outcome

    conn, cur = _conn()
    cur.rowcount = 1
    assert reconcile_operation_outcome(
        conn,
        operation_id="op-1",
        resolved_outcome="succeeded",
        evidence_hash="f" * 64,
    ) is True
    assert "state = 'outcome_unknown'" in cur.execute.call_args.args[0]


@pytest.mark.live_pg
def test_live_pg_reconciliation_actually_runs_against_a_real_server(pg_conn):
    """Le test ci-dessus etait VERT sur une fonction qui ne pouvait jamais marcher.

    Il passe un curseur `MagicMock`, qui accepte n'importe quel SQL et ne
    verifie aucun type -- exactement ce que l'en-tete de
    `conformance/test_sql_parameter_typing.py` decrit comme rendant cette classe
    STRUCTURELLEMENT invisible aux doubles de test.

    La requete portait `jsonb_build_object('reconciliation_evidence_hash', %s)`.
    `jsonb_build_object` accepte `any` : un `%s` nu n'a aucun contexte de type,
    et Postgres refuse la requete ENTIERE. Mesure le 2026-08-09 contre un
    serveur reel :

        SELECT jsonb_build_object('k', %s)        -> IndeterminateDatatype
        SELECT jsonb_build_object('k', %s::text)  -> {"k": "abc"}

    Consequence : AUCUNE operation `outcome_unknown` ne pouvait quitter cet
    etat. La seule sortie prevue levait a chaque appel. C'est le meme defaut,
    par le meme mecanisme, que `advance_state` en 2026-08-04 -- ou aucun
    candidat ne pouvait quitter `created`.

    Ce test-ci exerce la vraie requete contre la vraie base, et c'est la seule
    forme de preuve qui aurait attrape celui-la.
    """
    import hashlib

    import ulid as _ulid
    from core.operations import reconcile_operation_outcome

    op_id = f"op_{_ulid.ULID()}"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.operations "
            "(id, effective_org_id, command_type, actor, resource_path, "
            " host_context, versions, request_hash, provider_references, "
            " confirmation_mode, idempotency_key_hash, state) "
            "VALUES (%s, NULL, 'test.reconciliation', 'test', '[]'::jsonb, "
            "        '{}'::jsonb, '{}'::jsonb, %s, '{}'::jsonb, 'server', %s, "
            "        'outcome_unknown')",
            (
                op_id,
                hashlib.sha256(f"req:{op_id}".encode()).hexdigest(),
                hashlib.sha256(f"idem:{op_id}".encode()).hexdigest(),
            ),
        )

    evidence = "f" * 64
    assert (
        reconcile_operation_outcome(
            pg_conn,
            operation_id=op_id,
            resolved_outcome="succeeded",
            evidence_hash=evidence,
        )
        is True
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT state, outcome, result->>'reconciliation_evidence_hash' "
            "FROM app.operations WHERE id = %s",
            (op_id,),
        )
        state, outcome, stored = cur.fetchone()
    assert (state, outcome, stored) == ("succeeded", "succeeded", evidence)

    # EXACTEMENT UNE FOIS. La seconde tentative ne trouve plus d'operation dans
    # `outcome_unknown` et rend False -- c'est ce que l'AC4 de 38.17 appelle
    # << reconciled without blind resubmission >>.
    assert (
        reconcile_operation_outcome(
            pg_conn,
            operation_id=op_id,
            resolved_outcome="failed",
            evidence_hash="a" * 64,
        )
        is False
    )


def test_migration_060_contains_operation_audit_outbox_contract():
    from tests.conftest import REPO_ROOT

    sql = (REPO_ROOT / "infra/nango/migrations/060_operation_audit_outbox.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS app.operations" in sql
    assert "CREATE TABLE IF NOT EXISTS app.operation_outbox" in sql
    assert "idempotency_key_hash" in sql
    assert "outcome_unknown" in sql
    assert "ALTER TABLE app.audit_log" in sql


def test_dispatch_retries_reuse_the_stable_consumer_idempotency_key():
    from core.operations import OutboxEvent, dispatch_outbox_event

    event = OutboxEvent("opout-1", "op-1", "test.event", {"safe": True}, 1)
    handler = MagicMock(return_value={"accepted": True})
    dispatch_outbox_event(event, handler)
    dispatch_outbox_event(event, handler)
    assert [call.kwargs["idempotency_key"] for call in handler.call_args_list] == [
        "opout-1",
        "opout-1",
    ]


def test_reconciliation_uses_original_provider_references():
    from core.operations import reconcile_uncertain_operation

    conn, cur = _conn(({"provider_operation_id": "provider-op-1"},))
    cur.rowcount = 1
    resolver = MagicMock(return_value=("succeeded", "a" * 64))
    assert reconcile_uncertain_operation(
        conn, operation_id="op-1", resolver=resolver
    ) is True
    resolver.assert_called_once_with(
        "op-1", {"provider_operation_id": "provider-op-1"}
    )


@pytest.mark.parametrize(
    "failure_sql",
    ["INSERT INTO app.audit_log", "INSERT INTO app.operation_outbox"],
)
def test_audit_or_outbox_failure_propagates_without_commit(failure_sql):
    from core.operations import MutationResult, execute_operation

    conn, cur = _conn(None, ("op-1",))
    def execute(sql, params=None):
        if failure_sql in sql:
            raise RuntimeError("evidence write failed")
        return None

    cur.execute.side_effect = execute
    mutation = MagicMock(
        return_value=MutationResult(
            "succeeded", None, "b" * 64, {"ok": True}, {"ok": True}
        )
    )
    with pytest.raises(RuntimeError, match="evidence write failed"):
        execute_operation(conn, _spec(), mutation=mutation)
    conn.commit.assert_not_called()

def test_same_idempotency_key_with_different_request_is_rejected():
    from core.operations import OperationIdempotencyConflict, execute_operation

    conn, _ = _conn(
        ("op-existing", "succeeded", {}, "audit-1", "opout-1", "0" * 64)
    )
    with pytest.raises(OperationIdempotencyConflict):
        execute_operation(conn, _spec(), mutation=MagicMock())
