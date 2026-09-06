"""Story 49.1 — the three Governance reads, on the PRODUCTION app factory.

Mounted through `build_asgi_app()` rather than a hand-built Router, because a
route that exists only in a test fixture is exactly the "server capability with
no door" this repository keeps producing.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from core.project_access import AccessDecision
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

COLLECTION = "/api/projects/proj_EXAMPLE/governance/master-data"
OBJECT = f"{COLLECTION}/objects/business-domain/bd_1"
VERSION = f"{COLLECTION}/objects/registry/reg_1/versions/pcv_1"


class _Connection:
    def __init__(self):
        self.commit = MagicMock()


@contextmanager
def _connection():
    yield _Connection()


def _client():
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _allowed(capability: str = "view"):
    return AccessDecision(True, "explicit_grant", capability, "org_EXAMPLE")


def _envelope(**overrides):
    body = {
        "schema_version": "governance-collection.v1",
        "project_ref": {"object_type": "project", "id": "proj_EXAMPLE"},
        "organization_ref": {"object_type": "organization", "id": "org_EXAMPLE"},
        "section": "master-data",
        "lens": "business-domains",
        "items": [],
        "unavailable_reasons": [],
    }
    body.update(overrides)
    return body


def test_collection_route_is_mounted_and_reads_one_authorized_snapshot():
    with (
        patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access", return_value=_allowed()
        ) as access,
        patch("core.db.get_connection", side_effect=_connection) as connection,
        patch(
            "core.governance_surface_api.compose_governance_collection", return_value=_envelope()
        ) as compose,
    ):
        response = _client().get(f"{COLLECTION}?lens=classifications")

    assert response.status_code == 200
    assert response.json() == _envelope()
    assert response.headers["cache-control"] == "no-store"
    connection.assert_called_once_with()
    assert access.call_args.kwargs["project_id"] == "proj_EXAMPLE"
    assert access.call_args.kwargs["minimum_capability"] == "view"
    assert access.call_args.kwargs["hold_access"] is True
    # `query` is always passed, and is empty when the caller sent no bounded
    # cursor or filter. Story 49.5 added it; the three route shapes did not
    # change, and nothing else about this call did either.
    compose.assert_called_once_with(
        "proj_EXAMPLE",
        "master-data",
        access.call_args.args[1],
        lens="classifications",
        org_id="org_EXAMPLE",
        query={},
    )


def test_object_and_version_routes_are_mounted_and_pass_the_exact_identifiers():
    detail = {
        "schema_version": "governance-object.v1",
        "project_ref": {"object_type": "project", "id": "proj_EXAMPLE"},
        "section": "master-data",
        "object": None,
        "unavailable_reasons": [],
    }
    with (
        patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access", return_value=_allowed()
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.governance_surface_api.compose_governance_object", return_value=detail) as obj,
        patch(
            "core.governance_surface_api.compose_governance_object_version", return_value=detail
        ) as version,
    ):
        client = _client()
        assert client.get(OBJECT).status_code == 200
        assert client.get(VERSION).status_code == 200

    assert obj.call_args.args[:4] == ("proj_EXAMPLE", "master-data", "business-domain", "bd_1")
    assert version.call_args.args[:5] == (
        "proj_EXAMPLE",
        "master-data",
        "registry",
        "reg_1",
        "pcv_1",
    )
    assert version.call_args.kwargs["org_id"] == "org_EXAMPLE"


def test_an_opaque_identifier_survives_url_encoding_without_becoming_a_label():
    with (
        patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access", return_value=_allowed()
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.governance_surface_api.compose_governance_object",
            return_value={"object": None, "unavailable_reasons": []},
        ) as compose,
    ):
        _client().get(f"{COLLECTION}/objects/business-domain/bd%201%20a")

    # The id reaches the read model as the opaque string it is — not trimmed to a
    # slug, not resolved through a label.
    assert compose.call_args.args[3] == "bd 1 a"


def test_an_unauthenticated_read_never_reaches_the_read_model():
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))),
        patch(
            "core.governance_surface_api.compose_governance_collection", new=MagicMock()
        ) as compose,
    ):
        response = _client().get(COLLECTION)

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    compose.assert_not_called()


def test_a_cross_project_read_is_existence_hiding_and_never_composes():
    with (
        patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(False, "not_found"),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.governance_surface_api.compose_governance_object", new=MagicMock()) as compose,
    ):
        response = _client().get(
            "/api/projects/proj_OTHER/governance/evidence/objects/audit-event/audit_1"
        )

    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "Governance object not found"}
    compose.assert_not_called()


def test_a_grant_that_is_below_the_minimum_capability_is_denied_not_hidden():
    # `insufficient_capability` is the one denial that concerns a caller who IS
    # an active member of this organization. Collapsing it into 404 would tell
    # them the Project does not exist, which is false.
    with (
        patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(False, "insufficient_capability"),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.governance_surface_api.compose_governance_collection", new=MagicMock()
        ) as compose,
    ):
        response = _client().get(COLLECTION)

    assert response.status_code == 403
    assert response.json()["code"] == "denied"
    compose.assert_not_called()


def test_a_dependency_failure_is_unavailable_rather_than_a_healthy_empty_answer():
    with (
        patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(False, "access_unavailable"),
        ),
        patch("core.db.get_connection", side_effect=_connection),
    ):
        response = _client().get(COLLECTION)

    assert response.status_code == 503
    assert response.json()["code"] == "governance_access_unavailable"
    assert response.headers["cache-control"] == "no-store"


def test_an_owner_that_raises_fails_closed_without_leaking_the_reason():
    with (
        patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access", return_value=_allowed()
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.governance_surface_api.compose_governance_collection",
            side_effect=RuntimeError("relation app.mdm_business_domains does not exist"),
        ),
    ):
        response = _client().get(COLLECTION)

    assert response.status_code == 503
    assert response.json() == {
        "code": "governance_unavailable",
        "message": "Governance is unavailable",
    }


def test_an_unregistered_section_lens_or_type_is_an_unknown_address_not_a_fallback():
    from core.governance_read_model import GovernanceUnknownRoute

    with (
        patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access", return_value=_allowed()
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.governance_surface_api.compose_governance_collection",
            side_effect=GovernanceUnknownRoute("unknown lens"),
        ),
    ):
        response = _client().get("/api/projects/proj_EXAMPLE/governance/mapping")

    assert response.status_code == 404
    assert response.json()["code"] == "unknown_route"


def test_three_generic_shapes_and_the_exact_country_owner_reach_the_running_app():
    """Three generic READ shapes plus Country's exact workbench owner.

    This used to assert the route set was exactly the three reads. That was the
    contract until the Master Data command needed a way in -- its guard and its
    audit existed with nothing able to reach them. The invariant kept here is
    the one that still holds: the generic reads plus named exact owners are
    explicit, and every GET route is GET-only.
    """
    from core.governance_surface_api import GOVERNANCE_SURFACE_ROUTES

    read_routes = [r for r in GOVERNANCE_SURFACE_ROUTES if "GET" in r.methods]
    assert {route.path for route in read_routes} == {
        "/api/projects/{project_id}/governance/master-data/country",
        # Story 49.2 AC1: the convergence PLAN. It is a read with its own exact
        # address rather than a query parameter on the command, so seeing what
        # would move needs `view` and moving it needs `manage`.
        "/api/projects/{project_id}/governance/master-data/convergence",
        # 2026-08-25: the preconfigured metric offer. An exact address for the
        # same reason as the two above -- it answers what THIS Project could
        # adopt, and adopting travels on the change-set routes, which hold
        # `edit` where this holds `view`.
        "/api/projects/{project_id}/governance/semantic-model/metric-presets",
        "/api/projects/{project_id}/governance/{section}",
        "/api/projects/{project_id}/governance/{section}/objects/{object_type}/{object_id}",
        "/api/projects/{project_id}/governance/{section}/objects/{object_type}/{object_id}/versions/{version_id}",
    }
    assert all(route.methods == {"GET", "HEAD"} for route in read_routes)

    # Reaching the auth gate proves the path matched a real mounted route; a
    # path that is not mounted answers 404 before authentication is ever asked.
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        client = _client()
        for path in (COLLECTION, OBJECT, VERSION):
            assert client.get(path).status_code == 401, path
        unmounted = client.get(f"{VERSION}/deeper")
    assert unmounted.status_code == 404


def test_the_reads_are_read_only():
    """The Story 49.2 write is its own address; it never widens a read route.

    A POST accepted on the collection or object path would make the same URL
    both a projection and a command, and the capability required would depend on
    the verb rather than the address.
    """
    from core.governance_surface_api import GOVERNANCE_SURFACE_ROUTES

    for route in GOVERNANCE_SURFACE_ROUTES:
        if "GET" not in route.methods:
            continue
        assert "POST" not in route.methods
        assert "PATCH" not in route.methods
        assert "DELETE" not in route.methods
    with patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person@example.com"))
    ):
        assert _client().post(COLLECTION).status_code == 405


# ---------------------------------------------------------------------------
# Story 49.5 — the Evidence index, on the same three routes and no others.
# ---------------------------------------------------------------------------

EVIDENCE = "/api/projects/proj_EXAMPLE/governance/evidence"


@contextmanager
def _authorized(**extra):
    """The four patches every Evidence seam shares, plus whatever it adds.

    An `ExitStack` rather than four repeated `with` lines: repeating them is how
    one of the four quietly goes missing in the fifth test and that test proves
    something weaker than it claims.
    """
    from contextlib import ExitStack

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "core.admin_api._check_auth",
                new=AsyncMock(return_value=(True, "person@example.com")),
            )
        )
        stack.enter_context(patch("core.db.install_access_context"))
        stack.enter_context(
            patch(
                "core.governance_surface_api.resolve_strict_resource_access",
                return_value=_allowed(),
            )
        )
        stack.enter_context(patch("core.db.get_connection", side_effect=_connection))
        yield {
            name: stack.enter_context(patch(f"core.governance_surface_api.{name}", **options))
            for name, options in extra.items()
        }


def _detail(**overrides):
    body = {
        "schema_version": "governance-object.v1",
        "project_ref": {"object_type": "project", "id": "proj_EXAMPLE"},
        "section": "evidence",
        "object": None,
        "unavailable_reasons": [],
    }
    body.update(overrides)
    return body


def test_evidence_gains_no_route_family_of_its_own():
    """Three lenses and three object types, on the SAME three shapes.

    A fourth route, a nested `/versions/` under an Evidence Record, or a
    `/graph` endpoint would each be a second address for the same object.
    """
    from core.governance_surface_api import GOVERNANCE_SURFACE_ROUTES

    # Counted over the READ routes: Story 49.2 added one write, which is not a
    # route family for Evidence and does not change what this test protects.
    read_routes = [
        r for r in GOVERNANCE_SURFACE_ROUTES
        if "GET" in r.methods and "{section}" in r.path
    ]
    assert len(read_routes) == 3
    assert not any("evidence" in route.path for route in GOVERNANCE_SURFACE_ROUTES)


def test_the_three_evidence_lenses_and_three_object_types_reach_the_read_model():
    envelope = _envelope(section="evidence", lens="lineage-provenance")
    with _authorized(
        compose_governance_collection={"return_value": envelope},
        compose_governance_object={"return_value": _detail()},
    ) as patched:
        client = _client()
        for lens in ("lineage-provenance", "versions-approvals", "audit-activity"):
            assert client.get(f"{EVIDENCE}?lens={lens}").status_code == 200
        for object_type in ("evidence-trace", "object-version", "audit-event"):
            assert client.get(f"{EVIDENCE}/objects/{object_type}/evr_1").status_code == 200

        collection = patched["compose_governance_collection"]
        obj = patched["compose_governance_object"]
        assert [call.kwargs["lens"] for call in collection.call_args_list] == [
            "lineage-provenance",
            "versions-approvals",
            "audit-activity",
        ]
        assert [call.args[2] for call in obj.call_args_list] == [
            "evidence-trace",
            "object-version",
            "audit-event",
        ]


def test_only_the_allowlisted_query_parameters_reach_the_read_model():
    envelope = _envelope(section="evidence", lens="audit-activity")
    with _authorized(compose_governance_collection={"return_value": envelope}) as patched:
        client = _client()
        response = client.get(
            f"{EVIDENCE}?lens=audit-activity&outcome=success&limit=25"
            "&correlation_kind=operation&correlation_id=op_1"
        )
        assert response.status_code == 200
        # An undeclared parameter is REFUSED, not dropped: a silently ignored
        # filter renders a page that does not match the address that produced it.
        refused = client.get(f"{EVIDENCE}?lens=audit-activity&project_id=proj_OTHER")

        assert refused.status_code == 400
        assert refused.json()["code"] == "invalid_query"
        assert patched["compose_governance_collection"].call_args.kwargs["query"] == {
            "outcome": "success",
            "limit": "25",
            "correlation_kind": "operation",
            "correlation_id": "op_1",
        }


def test_an_out_of_range_limit_is_refused_rather_than_clamped():
    """Clamping would answer a question nobody asked.

    A caller who asked for 5000 records gets a refusal, not a silent 200 with a
    different bound than the one their address states.
    """
    with _authorized(compose_governance_collection={"return_value": _envelope(section="evidence")}):
        client = _client()
        for bad in ("0", "5000", "-1", "many"):
            response = client.get(f"{EVIDENCE}?lens=audit-activity&limit={bad}")
            assert response.status_code == 400, bad
            assert response.json()["code"] == "invalid_query"


def test_an_invalid_cursor_fails_closed_instead_of_returning_page_one():
    from core.evidence_index import EvidenceCursorInvalid

    with _authorized(
        compose_governance_collection={
            "side_effect": EvidenceCursorInvalid("cursor is scoped elsewhere")
        }
    ):
        response = _client().get(f"{EVIDENCE}?lens=audit-activity&cursor=bm90LW1pbmU")

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_query"
    assert response.headers["cache-control"] == "no-store"


def test_every_evidence_read_is_no_store():
    with _authorized(
        compose_governance_collection={"return_value": _envelope(section="evidence")},
        compose_governance_object={"return_value": _detail()},
    ):
        client = _client()
        for path in (f"{EVIDENCE}?lens=audit-activity", f"{EVIDENCE}/objects/audit-event/evr_1"):
            assert client.get(path).headers["cache-control"] == "no-store", path


def test_project_access_is_resolved_before_the_index_is_read():
    """A caller with no grant never reaches the index at all.

    Not merely "gets no rows": the read model is not called, so there is no
    measurable difference between a Project that exists and one that does not.
    """
    with (
        patch(
            "core.admin_api._check_auth",
            new=AsyncMock(return_value=(True, "stranger@example.com")),
        ),
        patch("core.db.install_access_context"),
        patch(
            "core.governance_surface_api.resolve_strict_resource_access",
            return_value=AccessDecision(False, "no_grant"),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.governance_surface_api.compose_governance_collection") as compose,
    ):
        response = _client().get(f"{EVIDENCE}?lens=audit-activity")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    compose.assert_not_called()
