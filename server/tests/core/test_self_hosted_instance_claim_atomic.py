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

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
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


# EVERY STATEMENT the self-hosted claim seam issues on a tested path, NAMED.
# Twelve of the twenty-five used to fall into an `else` that answered no rows:
# the two `LOCK TABLE`s, the two `revoked` rotations, the capability INSERT,
# every write of the first tenant scope (`instance_members`, `org_members`,
# `projects`, `project_preferences`) and every write of the Getting Started
# journey bootstrap (`setup_journeys`, `setup_tasks`, `setup_task_events`).
# None of their results is read, so the silence answered correctly BY LUCK --
# and a fake that is right by luck stays quiet when the query beside it moves
# (AI-317).
#
# Each `UPDATE` pair names the relation AND the state it writes: the three
# capability updates ('revoked', 'exchanged', 'consumed') and the two exchange
# updates ('revoked', 'consumed') would otherwise collapse onto one fragment.
_CLAIM = StatementInventory(
    "_Cursor (self-hosted instance claim)",
    # provision_bootstrap_capability
    lock_bootstrap_tables="lock table app.instance_bootstrap_capabilities",
    instance_claim_probe="select 1 from app.instance_claims limit 1",
    # `bootstrap_exchange_session_is_ready` carries three EXISTS in one
    # statement, one of which reads app.organizations -- so it is declared
    # before the standalone non-seed probe and pinned on its own first relation.
    exchange_session_ready="select exists ( select 1 from app.instance_bootstrap_exchange_sessions",
    non_seed_organizations="select exists ( select 1 from app.organizations",
    exchange_revoked=(
        "update app.instance_bootstrap_exchange_sessions",
        "set state = 'revoked'",
    ),
    capability_revoked=(
        "update app.instance_bootstrap_capabilities",
        "set state = 'revoked'",
    ),
    capability_insert="insert into app.instance_bootstrap_capabilities",
    # exchange_bootstrap_capability
    capability_by_hash="select id, expires_at from app.instance_bootstrap_capabilities",
    capability_exchanged=(
        "update app.instance_bootstrap_capabilities",
        "set state = 'exchanged'",
    ),
    exchange_insert="insert into app.instance_bootstrap_exchange_sessions",
    # claim_self_hosted_instance -> mutation
    exchange_for_update="select exchange.id, capability.id",
    lock_scope_tables="lock table app.organizations, app.projects",
    claim_insert="insert into app.instance_claims",
    instance_member_insert="insert into app.instance_members",
    organization_insert="insert into app.organizations",
    org_member_insert="insert into app.org_members",
    project_insert="insert into app.projects",
    project_preferences_insert="insert into app.project_preferences",
    # Story 46.4 routed the claim through the ONE Getting Started bootstrap,
    # which reads the Project it is about to open a journey for.
    project_org="select org_id from app.projects",
    journey_probe="select id from app.setup_journeys",
    journey_insert="insert into app.setup_journeys",
    setup_task_event_insert="insert into app.setup_task_events",
    setup_task_insert="insert into app.setup_tasks",
    exchange_consumed=(
        "update app.instance_bootstrap_exchange_sessions",
        "set state = 'consumed'",
    ),
    capability_consumed=(
        "update app.instance_bootstrap_capabilities",
        "set state = 'consumed'",
    ),
)

# The statements that genuinely return NO RESULT SET. `description = None` is
# what psycopg reserves for exactly those; everywhere else it is DERIVED from
# the statement, never restated here.
_NO_RESULT_SET = frozenset(
    {
        "lock_bootstrap_tables",
        "exchange_revoked",
        "capability_revoked",
        "capability_insert",
        "capability_exchanged",
        "lock_scope_tables",
        "instance_member_insert",
        "organization_insert",
        "org_member_insert",
        "project_insert",
        "project_preferences_insert",
        "journey_insert",
        "setup_task_insert",
        "setup_task_event_insert",
        "exchange_consumed",
        "capability_consumed",
    }
)

# The only two projections `describe` refuses -- a parenthesis in the select
# list. psycopg labels a bare `SELECT EXISTS (...)` "exists", and the
# readiness conjunction of three EXISTS "?column?".
_NAMED_DESCRIPTION = {
    "non_seed_organizations": [("exists",)],
    "exchange_session_ready": [("?column?",)],
}


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None
        self.rowcount = 0
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.conn.statements.append((normalized, params))
        self.rowcount = 0
        statement = _CLAIM.match(normalized)
        self.description = (
            None
            if statement in _NO_RESULT_SET
            else _NAMED_DESCRIPTION.get(statement) or describe(normalized)
        )
        match statement:
            case "instance_claim_probe":
                self._row = (1,) if self.conn.already_claimed else None
            case "non_seed_organizations":
                self._row = (self.conn.has_non_seed_organizations,)
            case "exchange_session_ready":
                self._row = (
                    self.conn.exchange_available
                    and not self.conn.already_claimed
                    and not self.conn.has_non_seed_organizations,
                )
            case "capability_by_hash":
                self._row = (
                    (self.conn.capability_id, self.conn.capability_expiry)
                    if self.conn.capability_available
                    else None
                )
            case "capability_exchanged":
                self._row = None
                self.rowcount = 1 if self.conn.exchange_winner else 0
            case "exchange_insert":
                self._row = (self.conn.exchange_expiry,)
            case "exchange_for_update":
                self._row = (
                    (self.conn.exchange_id, self.conn.capability_id)
                    if self.conn.exchange_available
                    else None
                )
            case "claim_insert":
                self._row = ("iclaim_winner",) if self.conn.singleton_winner else None
            case "organization_insert":
                self.conn.org_id = params[0] if params else self.conn.org_id
                self._row = None
            case "project_org":
                self._row = (self.conn.org_id,)
            case "journey_probe":
                # No journey exists yet for the Project the claim just created.
                self._row = None
            case "exchange_consumed" | "capability_consumed":
                self._row = None
                self.rowcount = 1 if self.conn.consume_winner else 0
            case (
                "lock_bootstrap_tables"
                | "lock_scope_tables"
                | "exchange_revoked"
                | "capability_revoked"
                | "capability_insert"
                | "instance_member_insert"
                | "org_member_insert"
                | "project_insert"
                | "project_preferences_insert"
                | "journey_insert"
                | "setup_task_insert"
                | "setup_task_event_insert"
            ):
                # Writes and locks the product issues without reading anything
                # back. Named so a REWRITE of one of them is a failure, not a
                # shrug.
                self._row = None
            case _:  # pragma: no cover - a name added to the inventory, unanswered
                raise _CLAIM.unknown(normalized)

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
        # Echoed back to the journey bootstrap; the real id is minted at runtime.
        self.org_id = "org_claim"
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
        # AI-77/AI-81: the confirmation payload records EUR and Europe/Paris as
        # the claimant's choice, so the call states them. They used to be
        # signature defaults, which is what let a fallback reach the row wearing
        # the label of a suggestion nobody made.
        currency="EUR",
        timezone_name="Europe/Paris",
    )
    return result, captured["spec"]


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: an unrecognised statement must NAME itself, not answer no rows.

    The old `else` answered `None`. Twelve of the seam's statements landed
    there -- every lock, every rotation, every INSERT of the first tenant scope
    and of the journey bootstrap -- and none of them is read, so the fake was
    right for no reason. The moment one of the reads BESIDE them is rewritten
    (the capability lookup, the singleton `RETURNING id`, the journey probe),
    the same `else` answers "no rows" and the claim fails closed in a test that
    is meant to prove it opens.
    """

    cursor = _Cursor(_Connection())
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute("SELECT owner_person_id FROM app.instance_claims WHERE singleton_key = 1")
    message = str(raised.value)
    assert "owner_person_id" in message
    assert "instance_claim_probe" in message


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
    # Story 46.4 retired `app.project_members`: the claimer holds the owner floor
    # through Organization membership, and 46.3 moved the Project defaults into
    # a `project_preferences` row.
    assert "INSERT INTO app.project_members" not in sql
    assert "INSERT INTO app.project_preferences" in sql
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
    # The single journey bootstrap mints the id; the invariant is that one was
    # opened, not which literal it carries.
    assert result.journey_id.startswith("setup_")
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
    from tests.conftest import REPO_ROOT

    sql = (REPO_ROOT / "infra/nango/migrations/114_self_hosted_instance_claim.sql").read_text(
        encoding="utf-8"
    )
    assert "bearer_hash" in sql
    assert "expires_at" in sql
    assert "singleton_key = 1" in sql
    assert "instance claim is immutable" in sql
    assert "operation_id" in sql
    assert "instance_bootstrap_exchange_sessions" in sql
