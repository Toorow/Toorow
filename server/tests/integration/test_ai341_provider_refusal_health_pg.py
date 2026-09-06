"""Live-Postgres proof that a provider's refusal at pull time is a fact of health.

AI-341, ratified in execution-substrate.md (Decided 2026-08-31). Measured on the
reference project: six days of 403 permission_denied on every Analytics pull
while `app.connection_health` read `ok` -- the poll reads local token state and
cannot see what the provider refused, and the pull, which knew, wrote only
`pull_jobs`. These tests hold the four rules on a REAL database, because every
one of them lives in SQL (an ON CONFLICT branch, a sticky CASE): a double
answering on `"connection_health" in statement` would keep passing with the
CASE deleted.

Rules held here:
  1. the pull that learns writes -- `permission_denied` raises `provider_denied`
     naming the pull; `auth_expired`/`auth_revoked` raise `revoked`;
  2. the sweep never lifts a pull-raised red -- its `ok` keeps `provider_denied`,
     only `revoked` outranks it;
  3. a verified `ok` pull lifts `provider_denied` (and nulls its label), and
     leaves `revoked` alone;
  4. the refusal sentence for `provider_denied` names the provider gesture,
     never `reconnect`.
"""

from __future__ import annotations

import contextlib

import pytest

CREF = "cref_ai341"
PULL = "pull_ai341_denied"


@pytest.fixture()
def one_connection(live_postgres):
    """One org, one project, one google_direct credential, health `ok`."""
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES ('org_ai341','AI341','ai341','test') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, org_id, status, created_by) "
            "VALUES ('proj_ai341','AI341','proj-ai341','org_ai341','active','test') "
            "ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "(id, project_id, provider, nango_connection_id, owner_org_id, "
            " owner_identity, auth_path) "
            "VALUES (%s,'proj_ai341','google',NULL,'org_ai341','test','google_direct') "
            "ON CONFLICT (id) DO NOTHING",
            (CREF,),
        )
        cur.execute(
            "INSERT INTO app.connection_health (connection_ref_id, status, last_checked_at) "
            "VALUES (%s,'ok',NOW()) "
            "ON CONFLICT (connection_ref_id) DO UPDATE SET status='ok', "
            "  provider_denied_pull_id=NULL, provider_denied_at=NULL, "
            "  populate_failed_pull_id=NULL, populate_failed_verdict=NULL, "
            "  populate_failed_at=NULL",
            (CREF,),
        )
    yield conn


def _health(conn) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, provider_denied_pull_id, provider_denied_at "
            "FROM app.connection_health WHERE connection_ref_id = %s",
            (CREF,),
        )
        return cur.fetchone()


def _reroute_get_connection(monkeypatch, conn) -> None:
    """Route `core.db.get_connection` to the fixture connection, uncommitted.

    `_upsert_health` and `clear_connection_health_red` open their own
    connection; in this suite they must write into the same transaction the
    fixture rolls back, or the test would leave rows behind.
    """

    @contextlib.contextmanager
    def _handed(*_a, **_k):
        yield conn

    import core.db as db  # noqa: PLC0415

    monkeypatch.setattr(db, "get_connection", _handed)
    # Their `conn.commit()` must not end the fixture's transaction for real --
    # psycopg commits are fine here because the fixture TRUNCATEs nothing and
    # rolls back only what is left; keep the commit a no-op instead.
    monkeypatch.setattr(conn, "commit", lambda: None, raising=False)


# ── 1. the pull that learns writes ─────────────────────────────────────────────


def test_a_permission_denied_pull_raises_provider_denied_naming_the_pull(one_connection):
    from core.queue import _record_auth_red

    _record_auth_red(one_connection, CREF, PULL, "permission_denied")

    status, pull_id, raised_at = _health(one_connection)
    assert status == "provider_denied", f"the pull knew and health still says {status!r}"
    assert pull_id == PULL, "a red names the pull that raised it (migration 331)"
    assert raised_at is not None


def test_an_auth_expired_pull_raises_revoked_with_no_denied_label(one_connection):
    from core.queue import _record_auth_red

    _record_auth_red(one_connection, CREF, "pull_expired", "auth_expired")

    status, pull_id, raised_at = _health(one_connection)
    assert status == "revoked"
    assert pull_id is None and raised_at is None, (
        "the provider_denied label may never describe a status the row does not hold"
    )


def test_a_non_auth_class_leaves_health_alone(one_connection):
    from core.queue import _record_auth_red

    _record_auth_red(one_connection, CREF, "pull_transient", "provider_transient")

    status, _pull, _at = _health(one_connection)
    assert status == "ok", "a transient fault is not a fact of authorization health"


# ── 2. the sweep never lifts a pull-raised red ────────────────────────────────


def test_the_sweeps_ok_keeps_provider_denied_and_its_label(one_connection, monkeypatch):
    from datetime import datetime, timezone

    from core.health_poller import _upsert_health
    from core.queue import _record_auth_red

    _record_auth_red(one_connection, CREF, PULL, "permission_denied")
    _reroute_get_connection(monkeypatch, one_connection)

    _upsert_health(CREF, "ok", datetime.now(tz=timezone.utc), None)

    status, pull_id, _at = _health(one_connection)
    assert status == "provider_denied", (
        "the local poll cannot see what the provider refused; its ok must not lift the red"
    )
    assert pull_id == PULL, "the sticky branch keeps the label with the status"


def test_revoked_outranks_provider_denied_and_drops_its_label(one_connection, monkeypatch):
    from datetime import datetime, timezone

    from core.health_poller import _upsert_health
    from core.queue import _record_auth_red

    _record_auth_red(one_connection, CREF, PULL, "permission_denied")
    _reroute_get_connection(monkeypatch, one_connection)

    _upsert_health(CREF, "revoked", datetime.now(tz=timezone.utc), None)

    status, pull_id, raised_at = _health(one_connection)
    assert status == "revoked"
    assert pull_id is None and raised_at is None


# ── 3. only a proven read lifts ───────────────────────────────────────────────


def test_a_verified_ok_pull_lifts_provider_denied(one_connection, monkeypatch):
    from core.queue import _record_auth_red
    from core.verification import clear_connection_health_red

    _record_auth_red(one_connection, CREF, PULL, "permission_denied")
    _reroute_get_connection(monkeypatch, one_connection)

    clear_connection_health_red(CREF, evidence=f"pull {PULL} verified ok")

    status, pull_id, raised_at = _health(one_connection)
    assert status == "ok", "a verified read is the proof the provider serves data again"
    assert pull_id is None and raised_at is None


def test_a_verified_ok_pull_does_not_revive_revoked(one_connection, monkeypatch):
    from core.queue import _record_auth_red
    from core.verification import clear_connection_health_red

    _record_auth_red(one_connection, CREF, "pull_expired", "auth_revoked")
    _reroute_get_connection(monkeypatch, one_connection)

    clear_connection_health_red(CREF, evidence="a read that predates the death")

    status, _pull, _at = _health(one_connection)
    assert status == "revoked", "a dead authorization is not repaired by an old read"


# ── 4. the sentence names the true gesture ────────────────────────────────────


def test_the_provider_denied_refusal_never_says_reconnect(one_connection):
    from core.queue import _connection_unhealthy_refusal, _record_auth_red

    _record_auth_red(one_connection, CREF, PULL, "permission_denied")

    refusal = _connection_unhealthy_refusal(CREF, one_connection)

    assert refusal["code"] == "connection_unhealthy"
    message = refusal["message"].lower()
    assert "provider" in message and "restore" in message
    assert not message.startswith("this connection's authorization is no longer valid"), (
        "the revoked sentence repairs nothing here"
    )
