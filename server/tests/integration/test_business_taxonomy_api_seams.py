"""ASGI seams for Story 45.1 business taxonomy and governed links."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest
from core.main import build_asgi_app
from starlette.testclient import TestClient


def _conn():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    return conn


def test_taxonomy_read_uses_project_to_organization_scope():
    app = build_asgi_app()
    client = TestClient(app)
    conn = _conn()
    payload = {
        "org_id": "org_01",
        "domains": [{"id": "bdm_sales", "slug": "sales", "name": "Sales", "status": "active"}],
        "classifications": [],
    }
    with (
        patch("core.admin_api._check_auth", return_value=(True, "viewer@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value="org_01"),
        patch("core.business_taxonomy.list_taxonomy", return_value=payload) as list_mock,
    ):
        response = client.get("/api/context/business-taxonomy?project_id=proj_01")

    assert response.status_code == 200
    assert response.json()["domains"][0]["slug"] == "sales"
    list_mock.assert_called_once_with(conn, org_id="org_01", status="active")


def test_analysis_picker_taxonomy_is_server_bounded_and_compact():
    app = build_asgi_app()
    client = TestClient(app)
    conn = _conn()
    payload = {
        "org_id": "org_01",
        "domains": [
            {
                "id": f"bd_{index}",
                "slug": f"domain-{index}",
                "name": f"Domain {index}",
                "status": "active",
                "version_number": 1,
            }
            for index in range(201)
        ],
        "classifications": [],
    }
    with (
        patch("core.admin_api._check_auth", return_value=(True, "viewer@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value="org_01"),
        patch(
            "core.business_taxonomy.list_domain_picker",
            return_value=payload["domains"],
        ) as list_mock,
    ):
        response = client.get(
            "/api/context/business-taxonomy"
            "?project_id=proj_01&projection=analysis-picker"
        )

    assert response.status_code == 200
    assert len(response.json()["domains"]) == 200
    assert response.json()["truncated"] is True
    assert response.json()["classifications"] == []
    assert "slug" not in response.text
    list_mock.assert_called_once_with(conn, org_id="org_01", status="active", limit=201)


def test_cross_tenant_taxonomy_read_is_non_disclosing():
    app = build_asgi_app()
    client = TestClient(app)
    conn = _conn()
    with (
        patch("core.admin_api._check_auth", return_value=(True, "outsider@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value=None),
    ):
        response = client.get("/api/context/business-taxonomy?project_id=proj_private")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_create_governed_link_is_project_scoped_and_audited_by_store():
    app = build_asgi_app()
    client = TestClient(app)
    conn = _conn()
    created = {
        "id": "blink_01",
        "org_id": "org_01",
        "project_id": "proj_01",
        "taxonomy_type": "business_domain",
        "taxonomy_id": "bdm_sales",
        "target_type": "report_view",
        "target_id": "google-analytics/acquisition",
        "relation_type": "explains",
        "link_origin": "direct",
    }
    with (
        patch("core.admin_api._check_auth", return_value=(True, "member@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value="org_01"),
        patch("core.business_taxonomy.create_link", return_value=created) as create_mock,
    ):
        response = client.post(
            "/api/context/business-links?project_id=proj_01",
            json={
                "taxonomy_type": "business_domain",
                "taxonomy_id": "bdm_sales",
                "target_type": "report_view",
                "target_id": "google-analytics/acquisition",
                "relation_type": "explains",
                "reason": "Connect acquisition reporting to Sales",
                "trace_id": "trace_01",
            },
        )

    assert response.status_code == 201
    assert response.json()["link_origin"] == "direct"
    assert create_mock.call_args.kwargs["project_id"] == "proj_01"
    assert create_mock.call_args.kwargs["org_id"] == "org_01"
    assert create_mock.call_args.kwargs["actor"] == "member@example.com"
    assert conn.commit.called


def test_business_link_rejects_unknown_target_type():
    app = build_asgi_app()
    client = TestClient(app)
    conn = _conn()
    with (
        patch("core.admin_api._check_auth", return_value=(True, "member@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value="org_01"),
    ):
        response = client.post(
            "/api/context/business-links?project_id=proj_01",
            json={
                "taxonomy_type": "business_domain",
                "taxonomy_id": "bdm_sales",
                "target_type": "frontend_url",
                "target_id": "/reports/acquisition",
                "relation_type": "explains",
                "reason": "Invalid arbitrary UI route",
            },
        )
    assert response.status_code == 422


def test_business_path_preview_is_scoped_and_does_not_persist_evidence():
    app = build_asgi_app()
    client = TestClient(app)
    conn = _conn()
    preview = {
        "path_key": "ctxp_preview",
        "target": {"type": "target_field", "id": "sessions"},
        "link_origin": "direct",
        "ordered_path": [],
        "source_versions": [],
    }
    with (
        patch("core.admin_api._check_auth", return_value=(True, "viewer@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value="org_01"),
        patch("core.business_taxonomy.preview_business_path", return_value=preview) as path_mock,
    ):
        response = client.post(
            "/api/context/business-paths/preview?project_id=proj_01",
            json={"target_type": "target_field", "target_id": "sessions"},
        )

    assert response.status_code == 200
    assert response.json()["path_key"] == "ctxp_preview"
    assert path_mock.call_args.kwargs["org_id"] == "org_01"
    assert not conn.commit.called


# ---------------------------------------------------------------------------
# Live finding F1/F2 (2026-08-05): strict status filter, relation vocabulary
# ---------------------------------------------------------------------------


def test_taxonomy_status_filter_is_strict_and_validated():
    """`status=archived` must reach the store as "archived" (it returned
    NOTHING before: only `status=all` included archived rows), and an unknown
    value is a 422, not a silent default."""
    app = build_asgi_app()
    client = TestClient(app)
    conn = _conn()
    with (
        patch("core.admin_api._check_auth", return_value=(True, "viewer@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value="org_01"),
        patch(
            "core.business_taxonomy.list_taxonomy",
            return_value={"org_id": "org_01", "domains": [], "classifications": []},
        ) as list_mock,
    ):
        archived = client.get(
            "/api/context/business-taxonomy?project_id=proj_01&status=archived"
        )
        bogus = client.get("/api/context/business-taxonomy?project_id=proj_01&status=bogus")

    assert archived.status_code == 200
    list_mock.assert_called_once_with(conn, org_id="org_01", status="archived")
    assert bogus.status_code == 422
    assert bogus.json()["code"] == "invalid_param"


def test_business_link_rejects_a_relation_outside_the_vocabulary():
    """Free-form relation_type made every link its own vocabulary (F2). The
    canonical set mirrors the console's Link dialog, post-normalize_slug."""
    app = build_asgi_app()
    client = TestClient(app)
    conn = _conn()
    with (
        patch("core.admin_api._check_auth", return_value=(True, "member@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value="org_01"),
    ):
        response = client.post(
            "/api/context/business-links?project_id=proj_01",
            json={
                "taxonomy_type": "business_domain",
                "taxonomy_id": "bdm_sales",
                "target_type": "topic",
                "target_id": "top_01",
                "relation_type": "nimporte-quoi",
                "reason": "Relation hors vocabulaire",
            },
        )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_param"


def test_retiring_a_governed_link_without_a_reason_is_refused_at_the_door():
    """The route no longer accepts what the writer has always refused.

    Live finding F3 removed the 422 "JSON object body required" this route
    answered when the caller sent nothing, on the ground that the body carried
    nothing required. That was half true: `reason` was described as optional
    audit metadata here while `business_taxonomy._audit` has ALWAYS called
    `_required(reason, ...)`, so a body-less DELETE was accepted by the route
    and then raised inside the writer, on a field the route called optional.

    Now that the act is a RETIREMENT (migration 306) the reason is what tells a
    wrong link from an inconvenient one, so it is refused here, with a sentence
    that names what to send -- and the WRITER IS NEVER REACHED, which is the
    part that makes this a door and not a second guard.
    """
    app = build_asgi_app()
    client = TestClient(app)
    conn = _conn()
    with (
        patch("core.admin_api._check_auth", return_value=(True, "member@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value="org_01"),
        patch("core.business_taxonomy.retire_link", return_value={"id": "lnk_01"}) as del_mock,
    ):
        response = client.request(
            "DELETE", "/api/context/business-links/lnk_01?project_id=proj_01"
        )

    assert response.status_code == 422
    assert response.json()["code"] == "reason_required"
    del_mock.assert_not_called()


def test_retiring_a_governed_link_carries_the_reason_it_was_given():
    """The reason the console always sends reaches the writer unchanged."""
    app = build_asgi_app()
    client = TestClient(app)
    conn = _conn()
    with (
        patch("core.admin_api._check_auth", return_value=(True, "member@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value="org_01"),
        patch("core.business_taxonomy.retire_link", return_value={"id": "lnk_01"}) as del_mock,
    ):
        response = client.request(
            "DELETE",
            "/api/context/business-links/lnk_01?project_id=proj_01",
            json={"reason": "Link no longer governs this view"},
        )

    assert response.status_code == 204
    assert del_mock.call_args.kwargs["reason"] == "Link no longer governs this view"


# ---------------------------------------------------------------------------
# THE FOUR IDENTITY DOORS REFUSE -- story 49.3, the acceptance schedule ratified
# on 2026-08-25: *"The legacy writers refuse from now on (409 naming the
# convergence gesture); an unconverged organization converges first, then writes
# through the Master Data authority."*
#
# WHAT THIS BLOCK REPLACES: nothing in this file, and that is itself a finding.
# The four write doors of this surface had NO seam test -- only the two link
# doors did. The store-level proofs went with the writers, in
# `tests/core/test_business_taxonomy.py`; these are the wire contract, which
# nothing was holding before.
# ---------------------------------------------------------------------------

_IDENTITY_DOORS = (
    ("POST", "/api/context/business-domains?project_id=proj_01"),
    ("PATCH", "/api/context/business-domains/bdm_01?project_id=proj_01"),
    ("POST", "/api/context/business-classifications?project_id=proj_01"),
    ("PATCH", "/api/context/business-classifications/bcl_01?project_id=proj_01"),
)


@pytest.mark.parametrize(("verb", "path"), _IDENTITY_DOORS)
def test_every_identity_door_answers_409_legacy_store_is_read_only(verb, path):
    client = TestClient(build_asgi_app())

    with patch("core.admin_api._check_auth", return_value=(True, "owner@example.com")):
        response = client.request(verb, path, json={"name": "Retail", "reason": "why"})

    assert response.status_code == 409, (verb, path, response.text)
    assert response.json()["code"] == "legacy_store_is_read_only"


@pytest.mark.parametrize(("verb", "path"), _IDENTITY_DOORS)
def test_the_refusal_names_the_gesture_and_never_a_store(verb, path):
    client = TestClient(build_asgi_app())

    with patch("core.admin_api._check_auth", return_value=(True, "owner@example.com")):
        message = client.request(verb, path, json={}).json()["message"]

    assert "Converge" in message and "Master Data" in message, message
    for db_word in ("mdm_", "app.", "table", "column"):
        assert db_word not in message, (db_word, message)


@pytest.mark.parametrize(("verb", "path"), _IDENTITY_DOORS)
def test_a_refused_write_never_opens_a_connection(verb, path):
    """The refusal is BEFORE the store and before the scope read, not after."""
    client = TestClient(build_asgi_app())

    with (
        patch("core.admin_api._check_auth", return_value=(True, "owner@example.com")),
        patch("core.db.get_connection") as connection_mock,
        patch("core.business_taxonomy.create_domain") as create_domain_mock,
    ):
        client.request(verb, path, json={"name": "Retail"})

    connection_mock.assert_not_called()
    create_domain_mock.assert_not_called()


@pytest.mark.parametrize(("verb", "path"), _IDENTITY_DOORS)
def test_the_refusal_is_unconditional_including_without_a_token(verb, path):
    """409 even unauthenticated -- the same shape `datamodel_api` took the same day.

    The body carries no tenant fact: one code and one sentence, identical for
    every caller. Checking a token first would answer 401 to a request that was
    going to be refused anyway, and would put a branch back on a handler that
    must have none.
    """
    client = TestClient(build_asgi_app())

    response = client.request(verb, path, json={"name": "Retail"})

    assert response.status_code == 409
    assert response.json()["code"] == "legacy_store_is_read_only"


def test_a_write_door_is_never_unmounted_because_404_sends_a_caller_looking():
    """All four still answer their verb. A 405 or a 404 would be the other bug."""
    client = TestClient(build_asgi_app())

    for verb, path in _IDENTITY_DOORS:
        assert client.request(verb, path, json={}).status_code == 409, (verb, path)


def test_the_reads_and_the_link_doors_stayed():
    """The other half of the cutover: a superseded source is readable forever.

    And the two link doors keep writing -- a link is not an identity, and the
    amendment's own *Incomplete if* forbids a retirement whose repair does not
    exist.
    """
    client = TestClient(build_asgi_app())
    conn = _conn()
    with (
        patch("core.admin_api._check_auth", return_value=(True, "viewer@example.com")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.business_taxonomy_api._authorize_scope", return_value="org_01"),
        patch(
            "core.business_taxonomy.list_taxonomy",
            return_value={"org_id": "org_01", "domains": [], "classifications": []},
        ),
        patch("core.business_taxonomy.retire_link", return_value={"id": "lnk_01"}),
    ):
        read = client.get("/api/context/business-taxonomy?project_id=proj_01")
        retire = client.request(
            "DELETE",
            "/api/context/business-links/lnk_01?project_id=proj_01",
            json={"reason": "withdrawn by mistake, will be made again"},
        )

    assert read.status_code == 200
    assert retire.status_code == 204


def test_the_refusal_that_escapes_a_writer_is_a_409_and_not_a_500():
    """The second line of defence, and it is not decoration.

    Every operation in this module runs inside `_with_scope`, whose `except`
    funnels into `_error_response`. Without a branch for the cutover, a
    `LegacyTaxonomyWriteRefused` raised by a store function would fall through to
    the generic case and answer 500 "Context Hub is temporarily unavailable" --
    a caller told to wait, for a request that will never be accepted again.
    """
    from core import business_taxonomy as taxonomy
    from core.business_taxonomy_api import _error_response

    response = _error_response(
        taxonomy.LegacyTaxonomyWriteRefused(taxonomy.LEGACY_TAXONOMY_WRITE_REFUSED_MESSAGE)
    )

    assert response.status_code == 409
    assert json.loads(bytes(response.body))["code"] == "legacy_store_is_read_only"
