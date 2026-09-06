"""Story 52.4 AC5 -- the same answer contract, whatever the host.

The two hosts have always called the same `core.cards.get_card`, so their CONTENTS
agreed. What was never asserted is that they carry the same ANSWER: nothing
stopped one of them from projecting a field the other dropped, and "an answer that
changes shape with its host" is exactly what the epic forbids.

This file asserts it field by field, through the real seams: the MCP tool over
`FastMCPTransport`, and the REST route through `build_asgi_app()`.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from core import answer_contract as contract  # noqa: E402
from core.main import mcp  # noqa: E402
from fastmcp.client import Client, FastMCPTransport  # noqa: E402


def _rows():
    """Two days of one canonical metric -- enough for a real card to resolve."""
    rows = []
    for day in ("2026-07-05", "2026-07-06"):
        rows.append(
            {
                "date": day,
                "connector": "ga4",
                "metric": "sessions",
                "breakdown_dimension": None,
                "breakdown_value": None,
                "value": 100.0,
                "pull_id": "pull_1",
                "loaded_at": f"{day}T00:00:00",
            }
        )
    return rows


class _FakeConnection:
    """Enough of a connection for the AD-5 scope check to RUN and say yes."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _splice_card_routes():
    from core import admin_api  # noqa: PLC0415
    from core.cards_api import CARDS_ROUTES  # noqa: PLC0415

    existing = {getattr(r, "path", None) for r in admin_api.router.routes}
    for route in CARDS_ROUTES:
        if getattr(route, "path", None) not in existing:
            admin_api.router.routes.append(route)


def _same_world():
    """The patches BOTH hosts run under. Not a convenience -- a precondition.

    `core.db.get_connection` is patched deliberately: `cards_api` refuses a scope
    check it cannot RUN (an unverifiable decision is a refusal, 404). With no local
    Postgres the route would answer 404 before ever building a card -- which is
    also why three tests in `test_cards_integration_seams.py` are red at HEAD, and
    were red before Epic 52.

    It must apply to the MCP call TOO, and that is a finding rather than tidying.
    It used to wrap only the REST call, so the two hosts read the project's
    governed knowledge under different conditions: the REST side reached
    `answerable_topics.knowledge_citations` with a connection whose cursor raises
    (its pin read fails, and the AC5 refusal FAILS CLOSED to `refused=True`), while
    the MCP side never got a connection at all (`cards._resolve_knowledge_for_card`
    catches that earlier and answers `refused=False`). The test then blamed the
    hosts for a difference the fixture had created -- an instrument reporting its
    own contamination as a product defect.
    """
    return (
        patch("core.warehouse.query_daily_report", return_value=_rows()),
        patch(
            "core.cards_api._check_auth",
            new=AsyncMock(return_value=(True, "test@example.com")),
        ),
        patch("core.project_access.identity_can_read_project", return_value=True),
        patch("core.db.get_connection", _FakeConnection),
    )


def _rest_answer():
    from core.main import build_asgi_app  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    _splice_card_routes()
    patches = _same_world()
    for started in patches:
        started.start()
    try:
        with TestClient(build_asgi_app(), raise_server_exceptions=True) as c:
            resp = c.get(
                "/api/cards?project_id=default&metrics=sessions"
                "&date_from=2026-07-01&date_to=2026-07-10",
                headers={"Host": "localhost"},
            )
    finally:
        for started in patches:
            started.stop()
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.anyio
async def test_both_hosts_carry_the_same_answer_contract():
    rest = _rest_answer()

    patches = _same_world()
    for started in patches:
        started.start()
    try:
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_card",
                {
                    "project_id": "default",
                    "metrics": ["sessions"],
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-10",
                },
            )
    finally:
        for started in patches:
            started.stop()
    assert not result.is_error, f"get_card errored: {result}"
    # The MCP host carries it in `meta`, which `enforce_model_channel` does not
    # measure -- the same place the widget resource URI travels.
    mcp_answer = (getattr(result, "meta", None) or {}).get("answer")

    # The contract is a HOST BINDING, deliberately outside the envelope: the
    # `dedup` envelope has only tens of bytes of room under the 4096-byte
    # model-channel budget, so a contract inside it displaced `composition` into
    # the app channel. The margin is measured by `test_model_channel_margin.py` and
    # is deliberately not quoted here -- it was quoted as "117" in six places and
    # reproduced as 127 (review-epic-52 D-11). The envelope does NOT carry it...
    assert "answer" not in (rest["envelope"].get("data") or {})

    # ...and the two hosts carry the same one, key by key, built by one builder.
    assert mcp_answer is not None, "the MCP host carries no answer contract"
    contract.validate_answer(mcp_answer)
    contract.validate_answer(rest["answer"])

    for key in contract.ANSWER_KEYS:
        assert mcp_answer[key] == rest["answer"][key], (
            f"the answer changes shape with its host at {key!r}"
        )


def test_the_rest_host_validates_its_own_answer():
    rest = _rest_answer()
    contract.validate_answer(rest["answer"])
    # A real card with rows resolves a visual; if it ever stops, the state must
    # SAY so rather than ship an empty frame.
    assert rest["answer"]["visual"]["state"] in contract.VISUAL_STATES
    if rest["answer"]["visual"]["state"] != contract.VISUAL_RENDERED:
        assert rest["answer"]["visual"]["reason"]


# ---------------------------------------------------------------------------
# Story 52.4 AC3 -- each visual state has a REAL producer.
#
# review-epic-52 C-6 and D-4 make the same objection about two different states:
# a state that only a hand-built envelope can produce is a promise the product
# does not keep. So this walks real cards through `core.cards.get_card` -- the
# path both hosts call -- and requires each of the three states to come out of one.
#
# What each producer is, and why it is honest:
#
#   * `rendered`       -- `keywords` with rows. A chart was declared and drew.
#   * `not_applicable` -- `kpi`, whose composition is a KPI row and a comment. No
#                         chart is declared, so no chart is missing. This is the
#                         state that had NO producer before: defined as
#                         "comment-only composition", it was unreachable because
#                         all nine templates carry 1..5 non-comment blocks and a
#                         project-authored topic inherits its base composition.
#   * `unavailable`    -- `connectors` with the database unreachable. Its inventory
#                         table resolves to zero rows, so the card really is a
#                         frame with nothing in it. This is a PRODUCTION path, not
#                         a fixture: `_resolve_connectors_card` is written to
#                         degrade to an empty envelope rather than crash.
# ---------------------------------------------------------------------------


def _real_card_answer(template, **kwargs):
    from core import cards  # noqa: PLC0415

    _summary, envelope, uri = cards.get_card([], "default", template=template, **kwargs)
    return contract.build_answer(envelope, widget_uri=uri)


def _keyword_rows():
    rows = []
    for day in ("2026-07-05", "2026-07-06"):
        for metric, value in (("clicks", 50.0), ("impressions", 900.0)):
            rows.append(
                {
                    "date": day,
                    "connector": "google-search-console",
                    "metric": metric,
                    "breakdown_dimension": "page",
                    "breakdown_value": "/pricing",
                    "value": value,
                    "pull_id": "pull_1",
                    "loaded_at": f"{day}T00:00:00",
                }
            )
    return rows


def test_a_real_card_produces_rendered():
    with patch("core.warehouse.query_daily_report", return_value=_keyword_rows()):
        answer = _real_card_answer(
            "keywords", date_from="2026-07-01", date_to="2026-07-10"
        )

    assert answer["visual"]["state"] == contract.VISUAL_RENDERED
    contract.validate_answer(answer)


def test_a_real_card_produces_not_applicable_without_a_hand_built_envelope():
    """C-6: before the chart types were named, NO card could reach this state."""
    with patch("core.warehouse.query_daily_report", return_value=_rows()):
        answer = _real_card_answer("kpi", date_from="2026-07-01", date_to="2026-07-10")

    assert answer["visual"]["state"] == contract.VISUAL_NOT_APPLICABLE
    assert answer["visual"]["reason"], "an absent visual must say why"
    contract.validate_answer(answer)


def test_a_real_card_produces_unavailable_when_nothing_resolved():
    """D-6: none of the measured cards ever produced `unavailable`. This one does.

    No warehouse patch and no database: `connectors` reads app-level Postgres and
    is written to degrade to an empty inventory rather than crash, so the card
    comes back as a table with zero rows -- a declared visual that drew nothing.
    """
    answer = _real_card_answer("connectors")

    assert answer["visual"]["state"] == contract.VISUAL_UNAVAILABLE
    assert answer["visual"]["reason"]
    contract.validate_answer(answer)


def test_every_visual_state_the_contract_declares_has_a_real_producer():
    """The rule, not the three instances: no state may be test-only."""
    produced = set()

    with patch("core.warehouse.query_daily_report", return_value=_keyword_rows()):
        produced.add(
            _real_card_answer("keywords", date_from="2026-07-01", date_to="2026-07-10")[
                "visual"
            ]["state"]
        )
    with patch("core.warehouse.query_daily_report", return_value=_rows()):
        produced.add(
            _real_card_answer("kpi", date_from="2026-07-01", date_to="2026-07-10")[
                "visual"
            ]["state"]
        )
    produced.add(_real_card_answer("connectors")["visual"]["state"])

    missing = set(contract.VISUAL_STATES) - produced
    assert not missing, (
        f"{sorted(missing)} is declared by the contract and produced by no real "
        "card -- a state only a fabricated envelope can reach is a promise the "
        "product does not keep. Either give it a producer or remove it."
    )


def test_a_context_card_carries_the_knowledge_half_through_the_contract():
    """52.3 AC3 seen from the contract: the field is present, not absent (D-2)."""
    answer = _real_card_answer("connectors")

    assert "knowledge_status" in answer["text"]
    assert answer["text"]["citation_count"] == 0
    assert answer["text"]["knowledge_status"] is not None, (
        "a context card used to reach the contract with no knowledge field at all, "
        "and `null` there is indistinguishable from 'nothing is declared'"
    )
