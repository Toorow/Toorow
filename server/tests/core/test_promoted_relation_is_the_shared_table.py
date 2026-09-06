"""A published candidate is a promoted one: the relation a reader is handed is the SHARED table.

MEASURED 2026-09-04 ON THE HARNESS PROJECT. A mapping change confirmed through the
workbench minted candidate `dse_01M1P084…`, whose rows landed in
`managed_feed_ds_…__cand_dse_01M1P084…`; `publish-activate` promoted them into
`managed_feed_ds_…` and the isolated table was gone -- and the Output version
recorded the ISOLATED name as `relation_ref`. Every query planned against it
answered `unavailable` on a BigQuery 404 the attempt could not explain. A connector
pull records the shared name (`raw_youtube_daily`), which is how the same path was
green on Kardinal and red on the first managed feed anyone queried.

Two sides, one rule (`raw_landing.promoted_relation`): the writer records the
promoted relation at publish, and the two readers (`query_execution`, the semantic
compiler) normalise an isolated name they still meet on rows written before today.
"""

from __future__ import annotations

from core.raw_landing import candidate_table, promoted_relation


def test_the_promoted_relation_strips_exactly_the_candidate_suffix():
    isolated = candidate_table(
        "managed_feed_ds_01KZEV9T7RGMDRRD7E6JT262K1", "dse_01M1P084GBYE37HX2TZZ2Y5MC7"
    )
    assert isolated.endswith("__cand_dse_01M1P084GBYE37HX2TZZ2Y5MC7")
    assert promoted_relation(isolated) == "managed_feed_ds_01KZEV9T7RGMDRRD7E6JT262K1"


def test_a_shared_or_logical_name_is_left_alone():
    assert promoted_relation("raw_youtube_daily") == "raw_youtube_daily"
    assert (
        promoted_relation("execution/dse_x/candidate/relation")
        == "execution/dse_x/candidate/relation"
    )
    assert promoted_relation("") == ""


def test_a_dataset_qualified_candidate_keeps_its_dataset():
    assert (
        promoted_relation("raw_proj_x.managed_feed_ds_y__cand_dse_z")
        == "raw_proj_x.managed_feed_ds_y"
    )


def test_the_semantic_compiler_reads_the_promoted_relation(monkeypatch):
    from core import semantic_model

    monkeypatch.setattr(
        semantic_model,
        "_fetch",
        lambda *a, **k: [{"relation_ref": "managed_feed_ds_y__cand_dse_z"}],
    )
    assert (
        semantic_model._published_relation(object(), "proj_x", "ds_y", "dmap_x")
        == "managed_feed_ds_y"
    )


def test_a_managed_feed_landing_is_read_from_the_raw_dataset(monkeypatch):
    """The second half of the same 2026-09-04 measurement: once the promoted name
    resolved, `_dataset_for` sent `managed_feed_ds_…` to the MARTS dataset and
    BigQuery answered 404 again. `managed_feed_ledger` already calls it a raw
    landing, "never a mart"."""
    from core import query_execution, warehouse_tenancy

    monkeypatch.setattr(warehouse_tenancy, "bigquery_raw_dataset", lambda p: f"raw_{p}")
    monkeypatch.setattr(warehouse_tenancy, "bigquery_marts_dataset", lambda p: f"marts_{p}")
    assert query_execution._dataset_for("proj_x", "managed_feed_ds_y") == "raw_proj_x"
    assert query_execution._dataset_for("proj_x", "raw_youtube_daily") == "raw_proj_x"
    assert query_execution._dataset_for("proj_x", "fact_daily_kpi") == "marts_proj_x"
    assert query_execution._dataset_for("proj_x", "already.qualified") == ""
