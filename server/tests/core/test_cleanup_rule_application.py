"""AI-260 second half -- cleanup rules reach the served warehouse read.

The subject is the SAME ratified sentence as the first half, crossed on the same
`datastreams_dim` bridge: a Datastream-scoped rule that does not cover every
live Datastream of its connector is an AMBIGUITY -- a gap, NAMED, never a pick.
And the second subject is the 60.3 obligation: the applied rules compile INTO
the warehouse statement, in the dialect that statement is built for, and the
DuckDB emission is EXECUTED here against a real engine rather than asserted on
as a string (the BigQuery half is policed in
`tests/conformance/test_cleanup_application_dialects.py`).

Offline (no DB): a scripted connection serves the two reads the resolution makes
(the project's rules through their own store module, the dim the mirror is made
from), so every rung of the classification is proved without a database --
including the refusal to report an outage as "no rule".

End-to-end (real DuckDB file, no Postgres): the campaign-spend readers apply a
resolved application -- rows removed, values stripped, identities merged, micros
still exact -- and the plan-versus-actual matrix proves the ORDER: cleanup
rewrites in SQL first, and the client value table of the first half translates
the value cleanup produced.

Live Postgres (skipped when TEST_POSTGRES_DSN is unset): the same rungs against
the real stores, on the fixture whose two Datastreams share one connector.
"""

from __future__ import annotations

import importlib
import os
import uuid

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import cleanup_rule_application as application_module  # noqa: E402
from core.cleanup_rule_application import (  # noqa: E402
    REASON_NO_LIVE_DATASTREAM,
    REASON_NO_RULE,
    REASON_PARTIAL_ASSIGNMENT,
    STATE_AMBIGUOUS,
    STATE_APPLIED,
    STATE_NONE,
    STATE_UNAVAILABLE,
    CleanupRuleApplication,
    resolve_cleanup_rules,
)

from tests.core.test_value_mapping_tables import fixture_org, pg_available  # noqa: E402,F401

# ===========================================================================
# Offline -- a scripted connection, one result set per relation named.
# ===========================================================================


class _ScriptedCursor:
    """Serves (columns, rows) for the first script key found in the SQL."""

    def __init__(self, script):
        self._script = script
        self._columns: list[str] = []
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, _params=None):
        for key, (columns, rows) in self._script.items():
            if key in sql:
                self._columns = list(columns)
                self._rows = list(rows)
                return
        self._columns, self._rows = [], []

    @property
    def description(self):
        return [(name,) for name in self._columns]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _ScriptedConn:
    def __init__(self, script):
        self._script = script

    def cursor(self):
        return _ScriptedCursor(self._script)


_RULE_COLUMNS = [
    "id", "org_id", "project_id", "datastream_id", "name", "source_field",
    "rule_kind", "pattern", "enabled", "dry_run_state", "dry_run_detail",
    "created_by", "created_at", "updated_at", "current_version_id",
    "datastream_count",
]


def _rule_row(
    rule_id,
    *,
    datastream_id=None,
    name=None,
    source_field="campaign_id",
    rule_kind="exclude_row",
    pattern="_TEST_",
    enabled=True,
):
    return (
        rule_id, "org-1", "proj-1", datastream_id, name or rule_id, source_field,
        rule_kind, pattern, enabled, "passed", None, None, None, None, None, 1,
    )


def _conn(*, rules, datastreams=()):
    """A connection scripted with the two relations the resolution reads.

    `datastreams` rows are (datastream_id, connector, name) -- the dim joined to
    its names. SAVEPOINT statements match no key and harmlessly return nothing.
    """
    return _ScriptedConn(
        {
            "app.cleanup_rules": (_RULE_COLUMNS, list(rules)),
            "app.datastreams_dim_v": (
                ["datastream_id", "connector", "name"],
                list(datastreams),
            ),
        }
    )


def _resolve(conn):
    return resolve_cleanup_rules(conn, project_id="proj-1", source_field="campaign_id")


def test_a_project_wide_rule_applies_directly():
    conn = _conn(rules=[_rule_row("crule_1", name="No test campaigns")])
    application = _resolve(conn)
    assert application.state == STATE_APPLIED
    assert application.applies
    [unit] = application.rules
    assert unit["scope"] == "project"
    assert unit["connector"] is None
    assert unit["pattern"] == "_TEST_"
    assert unit["rules"] == [{"rule_id": "crule_1", "name": "No test campaigns"}]
    assert application.gaps == ()


def test_a_rule_on_the_connectors_only_datastream_applies_at_connector_scope():
    """The bridge: the fact keys on connector, so a Datastream-scoped rule is
    served as a connector-scoped guard -- legitimately, because the one live
    Datastream IS the connector's whole served surface."""
    conn = _conn(
        rules=[_rule_row("crule_1", datastream_id="ds_1")],
        datastreams=[("ds_1", "example_connector", "Stream one")],
    )
    application = _resolve(conn)
    assert application.state == STATE_APPLIED
    [unit] = application.rules
    assert unit["scope"] == "connector"
    assert unit["connector"] == "example_connector"


def test_a_rule_on_one_of_two_datastreams_is_a_gap_NAMED_never_a_pick():
    """Connector-keyed rows merge every Datastream's rows; removing them under a
    rule one Datastream never carried would be a pick. Mirrors the mart's own
    `declared_count < datastream_count` rung, like the value-table bridge."""
    conn = _conn(
        rules=[_rule_row("crule_1", datastream_id="ds_1", name="Half a rule")],
        datastreams=[
            ("ds_1", "example_connector", "Stream one"),
            ("ds_2", "example_connector", "Stream two"),
        ],
    )
    application = _resolve(conn)
    assert application.state == STATE_AMBIGUOUS
    assert application.reason == REASON_PARTIAL_ASSIGNMENT
    assert not application.applies
    assert application.rules == ()
    [gap] = application.gaps
    assert gap["reason"] == REASON_PARTIAL_ASSIGNMENT
    assert gap["connector"] == "example_connector"
    # Both sides are NAMED, checkable by the person reading the state.
    assert [r["rule_id"] for r in gap["rules"]] == ["crule_1"]
    assert gap["missing_datastreams"] == [
        {"datastream_id": "ds_2", "datastream_name": "Stream two"}
    ]


def test_the_same_body_on_every_datastream_is_agreement_one_compiled_unit():
    conn = _conn(
        rules=[
            _rule_row("crule_1", datastream_id="ds_1"),
            _rule_row("crule_2", datastream_id="ds_2"),
        ],
        datastreams=[
            ("ds_1", "example_connector", "Stream one"),
            ("ds_2", "example_connector", "Stream two"),
        ],
    )
    application = _resolve(conn)
    assert application.state == STATE_APPLIED
    [unit] = application.rules
    assert unit["connector"] == "example_connector"
    assert [r["rule_id"] for r in unit["rules"]] == ["crule_1", "crule_2"]
    assert application.gaps == ()


def test_a_rule_on_a_datastream_no_longer_live_is_a_named_gap_too():
    conn = _conn(
        rules=[_rule_row("crule_1", datastream_id="ds_gone", name="Orphan rule")],
        datastreams=[("ds_1", "example_connector", "Stream one")],
    )
    application = _resolve(conn)
    assert application.state == STATE_AMBIGUOUS
    [gap] = application.gaps
    assert gap["reason"] == REASON_NO_LIVE_DATASTREAM
    assert gap["rules"][0]["datastream_id"] == "ds_gone"


def test_another_field_or_a_disabled_rule_does_not_reach_this_read():
    """Exact string equality on the field, and only ENABLED rules: the
    correspondence between a raw column and this partition is never guessed."""
    conn = _conn(
        rules=[
            _rule_row("crule_1", source_field="utm_campaign"),
            _rule_row("crule_2", enabled=False),
        ]
    )
    application = _resolve(conn)
    assert application.state == STATE_NONE
    assert application.reason == REASON_NO_RULE
    assert not application.applies


def test_an_outage_is_UNAVAILABLE_never_reported_as_no_rule():
    """'I could not check' and 'nothing is removed from my data' are different
    facts, and only one of them may serve the read uncleaned in silence."""

    class _BrokenCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, *_a, **_k):
            raise RuntimeError("the store is unreachable")

    class _BrokenConn:
        def cursor(self):
            return _BrokenCursor()

    application = resolve_cleanup_rules(
        _BrokenConn(), project_id="proj-1", source_field="campaign_id"
    )
    assert application.state == STATE_UNAVAILABLE
    assert "RuntimeError" in application.reason
    assert not application.applies


def test_the_application_serialises_for_the_read_contract():
    conn = _conn(rules=[_rule_row("crule_1", name="No test campaigns")])
    payload = _resolve(conn).as_dict()
    assert payload["state"] == "applied"
    assert payload["source_field"] == "campaign_id"
    assert payload["rules"][0]["rules"] == [
        {"rule_id": "crule_1", "name": "No test campaigns"}
    ]
    assert payload["gaps"] == []


def test_a_mixed_project_keeps_the_applied_half_and_names_the_gapped_half():
    """Applied units and gaps travel TOGETHER: the project-wide rule compiles,
    the half-covered one does not, and neither fact hides the other."""
    conn = _conn(
        rules=[
            _rule_row("crule_1", name="Project wide"),
            _rule_row("crule_2", datastream_id="ds_1", name="Half covered",
                      pattern="^BRAND"),
        ],
        datastreams=[
            ("ds_1", "example_connector", "Stream one"),
            ("ds_2", "example_connector", "Stream two"),
        ],
    )
    application = _resolve(conn)
    assert application.state == STATE_APPLIED
    assert [u["scope"] for u in application.rules] == ["project"]
    assert [g["reason"] for g in application.gaps] == [REASON_PARTIAL_ASSIGNMENT]


# ===========================================================================
# End-to-end -- the campaign-spend readers, against a real DuckDB.
# ===========================================================================


def _seed_fact(db_file, rows):
    """A minimal `fact_daily_kpi`: rows are (project, date, connector, campaign, value)."""
    import duckdb

    con = duckdb.connect(str(db_file))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute(
        """
        CREATE TABLE main_marts.fact_daily_kpi (
            project_id VARCHAR, date DATE, connector VARCHAR, metric VARCHAR,
            breakdown_dimension VARCHAR, breakdown_value VARCHAR, value DOUBLE
        )
        """
    )
    con.executemany(
        "INSERT INTO main_marts.fact_daily_kpi VALUES (?, ?, ?, 'cost', 'campaign_id', ?, ?)",
        rows,
    )
    con.close()


def _warehouse_on(db_file, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_file))
    from core import warehouse

    return importlib.reload(warehouse)


def _applied(*units):
    return CleanupRuleApplication(
        state=STATE_APPLIED,
        project_id="default",
        source_field="campaign_id",
        rules=tuple(units),
    )


def _unit(rule_kind, pattern, *, connector=None):
    return {
        "scope": "project" if connector is None else "connector",
        "connector": connector,
        "rule_kind": rule_kind,
        "pattern": pattern,
        "rules": [{"rule_id": "crule_x", "name": "Rule under test"}],
    }


def test_a_project_wide_exclude_removes_the_row_from_the_served_read(
    tmp_path, monkeypatch
):
    db_file = tmp_path / "cleanup.duckdb"
    _seed_fact(
        db_file,
        [
            ("default", "2026-03-01", "c1", "KEEP-1", 0.10),
            ("default", "2026-03-02", "c1", "internal_TEST_run", 5.0),
        ],
    )
    warehouse = _warehouse_on(db_file, monkeypatch)
    rows = warehouse.query_campaign_spend(
        "default", "2026-03-01", "2026-03-31",
        cleanup=_applied(_unit("exclude_row", "_TEST_")),
    )
    assert {r["campaign_ref"] for r in rows} == {"KEEP-1"}
    # The money discipline of AI-267 survives the weave: exact integer micros.
    assert rows[0]["spend_micros"] == 100_000


def test_a_connector_scoped_rule_guards_only_its_connector(tmp_path, monkeypatch):
    """The bridge-resolved unit compiles behind a bound connector guard: the
    other connector's rows pass untouched -- the rule reaches exactly the
    Datastreams the client scoped it to, and nothing wider."""
    db_file = tmp_path / "scoped.duckdb"
    _seed_fact(
        db_file,
        [
            ("default", "2026-03-01", "c1", "internal_TEST_run", 5.0),
            ("default", "2026-03-01", "c2", "internal_TEST_run", 7.0),
        ],
    )
    warehouse = _warehouse_on(db_file, monkeypatch)
    rows = warehouse.query_campaign_spend(
        "default", "2026-03-01", "2026-03-31",
        cleanup=_applied(_unit("exclude_row", "_TEST_", connector="c1")),
    )
    assert [(r["connector"], r["campaign_ref"]) for r in rows] == [
        ("c2", "internal_TEST_run")
    ]


def test_a_strip_rule_merges_the_cleaned_identities_and_micros_stay_exact(
    tmp_path, monkeypatch
):
    """Two raw values, one served identity: the GROUP BY groups on the CLEANED
    value, which is the point of a strip rule at read -- and 0.10 + 0.20 is
    300000 micros exactly, not a float drift."""
    db_file = tmp_path / "strip.duckdb"
    _seed_fact(
        db_file,
        [
            ("default", "2026-03-01", "c1", "BRAND-A?utm_source=x", 0.10),
            ("default", "2026-03-02", "c1", "BRAND-A", 0.20),
        ],
    )
    warehouse = _warehouse_on(db_file, monkeypatch)
    rows = warehouse.query_campaign_spend(
        "default", "2026-03-01", "2026-03-31",
        cleanup=_applied(_unit("strip_match", r"\?utm_.*$")),
    )
    assert len(rows) == 1
    assert rows[0]["campaign_ref"] == "BRAND-A"
    assert rows[0]["spend_micros"] == 300_000


def test_the_daily_reader_applies_the_same_rules_as_the_window_total(
    tmp_path, monkeypatch
):
    """A rule applied on one grain and not the other would make the two readers
    disagree about which campaigns exist -- same builder, same rules."""
    db_file = tmp_path / "daily.duckdb"
    _seed_fact(
        db_file,
        [
            ("default", "2026-03-01", "c1", "KEEP-1", 0.07),
            ("default", "2026-03-01", "c1", "internal_TEST_run", 5.0),
            ("default", "2026-03-02", "c1", "KEEP-1", 0.29),
        ],
    )
    warehouse = _warehouse_on(db_file, monkeypatch)
    cleanup = _applied(_unit("exclude_row", "_TEST_"))
    daily = warehouse.query_campaign_spend_daily(
        "default", "2026-03-01", "2026-03-31", cleanup=cleanup
    )
    assert {r["campaign_ref"] for r in daily} == {"KEEP-1"}
    assert {r["day"]: r["spend_micros"] for r in daily} == {
        "2026-03-01": 70_000,
        "2026-03-02": 290_000,
    }


def test_an_unresolvable_control_plane_serves_the_read_uncleaned_never_a_500(
    tmp_path, monkeypatch
):
    """The default resolution failing is an OUTAGE, not a refusal: the rows are
    served (uncleaned, logged), because taking down every spend read over a
    governance-store outage would break the honest majority no rule touches."""
    db_file = tmp_path / "outage.duckdb"
    _seed_fact(db_file, [("default", "2026-03-01", "c1", "internal_TEST_run", 5.0)])
    warehouse = _warehouse_on(db_file, monkeypatch)

    import core.db as db

    def _boom():
        raise RuntimeError("control plane down")

    monkeypatch.setattr(db, "get_connection", _boom)
    rows = warehouse.query_campaign_spend("default", "2026-03-01", "2026-03-31")
    assert [r["campaign_ref"] for r in rows] == ["internal_TEST_run"]


def test_a_gapped_application_compiles_nothing_the_gap_is_not_a_half_pick(
    tmp_path, monkeypatch
):
    """An application whose only content is gaps applies NO rule: filtering
    would clean rows of a Datastream the client never scoped, and half-applying
    would be the pick the ratified rule forbids."""
    db_file = tmp_path / "gap.duckdb"
    _seed_fact(db_file, [("default", "2026-03-01", "c1", "internal_TEST_run", 5.0)])
    warehouse = _warehouse_on(db_file, monkeypatch)
    gapped = CleanupRuleApplication(
        state=STATE_AMBIGUOUS,
        project_id="default",
        source_field="campaign_id",
        reason=REASON_PARTIAL_ASSIGNMENT,
        gaps=(
            {
                "reason": REASON_PARTIAL_ASSIGNMENT,
                "connector": "c1",
                "rule_kind": "exclude_row",
                "pattern": "_TEST_",
                "rules": [{"rule_id": "crule_1", "name": "Half", "datastream_id": "ds_1"}],
                "missing_datastreams": [
                    {"datastream_id": "ds_2", "datastream_name": "Stream two"}
                ],
            },
        ),
    )
    rows = warehouse.query_campaign_spend(
        "default", "2026-03-01", "2026-03-31", cleanup=gapped
    )
    assert [r["campaign_ref"] for r in rows] == ["internal_TEST_run"]


# ===========================================================================
# The ORDER with the value tables, pinned end to end (AI-260, both halves).
# ===========================================================================


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_a, **_k):
        return None

    def fetchone(self):
        return None


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return _FakeCursor()


def _patch_plan(monkeypatch, *, plan, mappings):
    import core.db as db
    import core.mediaplan_mapping as mm
    import core.mediaplan_store as store

    monkeypatch.setattr(store, "get_plan", lambda conn, *, plan_id: plan)
    monkeypatch.setattr(mm, "list_mappings", lambda conn, *, plan_id: mappings)
    monkeypatch.setattr(db, "get_connection", lambda: _FakeConn())


def _alignment_fixture(monkeypatch, *, campaign_ref):
    plan = {
        "id": "p1",
        "project_id": "default",
        "lines": [
            {
                "line_key": "l1",
                "label": "Placement X",
                "start_date": "2026-03-01",
                "end_date": "2026-03-31",
                "budget": "100.00",
            }
        ],
    }
    mappings = {
        "plan_id": "p1",
        "lines": [
            {
                "line_key": "l1",
                "mappings": [
                    {
                        "connector": "c1",
                        "campaign_ref": campaign_ref,
                        "split_weight": "1.000000",
                        "status": "active",
                    }
                ],
            }
        ],
    }
    _patch_plan(monkeypatch, plan=plan, mappings=mappings)


def _value_table(pairs):
    from core.value_table_resolution import ValueTableResolution

    return ValueTableResolution(
        state="applied",
        project_id="default",
        connector="c1",
        source_field="campaign_id",
        table_id="vmt_1",
        table_name="Client vocabulary",
        pairs=pairs,
    )


def test_cleanup_strips_in_sql_BEFORE_the_value_table_translates(
    tmp_path, monkeypatch
):
    """THE ORDER, pinned through the real statement: the strip rule rewrites
    `BRAND-A?utm_source=x` to `BRAND-A` inside the warehouse read, and the
    client's table -- keyed on the CLEANED value -- then translates it. A pair
    keyed on the raw value would prove the opposite order; the counter-pin
    below shows it does not translate."""
    from core import plan_actual_alignment as paa

    db_file = tmp_path / "order.duckdb"
    _seed_fact(db_file, [("default", "2026-03-05", "c1", "BRAND-A?utm_source=x", 80.0)])
    _warehouse_on(db_file, monkeypatch)
    # The mapping names the CLEANED identity: that is what the read serves.
    _alignment_fixture(monkeypatch, campaign_ref="BRAND-A")

    strip = _applied(_unit("strip_match", r"\?utm_.*$"))
    out = paa.build_plan_actual_alignment(
        "p1",
        dimension="placement",
        org_id="org-1",
        conform_fn=lambda c, s: None,  # 052 silent: only the table can conform
        value_table_resolution_fn=lambda conn, **_: _value_table(
            {"BRAND-A": "Product Line"}
        ),
        cleanup_resolution_fn=lambda conn, **_: strip,
    )
    conformed = [c for c in out["cells"] if c["conformed"] and c["actual"] > 0]
    assert [c["axis_value"] for c in conformed] == ["Product Line"]
    assert conformed[0]["actual"] == 80.0
    assert out["cleanup_rules"]["state"] == "applied"


def test_counter_pin_a_pair_keyed_on_the_raw_value_does_not_translate(
    tmp_path, monkeypatch
):
    """If the value table ran FIRST, a pair keyed on the raw collected value
    would win. It does not: the table only sees what cleanup already served."""
    from core import plan_actual_alignment as paa

    db_file = tmp_path / "order2.duckdb"
    _seed_fact(db_file, [("default", "2026-03-05", "c1", "BRAND-A?utm_source=x", 80.0)])
    _warehouse_on(db_file, monkeypatch)
    _alignment_fixture(monkeypatch, campaign_ref="BRAND-A")

    strip = _applied(_unit("strip_match", r"\?utm_.*$"))
    out = paa.build_plan_actual_alignment(
        "p1",
        dimension="placement",
        org_id="org-1",
        conform_fn=lambda c, s: None,
        value_table_resolution_fn=lambda conn, **_: _value_table(
            {"BRAND-A?utm_source=x": "Product Line"}  # raw key: must NOT match
        ),
        cleanup_resolution_fn=lambda conn, **_: strip,
    )
    # The actual stays under the CLEANED source name: applied table, no pair for
    # the served value -- the precedence is a precedence, not a merge.
    source_named = [c for c in out["cells"] if not c["conformed"]]
    assert [c["axis_value"] for c in source_named] == ["BRAND-A"]
    assert not any(
        c["axis_value"] == "Product Line" and c["actual"] > 0 for c in out["cells"]
    )


def test_alignment_contract_names_the_gap_and_the_outage_never_a_500(monkeypatch):
    """Offline: the named states travel on the contract exactly as the value
    tables' do -- a gap is a note and a data row, an outage is a note and a
    data row, and neither raises."""
    from core import plan_actual_alignment as paa

    _alignment_fixture(monkeypatch, campaign_ref="camp-A")
    spend = [{"connector": "c1", "campaign_ref": "camp-A", "spend": 40.0}]

    gapped = CleanupRuleApplication(
        state=STATE_AMBIGUOUS,
        project_id="default",
        source_field="campaign_id",
        reason=REASON_PARTIAL_ASSIGNMENT,
        gaps=(
            {
                "reason": REASON_PARTIAL_ASSIGNMENT,
                "connector": "c1",
                "rule_kind": "exclude_row",
                "pattern": "_TEST_",
                "rules": [{"rule_id": "crule_1", "name": "Half", "datastream_id": "ds_1"}],
                "missing_datastreams": [
                    {"datastream_id": "ds_2", "datastream_name": "Stream two"}
                ],
            },
        ),
    )
    out = paa.build_plan_actual_alignment(
        "p1",
        dimension="placement",
        org_id="org-1",
        campaign_spend_fn=lambda p, s, e: spend,
        conform_fn=lambda c, s: None,
        value_table_resolution_fn=lambda conn, **_: _value_table({}),
        cleanup_resolution_fn=lambda conn, **_: gapped,
    )
    assert out["cleanup_rules"]["state"] == "ambiguous"
    assert out["cleanup_rules"]["gaps"][0]["missing_datastreams"][0][
        "datastream_name"
    ] == "Stream two"
    gap_notes = [n for n in out["notes"] if "cleanup rule gap" in n]
    assert len(gap_notes) == 1
    assert "a gap, never a pick" in gap_notes[0]
    assert "Stream two" in gap_notes[0]

    unavailable = CleanupRuleApplication(
        state=STATE_UNAVAILABLE,
        project_id="default",
        source_field="campaign_id",
        reason="store unreadable: RuntimeError",
    )
    out = paa.build_plan_actual_alignment(
        "p1",
        dimension="placement",
        org_id="org-1",
        campaign_spend_fn=lambda p, s, e: spend,
        conform_fn=lambda c, s: None,
        value_table_resolution_fn=lambda conn, **_: _value_table({}),
        cleanup_resolution_fn=lambda conn, **_: unavailable,
    )
    assert out["cleanup_rules"]["state"] == "unavailable"
    assert any("cleanup rules unavailable" in n for n in out["notes"])


# ===========================================================================
# [Unmapped Actuals] -- the served caller carries the same named states.
# ===========================================================================


def test_unmapped_actuals_contract_carries_the_cleanup_states():
    from core.mediaplan_mapping import list_unmapped_actuals

    conn = _ScriptedConn(
        {
            "FROM app.media_plans": (["project_id"], [("proj-1",)]),
            "MIN(l.start_date)": (["min", "max"], [("2026-03-01", "2026-03-31")]),
            "app.plan_line_mappings": (["connector", "campaign_ref"], []),
        }
    )
    gapped = CleanupRuleApplication(
        state=STATE_AMBIGUOUS,
        project_id="proj-1",
        source_field="campaign_id",
        reason=REASON_PARTIAL_ASSIGNMENT,
        gaps=(
            {
                "reason": REASON_PARTIAL_ASSIGNMENT,
                "connector": "c1",
                "rule_kind": "exclude_row",
                "pattern": "_TEST_",
                "rules": [{"rule_id": "crule_1", "name": "Half", "datastream_id": "ds_1"}],
                "missing_datastreams": [
                    {"datastream_id": "ds_2", "datastream_name": "Stream two"}
                ],
            },
        ),
    )
    seen = {}

    def _resolution_fn(conn_arg, *, project_id, source_field):
        seen["key"] = (project_id, source_field)
        return gapped

    result = list_unmapped_actuals(
        conn,
        plan_id="p1",
        campaign_spend_daily_fn=lambda p, s, e: [
            {"connector": "c1", "campaign_ref": "B", "day": "2026-03-05", "spend": 40.0}
        ],
        cleanup_resolution_fn=_resolution_fn,
    )
    # The resolution was asked with the read's exact field, once, on this conn.
    assert seen["key"] == ("proj-1", "campaign_id")
    assert result["cleanup_rules"]["state"] == "ambiguous"
    assert result["cleanup_rules"]["gaps"][0]["connector"] == "c1"
    assert [r["campaign_ref"] for r in result["unmapped"]] == ["B"]


def test_unmapped_actuals_without_a_window_says_no_cleanup_was_involved():
    from core.mediaplan_mapping import list_unmapped_actuals

    conn = _ScriptedConn(
        {
            "FROM app.media_plans": (["project_id"], [("proj-1",)]),
            "MIN(l.start_date)": (["min", "max"], [(None, None)]),
        }
    )
    result = list_unmapped_actuals(conn, plan_id="p1")
    assert result["window"] is None
    assert result["cleanup_rules"] is None


# ===========================================================================
# Live Postgres -- the same rungs against the real stores.
# ===========================================================================


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _make_rule(conn, fixture, *, datastream_id=None, name=None, pattern="_TEST_",
               rule_kind="exclude_row", source_field="campaign_id", enabled=True):
    from core.cleanup_rules import create_rule

    return create_rule(
        conn,
        org_id=fixture["org_id"],
        project_id=fixture["project_id"],
        datastream_id=datastream_id,
        name=name or f"Rule {uuid.uuid4().hex[:6]}",
        source_field=source_field,
        rule_kind=rule_kind,
        pattern=pattern,
        identity="owner@example.com",
        dry_run_state="not_attempted",
        enabled=enabled,
    )


@pg_available
def test_pg_project_wide_applies_and_a_half_covered_body_is_a_named_gap(fixture_org):  # noqa: F811
    """The fixture's two Datastreams share module_name='example_connector' --
    exactly the bridge's case: the fact would key them as ONE connector."""
    from core.db import get_connection

    ds_1, ds_2 = fixture_org["datastreams"]
    with get_connection() as conn:
        _make_rule(conn, fixture_org, name="Project wide")
        _make_rule(
            conn, fixture_org, datastream_id=ds_1, name="Half covered",
            pattern="^BRAND",
        )
        conn.commit()

        application = resolve_cleanup_rules(
            conn,
            project_id=fixture_org["project_id"],
            source_field="campaign_id",
        )

    assert application.state == STATE_APPLIED
    assert [u["scope"] for u in application.rules] == ["project"]
    [gap] = application.gaps
    assert gap["reason"] == REASON_PARTIAL_ASSIGNMENT
    assert gap["connector"] == "example_connector"
    assert [d["datastream_id"] for d in gap["missing_datastreams"]] == [ds_2]


@pg_available
def test_pg_the_same_body_on_both_datastreams_applies_at_connector_scope(fixture_org):  # noqa: F811
    from core.db import get_connection

    ds_1, ds_2 = fixture_org["datastreams"]
    with get_connection() as conn:
        _make_rule(conn, fixture_org, datastream_id=ds_1, name="Body on one")
        _make_rule(conn, fixture_org, datastream_id=ds_2, name="Body on two")
        conn.commit()

        application = resolve_cleanup_rules(
            conn,
            project_id=fixture_org["project_id"],
            source_field="campaign_id",
        )

    assert application.state == STATE_APPLIED
    [unit] = application.rules
    assert unit["scope"] == "connector"
    assert unit["connector"] == "example_connector"
    assert application.gaps == ()


@pg_available
def test_pg_another_field_or_a_disabled_rule_stays_out_of_the_read(fixture_org):  # noqa: F811
    from core.db import get_connection

    with get_connection() as conn:
        none_yet = resolve_cleanup_rules(
            conn, project_id=fixture_org["project_id"], source_field="campaign_id"
        )
        assert none_yet.state == STATE_NONE
        assert none_yet.reason == REASON_NO_RULE

        _make_rule(conn, fixture_org, name="Other field", source_field="utm_campaign")
        _make_rule(conn, fixture_org, name="Disabled", enabled=False)
        conn.commit()

        still_none = resolve_cleanup_rules(
            conn, project_id=fixture_org["project_id"], source_field="campaign_id"
        )

    assert still_none.state == STATE_NONE
    assert still_none.reason == REASON_NO_RULE


def test_the_module_under_test_is_the_one_the_served_readers_import():
    """The seam is real: `warehouse._campaign_cleanup` resolves through THIS
    module, so a rule proven here is a rule the served read applies."""
    import inspect

    from core import warehouse

    source = inspect.getsource(warehouse._campaign_cleanup)
    assert "cleanup_rule_application" in source
    assert application_module.resolve_cleanup_rules is resolve_cleanup_rules
