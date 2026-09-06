"""The final-review composer RUNS -- not a doubled copy of it.

AI-321 (2026-08-28). Story 67.23 extracted `freeze_and_persist_final_review`
out of the REST handler so the MCP door could compose the SAME review. The
extraction left two references to the handler's local `body` inside the new
function, and both callers -- `_prepare_final_review` and the wizard MCP tool --
were tested with the function MOCKED, so nothing ever executed its body. In
production every final review of a file-source Datastream answered 503
`preconfiguration_unavailable` (a `NameError`, swallowed by the catch-all),
no plan version was ever recorded, and the governed import ran without a
gate, without a lock and without a ledger row: G9 2026-08-28, five failures
from one cause.

This file executes the composer with its collaborators doubled ONE LEVEL
DOWN, so the function's own lines run. The rule it pins is the one this
repository already names for instruments: a function whose only tests mock
it has no test.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from core import datastream_preconfiguration_api as api


def _conn_with_project(row):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = row
    return conn


@pytest.fixture
def collaborators(monkeypatch):
    calls: dict[str, dict] = {}

    def record(name, result):
        def _impl(*args, **kwargs):
            calls[name] = kwargs
            return result

        return _impl

    preview = {
        "preview_ref": "prev_1",
        "proposal_ref": "prop_1",
        "observation_ref": "obs_1",
        "is_stale": False,
    }
    monkeypatch.setattr(api, "read_preview", record("read_preview", preview))
    monkeypatch.setattr(api, "read_proposal", record("read_proposal", {"is_stale": False}))
    monkeypatch.setattr(api, "read_observation", record("read_observation", {"obs": 1}))
    monkeypatch.setattr(api, "read_draft", record("read_draft", {"operator_input": {}}))
    monkeypatch.setattr(api, "freeze_final_review", record("freeze", {"frozen": True}))
    monkeypatch.setattr(api, "_load_connector_capabilities", record("caps", {}))
    monkeypatch.setattr(
        api, "compile_materialization_contract", record("compile", {"contract": True})
    )
    monkeypatch.setattr(api, "persist_final_review", record("persist", {"final_review_id": "fr_1"}))
    return calls


def test_the_composer_reads_the_preview_it_was_given_and_freezes_the_acknowledgements(
    collaborators,
):
    conn = _conn_with_project(("org_1", "Europe/Paris"))

    result = api.freeze_and_persist_final_review(
        conn,
        project_id="proj_EXAMPLE",
        draft_id="dsd_1",
        preview_id="prev_1",
        acknowledged_warning_ids=["w1", 7],
        actor="owner@example.com",
    )

    assert result == {"final_review_id": "fr_1"}
    # The two lines that used to raise NameError: the preview id and the
    # acknowledgements come from the PARAMETERS, not from a request body.
    assert collaborators["read_preview"]["preview_id"] == "prev_1"
    assert collaborators["freeze"]["acknowledged_warning_ids"] == ["w1", "7"]
    assert collaborators["compile"]["timezone_name"] == "Europe/Paris"
    assert collaborators["compile"]["timezone_origin"] == "project_configuration"
    assert collaborators["persist"]["preview_id"] == "prev_1"


def test_an_unconfirmed_timezone_is_labelled_not_filled(collaborators):
    conn = _conn_with_project(("org_1", None))

    api.freeze_and_persist_final_review(
        conn,
        project_id="proj_EXAMPLE",
        draft_id="dsd_1",
        preview_id="prev_1",
        acknowledged_warning_ids=[],
        actor="owner@example.com",
    )

    assert collaborators["compile"]["timezone_name"] == "UTC"
    assert collaborators["compile"]["timezone_origin"] == "scheduling_default_unconfirmed"


def test_stale_evidence_is_refused_before_anything_is_frozen(collaborators, monkeypatch):
    monkeypatch.setattr(
        api,
        "read_preview",
        lambda *a, **k: {
            "preview_ref": "prev_1",
            "proposal_ref": "prop_1",
            "observation_ref": "obs_1",
            "is_stale": True,
        },
    )
    conn = _conn_with_project(("org_1", "UTC"))

    with pytest.raises(api.ActivationValidationError):
        api.freeze_and_persist_final_review(
            conn,
            project_id="proj_EXAMPLE",
            draft_id="dsd_1",
            preview_id="prev_1",
            acknowledged_warning_ids=[],
            actor="owner@example.com",
        )
    assert "freeze" not in collaborators
    assert "persist" not in collaborators
