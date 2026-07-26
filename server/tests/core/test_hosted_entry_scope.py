from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from core.entry_confirmations import (
    HOSTED_ENTRY_COMMAND,
    ConsumedEntryConfirmation,
    canonical_payload_hash,
)
from core.hosted_entry_scope import (
    HostedEntryScopeUnavailable,
    HostedEntryScopeValidationError,
    create_hosted_entry_scope,
)
from core.operations import OperationResult


def _confirmation(
    *, actor: str = "person_entry", idempotency_key: str = "entry-scope-request-1"
) -> ConsumedEntryConfirmation:
    payload = {
        "organization_name": "Entry organization",
        "organization_slug": "entry-organization",
        "project_name": "First project",
        "project_slug": "first-project",
        "currency": "EUR",
        "timezone": "Europe/Paris",
    }
    return ConsumedEntryConfirmation(
        confirmation_id="econf_1",
        command_type=HOSTED_ENTRY_COMMAND,
        actor_person_id=actor,
        payload_hash=canonical_payload_hash(HOSTED_ENTRY_COMMAND, payload),
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

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.conn.statements.append((normalized, params))
        if normalized.startswith("SELECT id FROM app.persons"):
            self._row = (self.conn.person_id,) if self.conn.person_exists else None
        elif normalized.startswith("SELECT 1 FROM app.org_members"):
            self._row = (1,) if self.conn.has_active_membership else None
        elif normalized.startswith("SELECT invitation.id"):
            self._row = (self.conn.invitation_id,) if self.conn.entitlement_available else None
        elif normalized.startswith("INSERT INTO app.hosted_entry_scope_consumptions"):
            self._row = ("entryscope_winner",) if self.conn.consumption_winner else None
        elif self.conn.fail_on_org_insert and normalized.startswith(
            "INSERT INTO app.organizations"
        ):
            raise RuntimeError("injected write failure")
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
        self.person_id = "person_entry"
        self.person_exists = True
        self.has_active_membership = False
        self.invitation_id = "invite_entry"
        self.entitlement_available = True
        self.consumption_winner = True
        self.fail_on_org_insert = False

    def transaction(self):
        return _Transaction(self)

    def cursor(self):
        return _Cursor(self)


def _create(conn, monkeypatch):
    captured = {}

    def execute_operation(connection, spec, *, mutation):
        captured["spec"] = spec
        changed = mutation(connection, "op_entry_scope")
        return OperationResult(
            operation_id="op_entry_scope",
            outcome=changed.outcome,
            result=changed.result,
            audit_event_id="audit_entry_scope",
            outbox_event_id="opout_entry_scope",
            replayed=False,
        )

    monkeypatch.setattr("core.hosted_entry_scope.execute_operation", execute_operation)
    monkeypatch.setattr(
        "core.setup_responsibilities.bootstrap_journey_from_acceptance",
        lambda *_args, **_kwargs: "setup_entry",
    )
    result = create_hosted_entry_scope(
        conn,
        deployment_mode="hosted",
        person_id="person_entry",
        organization_name="Entry organization",
        organization_slug="entry-organization",
        project_name="First project",
        project_slug="first-project",
        idempotency_key="entry-scope-request-1",
        confirmation=_confirmation(),
    )
    return result, captured["spec"]


def test_accepted_entry_creates_one_scope_and_transactional_evidence(monkeypatch):
    conn = _Connection()
    result, spec = _create(conn, monkeypatch)
    sql = "\n".join(statement for statement, _ in conn.statements)

    assert "invitation.org_id IS NULL" in sql
    assert "invitation.state = 'accepted'" in sql
    assert "exchange.person_id = %s" in sql
    assert "INSERT INTO app.hosted_entry_scope_consumptions" in sql
    assert "INSERT INTO app.organizations" in sql
    assert "INSERT INTO app.org_members" in sql
    assert "INSERT INTO app.projects" in sql
    assert "INSERT INTO app.project_members" in sql
    assert spec.command_type == "hosted.entry_scope.create"
    assert spec.actor == "person_entry"
    assert spec.effective_org_id is None
    assert spec.idempotency_key != "entry-scope-request-1"
    assert len(spec.idempotency_key) == 64
    assert spec.confirmation_mode == "human"
    assert spec.confirmation_reference == "econf_1"
    assert "ecfs_server_secret" not in repr(spec)
    assert result.invitation_id == "invite_entry"
    assert result.audit_event_id == "audit_entry_scope"
    assert result.outbox_event_id == "opout_entry_scope"
    assert result.journey_id == "setup_entry"
    assert result.next_url == f"/p/{result.project_id}/overview/getting-started"
    assert conn.transaction_successes == 1


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("person_exists", False),
        ("entitlement_available", False),
        ("consumption_winner", False),
    ],
)
def test_scope_fails_closed_without_entry_or_for_a_second_scope(monkeypatch, attribute, value):
    conn = _Connection()
    setattr(conn, attribute, value)

    with pytest.raises(HostedEntryScopeUnavailable, match="scope unavailable"):
        _create(conn, monkeypatch)

    sql = "\n".join(statement for statement, _ in conn.statements)
    if attribute in {"person_exists", "entitlement_available"}:
        assert "INSERT INTO app.organizations" not in sql
    assert conn.transaction_rollbacks == 1
    assert conn.transaction_successes == 0


def test_invited_membership_does_not_consume_the_self_service_entry_cap(monkeypatch):
    conn = _Connection()
    conn.has_active_membership = True

    result, _spec = _create(conn, monkeypatch)

    assert result.org_id.startswith("org_")
    assert not any("SELECT 1 FROM app.org_members" in sql for sql, _ in conn.statements)

def test_failure_after_consumption_rolls_back_all_authority(monkeypatch):
    conn = _Connection()
    conn.fail_on_org_insert = True

    with pytest.raises(RuntimeError, match="injected write failure"):
        _create(conn, monkeypatch)

    assert conn.transaction_rollbacks == 1
    assert conn.transaction_successes == 0


def test_hosted_mode_and_canonical_person_are_mandatory():
    conn = _Connection()
    common = {
        "conn": conn,
        "organization_name": "Org",
        "organization_slug": "org",
        "project_name": "Project",
        "project_slug": "project",
        "idempotency_key": "entry-1",
        "confirmation": _confirmation(idempotency_key="entry-1"),
    }
    with pytest.raises(HostedEntryScopeValidationError, match="hosted mode"):
        create_hosted_entry_scope(
            deployment_mode="self_hosted",
            person_id="person_entry",
            **common,
        )
    with pytest.raises(HostedEntryScopeValidationError, match="canonical"):
        create_hosted_entry_scope(
            deployment_mode="hosted",
            person_id="raw-subject",
            **common,
        )
    assert conn.statements == []


def test_migration_persists_one_immutable_consumption_per_person_and_entry():
    sql = Path("infra/nango/migrations/115_hosted_entry_scope.sql").read_text(encoding="utf-8")
    assert "person_id     TEXT        NOT NULL UNIQUE" in sql
    assert "invitation_id TEXT        NOT NULL UNIQUE" in sql
    assert "invitation.org_id IS NULL" in sql
    assert "invitation.state = 'accepted'" in sql
    assert "exchange.person_id = NEW.person_id" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "hosted ENTRY consumption is immutable" in sql
