"""Story 49.3 — Mapping Coverage projects Data, and never becomes a second store.

What these tests hold in place:

* only the mapping version a Datastream currently PUBLISHES counts as coverage;
  a newer unpublished version, an open proposal and a connector default do not;
* a Datastream with no published mapping is in the denominator, not absent;
* an unreadable Data adapter yields `unavailable` with `None` counts — never a
  coverage of zero;
* the page and its totals come from one snapshot, so they cannot disagree;
* no sampled value is read, and none can be returned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from core.semantic_coverage import (
    COVERAGE_STATES,
    CoverageUnavailable,
    compose_mapping_coverage,
    unavailable_report,
)

PROJECT = "proj_EXAMPLE"
MOMENT = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


class _Cursor:
    def __init__(self, tables: dict[str, list[dict]], *, fail_on: str | None = None):
        self._tables = tables
        self._fail_on = fail_on
        self._rows: list[dict] = []
        self._columns: list[str] = ["id"]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query: str, params=None):
        if self._fail_on and self._fail_on in query:
            raise RuntimeError("data adapter unreachable")
        for marker, rows in self._tables.items():
            if marker in query:
                self._rows = rows
                break
        else:
            self._rows = []
        self._columns = list(self._rows[0].keys()) if self._rows else ["id"]

    @property
    def description(self):
        return [(name,) for name in self._columns]

    def fetchall(self):
        return [tuple(row[name] for name in self._columns) for row in self._rows]


def _conn(*, active=(), unmapped=(), proposals=(), fail_on=None) -> MagicMock:
    cursor = _Cursor(
        {
            # Order matters: the active query is the only one joining the
            # mapping-version table, so it is matched on that join.
            "app.datastream_mapping_versions": list(active),
            "current_mapping_version_id IS NULL": list(unmapped),
            "app.mapping_proposals": list(proposals),
        },
        fail_on=fail_on,
    )
    connection = MagicMock()
    connection.cursor.return_value = cursor
    return connection


def _binding(field_id="revenue_micros", status="confirmed", target="mdm_REVENUE", **overrides):
    entry = {
        "field_id": field_id,
        "source_path": f"report.{field_id}",
        "binding": {"status": status, "mdm_target": target, "canonical_target": "revenue"},
        "profile": {"confidence": 0.92},
        "suggestion": {"semantic_role": "measure"},
    }
    entry.update(overrides)
    return entry


def _active_row(fields, *, datastream_id="ds_1", executable=True, **overrides):
    row = {
        "datastream_id": datastream_id,
        "datastream_name": "Meta Ads daily",
        "current_published_execution_id": "dex_1",
        "mapping_version_id": "dmv_ACTIVE",
        "version_number": 4,
        "mapping_payload": {"fields": list(fields)},
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dpv_1",
        "capability_fingerprint": "b" * 64,
        "content_hash": "c" * 64,
        "executable": executable,
        "blocking_count": 0,
        "mapping_created_at": MOMENT,
        "mapping_created_by": "owner@example.com",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Only what Data publishes counts
# ---------------------------------------------------------------------------


def test_a_confirmed_binding_of_the_active_version_counts():
    report = compose_mapping_coverage(_conn(active=[_active_row([_binding()])]), PROJECT)
    assert report.state == "available"
    assert report.bound == 1
    assert report.eligible == 1
    assert report.rows[0].state == "active"
    assert report.rows[0].concept_id == "mdm_REVENUE"
    assert report.rows[0].mapping_version_id == "dmv_ACTIVE"


def test_a_suggested_binding_is_reported_and_not_counted():
    report = compose_mapping_coverage(
        _conn(active=[_active_row([_binding(status="suggested")])]), PROJECT
    )
    assert report.bound == 0
    assert report.eligible == 1
    assert report.rows[0].state == "suggested"
    assert report.by_state == {"suggested": 1}


def test_an_open_proposal_never_moves_the_numerator():
    report = compose_mapping_coverage(
        _conn(
            active=[_active_row([_binding()])],
            proposals=[
                {
                    "id": "mp_1",
                    "datastream_id": "ds_1",
                    "mapping_version_id": "dmv_CANDIDATE",
                    "state": "ready",
                    "mode": "replace",
                    "checks": {},
                    "created_at": MOMENT,
                    "datastream_name": "Meta Ads daily",
                }
            ],
        ),
        PROJECT,
    )
    assert report.bound == 1, "the proposal must not add to coverage"
    # It is linked, so an operator can see what is waiting -- and the decision
    # itself stays with Story 49.4.
    assert report.rows[0].blocking_refs[0]["id"] == "mp_1"
    assert report.rows[0].blocking_refs[0]["decision_owner"] == "governance/controls-quality"


def test_a_confirmed_binding_of_a_non_executable_version_is_stale_not_active():
    report = compose_mapping_coverage(
        _conn(active=[_active_row([_binding()], executable=False)]), PROJECT
    )
    assert report.rows[0].state == "stale"
    assert report.bound == 0


def test_a_blocked_binding_is_blocking_not_merely_unbound():
    report = compose_mapping_coverage(
        _conn(active=[_active_row([_binding(status="blocked")], blocking_count=2)]), PROJECT
    )
    assert report.rows[0].state == "blocking"


def test_an_excluded_binding_stays_distinguishable_from_an_unbound_one():
    report = compose_mapping_coverage(
        _conn(active=[_active_row([_binding(status="excluded")])]), PROJECT
    )
    assert report.rows[0].state == "excluded"


def test_every_declared_state_is_distinguishable():
    assert len(set(COVERAGE_STATES)) == len(COVERAGE_STATES)
    assert set(COVERAGE_STATES) >= {
        "active",
        "confirmed",
        "candidate",
        "suggested",
        "excluded",
        "stale",
        "blocking",
        "unavailable",
    }


# ---------------------------------------------------------------------------
# The denominator is enumerated
# ---------------------------------------------------------------------------


def test_a_datastream_with_no_published_mapping_is_counted_beside_the_fraction():
    report = compose_mapping_coverage(
        _conn(
            active=[_active_row([_binding()])],
            unmapped=[
                {"datastream_id": "ds_2", "datastream_name": "GA4 daily", "status": "active"}
            ],
        ),
        PROJECT,
    )
    # The fraction is counted in (datastream, field) pairs on BOTH sides. An
    # unmapped Datastream has no published mapping, so its field population is
    # UNKNOWN -- adding 1 per Datastream to a denominator counted in fields was the
    # category error that made this fraction mix three populations (CAV-12).
    assert report.bound == 1
    assert report.eligible == 1
    # It is not dropped either: it keeps its own number and its own named row, so
    # the surface still cannot read 100% while a Datastream sits unmapped.
    assert report.unmapped_datastreams == 1
    unmapped_row = next(row for row in report.rows if row.datastream_id == "ds_2")
    assert unmapped_row.state == "unavailable"
    assert unmapped_row.concept_id is None


def test_the_page_and_its_totals_come_from_one_snapshot():
    fields = [_binding(field_id=f"f{i}") for i in range(10)]
    report = compose_mapping_coverage(_conn(active=[_active_row(fields)]), PROJECT, limit=3)
    assert len(report.rows) == 3
    assert report.total_rows == 10
    assert report.truncated is True
    assert report.as_dict()["coverage"]["total"] == 10
    assert report.as_dict()["coverage"]["returned"] == 3


def test_paging_never_changes_the_totals():
    fields = [_binding(field_id=f"f{i}") for i in range(10)]
    first = compose_mapping_coverage(_conn(active=[_active_row(fields)]), PROJECT, limit=4)
    second = compose_mapping_coverage(
        _conn(active=[_active_row(fields)]), PROJECT, limit=4, offset=4
    )
    # Ten eligible fields, all bound: 10/10. Under the old concept-keyed numerator
    # this read 1/10 however complete the mapping was.
    assert first.bound == second.bound == 10
    assert first.eligible == second.eligible == 10
    assert first.total_rows == second.total_rows == 10


# ---------------------------------------------------------------------------
# Unavailable is not zero
# ---------------------------------------------------------------------------


def test_an_unreadable_data_adapter_raises_rather_than_returning_zero():
    with pytest.raises(CoverageUnavailable):
        compose_mapping_coverage(
            _conn(fail_on="app.datastream_mapping_versions"), PROJECT
        )


# The keys of `coverage` that describe the PAGE, not the population: they are
# properties of the window this call returned and stay 0/False even when nothing
# was read. Everything else in `coverage` is a measurement and must be None.
_PAGE_KEYS = frozenset({"by_state", "returned", "total", "bound_limit", "truncated"})


def test_the_unavailable_report_carries_no_counts_at_all():
    """ALL of them, which is what the name has always claimed.

    This assertion used to name `bound` and `eligible` and stop there, so
    `unmapped_datastreams` -- added later, defaulting to 0 -- rendered a
    population that was never read as a population that is empty, inside the very
    payload that refuses to say "0 bound". Enumerating the keys instead of listing
    two of them means a FOURTH count cannot be added without this test noticing.
    """
    report = unavailable_report("connection refused")
    assert report.state == "unavailable"
    assert report.bound is None and report.eligible is None
    assert report.unmapped_datastreams is None
    payload = report.as_dict()
    measured = {key: value for key, value in payload["coverage"].items() if key not in _PAGE_KEYS}
    assert set(measured) == {"bound", "eligible", "unmapped_datastreams"}, (
        "a new count joined the coverage payload: decide what it reads when nothing "
        "was read, and name it here"
    )
    assert all(value is None for value in measured.values()), measured
    assert "not a coverage of zero" in payload["unavailable_reason"]["message"]


def test_a_count_nobody_set_is_not_a_count_of_zero():
    """The default itself, so the next count added cannot repeat the defect."""
    from core.semantic_coverage import CoverageReport

    blank = CoverageReport(state="unavailable")
    assert blank.bound is None
    assert blank.eligible is None
    assert blank.unmapped_datastreams is None


# ---------------------------------------------------------------------------
# Filters, identity resolution and value safety
# ---------------------------------------------------------------------------


def test_a_name_based_binding_resolves_through_the_concept_name_map():
    report = compose_mapping_coverage(
        _conn(active=[_active_row([_binding(target=None)])]),
        PROJECT,
        concept_names={"sc_REVENUE": "revenue"},
    )
    assert report.rows[0].concept_id == "sc_REVENUE"


def test_an_unresolvable_target_is_left_unattributed_not_attached_to_a_neighbour():
    report = compose_mapping_coverage(
        _conn(active=[_active_row([_binding(target=None)])]),
        PROJECT,
        concept_names={"sc_OTHER": "something_else"},
    )
    assert report.rows[0].concept_id is None
    assert report.rows[0].concept_name == "revenue", "the raw target is still shown"
    assert report.bound == 0


def test_filtering_by_concept_keeps_the_totals_of_that_concept():
    report = compose_mapping_coverage(
        _conn(
            active=[
                _active_row([_binding(), _binding(field_id="clicks", target="mdm_CLICKS")])
            ]
        ),
        PROJECT,
        concept_ids=["mdm_REVENUE"],
    )
    assert [row.concept_id for row in report.rows] == ["mdm_REVENUE"]
    assert report.bound == 1


def test_a_concept_filter_does_not_hide_the_datastreams_it_still_counts():
    """The concept workbench reads with `concept_ids=[…]` (`governance_read_model.py:2885`).

    An unmapped Datastream used to be dropped from the rows there while its count
    stayed in `by_state` and in `unmapped_datastreams`: the state filter offered
    "Unavailable (8)" and selecting it showed nothing, and the eight Datastreams
    the number spoke of could not be named. A field bound to ANOTHER Concept is
    correctly outside the question; a Datastream that publishes no mapping is
    outside no question, because its field population is unknown.
    """
    report = compose_mapping_coverage(
        _conn(
            active=[_active_row([_binding()])],
            unmapped=[
                {"datastream_id": f"ds_{i}", "datastream_name": f"Stream {i}", "status": "active"}
                for i in range(2, 10)
            ],
        ),
        PROJECT,
        concept_ids=["mdm_REVENUE"],
    )
    named = [row.datastream_id for row in report.rows if row.state == "unavailable"]
    assert len(named) == 8, "the count says eight; the rows must be able to name eight"
    assert report.unmapped_datastreams == 8
    # The census and the enumeration agree: every state the filter offers has rows.
    assert report.by_state["unavailable"] == len(named)
    # And the fraction itself is untouched by them -- one population, both sides.
    assert report.bound == 1
    assert report.eligible == 1


def test_a_concept_filter_still_excludes_bindings_of_another_concept():
    """The other half of the same rule, so the fix above cannot widen into "show all"."""
    report = compose_mapping_coverage(
        _conn(
            active=[
                _active_row([_binding(), _binding(field_id="clicks", target="mdm_CLICKS")])
            ]
        ),
        PROJECT,
        concept_ids=["mdm_REVENUE"],
    )
    assert [row.source_field_id for row in report.rows] == ["revenue_micros"]
    assert "suggested" not in report.by_state
    assert report.eligible == 1


def test_no_sampled_value_is_ever_returned():
    row = compose_mapping_coverage(
        _conn(
            active=[
                _active_row(
                    [
                        {
                            **_binding(),
                            # A payload that carries samples must not leak them.
                            "samples": ["4900.00", "5100.00"],
                            "profile": {"confidence": 0.9, "sample_values": ["4900.00"]},
                        }
                    ]
                )
            ]
        ),
        PROJECT,
    ).rows[0]
    serialized = repr(row.as_dict())
    assert "4900.00" not in serialized and "5100.00" not in serialized
    assert row.source_field_path == "report.revenue_micros"


def test_the_owner_link_points_at_data_and_carries_the_exact_version():
    row = compose_mapping_coverage(_conn(active=[_active_row([_binding()])]), PROJECT).rows[0]
    assert row.owner_href["workspace"] == "data"
    assert row.owner_href["object_id"] == "ds_1"
    assert row.owner_href["tab"] == "mapping"
    assert row.owner_href["version_id"] == "dmv_ACTIVE"


# ---------------------------------------------------------------------------
# CAV-12: one population on both sides, so 100% is reachable.
# ---------------------------------------------------------------------------


def test_two_fields_bound_to_the_same_concept_reach_full_coverage():
    """The exact shape that made the ceiling unreachable.

    The numerator keyed on the CONCEPT and the denominator on the FIELD, so two
    fields bound to one concept gave bound=1, eligible=2 -- 50% for a mapping that
    is complete. A ratio whose ceiling cannot be reached is not a measure.
    """
    report = compose_mapping_coverage(
        _conn(active=[_active_row([_binding(field_id="f1"), _binding(field_id="f2")])]),
        PROJECT,
    )
    assert report.bound == 2
    assert report.eligible == 2


def test_coverage_payload_names_the_unmapped_count_beside_the_fraction():
    report = compose_mapping_coverage(
        _conn(
            active=[_active_row([_binding()])],
            unmapped=[
                {"datastream_id": "ds_2", "datastream_name": "GA4 daily", "status": "active"}
            ],
        ),
        PROJECT,
    )
    coverage = report.as_dict()["coverage"]
    assert coverage["bound"] == 1
    assert coverage["eligible"] == 1
    assert coverage["unmapped_datastreams"] == 1


# ---------------------------------------------------------------------------
# CAV-19: the publication gate reads a verdict rendered against THIS candidate.
#
# The lookup used to take the LATEST verdict for (project, object) with no version
# pin, so a `pass` recorded against one candidate still answered for the next.
# `analyze-and-test.md:419` states the rule for regression comparisons; a
# publication gate that mixes unpinned versions is the same defect on the
# transition that actually ships.
# ---------------------------------------------------------------------------


class _GateConn:
    """A connection whose Gate Decision store holds one verdict, for one change set.

    THE COLUMNS ARE THE QUERY'S, and that is not a detail. This fake described
    `(outcome, created_at)` -- the shape of the `app.audit_log` row the gate read
    until 2026-08-16, when it moved to `app.evaluation_gate_decisions` and began
    selecting `decision, decided_at, ... candidate_object_type`. `_fetch` builds
    its dict from `cursor.description`, so the fake kept answering a row whose
    `decision` was absent: the gate read `unverifiable` and the test below, which
    asserts a PASS, had been red ever since. A fake that describes columns the
    query does not select proves nothing about either side.
    """

    def __init__(self, stored_change_set: str, outcome: str = "pass"):
        self.stored_change_set = stored_change_set
        self.outcome = outcome
        self.params: dict | None = None

    class _Cur:
        def __init__(self, outer):
            self.outer = outer
            self.description = [
                ("decision",), ("decided_at",), ("comparison_id",), ("coverage",),
                ("failing_dimensions",), ("decision_reason",), ("candidate_object_type",),
            ]
            self._rows: list = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, _sql, params=None):
            self.outer.params = params or {}
            asked = (params or {}).get("change_set_id")
            self._rows = (
                [(
                    self.outer.outcome,
                    "2026-07-31T00:00:00",
                    "ecmp_EXAMPLE",
                    {"eligible": 1, "evaluated": 1, "missing": 0},
                    [],
                    "Every applicable Golden Question passed.",
                    "view",
                )]
                if asked == self.outer.stored_change_set
                else []
            )

        def fetchall(self):
            return self._rows

    def cursor(self):
        return self._Cur(self)


def test_the_gate_asks_for_the_candidate_being_published():
    from core.semantic_model import _evaluate_test_gate

    conn = _GateConn(stored_change_set="chg_1")
    state, _detail = _evaluate_test_gate(conn, PROJECT, "view", "obj_1", change_set_id="chg_1")

    assert state == "pass"
    assert conn.params["change_set_id"] == "chg_1"


def test_a_verdict_from_another_candidate_does_not_satisfy_the_gate():
    """The regression: a pass on the previous candidate clearing the next one."""
    from core.semantic_model import _evaluate_test_gate

    conn = _GateConn(stored_change_set="chg_1")
    state, detail = _evaluate_test_gate(conn, PROJECT, "view", "obj_1", change_set_id="chg_2")

    assert state == "unverifiable"
    assert detail["reason"] == "no_test_coverage"


def test_an_unpinned_call_cannot_reach_a_stored_verdict():
    """Omitting the pin must not silently widen back to "any verdict for this object"."""
    from core.semantic_model import _evaluate_test_gate

    conn = _GateConn(stored_change_set="chg_1")
    state, _detail = _evaluate_test_gate(conn, PROJECT, "view", "obj_1")

    assert state == "unverifiable"
