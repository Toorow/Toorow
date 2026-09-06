from __future__ import annotations

import pytest

from tests.core.test_geographic_plan_compilation import _country_evidence, _intent
from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

# EVERY STATEMENT `apply_country_plan_fan_out` ISSUES ON ITS OWN CONNECTION,
# NAMED. There is exactly one: the fan-out reads each reviewed Datastream with
# its current plan and its current mapping under a row lock, and every other
# collaborator on the path (`CountryCompiler.project_evidence`,
# `save_datastream_intent`, `save_field_mapping`, `compile_projection`,
# `create_execution`, `enqueue_activation_work`) is substituted by the tests.
#
# AI-317. The chain this replaces was a bare `if` with NO `else`: a statement
# the fake did not recognize left `self._row` at its previous value -- `None` on
# a fresh cursor, or the PREVIOUS Datastream's pins on a reused one. Both are
# answers the product reads as a decision: `row is None` is the `ProjectSettingsStale`
# branch at core/country_activation.py:262, and a stale row is a plan pin that
# silently agrees with the proposal. A rewritten join -- the alias `d`, the two
# `JOIN`s, the `FOR UPDATE` tail -- would have gone on producing them.
_FAN_OUT = StatementInventory(
    "test_country_activation._Cursor",
    # core/country_activation.py:241 -- the reviewed Datastream's current plan
    # and mapping pins, locked for the duration of the fan-out.
    datastream_pins=("from app.datastreams d", "for update"),
)


class _Cursor:
    """A cursor that answers the STATEMENT, and refuses everything else."""

    def __init__(self, plan_version_id="dsp_1"):
        self.plan_version_id = plan_version_id
        self._row = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=()):
        statement = _FAN_OUT.match(sql)
        if statement == "datastream_pins":
            # The description is DERIVED from the projection, never restated:
            # a hand-written column list is a second copy of the query, and it
            # is the copy that goes stale because nothing reads it.
            self.description = describe(sql)
            self._row = (self.plan_version_id, _intent(), "dsm_1", {"fields": []})
        else:  # pragma: no cover - a name added to the inventory, unanswered
            raise _FAN_OUT.unknown(sql)

    def fetchone(self):
        return self._row


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: the bare `if` that left the previous answer standing is gone.

    The statement below is the plausible neighbour -- the same relation, the
    same key, read WITHOUT the plan and mapping joins. It used to leave
    `self._row` untouched and be read as "this Datastream is not active".
    """
    cursor = _Cursor()
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT lifecycle_state FROM app.datastreams WHERE id = %s AND project_id = %s",
            ("ds_1", "proj_1"),
        )
    message = str(raised.value)
    assert "app.datastreams where id = %s" in message
    assert "datastream_pins" in message
    assert cursor.fetchone() is None


class _Connection:
    def __init__(self, plan_version_id="dsp_1"):
        self.plan_version_id = plan_version_id

    def cursor(self):
        return _Cursor(self.plan_version_id)


def _proposal(*, version_id="mdv_country_1"):
    return {
        "id": "dscp_country_1",
        "datastream_id": "ds_1",
        "capability_key": "country",
        "applicability": "applicable",
        "current_plan_version_id": "dsp_1",
        "dependency_snapshot": {"current_plan_version_id": "dsp_1"},
        "governance_owner_references": [
            {
                "object_type": "registry",
                "object_id": "mdr_country",
                "version_id": version_id,
                "evidence_hash": "c" * 64,
            }
        ],
        "impact": {
            "detected_support_selection": {
                "proposed_plan_content_hash": "d" * 64,
            }
        },
    }


def _patch_candidate_pipeline(monkeypatch):
    import core.datastream_field_mapping as field_mapping
    import core.datastream_projection as projection
    import core.datastream_publication as publication
    import core.queue as queue

    monkeypatch.setattr(
        field_mapping,
        "save_field_mapping",
        lambda **_kwargs: {"id": "dsm_country_2", "executable": True},
    )
    monkeypatch.setattr(
        projection,
        "compile_projection",
        lambda _mapping: {"executable": True, "projection": "country"},
    )
    monkeypatch.setattr(
        publication,
        "create_execution",
        lambda *_args, **_kwargs: {"id": "dse_country_2"},
    )
    monkeypatch.setattr(
        queue,
        "enqueue_activation_work",
        lambda **_kwargs: {"job_id": "dsaj_country_2", "state": "pending"},
    )

def test_country_fan_out_reuses_the_existing_plan_writer(monkeypatch):
    import core.datastream_intents as datastream_intents
    from core.capability_compilers import CountryCompiler
    from core.country_activation import apply_country_plan_fan_out

    monkeypatch.setattr(
        CountryCompiler,
        "project_evidence",
        lambda *_args, **_kwargs: _country_evidence(),
    )
    calls = []

    def save(**kwargs):
        calls.append(kwargs)
        return {
            "id": "dsp_country_2",
            "version_number": 2,
            "content_hash": "d" * 64,
            "executable": True,
        }

    monkeypatch.setattr(datastream_intents, "save_datastream_intent", save)
    _patch_candidate_pipeline(monkeypatch)

    result = apply_country_plan_fan_out(
        _Connection(),
        project_id="proj_1",
        change_set_id="pcset_1",
        proposals=[_proposal()],
        actor="person_1",
        enabled=True,
        loaded_modules=[object()],
    )

    assert result["data_pointers_moved"] is False
    assert result["candidates"][0]["candidate_execution_id"] == "dse_country_2"
    assert result["plan_versions"][0]["plan_version_id"] == "dsp_country_2"
    assert calls[0]["idempotency_key"] == "country:pcset_1:ds_1"
    assert calls[0]["commit"] is False
    assert calls[0]["advance_pointer"] is False
    assert calls[0]["loaded_modules"]


def test_country_fan_out_rejects_a_moved_governance_version_before_writing(monkeypatch):
    import core.datastream_intents as datastream_intents
    from core.capability_compilers import CountryCompiler
    from core.country_activation import apply_country_plan_fan_out
    from core.project_settings import ProjectSettingsStale

    monkeypatch.setattr(
        CountryCompiler,
        "project_evidence",
        lambda *_args, **_kwargs: _country_evidence(),
    )
    called = []
    monkeypatch.setattr(
        datastream_intents,
        "save_datastream_intent",
        lambda **kwargs: called.append(kwargs),
    )

    with pytest.raises(ProjectSettingsStale, match="hierarchy changed"):
        apply_country_plan_fan_out(
            _Connection(),
            project_id="proj_1",
            change_set_id="pcset_1",
            proposals=[_proposal(version_id="mdv_old")],
            actor="person_1",
            enabled=True,
            loaded_modules=[object()],
        )

    assert called == []


def test_country_deactivation_compiles_global_without_requiring_live_hierarchy(
    monkeypatch,
):
    import core.datastream_intents as datastream_intents
    from core.capability_compilers import CountryCompiler
    from core.country_activation import apply_country_plan_fan_out

    monkeypatch.setattr(
        CountryCompiler,
        "project_evidence",
        lambda *_args, **_kwargs: {
            "registry_id": None,
            "hierarchy_version_id": None,
            "evidence_hash": None,
            "markets": [],
        },
    )
    calls = []

    def save(**kwargs):
        calls.append(kwargs)
        return {
            "id": "dsp_global_2",
            "version_number": 2,
            "content_hash": "e" * 64,
            "executable": True,
        }

    monkeypatch.setattr(datastream_intents, "save_datastream_intent", save)
    _patch_candidate_pipeline(monkeypatch)
    proposal = {**_proposal(), "applicability": "not_applicable"}

    apply_country_plan_fan_out(
        _Connection(),
        project_id="proj_1",
        change_set_id="pcset_2",
        proposals=[proposal],
        actor="person_1",
        enabled=False,
        loaded_modules=[object()],
    )

    assert calls[0]["geographic_posture"].mode == "global"

def test_country_fan_out_refuses_a_non_executable_plan_before_candidate_creation(monkeypatch):
    import core.datastream_intents as datastream_intents
    import core.datastream_publication as publication
    from core.capability_compilers import CountryCompiler
    from core.country_activation import CountryActivationError, apply_country_plan_fan_out

    monkeypatch.setattr(
        CountryCompiler,
        "project_evidence",
        lambda *_args, **_kwargs: _country_evidence(),
    )
    monkeypatch.setattr(
        datastream_intents,
        "save_datastream_intent",
        lambda **_kwargs: {
            "id": "dsp_blocked",
            "version_number": 2,
            "content_hash": "d" * 64,
            "executable": False,
        },
    )
    created = []
    monkeypatch.setattr(
        publication,
        "create_execution",
        lambda *_args, **_kwargs: created.append(True),
    )

    with pytest.raises(CountryActivationError, match="non-executable"):
        apply_country_plan_fan_out(
            _Connection(),
            project_id="proj_1",
            change_set_id="pcset_1",
            proposals=[_proposal()],
            actor="person_1",
            enabled=True,
            loaded_modules=[object()],
        )

    assert created == []
