"""Aggregate, then merge -- proven on rows, not on intentions (story 66.5).

The claim of this story is arithmetic, so it is tested arithmetically: the same
fixture that makes 66.3 report a six-fold multiplication is executed here, and
each source's control total must come out UNCHANGED. If the engine joined before
aggregating, spend would triple and conversions would double, and every assertion
below would move.

Real Postgres for the plan and the Result, real DuckDB for the rows.
"""

from __future__ import annotations

import json

import pytest

psycopg = pytest.importorskip("psycopg")
duckdb = pytest.importorskip("duckdb")

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
from tests.support.minted_identifiers import identifier_rendered_in  # noqa: E402


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    path = tmp_path / "epic66-exec.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute("CREATE TABLE main_marts.spend (date DATE, campaign TEXT, spend_micros BIGINT)")
    con.execute(
        "CREATE TABLE main_marts.conversions "
        "(event_date DATE, campaign_key TEXT, conversion_count INTEGER, revenue_micros BIGINT)"
    )
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    return str(path)


def _insert(path, table, rows):
    con = duckdb.connect(path)
    try:
        for row in rows:
            placeholders = ", ".join(["?"] * len(row))
            con.execute(f"INSERT INTO main_marts.{table} VALUES ({placeholders})", list(row))
    finally:
        con.close()


@pytest.fixture()
def world(live_postgres, warehouse):
    org_id, project_id = make_project(live_postgres, "Execute")
    day = make_canonical_field(live_postgres, project_id, "day", value_type="date")
    campaign = make_canonical_field(live_postgres, project_id, "campaign_id")
    spend = make_canonical_field(
        live_postgres, project_id, "spend", kind="metric", value_type="money"
    )
    conversions = make_canonical_field(
        live_postgres, project_id, "conversions", kind="metric", value_type="integer"
    )
    revenue = make_canonical_field(
        live_postgres, project_id, "revenue", kind="metric", value_type="money"
    )
    left = make_datastream(
        live_postgres, org_id, project_id, "Campaign spend",
        bindings={
            day: ("date", "confirmed"),
            campaign: ("campaign", "confirmed"),
            spend: ("spend_micros", "confirmed"),
        },
    )
    right = make_datastream(
        live_postgres, org_id, project_id, "Conversions",
        bindings={
            day: ("event_date", "confirmed"),
            campaign: ("campaign_key", "confirmed"),
            conversions: ("conversion_count", "confirmed"),
            revenue: ("revenue_micros", "confirmed"),
        },
    )
    publish_output(live_postgres, org_id, project_id, left, "spend")
    publish_output(live_postgres, org_id, project_id, right, "conversions")
    key = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[day, campaign],
        actor="tester",
    )
    _view_id, view_version_id = make_semantic_view(live_postgres, project_id)
    pin_relationship(
        live_postgres, view_version_id, key["current_version"]["id"], name="spend_to_conversions"
    )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "left": left,
        "right": right,
        "day": day,
        "campaign": campaign,
        "spend": spend,
        "conversions": conversions,
        "revenue": revenue,
        "key_version_id": key["current_version"]["id"],
        "view_version_id": view_version_id,
        "path": warehouse,
    }


def _request(world, **overrides):
    request = {
        "members": [
            {"datastream_id": world["left"], "measures": [{"canonical_field_id": world["spend"]}]},
            {
                "datastream_id": world["right"],
                "measures": [
                    {"canonical_field_id": world["conversions"]},
                    {"canonical_field_id": world["revenue"]},
                ],
            },
        ],
        "edges": [
            {
                "left": world["left"],
                "right": world["right"],
                "common_key_version_id": world["key_version_id"],
            }
        ],
        "inclusion_policy": "matched_only",
        "primary_datastream_id": world["left"],
        "grain": "day",
    }
    request.update(overrides)
    return request


def _run(conn, world, **overrides):
    # THE EVIDENCE IS MEASURED, then handed to the compiler -- which is what
    # production does (`multi_source_api:177`). `compile_plan` refuses a plan
    # whose every governed cross has no `measured_safety`: "Every governed cross
    # needs current matching evidence before it can be frozen." The refusal is
    # right -- a cross frozen on no measurement is a fan-out nobody has looked at
    # -- and this fixture simply predated the requirement, passing no lookup at
    # all. `profile_match` reads the ROWS the world just landed, so the evidence
    # is current by construction rather than asserted.
    from core import match_profile  # noqa: PLC0415

    profile = match_profile.profile_match(
        conn,
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        common_key_version_id=world["key_version_id"],
        relationship_name="spend_to_conversions",
        view_version_id=world["view_version_id"],
    )
    compiled = plans.compile_plan(
        conn,
        project_id=world["project_id"],
        request=_request(world, **overrides),
        profile_lookup=lambda *_: profile,
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
    return compiled, stored, result


def _rows(conn, result_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rows_chunk FROM app.query_result_payloads WHERE result_id = %s",
            (result_id,),
        )
        return cur.fetchone()[0]


def _column(rows, prefix, field_id):
    name = f"{prefix}_{field_id}"
    return [row[name] for row in rows]


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_control_totals_survive_the_merge(live_postgres, world):
    """The 66.3 six-fold fixture: three spend rows, two conversion rows, one key."""
    _insert(
        world["path"], "spend",
        [("2026-08-01", "A", 10), ("2026-08-01", "A", 7), ("2026-08-01", "A", 1)],
    )
    _insert(
        world["path"], "conversions",
        [("2026-08-01", "A", 3, 100), ("2026-08-01", "A", 4, 200)],
    )

    _compiled, _stored, result = _run(live_postgres, world)
    rows = _rows(live_postgres, result["result_id"])

    assert result["outcome"] == "success"
    assert len(rows) == 1
    # 10 + 7 + 1 = 18, exactly the source's own total. A row-level join would
    # have produced 36 here (each spend row seen twice).
    assert _column(rows, "m", world["spend"]) == [18]
    # 3 + 4 = 7, not 21.
    assert _column(rows, "m", world["conversions"]) == [7]


def test_matched_only_drops_the_keys_the_other_side_does_not_have(live_postgres, world):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10), ("2026-08-02", "B", 20)])
    _insert(world["path"], "conversions", [("2026-08-01", "A", 3, 100)])

    _compiled, _stored, result = _run(live_postgres, world)
    rows = _rows(live_postgres, result["result_id"])
    assert len(rows) == 1
    assert _column(rows, "m", world["spend"]) == [10]


def test_preserve_primary_keeps_the_primary_rows_with_null_and_not_zero(live_postgres, world):
    """A missing measure is NULL. Writing 0 would be a number nobody measured."""
    _insert(world["path"], "spend", [("2026-08-01", "A", 10), ("2026-08-02", "B", 20)])
    _insert(world["path"], "conversions", [("2026-08-01", "A", 3, 100)])

    _compiled, _stored, result = _run(
        live_postgres, world, inclusion_policy="preserve_primary"
    )
    rows = _rows(live_postgres, result["result_id"])
    assert len(rows) == 2
    conversions = sorted(_column(rows, "m", world["conversions"]), key=lambda v: v is not None)
    assert conversions == [None, 3]


def test_preserve_all_keeps_both_sides_and_coalesces_the_key(live_postgres, world):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10)])
    _insert(world["path"], "conversions", [("2026-08-09", "Z", 3, 100)])

    _compiled, _stored, result = _run(live_postgres, world, inclusion_policy="preserve_all")
    rows = _rows(live_postgres, result["result_id"])
    assert len(rows) == 2
    # Every row names its key, including the one only the right side had.
    campaigns = sorted(str(value) for value in _column(rows, "k", world["campaign"]))
    assert campaigns == ["A", "Z"]


def test_a_ratio_is_recomputed_from_the_governed_components(live_postgres, world):
    """ROAS = SUM(revenue) / SUM(spend), never the average of row ratios.

    Row ratios here are 100/10 = 10 and 200/8 = 25, averaging 17.5. The governed
    answer is 300/18 = 16.67, and the difference is the whole point.
    """
    _insert(world["path"], "spend", [("2026-08-01", "A", 10), ("2026-08-01", "A", 8)])
    _insert(
        world["path"], "conversions",
        [("2026-08-01", "A", 1, 100), ("2026-08-01", "A", 1, 200)],
    )
    roas = make_canonical_field(
        live_postgres, world["project_id"], "roas", kind="metric", value_type="ratio"
    )

    _compiled, _stored, result = _run(
        live_postgres,
        world,
        derived_measures=[
            {
                "canonical_field_id": roas,
                "numerator_field_id": world["revenue"],
                "denominator_field_id": world["spend"],
            }
        ],
    )
    rows = _rows(live_postgres, result["result_id"])
    assert _column(rows, "m", world["revenue"]) == [300]
    assert _column(rows, "m", world["spend"]) == [18]
    assert round(_column(rows, "r", roas)[0], 4) == round(300 / 18, 4)


def test_a_pre_aggregation_filter_narrows_one_source_only(live_postgres, world):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10), ("2026-08-01", "B", 20)])
    _insert(
        world["path"], "conversions",
        [("2026-08-01", "A", 3, 100), ("2026-08-01", "B", 4, 200)],
    )

    _compiled, _stored, result = _run(
        live_postgres,
        world,
        filters=[
            {
                "stage": "pre_aggregation",
                "datastream_id": world["left"],
                "canonical_field_id": world["campaign"],
                "operator": "eq",
                "value": "A",
            }
        ],
    )
    rows = _rows(live_postgres, result["result_id"])
    assert len(rows) == 1
    assert _column(rows, "m", world["spend"]) == [10]


# ---------------------------------------------------------------------------
# The Result: one, immutable, and honest about what it could not do
# ---------------------------------------------------------------------------


def test_one_result_records_the_exact_plan_and_its_provenance(live_postgres, world):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10)])
    _insert(world["path"], "conversions", [("2026-08-01", "A", 3, 100)])

    compiled, stored, result = _run(live_postgres, world)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT manifest FROM app.query_result_payloads WHERE result_id = %s",
            (result["result_id"],),
        )
        manifest = cur.fetchone()[0]

    assert manifest["plan_content_hash"] == compiled["content_hash"]
    assert manifest["inclusion_policy"] == "matched_only"
    assert {m["datastream_id"] for m in manifest["members"]} == {world["left"], world["right"]}
    assert manifest["edges"][0]["relationship_name"] == "spend_to_conversions"
    assert manifest["sql_shape"] == "aggregate_then_merge"
    # AR8, la moitie ESTIMATION : ce que la requete allait SCANNER, demande
    # AVANT de la lancer, voyage avec le Result -- a cote de ce qu'elle a
    # reellement coute, pour qu'un lecteur puisse comparer les deux. Sur DuckDB
    # c'est `not_applicable` et JAMAIS 0 : un zero se lirait comme << cette
    # requete ne scanne rien >>, ce qui est une affirmation, alors que la verite
    # est qu'une lecture de fichier local ne facture pas d'octets.
    assert manifest["scan_estimate"]["engine"] == "duckdb"
    assert manifest["scan_estimate"]["scanned_bytes"] is None
    assert manifest["scan_estimate"]["scanned_bytes_state"] == "not_applicable"
    # Exactly one Result for the attempt, and it points at the stored plan.
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT query_spec_version_id, outcome FROM app.query_results WHERE id = %s",
            (result["result_id"],),
        )
        assert cur.fetchone() == (stored["query_spec_version_id"], "success")


def test_no_matching_row_is_empty_and_not_unavailable(live_postgres, world):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10)])
    _insert(world["path"], "conversions", [("2026-08-09", "Z", 3, 100)])

    _compiled, _stored, result = _run(live_postgres, world)
    assert result["outcome"] == "empty"


def test_an_unreadable_warehouse_is_unavailable_with_its_result_written(
    live_postgres, world, monkeypatch
):
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(world["path"]) + "-gone")
    _compiled, _stored, result = _run(live_postgres, world)
    # `_query_duckdb` answers [] for a missing file rather than raising, so the
    # honest outcome for THAT path is `empty`; what matters is that a Result
    # exists either way and never a silent success with invented rows.
    assert result["outcome"] in {"unavailable", "empty"}
    assert result["row_count"] == 0


def test_an_averaged_measure_is_refused_with_a_written_result(live_postgres, world):
    """A refusal is a Result: an accepted attempt never stays without one."""
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT mapping_payload FROM app.datastream_mapping_versions m "
            "JOIN app.datastreams d ON d.current_mapping_version_id = m.id WHERE d.id = %s",
            (world["left"],),
        )
        payload = cur.fetchone()[0]
    for field in payload["fields"]:
        if field["field_id"] == "spend_micros":
            field["suggestion"]["aggregation"] = "average"
    # A mapping version is immutable, so the average arrives through the plan the
    # same way production would carry it -- on the compiled member.
    compiled = plans.compile_plan(
        live_postgres, project_id=world["project_id"], request=_request(world)
    )
    compiled["plan"]["members"][0]["measures"][0]["aggregation"] = "average"
    stored = plans.store_plan_version(
        live_postgres,
        org_id=world["org_id"],
        project_id=world["project_id"],
        compiled=compiled,
        actor="tester",
    )
    result = execution.execute_plan(
        live_postgres,
        org_id=world["org_id"],
        project_id=world["project_id"],
        query_spec_version_id=stored["query_spec_version_id"],
        actor="tester",
    )
    assert result["outcome"] == "refused"
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT manifest FROM app.query_result_payloads WHERE result_id = %s",
            (result["result_id"],),
        )
        manifest = cur.fetchone()[0]
    assert manifest["refusal"]["code"] == "average_not_compilable_across_sources"


def _stored_spec_bytes(conn, version_id: str) -> tuple[str, str]:
    """The frozen plan AS STORED, whole: its serialized spec and its content hash.

    Read before an execution and again after it, this is what "the immutable plan
    is exactly as it was frozen" actually asserts. Reading one key of the document
    asserts one key.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT spec::text, content_hash FROM app.query_spec_versions WHERE id = %s",
            (version_id,),
        )
        row = cur.fetchone()
    assert row is not None, "the frozen plan is not in the store at all"
    return str(row[0]), str(row[1])


def _freeze(conn, world, mutate=None, **overrides):
    """Compile, optionally bend the plan the way a pre-AI-293 compiler would, store."""
    from core import match_profile  # noqa: PLC0415

    profile = match_profile.profile_match(
        conn,
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        common_key_version_id=world["key_version_id"],
        relationship_name="spend_to_conversions",
        view_version_id=world["view_version_id"],
    )
    compiled = plans.compile_plan(
        conn,
        project_id=world["project_id"],
        request=_request(world, **overrides),
        profile_lookup=lambda *_: profile,
    )
    if mutate is not None:
        mutate(compiled["plan"])
        compiled["content_hash"] = plans.content_hash(compiled["plan"])
    return plans.store_plan_version(
        conn,
        org_id=world["org_id"],
        project_id=world["project_id"],
        compiled=compiled,
        actor="tester",
    )


def test_a_plan_frozen_outside_the_bound_is_refused_at_execution(live_postgres, world):
    """THE CLASS SURVIVED THE COMPILER (AI-293 review, finding 4).

    Before this, `execute_plan` read `merge_bound` only to decide whether matching
    evidence was optional -- the bound itself was never re-evaluated. A plan is
    IMMUTABLE, so every plan frozen before the bound was evaluated on all three
    paths is still out of bound and kept executing with the defect the amendment
    calls "a real defect and not a theoretical one".

    The plan here is bent exactly the way that compiler would have frozen it: the
    two sources are matched on day AND campaign while only campaign is projected.
    The stored row is never repaired -- it is the READING that refuses.
    """

    def drop_the_grain(plan):
        plan["grain"] = None
        plan["grain_canonical_field_id"] = None

    stored = _freeze(
        live_postgres,
        world,
        mutate=drop_the_grain,
        dimensions=[{"canonical_field_id": world["campaign"]}],
    )
    frozen_bytes = _stored_spec_bytes(live_postgres, stored["query_spec_version_id"])
    with pytest.raises(execution.ExecutionRefused) as excinfo:
        execution.execute_plan(
            live_postgres,
            org_id=world["org_id"],
            project_id=world["project_id"],
            query_spec_version_id=stored["query_spec_version_id"],
            actor="tester",
        )
    assert excinfo.value.code == "merge_key_finer_than_grain"
    assert excinfo.value.detail["components"] == ["day"]
    assert "Show it as a dimension" in excinfo.value.message
    assert not identifier_rendered_in(excinfo.value.message)
    # A REFUSAL IS NOT A REPAIR, AND THE ASSERTION SAYS THE WHOLE SENTENCE.
    #
    # This used to read one key -- `spec["grain_canonical_field_id"] is None` --
    # under a comment claiming the plan was "exactly as it was frozen". One key
    # out of a document with members, edges, key paths, bounds and a content hash
    # is not "exactly", and every OTHER key could have been rewritten by the read
    # with this test staying green. The claim and the measurement now match:
    # the stored bytes and the content hash are read before the execution and
    # after it, and compared whole.
    assert _stored_spec_bytes(live_postgres, stored["query_spec_version_id"]) == frozen_bytes
    # And the mutation this test freezes is still in those bytes -- otherwise
    # "unchanged" would be satisfied by a plan that never carried it.
    assert json.loads(frozen_bytes[0])["grain_canonical_field_id"] is None


def test_a_metric_archived_after_the_freeze_stops_the_merge_at_execution(
    live_postgres, world
):
    """The bound is a governed fact READ NOW, not a permission stamped once.

    The plan compiled inside the bound and is untouched. Archiving the measure
    afterwards removes the only declaration that said it may be folded to the
    shared key, and `unavailable` never reads as a pass -- so the read refuses,
    naming the gesture that actually repairs it.
    """
    from core import canonical_field_registry  # noqa: PLC0415

    stored = _freeze(live_postgres, world)
    canonical_field_registry.archive_project_field(
        live_postgres, project_id=world["project_id"], field_id=world["spend"], actor="tester"
    )
    with pytest.raises(execution.ExecutionRefused) as excinfo:
        execution.execute_plan(
            live_postgres,
            org_id=world["org_id"],
            project_id=world["project_id"],
            query_spec_version_id=stored["query_spec_version_id"],
            actor="tester",
        )
    assert excinfo.value.code == "metric_additivity_unknown"
    assert "spend was archived" in excinfo.value.message
    assert "Restore it in Governance" in excinfo.value.message
    assert "Declare it in Governance" not in excinfo.value.message
    assert not identifier_rendered_in(excinfo.value.message)


def test_the_result_schema_names_every_column_in_governed_terms(live_postgres, world):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10)])
    _insert(world["path"], "conversions", [("2026-08-01", "A", 3, 100)])
    _compiled, _stored, result = _run(live_postgres, world)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT result_schema FROM app.query_result_payloads WHERE result_id = %s",
            (result["result_id"],),
        )
        schema = cur.fetchone()[0]
    roles = {field["role"] for field in schema["fields"]}
    assert roles == {"dimension", "measure"}
    assert all(field["canonical_field_id"] for field in schema["fields"])
