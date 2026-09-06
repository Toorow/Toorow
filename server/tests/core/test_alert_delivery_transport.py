"""Story 59.6 -- what actually leaves the process, and what honestly does not.

`transport_unavailable` IS THE POINT OF THIS FILE. `infra/scripts/deploy.sh:148`
pushes seventeen environment variables and not one of them is `SMTP_*` or
`ALERT_*` (`grep -c` -> 0), so on every deployment of this product the e-mail
transport does not exist. A "Send a test" button that reported success against
an absent transport would be the worst outcome this story could have; naming the
missing variable is the honest one, and it is asserted here rather than
described.

`smtplib.SMTP` is mocked on the pattern of `test_email_channel.py:106`; `httpx`
is mocked for the webhook. Nothing here opens a socket.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import purge_fixture_project

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import alert_destinations as ad  # noqa: E402

PROJECT = "proj_ALERTSEND"
WEBHOOK_URL = "https://hooks.example.invalid/services/T000/B000/XXXXXXXX"
EMAIL_TARGET = "alerts@example.invalid"

FIRING = {
    "event": "alert_firing",
    "firing_id": "fire_EXAMPLE",
    "alert_type": "dq_timeliness",
    "project_id": PROJECT,
    "datastream_id": "ds_EXAMPLE",
    "severity": "warning",
    "observed_value": 26.0,
    "threshold": 24.0,
    "window_date": "2026-08-07",
    "fired_at": "2026-08-08T02:00:00+00:00",
    "message": "Yesterday has no accepted collection.",
}


@pytest.fixture(autouse=True)
def _armed(monkeypatch, tmp_path):
    """Every test starts from a deployment with the off-switch ON and no SMTP."""
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("TENANT_KEY_DIR", str(tmp_path))


def _smtp_double():
    smtp = MagicMock()
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)
    return smtp


# ---------------------------------------------------------------------------
# The transport this deployment does not have
# ---------------------------------------------------------------------------


def test_an_email_destination_answers_transport_unavailable_and_names_the_variable():
    verdict = ad.send_to_destination(
        {"kind": "email", "target": EMAIL_TARGET, "project_id": PROJECT}, FIRING
    )
    assert verdict["delivered"] is False
    assert verdict["error_class"] == "transport_unavailable"
    # It names WHICH variable. "It did not send" is not actionable.
    assert "SMTP_HOST" in verdict["detail"]


def test_transport_unavailable_opens_no_smtp_connection():
    with patch("smtplib.SMTP") as smtp:
        ad.send_to_destination(
            {"kind": "email", "target": EMAIL_TARGET, "project_id": PROJECT}, FIRING
        )
    smtp.assert_not_called()


def test_the_off_switch_stops_every_destination(monkeypatch):
    """`ALERTS_ENABLED` already governs the console channel. A second off-switch
    for the new destination would be a second authority for one decision."""
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    with patch("smtplib.SMTP") as smtp:
        verdict = ad.send_to_destination(
            {"kind": "email", "target": EMAIL_TARGET, "project_id": PROJECT}, FIRING
        )
    assert verdict["error_class"] == "alerts_disabled"
    smtp.assert_not_called()


# ---------------------------------------------------------------------------
# The transport it does have, once armed
# ---------------------------------------------------------------------------


def test_an_armed_email_destination_sends_through_the_existing_channel(monkeypatch):
    """Arbitrage 9: the `EmailChannel` written for story 5.2, per destination
    address. No new transport, no new API key."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    smtp = _smtp_double()
    with patch("smtplib.SMTP", return_value=smtp):
        verdict = ad.send_to_destination(
            {"kind": "email", "target": EMAIL_TARGET, "project_id": PROJECT}, FIRING
        )
    assert verdict["delivered"] is True
    assert smtp.sendmail.called
    _sender, recipients, raw = smtp.sendmail.call_args[0]
    assert EMAIL_TARGET in recipients
    assert "dq_timeliness" in raw


def test_the_outgoing_email_body_is_english(monkeypatch):
    """The ONLY text this product sends outside itself. Four of its eight labels
    were French until this story, and no test asserted a word of it -- which is
    exactly why it survived two English sweeps."""
    from email import message_from_string

    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    smtp = _smtp_double()
    with patch("smtplib.SMTP", return_value=smtp):
        ad.send_to_destination(
            {"kind": "email", "target": EMAIL_TARGET, "project_id": PROJECT}, FIRING
        )
    _sender, _recipients, raw = smtp.sendmail.call_args[0]
    body = message_from_string(raw).get_payload(decode=True).decode("utf-8")

    for french in ("Connecteur", "Valeur", "Seuil", "Horodatage", "Lien"):
        assert french not in body, f"the outgoing email still says {french!r}"
    for english in ("Value", "Threshold", "Severity", "Timestamp", "Link"):
        assert english in body
    # A persisted firing carries the monitor's own sentence, and it must survive
    # the trip: a recipient given a number with no sentence has to open the
    # console to learn what happened.
    assert "Yesterday has no accepted collection." in body


def test_a_webhook_posts_the_firing_and_carries_its_key_in_a_header():
    posted = {}

    def fake_post(url, **kwargs):
        posted["url"] = url
        posted["json"] = kwargs.get("json")
        posted["headers"] = kwargs.get("headers")
        return MagicMock(status_code=200)

    with patch("httpx.post", side_effect=fake_post):
        verdict = ad.send_to_destination(
            {
                "kind": "slack_webhook",
                "target": WEBHOOK_URL,
                "project_id": PROJECT,
                "secret": "signing-key",
                "label": "Ops Slack",
                "id": "adest_EXAMPLE",
            },
            FIRING,
        )

    assert verdict["delivered"] is True
    assert posted["url"] == WEBHOOK_URL
    assert posted["json"]["alert"]["type"] == "dq_timeliness"
    assert posted["json"]["alert"]["firing_id"] == "fire_EXAMPLE"
    assert "Yesterday has no accepted collection." in posted["json"]["text"]
    # The key travels in a header and never in the body.
    assert posted["headers"]["Authorization"] == "Bearer signing-key"
    assert "signing-key" not in str(posted["json"])


def test_a_webhook_rejection_is_reported_as_its_class():
    with patch("httpx.post", return_value=MagicMock(status_code=403)):
        verdict = ad.send_to_destination(
            {"kind": "webhook", "target": WEBHOOK_URL, "project_id": PROJECT}, FIRING
        )
    assert verdict["delivered"] is False
    assert verdict["error_class"] == "http_error"
    assert "403" in verdict["detail"]


def test_an_unreachable_webhook_is_not_a_crash():
    with patch("httpx.post", side_effect=OSError("no route to host")):
        verdict = ad.send_to_destination(
            {"kind": "webhook", "target": WEBHOOK_URL, "project_id": PROJECT}, FIRING
        )
    assert verdict["delivered"] is False
    assert verdict["error_class"] == "unreachable"


def test_a_sealed_key_is_unsealed_only_at_send_time():
    blob = ad.encrypt_secret("signing-key", PROJECT)
    captured = {}

    def fake_post(url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return MagicMock(status_code=204)

    with patch("httpx.post", side_effect=fake_post):
        verdict = ad.send_to_destination(
            {
                "kind": "webhook",
                "target": WEBHOOK_URL,
                "project_id": PROJECT,
                "secret_blob": blob,
            },
            FIRING,
        )
    assert verdict["delivered"] is True
    assert captured["headers"]["Authorization"] == "Bearer signing-key"


# ---------------------------------------------------------------------------
# "Send a test": the real transport, no firing, no scheduler
# ---------------------------------------------------------------------------


def _fake_conn(row):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=row)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


def test_send_test_writes_no_firing_and_needs_no_scheduler():
    conn, cur = _fake_conn(
        ("adest_EXAMPLE", PROJECT, "webhook", "Ops", WEBHOOK_URL, None, [], True)
    )
    with patch("httpx.post", return_value=MagicMock(status_code=200)):
        verdict = ad.send_test(conn, "adest_EXAMPLE", PROJECT)

    assert verdict["delivered"] is True
    assert verdict["code"] == "delivered"
    # The one statement this function issues is the SELECT that loads the row.
    statements = " ".join(str(call.args[0]) for call in cur.execute.call_args_list)
    assert "INSERT" not in statements.upper()
    assert "alert_firings" not in statements


def test_send_test_on_an_undeployed_email_transport_names_the_variable():
    conn, _cur = _fake_conn(
        ("adest_MAIL", PROJECT, "email", "Ops mailbox", EMAIL_TARGET, None, [], True)
    )
    verdict = ad.send_test(conn, "adest_MAIL", PROJECT)
    assert verdict["code"] == "transport_unavailable"
    assert "SMTP_HOST" in verdict["detail"]


def test_send_test_on_an_unknown_destination_is_not_found():
    conn, _cur = _fake_conn(None)
    assert ad.send_test(conn, "adest_NOPE", PROJECT)["code"] == "not_found"


# ---------------------------------------------------------------------------
# The bridge: what is persisted finally leaves
# ---------------------------------------------------------------------------


@pytest.fixture()
def firing_project(live_postgres, test_org):
    """One REAL project, its firings and its destinations -- removed afterwards.

    `deliver_pending_firings` COMMITS, deliberately: a delivery that was sent and
    then rolled back would be sent again tomorrow, and the whole point of
    `app.alert_deliveries` is that the record outlives the process. So this
    fixture cleans up by id rather than relying on the transaction, and it does
    so deepest-first because the `project_id` foreign keys are RESTRICT.
    """
    conn = live_postgres
    project = "proj_ALERTBRIDGE"

    def _wipe():
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.alert_deliveries WHERE destination_id IN "
                "(SELECT id FROM app.alert_destinations WHERE project_id = %s)",
                (project,),
            )
            cur.execute("DELETE FROM app.alert_destinations WHERE project_id = %s", (project,))
            cur.execute("DELETE FROM app.alert_firings WHERE project_id = %s", (project,))
        conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, 'Alert delivery fixture', 'alert-delivery-fixture', "
            "'active', 'system') ON CONFLICT (id) DO NOTHING",
            (project, test_org),
        )
    conn.commit()
    _wipe()
    try:
        yield conn, project
    finally:
        conn.rollback()
        _wipe()
        with conn.cursor() as cur:
            # `trg_projects_seed_capabilities` fills `app.project_capabilities`
            # on INSERT, and that FK is not a cascade: the seeded rows go first
            # or the project cannot leave.
            cur.execute("DELETE FROM app.project_capabilities WHERE project_id = %s", (project,))
            # AI-291: le graphe prend le relais si une table gouvernee
            # ajoutee depuis retient le projet en ON DELETE RESTRICT.
            purge_fixture_project(cur.connection, project)
        conn.commit()


def _write_firing(conn, project, firing_id, alert_type):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.alert_firings
                (id, definition_id, type, project_id, metric, fired_at, observed_value,
                 threshold, window_date, severity, message)
            VALUES (%s, NULL, %s, %s, 'infra', NOW(), 1, 0, CURRENT_DATE, 'warning',
                    'Yesterday has no accepted collection.')
            """,
            (firing_id, alert_type, project),
        )
    conn.commit()


def test_a_persisted_firing_reaches_its_destination_exactly_once(firing_project):
    conn, project = firing_project
    ad.create_destination(
        conn,
        project_id=project,
        kind="webhook",
        label="Ops",
        target=WEBHOOK_URL,
        alert_types=["dq_timeliness"],
    )
    conn.commit()
    _write_firing(conn, project, "fire_ALERTBRIDGE_1", "dq_timeliness")

    with patch("httpx.post", return_value=MagicMock(status_code=200)) as post:
        first = ad.deliver_pending_firings(conn, project_id=project)
    assert first == {"attempted": 1, "delivered": 1, "failed": 0, "error_classes": {}}
    assert post.call_count == 1

    # A second night over the same lookback window must send nothing: the pair is
    # UNIQUE, which is what keeps a week of nights from restating one finding
    # seven times.
    with patch("httpx.post", return_value=MagicMock(status_code=200)) as post_again:
        second = ad.deliver_pending_firings(conn, project_id=project)
    assert second["attempted"] == 0
    assert post_again.call_count == 0

    listed = ad.list_destinations(conn, project)
    assert listed[0]["last_delivery"]["state"] == "delivered"
    assert listed[0]["last_delivery"]["delivered_count"] == 1


def test_a_firing_of_another_type_does_not_reach_a_ruled_destination(firing_project):
    conn, project = firing_project
    ad.create_destination(
        conn,
        project_id=project,
        kind="webhook",
        label="Timeliness only",
        target=WEBHOOK_URL,
        alert_types=["dq_timeliness"],
    )
    conn.commit()
    _write_firing(conn, project, "fire_ALERTBRIDGE_2", "dq_schema")

    with patch("httpx.post", return_value=MagicMock(status_code=200)) as post:
        summary = ad.deliver_pending_firings(conn, project_id=project)
    assert summary["attempted"] == 0
    assert post.call_count == 0


def test_a_failed_delivery_records_its_error_class_and_is_retried(firing_project):
    """"An alert lost in silence" stops being unverifiable here: the row survives
    the process that failed to send it."""
    conn, project = firing_project
    ad.create_destination(
        conn, project_id=project, kind="webhook", label="Ops", target=WEBHOOK_URL
    )
    conn.commit()
    _write_firing(conn, project, "fire_ALERTBRIDGE_3", "dq_zero_rows")

    with patch("httpx.post", return_value=MagicMock(status_code=500)):
        summary = ad.deliver_pending_firings(conn, project_id=project)
    assert summary == {
        "attempted": 1,
        "delivered": 0,
        "failed": 1,
        "error_classes": {"http_error": 1},
    }

    listed = ad.list_destinations(conn, project)
    assert listed[0]["last_delivery"]["state"] == "pending"
    assert listed[0]["last_delivery"]["last_error_class"] == "http_error"
    assert listed[0]["last_delivery"]["delivered_count"] == 0
    assert listed[0]["last_delivery"]["failed_count"] == 1


def test_the_off_switch_stops_the_bridge(firing_project, monkeypatch):
    conn, project = firing_project
    ad.create_destination(
        conn, project_id=project, kind="webhook", label="Ops", target=WEBHOOK_URL
    )
    conn.commit()
    _write_firing(conn, project, "fire_ALERTBRIDGE_4", "dq_volume")

    monkeypatch.setenv("ALERTS_ENABLED", "false")
    with patch("httpx.post") as post:
        summary = ad.deliver_pending_firings(conn, project_id=project)
    assert summary["skipped"] == "alerts_disabled"
    post.assert_not_called()
