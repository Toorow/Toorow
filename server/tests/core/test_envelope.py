"""Tests for core.envelope — Story 1.5, T4.5.

Covers:
  * schema_version is always "1".
  * alerts is always a list (never null).
  * rows are present in data.
  * _meta.ui.resourceUri matches expected URI (tested at tool call level in integration tests).
  * derive_meta_from_rows correctly extracts freshness and provenance.
"""

from __future__ import annotations

from core.envelope import build_canonical_envelope, derive_meta_from_rows

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _sample_rows() -> list[dict]:
    return [
        {
            "date": "2026-04-01",
            "connector": "my-connector",
            "metric": "sessions",
            "breakdown_dimension": "device_category",
            "breakdown_value": "desktop",
            "value": 500.0,
            "pull_id": "pull_01TESTULID",
            "loaded_at": "2026-06-30T12:00:00",
        },
        {
            "date": "2026-04-02",
            "connector": "my-connector",
            "metric": "active_users",
            "breakdown_dimension": "device_category",
            "breakdown_value": "mobile",
            "value": 300.0,
            "pull_id": "pull_01TESTULID",
            "loaded_at": "2026-06-30T12:00:00",
        },
    ]


def _sample_freshness() -> dict:
    return {"last_pull": "2026-06-30T12:00:00", "cadence_hours": 24, "stale_since": None}


def _sample_provenance() -> list[dict]:
    return [{"source_system": "my-connector", "pull_id": "pull_01TESTULID"}]


def _sample_date_range() -> dict:
    return {"start": "2026-04-01", "end": "2026-06-30"}


# ---------------------------------------------------------------------------
# schema_version
# ---------------------------------------------------------------------------

def test_envelope_schema_version_is_always_1():
    """schema_version must always be '1' (AC3 / AD-1 contract)."""
    envelope = build_canonical_envelope(
        rows=_sample_rows(),
        meta_freshness=_sample_freshness(),
        meta_provenance=_sample_provenance(),
        date_range=_sample_date_range(),
        connectors=["my-connector"],
    )
    assert envelope["schema_version"] == "1"


# ---------------------------------------------------------------------------
# alerts is always a list
# ---------------------------------------------------------------------------

def test_envelope_alerts_is_always_list():
    """meta.alerts must always be a list, never null (Architecture Spine)."""
    envelope = build_canonical_envelope(
        rows=[],
        meta_freshness=_sample_freshness(),
        meta_provenance=_sample_provenance(),
        date_range=_sample_date_range(),
        connectors=[],
    )
    assert isinstance(envelope["meta"]["alerts"], list)
    assert envelope["meta"]["alerts"] == []


# ---------------------------------------------------------------------------
# rows present in data
# ---------------------------------------------------------------------------

def test_envelope_rows_present_in_data():
    """data.rows must contain the full row list (AC3)."""
    rows = _sample_rows()
    envelope = build_canonical_envelope(
        rows=rows,
        meta_freshness=_sample_freshness(),
        meta_provenance=_sample_provenance(),
        date_range=_sample_date_range(),
        connectors=["my-connector"],
    )
    assert "rows" in envelope["data"]
    assert len(envelope["data"]["rows"]) == len(rows)


def test_envelope_empty_rows():
    """Empty rows → data.rows is empty list, envelope is still valid."""
    envelope = build_canonical_envelope(
        rows=[],
        meta_freshness={"last_pull": None, "cadence_hours": 24, "stale_since": None},
        meta_provenance=[],
        date_range={"start": "2026-01-01", "end": "2026-01-31"},
        connectors=[],
    )
    assert envelope["schema_version"] == "1"
    assert envelope["data"]["rows"] == []
    assert isinstance(envelope["meta"]["alerts"], list)


# ---------------------------------------------------------------------------
# date_range and connectors propagation
# ---------------------------------------------------------------------------

def test_envelope_date_range_propagated():
    """date_range must appear in data (AC3 contract shape)."""
    dr = {"start": "2026-04-01", "end": "2026-06-30"}
    envelope = build_canonical_envelope(
        rows=[],
        meta_freshness=_sample_freshness(),
        meta_provenance=_sample_provenance(),
        date_range=dr,
        connectors=["my-connector"],
    )
    assert envelope["data"]["date_range"] == dr


def test_envelope_connectors_propagated():
    """connectors list must appear in data."""
    envelope = build_canonical_envelope(
        rows=[],
        meta_freshness=_sample_freshness(),
        meta_provenance=_sample_provenance(),
        date_range=_sample_date_range(),
        connectors=["conn-a", "conn-b"],
    )
    assert envelope["data"]["connectors"] == ["conn-a", "conn-b"]


# ---------------------------------------------------------------------------
# derive_meta_from_rows
# ---------------------------------------------------------------------------

def test_derive_meta_extracts_last_pull():
    """derive_meta_from_rows must extract the latest loaded_at as last_pull."""
    rows = [
        {"connector": "my-connector", "pull_id": "pull_01A", "loaded_at": "2026-04-01T00:00:00"},
        {"connector": "my-connector", "pull_id": "pull_01B", "loaded_at": "2026-06-30T12:00:00"},
    ]
    freshness, _ = derive_meta_from_rows(rows, ["my-connector"])
    assert freshness["last_pull"] == "2026-06-30T12:00:00"
    assert freshness["cadence_hours"] == 24
    assert freshness["stale_since"] is None


def test_derive_meta_no_rows():
    """Empty rows → last_pull is None, provenance list is present."""
    freshness, provenance = derive_meta_from_rows([], ["my-connector"])
    assert freshness["last_pull"] is None
    assert isinstance(provenance, list)


def test_derive_meta_provenance_per_connector():
    """Each connector has its own provenance entry."""
    rows = [
        {"connector": "conn-a", "pull_id": "pull_A", "loaded_at": "2026-04-01"},
        {"connector": "conn-b", "pull_id": "pull_B", "loaded_at": "2026-04-01"},
    ]
    _, provenance = derive_meta_from_rows(rows, ["conn-a", "conn-b"])
    source_systems = [p["source_system"] for p in provenance]
    assert "conn-a" in source_systems
    assert "conn-b" in source_systems


# ---------------------------------------------------------------------------
# Story 3.5 (AC8, HG-6) -- confidence parameter (additive, optional)
# ---------------------------------------------------------------------------


def test_envelope_confidence_none_omits_key():
    """confidence=None (default) must NOT add a 'confidence' key to meta (HG-6)."""
    envelope = build_canonical_envelope(
        rows=[],
        meta_freshness=_sample_freshness(),
        meta_provenance=_sample_provenance(),
        date_range=_sample_date_range(),
        connectors=[],
        confidence=None,
    )
    assert "confidence" not in envelope["meta"], (
        "meta.confidence must be absent when confidence=None"
    )


def test_envelope_confidence_present_when_provided():
    """confidence={'completeness': 0.95} must appear as meta.confidence."""
    envelope = build_canonical_envelope(
        rows=[],
        meta_freshness=_sample_freshness(),
        meta_provenance=_sample_provenance(),
        date_range=_sample_date_range(),
        connectors=[],
        confidence={"completeness": 0.95},
    )
    assert "confidence" in envelope["meta"]
    assert envelope["meta"]["confidence"]["completeness"] == 0.95


def test_envelope_confidence_default_is_none():
    """Calling build_canonical_envelope without confidence= omits the key."""
    envelope = build_canonical_envelope(
        rows=[],
        meta_freshness=_sample_freshness(),
        meta_provenance=_sample_provenance(),
        date_range=_sample_date_range(),
        connectors=[],
        # confidence parameter not passed -- tests default=None backward compat
    )
    assert "confidence" not in envelope["meta"]


def test_envelope_existing_callers_unaffected():
    """All existing required fields are still present when confidence is not passed (HG-6)."""
    envelope = build_canonical_envelope(
        rows=_sample_rows(),
        meta_freshness=_sample_freshness(),
        meta_provenance=_sample_provenance(),
        date_range=_sample_date_range(),
        connectors=["my-connector"],
    )
    assert envelope["schema_version"] == "1"
    assert "freshness" in envelope["meta"]
    assert "provenance" in envelope["meta"]
    assert isinstance(envelope["meta"]["alerts"], list)
    assert "rows" in envelope["data"]
