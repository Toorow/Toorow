"""Story 75-3 -- `get_exemplars`, THE TOOL, driven through a FastMCP client on a real Postgres.

WHY THE TOOL AND NOT THE FUNCTION. AI-345 measured what a function test proves
about an MCP door: nothing about the door. These tests call `get_exemplars` by
name through `Client(FastMCPTransport(...))`, so the argument shapes, the
`ToolError` a refusal produces and the `structured_content` a host receives are
the ones a host receives.

WHY A REAL DATABASE. Every property below is a row or a join: a head whose
`lifecycle` moved, a version pinned by an immutable Golden Question version, a
Query Spec version whose `spec.measures` carries the metric the caller named, a
topic binding, and a path comparison that judged one walk `pass` against that
exact version. A mocked cursor agrees with any of those cheerfully.

Rolled back by the `live_postgres` fixture: nothing this file writes survives it.
"""

from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from core import exemplars, exemplars_mcp
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from ulid import ULID

from tests.core.test_evaluation_persistence_pg import Chain, _uid

pytestmark = pytest.mark.anyio

IDENTITY = "owner@example.com"
HASH = "a" * 64
METRIC_ID = "sc_EXAMPLE_SPEND"
METRIC_VERSION_ID = "scv_EXAMPLE_SPEND_1"
OTHER_METRIC_ID = "sc_EXAMPLE_CLICKS"
TOPIC_KEY = "paid-media-spend"
FOREIGN_PROJECT = "proj_EXAMPLE"


# ---------------------------------------------------------------------------
# Seeding: everything upstream of one served exemplar.
# ---------------------------------------------------------------------------


def _first_domain(conn, org_id: str) -> str:
    """A Business Domain of this organization, numbered in the pin registry."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id FROM app.mdm_business_domains d
              JOIN app.business_domain_version_registry r
                ON r.domain_id = d.id AND r.version_number = 1
             WHERE d.org_id = %s ORDER BY d.id LIMIT 1
            """,
            (org_id,),
        )
        row = cur.fetchone()
    assert row is not None, "an organization is seeded with its Business Domains"
    return str(row[0])


def _query_spec_version(chain, *, metric_id: str, metric_version_id: str) -> str:
    spec_id = _uid("qs")
    version_id = _uid("qsv")
    spec = {
        "measures": [{"id": metric_id, "version_id": metric_version_id}],
        "dimensions": [{"id": "sc_EXAMPLE_DAY", "version_id": "scv_EXAMPLE_DAY_1"}],
        "filters": [],
        "sort": [],
    }
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, created_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (spec_id, chain.org_id, chain.project_id, chain.view_id, IDENTITY),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                version_id,
                spec_id,
                chain.org_id,
                chain.project_id,
                chain.view_id,
                chain.view_version_id,
                json.dumps(spec),
                HASH,
                IDENTITY,
            ),
        )
    return version_id


def _definition(chain, *, domain_id: str, query_spec_version_id: str, question: str,
                expected_result: list[dict[str, Any]] | None = None) -> dict:
    return {
        "business_domain_id": domain_id,
        "business_domain_version_number": 1,
        "semantic_view_id": chain.view_id,
        "semantic_view_version_id": chain.view_version_id,
        "semantic_view_version_role": "baseline",
        "question": question,
        "time_boundary": {"as_of": "2026-07-31", "grain": "month"},
        "expected_result": expected_result or [
            {"assertion_type": "value", "member_id": METRIC_ID, "tolerance": None},
        ],
        "required_provenance": [
            {"link_kind": "source", "required": True},
            {"link_kind": "semantic_view", "required": True},
        ],
        "expected_ai_path": {
            "grammar_version": 1,
            "required_nodes": [
                {"key": "context-hub/knowledge-note/kn_EXAMPLE", "step_kind": "knowledge_read"},
                {"key": "governance/semantic-view/sv_EXAMPLE", "step_kind": "semantic_query"},
            ],
            "forbidden_nodes": [{"tool_name": "raw_sql_passthrough"}],
            "order_constraints": [
                {
                    "before": "context-hub/knowledge-note/kn_EXAMPLE",
                    "after": "governance/semantic-view/sv_EXAMPLE",
                }
            ],
        },
        "reference_paths": [
            {"query_spec_version_id": query_spec_version_id, "role": "canonical"}
        ],
        "result_type": "breakdown",
        "capability_tags": ["media_spend_reporting"],
        "severity": "critical",
    }


def _golden_question(
    chain,
    *,
    domain_id: str,
    query_spec_version_id: str,
    question: str,
    title: str,
    lifecycle: str = "active",
    expected_result: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from core.golden_questions import (
        create_golden_question,
        set_lifecycle,
        validate_golden_question_version,
    )

    validated = validate_golden_question_version(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        payload=_definition(
            chain,
            domain_id=domain_id,
            query_spec_version_id=query_spec_version_id,
            question=question,
            expected_result=expected_result,
        ),
    )
    head = create_golden_question(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        title=title,
        owner=IDENTITY,
        validated=validated,
        actor=IDENTITY,
    )
    if lifecycle != "draft":
        set_lifecycle(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            golden_question_id=head["golden_question_id"],
            lifecycle="active",
            actor=IDENTITY,
        )
    if lifecycle not in {"draft", "active"}:
        set_lifecycle(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            golden_question_id=head["golden_question_id"],
            lifecycle=lifecycle,
            actor=IDENTITY,
        )
    return head


def _bind_topic(chain, *, query_spec_version_id: str, topic_key: str = TOPIC_KEY) -> str:
    topic_id = _uid("atp")
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.answerable_topics
                (id, org_id, project_id, topic_key, base_template_id, created_by)
            VALUES (%s, %s, %s, %s, 'tpl_example', %s)
            """,
            (topic_id, chain.org_id, chain.project_id, topic_key, IDENTITY),
        )
        cur.execute("SELECT query_spec_id FROM app.query_spec_versions WHERE id = %s",
                    (query_spec_version_id,))
        spec_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO app.answerable_topic_query_bindings
                (id, answerable_topic_id, org_id, project_id, query_spec_id,
                 query_spec_version_id, role, position, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'canonical', 1, %s)
            """,
            (
                _uid("atqb"),
                topic_id,
                chain.org_id,
                chain.project_id,
                spec_id,
                query_spec_version_id,
                IDENTITY,
            ),
        )
    return topic_id


def _walk(chain, *, outcome: str = "succeeded", finalize: bool = True, steps: int = 2) -> str:
    """An observed walk with two ordered steps, finalized unless asked otherwise.

    `steps` widens it: a walk of twelve is what a real interaction records, and
    what makes a head heavier than the model channel.

    `finalize=False` leaves it `recording` -- a walk that can still grow steps,
    which is the state `exemplars._attach_approved_paths` filters out and which
    no application path would ever pin as evidence.
    """
    path_id = _uid("aip")
    with chain.conn.cursor() as cur:
        # `reject_step_on_finalized_path` is the whole point of the lifecycle: the
        # walk is recorded open, then frozen. Seeding it finalized-first would be
        # seeding a shape the product cannot produce.
        cur.execute(
            """
            INSERT INTO app.ai_paths
                (id, org_id, project_id, lifecycle, actor, policy_snapshot_hash)
            VALUES (%s, %s, %s, 'recording', %s, %s)
            """,
            (path_id, chain.org_id, chain.project_id, IDENTITY, HASH),
        )
        walked = [
            ("knowledge_read", "search_context"),
            ("semantic_query", "execute_analyze_query_spec"),
        ]
        while len(walked) < steps:
            walked.append(("semantic_query", "execute_analyze_query_spec"))
        for ordinal, (kind, tool) in enumerate(walked[:steps]):
            cur.execute(
                """
                INSERT INTO app.ai_path_steps
                    (id, path_id, org_id, project_id, ordinal, step_kind,
                     owner_workspace, owner_object_type, owner_object_id, owner_version_id,
                     tool_name, outcome)
                VALUES (%s, %s, %s, %s, %s, %s, 'governance', 'semantic-view', %s, %s, %s,
                        'succeeded')
                """,
                (
                    f"aps_{ULID()}",
                    path_id,
                    chain.org_id,
                    chain.project_id,
                    ordinal,
                    kind,
                    chain.view_id,
                    chain.view_version_id,
                    tool,
                ),
            )
        if finalize:
            cur.execute(
                """
                UPDATE app.ai_paths
                   SET lifecycle = 'finalized', outcome = %s, ended_at = now(),
                       content_hash = %s
                 WHERE id = %s
                """,
                (outcome, HASH, path_id),
            )
        # `ck_ai_paths_recording_is_open` forbids an outcome, an end and a hash
        # on a recording path: an open walk is left exactly open.
    return path_id


def _judge(
    chain,
    *,
    golden_question_version_id: str,
    domain_id: str,
    path_id: str | None,
    verdict: str = "pass",
) -> str:
    """One evaluation case and its path comparison -- the only 'approved' there is."""
    run_id = chain.open_run()
    case_id = f"ecase_{ULID()}"
    comparison_id = f"epc_{ULID()}"
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evaluation_run_cases
                (id, run_id, org_id, project_id, golden_question_version_id, ai_path_id,
                 business_domain_id, business_domain_version_number, capability_key, result_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'media_spend_reporting', 'breakdown')
            """,
            (
                case_id,
                run_id,
                chain.org_id,
                chain.project_id,
                golden_question_version_id,
                path_id,
                domain_id,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.evaluation_path_comparisons
                (id, case_id, org_id, project_id, observed_ai_path_id, expected_pattern_hash,
                 evidence_state, path_verdict, matched_alternative_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'canonical')
            """,
            (
                comparison_id,
                case_id,
                chain.org_id,
                chain.project_id,
                path_id,
                HASH,
                "observed" if path_id else "missing",
                verdict,
            ),
        )
    return comparison_id


# ---------------------------------------------------------------------------
# The wiring: the REAL tool on a bare FastMCP, on the fixture's connection.
# ---------------------------------------------------------------------------


@pytest.fixture()
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


@pytest.fixture()
def seeded(chain) -> dict[str, Any]:
    domain_id = _first_domain(chain.conn, chain.org_id)
    spec_version_id = _query_spec_version(
        chain, metric_id=METRIC_ID, metric_version_id=METRIC_VERSION_ID
    )
    head = _golden_question(
        chain,
        domain_id=domain_id,
        query_spec_version_id=spec_version_id,
        question="What was paid media spend last completed month, by market?",
        title="Paid media spend by market",
    )
    return {
        "domain_id": domain_id,
        "query_spec_version_id": spec_version_id,
        "golden_question_id": head["golden_question_id"],
        "version_id": head["version_id"],
    }


@pytest.fixture()
def wired(chain, monkeypatch):
    """The real tool bound on a bare FastMCP, with the caller and the store ours."""
    import core.db
    import core.main

    opened: list[str] = []
    refused: list[str] = []

    monkeypatch.setattr(exemplars_mcp, "_identity", lambda: IDENTITY)
    monkeypatch.setattr(core.main, "_resolve_project", lambda pid, identity=None: pid)

    def _guard(project_id, identity, **_kw):
        # The REAL refusal, on the real seam's envelope: a Project this caller
        # cannot reach answers exactly like a Project that does not exist.
        refused.append(project_id)
        if project_id != chain.project_id:
            from core.project_resolver import _raise_not_found

            _raise_not_found()

    monkeypatch.setattr(exemplars_mcp, "refuse_unless_project_scope", _guard)

    @contextmanager
    def _request_connection(identity):
        assert identity == IDENTITY
        opened.append(identity)
        yield chain.conn

    monkeypatch.setattr(core.db, "request_connection", _request_connection)
    target = FastMCP("story-75-3")
    exemplars_mcp.register(target)
    return {"mcp": target, "opened": opened, "refused": refused}


async def _call(wired, **arguments):
    async with Client(FastMCPTransport(wired["mcp"])) as client:
        return await client.call_tool("get_exemplars", arguments)


def _data(result) -> dict[str, Any]:
    return result.structured_content["data"]


# ---------------------------------------------------------------------------
# 1. An active question is served, and every pin it carries is the stored one.
# ---------------------------------------------------------------------------


async def test_an_active_question_is_served_with_its_exact_pins(chain, seeded, wired):
    """ONE head carrying both its pins and a judged walk: the PINS survive.

    A reference path plus one judged walk of two steps measures ~2900 bytes
    against a 2048-byte list budget, so this head cannot be served whole at any
    budget -- the model channel is 4096 bytes for everything. The ladder
    therefore has to choose, and it was arbitrated on 2026-09-06: the pins are
    the exemplar's IDENTITY ("the pins, and they are exact"), the judged walks
    are illustrations, so the walks go first and every pin below is still here.
    """
    path_id = _walk(chain)
    _judge(
        chain,
        golden_question_version_id=seeded["version_id"],
        domain_id=seeded["domain_id"],
        path_id=path_id,
    )

    result = await _call(wired, subject_type="metric", subject_id=METRIC_ID,
                         project_id=chain.project_id)

    # The answer must pass the SAME model-channel guard the profiled surface
    # applies (`mcp_profiles` -> `model_channel.enforce_model_channel`). A tool
    # that is green here and refused on the wire is the AI-345 defect.
    #
    # WHAT THIS USED TO MEASURE, AND IT WAS THE WRONG THING (G16, revision
    # `mcp-server-00274`). It partitioned FIRST and enforced on the surviving
    # half -- so it passed exactly when `partition_envelope` had moved the
    # exemplars to the app channel and left the model
    # `{"withheld": "moved_to_app_channel", "bytes": 4391}`. Green here, and the
    # model never saw a single exemplar. The split must therefore move NOTHING.
    from core import model_channel

    model_visible, app_payload = model_channel.partition_envelope(
        result.structured_content, tool_name="get_exemplars"
    )
    assert app_payload == {}, (
        "a full-budget answer is served WHOLE to the model: anything in the app "
        f"payload is an exemplar the model never sees ({sorted(app_payload)})"
    )
    assert model_visible == result.structured_content
    model_channel.enforce_model_channel(
        "get_exemplars", result.content, result.structured_content
    )

    data = _data(result)
    assert data["exemplar_store_state"] == "available"
    assert data["served"] == 1 and data["truncated"] is False
    exemplar = data["exemplars"][0]
    assert exemplar["lifecycle"] == "active"
    assert exemplar["question"].startswith("What was paid media spend")
    assert exemplar["expected_ai_path"]["required_nodes"]
    assert exemplar["expected_answer_shape"]["result_type"] == "breakdown"
    assert exemplar["expected_answer_shape"]["severity"] == "critical"

    pins = exemplar["pins"]
    assert pins["golden_question_version_id"] == seeded["version_id"]
    assert pins["version_number"] == 1
    assert pins["semantic_view_id"] == chain.view_id
    assert pins["semantic_view_version_id"] == chain.view_version_id
    assert pins["semantic_view_version_role"] == "baseline"
    assert pins["business_domain_id"] == seeded["domain_id"]
    assert pins["business_domain_version_number"] == 1
    assert [p["query_spec_version_id"] for p in pins["reference_paths"]] == [
        seeded["query_spec_version_id"]
    ]
    assert pins["reference_paths"][0]["measures"] == [
        {"metric_id": METRIC_ID, "version_id": METRIC_VERSION_ID}
    ]
    # The metric the CALLER named, at the version the question was written
    # against -- never the version today's Semantic View happens to carry.
    assert pins["subject_metric_version_id"] == METRIC_VERSION_ID
    # EVERY pin, whole: the ladder took the walk instead.
    assert pins["reference_paths_omitted"] == 0
    assert pins["reference_paths"][0]["measures_omitted"] == 0

    # And the walk it took is COUNTED, never silently absent. A trimmed head
    # says so twice -- on itself and on the payload -- and the plain-text
    # channel says it in words.
    assert exemplar["trimmed_for_budget"] is True
    assert data["trimmed_for_budget"] is True
    # The walk the join found is either served with its exact identity, or cut
    # and COUNTED. Never absent without a number: that is the whole difference
    # between a trimmed exemplar and one that quietly reads complete.
    served_walks = exemplar["approved_paths"]
    assert len(served_walks) + exemplar["approved_paths_omitted"] == 1
    if served_walks:
        assert served_walks[0]["observed_ai_path_id"] == path_id
        assert served_walks[0]["path_verdict"] == "pass"
        assert served_walks[0]["expected_pattern_hash"] == HASH
    else:
        assert exemplar["approved_paths_omitted"] == 1
    assert "trimmed" in result.content[0].text

    # Story 75-4 rides this door like the three beside it -- and it is the 1024
    # bytes `ENVELOPE_RESERVE_BYTES` already holds back for it, which is why the
    # split above still moves nothing.
    block = result.structured_content["meta"]["ai_settings"]
    assert set(block) == {"values", "sources"}
    assert set(block["values"]) == set(block["sources"]) == {
        "rules_always", "rules_never", "query_scope", "fiscal_calendar",
        "narrative_language", "narrative_register",
    }
    assert block["sources"]["narrative_language"] in {"PLATFORM", "ORG", "PROJECT"}


async def test_the_view_and_topic_selectors_reach_the_same_question(chain, seeded, wired):
    _bind_topic(chain, query_spec_version_id=seeded["query_spec_version_id"])

    by_view = _data(await _call(wired, subject_type="view", subject_id=chain.view_id,
                                project_id=chain.project_id))
    by_version = _data(await _call(wired, subject_type="view",
                                   subject_id=chain.view_version_id,
                                   project_id=chain.project_id))
    by_topic = _data(await _call(wired, subject_type="topic", subject_id=TOPIC_KEY,
                                 project_id=chain.project_id))

    for data in (by_view, by_version, by_topic):
        assert [e["golden_question_version_id"] for e in data["exemplars"]] == [
            seeded["version_id"]
        ]

    # A subject nobody bound reaches nothing, and says so as an EMPTY store, not
    # as an unavailable one.
    absent = _data(await _call(wired, subject_type="metric", subject_id=OTHER_METRIC_ID,
                               project_id=chain.project_id))
    assert absent["exemplars"] == [] and absent["exemplar_store_state"] == "available"


# ---------------------------------------------------------------------------
# 2. Stewardship governs what is served -- in both directions.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retired", ["deprecated", "archived"])
async def test_a_retired_question_is_no_longer_served(chain, seeded, wired, retired):
    from core.golden_questions import set_lifecycle

    served = _data(await _call(wired, subject_type="metric", subject_id=METRIC_ID,
                               project_id=chain.project_id))
    assert served["served"] == 1

    set_lifecycle(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        golden_question_id=seeded["golden_question_id"],
        lifecycle=retired,
        actor=IDENTITY,
    )

    after = _data(await _call(wired, subject_type="metric", subject_id=METRIC_ID,
                              project_id=chain.project_id))
    assert after["exemplars"] == []
    assert after["served"] == 0 and after["truncated"] is False
    assert after["exemplar_store_state"] == "available"


async def test_a_draft_question_is_never_served(chain, wired):
    domain_id = _first_domain(chain.conn, chain.org_id)
    spec_version_id = _query_spec_version(
        chain, metric_id=METRIC_ID, metric_version_id=METRIC_VERSION_ID
    )
    _golden_question(
        chain,
        domain_id=domain_id,
        query_spec_version_id=spec_version_id,
        question="A question nobody has stewarded into use yet?",
        title="Draft question",
        lifecycle="draft",
    )

    data = _data(await _call(wired, subject_type="metric", subject_id=METRIC_ID,
                             project_id=chain.project_id))
    assert data["exemplars"] == []


async def _walks_approved(chain, wired) -> int:
    """How many walks the approval JOIN found, served plus cut for budget.

    NOT the served list: since the ladder takes the walks before the pins
    (2026-09-06), a head with a reference path and a judged walk is served with
    its pins and `approved_paths_omitted: 1`. The four tests below are about the
    PREDICATE -- which walk counts as approved -- and the predicate is the sum,
    which no budget moves. The identity of a served walk is asserted in
    `test_an_active_question_is_served_with_its_exact_pins`.
    """
    data = _data(await _call(wired, subject_type="metric", subject_id=METRIC_ID,
                             project_id=chain.project_id))
    exemplar = data["exemplars"][0]
    return len(exemplar["approved_paths"]) + exemplar["approved_paths_omitted"]


async def test_the_comparison_verdict_approves_a_walk_and_the_walks_own_outcome_does_not(
    chain, seeded, wired
):
    """The two flags moved TOGETHER in the first delivery, so neither was measured.

    The amendment's condition is `path_verdict = 'pass'` and nothing else:
    `ai_paths.outcome` is what the execution reported about itself, and a walk
    that ended in error while still following the governed route is exactly the
    shape a model should be shown. Flipping both at once would have been green
    with a query that read `p.outcome` instead of `c.path_verdict`.
    """
    ended_badly = _walk(chain, outcome="failed")
    _judge(
        chain,
        golden_question_version_id=seeded["version_id"],
        domain_id=seeded["domain_id"],
        path_id=ended_badly,
        verdict="pass",
    )

    assert await _walks_approved(chain, wired) == 1


async def test_a_walk_the_comparison_failed_is_not_served_though_it_succeeded(
    chain, seeded, wired
):
    """The mirror: the execution says `succeeded`, the comparison says `fail`."""
    ended_well = _walk(chain, outcome="succeeded")
    _judge(
        chain,
        golden_question_version_id=seeded["version_id"],
        domain_id=seeded["domain_id"],
        path_id=ended_well,
        verdict="fail",
    )

    assert await _walks_approved(chain, wired) == 0


async def test_a_walk_that_is_still_recording_is_not_served_as_approved(
    chain, seeded, wired
):
    """`p.lifecycle = 'finalized'` -- asserted by nothing until this test.

    A recording walk can still grow steps (`reject_step_on_finalized_path` is
    the whole point of the lifecycle), so serving one would show a model a route
    that is not finished. Nothing in the database refuses the comparison row
    below -- `ai_path_reference` refuses it in application code -- which is
    exactly why the predicate has to be measured here.
    """
    open_walk = _walk(chain, finalize=False)
    _judge(
        chain,
        golden_question_version_id=seeded["version_id"],
        domain_id=seeded["domain_id"],
        path_id=open_walk,
        verdict="pass",
    )

    with chain.conn.cursor() as cur:
        cur.execute("SELECT lifecycle FROM app.ai_paths WHERE id = %s", (open_walk,))
        assert cur.fetchone()[0] == "recording", (
            "the walk finalized itself: this test would then measure nothing"
        )

    assert await _walks_approved(chain, wired) == 0


async def test_a_comparison_without_observed_evidence_is_not_served_as_approved(
    chain, seeded, wired
):
    """A case judged with NO walk to look at is not an approved path.

    WHAT THIS MEASURES, EXACTLY. The product property: a `missing` comparison on
    the very version being served, beside an approved one, changes nothing in
    what is served. It does NOT measure that `c.evidence_state = 'observed'` is
    load-bearing, and saying so would be a false claim -- MEASURED 2026-09-05 by
    removing that clause from `exemplars._attach_approved_paths`: this test still
    passes. `ck_evaluation_path_comparisons_state_matches_pin` makes
    `evidence_state = 'observed'` and `observed_ai_path_id IS NOT NULL` the same
    fact, so the inner JOIN on that column already excludes the row. The clause
    stays because it states the amendment's condition where a reader looks for
    it; the database is what enforces it.
    """
    approved = _walk(chain)
    _judge(
        chain,
        golden_question_version_id=seeded["version_id"],
        domain_id=seeded["domain_id"],
        path_id=approved,
    )
    _judge(
        chain,
        golden_question_version_id=seeded["version_id"],
        domain_id=seeded["domain_id"],
        path_id=None,
        verdict="unverifiable",
    )

    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.evaluation_path_comparisons "
            " WHERE project_id = %s AND evidence_state = 'missing'",
            (chain.project_id,),
        )
        assert cur.fetchone()[0] == 1, (
            "no `missing` comparison was written: the assertion below would be "
            "about an empty table"
        )

    assert await _walks_approved(chain, wired) == 1


# ---------------------------------------------------------------------------
# 3. The budget: a prefix, a flag, and the number left out.
# ---------------------------------------------------------------------------


def _ordered_version_ids(chain, subject_id: str = METRIC_ID) -> list[str]:
    """The deterministic ordering itself, read WITHOUT the budget.

    `_fetch_heads` is the one place the prefix is decided (`ORDER BY
    last_stewarded_at DESC, q.id DESC`). Reading it directly is how a test can
    say "the tool served a prefix of the ordering" now that the budget is the
    model channel's and no budget can hold every head.
    """
    heads, _total = exemplars._fetch_heads(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        subject_type="metric",
        subject_id=subject_id,
    )
    return [h["golden_question_version_id"] for h in heads]


@pytest.fixture()
def oversized(chain) -> dict[str, Any]:
    """ONE head deliberately built past the whole model channel.

    Several typed assertions, several reference paths and three judged walks of
    twelve steps each -- the shape G16 met on the harness project, where the
    served `exemplars` value came back as
    `{"withheld": "moved_to_app_channel", "bytes": 4391}`.
    """
    domain_id = _first_domain(chain.conn, chain.org_id)
    spec_versions = [
        _query_spec_version(chain, metric_id=METRIC_ID, metric_version_id=METRIC_VERSION_ID)
        for _ in range(3)
    ]
    head = _golden_question(
        chain,
        domain_id=domain_id,
        query_spec_version_id=spec_versions[0],
        question=(
            "What was paid media spend last completed month, by market, by channel "
            "and by campaign objective, against the same month last year?"
        ),
        title="Paid media spend, the wide question",
        expected_result=[
            {"assertion_type": "value", "member_id": METRIC_ID, "tolerance": None},
            {"assertion_type": "value", "member_id": OTHER_METRIC_ID, "tolerance": None},
            {"assertion_type": "value", "member_id": "sc_EXAMPLE_DAY", "tolerance": None},
        ],
    )
    with chain.conn.cursor() as cur:
        for ordinal, spec_version_id in enumerate(spec_versions[1:], start=1):
            cur.execute(
                """
                INSERT INTO app.golden_question_reference_paths
                    (id, golden_question_version_id, org_id, project_id, ordinal,
                     query_spec_version_id, role)
                VALUES (%s, %s, %s, %s, %s, %s, 'alternative')
                """,
                (
                    _uid("gqr"),
                    head["version_id"],
                    chain.org_id,
                    chain.project_id,
                    ordinal,
                    spec_version_id,
                ),
            )
    for _ in range(3):
        path_id = _walk(chain, steps=12)
        _judge(
            chain,
            golden_question_version_id=head["version_id"],
            domain_id=domain_id,
            path_id=path_id,
        )
    return {"version_id": head["version_id"], "domain_id": domain_id}


async def test_a_head_over_the_channel_is_trimmed_to_fit_and_never_withheld(
    chain, oversized, wired
):
    """THE G16 REGRESSION, and the shape that answers it.

    Measured on revision `mcp-server-00274`, deployed: `get_exemplars` answered
    `"exemplars": {"withheld": "moved_to_app_channel", "bytes": 4391},
    "served": 1` -- the model was handed a marker where the exemplars were, and
    the exemplars are the only reason the tool exists. Two things had to be
    true and neither was: the budget had to be the CHANNEL's (it was 1200
    tokens, about 4800 bytes, against a 4096-byte channel), and one head over
    it had to be TRIMMED rather than served whole.
    """
    from core import model_channel

    raw, _total = exemplars._fetch_heads(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        subject_type="metric", subject_id=METRIC_ID,
    )
    exemplars._attach_reference_paths(
        chain.conn, raw, org_id=chain.org_id, project_id=chain.project_id,
        subject=("metric", METRIC_ID),
    )
    exemplars._attach_approved_paths(
        chain.conn, raw, org_id=chain.org_id, project_id=chain.project_id
    )
    assert exemplars.estimated_bytes(raw[0]) > model_channel.MODEL_CHANNEL_MAX_BYTES, (
        "this fixture must be past the WHOLE model channel, or the test measures "
        f"nothing ({exemplars.estimated_bytes(raw[0])} bytes)"
    )

    result = await _call(wired, subject_type="metric", subject_id=METRIC_ID,
                         project_id=chain.project_id)
    envelope = result.structured_content
    model_visible, app_payload = model_channel.partition_envelope(
        envelope, tool_name="get_exemplars"
    )
    assert app_payload == {}, "the exemplars must not travel to the app channel"
    assert model_visible == envelope
    model_channel.enforce_model_channel("get_exemplars", result.content, envelope)

    data = _data(result)
    exemplar = data["exemplars"][0]
    assert data["served"] == 1
    # NOT a marker. This is the assertion the deployed revision failed.
    assert isinstance(data["exemplars"], list)
    assert "withheld" not in json.dumps(data)

    assert data["trimmed_for_budget"] is True
    assert exemplar["trimmed_for_budget"] is True
    # Every cut carries its count: the payload never reads as a whole exemplar.
    shape = exemplar["expected_answer_shape"]
    pins = exemplar["pins"]
    cuts = (
        shape["assertions_omitted"]
        + pins["reference_paths_omitted"]
        + exemplar["approved_paths_omitted"]
        + sum(p["steps_omitted"] for p in exemplar["approved_paths"])
        + exemplar.get("question_omitted_chars", 0)
    )
    assert cuts > 0
    assert pins["reference_paths_omitted"] == 3 - len(pins["reference_paths"])
    assert exemplar["approved_paths_omitted"] == (
        exemplars.MAX_APPROVED_PATHS - len(exemplar["approved_paths"])
    )
    # What survives still NAMES the whole: the exact version and its hash, so the
    # trimmed detail is one governed read away.
    assert pins["golden_question_version_id"] == oversized["version_id"]
    assert pins["content_hash"]
    assert exemplars.estimate_tokens(exemplar) <= data["budget_tokens"]
    assert "trimmed" in result.content[0].text


async def test_a_budget_above_the_channel_is_clamped_to_what_the_channel_carries(
    chain, seeded, wired
):
    """The old default -- 1200 tokens -- is now above the ceiling, and says so."""
    assert exemplars.MAX_BUDGET_TOKENS == exemplars.DEFAULT_BUDGET_TOKENS
    assert exemplars.MAX_BUDGET_TOKENS * exemplars.BYTES_PER_TOKEN == (
        exemplars.EXEMPLAR_LIST_MAX_BYTES
    )
    assert exemplars.ENVELOPE_RESERVE_BYTES + exemplars.EXEMPLAR_LIST_MAX_BYTES == (
        exemplars.MODEL_CHANNEL_MAX_BYTES
    )

    data = _data(
        await _call(wired, subject_type="metric", subject_id=METRIC_ID,
                    project_id=chain.project_id, budget_tokens=1200)
    )
    assert data["budget_tokens"] == exemplars.MAX_BUDGET_TOKENS


@pytest.fixture()
def three_questions(chain) -> list[str]:
    domain_id = _first_domain(chain.conn, chain.org_id)
    version_ids = []
    for index in range(3):
        spec_version_id = _query_spec_version(
            chain, metric_id=METRIC_ID, metric_version_id=METRIC_VERSION_ID
        )
        head = _golden_question(
            chain,
            domain_id=domain_id,
            query_spec_version_id=spec_version_id,
            question=f"Question number {index} about paid media spend?",
            title=f"Exemplar {index}",
        )
        version_ids.append(head["version_id"])
    return version_ids


async def test_the_budget_truncates_deterministically_and_says_how_much_it_hid(
    chain, three_questions, wired
):
    """The list is a PREFIX of the ordering, and the tail is counted, not hidden.

    The ordering is read from `_fetch_heads`, which is where it is decided:
    since the budget became the model channel's (2048 bytes for the list), no
    budget holds three heads, so a test that asked for "the whole list" would be
    asking for something the channel never carries.
    """
    ordering = _ordered_version_ids(chain)
    assert len(ordering) == 3

    cut = _data(
        await _call(
            wired,
            subject_type="metric",
            subject_id=METRIC_ID,
            project_id=chain.project_id,
            budget_tokens=exemplars.MAX_BUDGET_TOKENS,
        )
    )
    assert cut["truncated"] is True
    assert cut["served"] >= 1
    assert cut["served"] + cut["omitted_for_budget"] == len(ordering)
    assert cut["omitted_beyond_max_exemplars"] == 0
    # A PREFIX of the ordering, not the cheapest subset.
    assert [e["golden_question_version_id"] for e in cut["exemplars"]] == (
        ordering[: cut["served"]]
    )
    assert cut["token_estimate_method"] == "utf-8 bytes of the canonical JSON // 4"
    assert cut["estimated_tokens"] <= cut["budget_tokens"]

    again = _data(
        await _call(
            wired,
            subject_type="metric",
            subject_id=METRIC_ID,
            project_id=chain.project_id,
            budget_tokens=exemplars.MAX_BUDGET_TOKENS,
        )
    )
    assert again["exemplars"] == cut["exemplars"]
    assert "left out" in _summary_text(cut)


def _summary_text(data: dict[str, Any]) -> str:
    return exemplars_mcp._summary(data)


@pytest.fixture()
def more_heads_than_the_bound(chain) -> int:
    """`MAX_EXEMPLARS + 1` active questions on the same metric, one Query Spec.

    One shared Query Spec version: the bound is about how many HEADS match, and
    twenty-six specs would only make the fixture slower.
    """
    domain_id = _first_domain(chain.conn, chain.org_id)
    spec_version_id = _query_spec_version(
        chain, metric_id=METRIC_ID, metric_version_id=METRIC_VERSION_ID
    )
    wanted = exemplars.MAX_EXEMPLARS + 1
    for index in range(wanted):
        _golden_question(
            chain,
            domain_id=domain_id,
            query_spec_version_id=spec_version_id,
            question=f"Question {index} about paid media spend by market?",
            title=f"Beyond the bound {index}",
        )
    return wanted


async def test_the_server_bound_is_a_declared_cut_not_a_silent_one(
    chain, more_heads_than_the_bound, wired
):
    """A second cut nobody was told about is a truncation that reads complete.

    Measured before this test existed: with twenty-six matching active
    questions the payload answered `served: 25, omitted_for_budget: 0,
    truncated: false` at any budget -- a caller could not tell that list from
    the whole of the subject, and raising `budget_tokens` would never have
    changed it.
    """
    data = _data(
        await _call(
            wired,
            subject_type="metric",
            subject_id=METRIC_ID,
            project_id=chain.project_id,
            budget_tokens=exemplars.MAX_BUDGET_TOKENS,
        )
    )

    assert data["matching_active_questions"] == more_heads_than_the_bound
    assert data["max_exemplars"] == exemplars.MAX_EXEMPLARS
    assert data["served"] <= exemplars.MAX_EXEMPLARS
    assert data["omitted_beyond_max_exemplars"] == (
        more_heads_than_the_bound - exemplars.MAX_EXEMPLARS
    )
    # The two cuts add up to everything that matched and was not served.
    assert data["omitted_total"] == data["omitted_for_budget"] + (
        data["omitted_beyond_max_exemplars"]
    )
    assert data["served"] + data["omitted_total"] == more_heads_than_the_bound
    assert data["truncated"] is True

    # And the LLM channel says which of the two cuts a bigger budget can lift.
    summary = _summary_text(data)
    assert "narrow the subject" in summary
    assert str(data["omitted_total"]) in summary


async def test_the_served_time_is_the_stewardship_column_and_is_named_as_one(
    chain, seeded, wired
):
    """`golden_questions.updated_at` is not an activation date, so it is not served as one.

    Create, new version and lifecycle change all move that column
    (`golden_questions.py:1959`, `:2038`, `:2110`), so `activated_at` named one
    of three gestures and a caller reading it would date a re-titled question to
    a day nothing was activated. The served value is that column, verbatim.
    """
    exemplar = _data(
        await _call(wired, subject_type="metric", subject_id=METRIC_ID,
                    project_id=chain.project_id)
    )["exemplars"][0]

    assert "activated_at" not in exemplar
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT updated_at FROM app.golden_questions WHERE id = %s",
            (seeded["golden_question_id"],),
        )
        stored = cur.fetchone()[0]
    assert exemplar["last_stewarded_at"] == stored.isoformat()


async def test_the_order_is_that_same_column_most_recently_stewarded_first(
    chain, three_questions, wired
):
    """The prefix is decided by the field that is served, not by a second one.

    Every head of this fixture carries the SAME `updated_at` -- one transaction,
    one `NOW()` -- so the ordering is settled by the id tie-break until the
    column is actually moved. Moving one head forward must move it to the front.
    """
    call = dict(subject_type="metric", subject_id=METRIC_ID,
                project_id=chain.project_id,
                budget_tokens=exemplars.MAX_BUDGET_TOKENS)
    order_before = _ordered_version_ids(chain)
    assert len(order_before) == 3
    served_before = _data(await _call(wired, **call))["exemplars"]
    assert served_before[0]["golden_question_version_id"] == order_before[0]

    with chain.conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.golden_questions
               SET updated_at = updated_at + interval '1 hour'
             WHERE current_version_id = %s
            """,
            (order_before[-1],),
        )

    assert _ordered_version_ids(chain) == [order_before[-1], *order_before[:-1]]
    # And the TOOL serves the head of that ordering, budget or no budget: the
    # column that decides the prefix is the column the payload serves.
    served_after = _data(await _call(wired, **call))["exemplars"]
    assert served_after[0]["golden_question_version_id"] == order_before[-1]
    assert served_after[0]["last_stewarded_at"] > served_before[0]["last_stewarded_at"]


# ---------------------------------------------------------------------------
# 4. The refusals.
# ---------------------------------------------------------------------------


async def test_the_real_scope_seam_refuses_a_second_project_and_the_two_refusals_are_one(
    live_postgres, monkeypatch
):
    """The REAL guard, the REAL acquisition, and a Project that really exists.

    The test below this one patches `refuse_unless_project_scope`,
    `_resolve_project` and `request_connection`, so it proves that a name is
    called and nothing about what that name decides. Here nothing of the seam is
    replaced: `core.main._resolve_project` resolves existence on an armed
    connection, `core.mcp_scope.refuse_unless_project_scope` resolves access
    through `resolve_strict_resource_access`, and both open their own
    `core.db.request_connection`.

    Globex's project EXISTS and is not the caller's; `proj_EXAMPLE` exists
    nowhere. The two refusals must be the SAME string -- a distinguishable pair
    is an enumeration oracle, which is the property `core/mcp_scope.py` states
    first. Acme's project is called last: without it a guard that refused
    everything would pass this test.
    """
    import os
    from types import SimpleNamespace

    import core.main as core_main
    from core.project_resolver import PROJECT_NOT_FOUND_CODE

    from tests.core.test_mcp_tools_request_connection_rls_pg import _TwoOrganizations

    two = _TwoOrganizations(live_postgres).build()

    # Without `oauth` the single-operator carve-out applies and the guard never
    # resolves anything: the test would measure the derogation.
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setattr(
        core_main,
        "get_access_token",
        lambda: SimpleNamespace(claims={"sub": two.subject}, client_id="cli"),
    )

    read: list[str] = []
    real_list = exemplars.list_exemplars

    def _watched(conn, **kwargs):
        read.append(kwargs["project_id"])
        return real_list(conn, **kwargs)

    monkeypatch.setattr(exemplars, "list_exemplars", _watched)

    target = FastMCP("story-75-3-strangers")
    exemplars_mcp.register(target)

    async def _refusal(project_id: str) -> str:
        async with Client(FastMCPTransport(target)) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool(
                    "get_exemplars",
                    {"subject_type": "metric", "subject_id": METRIC_ID,
                     "project_id": project_id},
                )
        return str(excinfo.value)

    unreachable = await _refusal(two.globex_project)
    absent = await _refusal("proj_EXAMPLE")

    assert PROJECT_NOT_FOUND_CODE in unreachable
    assert unreachable == absent, (
        "a Project the caller may not reach must answer exactly like one that "
        "does not exist, or comparing two refusals enumerates the neighbours"
    )
    assert read == [], "the store was opened for a Project the guard had refused"

    async with Client(FastMCPTransport(target)) as client:
        allowed = await client.call_tool(
            "get_exemplars",
            {"subject_type": "metric", "subject_id": METRIC_ID,
             "project_id": two.acme_project},
        )
    assert _data(allowed)["exemplar_store_state"] == "available"
    assert read == [two.acme_project], (
        "the caller's OWN project was refused too: the assertions above would "
        "then be about a guard that refuses everything"
    )


async def test_a_foreign_project_is_refused_and_nothing_is_read(chain, seeded, wired):
    with pytest.raises(ToolError) as refusal:
        await _call(
            wired, subject_type="metric", subject_id=METRIC_ID, project_id=FOREIGN_PROJECT
        )
    assert "project_not_found" in str(refusal.value)
    # The guard runs BEFORE the store: a refused caller never opens a connection,
    # so it cannot learn from a timing difference that the Project exists.
    assert wired["opened"] == []
    assert wired["refused"] == [FOREIGN_PROJECT]


async def test_an_unknown_subject_type_is_refused_by_name_never_widened(chain, seeded, wired):
    with pytest.raises(ToolError) as refusal:
        await _call(
            wired, subject_type="dashboard", subject_id=METRIC_ID, project_id=chain.project_id
        )
    message = str(refusal.value)
    assert "unknown_subject_type" in message
    assert "metric" in message and "view" in message and "topic" in message
    # Refused before the scope seam: the caller's typo must not be reported as a
    # missing Project.
    assert wired["refused"] == [] and wired["opened"] == []


# ---------------------------------------------------------------------------
# 5. The corpus stays test code.
# ---------------------------------------------------------------------------


_OWNED = ("exemplars.py", "exemplars_mcp.py")
_CORE = Path(__file__).resolve().parents[3] / "server" / "core"


def _executable_source(name: str) -> str:
    """The module's CODE -- docstrings and comments removed.

    Scanning raw text would flag the very prose that explains why the boundary
    exists. The boundary is about what the code DOES.
    """
    tree = ast.parse((_CORE / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


@pytest.mark.parametrize("module", _OWNED)
def test_the_evaluation_corpus_is_not_runtime_knowledge(module):
    body = _executable_source(module)
    for reference in ("corpus.yaml", "tests/evals", "tests.evals", "seed_eval_platform"):
        assert reference not in body, (
            f"{module} names `{reference}`. `analyze-and-test.md:243-247`: the "
            "repository corpus remains test code and never becomes runtime knowledge."
        )


@pytest.mark.parametrize("module", _OWNED)
def test_this_surface_writes_nothing_at_all(module):
    body = _executable_source(module).upper()
    for statement in ("INSERT INTO", "UPDATE APP.", "DELETE FROM"):
        assert statement not in body, (
            f"{module} contains `{statement}`. `get_exemplars` is declared "
            "effect=read: a read does not write."
        )


def test_the_tool_is_declared_like_its_three_neighbours():
    from core.mcp_profiles import _REGISTRY

    target = FastMCP("declaration-check")
    exemplars_mcp.register(target)
    declaration = _REGISTRY.declarations["get_exemplars"]
    assert declaration.profile == "insights"
    assert declaration.effect == "read"
    assert declaration.data_class == "operational"
    assert declaration.confirmation_mode == "none"


def test_the_wire_description_stays_inside_the_catalog_budget():
    import inspect

    description = inspect.getdoc(exemplars_mcp.get_exemplars) or ""
    assert len(description.encode("utf-8")) <= 600, (
        "a tool description is catalog BUDGET: every host pays for it on every call"
    )


def test_the_three_selectors_and_the_one_lifecycle_are_the_documented_ones():
    """The amendment names three subjects and one lifecycle. So does the code."""
    assert exemplars.SUBJECT_TYPES == ("metric", "view", "topic")
    assert exemplars.SERVED_LIFECYCLE == "active"


def test_the_trim_ladder_runs_in_the_documented_order_and_declares_every_rung():
    """The five rungs, on one synthetic head, cut in the order the amendment states.

    The Postgres test above proves the tool never withholds; this one proves the
    ORDER, including the last two rungs a fixture cannot comfortably reach: the
    question text (never before the structure around it) and `expected_ai_path`
    (only when nothing else was enough, and replaced by a stated descriptor
    rather than removed).
    """
    head = {
        "golden_question_id": "gq_EXAMPLE",
        "title": "A wide question",
        "owner": "owner@example.com",
        "lifecycle": "active",
        "last_stewarded_at": "2026-09-06T00:00:00+00:00",
        "golden_question_version_id": "gqv_EXAMPLE",
        "version_number": 3,
        "question": "Q" * 4000,
        "time_boundary": {"as_of": "2026-08-31", "grain": "month"},
        "expected_ai_path": {"required_nodes": [{"key": "k" * 200} for _ in range(6)]},
        "expected_answer_shape": {
            "result_type": "breakdown",
            "severity": "critical",
            "capability_tags": ["media_spend_reporting"],
            "assertions": [{"assertion_type": "value", "member_id": f"sc_{i}"} for i in range(8)],
            "assertions_omitted": 0,
        },
        "pins": {
            "golden_question_version_id": "gqv_EXAMPLE",
            "version_number": 3,
            "content_hash": HASH,
            "reference_paths": [{"ordinal": i, "query_spec_version_id": f"qsv_{i}"}
                                for i in range(8)],
            "reference_paths_omitted": 0,
        },
        "approved_paths": [
            {
                "observed_ai_path_id": f"aip_{i}",
                "steps": [{"ordinal": j, "tool_name": "search_context"} for j in range(12)],
                "steps_omitted": 0,
            }
            for i in range(3)
        ],
        "approved_paths_omitted": 0,
    }
    whole = exemplars.estimated_bytes(head)
    assert whole > exemplars.EXEMPLAR_LIST_MAX_BYTES

    # A budget small enough that the ladder must reach its last rung.
    trimmed = exemplars._trim_head(head, exemplars.MIN_BUDGET_TOKENS)

    # THE GUARANTEE THAT MATTERS: whatever the caller asked for, a head cut down
    # every rung fits the list budget -- so `partition_envelope` has nothing to
    # move and the model is never handed a marker. The clamp floor
    # (`MIN_BUDGET_TOKENS`) is below the irreducible core of an exemplar -- its
    # identity, its pins and 80 characters of question -- and the ladder stops
    # there rather than shredding what names the version.
    assert exemplars.estimate_tokens(trimmed) <= exemplars.MAX_BUDGET_TOKENS
    assert trimmed["trimmed_for_budget"] is True
    # 1..3: the structure went first, and each cut is counted where it happened.
    assert trimmed["expected_answer_shape"]["assertions"] == []
    assert trimmed["expected_answer_shape"]["assertions_omitted"] == 8
    assert trimmed["pins"]["reference_paths"] == []
    assert trimmed["pins"]["reference_paths_omitted"] == 8
    assert trimmed["approved_paths"] == []
    assert trimmed["approved_paths_omitted"] == 3
    # 4: the question, and never below the floor that keeps it a question.
    assert len(trimmed["question"]) >= exemplars.MIN_QUESTION_CHARS
    assert trimmed["question_omitted_chars"] == 4000 - len(trimmed["question"])
    # 5: the pattern, stated rather than silently gone.
    assert trimmed["expected_ai_path"]["omitted_for_budget"] is True
    assert trimmed["expected_ai_path"]["bytes"] > 0
    # And what it still names is exact, so the whole of it is one read away.
    assert trimmed["pins"]["golden_question_version_id"] == "gqv_EXAMPLE"
    assert trimmed["pins"]["content_hash"] == HASH


def test_a_head_that_fits_is_never_touched_by_the_ladder():
    head = {
        "question": "What was spend last month?",
        "expected_ai_path": {"required_nodes": []},
        "expected_answer_shape": {"assertions": [{"a": 1}], "assertions_omitted": 0},
        "pins": {"reference_paths": [{"ordinal": 0}], "reference_paths_omitted": 0},
        "approved_paths": [],
        "approved_paths_omitted": 0,
    }
    before = json.loads(json.dumps(head))
    assert exemplars._trim_head(head, exemplars.MAX_BUDGET_TOKENS) == before
    assert "trimmed_for_budget" not in head
