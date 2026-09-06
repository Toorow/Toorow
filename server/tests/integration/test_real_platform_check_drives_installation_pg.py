"""AI-206 -- the chain driven by the REAL check, not an injected one.

WHY THIS FILE EXISTS BESIDE `test_api_connector_owes_no_domain_pg.py`. That guard
proved the six Postgres locks were gone, and it did so with a hand-written
passing check:

    checks=[lambda: (True, "auth_check", "account discovery reached the provider")]

Which is the right shape for what it proves -- the triggers, the actor class, the
evidence class, the absence of a routing contract -- and says nothing about
whether the code that BUILDS checks produces one. It could not: `_build_checks`
returned `[]` until 2026-08-17, so no installation had ever reached READY through
the path a deployment actually takes.

So this file drives `connector_verification_api._build_checks` itself. Two
directions, because a check that cannot fail proves nothing either:

* fully configured platform -> the check passes, the installation reaches READY,
  and the evidence is an `auth_check` with no domain config;
* the platform prerequisite removed -> the same connector does NOT reach READY,
  and the ledger says `dependency_unavailable` rather than inventing a pass.

Live Postgres and the owner role: four of the six locks were triggers, so a
mocked connection cannot see them.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_OWNER_DSN"),
    reason="TEST_POSTGRES_OWNER_DSN not set -- live Postgres trigger test skipped",
)

_GOOGLE_ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "client-EXAMPLE",
    "GOOGLE_OAUTH_CLIENT_SECRET": "secret-EXAMPLE",
    "GOOGLE_OAUTH_REDIRECT_URI": "https://example.com/oauth/callback",
}


def _conn():
    import psycopg  # noqa: PLC0415

    return psycopg.connect(os.environ["TEST_POSTGRES_OWNER_DSN"], connect_timeout=5)


def _unique(prefix: str) -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}-{ULID()}".lower()


#: A CONNECTOR THE REGISTRY KNOWS (AI-206, 2026-08-17). `run_verification` asks
#: `core.connector_family` whether this connector owes a domain routing contract,
#: and the registry is what answers. A synthetic name is not in it, so it would be
#: classified as a transport and the run refused -- which is correct of the code
#: and useless to this test. Each test still gets a row of its own, by taking a
#: fresh ENVIRONMENT rather than a fresh name.
_MODULE_CONNECTOR = "linkedin-ads"


def _drive(environment: str, *, declared_path: str) -> tuple:
    """Install, then verify with the REAL `_build_checks`. Return the persisted row."""
    from unittest.mock import patch  # noqa: PLC0415

    from core import connector_verification_api as api  # noqa: PLC0415
    from core.connector_installation import apply_installation  # noqa: PLC0415
    from core.connector_verification import run_verification  # noqa: PLC0415

    connector_name = _MODULE_CONNECTOR
    conn = _conn()
    try:
        installed = apply_installation(
            conn, environment=environment, connector_name=connector_name,
            responsible_actor=None, blocking_cause=None, actor="person_TEST",
            idempotency_key=f"{environment}-install", host_context={}, trace_id=None,
            requires_domain=False,
        )
        conn.commit()
        assert installed["state"] == "VERIFYING"

        # The DECLARED PATH is what the check branches on, and it is the only
        # thing stubbed here -- each test exercises one path independently of what
        # this particular module happens to declare on disk. Everything downstream
        # of it is the real code.
        with patch.object(api, "_declared_auth_path", return_value=declared_path):
            checks = api._build_checks(
                connector_name=connector_name, environment=environment
            )
            run_verification(
                conn, environment=environment, connector_name=connector_name,
                checks=checks, actor="person_TEST",
                idempotency_key=f"{environment}-verify",
                host_context={}, trace_id=None,
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, blocking_cause, responsible_actor,"
                " last_verified_at IS NOT NULL"
                " FROM app.connector_installations"
                " WHERE environment = %s AND connector_name = %s",
                (environment, connector_name),
            )
            installation = cur.fetchone()
            cur.execute(
                "SELECT outcome, evidence_class, domain_config_id, blocking_reason"
                " FROM app.connector_verification_runs"
                " WHERE environment = %s AND connector_name = %s",
                (environment, connector_name),
            )
            run = cur.fetchone()
    finally:
        conn.close()
    return installation, run


def test_the_real_check_carries_a_module_connector_to_ready(monkeypatch):
    """The path a deployment takes, end to end, with nothing injected."""
    for name, value in _GOOGLE_ENV.items():
        monkeypatch.setenv(name, value)

    installation, run = _drive(_unique("t206-real"), declared_path="google_direct")

    state, cause, actor_class, verified = installation
    assert state == "READY"
    assert cause is None
    assert verified is True
    assert actor_class == "automated"

    outcome, evidence_class, domain_config_id, blocking_reason = run
    assert (outcome, evidence_class) == ("passed", "auth_check")
    assert domain_config_id is None
    assert blocking_reason is None


def test_the_same_check_refuses_when_the_platform_is_not_configured(monkeypatch):
    """A check that cannot fail proves nothing, so the failure is proven too.

    Same connector shape, same code path, one prerequisite removed: the
    installation must NOT reach READY, and the ledger must name the bounded
    cause instead of a pass nobody earned.
    """
    for name in _GOOGLE_ENV:
        monkeypatch.delenv(name, raising=False)

    installation, run = _drive(_unique("t206-unconf"), declared_path="google_direct")

    state, _cause, _actor_class, _verified = installation
    assert state != "READY", (
        "an unconfigured platform advanced an installation to READY: the check "
        "either passed without its prerequisite or was not evaluated at all"
    )

    outcome, evidence_class, domain_config_id, blocking_reason = run
    assert outcome != "passed"
    assert evidence_class == "auth_check"
    assert domain_config_id is None
    assert blocking_reason == "dependency_unavailable"
