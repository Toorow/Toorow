"""Story 59.6 -- an alert destination: created, read, routed, and never leaking.

THE FILE THE PLAN NAMED IS `test_alert_channels.py` (`epic-59:147`). It is
`test_alert_destinations.py` here, and that is an AMENDMENT to the plan rather
than a deviation from it: `Channel` is already ratified for the INPUT side of a
Datastream (`glossary.md:355`) and the glossary's own rule is "One word, one
meaning" (`:161-163`). Naming an outgoing Slack target a "channel" would break,
on the day it shipped, the rule story 59.5 spent itself enforcing.

Two halves, deliberately:

  * the pure half -- validation, masking, sealing -- runs everywhere, including a
    machine with no Postgres;
  * the DB half takes `live_postgres` and proves what a mocked cursor cannot:
    that the CHECK refusing a secret on an e-mail destination is in the schema,
    that `project_id` in the WHERE actually bounds a caller, and that the routing
    predicate selects on `type` and not on `severity`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import alert_destinations as ad  # noqa: E402
from core import dq_monitor_registry  # noqa: E402

PROJECT = "proj_ALERTDEST"
OTHER_PROJECT = "proj_ALERTDEST_OTHER"

WEBHOOK_URL = "https://hooks.example.invalid/services/T000/B000/XXXXXXXX"
EMAIL_TARGET = "alerts@example.invalid"


# ---------------------------------------------------------------------------
# The vocabulary, and what it refuses
# ---------------------------------------------------------------------------


def test_the_routable_types_are_the_registry_and_not_a_second_list():
    """A monitor added to the registry becomes routable without touching this module.

    An equality against a hand-written list would be silent about a second list
    standing next to it -- the exact shape of the eleven divergences story 59.5
    closed. So this asserts IDENTITY of the object, not equality of contents.
    """
    assert ad.ROUTABLE_ALERT_TYPES is dq_monitor_registry.FIRING_ALERT_TYPES


def test_a_rule_on_an_unknown_type_is_refused():
    with pytest.raises(ad.AlertDestinationError) as excinfo:
        ad.normalize_alert_types(["dq_timeliness", "dq_invented"])
    assert excinfo.value.code == "unknown_alert_type"


def test_a_rule_may_not_be_severity():
    """`warning` is not a type. Eight DQ monitors out of eight write it, so a
    destination routed on it would receive all of them and tell them apart never."""
    with pytest.raises(ad.AlertDestinationError) as excinfo:
        ad.normalize_alert_types(["warning"])
    assert excinfo.value.code == "unknown_alert_type"


def test_no_rule_is_an_empty_list_and_means_everything():
    assert ad.normalize_alert_types(None) == []
    assert ad.normalize_alert_types([]) == []


def test_a_rule_keeps_one_entry_per_type():
    assert ad.normalize_alert_types(["dq_volume", "dq_volume", " dq_schema "]) == [
        "dq_volume",
        "dq_schema",
    ]


def test_an_email_destination_carries_no_secret():
    """Arbitrage 9, and the database says it too (`alert_destinations_email_has_no_secret`)."""
    with pytest.raises(ad.AlertDestinationError) as excinfo:
        ad.normalize_secret(ad.KIND_EMAIL, "hunter2")
    assert excinfo.value.code == "email_carries_no_secret"


def test_a_webhook_over_http_is_refused():
    with pytest.raises(ad.AlertDestinationError) as excinfo:
        ad.normalize_target(ad.KIND_WEBHOOK, "http://hooks.example.invalid/x")
    assert excinfo.value.code == "invalid_target"


def test_an_email_target_that_is_not_an_address_is_refused():
    with pytest.raises(ad.AlertDestinationError) as excinfo:
        ad.normalize_target(ad.KIND_EMAIL, "not-an-address")
    assert excinfo.value.code == "invalid_target"


def test_the_mask_never_renders_the_whole_target():
    """A Slack incoming-webhook URL IS the credential. The list screen shows a
    shape, never the value."""
    masked = ad.mask_target(ad.KIND_SLACK, WEBHOOK_URL)
    assert "T000" not in masked
    assert "XXXXXXXX" not in masked
    assert masked.startswith("https://hooks.example.invalid/")

    masked_email = ad.mask_target(ad.KIND_EMAIL, EMAIL_TARGET)
    assert masked_email == "a…@example.invalid"
    assert "alerts@" not in masked_email


def test_a_sealed_secret_round_trips_and_is_not_readable_as_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_KEY_DIR", str(tmp_path))
    blob = ad.encrypt_secret("s3cr3t-signing-key", PROJECT)
    assert b"s3cr3t" not in blob
    assert ad.decrypt_secret(blob, PROJECT) == "s3cr3t-signing-key"


def test_a_secret_sealed_for_one_project_does_not_open_for_another(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_KEY_DIR", str(tmp_path))
    blob = ad.encrypt_secret("s3cr3t-signing-key", PROJECT)
    # The other project HAS its own key -- this is a genuine wrong-key attempt,
    # not a missing one, and it must fail as such.
    ad.encrypt_secret("other-project-secret", OTHER_PROJECT)
    with pytest.raises(ad.AlertDestinationError) as excinfo:
        ad.decrypt_secret(blob, OTHER_PROJECT)
    assert excinfo.value.code == "secret_unseal_failed"


def test_unsealing_without_a_tenant_key_says_the_key_is_gone(tmp_path, monkeypatch):
    """A lost key is reported as a lost key, and no replacement is minted (AI-278).

    `get_or_create_key` on the unseal path made this case indistinguishable from
    a tampered secret, while quietly writing a new key over the missing one.
    """
    monkeypatch.setenv("TENANT_KEY_DIR", str(tmp_path))
    blob = ad.encrypt_secret("s3cr3t-signing-key", PROJECT)
    for key_file in tmp_path.glob("*.key"):
        key_file.unlink()

    with pytest.raises(ad.AlertDestinationError) as excinfo:
        ad.decrypt_secret(blob, PROJECT)
    assert excinfo.value.code == "secret_key_missing"
    assert list(tmp_path.glob("*.key")) == []


# ---------------------------------------------------------------------------
# The route refusals that need no database
# ---------------------------------------------------------------------------


def _conn_ctx(conn):
    @contextmanager
    def factory():
        yield conn

    return factory


@pytest.fixture()
def client():
    from core.admin_api import router
    from starlette.testclient import TestClient

    with (
        patch("core.api_auth.authenticate_api_request", return_value=(True, "test-user")),
        patch("core.admin_api._refuse_unless_project_allowed", return_value=None),
    ):
        with TestClient(router, raise_server_exceptions=True) as test_client:
            yield test_client


def test_a_read_route_refuses_to_hand_back_a_secret(client):
    """`secret_is_write_only` -- and it is a NAMED refusal, not a silent omission.

    A flag that merely did nothing would leave the next caller believing the
    field exists and is empty.
    """
    response = client.get(f"/api/alert-destinations?project_id={PROJECT}&include_secret=1")
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "secret_is_write_only"


def test_creating_with_an_unknown_type_is_refused_by_the_route(client):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    with patch("core.db.get_connection", side_effect=_conn_ctx(conn)):
        response = client.post(
            "/api/alert-destinations",
            json={
                "project_id": PROJECT,
                "kind": "email",
                "label": "Ops mailbox",
                "target": EMAIL_TARGET,
                "alert_types": ["dq_invented"],
            },
        )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unknown_alert_type"


def test_a_list_without_a_project_is_refused(client):
    response = client.get("/api/alert-destinations")
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "missing_param"


# ---------------------------------------------------------------------------
# The database half
# ---------------------------------------------------------------------------


@pytest.fixture()
def sealed_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_KEY_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def projects(live_postgres, test_org, sealed_keys):
    """Two REAL projects, in the test's own transaction.

    Real rather than plausible: `app.alert_destinations.project_id` references
    `app.projects` (migration 234) precisely so that `core.org_purge`, which
    walks the FK graph, can find this table during an RGPD erasure. A fixture
    naming a project that does not exist would have proven nothing here and
    would have failed at the first INSERT.

    Nothing is committed, so `live_postgres`'s rollback removes both projects and
    everything hung on them -- which is why this file needs no teardown.
    """
    conn = live_postgres
    with conn.cursor() as cur:
        for index, project_id in enumerate((PROJECT, OTHER_PROJECT)):
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
                "VALUES (%s, %s, 'Alert destination fixture', %s, 'active', 'system') "
                "ON CONFLICT (id) DO NOTHING",
                (project_id, test_org, f"alert-destination-fixture-{index}"),
            )
    return conn


def test_a_destination_is_created_read_updated_and_deleted(projects, sealed_keys):
    conn = projects
    created = ad.create_destination(
        conn,
        project_id=PROJECT,
        kind="slack_webhook",
        label="Ops Slack",
        target=WEBHOOK_URL,
        alert_types=["dq_timeliness"],
        secret="signing-key",
        created_by="test-user",
    )
    assert created["id"].startswith("adest_")
    assert created["alert_types"] == ["dq_timeliness"]
    assert created["has_secret"] is True

    listed = ad.list_destinations(conn, PROJECT)
    assert [row["id"] for row in listed] == [created["id"]]
    # What the screen reads, and what it never reads.
    assert "target" not in listed[0]
    assert "secret" not in listed[0] and "secret_blob" not in listed[0]
    assert WEBHOOK_URL not in listed[0]["target_masked"]
    assert "XXXXXXXX" not in listed[0]["target_masked"]
    # Nothing has been delivered, and the screen must be able to say exactly that
    # rather than print a zero.
    assert listed[0]["last_delivery"] is None

    updated = ad.update_destination(
        conn, created["id"], PROJECT, fields={"alert_types": [], "label": "Ops Slack (all)"}
    )
    assert updated["alert_types"] == []
    assert updated["label"] == "Ops Slack (all)"

    removed = ad.delete_destination(conn, created["id"], PROJECT)
    assert removed["deleted"] is True
    assert ad.list_destinations(conn, PROJECT) == []


def test_a_destination_of_another_project_is_not_found(projects, sealed_keys):
    conn = projects
    created = ad.create_destination(
        conn,
        project_id=PROJECT,
        kind="email",
        label="Ops mailbox",
        target=EMAIL_TARGET,
    )
    # The project is part of the WHERE, not of the body: an id from another
    # tenant answers "not found", it does not answer with the row.
    assert ad.get_destination(conn, created["id"], OTHER_PROJECT) is None
    assert ad.update_destination(
        conn, created["id"], OTHER_PROJECT, fields={"enabled": False}
    ) is None
    assert ad.delete_destination(conn, created["id"], OTHER_PROJECT) is None
    assert ad.list_destinations(conn, OTHER_PROJECT) == []
    # And it is still there for its own project.
    assert ad.get_destination(conn, created["id"], PROJECT) is not None


def test_the_schema_refuses_a_secret_on_an_email_destination(projects, sealed_keys):
    """Not only the module: the CHECK is in migration 234.

    A validation that lives only in Python is one `psql` away from being false.
    """
    import psycopg

    conn = projects
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO app.alert_destinations
                    (id, project_id, kind, label, target, secret_blob)
                VALUES ('adest_CHECK', %s, 'email', 'Ops mailbox', %s, %s)
                """,
                (PROJECT, EMAIL_TARGET, b"sealed"),
            )
    conn.rollback()


def test_two_destinations_of_one_project_cannot_share_a_label(projects, sealed_keys):
    """The deletion confirmation names the label. Two identical labels make that
    sentence a lie about which one is going away."""
    import psycopg

    conn = projects
    ad.create_destination(
        conn, project_id=PROJECT, kind="email", label="Ops mailbox", target=EMAIL_TARGET
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        ad.create_destination(
            conn,
            project_id=PROJECT,
            kind="webhook",
            label="Ops mailbox",
            target=WEBHOOK_URL,
        )
    conn.rollback()


def test_routing_selects_on_the_type_and_a_rule_less_destination_gets_everything(
    projects, sealed_keys
):
    conn = projects
    timeliness_only = ad.create_destination(
        conn,
        project_id=PROJECT,
        kind="webhook",
        label="Timeliness only",
        target=WEBHOOK_URL,
        alert_types=["dq_timeliness"],
    )
    catch_all = ad.create_destination(
        conn,
        project_id=PROJECT,
        kind="webhook",
        label="Everything",
        target=WEBHOOK_URL,
        alert_types=[],
    )
    disabled = ad.create_destination(
        conn,
        project_id=PROJECT,
        kind="webhook",
        label="Disabled",
        target=WEBHOOK_URL,
        alert_types=[],
    )
    ad.update_destination(conn, disabled["id"], PROJECT, fields={"enabled": False})

    reached = {row["id"] for row in ad.destinations_for_alert(conn, PROJECT, "dq_timeliness")}
    assert reached == {timeliness_only["id"], catch_all["id"]}

    # A DIFFERENT type reaches only the rule-less one -- which is the whole point
    # of routing on `type`: `severity` is `warning` for both of these.
    reached_schema = {row["id"] for row in ad.destinations_for_alert(conn, PROJECT, "dq_schema")}
    assert reached_schema == {catch_all["id"]}

    # And a type no DQ monitor writes still reaches the rule-less destination:
    # that is the ONLY route `business_threshold` and `anomaly` have.
    reached_business = {
        row["id"] for row in ad.destinations_for_alert(conn, PROJECT, "business_threshold")
    }
    assert reached_business == {catch_all["id"]}
    conn.rollback()


def test_the_empty_state_number_is_measured_not_assumed(projects, sealed_keys):
    """The empty screen states how many firings stayed in the console. That number
    is counted on the project asked for, and a project with none reads zero
    because it HAS none -- not because nothing was measured."""
    conn = projects
    assert ad.firing_count_since(conn, OTHER_PROJECT, 24) == 0
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.alert_firings
                (id, definition_id, type, project_id, metric, fired_at, observed_value,
                 threshold, window_date, severity, message)
            VALUES ('fire_ALERTDEST_1', NULL, 'dq_timeliness', %s, 'infra', NOW(), 1, 0,
                    CURRENT_DATE, 'warning', 'Yesterday has no accepted collection.')
            """,
            (OTHER_PROJECT,),
        )
    assert ad.firing_count_since(conn, OTHER_PROJECT, 24) == 1
    conn.rollback()
