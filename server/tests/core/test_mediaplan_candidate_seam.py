"""The JSON door onto a plan version is the SAME governed act as the file door.

WHY THIS FILE EXISTS (AI-331, Jean 2026-08-31, « oui aux deux »).
`file-source-ingestion.md` says a mediaplan reaches `app.media_plan_lines` only
through `run_import()`. That clause is scoped to FILES; the other door,
`POST /api/mediaplans/{plan_id}/versions`, is the plan's own editing gesture and
it stays. What Jean ratified is that it must go through the SAME governed
two-step the plan object already owns -- a candidate, then an explicit publish --
and never a silent direct write.

WHAT IS PROVEN HERE, on live Postgres, by replaying the two doors:

  * a POST of lines creates a CANDIDATE and nothing is active until the explicit
    publish gesture runs -- read back from the DATABASE, not from the response;
  * a body that ASKS to publish is refused (422) instead of being ignored with a
    201 that lets the caller believe the plan is live;
  * zero lines is refused, and leaves no version behind;
  * an amount that is not an exact number of cents is refused by BOTH doors with
    the same code and the SAME SENTENCE -- asserted by comparing the two messages
    character for character, because "one rule, two callers" is a claim about the
    seam and not about two copies that happen to agree today.

Live-Postgres gated, same harness as `test_mediaplan_api.py`.
"""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import enrol_fixture_identity, purge_fixture_project

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

if os.environ.get("TEST_POSTGRES_DSN") and not os.environ.get("PLATFORM_DB_URL"):
    os.environ["PLATFORM_DB_URL"] = os.environ["TEST_POSTGRES_DSN"]


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(
    not _pg_reachable(), reason="platform Postgres not reachable"
)

_TESTER = "tester@example.com"
_AUTH = ("core.admin_api._check_auth", (True, _TESTER))


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _production_auth_mode(monkeypatch):
    """These routes take a PRODUCTION access decision, so the mode must allow one."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")


def _connect():
    import psycopg

    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"])


def _req(*, path_params=None, body=None) -> MagicMock:
    req = MagicMock()
    req.path_params = path_params or {}
    req.query_params = {}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


def _seed_project() -> str:
    project_id = f"proj-mpseam-{uuid.uuid4().hex[:8]}"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
                VALUES (%s, %s, %s, 'active', 'system', 'org_test_fixture')
                """,
                (project_id, "MP seam", project_id),
            )
            enrol_fixture_identity(
                cur,
                _TESTER,
                project_id=project_id,
                org_role="owner",
                capability="manage",
            )
        conn.commit()
    finally:
        conn.close()
    return project_id


def _drop_project(project_id: str) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.resource_grants "
                "WHERE scope_type = 'project' AND scope_id = %s",
                (project_id,),
            )
        purge_fixture_project(conn, project_id)
        conn.commit()
    finally:
        conn.close()


_LINES = [
    {
        "line_key": "digital/meta",
        "label": "Meta",
        "channel": "Meta",
        "start_date": "2026-03-15",
        "end_date": "2026-04-28",
        "budget": "10000.00",
    }
]

#: The amount the store would have ROUNDED before AI-331: 10000.005 -> 10000.00.
_SUB_CENT = "10000.005"


async def _create_plan(project_id: str) -> dict:
    from core.mediaplan_api import _create_plan as handler

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await handler(
            _req(path_params={"project_id": project_id}, body={"name": "Plan"})
        )
    assert resp.status_code == 201
    return json.loads(resp.body)


async def _post_version(plan_id: str, body: dict):
    from core.mediaplan_api import _create_version

    with patch(_AUTH[0], return_value=_AUTH[1]):
        return await _create_version(_req(path_params={"plan_id": plan_id}, body=body))


def _versions_in_db(plan_id: str) -> list[tuple]:
    """What the DATABASE holds -- never the response body's account of it."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, status, is_active FROM app.media_plan_versions "
                "WHERE plan_id = %s ORDER BY version_number",
                (plan_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The two-step
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_json_door_creates_a_candidate_and_publish_stays_a_separate_act():
    project_id = _seed_project()
    try:
        plan = await _create_plan(project_id)

        resp = await _post_version(plan["id"], {"lines": _LINES})
        assert resp.status_code == 201
        version = json.loads(resp.body)
        assert version["status"] == "candidate"
        assert version["is_active"] is False

        # The DB agrees, and NOTHING is published by the creation alone.
        assert _versions_in_db(plan["id"]) == [(version["id"], "candidate", False)]

        from core.mediaplan_api import _publish_version

        with patch(_AUTH[0], return_value=_AUTH[1]):
            presp = await _publish_version(_req(path_params={"version_id": version["id"]}))
        assert presp.status_code == 200
        published = json.loads(presp.body)
        assert published["status"] == "published"
        assert published["is_active"] is True
        assert _versions_in_db(plan["id"]) == [(version["id"], "published", True)]
    finally:
        _drop_project(project_id)


@pg_available
@pytest.mark.anyio
@pytest.mark.parametrize(
    "intent",
    [
        {"publish": True},
        {"status": "published"},
        {"is_active": True},
        {"publish_candidate": True},
    ],
)
async def test_a_body_that_asks_to_publish_is_refused_not_ignored(intent):
    """Ignoring the key would answer 201 to a caller who believes it is live."""
    project_id = _seed_project()
    try:
        plan = await _create_plan(project_id)
        resp = await _post_version(plan["id"], {"lines": _LINES, **intent})
        assert resp.status_code == 422
        body = json.loads(resp.body)
        assert body["code"] == "publication_is_a_separate_act"
        assert "publish" in body["message"]
        # Refused BEFORE any write: no candidate is left behind either.
        assert _versions_in_db(plan["id"]) == []
    finally:
        _drop_project(project_id)


@pg_available
@pytest.mark.anyio
async def test_zero_lines_is_refused_and_leaves_no_version():
    """The file door creates NO candidate when zero line survives; so does this one."""
    project_id = _seed_project()
    try:
        plan = await _create_plan(project_id)
        resp = await _post_version(plan["id"], {"lines": []})
        assert resp.status_code == 422
        assert json.loads(resp.body)["code"] == "invalid_param"
        assert _versions_in_db(plan["id"]) == []
    finally:
        _drop_project(project_id)


# ---------------------------------------------------------------------------
# The money invariant: one rule, two callers
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_a_sub_cent_amount_is_refused_by_the_json_door():
    project_id = _seed_project()
    try:
        plan = await _create_plan(project_id)
        resp = await _post_version(
            plan["id"], {"lines": [dict(_LINES[0], budget=_SUB_CENT)]}
        )
        assert resp.status_code == 422
        body = json.loads(resp.body)
        assert body["code"] == "amount_not_to_the_cent"
        # The sentence names the money that would have landed and the repair.
        assert "10000.00" in body["message"]
        assert _versions_in_db(plan["id"]) == []
    finally:
        _drop_project(project_id)


@pg_available
@pytest.mark.anyio
async def test_both_doors_refuse_the_same_amount_with_the_same_sentence():
    """ONE rule, TWO callers -- proven by comparing the two refusals, not by trust.

    The file door is `import_landing.land_plan_store_rows`, which is what
    `import_runner.run_import` calls for a `landing_target: "plan_store"`
    template. It is called here directly, on the same connection the route uses,
    so the comparison is between the two doors and not between two fixtures.
    """
    from core.import_landing import land_plan_store_rows
    from core.mediaplan_store import MediaPlanAmountError

    project_id = _seed_project()
    try:
        plan = await _create_plan(project_id)

        resp = await _post_version(
            plan["id"], {"lines": [dict(_LINES[0], budget=_SUB_CENT)]}
        )
        json_message = json.loads(resp.body)["message"]

        conn = _connect()
        try:
            with pytest.raises(MediaPlanAmountError) as caught:
                land_plan_store_rows(
                    rows=[
                        {
                            "line_key": "digital/meta",
                            "label": "Meta",
                            "channel": "Meta",
                            "start_date": "2026-03-15",
                            "end_date": "2026-04-28",
                            "budget": Decimal(_SUB_CENT),
                        }
                    ],
                    plan_id=plan["id"],
                    actor=_TESTER,
                    conn=conn,
                )
            conn.rollback()
        finally:
            conn.close()

        assert str(caught.value) == json_message
        assert caught.value.code == json.loads(resp.body)["code"]
        assert _versions_in_db(plan["id"]) == []
    finally:
        _drop_project(project_id)


@pg_available
@pytest.mark.anyio
async def test_an_amount_with_a_shorter_exponent_is_still_the_same_money():
    """"100.5" and "100.50" are one amount: only a genuine sub-cent digit refuses."""
    project_id = _seed_project()
    try:
        plan = await _create_plan(project_id)
        resp = await _post_version(
            plan["id"], {"lines": [dict(_LINES[0], budget="10000.5")]}
        )
        assert resp.status_code == 201
        assert json.loads(resp.body)["lines"][0]["budget"] == "10000.50"
    finally:
        _drop_project(project_id)


# ---------------------------------------------------------------------------
# The audit trail, written by the seam and therefore by both doors
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_the_candidate_writes_an_audit_row_carrying_its_money():
    project_id = _seed_project()
    try:
        plan = await _create_plan(project_id)
        resp = await _post_version(plan["id"], {"lines": _LINES})
        version_id = json.loads(resp.body)["id"]

        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT metadata FROM app.audit_log "
                    "WHERE action = 'media_plan.version.created' "
                    "AND metadata->>'version_id' = %s",
                    (version_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        metadata = rows[0][0]
        assert metadata["line_count"] == 1
        assert metadata["total_budget"] == "10000.00"
        assert metadata["created_status"] == "candidate"
    finally:
        _drop_project(project_id)
