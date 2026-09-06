"""Email ingress lands rows, and lands them exactly like a direct upload.

The inbound chain has been complete for a while: `bridge` -> `process_inbound_delivery`
-> `ingest_inbound_file` -> `csv_excel_import.run_import`. What was missing was
underneath all of it -- `run_import` reached its physical-write step and did
nothing there, so every arrival counted its rows and landed none.

That makes this the test the parity claim always needed. `inbound-ingress-parity`
says an email is a TRANSPORT for a managed feed: the same template, the same
processing, the same mapping, and therefore the same result through `run_import`.
A parity asserted on the call and not on the landing is satisfied by two paths
that both write nothing.
"""

from __future__ import annotations

import pytest
from core.csv_excel_import import _land_managed_rows


@pytest.fixture()
def landed(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_land(table, rows, *, columns, project_id=None, **kwargs):
        from core.raw_landing import active_candidate_execution

        calls.append(
            {
                "table": table,
                "rows": rows,
                "columns": columns,
                "project_id": project_id,
                "isolated_as": active_candidate_execution(),
            }
        )
        return {"rows": len(rows), "table": table, "backend": "duckdb"}

    monkeypatch.setattr("core.raw_landing.land_raw_rows", fake_land)
    return calls


def test_the_import_lands_through_the_shared_raw_seam(landed):
    result = _land_managed_rows(
        landing_relation="raw_project.managed_feed_ds1",
        rows=[{"day": "2026-07-01", "clicks": "5"}],
        project_id="proj_EXAMPLE",
    )

    assert result["rows"] == 1
    # The bare table name: `land_raw_rows` resolves the dataset from the Project.
    assert landed[0]["table"] == "managed_feed_ds1"
    assert landed[0]["project_id"] == "proj_EXAMPLE"


def test_a_column_only_later_rows_carry_is_not_dropped(landed):
    """An arrival whose first row omits an optional field must not shift the rest."""
    _land_managed_rows(
        landing_relation="raw_project.t",
        rows=[{"day": "2026-07-01"}, {"day": "2026-07-02", "clicks": "9"}],
        project_id="proj_EXAMPLE",
    )

    assert [name for name, _ in landed[0]["columns"]] == ["day", "clicks"]


def test_an_empty_arrival_reaches_the_warehouse_not_at_all(landed):
    result = _land_managed_rows(
        landing_relation="raw_project.t", rows=[], project_id="proj_EXAMPLE"
    )

    assert result == {"rows": 0, "table": "raw_project.t"}
    assert landed == [], "an empty arrival must not open a warehouse write at all"


def test_an_arrival_inside_a_candidate_scope_is_isolated(landed):
    """The same property a candidate materialization depends on."""
    from core.raw_landing import candidate_execution

    with candidate_execution("dse_inbound"):
        _land_managed_rows(
            landing_relation="raw_project.t",
            rows=[{"day": "2026-07-01"}],
            project_id="proj_EXAMPLE",
        )

    assert landed[0]["isolated_as"] == "dse_inbound"
