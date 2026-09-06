"""What a measurement grain accepts, and the things it refuses by name.

Story 71.1. Proven without a database: head/member validation and hashing are
pure, and the refusals are the value. The store, the immutability trigger and the
versioning are proven on real Postgres in
`server/tests/integration/test_metric_dimensions_pg.py`.

The grain is the MIRROR of the common key on the measure axis: it REQUIRES a
metric head and refuses a dimension one, REQUIRES dimension members and refuses a
metric one -- the exact reflection of `component_is_metric`. Its one divergence:
a grain of ZERO dimensions is legal (a total), where a common key of zero is not.
"""

from __future__ import annotations

import pytest
from core.metric_dimensions import (
    MAX_MEMBERS,
    GrainHead,
    MeasurementGrainRefused,
    Member,
    MetricDimensionsUnavailable,
    clean_name,
    derived_coverage,
    grain_hash,
    grain_used_by,
    resolve_grain,
)

from tests.support.statement_router import StatementInventory, UnknownStatement, describe

# EVERY STATEMENT `resolve_grain` ISSUES, NAMED. It reads the canonical-field
# registry (`canonical_field_registry.list_visible_canonical_fields`) and, only
# when the grain has members, the published mappings that implement them
# (`metric_dimensions._implementing_physical_types`). `_FakeCursor` used to tell
# the two apart with `"app.datastreams" in sql` and hand the REGISTRY rows to
# everything else -- a third read, or either of these two rewritten, would have
# been answered with twelve-column canonical-field tuples and the suite would
# have stayed green about a path it never took (AI-317).
_GRAIN_READS = StatementInventory(
    "_FakeCursor (resolve_grain)",
    canonical_fields="from app.mdm_canonical_fields",
    implementing_physical_types="select d.name, m.mapping_payload",
)

# EVERY STATEMENT THE TWO 71.2 SURFACES ISSUE. `_RowsCursor.execute` ignored its
# statement entirely -- it did not even take the argument by a readable name --
# so the coverage rows and the used-by rows were the same answer to any question
# whatsoever. Neither fragment is shared: the coverage read names
# `d.current_mapping_version_id` in its projection, which the `_implementing_
# physical_types` read only mentions in its JOIN.
_GRAIN_SURFACES = StatementInventory(
    "_RowsCursor (derived_coverage / grain_used_by)",
    coverage="select d.id, d.name, d.current_mapping_version_id",
    used_by="jsonb_array_elements(v.master_data_refs)",
)


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: both fakes in this file answer NAMED statements, and nothing else.

    A textual fake holds a copy of the product's query. When the copy goes stale
    the honest outcome is a failure that names the query that moved -- not an
    empty result set, and not the rows of a different read.
    """
    assert (
        _GRAIN_READS.find(
            "SELECT id, project_id, canonical_name, concept_kind, value_type, "
            "aggregation, non_additive, unit, object_kind, description, "
            "dictionary_field_name, status FROM app.mdm_canonical_fields "
            "WHERE status = 'active'"
        )
        == "canonical_fields"
    )
    # A real product statement (`read_measurement_grain`) that `resolve_grain`
    # never issues: plausible in this module, and never taught to this fake.
    with pytest.raises(UnknownStatement) as excinfo:
        _FakeCursor([SPEND]).execute(
            "SELECT id, version_number, content_hash, head, members "
            "FROM app.mdm_metric_dimension_versions WHERE metric_dimensions_id = %s"
        )
    assert "app.mdm_metric_dimension_versions" in str(excinfo.value)
    assert "implementing_physical_types" in str(excinfo.value)

    with pytest.raises(UnknownStatement) as excinfo:
        _RowsCursor([], False).execute("SELECT org_id FROM app.projects WHERE id = %s")
    assert "app.projects" in str(excinfo.value)
    assert "used_by" in str(excinfo.value)


class _FakeConn:
    """Only what `list_visible_canonical_fields` and the mapping read need."""

    def __init__(self, rows, datastreams=()):
        self._rows = rows
        self._datastreams = list(datastreams)

    def cursor(self):
        return _FakeCursor(self._rows, self._datastreams)


class _FakeCursor:
    def __init__(self, rows, datastreams=()):
        self._rows = rows
        # `resolve_grain` reads the registry AND the published mappings (to compare
        # the physical types implementing each member). Two reads, two shapes.
        self._datastreams = list(datastreams)
        self._result: list = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, _params=None):
        statement = _GRAIN_READS.match(sql)
        # DERIVED from the SELECT, never written out here: a hand-typed column
        # list is a second copy of the query, and the copy nothing reads is the
        # one that rots first.
        self.description = describe(sql)
        if statement == "canonical_fields":
            self._result = self._rows
        else:  # implementing_physical_types -- the only other name `match` returns
            self._result = self._datastreams

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


def _field(field_id, name, kind="dimension", value_type="string"):
    # The column order of `canonical_field_registry._READ_COLUMNS`.
    return (
        field_id, "proj_EXAMPLE", name, kind, value_type, None, False, None, None,
        None, None, "active",
    )


def _conn(*fields, datastreams=()):
    return _FakeConn(list(fields), datastreams)


def _datastream(name, *bindings):
    """One published Datastream, as `_implementing_physical_types` reads it.

    That query selects `(d.name, m.mapping_payload)` -- two columns, not the
    four `mapping_coverage` reads -- so the fake row is a 2-tuple.
    `bindings` are `(mdm_target, physical_type)` pairs, resolved.
    """
    return (
        name,
        {
            "fields": [
                {
                    "field_id": f"{name}_{index}",
                    "physical_type": physical_type,
                    "binding": {"mdm_target": target, "status": "resolved"},
                }
                for index, (target, physical_type) in enumerate(bindings)
            ]
        },
    )


SPEND = _field("mdm_00000000000000000000000001", "spend", kind="metric", value_type="money")
CLICKS = _field("mdm_00000000000000000000000002", "clicks", kind="metric", value_type="integer")
DAY = _field("mdm_00000000000000000000000003", "day", value_type="date")
CAMPAIGN = _field("mdm_00000000000000000000000004", "campaign_id")
PUBLISHER = _field("mdm_00000000000000000000000005", "publisher")


# ---------------------------------------------------------------------------
# What it accepts
# ---------------------------------------------------------------------------


def test_a_metric_head_and_dimensions_is_a_grain():
    head, members = resolve_grain(
        _conn(SPEND, DAY, CAMPAIGN),
        project_id="proj_EXAMPLE",
        head_field_id=SPEND[0],
        member_field_ids=[DAY[0], CAMPAIGN[0]],
    )
    assert head.canonical_name == "spend"
    assert [m.canonical_name for m in members] == ["day", "campaign_id"]
    assert [m.ordinal for m in members] == [0, 1]


def test_a_grain_of_zero_dimensions_is_legal():
    """A measure reported at no breakdown is a total -- the one divergence.

    Where `resolve_components` refuses an empty component list (`components_
    required`), a grain of zero members is accepted: it says the measure is a
    grand total.
    """
    head, members = resolve_grain(
        _conn(SPEND),
        project_id="proj_EXAMPLE",
        head_field_id=SPEND[0],
        member_field_ids=[],
    )
    assert head.canonical_name == "spend"
    assert members == []


def test_the_order_of_the_dimensions_is_part_of_the_identity():
    forward = resolve_grain(
        _conn(SPEND, DAY, CAMPAIGN), project_id="proj_EXAMPLE",
        head_field_id=SPEND[0], member_field_ids=[DAY[0], CAMPAIGN[0]],
    )
    backward = resolve_grain(
        _conn(SPEND, DAY, CAMPAIGN), project_id="proj_EXAMPLE",
        head_field_id=SPEND[0], member_field_ids=[CAMPAIGN[0], DAY[0]],
    )
    assert grain_hash(*forward) != grain_hash(*backward)


def test_the_head_is_part_of_the_identity():
    """The same dimensions under a different measure are a different grain."""
    spend = resolve_grain(
        _conn(SPEND, CLICKS, DAY), project_id="proj_EXAMPLE",
        head_field_id=SPEND[0], member_field_ids=[DAY[0]],
    )
    clicks = resolve_grain(
        _conn(SPEND, CLICKS, DAY), project_id="proj_EXAMPLE",
        head_field_id=CLICKS[0], member_field_ids=[DAY[0]],
    )
    assert grain_hash(*spend) != grain_hash(*clicks)


def test_the_hash_ignores_the_names_so_a_rename_does_not_break_a_pin():
    a = grain_hash(GrainHead("mdm_A", "spend", "money"), [Member(0, "mdm_B", "day", "date")])
    b = grain_hash(
        GrainHead("mdm_A", "media_spend", "money"),
        [Member(0, "mdm_B", "reporting_day", "date")],
    )
    assert a == b


def test_adding_a_dimension_changes_the_hash():
    """Adding a dimension is a new version, and the content_hash proves it."""
    one = resolve_grain(
        _conn(SPEND, DAY, CAMPAIGN), project_id="proj_EXAMPLE",
        head_field_id=SPEND[0], member_field_ids=[DAY[0]],
    )
    two = resolve_grain(
        _conn(SPEND, DAY, CAMPAIGN), project_id="proj_EXAMPLE",
        head_field_id=SPEND[0], member_field_ids=[DAY[0], CAMPAIGN[0]],
    )
    assert grain_hash(*one) != grain_hash(*two)


# ---------------------------------------------------------------------------
# The refusals, each naming what it rejected -- the mirror of the common key
# ---------------------------------------------------------------------------


def test_a_missing_head_is_refused():
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        resolve_grain(
            _conn(SPEND), project_id="proj_EXAMPLE",
            head_field_id="   ", member_field_ids=[],
        )
    assert excinfo.value.code == "head_required"


def test_an_unknown_head_is_refused():
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        resolve_grain(
            _conn(SPEND), project_id="proj_EXAMPLE",
            head_field_id="mdm_ELSEWHERE", member_field_ids=[],
        )
    assert excinfo.value.code == "head_not_found"


def test_a_dimension_cannot_head_a_grain():
    """The mirror of `component_is_metric`: the head MUST be a measure."""
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        resolve_grain(
            _conn(SPEND, DAY), project_id="proj_EXAMPLE",
            head_field_id=DAY[0], member_field_ids=[],
        )
    assert excinfo.value.code == "head_is_dimension"
    assert "day" in excinfo.value.message


def test_a_measure_cannot_be_a_member():
    """The mirror on the other side: a member MUST be a dimension."""
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        resolve_grain(
            _conn(SPEND, CLICKS), project_id="proj_EXAMPLE",
            head_field_id=SPEND[0], member_field_ids=[CLICKS[0]],
        )
    assert excinfo.value.code == "member_is_metric"
    assert "clicks" in excinfo.value.message


def test_an_unknown_or_foreign_member_is_one_answer():
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        resolve_grain(
            _conn(SPEND, DAY), project_id="proj_EXAMPLE",
            head_field_id=SPEND[0], member_field_ids=[DAY[0], "mdm_ELSEWHERE"],
        )
    assert excinfo.value.code == "member_not_found"


def test_a_duplicated_member_is_refused():
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        resolve_grain(
            _conn(SPEND, DAY), project_id="proj_EXAMPLE",
            head_field_id=SPEND[0], member_field_ids=[DAY[0], DAY[0]],
        )
    assert excinfo.value.code == "member_duplicated"


def test_the_head_cannot_also_be_a_member():
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        resolve_grain(
            _conn(SPEND, DAY), project_id="proj_EXAMPLE",
            head_field_id=SPEND[0], member_field_ids=[SPEND[0]],
        )
    assert excinfo.value.code == "member_is_the_head"


def test_more_members_than_the_bound_is_refused():
    ids = [f"mdm_{i:026d}" for i in range(MAX_MEMBERS + 1)]
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        resolve_grain(
            _conn(SPEND), project_id="proj_EXAMPLE",
            head_field_id=SPEND[0], member_field_ids=ids,
        )
    assert excinfo.value.code == "too_many_members"


def test_a_missing_name_is_refused_with_a_sentence():
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        clean_name("   ")
    assert excinfo.value.code == "name_required"


def test_a_name_longer_than_the_bound_is_refused():
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        clean_name("x" * 121)
    assert excinfo.value.code == "name_too_long"


def test_every_refusal_carries_a_code_and_a_human_sentence():
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        resolve_grain(
            _conn(SPEND), project_id="proj_EXAMPLE",
            head_field_id="", member_field_ids=[],
        )
    refusal = excinfo.value
    assert refusal.code and refusal.message
    assert refusal.message[0].isupper() and refusal.message.endswith(".")


def test_no_refusal_prints_the_identifier_the_caller_sent():
    """A refusal names the gesture, never a `mdm_<ULID>` -- swept repo-wide too."""
    for head, members in (
        ("mdm_ELSEWHERE", []),
        (SPEND[0], ["mdm_ELSEWHERE"]),
    ):
        with pytest.raises(MeasurementGrainRefused) as excinfo:
            resolve_grain(
                _conn(SPEND, DAY), project_id="proj_EXAMPLE",
                head_field_id=head, member_field_ids=members,
            )
        assert "mdm_ELSEWHERE" not in excinfo.value.message


# ---------------------------------------------------------------------------
# The physical types implementing a member -- the mirror of governance.md:1554
# ---------------------------------------------------------------------------


def test_two_sources_typing_the_same_member_differently_are_refused():
    conn = _conn(
        SPEND, CAMPAIGN,
        datastreams=(
            _datastream("google_ads", (CAMPAIGN[0], "string")),
            _datastream("meta_ads", (CAMPAIGN[0], "bigint")),
        ),
    )
    with pytest.raises(MeasurementGrainRefused) as excinfo:
        resolve_grain(
            conn, project_id="proj_EXAMPLE",
            head_field_id=SPEND[0], member_field_ids=[CAMPAIGN[0]],
        )
    assert excinfo.value.code == "member_physical_types_disagree"
    assert "google_ads" in excinfo.value.message
    assert "meta_ads" in excinfo.value.message


def test_two_spellings_of_one_type_agree():
    conn = _conn(
        SPEND, CAMPAIGN,
        datastreams=(
            _datastream("google_ads", (CAMPAIGN[0], "VARCHAR")),
            _datastream("meta_ads", (CAMPAIGN[0], "text")),
        ),
    )
    _head, members = resolve_grain(
        conn, project_id="proj_EXAMPLE",
        head_field_id=SPEND[0], member_field_ids=[CAMPAIGN[0]],
    )
    assert [m.canonical_name for m in members] == ["campaign_id"]


def test_a_type_the_classifier_does_not_recognize_is_not_a_disagreement():
    conn = _conn(
        SPEND, CAMPAIGN,
        datastreams=(
            _datastream("google_ads", (CAMPAIGN[0], "string")),
            _datastream("weird_source", (CAMPAIGN[0], "st_geography")),
        ),
    )
    assert resolve_grain(
        conn, project_id="proj_EXAMPLE",
        head_field_id=SPEND[0], member_field_ids=[CAMPAIGN[0]],
    )


def test_a_suggested_binding_does_not_implement_the_member():
    google = _datastream("google_ads", (CAMPAIGN[0], "string"))
    meta = _datastream("meta_ads", (CAMPAIGN[0], "bigint"))
    meta[1]["fields"][0]["binding"]["status"] = "suggested"
    conn = _conn(SPEND, CAMPAIGN, datastreams=(google, meta))
    assert resolve_grain(
        conn, project_id="proj_EXAMPLE",
        head_field_id=SPEND[0], member_field_ids=[CAMPAIGN[0]],
    )


def test_a_read_that_failed_does_not_refuse_a_declaration():
    class _Unreadable(_FakeConn):
        def cursor(self):
            cursor = _FakeCursor(self._rows, self._datastreams)
            execute = cursor.execute

            def _execute(sql, params=None):
                if _GRAIN_READS.match(sql) == "implementing_physical_types":
                    raise RuntimeError("connection reset")
                execute(sql, params)

            cursor.execute = _execute
            return cursor

    assert resolve_grain(
        _Unreadable([SPEND, CAMPAIGN]), project_id="proj_EXAMPLE",
        head_field_id=SPEND[0], member_field_ids=[CAMPAIGN[0]],
    )


# ---------------------------------------------------------------------------
# Derived coverage -- the mirror of `mapping_coverage`, with the head/partial
# distinction that is the heart of 71.2
# ---------------------------------------------------------------------------


class _RowsConn:
    """A conn whose single read returns fixed rows, or raises.

    `derived_coverage` and `grain_used_by` each run exactly one query, so one
    result set is all a fake needs. `fail=True` proves the two failure modes:
    coverage returns `unavailable`, used-by RAISES.
    """

    def __init__(self, rows, *, fail=False):
        self._rows = list(rows)
        self._fail = fail

    def cursor(self):
        return _RowsCursor(self._rows, self._fail)


class _RowsCursor:
    def __init__(self, rows, fail):
        self._rows = rows
        self._fail = fail
        self._result: list = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, _params=None):
        # NAMED BEFORE IT FAILS. `fail=True` simulates an unreadable store, and
        # it must not become the door through which a statement this fake was
        # never taught leaves without being recognized.
        _GRAIN_SURFACES.match(sql)
        self.description = describe(sql)
        if self._fail:
            raise RuntimeError("connection reset")
        self._result = self._rows

    def fetchall(self):
        return self._result


def _cov_row(ds_id, name, mapping_version_id, *bindings):
    """One `(id, name, current_mapping_version_id, payload)` coverage row.

    `bindings` are `(mdm_target, physical_field_id, status, physical_type)`. A
    `mapping_version_id` of None is a Datastream that published nothing -- the
    `unknown` state, never counted as covered.
    """
    payload = None
    if mapping_version_id is not None:
        payload = {
            "fields": [
                {
                    "field_id": physical,
                    "physical_type": physical_type,
                    "binding": {"mdm_target": target, "status": status},
                }
                for target, physical, status, physical_type in bindings
            ]
        }
    return (ds_id, name, mapping_version_id, payload)


_HEAD = {"canonical_field_id": SPEND[0], "canonical_name": "spend", "value_type": "money"}
_MEMBERS = [
    {"ordinal": 0, "canonical_field_id": DAY[0], "canonical_name": "day"},
    {"ordinal": 1, "canonical_field_id": CAMPAIGN[0], "canonical_name": "campaign_id"},
]


def test_a_datastream_binding_head_and_all_dimensions_is_full():
    conn = _RowsConn([
        _cov_row(
            "ds_1", "google_ads", "dmv_1",
            (SPEND[0], "cost", "resolved", "number"),
            (DAY[0], "date", "confirmed", "date"),
            (CAMPAIGN[0], "campaign", "resolved", "string"),
        ),
    ])
    coverage = derived_coverage(conn, project_id="proj_EXAMPLE", head=_HEAD, members=_MEMBERS)
    assert coverage["state"] == "available"
    assert coverage["counts"] == {"full": 1, "partial": 0, "absent": 0, "unknown": 0}
    ds = coverage["datastreams"][0]
    assert ds["coverage"] == "full"
    assert ds["head_bound"] is True
    assert ds["missing_dimensions"] == []


def test_a_datastream_binding_the_metric_but_not_a_dimension_is_partial():
    """The heart of 71.2: the gap is NAMED, never rendered as covered."""
    conn = _RowsConn([
        _cov_row(
            "ds_1", "google_ads", "dmv_1",
            (SPEND[0], "cost", "resolved", "number"),
            (DAY[0], "date", "confirmed", "date"),
            # campaign is NOT bound.
        ),
    ])
    coverage = derived_coverage(conn, project_id="proj_EXAMPLE", head=_HEAD, members=_MEMBERS)
    assert coverage["counts"]["partial"] == 1
    ds = coverage["datastreams"][0]
    assert ds["coverage"] == "partial"
    assert ds["head_bound"] is True
    missing = [m["canonical_name"] for m in ds["missing_dimensions"]]
    assert missing == ["campaign_id"]  # the trou is named, not a hidden zero


def test_a_datastream_not_binding_the_metric_is_absent():
    """The grain is anchored on its measure: no head, no coverage of the grain."""
    conn = _RowsConn([
        _cov_row(
            "ds_1", "analytics", "dmv_1",
            (DAY[0], "date", "confirmed", "date"),
            (CAMPAIGN[0], "campaign", "resolved", "string"),
        ),
    ])
    coverage = derived_coverage(conn, project_id="proj_EXAMPLE", head=_HEAD, members=_MEMBERS)
    ds = coverage["datastreams"][0]
    assert ds["coverage"] == "absent"
    assert ds["head_bound"] is False
    assert coverage["counts"] == {"full": 0, "partial": 0, "absent": 1, "unknown": 0}


def test_a_datastream_without_a_published_mapping_is_unknown_never_covered():
    conn = _RowsConn([
        _cov_row("ds_1", "google_ads", None),  # nothing published
        _cov_row(
            "ds_2", "meta_ads", "dmv_2",
            (SPEND[0], "spend", "resolved", "number"),
        ),
    ])
    coverage = derived_coverage(conn, project_id="proj_EXAMPLE", head=_HEAD, members=[])
    assert coverage["unknown_datastreams"] == [{"id": "ds_1", "name": "google_ads"}]
    assert coverage["counts"]["unknown"] == 1
    # A grain of zero members is full the moment the head is bound: a total.
    assert coverage["datastreams"][0]["coverage"] == "full"


def test_a_suggested_binding_does_not_cover_the_head():
    conn = _RowsConn([
        _cov_row("ds_1", "google_ads", "dmv_1", (SPEND[0], "cost", "suggested", "number")),
    ])
    coverage = derived_coverage(conn, project_id="proj_EXAMPLE", head=_HEAD, members=[])
    assert coverage["datastreams"][0]["coverage"] == "absent"


def test_coverage_is_derived_it_moves_when_a_mapping_publishes_the_dimension():
    """Same grain; coverage flips partial -> full when a binding appears."""
    before = derived_coverage(
        _RowsConn([
            _cov_row(
                "ds_1", "google_ads", "dmv_1",
                (SPEND[0], "cost", "resolved", "number"),
                (DAY[0], "date", "confirmed", "date"),
            ),
        ]),
        project_id="proj_EXAMPLE", head=_HEAD, members=_MEMBERS,
    )
    after = derived_coverage(
        _RowsConn([
            _cov_row(
                "ds_1", "google_ads", "dmv_2",
                (SPEND[0], "cost", "resolved", "number"),
                (DAY[0], "date", "confirmed", "date"),
                (CAMPAIGN[0], "campaign", "resolved", "string"),
            ),
        ]),
        project_id="proj_EXAMPLE", head=_HEAD, members=_MEMBERS,
    )
    assert before["datastreams"][0]["coverage"] == "partial"
    assert after["datastreams"][0]["coverage"] == "full"


def test_the_head_and_each_member_name_the_datastreams_that_implement_them():
    conn = _RowsConn([
        _cov_row(
            "ds_1", "google_ads", "dmv_1",
            (SPEND[0], "cost", "resolved", "number"),
            (DAY[0], "date", "confirmed", "date"),
        ),
    ])
    coverage = derived_coverage(conn, project_id="proj_EXAMPLE", head=_HEAD, members=_MEMBERS)
    assert [i["datastream_name"] for i in coverage["head"]["implemented_by"]] == ["google_ads"]
    assert coverage["head"]["implemented_by"][0]["physical_field_id"] == "cost"
    day = next(m for m in coverage["members"] if m["canonical_name"] == "day")
    campaign = next(m for m in coverage["members"] if m["canonical_name"] == "campaign_id")
    assert [i["datastream_name"] for i in day["implemented_by"]] == ["google_ads"]
    assert campaign["implemented_by"] == []  # nobody binds it yet


def test_a_read_that_failed_returns_unavailable_not_a_zero():
    coverage = derived_coverage(
        _RowsConn([], fail=True), project_id="proj_EXAMPLE", head=_HEAD, members=_MEMBERS
    )
    assert coverage["state"] == "unavailable"
    assert coverage["counts"] is None
    assert coverage["unknown_datastreams"] is None


# ---------------------------------------------------------------------------
# Used-by -- lists what pins a version of the grain, and RAISES on an
# unreadable store rather than return a bare tuple
# ---------------------------------------------------------------------------


def test_used_by_returns_the_pins_and_derives_still_holds():
    conn = _RowsConn([
        ("svv_live", "semantic-view", "revenue_view", "published", "mmdv_1"),
        ("scv_hist", "semantic-concept", "old_metric", "archived", "mmdv_1"),
        ("svv_draft", "semantic-view", "draft_view", "draft", "mmdv_2"),
    ])
    pins = grain_used_by(conn, project_id="proj_EXAMPLE", measurement_grain_id="mmd_1")
    holds = {pin["name"]: pin["still_holds"] for pin in pins}
    assert holds == {"revenue_view": True, "old_metric": False, "draft_view": True}


def test_used_by_raises_when_the_store_is_unreadable_never_an_empty_tuple():
    """The `master_data.fetch_used_by` doctrine: a swallowed error reads as safe."""
    with pytest.raises(MetricDimensionsUnavailable):
        grain_used_by(
            _RowsConn([], fail=True), project_id="proj_EXAMPLE", measurement_grain_id="mmd_1"
        )
