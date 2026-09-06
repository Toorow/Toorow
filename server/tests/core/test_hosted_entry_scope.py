from __future__ import annotations

import hashlib

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

from tests.conftest import REPO_ROOT
from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)


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


# EVERY statement the hosted ENTRY path issues, named (AI-317). The previous
# version of this fake answered four reads and one write and swept the other
# SEVEN into an `else` that set `self._row = None` -- silently, and with the
# PREVIOUS statement's answer still standing. Six of those seven are the rows
# this path exists to create:
#
#   app.org_members            hosted_entry_scope.py:273
#   app.projects               hosted_entry_scope.py:281
#   app.project_preferences    project_provenance.py:182 (via insert_project_preferences)
#   app.setup_journeys         getting_started.py:93
#   app.setup_tasks            getting_started.py:103   (x5, one per _TASKS step)
#   app.setup_task_events      getting_started.py:132   (x5)
#
# and the seventh is the `RETURNING id` of the consumption receipt read back at
# hosted_entry_scope.py:255. `test_accepted_entry_creates_one_scope_and_...`
# asserts three of those INSERTs appear in `conn.statements`, so the silence was
# never total -- but nothing forced the fake to KNOW them, and a rewrite of any
# of them would have gone on being answered.
#
# Declaration order is first match wins. `app.setup_task_events` is declared
# before `app.setup_tasks` only for the reader's benefit; the two table names do
# not contain one another.
_ENTRY_SCOPE = StatementInventory(
    "_Cursor (create_hosted_entry_scope)",
    person_lock="select id from app.persons",
    entitlement=("select invitation.id", "from app.invitations as invitation"),
    consumption_insert="insert into app.hosted_entry_scope_consumptions",
    organization_insert="insert into app.organizations",
    org_member_insert="insert into app.org_members",
    project_insert="insert into app.projects",
    project_preferences_insert="insert into app.project_preferences",
    # Story 46.4 routed every entry path through ONE idempotent Getting Started
    # bootstrap, which reads the Project it is about to open a journey for.
    project_org_lookup="select org_id from app.projects",
    journey_lookup="select id from app.setup_journeys",
    journey_insert="insert into app.setup_journeys",
    task_event_insert="insert into app.setup_task_events",
    task_insert="insert into app.setup_tasks",
)

# The writes that return NO result set. `description = None` is what psycopg
# reserves for exactly those; everywhere else it is DERIVED from the statement,
# so a moved projection cannot keep agreeing with a column list nothing reads.
_NO_RESULT_SET = frozenset(
    {
        "organization_insert",
        "org_member_insert",
        "project_insert",
        "project_preferences_insert",
        "journey_insert",
        "task_insert",
        "task_event_insert",
    }
)


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.conn.statements.append((normalized, params))
        statement = _ENTRY_SCOPE.match(normalized)
        self.description = None if statement in _NO_RESULT_SET else describe(normalized)
        match statement:
            case "person_lock":
                self._row = (self.conn.person_id,) if self.conn.person_exists else None
            case "entitlement":
                self._row = (
                    (self.conn.invitation_id,) if self.conn.entitlement_available else None
                )
            case "consumption_insert":
                self._row = ("entryscope_winner",) if self.conn.consumption_winner else None
            case "organization_insert":
                if self.conn.fail_on_org_insert:
                    raise RuntimeError("injected write failure")
                # The organization id is minted at runtime, so the fake echoes back
                # the one it was actually handed rather than guessing a literal.
                self.conn.org_id = params[0] if params else self.conn.org_id
                self._row = None
            case "project_org_lookup":
                self._row = (self.conn.org_id,)
            case "journey_lookup":
                # No journey exists yet, so the bootstrap mints one.
                self._row = None
            case (
                "org_member_insert"
                | "project_insert"
                | "project_preferences_insert"
                | "journey_insert"
                | "task_insert"
                | "task_event_insert"
            ):
                # Nothing to fetch, and psycopg would say so with
                # `description = None`, set above.
                self._row = None
            case _:  # pragma: no cover - a name added to the inventory, unanswered
                raise _ENTRY_SCOPE.unknown(normalized)

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
        # Kept for `test_invited_membership_does_not_consume_the_self_service_entry_cap`,
        # which asserts the path issues NO membership probe at all. The fake used
        # to hold a branch answering `SELECT 1 FROM app.org_members`; that
        # statement is not in the inventory because the product does not issue it,
        # and if it ever comes back the fake refuses it by name.
        self.has_active_membership = False
        self.invitation_id = "invite_entry"
        self.entitlement_available = True
        self.consumption_winner = True
        self.fail_on_org_insert = False
        self.org_id = "org_entry"

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
        # AI-77/AI-81: the confirmation payload above records that the operator
        # chose EUR and Europe/Paris, so the call has to say so. Before the
        # repair these were signature defaults, which made a platform fallback
        # indistinguishable from a human choice -- and the row then claimed a
        # source had suggested it.
        currency="EUR",
        timezone_name="Europe/Paris",
    )
    return result, captured["spec"]


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: an unrecognised statement must NAME itself, not answer no rows.

    The old `else` answered `None` -- which is precisely the answer this path
    reads as a DECISION three times over: "this person has no row", "no accepted
    ENTRY is left to consume", "another submission won the receipt". A moved
    query would have gone on producing the fail-closed verdict for the wrong
    reason, and `test_scope_fails_closed_without_entry_or_for_a_second_scope`
    would still have passed.
    """
    cursor = _Cursor(_Connection())
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT id FROM app.org_members WHERE org_id = %s AND identity = %s"
        )
    message = str(raised.value)
    assert "app.org_members where org_id" in message
    assert "person_lock" in message


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
    # Story 46.4 retired `app.project_members`; the entry path grants the owner
    # floor through Organization membership, and 46.3 made the Project defaults a
    # `project_preferences` row rather than two columns on `app.projects`.
    assert "INSERT INTO app.project_members" not in sql
    assert "INSERT INTO app.project_preferences" in sql
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
    # Story 46.4 routed this through the ONE idempotent journey bootstrap, which
    # mints the id. What matters is that a journey was opened, not its literal.
    assert result.journey_id.startswith("setup_")
    # Story 46.1 made every route organization-rooted and 46.4 made Getting
    # Started a global surface rather than an Overview section.
    assert result.next_url == (
        f"/org/{result.org_id}/project/{result.project_id}/getting-started"
    )
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
        "currency": "EUR",
        "timezone_name": "Europe/Paris",
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
    sql = (REPO_ROOT / "infra/nango/migrations/115_hosted_entry_scope.sql").read_text(
        encoding="utf-8"
    )
    assert "person_id     TEXT        NOT NULL UNIQUE" in sql
    assert "invitation_id TEXT        NOT NULL UNIQUE" in sql
    assert "invitation.org_id IS NULL" in sql
    assert "invitation.state = 'accepted'" in sql
    assert "exchange.person_id = NEW.person_id" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "hosted ENTRY consumption is immutable" in sql


def test_entry_scope_provisions_the_organization_warehouse(monkeypatch):
    """Regression, production incident 2026-07-27.

    The first real account ever created through this path got an organization and
    a project, and the console presented them as ready -- but neither
    org_<slug>_raw nor org_<slug>_marts existed, because this module never called
    provision_org_schemas at all. _create_org has done it since story 24.2; this
    path simply omitted it. An organization without its datasets is a shell: no
    data can ever land in it, so every downstream gate is blocked.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        "core.warehouse_tenancy.provision_org_schemas",
        lambda *, org_id, conn=None: calls.append(org_id) or {"status": "ok"},
    )

    conn = _Connection()
    result, _spec = _create(conn, monkeypatch)

    assert calls == [result.org_id], "the entry scope must provision its own warehouse"


def test_entry_scope_survives_an_unavailable_warehouse(monkeypatch):
    """Provisioning is non-blocking, exactly as in _create_org.

    A BigQuery cold start must not roll back an organization the person
    legitimately created: the scope is still returned, and the organization stays
    repairable through POST /api/organizations/{org_id}/provision-warehouse.
    """

    def _boom(*, org_id, conn=None):
        raise RuntimeError("warehouse unavailable")

    monkeypatch.setattr("core.warehouse_tenancy.provision_org_schemas", _boom)

    conn = _Connection()
    result, _spec = _create(conn, monkeypatch)

    assert result.org_id, "an unavailable warehouse must not fail the entry scope"
