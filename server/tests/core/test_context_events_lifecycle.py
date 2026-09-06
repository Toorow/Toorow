"""A manual context event can be corrected, and retired without being erased.

Migration 286, audit 2026-08-17 (`reviews/audit-2026-08-17/08-context-hub.md`,
P1 item 2): `app.context_events` had a create path and nothing else, so a manual
event with the wrong date or the wrong label fed `narrative`, `summarizer` and
`get_events` forever. Any table the product cites as a CAUSE carries a human
correction path.

TWO HALVES, AND NEITHER COVERS THE OTHER.

* The live-Postgres half proves what only a database can: the trigger of 286
  refuses to un-retire, the CHECK refuses a retirement with no reason, and the
  audit row commits with the write it records. A mocked cursor agrees with any
  of those.
* The seam half proves the route wiring: which envelope a refusal wears, that an
  event of another project is not disclosed by its status code, and that the
  default list carries `retired_at IS NULL`. No database can show that -- the
  handler's own SQL and status codes are the thing under test.

WHY THE FIXTURE DISABLES A TRIGGER. Migration 133 poses
`app.require_event_observation_binding` BEFORE INSERT on every row, manual
included -- measured on the disposable cluster at migration 285, a plain manual
INSERT answers `CheckViolation: new event observations require a
Datastream-owned Event Configuration version`. The unbound manual rows this
change exists to correct are therefore rows the schema will no longer accept,
and the only honest way to stand one up is the one
`tests/integration/test_event_observation_references_postgres.py` already
documents: the OWNER disables the INSERT trigger for the module, which is
precisely "this row did not come through the trigger". Nothing here proves
anything about that trigger; the UPDATE trigger of 286, which is what this file
does prove, stays armed throughout.

`pg_owner`: the fixture does DDL (disable/enable a trigger). Under the
application role this file SKIPS, and a skip is not a pass.

Audit rows are NOT cleaned up: `app.audit_log` is append-only by trigger
(migration 003) and migration 098 deliberately withholds the erasure hatch from
it. The rows this file leaves carry `owner@example.com` and a fixture project id.

    cd server && python -m pytest tests/core/test_context_events_lifecycle.py -q
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from tests.conftest import purge_fixture_project

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- the event lifecycle needs a live Postgres",
)


# ═══════════════════════════════════════════════════════════════════════════════
# Half one -- live Postgres
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module", autouse=True)
def _unbound_manual_rows_are_possible():
    """Let this module stand up the unbound manual row the schema now forbids.

    Module-scoped and autouse so the ACCESS EXCLUSIVE lock of `ALTER TABLE` is
    taken before any test transaction touches `app.context_events`: a
    per-statement version would block on the very transaction that needs it.
    """
    if not os.environ.get("TEST_POSTGRES_DSN"):
        yield
        return

    import psycopg  # noqa: PLC0415

    owner_dsn = os.environ.get("TEST_POSTGRES_OWNER_DSN")
    if not owner_dsn:
        pytest.fail(
            "TEST_POSTGRES_OWNER_DSN is not set: an unbound manual event is a row "
            "migration 133 refuses on INSERT, and only the owner can stand one up. "
            "`python scripts/disposable_postgres.py env` prints it.",
            pytrace=False,
        )

    def _switch(state: str) -> None:
        with psycopg.connect(owner_dsn, connect_timeout=5, autocommit=True) as owner:
            with owner.cursor() as cur:
                cur.execute(
                    f"ALTER TABLE app.context_events {state} TRIGGER "
                    "trg_context_events_require_binding"
                )

    _switch("DISABLE")
    try:
        yield
    finally:
        _switch("ENABLE")


def _insert_event(conn, project_id: str, *, source: str, label: str) -> str:
    event_id = f"evt_{uuid.uuid4().hex[:20]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.context_events
                (id, project_id, event_date, type, label, description,
                 created_by, platform, source)
            VALUES (%s, %s, %s, 'business', %s, 'first wording',
                    'owner@example.com', 'youtube', %s)
            """,
            (event_id, project_id, dt.date(2026, 8, 1), label, source),
        )
    return event_id


@pytest.fixture()
def scope(live_postgres):
    """One org, TWO projects, one manual event and one connector event.

    The second project is not decoration: an event that exists in another project
    must answer exactly as one that exists nowhere, and only a second real project
    can show the difference between "scoped" and "happens to be alone".
    """
    conn = live_postgres
    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_evtlc_{suffix}"
    project_id = f"proj_evtlc_{suffix}"
    other_project_id = f"proj_evtlc2_{suffix}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, 'active', 'owner@example.com')",
            (org_id, "Event lifecycle", org_id.lower()),
        )
        for one in (project_id, other_project_id):
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
                "VALUES (%s, %s, %s, %s, 'active', 'owner@example.com')",
                (one, org_id, "Event lifecycle", one.lower()),
            )
    manual_id = _insert_event(conn, project_id, source="manual", label="Launch day")
    connector_id = _insert_event(conn, project_id, source="youtube", label="Video published")
    elsewhere_id = _insert_event(conn, other_project_id, source="manual", label="Elsewhere")
    conn.commit()

    yield conn, project_id, other_project_id, manual_id, connector_id, elsewhere_id

    conn.rollback()
    # `app.context_events` HAS a foreign key to `app.projects`: migration 018
    # added `fk_context_events_project` ON DELETE RESTRICT (009, which this
    # comment used to cite, predates it -- corrected 2026-09-01, AI-344). So the
    # project cannot be purged while its events exist: they go first, by hand.
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.context_events WHERE project_id = ANY(%s)",
            ([project_id, other_project_id],),
        )
    for one in (project_id, other_project_id):
        purge_fixture_project(conn, one)
    conn.commit()


def _row(conn, event_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_date, type, label, description, platform, "
            "       retired_at, retired_by, retired_reason "
            "  FROM app.context_events WHERE id = %s",
            (event_id,),
        )
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else {}


def _audit_actions(conn, event_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action FROM app.audit_log WHERE metadata->>'event_id' = %s "
            "ORDER BY created_at",
            (event_id,),
        )
        return [r[0] for r in cur.fetchall()]


@_skip_without_dsn
@pytest.mark.pg_owner
class TestManualEventLifecycleOnPostgres:
    def test_a_correction_is_what_the_next_read_returns(self, scope):
        from core.context_events import update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope

        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            event_date="2026-08-09",
            label="Launch day, corrected",
            conn=conn,
        )

        row = _row(conn, manual_id)
        assert row["event_date"] == dt.date(2026, 8, 9)
        assert row["label"] == "Launch day, corrected"
        # Untouched fields stay: a correction names what it changes, and a PATCH
        # that quietly blanked everything it was not given would be a delete
        # wearing another verb.
        assert row["description"] == "first wording"
        assert row["platform"] == "youtube"

    def test_a_correction_writes_the_audit_row_that_holds_the_superseded_value(
        self, scope
    ):
        """The row is overwritten, so the audit row is where the old value lives."""
        from core.context_events import update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope

        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            label="Launch day, corrected",
            conn=conn,
        )

        assert _audit_actions(conn, manual_id) == ["context_event.updated"]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata FROM app.audit_log WHERE metadata->>'event_id' = %s",
                (manual_id,),
            )
            metadata = cur.fetchone()[0]
        assert metadata["fields"] == ["label"]
        assert metadata["previous"] == {"label": "Launch day"}

    def test_a_connector_event_refuses_a_correction_and_names_the_gesture(self, scope):
        from core.context_events import ContextEventRefusal, update_manual_event

        conn, project_id, _other, _manual, connector_id, _elsewhere = scope

        with pytest.raises(ContextEventRefusal) as refusal:
            update_manual_event(
                project_id=project_id,
                event_id=connector_id,
                updated_by="owner@example.com",
                label="hand-edited",
                conn=conn,
            )

        assert refusal.value.code == "connector_owned"
        assert "youtube" in refusal.value.message
        assert "Event Configuration" in refusal.value.message, (
            "the refusal must name the gesture that repairs, not the cause"
        )
        conn.rollback()
        assert _row(conn, connector_id)["label"] == "Video published"

    def test_a_connector_event_refuses_a_retirement_and_names_the_gesture(self, scope):
        from core.context_events import ContextEventRefusal, retire_manual_event

        conn, project_id, _other, _manual, connector_id, _elsewhere = scope

        with pytest.raises(ContextEventRefusal) as refusal:
            retire_manual_event(
                project_id=project_id,
                event_id=connector_id,
                retired_by="owner@example.com",
                reason="it is wrong",
                conn=conn,
            )

        assert refusal.value.code == "connector_owned"
        assert "youtube" in refusal.value.message
        assert "Event Configuration" in refusal.value.message
        conn.rollback()
        assert _row(conn, connector_id)["retired_at"] is None

    def test_a_retirement_keeps_the_row_and_names_who_withdrew_it(self, scope):
        from core.context_events import retire_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope

        retire_manual_event(
            project_id=project_id,
            event_id=manual_id,
            retired_by="owner@example.com",
            reason="logged on the wrong project",
            conn=conn,
        )

        row = _row(conn, manual_id)
        assert row["retired_at"] is not None
        assert row["retired_by"] == "owner@example.com"
        assert row["retired_reason"] == "logged on the wrong project"
        # A supersede, not a delete: the label is still readable, which is what
        # makes an earlier briefing that cited it explainable.
        assert row["label"] == "Launch day"
        assert _audit_actions(conn, manual_id) == ["context_event.retired"]

    def test_a_retired_event_refuses_a_correction(self, scope):
        from core.context_events import (
            ContextEventRefusal,
            retire_manual_event,
            update_manual_event,
        )

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope
        retire_manual_event(
            project_id=project_id,
            event_id=manual_id,
            retired_by="owner@example.com",
            reason="wrong date",
            conn=conn,
        )

        with pytest.raises(ContextEventRefusal) as refusal:
            update_manual_event(
                project_id=project_id,
                event_id=manual_id,
                updated_by="owner@example.com",
                label="second thoughts",
                conn=conn,
            )

        assert refusal.value.code == "already_retired"
        assert "Write a new event" in refusal.value.message

    def test_a_retirement_is_never_unmade(self, scope):
        """The trigger of 286, from the writer's side rather than from its source."""
        import psycopg  # noqa: PLC0415
        from core.context_events import retire_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope
        retire_manual_event(
            project_id=project_id,
            event_id=manual_id,
            retired_by="owner@example.com",
            reason="wrong date",
            conn=conn,
        )

        with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation) as raised:
            cur.execute(
                "UPDATE app.context_events SET retired_at = NULL, retired_by = NULL, "
                "retired_reason = NULL WHERE id = %s",
                (manual_id,),
            )
        assert "never unmade" in str(raised.value)
        conn.rollback()
        assert _row(conn, manual_id)["retired_at"] is not None

    def test_a_retirement_cannot_be_re_attributed_either(self, scope):
        import psycopg  # noqa: PLC0415
        from core.context_events import retire_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope
        retire_manual_event(
            project_id=project_id,
            event_id=manual_id,
            retired_by="owner@example.com",
            reason="wrong date",
            conn=conn,
        )

        with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "UPDATE app.context_events SET retired_by = %s WHERE id = %s",
                ("someone-else@example.com", manual_id),
            )
        conn.rollback()

    def test_an_ordinary_correction_still_passes_the_retirement_trigger(self, scope):
        """A guard that refused every update would be indistinguishable from a
        broken table -- the same case migration 212's suite makes for its own."""
        from core.context_events import update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope
        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            platform=None,
            conn=conn,
        )
        assert _row(conn, manual_id)["platform"] is None

    def test_a_blank_reason_is_refused_before_anything_is_written(self, scope):
        from core.context_events import ContextEventRefusal, retire_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope

        for blank in ("", "   ", "\t\n"):
            with pytest.raises(ContextEventRefusal) as refusal:
                retire_manual_event(
                    project_id=project_id,
                    event_id=manual_id,
                    retired_by="owner@example.com",
                    reason=blank,
                    conn=conn,
                )
            assert refusal.value.code == "missing_reason"
            assert refusal.value.status == 422
        assert _row(conn, manual_id)["retired_at"] is None

    def test_the_database_refuses_a_retirement_that_names_nobody(self, scope):
        """The CHECK, not the service -- a second writer must meet the same rule."""
        import psycopg  # noqa: PLC0415

        conn, _project_id, _other, manual_id, _connector, _elsewhere = scope

        with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "UPDATE app.context_events SET retired_at = now() WHERE id = %s",
                (manual_id,),
            )
        conn.rollback()

    def test_an_unknown_id_and_an_id_of_another_project_answer_alike(self, scope):
        """An id that exists elsewhere must not be learnable by trying."""
        from core.context_events import ContextEventRefusal, update_manual_event

        conn, project_id, _other, _manual, _connector, elsewhere_id = scope

        refusals = []
        for event_id in (f"evt_{uuid.uuid4().hex[:20]}", elsewhere_id):
            with pytest.raises(ContextEventRefusal) as refusal:
                update_manual_event(
                    project_id=project_id,
                    event_id=event_id,
                    updated_by="owner@example.com",
                    label="whatever",
                    conn=conn,
                )
            refusals.append((refusal.value.code, refusal.value.status, refusal.value.message))

        assert refusals[0] == refusals[1] == ("not_found", 404, "Context event not found")
        assert _row(conn, elsewhere_id)["label"] == "Elsewhere"

    def test_a_retired_event_leaves_the_default_list_and_the_row_stays(self, scope, client):
        """End to end on the real table: the route the console reads.

        `_list_context_events` runs its own SQL on its own connection, so the only
        way to show that a retired event stops being SERVED -- rather than merely
        stops being stored -- is to let the handler read this database.
        """
        from core.context_events import retire_manual_event

        conn, project_id, _other, manual_id, connector_id, _elsewhere = scope
        retire_manual_event(
            project_id=project_id,
            event_id=manual_id,
            retired_by="owner@example.com",
            reason="wrong date",
            conn=conn,
        )

        class _Borrowed:
            """The live connection, handed to a handler that expects to own one."""

            def __enter__(self):
                return conn

            def __exit__(self, *args):
                return False

        with (
            patch("core.db.get_connection", return_value=_Borrowed()),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        ):
            live = client.get(f"/api/context-events?project_id={project_id}").json()
            everything = client.get(
                f"/api/context-events?project_id={project_id}&include_retired=true"
            ).json()

        assert [e["id"] for e in live["events"]] == [connector_id]
        assert sorted(e["id"] for e in everything["events"]) == sorted(
            [manual_id, connector_id]
        )
        withdrawn = next(e for e in everything["events"] if e["id"] == manual_id)
        assert withdrawn["retired_reason"] == "wrong date"
        assert withdrawn["capabilities"]["can_correct"] is False
        # And the connector row is live but still not correctable by hand.
        assert next(
            e for e in live["events"] if e["id"] == connector_id
        )["capabilities"]["can_correct"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# Half two -- the routes, with no database
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def client():
    """The admin router alone, auth disabled -- the shape used by its neighbours."""
    os.environ["TOOROW_AUTH_MODE"] = "disabled"
    from core import api_auth  # noqa: PLC0415
    from core.admin_api import router  # noqa: PLC0415

    api_auth.reset_verifier_cache()
    with TestClient(Starlette(routes=[Mount("/", app=router)])) as one:
        yield one
    os.environ.pop("TOOROW_AUTH_MODE", None)
    api_auth.reset_verifier_cache()


def _mock_conn(cursor=None):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    if cursor is not None:
        conn.cursor.return_value = cursor
    return conn


def _mock_cursor(fetchall_result=None, description=None):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = fetchall_result or []
    cur.description = description or []
    return cur


class _ArmedCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *args):
        return False


class TestTheRoutesRefuseTheWayTheScreenNeeds:
    def test_a_correction_outside_the_caller_s_scope_is_a_plain_not_found(self, client):
        """The same envelope an unreadable project wears -- 404 discloses nothing,
        and the guard is asked for `edit`, not for `view`."""
        conn = _mock_conn(cursor=_mock_cursor())
        with (
            patch("core.db.request_connection", return_value=_ArmedCtx(conn)),
            patch(
                "core.admin_api._strict_project_capability_allowed", return_value=False
            ) as guard,
        ):
            response = client.patch(
                "/api/context-events/evt_EXAMPLE?project_id=proj_EXAMPLE",
                json={"label": "corrected"},
            )

        assert response.status_code == 404
        assert response.json() == {"code": "not_found", "message": "Project not found"}
        assert guard.call_args.kwargs["minimum_capability"] == "edit"
        conn.cursor.assert_not_called()

    def test_a_connector_owned_refusal_reaches_the_caller_with_its_gesture(self, client):
        from core.context_events import ContextEventRefusal  # noqa: PLC0415

        refusal = ContextEventRefusal(
            "connector_owned",
            "This event was emitted by the youtube connector. Correct it in that "
            "Datastream's Event Configuration, not here.",
        )
        with (
            patch("core.db.request_connection", return_value=_ArmedCtx(_mock_conn())),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
            patch("core.context_events.update_manual_event", side_effect=refusal),
        ):
            response = client.patch(
                "/api/context-events/evt_EXAMPLE?project_id=proj_EXAMPLE",
                json={"label": "corrected"},
            )

        assert response.status_code == 409
        assert response.json()["code"] == "connector_owned"
        assert "Event Configuration" in response.json()["message"]

    def test_a_retirement_with_no_reason_is_refused_by_the_route(self, client):
        with (
            patch("core.db.request_connection", return_value=_ArmedCtx(_mock_conn())),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        ):
            response = client.post(
                "/api/context-events/evt_EXAMPLE/retire?project_id=proj_EXAMPLE",
                json={"reason": "   "},
            )

        assert response.status_code == 422
        assert response.json()["code"] == "missing_reason"
        assert "why" in response.json()["message"]

    def test_an_empty_correction_is_refused_before_a_connection_is_taken(self, client):
        """A PATCH that names no field is not a no-op to be swallowed: the caller
        meant something, and the server cannot guess what."""
        with patch("core.db.request_connection") as armed:
            response = client.patch(
                "/api/context-events/evt_EXAMPLE?project_id=proj_EXAMPLE", json={}
            )

        assert response.status_code == 422
        assert response.json()["code"] == "no_change"
        armed.assert_not_called()

    def test_an_invalid_date_answers_with_the_shared_validator_s_own_words(self, client):
        """One validator, two doors: the ToolError the create path raises is what
        this route renders, rather than a second rule written for HTTP."""
        conn = _mock_conn(cursor=_mock_cursor())
        with (
            patch("core.db.request_connection", return_value=_ArmedCtx(conn)),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
            patch(
                "core.context_events._locked_manual_event",
                return_value={
                    "source": "manual", "retired_at": None, "retired_by": None,
                    "event_date": dt.date(2026, 8, 1), "type": "business",
                    "label": "Launch day", "description": None, "platform": None,
                    "value": None, "entity_key": None, "entity_kind": None,
                },
            ),
        ):
            response = client.patch(
                "/api/context-events/evt_EXAMPLE?project_id=proj_EXAMPLE",
                json={"event_date": "04/07/2026"},
            )

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_input"
        assert "event_date" in response.json()["message"]

    # -- Migration 322: the create route carries the metric, and refuses a name
    #    the Project does not govern with the sentence that repairs.

    def test_the_create_route_writes_the_metric_it_was_given(self, client):
        cur = _mock_cursor(description=[("id",), ("metric",)])
        cur.fetchone.return_value = ("evt_X", "cost")
        conn = _mock_conn(cursor=cur)
        with (
            patch("core.db.get_connection", return_value=_ArmedCtx(conn)),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
            patch("core.context_events.assert_metric_is_governed", return_value=None),
            patch("core.audit.insert_audit_row", return_value=None),
        ):
            response = client.post(
                "/api/context-events?project_id=proj_EXAMPLE",
                json={
                    "event_date": "2026-08-17",
                    "type": "business",
                    "label": "Bid raised",
                    "metric": "cost",
                },
            )

        assert response.status_code == 201
        assert response.json()["metric"] == "cost"
        # The FIRST execute: the audit row rides the same cursor right after.
        insert_sql, insert_params = cur.execute.call_args_list[0].args
        assert "metric" in insert_sql
        assert insert_params[-1] == "cost"

    def test_the_create_route_refuses_a_metric_the_project_does_not_govern(self, client):
        """A refusal a person repairs, never dressed as a `db_error` 500."""
        from core.context_events import ContextEventRefusal  # noqa: PLC0415

        refusal = ContextEventRefusal(
            "metric_not_governed",
            "'Ad spend' is not a metric this Project governs. Pick one of its "
            "governed metrics, leave the metric empty when the event concerns "
            "every metric, or declare it as a Concept in the Semantic Model.",
            status=422,
        )
        conn = _mock_conn(cursor=_mock_cursor())
        with (
            patch("core.db.get_connection", return_value=_ArmedCtx(conn)),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
            patch("core.context_events.assert_metric_is_governed", side_effect=refusal),
        ):
            response = client.post(
                "/api/context-events?project_id=proj_EXAMPLE",
                json={
                    "event_date": "2026-08-17",
                    "type": "business",
                    "label": "Bid raised",
                    "metric": "Ad spend",
                },
            )

        assert response.status_code == 422
        assert response.json()["code"] == "metric_not_governed"
        assert "Semantic Model" in response.json()["message"]
        # And nothing was written: the check runs before the INSERT.
        conn.cursor.assert_not_called()

    def test_an_absent_metric_is_written_as_null_and_is_not_a_refusal(self, client):
        """"About every metric" is the default answer, not a missing one."""
        cur = _mock_cursor(description=[("id",), ("metric",)])
        cur.fetchone.return_value = ("evt_X", None)
        conn = _mock_conn(cursor=cur)
        with (
            patch("core.db.get_connection", return_value=_ArmedCtx(conn)),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
            patch("core.audit.insert_audit_row", return_value=None),
        ):
            response = client.post(
                "/api/context-events?project_id=proj_EXAMPLE",
                json={
                    "event_date": "2026-08-17",
                    "type": "incident",
                    "label": "Site outage",
                },
            )

        assert response.status_code == 201
        assert response.json()["metric"] is None
        _insert_sql, insert_params = cur.execute.call_args_list[0].args
        assert insert_params[-1] is None

    def test_the_default_list_asks_the_database_for_live_events_only(self, client):
        cur = _mock_cursor(fetchall_result=[], description=[("id",)])
        with (
            patch("core.db.get_connection", return_value=_ArmedCtx(_mock_conn(cursor=cur))),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        ):
            client.get("/api/context-events?project_id=proj_EXAMPLE")
            default_sql = cur.execute.call_args_list[-1].args[0]

            cur.execute.reset_mock()
            client.get("/api/context-events?project_id=proj_EXAMPLE&include_retired=true")
            asked_sql = cur.execute.call_args_list[-1].args[0]

        assert "retired_at IS NULL" in default_sql
        assert "retired_at IS NULL" not in asked_sql, (
            "a caller that explicitly asked for withdrawn events must get them -- "
            "silently dropping them from a list that claims to be complete is the "
            "defect, not the fix"
        )

    def test_a_connector_row_is_listed_but_not_advertised_as_correctable(self, client):
        """The screen must never render a button the server will refuse."""
        cur = _mock_cursor(
            fetchall_result=[
                ("evt_A", "proj_EXAMPLE", dt.date(2026, 8, 1), "business", "Manual",
                 None, "owner@example.com", None, "manual", None, None, None),
                ("evt_B", "proj_EXAMPLE", dt.date(2026, 8, 1), "release", "Emitted",
                 None, "youtube", None, "youtube", None, None, None),
            ],
            description=[
                ("id",), ("project_id",), ("event_date",), ("type",), ("label",),
                ("description",), ("created_by",), ("created_at",), ("source",),
                ("retired_at",), ("retired_by",), ("retired_reason",),
            ],
        )
        with (
            patch("core.db.get_connection", return_value=_ArmedCtx(_mock_conn(cursor=cur))),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        ):
            events = client.get("/api/context-events?project_id=proj_EXAMPLE").json()["events"]

        by_id = {event["id"]: event["capabilities"]["can_correct"] for event in events}
        assert by_id == {"evt_A": True, "evt_B": False}


# ═══════════════════════════════════════════════════════════════════════════════
# Migration 322 -- an annotation names the metric it is about, or every metric
#
# `proactive-assertions.md` is incomplete if "a context event is attached to a
# claim without being scoped to that claim's metric, connector and date". The two
# scoped walks can only compare a metric an author was able to WRITE, so the
# column and its doors are half the repair; the walks are the other half
# (`test_briefing_context_pairing.py`).
# ═══════════════════════════════════════════════════════════════════════════════


def _metric_of(conn, event_id: str):
    with conn.cursor() as cur:
        cur.execute("SELECT metric FROM app.context_events WHERE id = %s", (event_id,))
        row = cur.fetchone()
    return row[0] if row else None


@_skip_without_dsn
@pytest.mark.pg_owner
class TestAnAnnotationNamesTheMetricItIsAbout:
    def test_a_governed_metric_is_stored_as_written(self, scope):
        from core.context_events import persist_context_event

        conn, project_id, _other, _manual, _connector, _elsewhere = scope
        event_id = persist_context_event(
            project_id=project_id,
            event_date="2026-08-02",
            type="business",
            label="Bid raised",
            description="",
            created_by="owner@example.com",
            metric="cost",
            conn=conn,
        )
        assert _metric_of(conn, event_id) == "cost"

    def test_no_metric_is_the_default_and_means_every_metric(self, scope):
        """NULL is "about no metric in particular", never "we lost which"."""
        from core.context_events import persist_context_event

        conn, project_id, _other, _manual, _connector, _elsewhere = scope
        event_id = persist_context_event(
            project_id=project_id,
            event_date="2026-08-02",
            type="incident",
            label="Site outage",
            description="",
            created_by="owner@example.com",
            conn=conn,
        )
        assert _metric_of(conn, event_id) is None

    def test_a_blank_metric_collapses_to_every_metric_rather_than_to_a_blank(self, scope):
        """A blank would read as a name nobody can compare, and the CHECK refuses it."""
        from core.context_events import persist_context_event

        conn, project_id, _other, _manual, _connector, _elsewhere = scope
        event_id = persist_context_event(
            project_id=project_id,
            event_date="2026-08-02",
            type="incident",
            label="Site outage",
            description="",
            created_by="owner@example.com",
            metric="   ",
            conn=conn,
        )
        assert _metric_of(conn, event_id) is None

    def test_the_database_refuses_a_blank_metric_written_around_the_door(self, scope):
        import psycopg

        conn, project_id, _other, _manual, _connector, _elsewhere = scope
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.context_events "
                    "(id, project_id, event_date, type, label, created_by, metric) "
                    "VALUES (%s, %s, %s, 'business', 'Blank metric', "
                    "'owner@example.com', '  ')",
                    (f"evt_{uuid.uuid4().hex[:20]}", project_id, dt.date(2026, 8, 2)),
                )
        conn.rollback()

    def test_a_metric_the_project_does_not_govern_is_refused_by_name(self, scope):
        """One vocabulary, or the comparison this column exists for cannot happen."""
        from core.context_events import ContextEventRefusal, persist_context_event

        conn, project_id, _other, _manual, _connector, _elsewhere = scope
        with pytest.raises(ContextEventRefusal) as refused:
            persist_context_event(
                project_id=project_id,
                event_date="2026-08-02",
                type="business",
                label="Bid raised",
                description="",
                created_by="owner@example.com",
                metric="Ad spend",
                conn=conn,
            )
        assert refused.value.code == "metric_not_governed"
        assert refused.value.status == 422
        # The refusal NAMES the gesture, never the technical cause.
        assert "Ad spend" in refused.value.message
        assert "Semantic Model" in refused.value.message
        conn.rollback()

    def test_a_refusal_is_not_dressed_as_a_database_failure(self, scope):
        """`db_error` would send a person to look at the server, not at their metric."""
        from core.context_events import ContextEventRefusal, persist_context_event
        from fastmcp.exceptions import ToolError

        conn, project_id, _other, _manual, _connector, _elsewhere = scope
        with pytest.raises(ContextEventRefusal) as refused:
            persist_context_event(
                project_id=project_id,
                event_date="2026-08-02",
                type="business",
                label="Bid raised",
                description="",
                created_by="owner@example.com",
                metric="not_a_governed_name",
                conn=conn,
            )
        assert not isinstance(refused.value, ToolError)
        conn.rollback()

    def test_the_metric_is_correctable_and_clearable(self, scope):
        """Naming the WRONG metric goes silent with no symptom -- so it is repairable."""
        from core.context_events import UNSET, update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope

        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            metric="cost",
            conn=conn,
        )
        assert _metric_of(conn, manual_id) == "cost"

        # An explicit null widens the event back to "about every metric", which
        # is unreachable if `UNSET` and `None` were the same word.
        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            metric=None,
            conn=conn,
        )
        assert _metric_of(conn, manual_id) is None

        # And a correction that names nothing leaves it alone.
        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            metric=UNSET,
            label="Launch day, corrected",
            conn=conn,
        )
        assert _metric_of(conn, manual_id) is None

    def test_the_correction_door_meets_the_same_catalogue_as_the_create_door(self, scope):
        """A rule that held on create and not on correction is a rule with a hole."""
        from core.context_events import ContextEventRefusal, update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope
        with pytest.raises(ContextEventRefusal) as refused:
            update_manual_event(
                project_id=project_id,
                event_id=manual_id,
                updated_by="owner@example.com",
                metric="Ad spend",
                conn=conn,
            )
        assert refused.value.code == "metric_not_governed"
        conn.rollback()


# ═══════════════════════════════════════════════════════════════════════════════
# Half three -- the annotation's earlier wording, and the two closures of 328
#
# Migration 328, Jean's decision of 2026-08-31: an annotation is the manual
# context EVENT and its DESCRIPTION, and story 49.6's AC3 asks that "previous"
# and "superseded" be DISTINCT states rather than one word for "gone". Until this
# migration, a correction OVERWROTE the row and the superseded wording survived
# only inside an audit blob no reader could reach -- the list route said so in a
# literal boolean, `version_history: false`.
# ═══════════════════════════════════════════════════════════════════════════════


def _revisions(conn, event_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT revision_no, corrected_by, corrected_fields, previous_label, "
            "       previous_description, previous_event_date, previous_metric "
            "  FROM app.context_event_revisions WHERE event_id = %s "
            " ORDER BY revision_no",
            (event_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@_skip_without_dsn
@pytest.mark.pg_owner
class TestAnAnnotationsEarlierWordingSurvives:
    def test_a_correction_files_the_wording_it_superseded(self, scope):
        from core.context_events import update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope

        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            label="Launch day, corrected",
            description="second wording",
            conn=conn,
        )

        filed = _revisions(conn, manual_id)
        assert len(filed) == 1
        assert filed[0]["revision_no"] == 1
        assert filed[0]["corrected_by"] == "owner@example.com"
        # What the annotation READ BEFORE -- not what it reads now. A history
        # holding the current wording says nothing anyone can use.
        assert filed[0]["previous_label"] == "Launch day"
        assert filed[0]["previous_description"] == "first wording"
        assert filed[0]["corrected_fields"] == ["description", "label"]

    def test_the_first_wording_survives_the_second_correction(self, scope):
        """The defect this migration closes, in one assertion.

        Before 328 the second correction destroyed what the first one left, so a
        narration published under the ORIGINAL wording became unexplainable.
        """
        from core.context_events import update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope

        for wording in ("second wording", "third wording"):
            update_manual_event(
                project_id=project_id,
                event_id=manual_id,
                updated_by="owner@example.com",
                description=wording,
                conn=conn,
            )

        filed = _revisions(conn, manual_id)
        assert [r["revision_no"] for r in filed] == [1, 2]
        assert [r["previous_description"] for r in filed] == [
            "first wording",
            "second wording",
        ]
        assert _row(conn, manual_id)["description"] == "third wording"

    def test_a_revision_names_the_fields_the_author_moved(self, scope):
        """A diff computed downstream cannot tell moved from merely identical."""
        from core.context_events import update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope

        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            event_date="2026-08-09",
            conn=conn,
        )

        filed = _revisions(conn, manual_id)
        assert filed[0]["corrected_fields"] == ["event_date"]
        # The label did not move, and the revision still carries it: a reader
        # rendering the superseded annotation needs it whole.
        assert filed[0]["previous_label"] == "Launch day"
        assert filed[0]["previous_event_date"] == dt.date(2026, 8, 1)

    def test_a_revision_is_never_rewritten(self, scope):
        """The append-only trigger of 328, on the record of a supersede."""
        import psycopg
        from core.context_events import update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope
        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            label="Launch day, corrected",
            conn=conn,
        )

        with pytest.raises(psycopg.errors.CheckViolation) as refused:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.context_event_revisions SET previous_label = %s "
                    " WHERE event_id = %s",
                    ("something else entirely", manual_id),
                )
        conn.rollback()
        assert "never rewritten" in str(refused.value)
        assert _revisions(conn, manual_id)[0]["previous_label"] == "Launch day"

    def test_the_application_role_cannot_update_a_revision_either(self, scope):
        """The trigger is one half; the privilege is the half 198 measured.

        PostgreSQL checks the table privilege BEFORE the trigger, so a table can
        carry a perfect guard and still be writable by a role that holds UPDATE
        the day someone replaces the function.
        """
        conn, _project_id, _other, _manual, _connector, _elsewhere = scope
        with conn.cursor() as cur:
            cur.execute(
                "SELECT has_table_privilege('connector', "
                "       'app.context_event_revisions', 'UPDATE'), "
                "       has_table_privilege('connector', "
                "       'app.context_event_revisions', 'DELETE')"
            )
            may_update, may_delete = cur.fetchone()
        assert not may_update, (
            "connector holds UPDATE on app.context_event_revisions -- migration "
            "207's blanket grant has been handed back and the REVOKE of 328 undone"
        )
        assert may_delete, (
            "connector holds no DELETE on app.context_event_revisions: the day "
            "app.context_events joins the tenant tree, the erasure would fail "
            "with 42501 before any trigger is reached (migration 198's finding)"
        )

    def test_deleting_the_annotation_takes_its_history_with_it(self, scope):
        """The erasure reaches the child, whatever the append-only guard thinks."""
        from core.context_events import update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope
        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            label="Launch day, corrected",
            conn=conn,
        )
        assert _revisions(conn, manual_id)

        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.context_events WHERE id = %s", (manual_id,))
        conn.commit()

        assert _revisions(conn, manual_id) == []

    def test_the_history_is_scoped_to_the_project_that_asks(self, scope):
        """An event of another project answers exactly as one that exists nowhere."""
        from core.context_events import fetch_event_revisions, update_manual_event

        conn, project_id, other_project_id, manual_id, _connector, _elsewhere = scope
        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            label="Launch day, corrected",
            conn=conn,
        )

        mine = fetch_event_revisions(
            project_id=project_id, event_id=manual_id, conn=conn
        )
        theirs = fetch_event_revisions(
            project_id=other_project_id, event_id=manual_id, conn=conn
        )
        assert [r["label"] for r in mine] == ["Launch day"]
        assert theirs == []

    def test_a_revision_is_read_in_the_vocabulary_of_an_event(self, scope):
        """`previous_label` is a column name; the screen reads `label`."""
        from core.context_events import fetch_event_revisions, update_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope
        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            label="Launch day, corrected",
            conn=conn,
        )

        revision = fetch_event_revisions(
            project_id=project_id, event_id=manual_id, conn=conn
        )[0]
        assert revision["label"] == "Launch day"
        assert revision["event_date"] == "2026-08-01"
        assert revision["corrected_fields"] == ["label"]
        assert revision["superseded_at"].startswith("20")
        assert not [key for key in revision if key.startswith("previous_")]


@_skip_without_dsn
@pytest.mark.pg_owner
class TestTheTwoClosuresOfMigration324AppliedToTheAnnotation:
    """324 closed two holes in 321's retraction guard; 286's guard had both.

    Neither is reachable through the product write path, and both are real at the
    SQL layer -- which is where the next writer will be.
    """

    def test_an_annotation_is_never_born_retired(self, scope):
        import psycopg

        conn, project_id, _other, _manual, _connector, _elsewhere = scope

        with pytest.raises(psycopg.errors.CheckViolation) as refused:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.context_events
                        (id, project_id, event_date, type, label, created_by,
                         source, retired_at, retired_by, retired_reason)
                    VALUES (%s, %s, DATE '2026-08-01', 'business', 'Forged',
                            'owner@example.com', 'manual', now(),
                            'owner@example.com', 'never happened')
                    """,
                    (f"evt_{uuid.uuid4().hex[:20]}", project_id),
                )
        conn.rollback()
        assert "never born retired" in str(refused.value)

    def test_a_withdrawn_annotation_is_frozen_whole(self, scope):
        """Not only its retirement triple: its wording, its day, its metric.

        `_locked_manual_event` refuses this in Python, and a refusal that lives
        only in the service is one the next writer undoes without noticing.
        """
        import psycopg
        from core.context_events import retire_manual_event

        conn, project_id, _other, manual_id, _connector, _elsewhere = scope
        retire_manual_event(
            project_id=project_id,
            event_id=manual_id,
            retired_by="owner@example.com",
            reason="wrong day",
            conn=conn,
        )

        with pytest.raises(psycopg.errors.CheckViolation) as refused:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.context_events SET label = %s WHERE id = %s",
                    ("re-worded after the fact", manual_id),
                )
        conn.rollback()
        assert "frozen whole" in str(refused.value)
        assert _row(conn, manual_id)["label"] == "Launch day"

    def test_an_ordinary_annotation_still_inserts_and_still_corrects(self, scope):
        """The calibration: a guard that refuses everything proves nothing."""
        from core.context_events import update_manual_event

        conn, project_id, _other, _manual, _connector, _elsewhere = scope
        fresh = _insert_event(conn, project_id, source="manual", label="Ordinary")
        conn.commit()

        update_manual_event(
            project_id=project_id,
            event_id=fresh,
            updated_by="owner@example.com",
            label="Ordinary, corrected",
            conn=conn,
        )
        assert _row(conn, fresh)["label"] == "Ordinary, corrected"


@_skip_without_dsn
@pytest.mark.pg_owner
class TestTheHistoryReachesTheScreen:
    """The route the console reads, against the real table.

    `_list_context_events` and the revisions route each run their own SQL on
    their own connection, so the only way to show that a history is SERVED --
    rather than merely stored -- is to let the handlers read this database.
    """

    def test_the_route_answers_the_earlier_wordings_and_the_list_counts_them(
        self, scope, client
    ):
        from core.context_events import update_manual_event

        conn, project_id, _other, manual_id, connector_id, _elsewhere = scope
        update_manual_event(
            project_id=project_id,
            event_id=manual_id,
            updated_by="owner@example.com",
            label="Launch day, corrected",
            conn=conn,
        )

        class _Borrowed:
            def __enter__(self):
                return conn

            def __exit__(self, *args):
                return False

        with (
            patch("core.db.get_connection", return_value=_Borrowed()),
            patch("core.db.request_connection", return_value=_Borrowed()),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        ):
            listed = client.get(f"/api/context-events?project_id={project_id}").json()
            history = client.get(
                f"/api/context-events/{manual_id}/revisions?project_id={project_id}"
            )

        corrected = next(e for e in listed["events"] if e["id"] == manual_id)
        assert corrected["revision_count"] == 1
        assert corrected["capabilities"]["version_history"] is True
        # An annotation nobody corrected counts zero -- an absence of history,
        # never an absence of the feature.
        assert next(
            e for e in listed["events"] if e["id"] == connector_id
        )["revision_count"] == 0

        assert history.status_code == 200
        body = history.json()
        # The current wording travels WITH its history: rendering superseded
        # wordings against a current one fetched at another moment is how a
        # screen shows a diff nobody made.
        assert body["event"]["label"] == "Launch day, corrected"
        assert [r["label"] for r in body["revisions"]] == ["Launch day"]
        assert body["revisions"][0]["corrected_fields"] == ["label"]

    def test_an_event_of_another_project_has_no_history_to_show(self, scope, client):
        """The same 404 an unreadable project wears -- nothing is disclosed."""
        conn, project_id, _other, _manual, _connector, elsewhere_id = scope

        class _Borrowed:
            def __enter__(self):
                return conn

            def __exit__(self, *args):
                return False

        with (
            patch("core.db.request_connection", return_value=_Borrowed()),
            patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        ):
            answered = client.get(
                f"/api/context-events/{elsewhere_id}/revisions?project_id={project_id}"
            )
            unknown = client.get(
                f"/api/context-events/evt_does_not_exist/revisions?project_id={project_id}"
            )

        assert answered.status_code == unknown.status_code == 404
        assert answered.json() == unknown.json()
