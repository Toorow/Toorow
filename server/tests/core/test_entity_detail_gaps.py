"""Chantier C -- the inventory of holes, and the confusion it exists to prevent.

MEASURED 2026-08-14 on the reference Project: 519 distinct `video` observed in the
published relation, 3 events landed, and NO expressible relation between them --
the id was inside a URL in a free-text column, so only a server that knew one
provider's URL shape could have joined them.

What these tests pin is the honesty of the answer, not its arithmetic:

* `unavailable` is never `0 missing`. "We could not look" and "nothing is
  missing" are opposite answers, and the second closes a case that is still open;
* a capped scan says it was capped rather than presenting a partial count as a
  whole one;
* the gesture names an event stream, never a column.
"""

from __future__ import annotations

import core.entity_detail_gaps as gaps
import pytest
from core.entity_detail_gaps import EntityGapsUnavailable, inventory


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, detailed=()):
        self._rows = [(key,) for key in detailed]
        self.cursors = []

    def cursor(self):
        cur = _Cursor(self._rows)
        self.cursors.append(cur)
        return cur


_PLAN = {
    "relation": "raw_video_daily",
    "dataset": "raw_proj_EXAMPLE",
    "columns": {"sc_video": "video"},
    "present_columns": ["date", "video", "metric", "value"],
    "long_form": ("metric", "value"),
}


def _inventory(conn, observed, monkeypatch, plan=None):
    monkeypatch.setattr(gaps, "observed_keys", lambda *a, **k: list(observed))
    return inventory(
        conn,
        project_id="proj_EXAMPLE",
        plan=plan or _PLAN,
        member_id="sc_video",
        member_name="video",
        entity_kind="video",
    )


def test_the_inventory_counts_what_is_observed_what_is_detailed_and_what_is_not(monkeypatch):
    out = _inventory(_Conn(detailed=["a", "b"]), ["a", "b", "c", "d"], monkeypatch)
    assert out["state"] == "exact"
    assert out["observed_count"] == 4
    assert out["detailed_count"] == 2
    assert out["missing_count"] == 2
    assert out["missing"] == ["c", "d"]


def test_an_entity_detailed_but_never_observed_does_not_inflate_the_detailed_count(monkeypatch):
    """A detail that arrived for something this Datastream does not measure is
    not coverage of it. Counting it would make an inventory look closed while
    every observed entity was still bare."""
    out = _inventory(_Conn(detailed=["x", "y", "z"]), ["a"], monkeypatch)
    assert out["observed_count"] == 1
    assert out["detailed_count"] == 0
    assert out["missing_count"] == 1


def test_a_relation_that_could_not_be_read_is_unavailable_and_never_zero_missing(monkeypatch):
    def _raise(*_a, **_k):
        raise EntityGapsUnavailable("the published relation could not be read")

    monkeypatch.setattr(gaps, "observed_keys", _raise)
    out = inventory(
        _Conn(detailed=["a"]),
        project_id="proj_EXAMPLE",
        plan=_PLAN,
        member_id="sc_video",
        member_name="video",
        entity_kind="video",
    )
    assert out["state"] == "unavailable"
    assert out["missing_count"] is None
    assert out["observed_count"] is None
    assert "nothing is claimed" in out["next_gesture"]


def test_a_capped_scan_says_so_rather_than_passing_a_partial_count_as_a_whole_one(monkeypatch):
    observed = [f"k{index}" for index in range(gaps.MAX_OBSERVED + 5)]
    out = _inventory(_Conn(), observed, monkeypatch)
    assert out["state"] == "capped"
    assert out["observed_truncated"] is True
    assert str(gaps.MAX_OBSERVED) in out["next_gesture"]


def test_the_listed_missing_is_bounded_and_says_when_it_was_cut(monkeypatch):
    observed = [f"k{index:04d}" for index in range(gaps.MAX_LISTED_MISSING + 20)]
    out = _inventory(_Conn(), observed, monkeypatch)
    assert len(out["missing"]) == gaps.MAX_LISTED_MISSING
    assert out["missing_truncated"] is True
    # The COUNT is still exact -- only the list is a page of it.
    assert out["missing_count"] == len(observed)


def test_a_complete_inventory_owes_no_gesture(monkeypatch):
    out = _inventory(_Conn(detailed=["a", "b"]), ["a", "b"], monkeypatch)
    assert out["missing_count"] == 0
    assert out["next_gesture"] is None


def test_a_datastream_that_has_measured_nothing_is_not_reported_as_complete(monkeypatch):
    out = _inventory(_Conn(), [], monkeypatch)
    assert out["missing_count"] == 0
    assert "measured no" in out["next_gesture"]


@pytest.mark.parametrize("field", ["next_gesture"])
def test_the_gesture_names_an_event_stream_and_never_a_column(monkeypatch, field):
    out = _inventory(_Conn(), ["a", "b"], monkeypatch)
    sentence = out[field].lower()
    assert "arm an event stream" in sentence
    for word in ("app.", "column", "entity_key", "context_events", "table"):
        assert word not in sentence


# ---------------------------------------------------------------------------
# The observed side reads the relation the executor reads, the same way.
# ---------------------------------------------------------------------------


def test_a_breakdown_landing_is_read_through_its_dimension_rows(monkeypatch):
    """The dimension of a breakdown landing is not a column of it -- the shape
    `build_sql` learned in chantier B. Reading it any other way here would make
    this module the one place that disagrees about what a relation holds."""
    captured: dict = {}

    # PATCHED ON THE REAL MODULE, not by swapping `sys.modules`. `observed_keys`
    # does `from core import warehouse`, which reads the attribute already bound
    # on the `core` package -- a `sys.modules` entry does not rebind it once the
    # package has imported it. Run alone the stub looked like it worked; run
    # after any test that had imported the warehouse, the REAL runner answered
    # and this test proved nothing while staying green.
    from core import warehouse

    monkeypatch.setattr(warehouse, "_db_mode", lambda: "duckdb")

    def _run(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{"k": "FR"}, {"k": "BE"}]

    monkeypatch.setattr(warehouse, "_query_duckdb", _run)

    keys = gaps.observed_keys(
        {
            "relation": "raw_breakdown",
            "dataset": "raw_proj_EXAMPLE",
            "columns": {"sc_country": "country"},
            "present_columns": [
                "breakdown_dimension", "breakdown_value", "metric", "value", "date",
            ],
        },
        member_id="sc_country",
    )
    assert keys == ["FR", "BE"]
    assert "breakdown_value" in captured["sql"]
    assert "breakdown_dimension = ?" in captured["sql"]
    assert captured["params"] == ["country"]


def test_a_member_with_no_physical_field_is_unavailable_not_empty():
    with pytest.raises(EntityGapsUnavailable):
        gaps.observed_keys({"relation": "r", "columns": {}}, member_id="sc_absent")


# ---------------------------------------------------------------------------
# The offer: which dimensions the question can even be asked for.
# ---------------------------------------------------------------------------


class _KindCursor:
    """Two reads, in order: the landed entity kinds, then the concept names."""

    def __init__(self, kinds, names):
        self._kinds = kinds
        self._names = names
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, *_a, **_k):
        self._last = sql

    def fetchall(self):
        if "context_events" in self._last:
            return [(kind,) for kind in self._kinds]
        return [(f"m{index}", name) for index, name in enumerate(self._names)]


class _KindConn:
    def __init__(self, kinds=(), names=(), fail=False):
        self._kinds = kinds
        self._names = names
        self._fail = fail

    def cursor(self):
        if self._fail:
            raise RuntimeError("db down")
        return _KindCursor(self._kinds, self._names)


def _record(names):
    """A Result whose fields carry a member id and a PHYSICAL column name.

    The physical names are deliberately unlike the concept names here: the offer
    must key on the concept's name, which is what the inventory measures under.
    """
    return {
        "manifest": {},
        "result_schema": {
            "fields": [
                {"id": f"m{index}", "name": f"col_{index}"} for index, _ in enumerate(names)
            ]
        },
    }


def test_only_a_dimension_whose_name_is_a_landed_entity_kind_is_offered():
    from core.analyze_workbench import _entity_gap_candidates

    names = ["video", "date", "country"]
    offered = _entity_gap_candidates(
        _KindConn(kinds=["video"], names=names), _record(names), "proj_EXAMPLE"
    )
    assert [entry["member_name"] for entry in offered] == ["video"]
    # And it is keyed on the CONCEPT name, not on the physical column the Result
    # field is called -- those two are unlike each other in this fixture.
    assert offered[0]["member_id"] == "m0"


def test_no_landed_entity_kind_offers_nothing_rather_than_every_dimension():
    from core.analyze_workbench import _entity_gap_candidates

    names = ["video", "date"]
    assert _entity_gap_candidates(_KindConn(names=names), _record(names), "proj_EXAMPLE") == []


def test_an_unreadable_registry_offers_nothing_and_still_lets_the_result_open():
    """The inventory is an OFFER. A Result must still open when the offer cannot
    be made -- raising here would take a whole lens down for a side question."""
    from core.analyze_workbench import _entity_gap_candidates

    assert (
        _entity_gap_candidates(_KindConn(fail=True), _record(["video"]), "proj_EXAMPLE") == []
    )


def test_the_offer_is_keyed_on_the_concept_name_even_when_the_column_differs():
    """The reference Project's `video` maps to a field also called `video`. That
    coincidence is not the contract, and an offer keyed on the column would offer
    a dimension the inventory then measures under another name."""
    from core.analyze_workbench import _entity_gap_candidates

    names = ["video"]
    record = {
        "manifest": {},
        "result_schema": {"fields": [{"id": "m0", "name": "yt_video_identifier"}]},
    }
    offered = _entity_gap_candidates(_KindConn(kinds=["video"], names=names), record, "p")
    assert offered == [{"member_id": "m0", "member_name": "video"}]
