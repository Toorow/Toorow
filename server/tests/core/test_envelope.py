"""Tests for core.envelope — Story 1.5, T4.5.

Covers:
  * schema_version is always "1".
  * alerts is always a list (never null).
  * rows are present in data.
  * _meta.ui.resourceUri matches expected URI (tested at tool call level in integration tests).
  * derive_meta_from_rows correctly extracts freshness and provenance.
"""

from __future__ import annotations

from unittest.mock import patch

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


# ---------------------------------------------------------------------------
# Cross-source freshness: `last_pull` is the OPTIMISTIC end of the range.
#
# `overview.md:63` fixes the rule for Project posture -- "never the newest
# timestamp from one isolated source" -- and core.project_overview obeys it with
# min(). This builder is the other surface and used to publish only the max, so a
# source refreshed minutes ago hid one frozen for days on a figure that adds both.
# ---------------------------------------------------------------------------


def _two_speed_rows():
    return [
        {"connector": "fast-source", "pull_id": "p_fast",
         "loaded_at": "2026-07-31T06:00:00"},
        {"connector": "slow-source", "pull_id": "p_slow",
         "loaded_at": "2026-07-22T06:00:00"},
    ]


def test_freshness_exposes_the_oldest_contributing_source_too():
    from core.envelope import derive_meta_from_rows

    freshness, _ = derive_meta_from_rows(_two_speed_rows(), [])

    assert freshness["last_pull"] == "2026-07-31T06:00:00"
    # The nine-day-old source is no longer invisible.
    assert freshness["complete_through"] == "2026-07-22T06:00:00"
    assert freshness["per_connector"] == {
        "fast-source": "2026-07-31T06:00:00",
        "slow-source": "2026-07-22T06:00:00",
    }


def test_unevaluated_staleness_is_flagged_as_unevaluated():
    """A null nobody computed must not read as "evaluated, and fresh"."""
    from core.envelope import derive_meta_from_rows

    freshness, _ = derive_meta_from_rows(_two_speed_rows(), [])
    assert freshness["stale_since"] is None
    assert freshness["stale_since_evaluated"] is False


def test_card_and_report_builders_declare_staleness_unevaluated():
    """The five hardcoded `stale_since: None` sites say so explicitly.

    core.health_enrichment is the only code that evaluates staleness, and it is
    reached from get_daily_report alone -- never from get_card / get_report, the
    surfaces that get frozen into a Render and shared.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "core"
    for name in ("cards.py", "reports.py"):
        src = (root / name).read_text(encoding="utf-8")
        bare = re.findall(r'"stale_since": None,\s*\n(?!\s*"stale_since_evaluated")', src)
        assert not bare, f"{name} still claims an unevaluated stale_since without saying so"


# ---------------------------------------------------------------------------
# Health enrichment reads the connections that CONTRIBUTED, and the worst wins.
#
# It used to read one row -- `ORDER BY r.created_at LIMIT 1`, the organization's
# oldest credential -- whichever connectors produced the figures. A two-source
# report could therefore be judged on a connection that contributed nothing,
# while a genuinely frozen contributor stayed invisible.
# ---------------------------------------------------------------------------


def _envelope_from(*connectors):
    return {"meta": {"provenance": [{"source_system": c, "pull_id": "p"} for c in connectors]}}


def test_contributing_connectors_are_read_from_the_envelope_provenance():
    from core.health_enrichment import _contributing_connectors

    assert _contributing_connectors(_envelope_from("gsc", "google-ads")) == [
        "google-ads", "gsc",
    ]
    assert _contributing_connectors({"meta": {}}) == []
    assert _contributing_connectors({}) == []


def test_the_worst_contributing_connection_decides_not_the_first():
    from datetime import datetime, timezone

    from core.health_enrichment import _worst_health

    old = datetime(2026, 7, 1, tzinfo=timezone.utc)
    new = datetime(2026, 7, 31, tzinfo=timezone.utc)
    rows = [
        ("ok", new, new),        # a healthy, freshly-pulled connection...
        ("stale", old, old),     # ...must not hide this one.
    ]
    assert _worst_health(rows)[0] == "stale"
    assert _worst_health(list(reversed(rows)))[0] == "stale"


def test_revoked_outranks_stale_and_stale_outranks_ok():
    from datetime import datetime, timezone

    from core.health_enrichment import _worst_health

    t = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert _worst_health([("stale", t, t), ("revoked", t, t)])[0] == "revoked"
    assert _worst_health([("ok", t, t), ("stale", t, t)])[0] == "stale"


def test_a_tie_on_status_breaks_on_the_stalest_fetch():
    from datetime import datetime, timezone

    from core.health_enrichment import _worst_health

    old = datetime(2026, 7, 1, tzinfo=timezone.utc)
    new = datetime(2026, 7, 31, tzinfo=timezone.utc)
    assert _worst_health([("ok", new, new), ("ok", old, old)])[1] == old


def test_an_unknown_health_never_masks_a_known_bad_one():
    """A connection whose poller has not run cannot be the reason, nor hide one."""
    from datetime import datetime, timezone

    from core.health_enrichment import _worst_health

    t = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert _worst_health([(None, None, t), ("revoked", t, t)])[0] == "revoked"


def test_a_row_of_another_shape_declines_rather_than_raising():
    """Best-effort posture: never raise inside a report over an unexpected row."""
    from core.health_enrichment import _worst_health

    assert _worst_health([(0.95,)]) is None
    assert _worst_health([("gsc", 0.95)]) is None
    assert _worst_health([]) is None
    assert _worst_health(None) is None


# ---------------------------------------------------------------------------
# CAV-17: an envelope says WHICH analytical path produced it.
#
# Toorow answers the same business question through two paths reading two
# different relations: this builder reads the `fact_daily_kpi` mart, while
# `core.query_execution` reads the Datastream's published output relation and
# pins a Semantic View + Query Spec version. Nothing linked them, so two numbers
# for one question were indistinguishable from one number seen twice.
# ---------------------------------------------------------------------------


def test_the_envelope_declares_the_path_and_relation_it_read():
    from core.envelope import build_canonical_envelope

    envelope = build_canonical_envelope(
        rows=[], meta_freshness={}, meta_provenance=[],
        date_range={"start": "2026-07-01", "end": "2026-07-02"}, connectors=[],
    )
    path = envelope["meta"]["analytical_path"]

    assert path["path"] == "mart"
    assert path["relation"] == "fact_daily_kpi"
    # It must NOT claim to be the governed Result path.
    assert path["governed_result"] is False


def test_the_declaration_is_a_copy_so_one_envelope_cannot_mutate_another():
    from core.envelope import ANALYTICAL_PATH_MART, build_canonical_envelope

    first = build_canonical_envelope(
        rows=[], meta_freshness={}, meta_provenance=[], date_range={}, connectors=[],
    )
    first["meta"]["analytical_path"]["relation"] = "tampered"

    second = build_canonical_envelope(
        rows=[], meta_freshness={}, meta_provenance=[], date_range={}, connectors=[],
    )
    assert second["meta"]["analytical_path"]["relation"] == "fact_daily_kpi"
    assert ANALYTICAL_PATH_MART["relation"] == "fact_daily_kpi"


class _Cursor:
    """Minimal cursor: records SQL, answers nothing. Enough to run an execution."""

    def __init__(self):
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.cursors: list[_Cursor] = []

    def cursor(self):
        cur = _Cursor()
        self.cursors.append(cur)
        return cur

    @property
    def calls(self):
        return [(sql, params) for cur in self.cursors for sql, params in cur.executed]


def test_the_governed_path_pins_what_the_mart_path_cannot():
    """The asymmetry this field exists to expose, asserted from both sides.

    This test used to read `inspect.getsource(query_execution)` and assert that
    the strings `semantic_view_version_id`, `query_spec_version_id` and
    `relation` appeared somewhere in it -- three substrings that survive any
    rewrite, including one that stops writing them to the Result. What is checked
    here instead is the artefact a reader can actually inspect: the manifest a
    real execution PERSISTS, and the mart envelope that persists none of it.
    """
    import json

    from core import query_execution

    conn = _Conn()
    plan = {
        "relation": "ds_output_relation",
        "columns": {"sc_clicks": "clicks", "sc_date": "date"},
        "grain": ["date"],
        "datastream_id": "ds_EXAMPLE",
        "mapping_version_id": "dmap_1",
        "pull_id": "pull_1",
    }
    spec = {
        "measures": [{"id": "sc_clicks", "version_id": "scv_clicks"}],
        "dimensions": [{"id": "sc_date", "version_id": "scv_date"}],
        "filters": [], "sort": [], "grain": "day", "row_limit": 500, "time": {},
    }
    with (
        patch.object(query_execution, "resolve_physical_plan", return_value=plan),
        patch("core.warehouse._db_mode", return_value="duckdb"),
        patch("core.warehouse._query_duckdb", return_value=[{"date": "2026-07-01", "clicks": 7}]),
    ):
        outcome = query_execution.run_execution(
            conn,
            attempt={"attempt_id": "qea_1", "result_id": "qr_1", "query_spec_version_id": "qsv_1"},
            org_id="org_1",
            project_id="proj_EXAMPLE",
            spec=spec,
            semantic_view_version_id="sv_ver_1",
        )
    assert outcome["outcome"] == "success"

    payload = next(
        params for sql, params in conn.calls if "INSERT INTO app.query_result_payloads" in sql
    )
    manifest = json.loads(payload[5])
    # The governed path pins all three, on the Result, for good.
    assert manifest["semantic_view_version_id"] == "sv_ver_1"
    assert manifest["query_spec_version_id"] == "qsv_1"
    assert manifest["relation"] == "ds_output_relation"

    # The mart path pins none of them, and says so rather than staying silent.
    mart = build_canonical_envelope(
        rows=[], meta_freshness={}, meta_provenance=[],
        date_range={"start": "2026-07-01", "end": "2026-07-01"}, connectors=[],
    )
    for pin in ("semantic_view_version_id", "query_spec_version_id"):
        assert pin not in mart["meta"], f"the mart envelope must not claim to pin {pin}"
    assert mart["meta"]["analytical_path"]["governed_result"] is False
    assert mart["meta"]["analytical_path"]["relation"] != manifest["relation"], (
        "the two paths read the same relation; the disclosure would be describing "
        "a divergence that no longer exists"
    )
