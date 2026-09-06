"""Story 71.3 -- the Datastream derives a candidate grain, and refuses one the
bindings do not back.

TWO FACTS, MEASURED HERE. First, the DERIVATION: from the `mdm_target` bindings of
the mapping in force, one candidate grain per bound metric -- head plus every
bound dimension present -- and NOTHING for a metric or a dimension not bound to
the MDM. `binding.mdm_target` is the wire, so a column without one cannot enter a
grain, and no metric bound at all means no candidate (an honest empty).

Second, the REFUSAL: the workbench offers to confirm ONLY over columns already
bound to the MDM. Confirming a grain whose head is a column with no `mdm_target`
is refused HERE, by the workbench, before the MDM's own refusals ever run -- the
mirror of `governance.md` § *Incomplete if (this amendment)*: "the Datastream
workbench offers to confirm a grain over columns that are not bound to MDM
canonical fields".

No database: the vocabulary and the current-mapping targets are the only reads,
and both are held still on purpose so the branch under test is the only variable.
"""

from __future__ import annotations

import pytest
from core import datastream_workbench as workbench

_VERSION_ID = "dmv_EXAMPLE0000000000000000"

_SPEND = "mdm_METRIC_SPEND00000000000000"
_CLICKS = "mdm_METRIC_CLICKS0000000000000"
_DAY = "mdm_DIM_DAY0000000000000000000"
_CAMPAIGN = "mdm_DIM_CAMPAIGN00000000000000"


def _vocabulary() -> list[dict]:
    """A tiny MDM: two metrics, two dimensions, all active and project-visible."""
    return [
        {"id": _SPEND, "canonical_name": "spend", "concept_kind": "metric", "value_type": "money"},
        {"id": _CLICKS, "canonical_name": "clicks", "concept_kind": "metric",
         "value_type": "integer"},
        {"id": _DAY, "canonical_name": "day", "concept_kind": "dimension", "value_type": "date"},
        {"id": _CAMPAIGN, "canonical_name": "campaign", "concept_kind": "dimension",
         "value_type": "string"},
    ]


def _versions(fields: list[dict]) -> list[dict]:
    return [{"id": _VERSION_ID, "mapping_payload": {"fields": fields}}]


def _bound(column: str, target: str) -> dict:
    return {"field_id": column, "binding": {"status": "confirmed", "mdm_target": target}}


def _unbound(column: str) -> dict:
    return {"field_id": column, "binding": {"status": "confirmed"}}


@pytest.fixture
def with_vocabulary(monkeypatch):
    def _wire(vocabulary: list[dict]):
        monkeypatch.setattr(
            "core.canonical_field_registry.list_visible_canonical_fields",
            lambda conn, *, project_id: vocabulary,
        )

    return _wire


# --------------------------------------------------------------------------- #
# The derivation                                                              #
# --------------------------------------------------------------------------- #


def test_one_metric_and_two_dimensions_bound_yield_one_grain_of_head_plus_two(with_vocabulary):
    with_vocabulary(_vocabulary())
    versions = _versions(
        [_bound("cost", _SPEND), _bound("date", _DAY), _bound("campaign_name", _CAMPAIGN)]
    )

    answer = workbench._measurement_grain_candidates(
        object(), "proj_EXAMPLE", "ds_EXAMPLE", versions, _VERSION_ID
    )

    assert answer["bound_metrics"] == 1
    assert answer["bound_dimensions"] == 2
    assert len(answer["candidates"]) == 1
    candidate = answer["candidates"][0]
    assert candidate["head"]["canonical_field_id"] == _SPEND
    assert candidate["head"]["canonical_name"] == "spend"
    assert candidate["member_count"] == 2
    assert [m["canonical_field_id"] for m in candidate["members"]] == [_DAY, _CAMPAIGN]


def test_a_dimension_not_bound_to_the_mdm_does_not_enter_the_grain(with_vocabulary):
    """The wire is `mdm_target`: an unbound column is not a member, full stop."""
    with_vocabulary(_vocabulary())
    versions = _versions(
        [_bound("cost", _SPEND), _bound("date", _DAY), _unbound("campaign_name")]
    )

    answer = workbench._measurement_grain_candidates(
        object(), "proj_EXAMPLE", "ds_EXAMPLE", versions, _VERSION_ID
    )

    assert answer["bound_metrics"] == 1
    assert answer["bound_dimensions"] == 1
    candidate = answer["candidates"][0]
    assert [m["canonical_field_id"] for m in candidate["members"]] == [_DAY]
    assert _CAMPAIGN not in {m["canonical_field_id"] for m in candidate["members"]}


def test_no_metric_bound_yields_no_candidate(with_vocabulary):
    """A mapping that binds only dimensions has no measure to report -- honest empty."""
    with_vocabulary(_vocabulary())
    versions = _versions([_bound("date", _DAY), _bound("campaign_name", _CAMPAIGN)])

    answer = workbench._measurement_grain_candidates(
        object(), "proj_EXAMPLE", "ds_EXAMPLE", versions, _VERSION_ID
    )

    assert answer["bound_metrics"] == 0
    assert answer["candidates"] == []
    assert answer["bound_dimensions"] == 2


def test_two_metrics_bound_yield_two_grains_sharing_the_same_dimensions(with_vocabulary):
    with_vocabulary(_vocabulary())
    versions = _versions(
        [_bound("cost", _SPEND), _bound("clicks", _CLICKS), _bound("date", _DAY)]
    )

    answer = workbench._measurement_grain_candidates(
        object(), "proj_EXAMPLE", "ds_EXAMPLE", versions, _VERSION_ID
    )

    assert answer["bound_metrics"] == 2
    heads = {c["head"]["canonical_field_id"] for c in answer["candidates"]}
    assert heads == {_SPEND, _CLICKS}
    for candidate in answer["candidates"]:
        assert [m["canonical_field_id"] for m in candidate["members"]] == [_DAY]


def test_a_binding_to_a_field_the_project_cannot_see_is_counted_unresolved(with_vocabulary):
    """Archived / foreign target: no head, no member, but named as needing repair."""
    with_vocabulary(_vocabulary())
    versions = _versions(
        [
            _bound("cost", _SPEND),
            _bound("date", _DAY),
            _bound("orphan", "mdm_GONE00000000000000000000000"),
        ]
    )

    answer = workbench._measurement_grain_candidates(
        object(), "proj_EXAMPLE", "ds_EXAMPLE", versions, _VERSION_ID
    )

    assert answer["bound_metrics"] == 1
    assert answer["bound_dimensions"] == 1
    assert answer["bound_unresolved"] == 1


def test_an_excluded_binding_is_not_derived(with_vocabulary):
    with_vocabulary(_vocabulary())
    versions = _versions(
        [
            _bound("cost", _SPEND),
            {"field_id": "date", "binding": {"status": "excluded", "mdm_target": _DAY}},
        ]
    )

    answer = workbench._measurement_grain_candidates(
        object(), "proj_EXAMPLE", "ds_EXAMPLE", versions, _VERSION_ID
    )

    assert answer["bound_dimensions"] == 0
    assert answer["candidates"][0]["member_count"] == 0


# --------------------------------------------------------------------------- #
# The refusal                                                                 #
# --------------------------------------------------------------------------- #


class _FakeCursor:
    def __init__(self, payload):
        self._payload = payload
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self._row = (self._payload,)

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, payload):
        self._payload = payload

    def cursor(self):
        return _FakeCursor(self._payload)


def test_confirming_a_grain_on_a_head_column_without_mdm_target_is_refused(monkeypatch):
    """The core refusal: the head must be a column bound to the MDM."""
    from core.metric_dimensions import MeasurementGrainRefused

    # The current mapping binds ONLY the day dimension -- the metric is not bound.
    conn = _FakeConn({"fields": [_bound("date", _DAY)]})

    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("create_measurement_grain must not run when the head is unbacked")

    monkeypatch.setattr("core.metric_dimensions.create_measurement_grain", _boom)

    with pytest.raises(MeasurementGrainRefused) as excinfo:
        workbench.confirm_measurement_grain(
            conn,
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            name="Spend by day",
            head_field_id=_SPEND,  # not present as an mdm_target of the mapping
            member_field_ids=[_DAY],
            actor="tester@example.com",
        )

    assert excinfo.value.code == "grain_head_not_bound_to_mdm"
    assert called["n"] == 0


def test_confirming_a_grain_on_a_member_column_without_mdm_target_is_refused(monkeypatch):
    from core.metric_dimensions import MeasurementGrainRefused

    # The head is bound, but the campaign dimension is not.
    conn = _FakeConn({"fields": [_bound("cost", _SPEND), _bound("date", _DAY)]})

    monkeypatch.setattr(
        "core.metric_dimensions.create_measurement_grain",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    with pytest.raises(MeasurementGrainRefused) as excinfo:
        workbench.confirm_measurement_grain(
            conn,
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            name="Spend by day and campaign",
            head_field_id=_SPEND,
            member_field_ids=[_DAY, _CAMPAIGN],  # campaign not bound
            actor="tester@example.com",
        )

    assert excinfo.value.code == "grain_member_not_bound_to_mdm"


def test_confirming_a_backed_grain_calls_the_mdm_with_both_sides(monkeypatch):
    """When head and every member are bound, the MDM declares the grain -- via it."""
    conn = _FakeConn(
        {
            "fields": [
                _bound("cost", _SPEND),
                _bound("date", _DAY),
                _bound("campaign_name", _CAMPAIGN),
            ]
        }
    )

    seen = {}

    def _create(conn_, *, project_id, name, head_field_id, member_field_ids, description, actor):
        seen.update(
            project_id=project_id,
            name=name,
            head=head_field_id,
            members=list(member_field_ids),
            actor=actor,
        )
        return {"id": "mmd_EXAMPLE00000000000000000000", "name": name}

    monkeypatch.setattr("core.metric_dimensions.create_measurement_grain", _create)

    result = workbench.confirm_measurement_grain(
        conn,
        project_id="proj_EXAMPLE",
        datastream_id="ds_EXAMPLE",
        name="Spend by day and campaign",
        head_field_id=_SPEND,
        member_field_ids=[_DAY, _CAMPAIGN],
        actor="tester@example.com",
    )

    assert result["id"] == "mmd_EXAMPLE00000000000000000000"
    assert seen["head"] == _SPEND
    assert seen["members"] == [_DAY, _CAMPAIGN]
    assert seen["project_id"] == "proj_EXAMPLE"
