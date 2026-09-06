"""Tests for Story 27.5 -- actual x forecast alignment matrix (Epic 27).

Offline (no DB, no warehouse): the PURE reducer build_alignment_matrix (conformed axis,
AD-9 unconformed distinct, pacing=None on planned==0, no inter-axis total), the Python
ventilation _ventilate_actuals (re-sums to spend EXACTLY, ignores orphaned), and the
injection-driven build_plan_actual_alignment (fake conform_fn / campaign_spend_fn,
WarehouseUnavailable propagation).

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): a confirmed dimension mapping
(27.4 conform_value real, project_id passed -> PROJECT override visible) drives the axis;
an unconformed value stays source-named; and a COUNT proves the matrix is a pure read.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

from tests.conftest import purge_fixture_project

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import plan_actual_alignment as paa  # noqa: E402

# ===========================================================================
# Offline -- build_alignment_matrix (PURE)
# ===========================================================================


def _conform_map(mapping):
    """A conform_fn from a {(connector, source_value): canonical} dict; None otherwise."""
    return lambda connector, source_value: mapping.get((connector, source_value))


def _axis_map(mapping):
    return lambda line_key: mapping.get(line_key)


def test_matrix_conformed_cell():
    """Sec.13: forecast+actual on a conformed axis -> one cell, planned/actual/delta/pacing."""
    planned = {"l1": "100"}
    ventilated = [
        {"connector": "c1", "campaign_ref": "camp-A", "line_key": "l1", "amount": Decimal("80")}
    ]
    conform = _conform_map({("c1", "camp-A"): "Placement X"})
    axis = _axis_map({"l1": "Placement X"})
    out = paa.build_alignment_matrix(planned, ventilated, conform, axis)
    assert len(out["cells"]) == 1
    cell = out["cells"][0]
    assert cell["axis_value"] == "Placement X"
    assert cell["conformed"] is True
    assert cell["planned"] == 100.0
    assert cell["actual"] == 80.0
    assert cell["delta"] == -20.0
    assert cell["pacing"] == pytest.approx(0.8)
    assert out["unconformed_count"] == 0


def test_pacing_none_when_planned_zero():
    """Sec.14: planned==0 -> pacing=None (no /0); a 100%-actual axis is visible."""
    ventilated = [
        {"connector": "c1", "campaign_ref": "camp-A", "line_key": "l1", "amount": Decimal("50")}
    ]
    conform = _conform_map({("c1", "camp-A"): "Placement X"})
    out = paa.build_alignment_matrix({}, ventilated, conform, _axis_map({}))
    assert len(out["cells"]) == 1
    cell = out["cells"][0]
    assert cell["planned"] == 0.0
    assert cell["actual"] == 50.0
    assert cell["pacing"] is None


def test_unconformed_value_stays_source_named_and_distinct():
    """Sec.15: AD-9 -- an unconformed actual (conform_fn -> None) stays under its source
    name, conformed=false, NEVER fused with a canonical homonym (distinct (axis, conformed) key)."""
    planned = {"l1": "100"}
    ventilated = [
        # This one conforms to the SAME string as the plan axis.
        {"connector": "c1", "campaign_ref": "camp-A", "line_key": "l1", "amount": Decimal("30")},
        # This one does NOT conform -> stays as raw campaign_ref, which HAPPENS to equal
        # the canonical string "Summer" -> must remain a DISTINCT cell (conformed=false).
        {"connector": "c2", "campaign_ref": "Summer", "line_key": "l1", "amount": Decimal("20")},
    ]
    conform = _conform_map({("c1", "camp-A"): "Summer"})  # c2/Summer -> None
    axis = _axis_map({"l1": "Summer"})
    out = paa.build_alignment_matrix(planned, ventilated, conform, axis)
    # Two cells: ("Summer", True) with planned 100 + actual 30, and ("Summer", False) actual 20.
    conformed_cell = [c for c in out["cells"] if c["conformed"]][0]
    unconformed_cell = [c for c in out["cells"] if not c["conformed"]][0]
    assert conformed_cell["axis_value"] == "Summer"
    assert conformed_cell["planned"] == 100.0
    assert conformed_cell["actual"] == 30.0
    assert unconformed_cell["axis_value"] == "Summer"
    assert unconformed_cell["actual"] == 20.0
    assert out["unconformed_count"] == 1


def test_no_inter_axis_total_in_contract():
    """Sec.18: no 'total' field summing heterogeneous axes (structural assertion)."""
    ventilated = [
        {"connector": "c1", "campaign_ref": "a", "line_key": "l1", "amount": Decimal("10")},
        {"connector": "c1", "campaign_ref": "b", "line_key": "l2", "amount": Decimal("20")},
    ]
    conform = _conform_map({("c1", "a"): "AxisA", ("c1", "b"): "AxisB"})
    out = paa.build_alignment_matrix({}, ventilated, conform, _axis_map({}))
    assert "total" not in out
    assert set(out) == {"cells", "unconformed_count"}


def test_matrix_determinism():
    """Sec.19: two calls -> identical cells in stable order."""
    ventilated = [
        {"connector": "c1", "campaign_ref": "b", "line_key": "l2", "amount": Decimal("20")},
        {"connector": "c1", "campaign_ref": "a", "line_key": "l1", "amount": Decimal("10")},
    ]
    conform = _conform_map({("c1", "a"): "Zeta", ("c1", "b"): "Alpha"})
    a = paa.build_alignment_matrix({}, ventilated, conform, _axis_map({}))
    b = paa.build_alignment_matrix({}, ventilated, conform, _axis_map({}))
    assert a == b
    assert [c["axis_value"] for c in a["cells"]] == ["Alpha", "Zeta"]


# ===========================================================================
# Offline -- _ventilate_actuals (PURE)
# ===========================================================================


def test_ventilate_shared_campaign_sums_to_spend_exactly():
    """Sec.16: a campaign shared 0.5/0.5 by 2 lines -> each 50%, Sum == spend EXACTLY."""
    mappings = {
        "plan_id": "p1",
        "lines": [
            {"line_key": "l1", "mappings": [
                {"connector": "c1", "campaign_ref": "camp-A",
                 "split_weight": "0.500000", "status": "active"}]},
            {"line_key": "l2", "mappings": [
                {"connector": "c1", "campaign_ref": "camp-A",
                 "split_weight": "0.500000", "status": "active"}]},
        ],
    }
    spend = [{"connector": "c1", "campaign_ref": "camp-A", "spend": 100.0}]
    out = paa._ventilate_actuals(mappings, spend)
    assert len(out) == 2
    amounts = [r["amount"] for r in out]
    assert all(isinstance(a, Decimal) for a in amounts)
    assert sum(amounts) == Decimal("100.0")  # exact, zero tolerance
    assert amounts[0] == Decimal("50.0")


def test_ventilate_three_way_remainder_sums_exactly():
    """1/3 split (0.333334/0.333333/0.333333) re-sums to the spend EXACTLY."""
    mappings = {
        "plan_id": "p1",
        "lines": [
            {"line_key": k, "mappings": [
                {"connector": "c1", "campaign_ref": "camp-A",
                 "split_weight": w, "status": "active"}]}
            for k, w in [("a", "0.333334"), ("b", "0.333333"), ("c", "0.333333")]
        ],
    }
    spend = [{"connector": "c1", "campaign_ref": "camp-A", "spend": 90.0}]
    out = paa._ventilate_actuals(mappings, spend)
    assert sum(r["amount"] for r in out) == Decimal("90.0") * Decimal("1.000000")


def test_ventilate_ignores_orphaned():
    """Sec.17: status='orphaned' mappings do not ventilate (22.3 rule)."""
    mappings = {
        "plan_id": "p1",
        "lines": [
            {"line_key": "l1", "mappings": [
                {"connector": "c1", "campaign_ref": "camp-A",
                 "split_weight": "1.000000", "status": "orphaned"}]},
        ],
    }
    spend = [{"connector": "c1", "campaign_ref": "camp-A", "spend": 100.0}]
    out = paa._ventilate_actuals(mappings, spend)
    assert out == []


def test_ventilate_mapped_campaign_without_spend_skipped():
    """A mapped campaign with no spend in the window contributes nothing."""
    mappings = {
        "plan_id": "p1",
        "lines": [
            {"line_key": "l1", "mappings": [
                {"connector": "c1", "campaign_ref": "ghost",
                 "split_weight": "1.000000", "status": "active"}]},
        ],
    }
    out = paa._ventilate_actuals(mappings, [])
    assert out == []


def test_ventilate_disjoint_window_lines_use_window_total_not_mart_daily():
    """F-1: 2 disjoint-window lines sharing a campaign -> v1 attributes window-TOTAL x weight.

    Freezes the documented fidelity boundary vs plan_vs_actual_daily.sql: v1 ventilates the
    WINDOW-TOTAL spend by split_weight, IGNORING each line's flight window. So a line that was
    only live early still gets its weighted share of spend that occurred later (and vice
    versa). The mart would instead bound each day's spend to the flight window before
    splitting. Only the per-campaign TOTAL is faithful here; the per-line split between the
    two disjoint-window lines is the window-total approximation. Spend is asymmetric across
    the two windows (early=30, late=70), yet v1 splits the total 100 by weight (0.4/0.6),
    NOT by which window the spend fell in -- hard values freeze the real v1 behaviour.
    """
    mappings = {
        "plan_id": "p1",
        "lines": [
            # line_early: flight window Jan 1-10 (disjoint from line_late), weight 0.4
            {"line_key": "line_early", "mappings": [
                {"connector": "c1", "campaign_ref": "camp-A",
                 "split_weight": "0.400000", "status": "active"}]},
            # line_late: flight window Jan 20-31 (disjoint from line_early), weight 0.6
            {"line_key": "line_late", "mappings": [
                {"connector": "c1", "campaign_ref": "camp-A",
                 "split_weight": "0.600000", "status": "active"}]},
        ],
    }
    # Window-total spend = 100 (asymmetric in reality: 30 early + 70 late), but v1 only sees
    # the total from query_campaign_spend.
    spend = [{"connector": "c1", "campaign_ref": "camp-A", "spend": 100.0}]
    out = paa._ventilate_actuals(mappings, spend)

    by_line = {r["line_key"]: r["amount"] for r in out}
    # v1: total x weight, NOT the mart's day-bounded figure (which would be 30 / 70).
    assert by_line["line_early"] == Decimal("40.0")  # 100 x 0.4 (NOT 30, the mart daily)
    assert by_line["line_late"] == Decimal("60.0")   # 100 x 0.6 (NOT 70, the mart daily)
    # Only the per-campaign TOTAL is faithful (re-sums to spend EXACTLY, zero tolerance).
    assert sum(by_line.values()) == Decimal("100.0")


# ===========================================================================
# Offline -- build_plan_actual_alignment orchestration (injection)
# ===========================================================================


class _FakeCursor:
    def __init__(self, rows=None):
        self._rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        return None

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor()


def _patch(monkeypatch, *, plan, mappings):
    import core.db as db
    import core.mediaplan_mapping as mm
    import core.mediaplan_store as store

    monkeypatch.setattr(store, "get_plan", lambda conn, *, plan_id: plan)
    monkeypatch.setattr(mm, "list_mappings", lambda conn, *, plan_id: mappings)
    monkeypatch.setattr(db, "get_connection", lambda: _FakeConn())


def _line(line_key, label, budget="100.00"):
    return {
        "line_key": line_key,
        "label": label,
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "budget": budget,
    }


def test_build_alignment_full_contract(monkeypatch):
    """Sec.24: injected conform_fn/campaign_spend_fn -> the full JSON contract."""
    plan = {"id": "p1", "project_id": "proj-1", "lines": [_line("l1", "Placement X", "100.00")]}
    mappings = {
        "plan_id": "p1",
        "lines": [
            {"line_key": "l1", "mappings": [
                {"connector": "c1", "campaign_ref": "camp-A",
                 "split_weight": "1.000000", "status": "active"}]},
        ],
    }
    _patch(monkeypatch, plan=plan, mappings=mappings)

    def _spend_fn(project_id, start, end):
        return [{"connector": "c1", "campaign_ref": "camp-A", "spend": 80.0}]

    def _conform_fn(connector, source_value):
        return "Placement X" if (connector, source_value) == ("c1", "camp-A") else None

    out = paa.build_plan_actual_alignment(
        "p1", dimension="placement", org_id="org-1",
        campaign_spend_fn=_spend_fn, conform_fn=_conform_fn,
    )
    assert out["plan_id"] == "p1"
    assert out["dimension"] == "placement"
    assert out["org_id"] == "org-1"
    assert out["window"] == {"start": "2026-03-01", "end": "2026-03-31"}
    assert len(out["cells"]) == 1
    cell = out["cells"][0]
    assert cell["axis_value"] == "Placement X"
    assert cell["planned"] == 100.0
    assert cell["actual"] == 80.0
    assert cell["conformed"] is True
    assert out["unconformed_count"] == 0
    # D-4: the ventilation assiette is exposed top-level + as an FR note.
    assert out["ventilation_basis"] == paa.VENTILATION_BASIS == "window_total"
    assert any("window_total" in n for n in out["notes"])


def test_build_alignment_warehouse_unavailable_propagates(monkeypatch):
    """Sec.24: WarehouseUnavailable propagates (no fake empty perimeter)."""
    from core.warehouse import WarehouseUnavailable

    plan = {"id": "p1", "project_id": "proj-1", "lines": [_line("l1", "Placement X")]}
    _patch(monkeypatch, plan=plan, mappings={"plan_id": "p1", "lines": []})

    def _spend_fn(*a):
        raise WarehouseUnavailable("down")

    with pytest.raises(WarehouseUnavailable):
        paa.build_plan_actual_alignment(
            "p1", dimension="placement", org_id="org-1",
            campaign_spend_fn=_spend_fn, conform_fn=lambda c, s: None,
        )


def test_build_alignment_unconformed_axis(monkeypatch):
    """An actual whose value never conforms stays source-named, conformed=false, counted."""
    plan = {"id": "p1", "project_id": "proj-1", "lines": [_line("l1", "Placement X", "100.00")]}
    mappings = {
        "plan_id": "p1",
        "lines": [
            {"line_key": "l1", "mappings": [
                {"connector": "c1", "campaign_ref": "raw-camp",
                 "split_weight": "1.000000", "status": "active"}]},
        ],
    }
    _patch(monkeypatch, plan=plan, mappings=mappings)

    out = paa.build_plan_actual_alignment(
        "p1", dimension="placement", org_id="org-1",
        campaign_spend_fn=lambda p, s, e: [
            {"connector": "c1", "campaign_ref": "raw-camp", "spend": 40.0}],
        conform_fn=lambda c, s: None,  # nothing conforms
    )
    unconformed = [c for c in out["cells"] if not c["conformed"]]
    assert len(unconformed) == 1
    assert unconformed[0]["axis_value"] == "raw-camp"
    assert out["unconformed_count"] == 1


def test_build_alignment_unknown_plan(monkeypatch):
    """Unknown plan -> empty contract with an honest note, no crash."""
    import core.db as db
    import core.mediaplan_store as store

    monkeypatch.setattr(store, "get_plan", lambda conn, *, plan_id: None)
    monkeypatch.setattr(db, "get_connection", lambda: _FakeConn())
    out = paa.build_plan_actual_alignment("nope", dimension="placement", org_id="org-1")
    assert out["cells"] == []
    assert out["window"] is None
    assert out["notes"] == ["unknown plan"]


# ===========================================================================
# Offline -- AI-260: the client value table reaches this served read
# ===========================================================================


def _two_campaign_setup(monkeypatch):
    plan = {"id": "p1", "project_id": "proj-1", "lines": [_line("l1", "Placement X", "100.00")]}
    mappings = {
        "plan_id": "p1",
        "lines": [
            {"line_key": "l1", "mappings": [
                {"connector": "c1", "campaign_ref": "camp-A",
                 "split_weight": "0.500000", "status": "active"},
                {"connector": "c1", "campaign_ref": "camp-B",
                 "split_weight": "0.500000", "status": "active"}]},
        ],
    }
    _patch(monkeypatch, plan=plan, mappings=mappings)

    def _spend_fn(project_id, start, end):
        return [
            {"connector": "c1", "campaign_ref": "camp-A", "spend": 80.0},
            {"connector": "c1", "campaign_ref": "camp-B", "spend": 20.0},
        ]

    return _spend_fn


def _resolution(state, **overrides):
    from core import value_table_resolution as vtr

    base = {
        "state": state,
        "project_id": "proj-1",
        "connector": "c1",
        "source_field": paa.ACTUAL_SOURCE_FIELD,
    }
    base.update(overrides)
    return vtr.ValueTableResolution(**base)


def test_value_table_wins_on_its_field_and_does_not_merge_with_052(monkeypatch):
    """AI_238_PRECEDENCE, applied on a served read. camp-A is named by the
    client's table and takes ITS canonical -- not 052's, which answers something
    else for the same pair. camp-B is NOT named by the table: it stays
    source-named even though 052 would conform it, because on the assigned field
    the precedence is a precedence and not a merge."""
    spend_fn = _two_campaign_setup(monkeypatch)
    seen = {}

    def _resolution_fn(conn, *, project_id, connector, source_field):
        seen["key"] = (project_id, connector, source_field)
        return _resolution("applied", table_id="vmt_1", table_name="Client vocabulary",
                           pairs={"camp-A": "Placement X"})

    out = paa.build_plan_actual_alignment(
        "p1", dimension="placement", org_id="org-1",
        campaign_spend_fn=spend_fn,
        conform_fn=lambda c, s: "From 052",  # 052 would answer for BOTH campaigns
        value_table_resolution_fn=_resolution_fn,
    )
    # The bridge was asked with the fact's own key and the read's exact field.
    assert seen["key"] == ("proj-1", "c1", "campaign_id")
    conformed = {c["axis_value"] for c in out["cells"] if c["conformed"]}
    assert "Placement X" in conformed  # the table's canonical, plan+actual fused
    assert "From 052" not in conformed  # 052 never answered on the assigned field
    source_named = [c for c in out["cells"] if not c["conformed"]]
    assert [c["axis_value"] for c in source_named] == ["camp-B"]
    assert out["unconformed_count"] == 1
    assert [entry["state"] for entry in out["value_tables"]] == ["applied"]
    assert out["value_tables"][0]["table_name"] == "Client vocabulary"


def test_value_table_ambiguity_is_a_readable_state_never_a_pick(monkeypatch):
    """Two Datastreams of one connector disagreeing -> the values stay
    source-named, the disagreement is NAMED in value_tables and notes, and
    nothing raises. Never a 500, never a silent pick -- not even 052's."""
    from core import value_table_resolution as vtr

    spend_fn = _two_campaign_setup(monkeypatch)
    ambiguous = _resolution(
        "ambiguous",
        reason=vtr.REASON_TABLES_DISAGREE,
        datastreams=(
            {"datastream_id": "ds_1", "datastream_name": "One",
             "table_id": "vmt_1", "table_name": "Vocabulary A"},
            {"datastream_id": "ds_2", "datastream_name": "Two",
             "table_id": "vmt_2", "table_name": "Vocabulary B"},
        ),
    )
    out = paa.build_plan_actual_alignment(
        "p1", dimension="placement", org_id="org-1",
        campaign_spend_fn=spend_fn,
        conform_fn=lambda c, s: "From 052",  # would pick a store: must not run
        value_table_resolution_fn=lambda conn, **_: ambiguous,
    )
    assert all(not c["conformed"] for c in out["cells"] if c["actual"] > 0)
    assert out["value_tables"][0]["state"] == "ambiguous"
    named = {(d["datastream_id"], d["table_id"])
             for d in out["value_tables"][0]["datastreams"]}
    assert named == {("ds_1", "vmt_1"), ("ds_2", "vmt_2")}
    ambiguity_notes = [n for n in out["notes"] if "ambiguity" in n]
    assert len(ambiguity_notes) == 1
    assert "never a pick" in ambiguity_notes[0]


def test_no_value_table_leaves_the_governed_store_in_charge(monkeypatch):
    """state='none' -> exactly the pre-AI-260 behaviour: 052 conforms, and the
    contract's value_tables list is empty (no table touched this read)."""
    spend_fn = _two_campaign_setup(monkeypatch)
    out = paa.build_plan_actual_alignment(
        "p1", dimension="placement", org_id="org-1",
        campaign_spend_fn=spend_fn,
        conform_fn=lambda c, s: "Placement X",
        value_table_resolution_fn=lambda conn, **_: _resolution(
            "none", reason="no_assignment_on_field"),
    )
    assert all(c["conformed"] for c in out["cells"])
    assert out["value_tables"] == []
    assert not any("value table" in n for n in out["notes"])


def test_an_unavailable_resolution_falls_back_to_052_and_says_so(monkeypatch):
    """An outage is not 'no table': 052 still answers (suppressing it over a
    question that could not be asked would degrade the read on unknowable
    information), and the outage is NAMED in the contract."""
    spend_fn = _two_campaign_setup(monkeypatch)
    out = paa.build_plan_actual_alignment(
        "p1", dimension="placement", org_id="org-1",
        campaign_spend_fn=spend_fn,
        conform_fn=lambda c, s: "Placement X",
        value_table_resolution_fn=lambda conn, **_: _resolution(
            "unavailable", reason="store unreadable: RuntimeError"),
    )
    assert all(c["conformed"] for c in out["cells"])
    assert out["value_tables"][0]["state"] == "unavailable"
    assert any("unavailable" in n for n in out["notes"])


# ===========================================================================
# Live-Postgres (opt-in)
# ===========================================================================


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


def _connect():
    import psycopg

    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"])


def _seed_org_project(conn) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_27_5_{suffix}"
    project_id = f"proj-27-5-{suffix}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.organizations (id, name, slug, created_by)
            VALUES (%s, %s, %s, 'system')
            """,
            (org_id, "Bridge Org", f"bridge-org-{suffix}"),
        )
        cur.execute(
            """
            INSERT INTO app.projects (id, org_id, name, slug, status, created_by)
            VALUES (%s, %s, %s, %s, 'active', 'system')
            """,
            (project_id, org_id, "Bridge Test", project_id),
        )
    conn.commit()
    return org_id, project_id


def _drop(conn, org_id: str, project_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE app.media_plan_versions DISABLE TRIGGER USER")
        # Same fixture-only bypass as the versions line above: guard triggers
        # (rightly) refuse deleting a published version's allocations and an
        # append-only audit, and this teardown erases the whole fixture project.
        # The audit's RGPD hatch (098) cannot be used instead: the live function
        # body is 049's unconditional one where 049 was applied after 098.
        cur.execute("ALTER TABLE app.plan_allocation_daily DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.metric_semantics_audit DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.audit_log DISABLE TRIGGER USER")
        try:  # noqa: SIM105 -- the except ROLLS BACK, see below
            cur.execute(
                "DELETE FROM app.plan_line_mappings WHERE plan_id IN "
                "(SELECT id FROM app.media_plans WHERE project_id = %s)",
                (project_id,),
            )
            # mediaplan_store materialises a daily allocation per line
            # (mediaplan_store.py:547, migration 040 FK ON DELETE RESTRICT), so
            # the allocations go before the lines they reference. Measured
            # red on 2026-08-17 on pre-AI-260 code: this teardown, not the read.
            cur.execute(
                """
                DELETE FROM app.plan_allocation_daily WHERE line_id IN (
                    SELECT l.id FROM app.media_plan_lines l
                    JOIN app.media_plan_versions v ON v.id = l.version_id
                    JOIN app.media_plans p ON p.id = v.plan_id WHERE p.project_id = %s
                )
                """,
                (project_id,),
            )
            cur.execute(
                """
                DELETE FROM app.media_plan_lines WHERE version_id IN (
                    SELECT v.id FROM app.media_plan_versions v
                    JOIN app.media_plans p ON p.id = v.plan_id WHERE p.project_id = %s
                )
                """,
                (project_id,),
            )
            cur.execute(
                "DELETE FROM app.media_plan_versions WHERE plan_id IN "
                "(SELECT id FROM app.media_plans WHERE project_id = %s)",
                (project_id,),
            )
            cur.execute("DELETE FROM app.media_plans WHERE project_id = %s", (project_id,))
            cur.execute(
                "DELETE FROM app.dimension_value_mappings WHERE project_id = %s OR org_id = %s",
                (project_id, org_id),
            )
            cur.execute(
                "DELETE FROM app.metric_semantics_audit WHERE org_id = %s", (org_id,)
            )
            # app.operations holds its audit event in ON DELETE RESTRICT
            # (060:121) and operations/operation_outbox reference EACH OTHER:
            # any suite that ran a governed mutation before this one leaves
            # rows that refuse the global audit delete below (measured
            # 2026-08-18, cross-suite contamination on the shared disposable
            # cluster). Null the outbox pointer, then sweep the cycle.
            cur.execute(
                """
                UPDATE app.operations SET outbox_event_id = NULL
                WHERE audit_event_id IN
                    (SELECT id FROM app.audit_log WHERE identity IN ('tester', 'system'))
                """
            )
            cur.execute(
                """
                DELETE FROM app.operation_outbox ob USING app.operations o
                WHERE ob.operation_id = o.id AND o.audit_event_id IN
                    (SELECT id FROM app.audit_log WHERE identity IN ('tester', 'system'))
                """
            )
            cur.execute(
                """
                DELETE FROM app.operations WHERE audit_event_id IN
                    (SELECT id FROM app.audit_log WHERE identity IN ('tester', 'system'))
                """
            )
            cur.execute("DELETE FROM app.audit_log WHERE identity IN ('tester', 'system')")
            # AI-291: le graphe prend le relais si une table gouvernee
            # ajoutee depuis retient le projet en ON DELETE RESTRICT.
            purge_fixture_project(cur.connection, project_id)
            # The org side has its own governed satellites (mdm_business_domains
            # holds it in ON DELETE RESTRICT): walk the repository's own eraser
            # rather than a hand-written DELETE list, exactly like the 60.1
            # fixture teardown does.
            from tests.conftest import purge_fixture_org

            purge_fixture_org(cur.connection, org_id)
        except Exception:
            # A failed statement leaves this transaction ABORTED: the ENABLE
            # statements a `finally` would issue are refused, the DISABLEs roll
            # back with the transaction anyway, but the ACCESS EXCLUSIVE locks
            # they took are held until rollback -- and an open aborted
            # connection blocks every other teardown on the cluster (measured
            # 2026-08-17: the conftest scrub waited on exactly that for
            # minutes). Roll back, THEN let the failure speak.
            conn.rollback()
            raise
        # The purge leaves deferred trigger events queued, and ALTER TABLE
        # refuses to run while any are pending: fire them now.
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE app.audit_log ENABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.metric_semantics_audit ENABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.plan_allocation_daily ENABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.media_plan_versions ENABLE TRIGGER USER")
    conn.commit()


_LIVE_LINES = [
    {
        "line_key": "social/summer",
        "label": "Placement X",
        "channel": "Social",
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "budget": "10000.00",
    },
]


@pg_available
def test_live_alignment_uses_confirmed_project_override(monkeypatch):
    """Sec.28/Sec.30: a confirmed PROJECT dimension mapping drives the axis (project_id
    passed -> override visible); an unconformed value stays source-named; and no
    plan_line_mappings row is created (pure read)."""
    import contextlib

    import core.db as db
    from core.dimension_conformance import set_mapping_manual
    from core.mediaplan_store import (
        create_plan,
        create_version_with_lines,
        publish_version,
    )

    conn = _connect()
    org_id, project_id = _seed_org_project(conn)
    try:
        plan = create_plan(
            conn, project_id=project_id, name=f"Plan {uuid.uuid4().hex[:6]}", created_by="tester"
        )
        version = create_version_with_lines(
            conn, plan_id=plan["id"], lines=_LIVE_LINES, created_by="tester"
        )
        publish_version(conn, version_id=version["id"], published_by="tester")
        conn.commit()

        # Confirmed PROJECT-scope dimension mapping: connector 'c1' value 'camp-A' -> 'Placement X'.
        @contextlib.contextmanager
        def _fake_conn():
            yield conn

        # set_mapping_manual opens its own get_connection + commits; route it to our conn.
        monkeypatch.setattr(db, "get_connection", _fake_conn)
        set_mapping_manual(
            canonical_dimension="placement",
            connector="c1",
            source_value="camp-A",
            canonical_value="Placement X",
            scope_level="PROJECT",
            org_id=org_id,
            project_id=project_id,
            identity="tester",
        )

        count_sql = "SELECT COUNT(*) FROM app.plan_line_mappings WHERE plan_id = %s"
        with conn.cursor() as cur:
            cur.execute(count_sql, (plan["id"],))
            before = int(cur.fetchone()[0])

        paa.build_plan_actual_alignment(
            plan["id"],
            dimension="placement",
            org_id=org_id,
            campaign_spend_fn=lambda p, s, e: [
                {"connector": "c1", "campaign_ref": "camp-A", "spend": 80.0},
                {"connector": "c1", "campaign_ref": "unmapped-camp", "spend": 20.0},
            ],
            # NOTE: no mapping ventilates camp-A yet (no plan_line_mappings), so the
            # actual side of the matrix is empty -- but the conform path is exercised via
            # a direct conform_value call below (the real 27.4 resolution with project_id).
        )
        with conn.cursor() as cur:
            cur.execute(count_sql, (plan["id"],))
            after = int(cur.fetchone()[0])
        assert before == after  # pure read: no mapping created

        # Prove the confirmed PROJECT override IS visible through the real conform_value
        # when project_id is passed (27.4 "piege pour 27.5").
        from core.dimension_conformance import conform_value

        assert conform_value(
            org_id, "placement", "c1", "camp-A", project_id=project_id
        ) == "Placement X"
        assert conform_value(
            org_id, "placement", "c1", "unmapped-camp", project_id=project_id
        ) is None
    finally:
        _drop(conn, org_id, project_id)
        conn.close()


def test_the_matrix_is_a_kept_orphan_and_its_successor_is_named() -> None:
    """AD-43, applied to the Python plan-versus-actual matrix (2026-08-17 audit).

    THE MIRROR OF THE 61.3 GUARD. `test_plan_mapping_suggest`'s twin asserts that
    an engine HAS a production importer, so the day it loses one the suite says
    so. This one asserts the opposite and for the same purpose: this engine has
    NONE, the card 27.6 its output contract was written for was never built, and
    the reading production actually serves is the dbt cascade
    `plan_vs_actual_daily` + `plan_pacing_by_*` that `core/cards.py` reads.

    So the module is kept rather than deleted -- two ratified pages still cite
    it, and `governance.md:826-828` even calls it the "only LIVE render-time
    caller of conform_value", which this measurement contradicts. Amending a
    ratified page is an arbitration, not a repair. What this test buys meanwhile
    is that the absence is NOISY: wire a caller and this goes red, so the engine
    can be remounted deliberately but never by accident.

    AN IMPORT, NOT A MENTION. Several files NAME the module in a comment; what is
    measured is whether anything IMPORTS it.
    """
    import pathlib
    import re

    imports = re.compile(
        r"^\s*(?:from\s+[\w.]*\bplan_actual_alignment\b|"
        r"import\s+[\w.]*\bplan_actual_alignment\b|"
        r"from\s+core\s+import\s+[^\n]*\bplan_actual_alignment\b)",
        re.MULTILINE,
    )
    server_root = pathlib.Path(__file__).resolve().parents[2]
    importers = sorted(
        path.relative_to(server_root).as_posix()
        for path in server_root.rglob("*.py")
        if path.name != "plan_actual_alignment.py"
        and imports.search(path.read_text(encoding="utf-8", errors="replace"))
    )

    assert [path for path in importers if not path.startswith("tests/")] == [], (
        "plan_actual_alignment has gained a production importer. That is allowed, "
        "but not silently: the module header calls it a kept orphan whose "
        "successor is the dbt pacing cascade, and divergence D-4 (window-total "
        "ventilation here, daily-bounded in the mart) is still unreconciled. "
        f"Reconcile D-4 and rewrite the header before turning this over: {importers}"
    )
