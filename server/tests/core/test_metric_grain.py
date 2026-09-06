"""Chantier B -- the same measure at several grains.

WHAT THESE TESTS ARE FOR. Not "does the code run": each one pins a distinction
that, if it collapsed, would let the product state a number it has not earned.

* a gap nobody explained must not read like one somebody did (`undeclared` vs
  `expected`);
* a total that could not be produced must not read like a gap of zero
  (`unavailable` vs a gap);
* a truncated Result has no total of its own, so it reconciles against nothing;
* a request several carriers can answer is arbitrated by a DECLARATION or not at
  all -- never by whichever id sorts first;
* and no refusal in this chantier names a table where it should name a gesture.
"""

from __future__ import annotations

import core.metric_grain as metric_grain
import pytest
from core.metric_grain import (
    VERDICT_EXPECTED,
    VERDICT_RECONCILED,
    VERDICT_UNAVAILABLE,
    VERDICT_UNDECLARED,
    VERDICT_UNEXPLAINED,
    MetricGrainRefused,
    carriers_of,
    grain_coverage,
    reconcile_breakdown,
)
from core.query_execution import resolve_physical_plan

VIEWS, COUNTRY, DATE = "sc_views", "sc_country", "sc_date"
TOTAL_DS, COUNTRY_DS = "ds_channel", "ds_country"


# ---------------------------------------------------------------------------
# A scripted connection, same shape as test_query_execution.py's.
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, script):
        self._script = script
        self._last = ""
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        self.executed.append((sql, params))

    @property
    def description(self):
        for pattern, value in self._script:
            if pattern in self._last:
                if isinstance(value, tuple) and len(value) == 2:
                    return [(name,) for name in value[0]]
        return []

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def fetchall(self):
        for pattern, value in self._script:
            if pattern in self._last:
                return value[1] if isinstance(value, tuple) and len(value) == 2 else value
        return []


class _Conn:
    def __init__(self, script=()):
        self._script = list(script)
        self.cursors = []

    def cursor(self):
        cur = _Cursor(self._script)
        self.cursors.append(cur)
        return cur


def _mapping(targets, grain):
    return {
        "grain": list(grain),
        "fields": [
            {"field_id": name, "binding": {"canonical_target": name}} for name in targets
        ],
    }


# ---------------------------------------------------------------------------
# 1. Who carries a concept is DERIVED from the active mappings.
# ---------------------------------------------------------------------------


def test_carriers_are_read_from_the_active_mappings_and_carry_their_grain():
    conn = _Conn(
        [
            (
                "FROM app.datastreams d",
                (
                    ["datastream_id", "datastream_name", "module_name",
                     "mapping_version_id", "mapping_payload"],
                    [
                        (TOTAL_DS, "channel", "youtube", "dmap_1",
                         _mapping(["views", "date"], ["channel_id", "date"])),
                        (COUNTRY_DS, "country", "youtube", "dmap_2",
                         _mapping(["views", "country", "date"],
                                  ["channel_id", "country", "date"])),
                        ("ds_snapshot", "snapshot", "youtube", "dmap_3",
                         _mapping(["subscriber_count"], ["channel_id", "date"])),
                    ],
                ),
            )
        ]
    )
    carriers = carriers_of(conn, project_id="proj_EXAMPLE", concept_name="views")

    assert [c["datastream_id"] for c in carriers] == [TOTAL_DS, COUNTRY_DS]
    assert carriers[1]["grain"] == ["channel_id", "country", "date"]
    # Nothing was written. The list is a reading, and a reading only.
    assert all("INSERT" not in sql.upper() for cur in conn.cursors for sql, _ in cur.executed)


def test_a_concept_no_mapping_targets_has_no_carriers_and_says_what_would_make_one():
    conn = _Conn([("FROM app.datastreams d", (["datastream_id"], []))])
    coverage = grain_coverage(
        conn, project_id="proj_EXAMPLE", concept_id=VIEWS, concept_name="views"
    )
    assert coverage["carrier_count"] == 0
    assert "Publish a mapping" in coverage["next_gesture"]


# ---------------------------------------------------------------------------
# 2. The four verdicts, and the two collapses that must never happen.
# ---------------------------------------------------------------------------


_DECLARATION = {
    "total_datastream_id": TOTAL_DS,
    "breakdowns": [
        {
            "datastream_id": COUNTRY_DS,
            "sums_to": "partial_by_design",
            "reason": "Rows below the reporting threshold are withheld by the source.",
            "tolerance_ratio": None,
        }
    ],
}


def test_a_declared_share_prints_its_reason_and_is_expected():
    verdict = reconcile_breakdown(
        declaration=_DECLARATION,
        breakdown_datastream_id=COUNTRY_DS,
        breakdown_sum=900,
        total=1000,
    )
    assert verdict["verdict"] == VERDICT_EXPECTED
    assert verdict["gap"] == 100
    assert "withheld by the source" in verdict["statement"]


def test_an_undeclared_gap_is_not_an_expected_one():
    """The collapse this chantier exists to prevent: `undeclared` reading as
    `expected` would turn "nobody looked" into "we checked, it is fine"."""
    verdict = reconcile_breakdown(
        declaration={"total_datastream_id": TOTAL_DS, "breakdowns": []},
        breakdown_datastream_id=COUNTRY_DS,
        breakdown_sum=900,
        total=1000,
    )
    assert verdict["verdict"] == VERDICT_UNDECLARED
    assert verdict["gap"] == 100
    assert verdict["reason"] is None
    assert "no reason claimed" in verdict["statement"]


def test_a_missing_total_is_unavailable_and_never_a_gap_of_zero():
    verdict = reconcile_breakdown(
        declaration=_DECLARATION,
        breakdown_datastream_id=COUNTRY_DS,
        breakdown_sum=900,
        total=None,
    )
    assert verdict["verdict"] == VERDICT_UNAVAILABLE
    assert verdict["gap"] is None
    assert verdict["gap_ratio"] is None


def test_a_declared_equality_inside_its_tolerance_is_reconciled_and_outside_it_is_not():
    declaration = {
        "total_datastream_id": TOTAL_DS,
        "breakdowns": [
            {
                "datastream_id": COUNTRY_DS,
                "sums_to": "equals",
                "reason": None,
                "tolerance_ratio": 0.01,
            }
        ],
    }
    inside = reconcile_breakdown(
        declaration=declaration,
        breakdown_datastream_id=COUNTRY_DS,
        breakdown_sum=995,
        total=1000,
    )
    outside = reconcile_breakdown(
        declaration=declaration,
        breakdown_datastream_id=COUNTRY_DS,
        breakdown_sum=800,
        total=1000,
    )
    assert inside["verdict"] == VERDICT_RECONCILED
    assert outside["verdict"] == VERDICT_UNEXPLAINED


def test_neither_figure_is_adjusted_to_make_the_two_agree():
    verdict = reconcile_breakdown(
        declaration=_DECLARATION,
        breakdown_datastream_id=COUNTRY_DS,
        breakdown_sum=613,
        total=1000,
    )
    assert verdict["total"] == 1000.0
    assert verdict["breakdown_sum"] == 613.0
    assert verdict["gap"] == 387.0


# ---------------------------------------------------------------------------
# 3. Arbitration between several capable carriers is a DECLARATION or a refusal.
# ---------------------------------------------------------------------------


_SPEC = {
    "measures": [{"id": VIEWS, "version_id": "scv_views"}],
    "dimensions": [{"id": DATE, "version_id": "scv_date"}],
    "filters": [],
    "sort": [],
    "row_limit": 500,
    "time": {},
}

_TWO_CAPABLE = [
    (VIEWS, TOTAL_DS, "dmap_1", "active", "views"),
    (DATE, TOTAL_DS, "dmap_1", "active", "date"),
    (VIEWS, COUNTRY_DS, "dmap_2", "active", "views"),
    (DATE, COUNTRY_DS, "dmap_2", "active", "date"),
]


def test_two_capable_carriers_and_no_declaration_refuses_by_naming_the_gesture():
    conn = _Conn(
        [
            ("semantic_view_version_bindings", _TWO_CAPABLE),
            ("metric_grain_declarations", []),
        ]
    )
    plan = resolve_physical_plan(
        conn, project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=_SPEC
    )
    assert "relation" not in plan
    assert "Declare which Datastream is authoritative" in plan["unavailable_reason"]
    # The sentence names a decision, not a row of a table.
    assert "semantic_view_version_bindings" not in plan["unavailable_reason"]


def test_a_declared_total_arbitrates_and_the_plan_records_that_it_did():
    conn = _Conn(
        [
            ("semantic_view_version_bindings", _TWO_CAPABLE),
            ("metric_grain_declarations", [(TOTAL_DS,)]),
            (
                "datastream_output_versions",
                [("mart_channel", "pull_1", "publog_1", None, "dsov_1", "dso_1")],
            ),
            ("mapping_payload", [(_mapping(["views", "date"], ["channel_id", "date"]),)]),
        ]
    )
    plan = resolve_physical_plan(
        conn, project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=_SPEC
    )
    assert plan.get("datastream_id") == TOTAL_DS
    assert plan.get("chosen_by") == "declared_total_authority"


def test_two_measures_declaring_two_different_totals_still_refuses():
    """Not a tie to break: one row answering from two Datastreams is two answers."""
    spec = {
        **_SPEC,
        "measures": [{"id": VIEWS}, {"id": "sc_watch_time"}],
    }
    conn = _Conn(
        [
            (
                "semantic_view_version_bindings",
                [
                    *_TWO_CAPABLE,
                    ("sc_watch_time", TOTAL_DS, "dmap_1", "active", "watch_time"),
                    ("sc_watch_time", COUNTRY_DS, "dmap_2", "active", "watch_time"),
                ],
            ),
            ("metric_grain_declarations", [(TOTAL_DS,), (COUNTRY_DS,)]),
        ]
    )
    plan = resolve_physical_plan(
        conn, project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=spec
    )
    assert "Declare which Datastream is authoritative" in plan["unavailable_reason"]


def test_an_archived_carrier_stops_carrying_and_the_living_one_answers_alone():
    """AI-358 (governance.md, 2026-09-02): a binding whose Datastream is
    archived does not carry -- the exact defect measured on the reference
    project, where an archived draft kept a one-source question refused as an
    arbitration between two carriers."""
    archived_rows = [
        (VIEWS, TOTAL_DS, "dmap_1", "active", "views", None),
        (DATE, TOTAL_DS, "dmap_1", "active", "date", None),
        (VIEWS, COUNTRY_DS, "dmap_2", "active", "views", "2026-08-30T00:00:00Z"),
        (DATE, COUNTRY_DS, "dmap_2", "active", "date", "2026-08-30T00:00:00Z"),
    ]
    conn = _Conn(
        [
            ("semantic_view_version_bindings", archived_rows),
            (
                "datastream_output_versions",
                [("mart_channel", "pull_1", "publog_1", None, "dsov_1", "dso_1")],
            ),
            ("mapping_payload", [(_mapping(["views", "date"], ["channel_id", "date"]),)]),
        ]
    )
    plan = resolve_physical_plan(
        conn, project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=_SPEC
    )
    assert plan.get("datastream_id") == TOTAL_DS
    # One living carrier is no arbitration at all.
    assert plan.get("chosen_by") is None


def test_a_declared_equals_breakdown_arbitrates_when_no_total_declaration_does():
    """AI-358: the person's own `sums_to='equals'` statement picks the carrier;
    the plan records that a breakdown declaration chose it."""
    conn = _Conn(
        [
            ("semantic_view_version_bindings", _TWO_CAPABLE),
            #  Order matters: the breakdown SQL joins the declarations table, so
            #  its more specific pattern must be matched first.
            ("metric_grain_breakdowns", [(COUNTRY_DS,)]),
            ("metric_grain_declarations", []),
            (
                "datastream_output_versions",
                [("mart_country", "pull_2", "publog_2", None, "dsov_2", "dso_2")],
            ),
            ("mapping_payload", [(_mapping(["views", "date"], ["country", "date"]),)]),
        ]
    )
    plan = resolve_physical_plan(
        conn, project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=_SPEC
    )
    assert plan.get("datastream_id") == COUNTRY_DS
    assert plan.get("chosen_by") == "declared_breakdown_authority"


def test_two_equals_breakdowns_on_two_candidates_still_refuse():
    """Two statements are not one: the refusal stands, naming the gesture."""
    conn = _Conn(
        [
            ("semantic_view_version_bindings", _TWO_CAPABLE),
            ("metric_grain_breakdowns", [(COUNTRY_DS,), (TOTAL_DS,)]),
            ("metric_grain_declarations", []),
        ]
    )
    plan = resolve_physical_plan(
        conn, project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=_SPEC
    )
    assert "relation" not in plan
    assert "Declare which Datastream is authoritative" in plan["unavailable_reason"]


def test_one_capable_carrier_is_unchanged_and_records_no_arbitration():
    conn = _Conn(
        [
            (
                "semantic_view_version_bindings",
                [
                    (VIEWS, COUNTRY_DS, "dmap_2", "active", "views"),
                    (DATE, COUNTRY_DS, "dmap_2", "active", "date"),
                ],
            ),
            (
                "datastream_output_versions",
                [("mart_country", "pull_1", "publog_1", None, "dsov_1", "dso_1")],
            ),
            ("mapping_payload", [(_mapping(["views", "date"], ["channel_id", "date"]),)]),
        ]
    )
    plan = resolve_physical_plan(
        conn, project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=_SPEC
    )
    assert plan.get("datastream_id") == COUNTRY_DS
    assert plan.get("chosen_by") is None


# ---------------------------------------------------------------------------
# 4. Every refusal of this chantier names a gesture.
# ---------------------------------------------------------------------------

#: Words that mean a table or a column to us and nothing at all to the person
#: reading the refusal. `metric_grain_declaration` is allowed as a `missing_link`
#: CODE, which is machine-facing -- never inside a sentence.
_DATABASE_WORDS = (
    "app.",
    "_id ",
    "table",
    "column",
    "row of",
    "metric_grain_declarations",
    "metric_grain_breakdowns",
    "semantic_view_version_bindings",
)


@pytest.mark.parametrize(
    "code",
    ["no_carrier", "not_a_carrier", "no_total_declared", "breakdown_is_the_total",
     "reason_required", "tolerance_out_of_range", "unknown_sums_to", "no_such_breakdown"],
)
def test_the_refusal_catalogue_is_complete_and_speaks_in_gestures(code):
    """Every code this module can raise is raised somewhere, and none of them
    hands a person a table name instead of something to do."""
    messages = _refusal_messages()
    assert code in messages, f"`{code}` is declared and never raised"
    text = messages[code].lower()
    for word in _DATABASE_WORDS:
        assert word not in text, f"`{code}` names `{word.strip()}` instead of a gesture"


def _refusal_messages() -> dict[str, str]:
    """Raise each refusal against scripted connections and collect its sentence."""
    out: dict[str, str] = {}

    def _capture(fn, **kwargs):
        try:
            fn(**kwargs)
        except MetricGrainRefused as exc:
            out[exc.code] = exc.message

    no_carriers = _Conn([("FROM app.datastreams d", (["datastream_id"], []))])
    _capture(
        metric_grain.declare_total,
        conn=no_carriers,
        project_id="proj_EXAMPLE",
        concept_id=VIEWS,
        concept_name="views",
        total_datastream_id=TOTAL_DS,
        identity="owner@example.com",
    )

    carriers_script = (
        "FROM app.datastreams d",
        (
            ["datastream_id", "datastream_name", "module_name", "mapping_version_id",
             "mapping_payload"],
            [(TOTAL_DS, "channel", "youtube", "dmap_1", _mapping(["views"], ["date"]))],
        ),
    )
    _capture(
        metric_grain.declare_total,
        conn=_Conn([carriers_script, ("FROM app.metric_grain_declarations", (["id"], []))]),
        project_id="proj_EXAMPLE",
        concept_id=VIEWS,
        concept_name="views",
        total_datastream_id="ds_absent",
        identity="owner@example.com",
    )
    _capture(
        metric_grain.declare_breakdown,
        conn=_Conn([("FROM app.metric_grain_declarations", (["id"], []))]),
        project_id="proj_EXAMPLE",
        concept_id=VIEWS,
        concept_name="views",
        datastream_id=COUNTRY_DS,
        sums_to="equals",
        identity="owner@example.com",
    )
    _capture(
        metric_grain.declare_breakdown,
        conn=_Conn([]),
        project_id="proj_EXAMPLE",
        concept_id=VIEWS,
        concept_name="views",
        datastream_id=COUNTRY_DS,
        sums_to="sometimes",
        identity="owner@example.com",
    )

    declared = [
        (
            "FROM app.metric_grain_declarations",
            (
                ["id", "project_id", "concept_id", "total_datastream_id", "note",
                 "created_by", "created_at", "updated_at"],
                [("mgd_1", "proj_EXAMPLE", VIEWS, TOTAL_DS, None, "o", None, None)],
            ),
        ),
        ("FROM app.metric_grain_breakdowns", (["id", "datastream_id"], [])),
    ]
    _capture(
        metric_grain.declare_breakdown,
        conn=_Conn(declared),
        project_id="proj_EXAMPLE",
        concept_id=VIEWS,
        concept_name="views",
        datastream_id=TOTAL_DS,
        sums_to="equals",
        identity="owner@example.com",
    )
    _capture(
        metric_grain.declare_breakdown,
        conn=_Conn([*declared, carriers_script]),
        project_id="proj_EXAMPLE",
        concept_id=VIEWS,
        concept_name="views",
        datastream_id=TOTAL_DS,
        sums_to="partial_by_design",
        reason="too short",
        identity="owner@example.com",
    )
    # `reason_required` and `tolerance_out_of_range` need a carrier that is not
    # the total, so the two earlier guards are passed first.
    two_carriers = (
        "FROM app.datastreams d",
        (
            ["datastream_id", "datastream_name", "module_name", "mapping_version_id",
             "mapping_payload"],
            [
                (TOTAL_DS, "channel", "youtube", "dmap_1", _mapping(["views"], ["date"])),
                (COUNTRY_DS, "country", "youtube", "dmap_2",
                 _mapping(["views", "country"], ["date", "country"])),
            ],
        ),
    )
    _capture(
        metric_grain.declare_breakdown,
        conn=_Conn([*declared, two_carriers]),
        project_id="proj_EXAMPLE",
        concept_id=VIEWS,
        concept_name="views",
        datastream_id=COUNTRY_DS,
        sums_to="partial_by_design",
        reason="too short",
        identity="owner@example.com",
    )
    _capture(
        metric_grain.declare_breakdown,
        conn=_Conn([*declared, two_carriers]),
        project_id="proj_EXAMPLE",
        concept_id=VIEWS,
        concept_name="views",
        datastream_id=COUNTRY_DS,
        sums_to="equals",
        tolerance_ratio=4.0,
        identity="owner@example.com",
    )
    _capture(
        metric_grain.withdraw_breakdown,
        conn=_Conn([*declared, ("DELETE FROM app.metric_grain_breakdowns", [])]),
        project_id="proj_EXAMPLE",
        concept_id=VIEWS,
        datastream_id=COUNTRY_DS,
        identity="owner@example.com",
    )
    return out


# ---------------------------------------------------------------------------
# 5. The MCP door says the same thing as the screen, in its own channel.
# ---------------------------------------------------------------------------

_RESULT = {
    "id": "res_EXAMPLE",
    "outcome": "success",
    "row_count": 7,
    "cell_count": 14,
    "truncated": False,
    "query_spec_version_id": "qsv_EXAMPLE",
    "ai_path": "No AI path",
    "content_hash": "abc",
}
_PROV = {"semantic_view_version_id": "svv_EXAMPLE"}
_FRESH = {"state": "fresh", "as_of": None}


def _mcp_text(reconciliation):
    from core.analyze_render_mcp import compact_answer

    return compact_answer(_RESULT, _PROV, _FRESH, reconciliation)


def test_the_model_is_told_the_difference_and_the_reason_declared_for_it():
    """A model reading only a breakdown would present it as the whole figure."""
    text = _mcp_text(
        [
            {
                "measure_name": "views",
                "total": 15713,
                "breakdown_sum": 12923,
                "gap": 2790,
                "verdict": "expected",
                "statement": "YouTube suppresses geography rows below its thresholds.",
            }
        ]
    )
    assert "total 15713" in text
    assert "ce Resultat 12923" in text
    assert "ecart 2790 (expected)" in text
    assert "suppresses geography rows" in text


def test_a_failed_comparison_and_nothing_to_compare_read_differently_to_the_model():
    failed = _mcp_text(None)
    nothing = _mcp_text([])
    assert "n'a pas pu etre compare au total declare" in failed
    assert "total declare" not in nothing


def test_the_text_channel_states_what_it_had_to_leave_out():
    entries = [
        {
            "measure_name": f"m{index}",
            "total": 1,
            "breakdown_sum": 1,
            "gap": 0,
            "verdict": "reconciled",
            "statement": "",
        }
        for index in range(5)
    ]
    text = _mcp_text(entries)
    assert "2 autre(s) mesure(s)" in text
    # And it stays inside the channel's own ceiling.
    assert len(text.splitlines()) <= 30


# ---------------------------------------------------------------------------
# 6. The guard's vocabulary is closed, and this door speaks it.
# ---------------------------------------------------------------------------


def test_every_role_this_door_asks_for_is_a_role_that_exists():
    """`identity_has_project_role` RAISES on an unknown name -- there is no
    lenient default -- so a write guarded by an invented `editor` answers 500 on
    every call. It did, until this test existed."""
    import re
    from pathlib import Path

    from core.project_access import _ROLE_CAPABILITY

    source = Path(__file__).resolve().parents[2] / "core" / "metric_grain_api.py"
    asked = set(re.findall(r'_authorize\(request, "(\w+)"\)', source.read_text(encoding="utf-8")))
    assert asked, "the guard is no longer called through `_authorize`; re-point this test"
    unknown = sorted(asked - set(_ROLE_CAPABILITY))
    assert not unknown, f"these are not project roles: {unknown}"
