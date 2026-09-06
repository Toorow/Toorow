"""2026-08-30 -- one consent, several connectors, and a selection that guessed.

`data-path.md`, "Incomplete if" [4]: *un consentement couvre N connecteurs et la
selection n'en distingue qu'un*. Measured that day, on this schema:

  * `app.connection_account_scope` carries NO connector column -- id,
    connection_ref_id, account_id, account_label, state, verified_at,
    selected_by, created_at, updated_at, selection_path, and nothing else;
  * the connector lives one join away, on
    `app.credential_accounts.discovered_for_connector` (migration 210), keyed
    `(credential_id, external_account_id)` -- which is the pair the scope row
    spells `(connection_ref_id, account_id)`;
  * migration 211 added `app.datastreams.source_account_id` WITHOUT NOT NULL,
    so a connector Datastream that binds no account is legal and reachable.

WHY THIS NEEDS A REAL DATABASE. The whole repair is a predicate: a LEFT JOIN
onto `credential_accounts` plus `account_connector_sql`, whose NULL branch is
the difference between "a pre-210 account still resolves" and "a pre-210
account disappears". A fake cursor agrees with any predicate -- it returns the
rows the test already chose. Only Postgres can say whether the join, the NULL
and the `= ANY(%s)` behave as written.

Everything runs inside one transaction and is rolled back; nothing is committed.
"""

from __future__ import annotations

import os
import uuid

import pytest

TEST_ORG_ID = "org_test_fixture"
CONNECTOR = "gsc"
OTHER_CONNECTOR = "google-analytics"

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- the connector predicate needs a live Postgres",
)

pytestmark = [_skip_without_dsn, pytest.mark.pg_owner]


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class _Consent:
    """One Google authorization, the accounts it verified, one Datastream."""

    def __init__(self, conn, project_id: str, connection_id: str):
        self.conn = conn
        self.project_id = project_id
        self.connection_id = connection_id

    def verify(self, external_account_id: str, connector: str | None) -> str:
        """Discover + verify one account, tagged with the Connector that found it."""
        source_account_id = _id("sacc_")
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.credential_accounts "
                "  (credential_id, external_account_id, label, source_account_id, "
                "   discovered_for_connector) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    self.connection_id,
                    external_account_id,
                    external_account_id,
                    source_account_id,
                    connector,
                ),
            )
            cur.execute(
                "INSERT INTO app.connection_account_scope "
                "  (id, connection_ref_id, account_id, account_label, state, verified_at) "
                "VALUES (%s, %s, %s, %s, 'ready', NOW())",
                (
                    _id("ascope_"),
                    self.connection_id,
                    external_account_id,
                    external_account_id,
                    ),
            )
        return source_account_id

    def datastream(self, *, source_account_id: str | None) -> str:
        stream_id = _id("ds_")
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.datastreams "
                "  (id, project_id, name, module_name, connection_ref_id, "
                "   source_account_id, enabled, schedule_mode, created_by, org_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'nightly', 'test', %s)",
                (
                    stream_id,
                    self.project_id,
                    stream_id,
                    CONNECTOR,
                    self.connection_id,
                    source_account_id,
                    TEST_ORG_ID,
                ),
            )
        return stream_id


@pytest.fixture
def consent(live_postgres):
    conn = live_postgres
    project_id = _id("proj_")
    connection_id = _id("cref_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'account-selection-test', %s)",
            (project_id, project_id, project_id.replace("_", "-").lower(), TEST_ORG_ID),
        )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "  (id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
            # `provider='google'` is the name of NO module, and that is the point:
            # one consent screen, seven connectors (migration 210's own comment).
            "VALUES (%s, 'google', %s, %s, %s, 'owner@example.com')",
            (connection_id, connection_id, project_id, TEST_ORG_ID),
        )
    yield _Consent(conn, project_id, connection_id)
    conn.rollback()


def _resolve(consent: _Consent, datastream_id: str | None):
    from core.queue import _resolve_selected_account

    return _resolve_selected_account(
        consent.conn, consent.connection_id, datastream_id, connector=CONNECTOR
    )


def test_the_bound_account_wins_even_with_three_verified(consent):
    """The operator's answer is the answer; nothing downstream reinterprets it."""
    bound = consent.verify("sc-domain:example.com", CONNECTOR)
    consent.verify("sc-domain:other.example", CONNECTOR)
    consent.verify("properties/222", OTHER_CONNECTOR)
    stream = consent.datastream(source_account_id=bound)

    assert _resolve(consent, stream) == "sc-domain:example.com"


def test_one_ready_account_of_the_connector_resolves(consent):
    """Unbound, and only one thing this Connector could read: that is not a guess.

    The consent also holds a GA4 property. Before the repair the fallback took
    the freshest row of the whole authorization and could hand `properties/222`
    to a Search Console pull -- the failure measured on 2026-08-01.
    """
    consent.verify("sc-domain:example.com", CONNECTOR)
    consent.verify("properties/222", OTHER_CONNECTOR)
    stream = consent.datastream(source_account_id=None)

    assert _resolve(consent, stream) == "sc-domain:example.com"


def test_a_pre_210_account_is_unknown_not_foreign(consent):
    """`discovered_for_connector IS NULL` stays admissible, per migration 210."""
    consent.verify("sc-domain:example.com", None)
    stream = consent.datastream(source_account_id=None)

    assert _resolve(consent, stream) == "sc-domain:example.com"


def test_two_ready_accounts_of_the_connector_refuse(consent):
    """Two Search Console sites, no binding: there is no answer, only a refusal."""
    from core.queue import AccountSelectionAmbiguous

    consent.verify("sc-domain:a.example", CONNECTOR)
    consent.verify("sc-domain:b.example", CONNECTOR)
    consent.verify("properties/222", OTHER_CONNECTOR)
    stream = consent.datastream(source_account_id=None)

    with pytest.raises(AccountSelectionAmbiguous) as raised:
        _resolve(consent, stream)

    # The GA4 property is NOT one of the candidates: this connector cannot read
    # it, so it can neither be chosen nor create the ambiguity.
    assert sorted(raised.value.accounts) == ["sc-domain:a.example", "sc-domain:b.example"]
    assert raised.value.connector == CONNECTOR


def test_only_the_other_connectors_account_is_verified_no_answer(consent):
    """A GA4 property alone answers NOTHING to a Search Console pull.

    Not a refusal either: nothing was ever selected for this Connector, and the
    enqueue gate already answers that with `account_not_selected` -- same
    gesture, choose an account.
    """
    consent.verify("properties/222", OTHER_CONNECTOR)
    stream = consent.datastream(source_account_id=None)

    assert _resolve(consent, stream) is None


def test_a_pending_scope_is_not_a_candidate(consent):
    """`state='pending_account_selection'` was offered, never verified."""
    consent.verify("sc-domain:example.com", CONNECTOR)
    with consent.conn.cursor() as cur:
        cur.execute(
            "UPDATE app.connection_account_scope SET state='pending_account_selection' "
            "WHERE connection_ref_id=%s",
            (consent.connection_id,),
        )
    stream = consent.datastream(source_account_id=None)

    assert _resolve(consent, stream) is None
