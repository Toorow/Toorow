from __future__ import annotations

import json
import os
import random
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.project_access import AccessDecision
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


class _Connection:
    def __init__(self):
        self.commit = MagicMock()


@contextmanager
def _connection():
    yield _Connection()


def test_data_surface_route_is_registered_and_uses_one_strict_snapshot() -> None:
    from core.main import build_asgi_app

    envelope = {
        "schema_version": "data-imports.v1",
        "project_ref": {"object_type": "project", "id": "proj-1"},
        "items": [],
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.db.install_access_context"),
        patch(
            "core.data_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(True, "explicit_grant", "view", "org-1"),
        ) as access,
        patch("core.db.get_connection", side_effect=_connection) as connection,
        patch("core.data_surface_api.compose_data_surface", return_value=envelope) as compose,
    ):
        response = TestClient(build_asgi_app(), raise_server_exceptions=False).get(
            "/api/projects/proj-1/imports"
        )

    assert response.status_code == 200
    assert response.json() == envelope
    assert response.headers["cache-control"] == "no-store"
    connection.assert_called_once_with()
    access.assert_called_once()
    assert access.call_args.kwargs["project_id"] == "proj-1"
    assert access.call_args.kwargs["minimum_capability"] == "view"
    compose.assert_called_once_with(
        "proj-1",
        "imports",
        access.call_args.args[1],
        object_id=None,
        can_edit=False,
        filters={"q": "", "state": "", "datastream": "", "from": "", "to": ""},
        limit=25,
        cursor=0,
    )


def test_the_paged_lenses_forward_the_cursor_the_screen_sent() -> None:
    """The route's own list of paged lenses was NOT the composer's.

    `Sources` has sent `?cursor=` since its pager was drawn, and this route
    answered it by dropping the parameter into the `else` branch: no filters, no
    limit, cursor 0. Page two was page one, forever. The set now comes from
    `core.data_surface` so the two cannot drift again.
    """
    from core.data_surface import PAGED_LENSES
    from core.main import build_asgi_app

    assert PAGED_LENSES == {"sources", "imports", "events", "connectors"}

    envelope = {
        "schema_version": "data-sources.v1",
        "project_ref": {"object_type": "project", "id": "proj-1"},
        "items": [],
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.db.install_access_context"),
        patch(
            "core.data_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(True, "explicit_grant", "view", "org-1"),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.data_surface_api.compose_data_surface", return_value=envelope) as compose,
    ):
        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        response = client.get("/api/projects/proj-1/source-accounts?cursor=25&limit=25")
        # A Source Account names an authorization scope, not a Datastream, so the
        # filter that narrows by Datastream is refused there rather than accepted
        # and silently narrowing to nothing.
        refused = client.get("/api/projects/proj-1/source-accounts?datastream=ds_1")
        events = client.get("/api/projects/proj-1/event-configurations?cursor=50&datastream=ds_1")

    assert response.status_code == 200
    assert compose.call_args_list[0].kwargs["cursor"] == 25
    assert compose.call_args_list[0].kwargs["limit"] == 25
    assert compose.call_args_list[0].kwargs["filters"] == {"q": "", "state": ""}

    assert refused.status_code == 422
    assert refused.json()["code"] == "invalid_query"

    assert events.status_code == 200
    assert compose.call_args_list[-1].kwargs["cursor"] == 50
    assert compose.call_args_list[-1].kwargs["filters"]["datastream"] == "ds_1"


def test_data_surface_denial_is_nondisclosing_and_never_composes() -> None:
    from core.main import build_asgi_app

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.db.install_access_context"),
        patch(
            "core.data_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(False, "grant_required"),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.data_surface_api.compose_data_surface", new=MagicMock()) as compose,
    ):
        response = TestClient(build_asgi_app(), raise_server_exceptions=False).get(
            "/api/projects/other-project/source-accounts/sacct_1"
        )

    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "Data object not found"}
    compose.assert_not_called()


def test_event_configuration_write_rechecks_project_edit_and_datastream_owner() -> None:
    from core.main import build_asgi_app

    result = {"event_configuration_id": "ecfg_1", "operation_id": "op_1"}
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.db.install_access_context"),
        patch(
            "core.data_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(True, "explicit_grant", "edit", "org-1"),
        ) as access,
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.event_configurations.create_event_configuration", return_value=result
        ) as create,
    ):
        response = TestClient(build_asgi_app(), raise_server_exceptions=False).post(
            "/api/projects/proj-1/datastreams/ds-1/event-configurations",
            headers={"Idempotency-Key": "idem-1"},
            json={"name": "Launch observations"},
        )

    assert response.status_code == 201
    assert response.json() == result
    assert access.call_args.kwargs["project_id"] == "proj-1"
    assert access.call_args.kwargs["minimum_capability"] == "edit"
    create.assert_called_once()
    assert create.call_args.kwargs["datastream_id"] == "ds-1"
    assert create.call_args.kwargs["org_id"] == "org-1"


def test_event_configuration_write_denial_is_nondisclosing() -> None:
    from core.main import build_asgi_app

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.db.install_access_context"),
        patch(
            "core.data_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(False, "insufficient_capability"),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.event_configurations.create_event_configuration") as create,
    ):
        response = TestClient(build_asgi_app(), raise_server_exceptions=False).post(
            "/api/projects/proj-1/datastreams/ds-1/event-configurations",
            headers={"Idempotency-Key": "idem-1"},
            json={"name": "Launch observations"},
        )

    assert response.status_code == 404
    create.assert_not_called()


# ---------------------------------------------------------------------------
# Story 58.8 -- the fleet lens, on real rows
# ---------------------------------------------------------------------------
#
# THE TRAP THIS FIXTURE EXISTS TO AVOID, measured on the disposable cluster on
# 2026-08-07: `app.project_flux` holds ZERO rows there, and the fleet query
# begins `FROM app.project_flux pf JOIN app.datastreams d`. A test that asks for
# the lens on any project of that database gets `items: []` and passes while
# proving nothing at all -- the 1258 Datastreams it contains are invisible to
# this query because nothing binds them to a Project. `credential_accounts` is
# empty for the same reason, so the source-account column has no existing
# fixture either. Both are seeded here, in the test's own transaction, and the
# transaction is rolled back.
#
# The mocked seams above cannot replace this one: a MagicMock cursor answers
# whatever it was told to, so it can neither execute `#> '{source,selection,
# grain}'` nor discover that `started_at` is not a column of
# `app.datastream_executions`.

IDENTITY = "reader@example.com"
AUTHOR = "story-58-8"

#: Crockford base32 without I, L, O and U -- the alphabet `ck_datastream_
#: executions_id` spells out, and the reason an id here cannot be a hex slice.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _ulid(prefix: str) -> str:
    return prefix + "".join(random.choice(_CROCKFORD) for _ in range(26))


@pytest.fixture
def a_fleet(live_postgres):
    """One Project bound to one Datastream, with everything 58.8 draws.

    Deliberately shaped so the four new facts are each provable: an account with
    a label, a current plan carrying a grain and a selection, a data role, and an
    execution whose `state_changed_at` is the instant the `Last run` cell shows.
    """
    conn = live_postgres
    ids = {
        "org": _id("org_"),
        "project": _id("proj_"),
        "empty_project": _id("proj_"),
        "datastream": _id("ds_"),
        "connection": _id("cref_"),
        "account": _ulid("sacct_"),
        "plan": _id("dsp_"),
        "mapping": _id("dmap_"),
        "execution": _ulid("dse_"),
    }
    selection = {
        "source": {
            "selection": {
                "grain": ["Date", "Campaign"],
                "metrics": ["Impressions", "Net cost"],
                "dimensions": ["Date", "Campaign", "Country"],
                "filters": [],
                "selection_mode": "subset",
            }
        }
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by, status)"
            " VALUES (%s, %s, %s, %s, 'active')",
            (ids["org"], ids["org"], ids["org"], AUTHOR),
        )
        for key in ("project", "empty_project"):
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by, org_id, status)"
                " VALUES (%s, %s, %s, %s, %s, 'active')",
                (ids[key], ids[key], ids[key], AUTHOR, ids["org"]),
            )
        cur.execute(
            "INSERT INTO app.connection_ref"
            " (id, provider, nango_connection_id, project_id, status, enabled,"
            "  owner_org_id, owner_identity)"
            " VALUES (%s, 'meta-ads', %s, %s, 'active', TRUE, %s, %s)",
            (ids["connection"], ids["connection"], ids["project"], ids["org"], IDENTITY),
        )
        # The source account the fleet must be able to NAME. Zero rows exist on
        # the disposable cluster, so nothing else could prove this column.
        cur.execute(
            "INSERT INTO app.credential_accounts"
            " (credential_id, external_account_id, label, source_account_id)"
            " VALUES (%s, 'act-000', 'Example brand account', %s)",
            (ids["connection"], ids["account"]),
        )
        cur.execute(
            "INSERT INTO app.datastreams (id, project_id, name, module_name,"
            " source_kind, connection_ref_id, enabled, created_by, org_id,"
            " data_role, source_account_id, schedule_mode)"
            " VALUES (%s, %s, 'Campaign performance', 'meta-ads', 'connector_pull',"
            "         %s, TRUE, %s, %s, 'Performance', %s, 'nightly')",
            (
                ids["datastream"],
                ids["project"],
                ids["connection"],
                AUTHOR,
                ids["org"],
                ids["account"],
            ),
        )
        # WITHOUT THIS ROW THE QUERY RETURNS NOTHING, whatever else is seeded.
        cur.execute(
            "INSERT INTO app.project_flux (project_id, flux_id, org_id)"
            " VALUES (%s, %s, %s)",
            (ids["project"], ids["datastream"], ids["org"]),
        )
        cur.execute(
            "INSERT INTO app.datastream_plan_versions"
            " (id, datastream_id, project_id, version_number, contract_version,"
            "  source_kind, writer_kind, destination_policy, normalized_payload,"
            "  content_hash, idempotency_key_hash, created_by, executable)"
            " VALUES (%s, %s, %s, 4, '1', 'connector_pull', 'toorow', 'managed_raw',"
            "         %s::jsonb, repeat('a', 64), repeat('b', 64), %s, TRUE)",
            (
                ids["plan"],
                ids["datastream"],
                ids["project"],
                json.dumps(selection),
                AUTHOR,
            ),
        )
        cur.execute(
            "INSERT INTO app.datastream_mapping_versions"
            " (id, datastream_id, project_id, version_number, mapping_contract_version,"
            "  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,"
            "  toorow_extension_version, executable, mapping_payload, ossie_projection,"
            "  idempotency_key_hash, created_by)"
            " VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('c', 64), '0.1.1',"
            "         '1', TRUE, '{}'::jsonb, '{}'::jsonb, repeat('d', 64), %s)",
            (ids["mapping"], ids["datastream"], ids["project"], ids["plan"], AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.datastream_executions"
            " (id, datastream_id, project_id, plan_version_id, mapping_version_id,"
            "  state, state_changed_at, created_by)"
            " VALUES (%s, %s, %s, %s, %s, 'failed', TIMESTAMPTZ '2026-08-06 23:10:00+00', %s)",
            (
                ids["execution"],
                ids["datastream"],
                ids["project"],
                ids["plan"],
                ids["mapping"],
                AUTHOR,
            ),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_plan_version_id = %s,"
            " current_mapping_version_id = %s WHERE id = %s",
            (ids["plan"], ids["mapping"], ids["datastream"]),
        )
    try:
        yield {"conn": conn, **ids}
    finally:
        conn.rollback()


def _read(ids, path: str):
    """Drive the real application against the fixture's own transaction."""
    from core.main import build_asgi_app

    connection = ids["conn"]

    @contextmanager
    def _open(*_a, **_k):
        yield connection

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY))),
        # `install_access_context` COMMITS the connection it arms (core/db.py:131).
        # Left alone it would commit this fixture's rows into the cluster, which
        # is the opposite of what the rollback above promises.
        patch("core.db.install_access_context"),
        patch("core.db.get_connection", side_effect=_open),
        patch(
            "core.data_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(True, "explicit_grant", "view", "org-1"),
        ),
    ):
        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        return client.get(path)


def _fleet(ids, project_id: str):
    return _read(ids, f"/api/projects/{project_id}/datastreams")


def test_the_fleet_lens_answers_with_what_the_row_collects_and_where_it_pulls(a_fleet) -> None:
    response = _fleet(a_fleet, a_fleet["project"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unavailable_reasons"] == []
    assert len(body["items"]) == 1
    evidence = body["items"][0]["evidence"]

    # The four facts story 58.8 opened, each read out of a real row.
    assert evidence["data_role"] == "Performance"
    assert evidence["source_account_id"] == a_fleet["account"]
    assert evidence["source_account_label"] == "Example brand account"
    assert evidence["plan_grain"] == ["Date", "Campaign"]
    assert evidence["plan_metric_count"] == 2
    assert evidence["plan_dimension_count"] == 3
    # `state_changed_at` of the latest execution -- proved against the deployed
    # column list rather than against a fixture's opinion of it.
    assert evidence["latest_run_at"].startswith("2026-08-06T23:10:00")
    assert evidence["latest_candidate_state"] == "failed"
    # And the query still answers everything it answered before.
    assert evidence["cadence"] == "nightly"


def test_the_sources_lens_counts_the_whole_collection_while_serving_one_page(a_fleet) -> None:
    """The page facts, over rows the database actually returned.

    A mocked cursor cannot prove this one: the `sources` query GROUPs BY eleven
    columns and aggregates `COUNT(DISTINCT pf.flux_id)`, so the count that ends
    up in `total` is the number of rows that survived that grouping — five
    Search Console properties of one consent are five Source Accounts, not one.
    """
    conn = a_fleet["conn"]
    second = _ulid("sacct_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.credential_accounts"
            " (credential_id, external_account_id, label, source_account_id)"
            " VALUES (%s, 'act-001', 'Example second property', %s)",
            (a_fleet["connection"], second),
        )

    response = _read(a_fleet, f"/api/projects/{a_fleet['project']}/source-accounts?limit=1")

    assert response.status_code == 200, response.text
    body = response.json()
    # One row served, and the collection says how many exist behind it.
    assert len(body["items"]) == 1
    assert body["total"] == 2
    assert body["bound"] == 1
    assert body["next_cursor"] == "1"
    assert body["applied_filters"] == {}
    # Declared from the states these two rows actually carry.
    assert "available" in body["filter_options"]["states"]
    # A Source Account names an authorization scope, never a Datastream.
    assert body["filter_options"]["datastreams"] == []

    page_two = _read(
        a_fleet, f"/api/projects/{a_fleet['project']}/source-accounts?limit=1&cursor=1"
    )
    assert page_two.status_code == 200, page_two.text
    assert len(page_two.json()["items"]) == 1
    assert page_two.json()["next_cursor"] is None
    # Two pages, two different accounts -- the cursor moved the window rather
    # than being dropped on the floor as it was until 2026-08-17.
    assert (
        page_two.json()["items"][0]["object_ref"]["id"] != body["items"][0]["object_ref"]["id"]
    )


def test_a_project_with_no_flux_binding_says_so_instead_of_returning_a_fleet(a_fleet) -> None:
    # Same organization, same cluster, same Datastream -- and no `project_flux`
    # row. This is the state 1258 of the disposable cluster's Datastreams are in,
    # and it must read as an empty Project rather than as somebody else's fleet.
    response = _fleet(a_fleet, a_fleet["empty_project"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["unavailable_reasons"], "an empty fleet must say why it is empty"
    assert body["unavailable_reasons"][0]["code"] == "datastreams_evidence_empty"
