"""Story 50.4 -- AC5: changing how an answer LOOKS is not changing what it ANSWERS.

This is the invariant most likely to be broken by an implementation, so it is
asserted as ROW COUNTS rather than as prose. Every presentation-only test below
counts the six Story 50.1 tables before and after with an explicit
`select count(*)`, and asserts the numbers are unchanged. A test that only checked
"a new visualization version exists" would stay green while a presentation edit
quietly re-executed the query -- which is precisely the confusion this AC exists
to prevent.

The mirror property is asserted too: a caller trying to change grain, a filter, a
limit or a comparison through the presentation door is REFUSED with
`query_owned_field` and told who owns it, and no version is written.
"""

from __future__ import annotations

import pytest
from core.visualization_specs import (
    VisualizationSpecRefused,
    create_visualization_spec_version,
    validate_visualization_spec,
)

from tests.core.test_visualization_specs_pg import Chain, bar_document

pytestmark = pytest.mark.usefixtures("live_postgres")

#: The six tables a presentation edit must never touch (AC5), by name.
QUERY_TABLES = (
    "app.query_specs",
    "app.query_spec_versions",
    "app.query_execution_attempts",
    "app.query_execution_attempt_events",
    "app.query_results",
    "app.query_result_payloads",
)


def counts(conn) -> dict[str, int]:
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in QUERY_TABLES:
            cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - fixed literal list
            out[table] = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM app.visualization_spec_versions")
        out["app.visualization_spec_versions"] = int(cur.fetchone()[0])
    return out


@pytest.fixture()
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


def revise(chain: Chain, visualization_id: str, document: dict, *, pin: str | None = None):
    validated = validate_visualization_spec(
        chain.conn,
        project_id=chain.project_id,
        query_spec_version_id=pin or chain.query_spec_version_id,
        payload=document,
    )
    return create_visualization_spec_version(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        validated=validated,
        actor="test",
        visualization_id=visualization_id,
    )


# ---------------------------------------------------------------------------
# A presentation-only change writes ONE row, and only in this story's table.
# ---------------------------------------------------------------------------


#: Every presentation-only change AC5 enumerates, one parametrized case each. A
#: single case would prove the family, not the class (CLAUDE.md §4).
PRESENTATION_ONLY_EDITS = {
    "family": {"family": "line", "bindings": {"measure": ["clicks"],
                                              "dimension": ["channel"]}},
    "well_binding": {"bindings": {"measure": ["clicks"], "dimension": ["channel"],
                                  "label": ["channel"]}},
    "axis": {"axes": {"y": {"scale": "log", "zero_baseline": False}}},
    "legend": {"legend": {"position": "bottom", "visible": False}},
    "formatting": {"formatting": {"number_style": "compact", "date_style": "short"}},
    "colour_role": {"color": {"role": "categorical", "semantic_direction": "higher_is_better"}},
    "threshold": {"thresholds": [{"member_id": "clicks", "comparator": "gt", "value": 100,
                                  "severity": "warning"}]},
    "reference_line": {"reference_lines": [{"member_id": "clicks", "kind": "average"}]},
    "interaction": {"interactions": {"zoom": True, "local_filter": True}},
    "responsive_profile": {"responsive": {"profiles": ["console", "share"]}},
    "label_override": {"labels": {"override": {"clicks": "Clicks, net"}}},
}


@pytest.mark.parametrize(
    "edit", sorted(PRESENTATION_ONLY_EDITS), ids=sorted(PRESENTATION_ONLY_EDITS)
)
def test_pg_a_presentation_only_change_touches_no_query_table(chain, edit):
    created = chain.visualization()
    before = counts(chain.conn)

    edited = bar_document(**PRESENTATION_ONLY_EDITS[edit])
    revised = revise(chain, created["visualization_id"], edited)

    after = counts(chain.conn)
    assert revised["version_number"] == 2
    assert after["app.visualization_spec_versions"] == before["app.visualization_spec_versions"] + 1
    for table in QUERY_TABLES:
        assert after[table] == before[table], (
            f"the `{edit}` presentation edit changed {table}: "
            f"{before[table]} -> {after[table]}"
        )
    chain.conn.rollback()


def test_pg_the_new_version_keeps_the_same_query_spec_version_pin(chain):
    created = chain.visualization()
    revised = revise(chain, created["visualization_id"], bar_document(family="line"))
    assert revised["query_spec_version_id"] == created["query_spec_version_id"]
    assert revised["query_spec_version_id"] == chain.query_spec_version_id
    chain.conn.rollback()


def test_pg_the_new_version_names_its_predecessor(chain):
    created = chain.visualization()
    revised = revise(chain, created["visualization_id"], bar_document(family="line"))
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT predecessor_version_id FROM app.visualization_spec_versions WHERE id = %s",
            (revised["id"],),
        )
        assert cur.fetchone()[0] == created["id"]
    chain.conn.rollback()


def test_pg_the_prior_version_is_untouched_by_the_revision(chain):
    created = chain.visualization()
    revise(chain, created["visualization_id"], bar_document(family="line"))
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT family, content_hash FROM app.visualization_spec_versions WHERE id = %s",
            (created["id"],),
        )
        family, content_hash = cur.fetchone()
    assert family == "bar"
    assert content_hash == created["content_hash"]
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# The mirror: a query-owned field is refused, and NO version is written.
# ---------------------------------------------------------------------------


QUERY_OWNED_ATTEMPTS = {
    "grain": {"grain": "week"},
    "filter": {"filters": [{"member_id": "channel", "operator": "eq", "value": "paid"}]},
    "time_range": {"time_range": {"start": "2026-01-01", "end": "2026-01-31"}},
    "comparison": {"comparison": "previous_period"},
    "limit": {"limit": 25},
    "measure_selection": {"measures": [{"id": "impressions"}]},
    "timezone": {"timezone": "Europe/Paris"},
    "aggregation": {"aggregation": "sum"},
}


@pytest.mark.parametrize("attempt", sorted(QUERY_OWNED_ATTEMPTS), ids=sorted(QUERY_OWNED_ATTEMPTS))
def test_pg_a_query_owned_field_is_refused_and_writes_nothing(chain, attempt):
    created = chain.visualization()
    before = counts(chain.conn)

    with pytest.raises(VisualizationSpecRefused) as exc:
        revise(
            chain, created["visualization_id"],
            bar_document(**QUERY_OWNED_ATTEMPTS[attempt]),
        )

    refusals = exc.value.as_dict()["refusals"]
    assert "query_owned_field" in [r["code"] for r in refusals], refusals
    owned = next(r for r in refusals if r["code"] == "query_owned_field")
    # The message names the owner -- or, for a control NOBODY applies, says so.
    # `timezone` used to answer "belongs to the Query Spec and to server
    # execution" while the Query Spec refused it too, which sent a person from
    # one closed door to another.
    assert any(
        phrase in owned["message"]
        for phrase in ("Query Spec", "Semantic View", "applied nowhere on this path")
    ), owned["message"]
    assert "Explore" in owned["remedy"], "the remedy must name where the query is changed"

    assert counts(chain.conn) == before, "a refused revision wrote a row"
    chain.conn.rollback()


def test_pg_the_grain_refusal_says_exactly_who_owns_grain(chain):
    """AC5 quotes this sentence. It is asserted rather than paraphrased."""
    created = chain.visualization()
    with pytest.raises(VisualizationSpecRefused) as exc:
        revise(chain, created["visualization_id"], bar_document(grain="week"))
    owned = next(
        r for r in exc.value.as_dict()["refusals"]
        if r["code"] == "query_owned_field" and r["subject"] == "/grain"
    )
    assert owned["message"] == "Grain belongs to the Query Spec."
    assert owned["remedy"].startswith("Changing it produces a new Result")
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# A genuine query change: re-pinning is a visible, revalidated act.
# ---------------------------------------------------------------------------


def test_pg_repinning_to_a_new_query_spec_version_creates_version_two(chain):
    created = chain.visualization()
    new_pin = chain.next_query_spec_version()
    before = counts(chain.conn)

    revised = revise(chain, created["visualization_id"], bar_document(), pin=new_pin)

    assert revised["version_number"] == 2
    assert revised["query_spec_version_id"] == new_pin
    after = counts(chain.conn)
    # The Query Spec version was created by EXPLORE, not by this call: the repin
    # itself adds exactly one visualization version and nothing else.
    assert after["app.query_spec_versions"] == before["app.query_spec_versions"]
    assert after["app.visualization_spec_versions"] == before["app.visualization_spec_versions"] + 1
    chain.conn.rollback()


def test_pg_a_repin_that_fails_revalidation_is_refused_and_drops_no_binding(chain):
    """AC5: no binding is silently dropped to make a repin succeed, and no version
    is written. Version 2 binds a member; the new pin no longer carries it."""
    import json

    created = chain.visualization()
    # A genuine query change that REMOVES the measure the presentation binds.
    narrowed = "qsv_narrowed_" + created["id"].split("_", 1)[1]
    spec = dict(Chain.SPEC)
    spec["measures"] = [{"id": "cost", "version_id": "mv_2"}]
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, %s, %s, %s::jsonb, %s, %s, 'test')
            """,
            (
                narrowed, chain.query_spec_id, chain.org_id, chain.project_id,
                chain.view_id, chain.view_version_id, json.dumps(spec), "c" * 64,
                chain.query_spec_version_id,
            ),
        )
    before = counts(chain.conn)

    with pytest.raises(VisualizationSpecRefused) as exc:
        revise(chain, created["visualization_id"], bar_document(), pin=narrowed)

    refusals = exc.value.as_dict()["refusals"]
    assert [r["code"] for r in refusals] == ["unknown_member"]
    assert refusals[0]["subject"] == "/bindings/measure/0"
    assert counts(chain.conn) == before, "a refused repin wrote a version"
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# F1 -- re-pinning may move to a new VERSION, never to another Query Spec.
#
# The service refuses it here; migration 164's widened head foreign key refuses it
# in `test_visualization_specs_pg.py` for the callers that never reach this code.
# Both layers, because the reviewer reproduced the defect against the database:
#
#     head.query_spec_id = qs_...KS5 · v1 = qs_...KS5 · v2 = qs_...Q2K  <- accepted
# ---------------------------------------------------------------------------


def test_pg_a_revision_cannot_move_the_visualization_to_another_query_spec(chain):
    created = chain.visualization()
    _other_query_spec_id, foreign_pin = chain.other_query_spec_version()
    before = counts(chain.conn)

    with pytest.raises(VisualizationSpecRefused) as exc:
        revise(chain, created["visualization_id"], bar_document(), pin=foreign_pin)

    refusals = exc.value.as_dict()["refusals"]
    assert [r["code"] for r in refusals] == ["query_spec_mismatch"], refusals
    assert refusals[0]["subject"] == "/query_spec_version_id"
    assert refusals[0]["remedy"], "a refusal without a remedy makes the reader guess"
    assert counts(chain.conn) == before, "a refused re-pin wrote a version"
    chain.conn.rollback()


def test_pg_the_head_still_advertises_the_query_spec_its_versions_name(chain):
    """The property the defect broke, asserted directly rather than through the
    refusal: every version of a Visualization names the head's Query Spec."""
    created = chain.visualization()
    revise(chain, created["visualization_id"], bar_document(family="line"))
    revise(chain, created["visualization_id"], bar_document(), pin=chain.next_query_spec_version())

    with chain.conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT h.query_spec_id, v.query_spec_id
            FROM app.visualization_spec_versions v
            JOIN app.visualizations h ON h.id = v.visualization_id
            WHERE v.visualization_id = %s
            """,
            (created["visualization_id"],),
        )
        pairs = cur.fetchall()
    assert pairs == [(chain.query_spec_id, chain.query_spec_id)], pairs
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# A local presentation interaction writes NOTHING at all.
# ---------------------------------------------------------------------------


def test_pg_a_local_interaction_writes_no_version_because_it_is_not_a_spec_edit(chain):
    """Hover, selection, zoom, legend toggle and a local row filter are runtime
    DISPLAY STATE (`visualization-and-rendering.md:82-87`). There is no server call
    that records one, and this test asserts that absence structurally: the module
    exposes no function that could."""
    import core.visualization_specs as module

    created = chain.visualization()
    before = counts(chain.conn)

    exported = {name for name in dir(module) if not name.startswith("_")}
    for forbidden in ("record_display_state", "apply_local_filter", "set_zoom",
                      "persist_interaction", "record_hover"):
        assert forbidden not in exported

    assert counts(chain.conn) == before
    assert created["version_number"] == 1
    chain.conn.rollback()
