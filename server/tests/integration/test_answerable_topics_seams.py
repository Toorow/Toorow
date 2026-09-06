"""Story 52.1 -- the Answerable Topic routes, exercised through the REAL ASGI stack.

AI-56: a new REST surface gets its `build_asgi_app()`-level seam test IN ITS OWN
STORY, not at epic review. The seam gap this rule exists for recurred in Epics 1,
8 and 9, each time because the routes were only ever called as functions.

What is proved here is the WIRING, not the storage: that the four routes exist on
the mounted app, that they are not swallowed by another route, that an
unauthenticated caller is refused, and that a governed write without an
`Idempotency-Key` is refused before anything is stored. Postgres is mocked -- the
storage behaviour lives in `tests/core/test_answerable_topics.py` and, for the
triggers, in the pg-gated suite.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from core import answerable_topics as topics  # noqa: E402
from core.main import mcp  # noqa: E402
from fastmcp.client import Client, FastMCPTransport  # noqa: E402

_BASE = "/api/projects/proj_EXAMPLE/answerable-topics"


def _app():
    from core.main import build_asgi_app  # noqa: PLC0415

    return build_asgi_app()


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", _BASE),
        ("post", _BASE),
        ("post", f"{_BASE}/weekly-pacing/versions"),
        ("post", f"{_BASE}/weekly-pacing/retire"),
        # Story 52.2
        ("get", f"{_BASE}/weekly-pacing/queries"),
        ("post", f"{_BASE}/weekly-pacing/queries"),
        ("delete", f"{_BASE}/weekly-pacing/queries/atq_1"),
        # Story 52.3
        ("get", f"{_BASE}/weekly-pacing/knowledge"),
        ("post", f"{_BASE}/weekly-pacing/knowledge"),
        ("delete", f"{_BASE}/weekly-pacing/knowledge/atk_1"),
        # Story 75-5: the third binding family, and the picker's source.
        ("get", f"{_BASE}/semantic-views"),
        ("get", f"{_BASE}/weekly-pacing/views"),
        ("post", f"{_BASE}/weekly-pacing/views"),
        ("delete", f"{_BASE}/weekly-pacing/views/atvb_1"),
    ],
)
def test_every_route_is_reachable_on_the_real_app(method, path):
    """A route with no mount is a capability nobody can reach (project-context, DoD 6).

    Reachability is proved by the REFUSAL, not by introspection: `build_asgi_app()`
    wraps the router in middleware that exposes no route table, so walking it
    returns an empty list and would make this test pass while proving nothing.
    An unauthenticated call to a MOUNTED route answers 401 (the handler ran and
    refused); an unmounted path answers 404 (nothing matched). The two are
    distinguishable, and that difference is the assertion.
    """
    from starlette.testclient import TestClient  # noqa: PLC0415

    with patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))
    ), TestClient(_app(), raise_server_exceptions=True) as c:
        resp = getattr(c, method)(path, headers={"Host": "localhost"})

    assert resp.status_code == 401, (
        f"{method.upper()} {path} answered {resp.status_code}; 404 means the route "
        "is not mounted in build_asgi_app()"
    )


def test_unauthenticated_read_is_refused():
    from starlette.testclient import TestClient  # noqa: PLC0415

    with patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))
    ), TestClient(_app(), raise_server_exceptions=True) as c:
        resp = c.get(_BASE, headers={"Host": "localhost"})
    assert resp.status_code == 401


def _read_catalog(resolved):
    from starlette.testclient import TestClient  # noqa: PLC0415

    with patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "test@example.com"))
    ), patch("core.admin_api._require_datastream_role", return_value=None), patch(
        "core.db.get_connection"
    ) as conn, patch(
        # The API module imported the name, so patching it on `core.answerable_topics`
        # would intercept nothing -- which is why the pre-existing version of this
        # test passed while proving nothing about the resolver.
        "core.answerable_topics_api.resolve_catalog_with_reason",
        return_value=resolved,
    ):
        conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (  # noqa: E501
            "org_EXAMPLE",
        )
        with TestClient(_app(), raise_server_exceptions=True) as c:
            return c.get(_BASE, headers={"Host": "localhost"})


def test_read_returns_the_resolved_catalog_through_the_stack():
    resp = _read_catalog((topics.default_catalog(), None))

    assert resp.status_code == 200
    body = resp.json()
    assert body["topics_total"] == len(topics.default_catalog())
    assert [t["id"] for t in body["topics"]] == [
        t["id"] for t in topics.default_catalog()
    ]
    assert body["catalog_status"] == "resolved"
    assert body["catalog_reason"] is None


def test_a_degraded_catalog_arrives_at_the_console_saying_it_is_degraded():
    """The console rendered this list under the Project's id with nothing to say it
    was the platform fallback -- so a question the Project retired came back as one
    of its own, and a rewording it authored disappeared, both without a word."""
    resp = _read_catalog((topics.default_catalog(), topics.CATALOG_UNAVAILABLE_REASON))

    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog_status"] == "defaults_only"
    assert body["catalog_reason"] == topics.CATALOG_UNAVAILABLE_REASON
    # The value is still served: a discovery surface that answers nothing is worse.
    assert body["topics_total"] == len(topics.default_catalog())


def test_the_mcp_catalog_tool_marks_a_denied_project_as_defaults_only():
    """`list_card_templates` served the nine defaults for "access refused", for
    "store unreadable" and for "nothing configured" through one door, with one
    payload and no marker (D-1). A model could not tell the three apart."""
    from core import main as _main  # noqa: PLC0415

    # (a) the scope check ran and REFUSED.
    with patch("core.db.get_connection"), patch(
        "core.project_access.identity_can_read_project", return_value=False
    ), patch.object(_main, "_resolve_project", return_value="proj_EXAMPLE"):
        templates, reason = _main._project_topic_catalog("proj_EXAMPLE")
    assert templates == topics.default_catalog()
    assert reason == topics.CATALOG_ACCESS_DENIED_REASON

    # (b) no project named at all -- the defaults ARE the answer, and say so.
    assert _main._project_topic_catalog("")[1] == topics.CATALOG_NO_PROJECT_REASON

    # (c) the store could not be read: a third fact, a third code, same value.
    with patch("core.db.get_connection", side_effect=RuntimeError("no database")), patch.object(
        _main, "_resolve_project", return_value="proj_EXAMPLE"
    ):
        assert _main._project_topic_catalog("proj_EXAMPLE")[1] == (
            topics.CATALOG_UNAVAILABLE_REASON
        )

    assert len({
        topics.CATALOG_ACCESS_DENIED_REASON,
        topics.CATALOG_NO_PROJECT_REASON,
        topics.CATALOG_UNAVAILABLE_REASON,
    }) == 3

    # And the tool body carries the marker, in BOTH channels.
    with patch("core.db.get_connection"), patch(
        "core.project_access.identity_can_read_project", return_value=False
    ), patch.object(_main, "_resolve_project", return_value="proj_EXAMPLE"):
        result = _main.list_card_templates("proj_EXAMPLE")
    data = (result.structured_content or {}).get("data", {})
    assert data["catalog_status"] == "defaults_only"
    assert data["catalog_reason"] == topics.CATALOG_ACCESS_DENIED_REASON
    # The model channel carries the caveat too: the summary IS the claim being made.
    assert "not this project's catalog" in result.content[0].text


def test_every_connection_this_module_opens_is_armed_with_the_rls_floor():
    """S-5: migrations 170/172/173 each install a policy of the form
    `current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on' OR ...`.
    That predicate is unconditionally TRUE until a session sets the flag, and no
    route of this epic set it -- the floor the three migration headers describe was
    decoration. Structural, like the Analyze suite's own guard: a bare
    `get_connection()` in a handler is the regression, and it is invisible to any
    test that mocks the connection.
    """
    import ast  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    from core import answerable_topics_api as api  # noqa: PLC0415

    source = pathlib.Path(api.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # `_authorize` resolves WHO may act and must stay unarmed: arming it would change
    # which rows the authorization check itself can see. `topic_connection` is the
    # seam and is where the one legitimate bare call lives.
    allowed = {"_authorize", "topic_connection"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in allowed:
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "get_connection"
            ):
                offenders.append(node.name)
    assert offenders == [], (
        "these handlers open an unarmed connection instead of `topic_connection`, "
        f"so the Epic-36 RLS floor of migrations 170/172/173 is never posted: {offenders}"
    )


def test_the_capability_tool_reads_the_projects_catalog_and_not_the_platform_set():
    """C-3: `agent_card_catalog(catalog=...)` existed and NO caller passed it -- the
    one surface an agent asks "what can this project render right now" answered with
    the nine platform questions, while `preview_daily_insight` two hundred lines
    below refused the very template it had just advertised. Proved on the wire the
    catalog travels, not on a signature."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from core import main as _main  # noqa: PLC0415

    project_catalog = [e for e in topics.default_catalog() if e["id"] != "kpi"]
    seen = {}

    def _record(**kwargs):
        seen.update(kwargs)
        return {"contractVersion": "1", "catalog": [], "catalogStatus": "resolved"}

    with patch.object(
        _main, "_project_topic_catalog", return_value=(project_catalog, None)
    ), patch.object(
        _main, "_daily_insight_scope", return_value=("id@example.com", "proj_EXAMPLE", True)
    ), patch.object(
        _main,
        "_resolve_daily_insight_inputs",
        return_value=({"clicks"}, {"page"}, "2026-07-30", [], []),
    ), patch(
        "core.daily_insights_tools.capabilities", new=MagicMock(side_effect=_record)
    ):
        _main.get_card_capabilities("proj_EXAMPLE")

    assert seen.get("catalog") == project_catalog, (
        "get_card_capabilities holds the project id and did not thread its catalog"
    )
    assert "kpi" not in {e["id"] for e in seen["catalog"]}


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", _BASE),
        ("post", f"{_BASE}/weekly-pacing/versions"),
        ("post", f"{_BASE}/weekly-pacing/retire"),
        # Story 52.2: binding and unbinding are governed writes too.
        ("post", f"{_BASE}/weekly-pacing/queries"),
        ("delete", f"{_BASE}/weekly-pacing/queries/atq_1"),
        ("post", f"{_BASE}/weekly-pacing/knowledge"),
        ("delete", f"{_BASE}/weekly-pacing/knowledge/atk_1"),
        # Story 75-5: declaring a Semantic View and withdrawing one are governed
        # writes too -- the audit row and the change are one act.
        ("post", f"{_BASE}/weekly-pacing/views"),
        ("delete", f"{_BASE}/weekly-pacing/views/atvb_1"),
    ],
)
def test_a_governed_write_without_an_idempotency_key_is_refused(method, path):
    """Refused BEFORE the mutation runs -- the audit row and the change are one act."""
    from starlette.testclient import TestClient  # noqa: PLC0415

    with patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "test@example.com"))
    ), patch("core.admin_api._require_datastream_role", return_value=None), patch(
        "core.db.get_connection"
    ) as conn:
        conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (  # noqa: E501
            "org_EXAMPLE",
        )
        with TestClient(_app(), raise_server_exceptions=True) as c:
            resp = (
                c.delete(path, headers={"Host": "localhost"})
                if method == "delete"
                else getattr(c, method)(path, json={}, headers={"Host": "localhost"})
            )

    assert resp.status_code == 400
    assert resp.json()["code"] == "missing_idempotency_key"


def test_a_write_naming_an_unknown_base_card_is_refused_with_its_reason():
    from starlette.testclient import TestClient  # noqa: PLC0415

    with patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "test@example.com"))
    ), patch("core.admin_api._require_datastream_role", return_value=None), patch(
        "core.db.get_connection"
    ) as conn:
        conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (  # noqa: E501
            "org_EXAMPLE",
        )
        with TestClient(_app(), raise_server_exceptions=True) as c:
            resp = c.post(
                _BASE,
                json={
                    "topic_key": "weekly-pacing",
                    "title": "Weekly pacing",
                    "answers_question": "Are we pacing to plan?",
                    "base_template_id": "no-such-card",
                },
                headers={"Host": "localhost", "Idempotency-Key": "idem-1"},
            )

    assert resp.status_code == 422
    assert resp.json()["code"] == "unknown_base_template"


@pytest.mark.anyio
async def test_list_card_templates_tool_accepts_a_project_and_still_answers():
    """The MCP catalog tool is project-aware and never answers `nothing`."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_card_templates", {"project_id": "proj_EXAMPLE"})

    assert not result.is_error, f"list_card_templates errored: {result}"
    data = (result.structured_content or {}).get("data", {})
    assert data["templates_total"] == len(topics.default_catalog())


def test_binding_an_inexact_pin_is_refused_with_its_reason():
    """`latest` is unstorable -- said by the route, the module and migration 172."""
    from starlette.testclient import TestClient  # noqa: PLC0415

    with patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "test@example.com"))
    ), patch("core.admin_api._require_datastream_role", return_value=None), patch(
        "core.db.get_connection"
    ) as conn:
        conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (  # noqa: E501
            "org_EXAMPLE",
        )
        with TestClient(_app(), raise_server_exceptions=True) as c:
            resp = c.post(
                f"{_BASE}/weekly-pacing/queries",
                json={"query_spec_version_id": "latest", "role": "headline"},
                headers={"Host": "localhost", "Idempotency-Key": "idem-1"},
            )

    assert resp.status_code == 422
    assert resp.json()["code"] == "pin_is_not_exact"


def test_declaring_a_skill_as_knowledge_is_refused_because_a_skill_has_no_store():
    """The spike measured it: `skills-registry` renders Procedures, `app.skills` does
    not exist. A pin to an object with no store would be a column never filled."""
    from starlette.testclient import TestClient  # noqa: PLC0415

    with patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "test@example.com"))
    ), patch("core.admin_api._require_datastream_role", return_value=None), patch(
        "core.db.get_connection"
    ) as conn:
        conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (  # noqa: E501
            "org_EXAMPLE",
        )
        with TestClient(_app(), raise_server_exceptions=True) as c:
            resp = c.post(
                f"{_BASE}/weekly-pacing/knowledge",
                json={"knowledge_kind": "skill", "knowledge_id": "sk_1",
                      "knowledge_version": 1},
                headers={"Host": "localhost", "Idempotency-Key": "idem-1"},
            )

    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_knowledge_kind"
