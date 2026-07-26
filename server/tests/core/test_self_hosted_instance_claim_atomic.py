from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from core.entry_confirmations import (
    INSTANCE_CLAIM_COMMAND,
    ConsumedEntryConfirmation,
    canonical_payload_hash,
)
from core.operations import OperationResult
from core.self_hosted_instance_claim import (
    SelfHostedClaimUnavailable,
    SelfHostedClaimValidationError,
    bootstrap_exchange_session_is_ready,
    claim_self_hosted_instance,
    exchange_bootstrap_capability,
    provision_bootstrap_capability,
)


def _confirmation(
    *, actor: str = "person_claimant", idempotency_key: str = "claim-request-1"
) -> ConsumedEntryConfirmation:
    payload = {
        "organization_name": "First organization",
        "organization_slug": "first-organization",
        "project_name": "First project",
        "project_slug": "first-project",
        "currency": "EUR",
        "timezone": "Europe/Paris",
    }
    return ConsumedEntryConfirmation(
        confirmation_id="econf_claim",
        command_type=INSTANCE_CLAIM_COMMAND,
        actor_person_id=actor,
        payload_hash=canonical_payload_hash(INSTANCE_CLAIM_COMMAND, payload),
        idempotency_key_hash=hashlib.sha256(idempotency_key.encode()).hexdigest(),
        operation_id=None,
        replayed=False,
    )
class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.transaction_entries += 1
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.conn.transaction_successes += 1
        else:
            self.conn.transaction_rollbacks += 1
        return False


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.conn.statements.append((normalized, params))
        self.rowcount = 0
        if normalized.startswith("SELECT 1 FROM app.instance_claims"):
            self._row = (1,) if self.conn.already_claimed else None
        elif normalized.startswith("SELECT EXISTS ( SELECT 1 FROM app.organizations"):
            self._row = (self.conn.has_non_seed_organizations,)
        elif normalized.startswith(
            "SELECT EXISTS ( SELECT 1 FROM app.instance_bootstrap_exchange_sessions"
        ):
            self._row = (
                self.conn.exchange_available
                and not self.conn.already_claimed
                and not self.conn.has_non_seed_organizations,
            )
        elif normalized.startswith(
            "SELECT id, expires_at FROM app.instance_bootstrap_capabilities"
        ):
            self._row = (
                (self.conn.capability_id, self.conn.capability_expiry)
                if self.conn.capability_available
                else None
            )
        elif normalized.startswith("SELECT exchange.id, capability.id"):
            self._row = (
                (self.conn.exchange_id, self.conn.capability_id)
                if self.conn.exchange_available
                else None
            )
        elif normalized.startswith(
            "UPDATE app.instance_bootstrap_capabilities SET state = 'exchanged'"
        ):
            self._row = None
            self.rowcount = 1 if self.conn.exchange_winner else 0
        elif normalized.startswith("INSERT INTO app.instance_bootstrap_exchange_sessions"):
            self._row = (self.conn.exchange_expiry,)
        elif normalized.startswith("INSERT INTO app.instance_claims"):
            self._row = ("iclaim_winner",) if self.conn.singleton_winner else None
        elif normalized.startswith(
            "UPDATE app.instance_bootstrap_exchange_sessions SET state = 'consumed'"
        ) or normalized.startswith(
            "UPDATE app.instance_bootstrap_capabilities SET state = 'consumed'"
        ):
            self._row = None
            self.rowcount = 1 if self.conn.consume_winner else 0
        else:
            self._row = None

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self):
        self.statements = []
        self.transaction_entries = 0
        self.transaction_successes = 0
        self.transaction_rollbacks = 0
        self.already_claimed = False
        self.has_non_seed_organizations = False
        self.capability_available = True
        self.capability_id = "iboot_1"
        self.capability_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        self.exchange_available = True
        self.exchange_id = "ibx_1"
        self.exchange_expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
        self.exchange_winner = True
        self.singleton_winner = True
        self.consume_winner = True

    def transaction(self):
        return _Transaction(self)

    def cursor(self):
        return _Cursor(self)


def _claim(conn, monkeypatch, *, bearer="b" * 48):
    captured = {}

    def execute_operation(connection, spec, *, mutation):
        captured["spec"] = spec
        changed = mutation(connection, "op_claim")
        return OperationResult(
            operation_id="op_claim",
            outcome=changed.outcome,
            result=changed.result,
            audit_event_id="audit_claim",
            outbox_event_id="opout_claim",
            replayed=False,
        )

    monkeypatch.setattr("core.self_hosted_instance_claim.execute_operation", execute_operation)
    monkeypatch.setattr(
        "core.setup_responsibilities.bootstrap_journey_from_acceptance",
        lambda *_args, **_kwargs: "setup_claim",
    )
    result = claim_self_hosted_instance(
        conn,
        deployment_mode="self_hosted",
        bootstrap_exchange_bearer=bearer,
        claimant_person_id="person_claimant",
        organization_name="First organization",
        organization_slug="first-organization",
        project_name="First project",
        project_slug="first-project",
        idempotency_key="claim-request-1",
        confirmation=_confirmation(),
    )
    return result, captured["spec"]


def test_provision_hashes_and_rotates_without_persisting_raw_bearer():
    conn = _Connection()
    bearer = "installer-bootstrap-secret-" + "x" * 32
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    capability = provision_bootstrap_capability(
        conn,
        deployment_mode="self_hosted",
        bearer=bearer,
        expires_at=expiry,
    )

    flat_params = [item for _, params in conn.statements for item in (params or ())]
    assert bearer not in flat_params
    assert hashlib.sha256(bearer.encode()).hexdigest() in flat_params
    assert capability.capability_id.startswith("iboot_")
    assert any("SET state = 'revoked'" in sql for sql, _ in conn.statements)
    assert conn.transaction_successes == 1


def test_exchange_consumes_fragment_bearer_into_short_session_without_leak():
    conn = _Connection()
    bearer = "fragment-bootstrap-" + "y" * 32

    exchange = exchange_bootstrap_capability(
        conn,
        deployment_mode="self_hosted",
        bootstrap_bearer=bearer,
    )

    flat_params = [item for _, params in conn.statements for item in (params or ())]
    assert bearer not in flat_params
    assert hashlib.sha256(bearer.encode()).hexdigest() in flat_params
    assert exchange.exchange_id.startswith("ibx_")
    assert exchange.session_bearer not in flat_params
    assert any("SET state = 'exchanged'" in sql for sql, _ in conn.statements)
    assert conn.transaction_successes == 1


def test_tokenless_exchange_session_can_resume_without_consuming_capability():
    conn = _Connection()
    bearer = "session-cookie-" + "s" * 32

    assert bootstrap_exchange_session_is_ready(
        conn,
        deployment_mode="self_hosted",
        bootstrap_exchange_bearer=bearer,
    )

    flat_params = [item for _, params in conn.statements for item in (params or ())]
    assert bearer not in flat_params
    assert hashlib.sha256(bearer.encode()).hexdigest() in flat_params
    assert conn.transaction_entries == 0

    conn.already_claimed = True
    assert not bootstrap_exchange_session_is_ready(
        conn,
        deployment_mode="self_hosted",
        bootstrap_exchange_bearer=bearer,
    )


def test_provision_refuses_claimed_instance():
    conn = _Connection()
    conn.already_claimed = True

    with pytest.raises(SelfHostedClaimUnavailable, match="claim unavailable"):
        provision_bootstrap_capability(
            conn,
            deployment_mode="self_hosted",
            bearer="x" * 48,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    assert conn.transaction_rollbacks == 1
    assert not any(
        sql.startswith("INSERT INTO app.instance_bootstrap_capabilities")
        for sql, _ in conn.statements
    )


def test_existing_non_seed_installation_refuses_bootstrap_and_claim(monkeypatch):
    provision_conn = _Connection()
    provision_conn.has_non_seed_organizations = True

    with pytest.raises(SelfHostedClaimUnavailable, match="claim unavailable"):
        provision_bootstrap_capability(
            provision_conn,
            deployment_mode="self_hosted",
            bearer="x" * 48,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    assert provision_conn.transaction_rollbacks == 1
    assert not any(
        sql.startswith("INSERT INTO app.instance_bootstrap_capabilities")
        for sql, _ in provision_conn.statements
    )

    claim_conn = _Connection()
    claim_conn.has_non_seed_organizations = True

    with pytest.raises(SelfHostedClaimUnavailable, match="claim unavailable"):
        _claim(claim_conn, monkeypatch)

    assert claim_conn.transaction_rollbacks == 1
    assert not any(
        sql.startswith("INSERT INTO app.instance_claims")
        or sql.startswith("INSERT INTO app.instance_members")
        or sql.startswith("INSERT INTO app.organizations")
        or sql.startswith("INSERT INTO app.projects")
        for sql, _ in claim_conn.statements
    )


def test_claim_creates_singleton_scope_owner_and_transactional_evidence(
    monkeypatch,
):
    conn = _Connection()
    result, spec = _claim(conn, monkeypatch)
    sql = "\n".join(statement for statement, _ in conn.statements)

    assert "INSERT INTO app.instance_claims" in sql
    assert "INSERT INTO app.instance_members" in sql
    assert "INSERT INTO app.organizations" in sql
    assert "INSERT INTO app.org_members" in sql
    assert "INSERT INTO app.projects" in sql
    assert "INSERT INTO app.project_members" in sql
    assert "SET state = 'consumed'" in sql
    assert spec.command_type == "instance.claim"
    assert spec.actor == "person_claimant"
    assert spec.effective_org_id is None
    assert spec.confirmation_mode == "human"
    assert spec.confirmation_reference == "econf_claim"
    assert "ecfs_server_secret" not in repr(spec)
    assert "b" * 48 not in str(spec.request_payload)
    assert result.audit_event_id == "audit_claim"
    assert result.outbox_event_id == "opout_claim"
    assert result.org_id.startswith("org_")
    assert result.project_id.startswith("proj_")
    assert result.journey_id == "setup_claim"
    assert conn.transaction_successes == 1


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("exchange_available", False),
        ("singleton_winner", False),
        ("consume_winner", False),
    ],
)
def test_claim_fails_closed_and_rolls_back_partial_authority(monkeypatch, attribute, value):
    conn = _Connection()
    setattr(conn, attribute, value)

    with pytest.raises(SelfHostedClaimUnavailable, match="claim unavailable"):
        _claim(conn, monkeypatch)

    assert conn.transaction_rollbacks == 1
    assert conn.transaction_successes == 0


def test_claim_rejects_hosted_mode_and_noncanonical_identity(monkeypatch):
    conn = _Connection()
    with pytest.raises(SelfHostedClaimValidationError, match="self_hosted"):
        claim_self_hosted_instance(
            conn,
            deployment_mode="hosted",
            bootstrap_exchange_bearer="x" * 48,
            claimant_person_id="person_claimant",
            organization_name="Org",
            organization_slug="org",
            project_name="Project",
            project_slug="project",
            idempotency_key="claim-1",
            confirmation=_confirmation(idempotency_key="claim-1"),
        )

    with pytest.raises(SelfHostedClaimValidationError, match="canonical"):
        claim_self_hosted_instance(
            conn,
            deployment_mode="self_hosted",
            bootstrap_exchange_bearer="x" * 48,
            claimant_person_id="raw-subject",
            organization_name="Org",
            organization_slug="org",
            project_name="Project",
            project_slug="project",
            idempotency_key="claim-1",
            confirmation=_confirmation(idempotency_key="claim-1"),
        )


def test_migration_has_singleton_hash_expiry_and_immutable_claim():
    sql = (
        __import__("pathlib")
        .Path("infra/nango/migrations/114_self_hosted_instance_claim.sql")
        .read_text(encoding="utf-8")
    )
    assert "bearer_hash" in sql
    assert "expires_at" in sql
    assert "singleton_key = 1" in sql
    assert "instance claim is immutable" in sql
    assert "operation_id" in sql
    assert "instance_bootstrap_exchange_sessions" in sql
