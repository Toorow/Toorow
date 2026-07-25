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
