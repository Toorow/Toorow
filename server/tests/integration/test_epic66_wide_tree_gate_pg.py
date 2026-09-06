"""G66-B's arity half: THREE and FOUR sources, executed (stories 66.4 and 66.11).

The two-source journey has had a gate since 2026-08-13. What G66-B also demands
had none, and the audit of 2026-08-17 measured its absence:

> Three- and four-source trees, competing paths and composite keys with a null
> component preserve every per-source control total; no path is chosen silently.

The capability was never in doubt -- `compile_plan` accepts 2..4 members over a
tree of N-1 edges and `_graph_traversal` roots any tree at its primary. What was
missing is the proof that a WIDER tree still keeps each source's own total, which
is exactly the property extra members are able to break: every additional edge is
another chance to multiply rows, and a sum that has been multiplied still looks
like a sum.

Two shapes, because they fail differently:

    a CHAIN of three   -- spend -> conversions -> viewability, the multi-hop case
    a STAR of four     -- spend at the centre, three leaves

Each world carries a key only one source has, and a row whose campaign part is
NULL, so `matched_only` has something real to drop.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")
duckdb = pytest.importorskip("duckdb")

from core import match_profile  # noqa: E402
from core import mdm_common_keys as keys  # noqa: E402
from core import multi_source_execution as execution  # noqa: E402
from core import multi_source_plan as plans  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    make_canonical_field,
    make_datastream,
    make_project,
    make_semantic_view,
    pin_relationship,
    publish_output,
)

#: `spend` repeats `(2026-08-01, A)`. It is the PRIMARY of both trees below, and
#: `many_to_one` (the fixture default) declares the left side as the many one --
#: so the duplicate is one-sided on every edge, which is the only fan-out the
#: compiler makes safe by aggregate-before-merge. Two-sided duplication is the
#: open arbitration AI-293 and is deliberately not what this gate tests.
_SPEND_ROWS = [
    ("2026-08-01", "A", 10),
    ("2026-08-01", "A", 8),      # the one-sided duplicate: 18, never 10 or 36
    ("2026-08-02", "B", 20),
    ("2026-08-03", "C", 30),     # C is here only -> unmatched
]
_CONVERSION_ROWS = [
    ("2026-08-01", "A", 100),
    ("2026-08-02", "B", 40),
    ("2026-08-02", None, 7),     # the NULL composite part -> never matched
]
_VIEWABILITY_ROWS = [
    ("2026-08-01", "A", 900),
    ("2026-08-02", "B", 400),
    ("2026-08-09", "Z", 5),      # Z is here only -> unmatched
]
_BRAND_ROWS = [
    ("2026-08-01", "A", 3),
    ("2026-08-02", "B", 4),
]

#: What each source is worth on the matched perimeter -- `(2026-08-01, A)` and
#: `(2026-08-02, B)` -- computed by hand from the rows above. Every assertion of
#: this gate compares the MERGED figure to one of these, because a merged figure
#: that drifts from its own source is precisely what fan-out looks like.
_MATCHED_TOTALS = {
    "spend": 18 + 20,          # A folds 10 + 8; C is out
    "revenue": 100 + 40,       # the NULL-campaign row is out
    "viewable": 900 + 400,     # Z is out
    "brand": 3 + 4,
}


@pytest.fixture()
def world(live_postgres, tmp_path, monkeypatch):
    """Four published Datastreams over one common key, on a real DuckDB."""
    path = tmp_path / "wide.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute("CREATE TABLE main_marts.spend (date DATE, campaign TEXT, spend_micros BIGINT)")
    con.execute(
        "CREATE TABLE main_marts.conversions "
        "(event_date DATE, campaign_key TEXT, revenue_micros BIGINT)"
    )
    con.execute(
        "CREATE TABLE main_marts.viewability (day DATE, campaign_name TEXT, viewable BIGINT)"
    )
    con.execute("CREATE TABLE main_marts.brand (d DATE, camp TEXT, lift BIGINT)")
    for row in _SPEND_ROWS:
        con.execute("INSERT INTO main_marts.spend VALUES (?,?,?)", list(row))
    for row in _CONVERSION_ROWS:
        con.execute("INSERT INTO main_marts.conversions VALUES (?,?,?)", list(row))
    for row in _VIEWABILITY_ROWS:
        con.execute("INSERT INTO main_marts.viewability VALUES (?,?,?)", list(row))
    for row in _BRAND_ROWS:
        con.execute("INSERT INTO main_marts.brand VALUES (?,?,?)", list(row))
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))

    org_id, project_id = make_project(live_postgres, "Wide tree")
    day = make_canonical_field(live_postgres, project_id, "day", value_type="date")
    campaign = make_canonical_field(live_postgres, project_id, "campaign_id")
    spend = make_canonical_field(
        live_postgres, project_id, "spend", kind="metric", value_type="money"
    )
    revenue = make_canonical_field(
        live_postgres, project_id, "revenue", kind="metric", value_type="money"
    )
    viewable = make_canonical_field(
        live_postgres, project_id, "viewable", kind="metric", value_type="integer"
    )
    brand = make_canonical_field(
        live_postgres, project_id, "brand", kind="metric", value_type="integer"
    )

    members = {}
    for label, name, columns, measure_field in (
        ("spend", "Campaign spend", ("date", "campaign", "spend_micros"), spend),
        ("conversions", "Conversions", ("event_date", "campaign_key", "revenue_micros"), revenue),
        ("viewability", "Viewability", ("day", "campaign_name", "viewable"), viewable),
        ("brand", "Brand lift", ("d", "camp", "lift"), brand),
    ):
        datastream_id = make_datastream(
            live_postgres, org_id, project_id, name,
            bindings={
                day: (columns[0], "confirmed"),
                campaign: (columns[1], "confirmed"),
                measure_field: (columns[2], "confirmed"),
            },
        )
        publish_output(live_postgres, org_id, project_id, datastream_id, label)
        members[label] = datastream_id

    key = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[day, campaign],
        actor="tester",
    )
    _view_id, view_version_id = make_semantic_view(live_postgres, project_id)
    return {
        "org_id": org_id,
        "project_id": project_id,
        "view_version_id": view_version_id,
        "key_version_id": key["current_version"]["id"],
        "day": day,
        "campaign": campaign,
        "measures": {
            "spend": spend, "revenue": revenue, "viewable": viewable, "brand": brand,
        },
        **members,
    }


def _pin(conn, world, edges):
    """One approved relationship per edge, each named after the pair it governs."""
    for ordinal, (left, right) in enumerate(edges):
        pin_relationship(
            conn,
            world["view_version_id"],
            world["key_version_id"],
            ordinal=ordinal,
            name=f"{left}_to_{right}",
            left_datastream_id=world[left],
            right_datastream_id=world[right],
        )


def _profile_lookup(conn, world):
    """Measure the edge the compiler is asking about -- never a canned answer.

    The compiler calls this once per edge with the exact pair, key version,
    relationship name and view version it is freezing. Returning a single
    pre-measured profile would let a three-edge tree be frozen on one edge's
    evidence, which is the failure this gate exists to make impossible.
    """

    def lookup(left_id, right_id, key_version_id, relationship_name, view_version_id):
        return match_profile.profile_match(
            conn,
            project_id=world["project_id"],
            left_datastream_id=left_id,
            right_datastream_id=right_id,
            common_key_version_id=key_version_id,
            relationship_name=relationship_name,
            view_version_id=view_version_id,
        )

    return lookup


def _run(conn, world, labels, edges):
    _pin(conn, world, edges)
    request = {
        "members": [
            {
                "datastream_id": world[label],
                "measures": [{"canonical_field_id": world["measures"][measure]}],
            }
            for label, measure in labels
        ],
        "edges": [
            {
                "left": world[left],
                "right": world[right],
                "common_key_version_id": world["key_version_id"],
                "relationship_name": f"{left}_to_{right}",
            }
            for left, right in edges
        ],
        "inclusion_policy": "matched_only",
        "primary_datastream_id": world["spend"],
    }
    compiled = plans.compile_plan(
        conn,
        project_id=world["project_id"],
        request=request,
        profile_lookup=_profile_lookup(conn, world),
    )
    stored = plans.store_plan_version(
        conn,
        org_id=world["org_id"],
        project_id=world["project_id"],
        compiled=compiled,
        actor="tester",
    )
    result = execution.execute_plan(
        conn,
        org_id=world["org_id"],
        project_id=world["project_id"],
        query_spec_version_id=stored["query_spec_version_id"],
        actor="tester",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rows_chunk, manifest FROM app.query_result_payloads WHERE result_id = %s",
            (result["result_id"],),
        )
        rows, manifest = cur.fetchone()
    return compiled, result, rows, manifest


def _totals(rows, world, measures):
    return {
        measure: sum(row[f"m_{world['measures'][measure]}"] or 0 for row in rows)
        for measure in measures
    }


def test_a_three_source_chain_keeps_every_source_total(live_postgres, world):
    """Multi-hop: spend -> conversions -> viewability, rooted at spend."""
    measures = ("spend", "revenue", "viewable")
    compiled, result, rows, manifest = _run(
        live_postgres,
        world,
        labels=(("spend", "spend"), ("conversions", "revenue"), ("viewability", "viewable")),
        edges=(("spend", "conversions"), ("conversions", "viewability")),
    )

    assert result["outcome"] == "success"
    assert len(compiled["plan"]["members"]) == 3
    assert len(compiled["plan"]["edges"]) == 2
    assert manifest["sql_shape"] == "aggregate_then_merge"

    # matched_only over a chain: only the keys ALL three sources carry.
    by_campaign = {row[f"k_{world['campaign']}"]: row for row in rows}
    assert set(by_campaign) == {"A", "B"}
    # Each source's own total, unchanged by two hops.
    assert _totals(rows, world, measures) == {
        measure: _MATCHED_TOTALS[measure] for measure in measures
    }
    # The duplicate folded once, not once per hop -- the number a second edge breaks.
    assert by_campaign["A"][f"m_{world['measures']['spend']}"] == 18


def test_a_four_source_star_keeps_every_source_total(live_postgres, world):
    """The declared maximum: four members, three edges, one centre."""
    measures = ("spend", "revenue", "viewable", "brand")
    compiled, result, rows, manifest = _run(
        live_postgres,
        world,
        labels=(
            ("spend", "spend"),
            ("conversions", "revenue"),
            ("viewability", "viewable"),
            ("brand", "brand"),
        ),
        edges=(("spend", "conversions"), ("spend", "viewability"), ("spend", "brand")),
    )

    assert result["outcome"] == "success"
    assert len(compiled["plan"]["members"]) == plans.MAX_MEMBERS
    assert len(compiled["plan"]["edges"]) == plans.MAX_MEMBERS - 1
    assert manifest["sql_shape"] == "aggregate_then_merge"

    by_campaign = {row[f"k_{world['campaign']}"]: row for row in rows}
    assert set(by_campaign) == {"A", "B"}
    assert _totals(rows, world, measures) == {
        measure: _MATCHED_TOTALS[measure] for measure in measures
    }
    # Three edges, and spend is still 18. A tree that multiplied would read 18 x 2
    # or 18 x 4 here and every downstream ratio would be wrong by that factor.
    assert by_campaign["A"][f"m_{world['measures']['spend']}"] == 18


def test_every_edge_of_a_wide_tree_is_profiled_on_its_own_rows(live_postgres, world):
    """No edge inherits another edge's evidence.

    The three edges of the star do NOT measure the same thing: conversions hides a
    NULL campaign, viewability hides an unmatched `Z`, brand hides neither. If one
    profile were reused for all three, two of those three facts would vanish -- and
    the plan would be frozen on evidence that was never taken about it.
    """
    _pin(
        live_postgres,
        world,
        (("spend", "conversions"), ("spend", "viewability"), ("spend", "brand")),
    )
    lookup = _profile_lookup(live_postgres, world)
    seen = {}
    for right in ("conversions", "viewability", "brand"):
        seen[right] = lookup(
            world["spend"],
            world[right],
            world["key_version_id"],
            f"spend_to_{right}",
            world["view_version_id"],
        )

    assert seen["conversions"]["right"]["null_key_rows"] == 1
    assert seen["viewability"]["right"]["null_key_rows"] == 0
    assert seen["viewability"]["matched"]["right_unmatched_keys"] == 1   # Z
    assert seen["brand"]["matched"]["right_unmatched_keys"] == 0
    # The left side is the same source three times, so its duplicate is reported
    # three times -- and each edge still carries its own right-hand evidence.
    assert {profile["left"]["duplicated_keys"] for profile in seen.values()} == {1}


def test_a_fifth_source_is_refused_and_names_the_cap(live_postgres, world):
    """The cap is a refusal with a number in it, not a truncation."""
    _pin(
        live_postgres,
        world,
        (("spend", "conversions"), ("spend", "viewability"), ("spend", "brand")),
    )
    request = {
        "members": [
            {
                "datastream_id": world[label],
                "measures": [{"canonical_field_id": world["measures"][m]}],
            }
            for label, m in (
                ("spend", "spend"),
                ("conversions", "revenue"),
                ("viewability", "viewable"),
                ("brand", "brand"),
            )
        ]
        + [{"datastream_id": world["spend"], "measures": []}],
        "edges": [
            {
                "left": world["spend"],
                "right": world[right],
                "common_key_version_id": world["key_version_id"],
                "relationship_name": f"spend_to_{right}",
            }
            for right in ("conversions", "viewability", "brand")
        ],
        "inclusion_policy": "matched_only",
        "primary_datastream_id": world["spend"],
    }
    with pytest.raises(plans.PlanRefused) as refused:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=request,
            profile_lookup=_profile_lookup(live_postgres, world),
        )
    assert str(plans.MAX_MEMBERS) in refused.value.message
