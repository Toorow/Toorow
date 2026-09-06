"""AI-206 -- the family decides who owes a routing contract, not the current state.

WHAT THE PREVIOUS REPAIR LEFT. Migration 267 let an installation that owes no
domain open at VERIFYING, and `run_verification` was taught to accept a missing
domain config -- but it read the permission off the TRANSIENT STATE::

    if current_state != "VERIFYING":
        raise ConnectorVerificationUnavailable("active domain configuration is required")

That is true for exactly one instant: the moment after `apply_installation`. The
first passing run moves the same row to READY, and from then on every further
`POST /verify` for that connector is refused for not having a domain it never
owed. Three consequences, all the same defect:

  * story 38.4 is titled CONTINUOUS verification and the evidence row carries a
    `ttl_seconds`. The re-run that TTL exists for cannot happen for any of the
    39 module connectors.
  * a platform prerequisite that disappears (an OAuth client rotated out, a Nango
    secret removed) can never degrade the installation: READY is the one state
    `_DEGRADABLE` allows to leave, and READY is refused entry.
  * `_SAFE_NEXT_ACTIONS["DEGRADED"]` offers « resolve cause, then advance to
    READY or VERIFYING », a gesture the endpoint would then refuse.

NOTHING IS MOCKED HERE, and that is the point. The connector names are the real
ones -- `google-ads` and `gsc` ship as modules, `managed_feed` is the inbound
family's only member and is not a module -- so `core.connector_family` is asked
the same question `apply_installation`'s caller asks, and answers it from the
registry. A test that stubbed the family answer would prove the branch and not
the derivation. Each test takes its own `environment` so the three installations
are three rows.

Live Postgres and the owner role: the write is guarded by two triggers
(`protect_connector_installation`, `validate_connector_verification_run_write`),
so a mocked connection sees neither.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_OWNER_DSN"),
    reason="TEST_POSTGRES_OWNER_DSN not set -- live Postgres trigger test skipped",
)


def _conn():
    import psycopg  # noqa: PLC0415

    return psycopg.connect(os.environ["TEST_POSTGRES_OWNER_DSN"], connect_timeout=5)


def _environment(prefix: str) -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}-{ULID()}".lower()


def _passing():
    return lambda: (True, "auth_check", "")


def _failing():
    return lambda: (False, "auth_check", "dependency_unavailable")


def _install(conn, environment: str, connector_name: str, *, requires_domain: bool) -> dict:
    from core.connector_installation import apply_installation  # noqa: PLC0415

    installed = apply_installation(
        conn,
        environment=environment,
        connector_name=connector_name,
        responsible_actor=None,
        blocking_cause=None,
        actor="person_TEST",
        idempotency_key=f"{environment}-install",
        host_context={},
        trace_id=None,
        requires_domain=requires_domain,
    )
    conn.commit()
    return installed


def _verify(conn, environment: str, connector_name: str, check, suffix: str) -> dict:
    from core.connector_verification import run_verification  # noqa: PLC0415

    read_model = run_verification(
        conn,
        environment=environment,
        connector_name=connector_name,
        checks=[check],
        actor="person_TEST",
        idempotency_key=f"{environment}-{suffix}",
        host_context={},
        trace_id=None,
    )
    conn.commit()
    return read_model


def _row(conn, environment: str, connector_name: str) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, blocking_cause, responsible_actor"
            " FROM app.connector_installations"
            " WHERE environment = %s AND connector_name = %s",
            (environment, connector_name),
        )
        return cur.fetchone()


def _runs(conn, environment: str, connector_name: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, evidence_class, domain_config_id"
            " FROM app.connector_verification_runs"
            " WHERE environment = %s AND connector_name = %s"
            " ORDER BY created_at",
            (environment, connector_name),
        )
        return list(cur.fetchall())


def test_a_ready_module_connector_can_be_verified_again_and_degrades():
    """READY is not the end of verification for a connector that owes no domain."""
    environment = _environment("t206-again")
    connector_name = "google-ads"
    conn = _conn()
    try:
        assert _install(
            conn, environment, connector_name, requires_domain=False
        )["state"] == "VERIFYING"
        assert _verify(
            conn, environment, connector_name, _passing(), "v1"
        )["last_outcome"] == "passed"
        assert _row(conn, environment, connector_name)[0] == "READY"

        # The re-run. Before the family resolver this raised
        # ConnectorVerificationUnavailable("active domain configuration is
        # required") and no second evidence row was ever written.
        second = _verify(conn, environment, connector_name, _failing(), "v2")
        assert second["last_outcome"] == "degraded"

        state, cause, actor_class = _row(conn, environment, connector_name)
        assert state == "DEGRADED"
        assert cause == "dependency_unavailable"
        assert actor_class == "platform_admin"

        runs = _runs(conn, environment, connector_name)
        assert len(runs) == 2, "the re-run wrote no evidence"
        assert runs[1] == ("degraded", "auth_check", None)
    finally:
        conn.close()


def test_a_degraded_module_connector_can_recover_to_ready():
    """The gesture `_SAFE_NEXT_ACTIONS['DEGRADED']` names has to actually work."""
    environment = _environment("t206-recover")
    connector_name = "gsc"
    conn = _conn()
    try:
        _install(conn, environment, connector_name, requires_domain=False)
        _verify(conn, environment, connector_name, _passing(), "v1")
        _verify(conn, environment, connector_name, _failing(), "v2")
        assert _row(conn, environment, connector_name)[0] == "DEGRADED"

        third = _verify(conn, environment, connector_name, _passing(), "v3")
        assert third["last_outcome"] == "passed"

        state, cause, actor_class = _row(conn, environment, connector_name)
        assert (state, cause, actor_class) == ("READY", None, "automated")
        assert len(_runs(conn, environment, connector_name)) == 3
    finally:
        conn.close()


#: What a deployment holds so a `google_direct` connector's platform check can
#: pass. Placeholder values: the check asks whether the configuration LOADS, never
#: whether the credential is accepted -- that is a per-project fact.
_GOOGLE_ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "client-EXAMPLE",
    "GOOGLE_OAUTH_CLIENT_SECRET": "secret-EXAMPLE",
    "GOOGLE_OAUTH_REDIRECT_URI": "https://example.com/oauth/callback",
}


def test_a_verified_module_connector_pins_its_contract(monkeypatch):
    """The whole chain, in the order `_post_connector_verify` walks it.

    apply -> the REAL `_build_checks` -> `run_verification` -> the contract pin.
    Nothing injected but the three platform OAuth variables a deployment holds,
    because the point is that the pin is REACHED: the endpoint calls
    `snapshot_verified_connector_contract` before its `conn.commit()`, so anything
    that raises there discards a verification that had already passed and answers
    500 « Verification failed » -- a message that would be false.
    """
    from core import connector_verification_api as api  # noqa: PLC0415
    from core.data_identities import (  # noqa: PLC0415
        snapshot_verified_connector_contract,
    )

    for name, value in _GOOGLE_ENV.items():
        monkeypatch.setenv(name, value)

    environment = _environment("t206-pin")
    connector_name = "google-ads"
    conn = _conn()
    try:
        _install(conn, environment, connector_name, requires_domain=False)

        checks = api._build_checks(
            connector_name=connector_name, environment=environment
        )
        assert len(checks) == 1, "_build_checks produced no platform check"

        read_model = _verify(conn, environment, connector_name, checks[0], "v1")
        assert read_model["last_outcome"] == "passed"
        assert read_model["evidence_class"] == "auth_check"

        pinned = snapshot_verified_connector_contract(
            conn,
            environment=environment,
            connector_id=connector_name,
            actor="person_TEST",
        )
        conn.commit()
        assert pinned is not None

        with conn.cursor() as cur:
            cur.execute(
                "SELECT v.connector_id, v.version_number,"
                " v.validation_evidence->>'status', r.outcome, r.evidence_class"
                " FROM app.connector_contract_versions v"
                " JOIN app.connector_verification_runs r"
                "   ON r.id = v.verification_run_id"
                " WHERE v.environment = %s",
                (environment,),
            )
            rows = cur.fetchall()
        assert rows == [(connector_name, 1, "validated", "passed", "auth_check")]
    finally:
        conn.close()


def test_the_transport_family_still_owes_its_domain():
    """The requirement is not removed -- it is moved to the family that has one.

    `managed_feed` is the inbound family's only member and is not a module, so
    the derivation answers « owes a routing contract » with nothing stubbed. Its
    verification must still be refused without an active domain config: a routing
    verdict with no routing contract is evidence about nothing.
    """
    from core.connector_verification import (  # noqa: PLC0415
        ConnectorVerificationUnavailable,
        run_verification,
    )

    environment = _environment("t206-transport")
    connector_name = "managed_feed"
    conn = _conn()
    try:
        # Opened at VERIFYING on purpose: under the old state proxy this was the
        # one state that skipped the domain requirement, so a transport parked
        # there would have been verified with no routing contract at all.
        _install(conn, environment, connector_name, requires_domain=False)
        with pytest.raises(ConnectorVerificationUnavailable):
            run_verification(
                conn,
                environment=environment,
                connector_name=connector_name,
                checks=[_passing()],
                actor="person_TEST",
                idempotency_key=f"{environment}-v1",
                host_context={},
                trace_id=None,
            )
        conn.rollback()
        assert _runs(conn, environment, connector_name) == []
    finally:
        conn.close()
