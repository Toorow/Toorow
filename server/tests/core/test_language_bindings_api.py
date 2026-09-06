"""Story 27.8 -- the language family finally has an address.

WHAT THIS FILE PINS. `core/language_dimensions.py` shipped a complete binding
lifecycle -- propose, persist, confirm, reject, list, resolve -- and measured on
2026-08-17 every one of those functions had ZERO production callers: the only
consumer of the module anywhere was the comparability guard in `query_specs.py`.
A client could not state that a column carries the language they TARGETED rather
than the language OBSERVED on the person, which is the entire distinction the
module exists to protect. These tests hold the other half: that a route reaches
the lifecycle, that what it writes is what the resolver reads, and that the
invariants the primitive defends are not loosened by giving it a door.

Offline (no DB): the family the surface offers is the family the guard knows, and
a target outside it is refused with the family named rather than stored.

Live Postgres (skipped when TEST_POSTGRES_DSN is unset): declaring makes the
resolver resolve, retiring makes it stop, a non-member does not learn the Project
exists, a reader may not write, and a binding id in a project URL cannot reach a
row belonging to another scope.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import language_bindings_api as api  # noqa: E402

OWNER = "owner@example.com"
STRANGER = "stranger@example.com"
VIEWER = "viewer@example.com"

CONNECTOR = "example-ads"
REPORT = "daily"
FIELD = "creativeLanguage"


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


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Offline -- the vocabulary the surface offers is the one the guard enforces.
# ---------------------------------------------------------------------------


def test_the_surface_offers_exactly_the_three_dimensions_of_the_family():
    """Three concepts under one word: the surface never flattens them to one."""
    from core.language_dimensions import LANGUAGE_DIMENSION_FAMILY

    listed = api.family_reference()
    assert [entry["dimension"] for entry in listed] == list(LANGUAGE_DIMENSION_FAMILY)
    assert len(listed) == 3
    # Each carries WHAT IT IS ABOUT -- the reason the three cannot collapse.
    natures = {entry["nature"] for entry in listed}
    assert natures == {"observed_on_person", "property_of_asset", "declared_intent"}
    assert all(entry["definition"] for entry in listed)


def test_a_target_outside_the_family_is_refused_with_the_family_named():
    """A request to bind a column to `country` is a wrong address, not a value error."""
    refused = api._reject_non_family("country")
    assert refused is not None
    assert refused.status_code == 400
    body = json.loads(refused.body)
    assert body["code"] == "not_a_language_dimension"
    # The refusal names the gesture that repairs it: the three legal targets.
    for dimension in ("audience_language", "content_language", "targeting_language"):
        assert dimension in body["message"]


def test_each_member_of_the_family_is_accepted_as_a_target():
    from core.language_dimensions import LANGUAGE_DIMENSION_FAMILY

    for dimension in LANGUAGE_DIMENSION_FAMILY:
        assert api._reject_non_family(dimension) is None


# ---------------------------------------------------------------------------
# The harness: a real Request against the real handlers, with auth stubbed.
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_org():
    """One org, one project, one owner and one viewer; dropped at the end."""
    from core.db import get_connection

    org_id, project_id = _uid("org"), _uid("proj")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 27.8 fixture', %s, 'active', %s)",
                (org_id, org_id.replace("_", "-"), OWNER),
            )
            for identity, role in ((OWNER, "owner"), (VIEWER, "viewer")):
                cur.execute(
                    "INSERT INTO app.org_members (id, org_id, identity, role, status) "
                    "VALUES (%s, %s, %s, %s, 'active')",
                    (_uid("mem"), org_id, identity, role),
                )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 27.8 fixture', %s, %s)",
                (project_id, org_id, project_id.replace("_", "-"), OWNER),
            )
        conn.commit()
    yield {"org_id": org_id, "project_id": project_id}

    from tests.conftest import purge_fixture_org

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.dimension_field_bindings WHERE project_id = %s",
                (project_id,),
            )
        conn.commit()
        purge_fixture_org(conn, org_id)
        conn.commit()


def _request(method: str, path_params: dict, body: dict | None = None):
    """A real Starlette Request -- the handler is exercised, not a stand-in."""
    from starlette.requests import Request

    payload = json.dumps(body or {}).encode()
    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "path_params": path_params,
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


@pytest.fixture
def as_identity(monkeypatch):
    """Stub the shared bearer check; the ORG GUARD under test is left real."""

    def _use(identity: str | None):
        async def _check(_request):
            return (identity is not None), (identity or "")

        import core.admin_api as admin_api

        monkeypatch.setattr(admin_api, "_check_auth", _check)

    return _use


def _call(handler, method, path_params, body=None):
    """Drive an async handler the way the rest of the suite does (no pytest-asyncio)."""
    return asyncio.run(handler(_request(method, path_params, body)))


def _json(response):
    return json.loads(response.body)


# ---------------------------------------------------------------------------
# Live Postgres -- what is written is what the resolver reads.
# ---------------------------------------------------------------------------


@pg_available
def test_declaring_a_binding_makes_the_resolver_resolve_it(
    fixture_org, as_identity
):
    """The point of the whole module: a declaration must reach the reading.

    `resolve_field_bindings` is the function the conformance path consults. If a
    POST does not change its answer, the surface is theatre.
    """
    from core.language_dimensions import resolve_field_bindings

    project_id = fixture_org["project_id"]
    as_identity(OWNER)

    key = (CONNECTOR, REPORT, FIELD)
    assert key not in resolve_field_bindings(project_id=project_id)

    created = _call(
        api._declare,
        "POST",
        {"project_id": project_id},
        {
            "connector": CONNECTOR,
            "report_id": REPORT,
            "source_field": FIELD,
            "canonical_dimension": "content_language",
        },
    )
    assert created.status_code == 201
    binding = _json(created)["binding"]
    assert binding["status"] == "confirmed"
    assert binding["canonical_dimension"] == "content_language"
    # The human is recorded as the reviewer AND as the evidence source.
    assert binding["reviewed_by"] == OWNER
    assert binding["evidence_source"] == "human"

    resolved = resolve_field_bindings(project_id=project_id)
    assert resolved[key] == "content_language"


@pg_available
def test_retiring_a_binding_makes_the_resolver_stop_and_keeps_the_trace(
    fixture_org, as_identity
):
    """A retirement is a recorded refusal, not a deletion."""
    from core.language_dimensions import resolve_field_bindings

    project_id = fixture_org["project_id"]
    as_identity(OWNER)

    created = _call(
        api._declare,
        "POST",
        {"project_id": project_id},
        {
            "connector": CONNECTOR,
            "report_id": REPORT,
            "source_field": FIELD,
            "canonical_dimension": "targeting_language",
        },
    )
    binding_id = _json(created)["binding"]["id"]
    assert resolve_field_bindings(project_id=project_id)[
        (CONNECTOR, REPORT, FIELD)
    ] == "targeting_language"

    retired = _call(
        api._retire, "DELETE", {"project_id": project_id, "binding_id": binding_id}
    )
    assert retired.status_code == 200
    assert _json(retired)["binding"]["status"] == "rejected"

    # It stopped applying...
    assert (CONNECTOR, REPORT, FIELD) not in resolve_field_bindings(
        project_id=project_id
    )
    # ...and the row is still there, with who refused it.
    listed = _call(api._list, "GET", {"project_id": project_id})
    rows = _json(listed)["bindings"]
    assert [row["status"] for row in rows] == ["rejected"]
    assert rows[0]["reviewed_by"] == OWNER


@pg_available
def test_the_three_dimensions_coexist_on_one_project(fixture_org, as_identity):
    """Three columns, three dimensions, never folded into one."""
    project_id = fixture_org["project_id"]
    as_identity(OWNER)

    for field, dimension in (
        ("browserLanguage", "audience_language"),
        ("creativeLanguage", "content_language"),
        ("targetLanguage", "targeting_language"),
    ):
        created = _call(
            api._declare,
            "POST",
            {"project_id": project_id},
            {
                "connector": CONNECTOR,
                "report_id": REPORT,
                "source_field": field,
                "canonical_dimension": dimension,
            },
        )
        assert created.status_code == 201

    listed = _json(_call(api._list, "GET", {"project_id": project_id}))
    assert listed["count"] == 3
    assert {row["canonical_dimension"] for row in listed["bindings"]} == {
        "audience_language",
        "content_language",
        "targeting_language",
    }


@pg_available
def test_a_non_member_does_not_learn_the_project_exists(
    fixture_org, as_identity
):
    """Existence-hiding on read: 404, never 403."""
    as_identity(STRANGER)
    denied = _call(api._list, "GET", {"project_id": fixture_org["project_id"]})
    assert denied.status_code == 404
    assert _json(denied)["code"] == "not_found"


@pg_available
def test_a_reader_may_read_but_may_not_declare(fixture_org, as_identity):
    """Read is membership, write is org-manage -- two different gates."""
    project_id = fixture_org["project_id"]
    as_identity(VIEWER)

    allowed = _call(api._list, "GET", {"project_id": project_id})
    assert allowed.status_code == 200

    refused = _call(
        api._declare,
        "POST",
        {"project_id": project_id},
        {
            "connector": CONNECTOR,
            "report_id": REPORT,
            "source_field": FIELD,
            "canonical_dimension": "content_language",
        },
    )
    assert refused.status_code == 403
    assert _json(refused)["code"] == "forbidden"


@pg_available
def test_an_unauthenticated_call_is_refused_before_anything_is_read(as_identity):
    as_identity(None)
    denied = _call(api._list, "GET", {"project_id": "proj_EXAMPLE"})
    assert denied.status_code == 401


@pg_available
def test_a_project_url_cannot_reach_a_binding_of_another_scope(
    fixture_org, as_identity
):
    """The id in the path is a claim, and it is re-read against the URL's project.

    Without this, an identity who may manage project A could retire a PLATFORM
    binding by naming its id -- the class d59ebc2a closed for grant changes.
    """
    from core.db import get_connection
    from core.language_dimensions import (
        SCOPE_PLATFORM,
        STATUS_PROPOSED,
        BindingEvidence,
        BindingProposal,
        list_bindings,
        persist_binding_proposals,
    )

    project_id = fixture_org["project_id"]
    platform_field = _uid("platformField")
    persist_binding_proposals(
        [
            BindingProposal(
                connector=CONNECTOR,
                report_id=REPORT,
                source_field=platform_field,
                canonical_dimension="content_language",
                status=STATUS_PROPOSED,
                confidence=0.9,
                evidence=BindingEvidence(
                    quote="the language of the creative", source="catalog_field_description"
                ),
                rationale="platform-scope fixture",
            )
        ],
        scope_level=SCOPE_PLATFORM,
        identity="system",
    )
    platform_id = [
        row["id"]
        for row in list_bindings(scope_level=SCOPE_PLATFORM)
        if row["source_field"] == platform_field
    ][0]

    try:
        as_identity(OWNER)
        denied = _call(
            api._retire,
            "DELETE",
            {"project_id": project_id, "binding_id": platform_id},
        )
        assert denied.status_code == 404
        # And it really was not touched.
        still = [
            row
            for row in list_bindings(scope_level=SCOPE_PLATFORM)
            if row["source_field"] == platform_field
        ][0]
        assert still["status"] == "proposed"
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.dimension_field_bindings WHERE id = %s",
                    (platform_id,),
                )
            conn.commit()


@pg_available
def test_a_declaration_that_names_no_dimension_is_refused(
    fixture_org, as_identity
):
    """There is no request shape that confirms without naming what is confirmed."""
    as_identity(OWNER)
    refused = _call(
        api._declare,
        "POST",
        {"project_id": fixture_org["project_id"]},
        {"connector": CONNECTOR, "report_id": REPORT, "source_field": FIELD},
    )
    assert refused.status_code == 400
    assert _json(refused)["code"] == "missing_fields"
    assert "canonical_dimension" in _json(refused)["message"]


@pg_available
def test_a_non_family_target_never_reaches_the_store(fixture_org, as_identity):
    from core.language_dimensions import SCOPE_PROJECT, list_bindings

    project_id = fixture_org["project_id"]
    as_identity(OWNER)
    refused = _call(
        api._declare,
        "POST",
        {"project_id": project_id},
        {
            "connector": CONNECTOR,
            "report_id": REPORT,
            "source_field": FIELD,
            "canonical_dimension": "country",
        },
    )
    assert refused.status_code == 400
    assert (
        list_bindings(
            scope_level=SCOPE_PROJECT,
            org_id=fixture_org["org_id"],
            project_id=project_id,
        )
        == []
    )


@pg_available
def test_a_column_no_shipped_catalog_describes_is_still_a_persons_to_decide(
    fixture_org, as_identity
):
    """THE TIKTOK CASE — the last of the four deferred findings of 2026-08-01.

    `target_languages` exists in exactly one place in the repository:
    `modules/tiktok-ads/catalog_sources/fusion-report.json`, as a bare NAME in a
    list — no description, no physical type. `propose_binding` judges on the
    provider's own words, so with no words it can propose nothing, and the review
    recorded the field as unable to enter the proposal/confirmation flow at all.

    IT NEVER NEEDED TO ENTER THE PROPOSAL HALF. A proposal is what the product
    offers when the evidence settles the question; the confirmation half exists
    precisely for the questions evidence does NOT settle, and there the authority
    is a person, not a catalog line. So this surface must not check the column
    against any catalog — it checks the column against the FAMILY, and the person
    against the org. Adding a catalog check here would look like rigour and would
    silently close the only door an undescribed column has.

    The console half is pinned in
    `ui/admin/src/__tests__/LanguageBindingsPanel.test.tsx`: the columns offered
    are the mapping's, never the catalog's.
    """
    from core.language_dimensions import (
        SCOPE_PROJECT,
        STATUS_CONFIRMED,
        list_bindings,
        resolve_field_bindings,
    )

    project_id = fixture_org["project_id"]
    as_identity(OWNER)
    accepted = _call(
        api._declare,
        "POST",
        {"project_id": project_id},
        {
            "connector": "tiktok-ads",
            "report_id": "basic_daily",
            "source_field": "target_languages",
            "canonical_dimension": "targeting_language",
            "rationale": "The delivery setting, not an observation.",
        },
    )
    assert accepted.status_code in (200, 201), _json(accepted)

    rows = list_bindings(
        scope_level=SCOPE_PROJECT,
        org_id=fixture_org["org_id"],
        project_id=project_id,
    )
    stored = [row for row in rows if row.get("source_field") == "target_languages"]
    assert len(stored) == 1, rows
    assert stored[0]["status"] == STATUS_CONFIRMED
    assert stored[0]["canonical_dimension"] == "targeting_language"

    # AND IT IS EXECUTABLE, which is what makes it a decision rather than a form.
    resolved = resolve_field_bindings(
        org_id=fixture_org["org_id"], project_id=project_id
    )
    assert resolved[("tiktok-ads", "basic_daily", "target_languages")] == (
        "targeting_language"
    )
