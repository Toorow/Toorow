"""Story 25.8 — core queue catalog_driven selection resolution (unit).

Covers the two surgical helpers added to core.queue without a DB:
  - _capability_report_for_profile resolves the right capability report;
  - _resolve_catalog_selection returns None for exact_bundle (bit-identical
    dispatch), a resolved selection for catalog_driven, and raises
    InvalidRequestError on a drifted selection (the drift signal).
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import queue  # noqa: E402
from core.pull_errors import InvalidRequestError  # noqa: E402


def _manifest_with_modes():
    return {
        "source_capabilities": {
            "reports": [
                {"id": "campaign_daily", "selection_mode": "exact_bundle"},
                {"id": "catalog_daily", "selection_mode": "catalog_driven"},
            ]
        }
    }


def test_capability_report_for_profile_resolves_by_id():
    manifest = _manifest_with_modes()
    report = queue._capability_report_for_profile(manifest, "catalog_daily")
    assert report["selection_mode"] == "catalog_driven"


def test_capability_report_for_profile_none_returns_first():
    manifest = _manifest_with_modes()
    report = queue._capability_report_for_profile(manifest, None)
    assert report["id"] == "campaign_daily"


def test_resolve_catalog_selection_none_for_exact_bundle():
    """exact_bundle -> None: the pull is called with no selection= kwarg (bit-identical)."""
    report = {"id": "campaign_daily", "selection_mode": "exact_bundle"}
    assert queue._resolve_catalog_selection("meta-ads", report, {}) is None


def test_resolve_catalog_selection_default_for_catalog_driven():
    """catalog_driven + no job selection -> the catalog tier-core default resolves."""
    report = {"id": "catalog_daily", "selection_mode": "catalog_driven"}
    resolved = queue._resolve_catalog_selection("meta-ads", report, {})
    assert resolved is not None
    assert "spend" in resolved["metrics"]  # meta-ads tier-core scalar
    assert resolved["source_fields"]["spend"] == "spend"


def test_resolve_catalog_selection_drift_raises_invalid_request():
    """A job selection referencing an unknown field id raises InvalidRequestError."""
    report = {"id": "catalog_daily", "selection_mode": "catalog_driven"}
    job = {"selection": {"metrics": ["ghost_metric_xyz"], "dimensions": []}}
    with pytest.raises(InvalidRequestError) as exc:
        queue._resolve_catalog_selection("meta-ads", report, job)
    assert exc.value.error_class == "invalid_request"
    assert "ghost_metric_xyz" in str(exc.value)


def test_resolve_catalog_selection_missing_catalog_raises_invalid_request():
    """A catalog_driven profile on a module without api_catalog.json is a refusal."""
    report = {"id": "catalog_daily", "selection_mode": "catalog_driven"}
    with pytest.raises(InvalidRequestError):
        queue._resolve_catalog_selection("not-a-real-module", report, {})


# ---------------------------------------------------------------------------
# The plan reaches the job (repair 7 of reviews/audit-2026-08-17/13-connecteurs.md)
# ---------------------------------------------------------------------------
#
# `_resolve_catalog_selection` read `job.get("selection")` from a job row that has
# no such column: `app.pull_jobs` carries no JSONB at all. So the answer was
# always None and every catalog_driven pull extracted the catalog's tier-core
# default -- while the Processing selector composed a choice, `normalize_intent`
# normalized it and `save_datastream_intent` wrote it, versioned and immutable,
# into `app.datastream_plan_versions.normalized_payload` at `$.source.selection`.
# The selection was stored, versioned and displayed, and changed nothing.
#
# It is read from the PLAN, like `_resolve_datastream_profile` and
# `_resolve_selected_account` next to it, because the plan is where the answer is
# authoritative and a job is only a request to run one.


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row


class _FakeConn:
    """One canned row, and a record of what was asked for."""

    def __init__(self, row):
        self._cursor = _FakeCursor(row)

    def cursor(self):
        return self._cursor


def _plan(selection):
    return {"source": {"selection": selection}}


def test_plan_selection_reaches_the_pull_for_a_catalog_driven_profile():
    """The operator's choice is what gets extracted -- not the tier-core default."""
    report = {"id": "catalog_daily", "selection_mode": "catalog_driven"}
    conn = _FakeConn(({"source": {"selection": {"metrics": ["clicks"], "dimensions": []}}},))
    resolved = queue._resolve_catalog_selection(
        "meta-ads", report, {"datastream_id": "ds_01TEST"}, conn
    )
    assert resolved["metrics"] == ["clicks"]
    # The tier-core default carries several metrics; a selection of one proves the
    # plan won rather than the default happening to agree.
    default = queue._resolve_catalog_selection("meta-ads", report, {})
    assert default["metrics"] != ["clicks"]


def test_plan_selection_is_read_as_json_when_the_driver_returns_text():
    """psycopg hands back JSONB as str on some paths; the reader must not care."""
    import json

    report = {"id": "catalog_daily", "selection_mode": "catalog_driven"}
    payload = json.dumps(_plan({"metrics": ["clicks"], "dimensions": []}))
    conn = _FakeConn((payload,))
    resolved = queue._resolve_catalog_selection(
        "meta-ads", report, {"datastream_id": "ds_01TEST"}, conn
    )
    assert resolved["metrics"] == ["clicks"]


def test_an_explicit_job_selection_still_wins_over_the_plan():
    """A caller that hands a selection directly is not overridden by the lookup."""
    report = {"id": "catalog_daily", "selection_mode": "catalog_driven"}
    conn = _FakeConn((_plan({"metrics": ["clicks"], "dimensions": []}),))
    job = {"datastream_id": "ds_01TEST", "selection": {"metrics": ["spend"], "dimensions": []}}
    resolved = queue._resolve_catalog_selection("meta-ads", report, job, conn)
    assert resolved["metrics"] == ["spend"]


def test_a_plan_that_chose_nothing_falls_back_to_the_catalog_default():
    """An empty choice is not a selection of no fields -- it is no choice at all."""
    report = {"id": "catalog_daily", "selection_mode": "catalog_driven"}
    conn = _FakeConn((_plan({"metrics": [], "dimensions": []}),))
    resolved = queue._resolve_catalog_selection(
        "meta-ads", report, {"datastream_id": "ds_01TEST"}, conn
    )
    default = queue._resolve_catalog_selection("meta-ads", report, {})
    assert resolved["metrics"] == default["metrics"]


def test_a_job_without_a_datastream_keeps_the_legacy_default():
    """The per-connection path has no plan to read, and must stay bit-identical."""
    report = {"id": "catalog_daily", "selection_mode": "catalog_driven"}
    conn = _FakeConn((_plan({"metrics": ["clicks"], "dimensions": []}),))
    resolved = queue._resolve_catalog_selection("meta-ads", report, {}, conn)
    default = queue._resolve_catalog_selection("meta-ads", report, {})
    assert resolved["metrics"] == default["metrics"]


def test_a_drifted_plan_selection_refuses_instead_of_silently_dropping_the_field():
    """A plan naming a field the catalog no longer exposes is the drift signal."""
    report = {"id": "catalog_daily", "selection_mode": "catalog_driven"}
    conn = _FakeConn((_plan({"metrics": ["ghost_metric_xyz"], "dimensions": []}),))
    with pytest.raises(InvalidRequestError) as exc:
        queue._resolve_catalog_selection(
            "meta-ads", report, {"datastream_id": "ds_01TEST"}, conn
        )
    assert "ghost_metric_xyz" in str(exc.value)


def test_a_failing_plan_read_never_fails_the_pull():
    """The lookup is best-effort, like its two siblings: it degrades to the default."""

    class _Exploding:
        def cursor(self):
            raise RuntimeError("connection lost")

    report = {"id": "catalog_daily", "selection_mode": "catalog_driven"}
    resolved = queue._resolve_catalog_selection(
        "meta-ads", report, {"datastream_id": "ds_01TEST"}, _Exploding()
    )
    default = queue._resolve_catalog_selection("meta-ads", report, {})
    assert resolved["metrics"] == default["metrics"]
