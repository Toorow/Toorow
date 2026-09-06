"""Conformance -- archiving a Project never costs it a warehouse zone.

`execution-substrate.md` § *An archived Project keeps its raw zone -- decided by
Jean, 2026-08-31* (AI-322) states the rule and its own criterion:

    *Incomplete if* : archiving a Project drops or empties any warehouse zone, or
    restoring an archived Project finds its raw zone gone by any path short of an
    organization purge.

The decision was written and the code already obeyed it -- `warehouse_tenancy`
resolves the zones of an archived Project exactly as it resolves an active one,
and `DELETE /api/projects/{id}` never reaches a drop. Nothing measured either
half, so a future archive path could take the datasets with it and no instrument
would have said a word. Collected history that cannot be re-collected must
survive a reversible gesture; that is the whole point of the arbitration.

THE CRITERION HAS TWO HALVES AND THEY NEED TWO DIFFERENT PROOFS.

  * *by any path* is a statement about the WHOLE repository, not about the one
    handler someone thought of. It is measured statically: the zone-drop
    primitives are declared in one module, and exactly one caller -- the
    organization purge -- reaches them.
  * *archiving ... or restoring* is a statement about behaviour. It is measured
    against the real Postgres schema by driving the real archive and restore
    handlers and asking the naming authority for the three zones before, between
    and after -- with the drop primitives patched so that a call would be caught
    rather than merely disbelieved.

A test that only did the first would pass on a handler that empties a dataset
without dropping it; a test that only did the second would pass on a background
job that drops the zone somewhere else. Both, or neither proves the bullet.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
SERVER = ROOT / "server"

#: The module allowed to name a zone-drop primitive at all: the single naming and
#: mutation point of the data plane (`warehouse_tenancy.py:1-25`).
ZONE_DROP_AUTHORITY = SERVER / "core" / "warehouse_tenancy.py"

#: The one caller of the authority's drop entry point: the organization purge,
#: which is the gesture the criterion explicitly exempts.
ORGANIZATION_PURGE = SERVER / "core" / "org_lifecycle.py"

#: What removes a ZONE (a schema / a dataset), as opposed to a table. `DROP TABLE`
#: is deliberately absent: `raw_landing` and `cache_warehouse` swap tables inside a
#: zone they keep, which is not what this criterion is about.
_ZONE_DROP = re.compile(r"DROP\s+SCHEMA\b|\bdelete_dataset\s*\(", re.I)

#: The public gesture that removes an organization's zones.
_DROP_ENTRY_POINT = re.compile(r"\bdrop_org_schemas\s*\(")


def _product_sources() -> list[Path]:
    """Every non-test Python file the deployment ships, excluding this suite.

    `server/` and not the repository: the criterion is about what the PRODUCT
    does when a Project is archived or restored. `scripts/ops/` holds hand-run
    operator gestures — `move_raw_datasets_to_eu.py` deletes a US dataset, but
    only after a verified copy, and only because a person typed it (AI-314).
    Sweeping those would make this guard refuse the human-gated path the
    criterion itself exempts.
    """
    out: list[Path] = []
    for path in SERVER.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or "__pycache__" in parts:
            continue
        out.append(path)
    return sorted(out)


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


# ---------------------------------------------------------------------------
# Half 1 -- "by any path": the repository, not the handler someone remembered
# ---------------------------------------------------------------------------


def test_a_warehouse_zone_is_dropped_in_exactly_one_module() -> None:
    """`DROP SCHEMA` / `delete_dataset` appear in the naming authority and nowhere else.

    A second site that removes a zone is a second answer to "when does a client
    lose collected history", and the two answers stop agreeing the day one of
    them is edited.
    """
    offenders = [
        _rel(path)
        for path in _product_sources()
        if path != ZONE_DROP_AUTHORITY
        and _ZONE_DROP.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not offenders, (
        "a warehouse zone is dropped outside "
        f"{_rel(ZONE_DROP_AUTHORITY)}: {offenders}"
    )


def test_only_the_organization_purge_calls_the_drop_entry_point() -> None:
    """`drop_org_schemas(...)` has ONE caller, and it is the organization purge.

    This is the exemption the criterion spells out -- "short of an organization
    purge". Any other caller would be a path by which a Project, an archive, a
    rename or a nightly sweep could take a zone with it.
    """
    callers = []
    for path in _product_sources():
        if path == ZONE_DROP_AUTHORITY:
            continue  # its own definition, not a call
        text = path.read_text(encoding="utf-8", errors="replace")
        if _DROP_ENTRY_POINT.search(text):
            callers.append(_rel(path))
    assert callers == [_rel(ORGANIZATION_PURGE)], (
        "the warehouse drop is reachable from somewhere other than the "
        f"organization purge: {callers}"
    )


def test_the_archive_and_restore_handlers_name_no_warehouse_gesture() -> None:
    """Neither `DELETE /api/projects/{id}` nor its `/restore` mentions the warehouse.

    Read from the SOURCE OF THE TWO FUNCTIONS rather than of the file: the module
    legitimately talks about many things, and a guard that reads the whole file
    would go green the day the handlers are moved next to something unrelated.
    """
    import inspect  # noqa: PLC0415

    from core import projects_api  # noqa: PLC0415

    handlers = (projects_api._delete_project, projects_api._restore_project)
    bodies = {h.__name__: inspect.getsource(h) for h in handlers}
    # The floor the quiet-guard census demands: a guard that scanned nothing
    # must not read as green. Two real handler bodies, or the sources moved.
    assert len(bodies) == 2 and all(len(b) > 100 for b in bodies.values()), (
        "the sources moved; this guard scanned none"
    )
    for handler in handlers:
        body = bodies[handler.__name__]
        assert not _ZONE_DROP.search(body), f"{handler.__name__} drops a warehouse zone"
        assert not _DROP_ENTRY_POINT.search(body), (
            f"{handler.__name__} calls drop_org_schemas"
        )
        assert "warehouse_tenancy" not in body, (
            f"{handler.__name__} reaches the warehouse naming authority; archiving "
            "a Project is a Postgres status change and nothing else"
        )


# ---------------------------------------------------------------------------
# Half 2 -- the behaviour, against the real schema
# ---------------------------------------------------------------------------

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _pg_reachable() -> bool:
    """Probe the opt-in disposable database without hanging collection."""
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(
    not _pg_reachable(), reason="platform Postgres not reachable"
)

#: Minted per session so the shared disposable base never holds two of them --
#: the same reason `tests/core/test_projects_api.py` mints its own.
_TEST_IDENTITY = f"owner-{uuid.uuid4().hex[:12]}@example.com"
_AUTH = ("core.admin_api._check_auth", (True, _TEST_IDENTITY))


def _post_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _id_request(project_id: str) -> MagicMock:
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    return req


@pytest.fixture
def caller_org(production_auth_mode):
    """The test identity owns an organization, the way a real person does.

    A project without an org has no warehouse to land in at all, so the whole
    measurement would be vacuous. Teardown walks the FK graph through
    `purge_fixture_org` rather than listing tables -- a hand-written list loses
    the race with the next migration.
    """
    from core.db import get_connection  # noqa: PLC0415

    org_id = f"org_test_{uuid.uuid4().hex[:12]}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.org_members WHERE identity = %s", (_AUTH[1][1],))
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, %s, %s, 'active', %s)",
                (org_id, f"Test {org_id}", org_id.replace("_", "-"), _AUTH[1][1]),
            )
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                (f"omem_test_{uuid.uuid4().hex[:12]}", org_id, _AUTH[1][1]),
            )
        conn.commit()
    try:
        yield org_id
    finally:
        from tests.conftest import purge_fixture_org  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (org_id,))
                still_there = cur.fetchone() is not None
            if still_there:
                purge_fixture_org(conn, org_id)
                conn.commit()


def _zones(project_id: str) -> tuple[str, str, str]:
    """The three zone names the READ layer addresses for this project, fresh."""
    from core import warehouse_tenancy as wt  # noqa: PLC0415

    wt._reset_cache()
    return (
        wt.bigquery_raw_dataset(project_id),
        wt.bigquery_staging_dataset(project_id),
        wt.bigquery_marts_dataset(project_id),
    )


@pg_available
@pytest.mark.anyio
@pytest.mark.parametrize("org_schemas_flag", ["0", "1"])
async def test_archive_then_restore_leaves_every_zone_resolving(
    caller_org, monkeypatch, org_schemas_flag
):
    """Archive, read the zones, restore, read them again -- identical, three times.

    BOTH VALUES OF `TOOROW_ORG_SCHEMAS`, because they resolve through different
    code: OFF returns the legacy per-project `raw_<project_id>` the live
    deployment uses, ON joins `app.projects -> app.organizations` and would be the
    half that could start answering "unresolvable" if the archive ever hid the
    row. A single-flag test measures one of the two paths and calls it the rule.

    The drop primitives are patched rather than trusted: an assertion on names
    alone would still pass if the handler emptied the dataset on its way past.
    """
    from core.projects_api import (  # noqa: PLC0415
        _create_project,
        _delete_project,
        _get_project,
        _restore_project,
    )

    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", org_schemas_flag)
    slug = f"keepzone-{uuid.uuid4().hex[:8]}"
    project_id = None
    try:
        with (
            patch(_AUTH[0], return_value=_AUTH[1]),
            patch("core.warehouse_tenancy.drop_org_schemas") as dropped,
            patch("core.warehouse_tenancy._drop_bigquery_datasets") as dropped_bq,
        ):
            created = await _create_project(_post_request({"name": "KeepZone", "slug": slug}))
            assert created.status_code == 201
            project_id = json.loads(created.body)["id"]

            before = _zones(project_id)
            assert all(before), f"a zone did not resolve while active: {before}"

            assert (await _delete_project(_id_request(project_id))).status_code == 200
            archived = json.loads((await _get_project(_id_request(project_id))).body)
            assert archived["status"] == "archived"

            during = _zones(project_id)
            assert during == before, (
                "archiving moved a warehouse zone: "
                f"{before} -> {during} (TOOROW_ORG_SCHEMAS={org_schemas_flag})"
            )

            assert (await _restore_project(_id_request(project_id))).status_code == 200
            after = _zones(project_id)
            assert after == before, (
                "restoring did not find the zones it left: "
                f"{before} -> {after} (TOOROW_ORG_SCHEMAS={org_schemas_flag})"
            )

            assert not dropped.called, "the archive path called drop_org_schemas"
            assert not dropped_bq.called, "the archive path dropped a BigQuery dataset"
    finally:
        if project_id:
            from core.db import get_connection  # noqa: PLC0415

            from tests.conftest import purge_fixture_org  # noqa: PLC0415

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                    )
                    row = cur.fetchone()
                if row is not None:
                    purge_fixture_org(conn, row[0])
                    conn.commit()


@pg_available
@pytest.mark.anyio
async def test_an_archived_project_leaves_the_nightly_fan_out_without_losing_its_zones(
    caller_org, monkeypatch
):
    """The one thing archiving DOES change, stated so it cannot be confused.

    `list_active_projects` stops returning the project -- an archived Project is
    not rebuilt nightly (`warehouse_tenancy.py:323-325`). That is a scheduling
    decision, not a deletion, and the zones it stopped rebuilding still resolve.
    Without this test the previous one reads as "nothing happens", and the next
    person to make archiving cheaper has no line telling them which half is the
    invariant.
    """
    from core import warehouse_tenancy as wt  # noqa: PLC0415
    from core.projects_api import _create_project, _delete_project  # noqa: PLC0415

    slug = f"fanout-{uuid.uuid4().hex[:8]}"
    project_id = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            created = await _create_project(_post_request({"name": "FanOut", "slug": slug}))
            project_id = json.loads(created.body)["id"]

            wt._reset_cache()
            listed = {z.project_id for z in wt.list_active_projects()}
            assert project_id in listed

            assert (await _delete_project(_id_request(project_id))).status_code == 200

            wt._reset_cache()
            listed_after = {z.project_id for z in wt.list_active_projects()}
            assert project_id not in listed_after, (
                "an archived project is still rebuilt nightly"
            )
            assert wt.bigquery_raw_dataset(project_id), (
                "the raw zone stopped resolving once the nightly stopped naming it"
            )
    finally:
        if project_id:
            from core.db import get_connection  # noqa: PLC0415

            from tests.conftest import purge_fixture_org  # noqa: PLC0415

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                    )
                    row = cur.fetchone()
                if row is not None:
                    purge_fixture_org(conn, row[0])
                    conn.commit()
