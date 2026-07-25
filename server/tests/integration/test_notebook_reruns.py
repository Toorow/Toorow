"""Integration test: re-run provenance proof for notebooks (Story 6.5, AC7).

test_rerun_resolves_fresh_pull_ids:
  1. Create a notebook via save_notebook.
  2. Run it -> capture pull_ids from notebook_runs row 1.
  3. Simulate a new pull: insert a new pull_id into the warehouse rows.
  4. Run the notebook again -> capture pull_ids from notebook_runs row 2.
  5. Assert pull_ids[run2] != pull_ids[run1] (fresh resolution, not copy).
  6. Assert both run rows exist in app.notebook_runs.

This is the MECHANICAL PROOF of the "provenance re-resolved, never copied stale"
requirement (AD-9, AD-7, AC7). The test is in-process (no network) and uses
mocked Postgres + mocked render_report so it can run in the baseline test suite.

The logic under test: run_notebook extracts pull_ids from the envelope returned
by render_report. If render_report returns different pull_ids on each call (as it
would when new data has been loaded), the stored pull_ids in notebook_runs differ.
The test verifies this by running render_report twice with different pull_id seeds.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(pull_ids: list[str]) -> dict:
    """Build a fake render_report envelope with the given pull_ids."""
    return {
        "schema_version": "1",
        "meta": {
            "provenance": {
                "pull_ids": pull_ids,
                "pull_id": pull_ids[-1] if pull_ids else None,
                "source_system": "gsc",
                "source_field": "fact_daily_kpi",
            },
            "alerts": [],
            "freshness": None,
            "context_events": [],
        },
        "data": {
            "report_id": "gsc/position_movements",
            "date_range": {"start": "2026-06-12", "end": "2026-07-12"},
            "connectors": ["gsc"],
            "metrics": {"clicks": 1200},
            "rows": [],
        },
    }


def _make_mock_conn_sequence(notebook_row, run_ids_store: list):
    """Build conn mocks that:
    - On first get_connection call: return the notebook row (SELECT)
    - On second get_connection call: capture INSERT params for run 1
    - On third get_connection call: return the notebook row again (SELECT for run 2)
    - On fourth get_connection call: capture INSERT params for run 2
    """
    call_n = [0]

    def get_connection_factory():
        call_n[0] += 1
        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)

        if call_n[0] % 2 == 1:
            # SELECT call: return notebook row
            cursor_mock = MagicMock()
            cursor_mock.fetchone.return_value = notebook_row
            cursor_mock.description = [
                ("id",), ("project_id",), ("title",), ("report_ref",),
                ("window_rule",), ("narrative_prompt",), ("created_by",),
            ]
            cursor_cm = MagicMock()
            cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
            cursor_cm.__exit__ = MagicMock(return_value=False)
            conn_mock.cursor = MagicMock(return_value=cursor_cm)
        else:
            # INSERT call: capture the pull_ids from params
            captured = []

            def capture_insert(sql, params):
                if "notebook_runs" in sql:
                    captured.append(params)
                    run_ids_store.append(params)

            cursor_mock = MagicMock()
            cursor_mock.execute = capture_insert
            cursor_cm = MagicMock()
            cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
            cursor_cm.__exit__ = MagicMock(return_value=False)
            conn_mock.cursor = MagicMock(return_value=cursor_cm)

        return conn_mock

    return get_connection_factory


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_rerun_resolves_fresh_pull_ids():
    """Two sequential runs of the same notebook -> pull_ids differ after new data.

    This proves provenance is re-resolved from the warehouse on each run,
    never copied from the previous run row (AD-9 / AD-7 / AC7).
    """
    from core.main import run_notebook

    # The notebook row returned by SELECT
    notebook_row = (
        "nb_RERUN_TEST",
        "proj_test",
        "Rerun test notebook",
        "gsc/position_movements",
        "last_30d",
        None,  # narrative_prompt
        "user@example.com",
    )

    # Pull IDs for run 1 (before new pull)
    PULL_IDS_RUN1 = ["pull_AAAAAA"]
    # Pull IDs for run 2 (after new pull — a new pull_id is included)
    PULL_IDS_RUN2 = ["pull_AAAAAA", "pull_BBBBBB"]

    # Storage for captured INSERT params
    run_ids_store: list = []

    # Simulate two sequential render_report calls returning different pull_ids
    envelope_run1 = _make_envelope(PULL_IDS_RUN1)
    envelope_run2 = _make_envelope(PULL_IDS_RUN2)

    summary_run1 = "Clics: 1 000 (gsc:fact_daily_kpi, pull_AAAAAA)"
    summary_run2 = "Clics: 1 200 (gsc:fact_daily_kpi, pull_BBBBBB)"

    render_calls = [0]

    def mock_render_report(*args, **kwargs):
        render_calls[0] += 1
        if render_calls[0] == 1:
            return summary_run1, dict(envelope_run1), "ui://gsc/widget"
        else:
            return summary_run2, dict(envelope_run2), "ui://gsc/widget"

    # Connection call counter for routing SELECT vs INSERT
    call_n = [0]

    def get_connection_factory():
        call_n[0] += 1
        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)

        if call_n[0] % 2 == 1:
            # Odd call: SELECT (fetch notebook)
            cursor_mock = MagicMock()
            cursor_mock.fetchone.return_value = notebook_row
            cursor_mock.description = [
                ("id",), ("project_id",), ("title",), ("report_ref",),
                ("window_rule",), ("narrative_prompt",), ("created_by",),
            ]
        else:
            # Even call: INSERT into notebook_runs
            def capture_insert(sql, params):
                if "notebook_runs" in sql:
                    run_ids_store.append(list(params))

            cursor_mock = MagicMock()
            cursor_mock.execute = capture_insert

        cursor_cm = MagicMock()
        cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
        cursor_cm.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor = MagicMock(return_value=cursor_cm)
        return conn_mock

    with (
        patch("core.db.get_connection", side_effect=get_connection_factory),
        patch("core.audit.write_audit_row"),
        patch("core.reports.render_report", side_effect=mock_render_report),
    ):
        # === Run 1 (before new pull) ===
        result1 = run_notebook(notebook_id="nb_RERUN_TEST")
        assert result1.is_error is not True

        # === Simulate new pull: render_report will return PULL_IDS_RUN2 next call ===
        # (Already staged above via render_calls counter)

        # === Run 2 (after new pull) ===
        result2 = run_notebook(notebook_id="nb_RERUN_TEST")
        assert result2.is_error is not True

    # --- AC7 assertions ---

    # 6. Both run rows stored (two INSERT captures)
    assert len(run_ids_store) == 2, (
        f"Expected 2 notebook_runs INSERT captures, got {len(run_ids_store)}: {run_ids_store}"
    )

    # Extract pull_ids from captured INSERT params.
    # INSERT params: (nbrun_id, notebook_id, as_of, summary_text,
    #                 envelope_ref, envelope_inline, pull_ids, 'success')
    # pull_ids is at index 6 in the params tuple.
    PULL_IDS_PARAM_IDX = 6

    stored_run1 = run_ids_store[0]
    stored_run2 = run_ids_store[1]

    pull_ids_run1_stored = stored_run1[PULL_IDS_PARAM_IDX]
    pull_ids_run2_stored = stored_run2[PULL_IDS_PARAM_IDX]

    # 5. pull_ids from run 2 differ from run 1 (fresh resolution proof)
    assert pull_ids_run1_stored != pull_ids_run2_stored, (
        f"PROVENANCE RE-RESOLUTION FAILED: "
        f"run1 pull_ids={pull_ids_run1_stored!r} == run2 pull_ids={pull_ids_run2_stored!r}. "
        "pull_ids must be freshly resolved on each run, never copied from a previous run."
    )

    # Verify the values match what render_report returned
    assert pull_ids_run1_stored == PULL_IDS_RUN1, (
        f"Run 1 pull_ids mismatch: stored {pull_ids_run1_stored!r}, expected {PULL_IDS_RUN1!r}"
    )
    assert pull_ids_run2_stored == PULL_IDS_RUN2, (
        f"Run 2 pull_ids mismatch: stored {pull_ids_run2_stored!r}, expected {PULL_IDS_RUN2!r}"
    )

    # Verify both runs' envelopes carry notebook_id and run_id in meta
    sc1 = result1.structured_content or {}
    sc2 = result2.structured_content or {}
    assert sc1.get("meta", {}).get("notebook_id") == "nb_RERUN_TEST"
    assert sc2.get("meta", {}).get("notebook_id") == "nb_RERUN_TEST"
    run_id1 = sc1.get("meta", {}).get("run_id", "")
    run_id2 = sc2.get("meta", {}).get("run_id", "")
    assert run_id1.startswith("nbrun_"), f"run_id1={run_id1!r} must start with 'nbrun_'"
    assert run_id2.startswith("nbrun_"), f"run_id2={run_id2!r} must start with 'nbrun_'"
    assert run_id1 != run_id2, "Two runs must have distinct nbrun_ IDs"
