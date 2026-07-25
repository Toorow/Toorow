"""Offline pure-engine tests for the cross-source day-offset SIGNAL (Story 39.8).

No DB, no network -- ``core.timezone_signal.check_cross_source_day_offset`` is a pure function.
Covers AC1 (advisory names timezones + metric), AC2 (lever posture), AC3 (no false positives),
§B.2 (best-effort offset label, equality on IANA names), §B.4 (defer to 39.7's GAP -- exclude
unknown-tz streams, never fabricate UTC), determinism, and AD-2 (no provider vocab in core).
"""

from __future__ import annotations

from pathlib import Path

from core.timezone_signal import (
    SEVERITY_ADVISORY,
    SIGNAL_TIMEZONE_DAY_OFFSET,
    check_cross_source_day_offset,
)


def _s(datastream, tz, *, has_lever=False, lever_hint=None):
    return {
        "datastream": datastream,
        "report_timezone": tz,
        "has_lever": has_lever,
        "lever_hint": lever_hint,
    }


# ---------------------------------------------------------------------------
# AC3 -- no false positives (1, 2, 3)
# ---------------------------------------------------------------------------


def test_same_timezone_two_streams_returns_none():  # 1
    out = check_cross_source_day_offset(
        metric="revenue",
        streams=[_s("a", "Europe/Paris"), _s("b", "Europe/Paris")],
    )
    assert out is None


def test_single_stream_returns_none():  # 2
    out = check_cross_source_day_offset(metric="revenue", streams=[_s("a", "Europe/Paris")])
    assert out is None


def test_empty_streams_returns_none():  # 3
    assert check_cross_source_day_offset(metric="revenue", streams=[]) is None


# ---------------------------------------------------------------------------
# AC1 -- the advisory (4, 6, 7)
# ---------------------------------------------------------------------------


def test_two_distinct_timezones_signal():  # 4
    out = check_cross_source_day_offset(
        metric="revenue",
        streams=[_s("a", "Europe/Paris"), _s("b", "UTC")],
    )
    assert out is not None
    assert out["code"] == SIGNAL_TIMEZONE_DAY_OFFSET
    assert out["distinct_timezones"] == ["Europe/Paris", "UTC"]  # sorted
    assert out["severity"] == SEVERITY_ADVISORY
    assert out["severity"] != "refusal"


def test_realignable_always_false():  # 5
    out = check_cross_source_day_offset(
        metric="clicks",
        streams=[_s("a", "Europe/Paris"), _s("b", "America/New_York"), _s("c", "UTC")],
    )
    assert out is not None
    assert out["realignable"] is False


def test_three_distinct_timezones_sorted_deduped():  # 6
    out = check_cross_source_day_offset(
        metric="impressions",
        streams=[
            _s("a", "UTC"),
            _s("b", "Europe/Paris"),
            _s("c", "America/New_York"),
            _s("d", "Europe/Paris"),  # duplicate name
        ],
    )
    assert out is not None
    assert out["distinct_timezones"] == [
        "America/New_York",
        "Europe/Paris",
        "UTC",
    ]


def test_metric_and_streams_echoed():  # 7
    out = check_cross_source_day_offset(
        metric="sessions",
        streams=[_s("ga4_web", "Europe/Paris"), _s("gam_net", "UTC")],
    )
    assert out is not None
    assert out["metric"] == "sessions"
    assert set(out["affected_streams"]) == {"ga4_web", "gam_net"}
    assert {s["datastream"] for s in out["report_timezones"]} == {"ga4_web", "gam_net"}


# ---------------------------------------------------------------------------
# §B.4 -- defer to 39.7's GAP: exclude unknown-tz streams, never fabricate UTC (8, 9)
# ---------------------------------------------------------------------------


def test_one_unknown_plus_one_known_returns_none():  # 8
    out = check_cross_source_day_offset(
        metric="revenue",
        streams=[_s("a", None), _s("b", "Europe/Paris")],
    )
    assert out is None  # only 1 known tz remains after exclusion


def test_unknown_excluded_signal_fires_on_two_known():  # 9
    out = check_cross_source_day_offset(
        metric="revenue",
        streams=[_s("unknown", None), _s("a", "Europe/Paris"), _s("b", "UTC")],
    )
    assert out is not None
    assert out["distinct_timezones"] == ["Europe/Paris", "UTC"]
    # The unknown stream is EXCLUDED, never coerced to UTC.
    assert "unknown" not in out["affected_streams"]
    assert all(s["report_timezone"] for s in out["report_timezones"])


def test_blank_timezone_excluded():  # 8b -- blank string is fail-closed like None
    out = check_cross_source_day_offset(
        metric="revenue",
        streams=[_s("a", "   "), _s("b", "Europe/Paris")],
    )
    assert out is None


# ---------------------------------------------------------------------------
# §B.2 -- equality on IANA names (not computed offset) (10)
# ---------------------------------------------------------------------------


def test_distinct_names_sharing_offset_still_fire():  # 10
    # Europe/Paris and Europe/Brussels share the CET offset seasonally but are DISTINCT IANA
    # names -> the signal fires (equality is on the NAME, the captured provenance).
    out = check_cross_source_day_offset(
        metric="revenue",
        streams=[_s("a", "Europe/Paris"), _s("b", "Europe/Brussels")],
    )
    assert out is not None
    assert out["distinct_timezones"] == ["Europe/Brussels", "Europe/Paris"]


# ---------------------------------------------------------------------------
# AC2 -- lever posture (11)
# ---------------------------------------------------------------------------


def test_lever_posture_per_stream():  # 11
    out = check_cross_source_day_offset(
        metric="revenue",
        streams=[
            _s("no_lever", "UTC"),  # GAM-like: no lever
            _s("has_lever", "Europe/Paris", has_lever=True, lever_hint="report_settings"),
        ],
    )
    assert out is not None
    by_ds = {s["datastream"]: s for s in out["report_timezones"]}
    assert by_ds["no_lever"]["has_lever"] is False
    assert by_ds["no_lever"]["lever_hint"] is None
    assert by_ds["has_lever"]["has_lever"] is True
    assert by_ds["has_lever"]["lever_hint"] == "report_settings"  # surfaced verbatim


def test_has_lever_without_hint_degrades_to_no_hint():
    out = check_cross_source_day_offset(
        metric="revenue",
        streams=[_s("a", "UTC"), _s("b", "Europe/Paris", has_lever=True, lever_hint=None)],
    )
    assert out is not None
    by_ds = {s["datastream"]: s for s in out["report_timezones"]}
    assert by_ds["b"]["lever_hint"] is None


# ---------------------------------------------------------------------------
# §B.2 -- best-effort offset label (12)
# ---------------------------------------------------------------------------


def test_offset_label_computed_and_null_on_unresolvable():  # 12
    out = check_cross_source_day_offset(
        metric="revenue",
        streams=[_s("a", "UTC"), _s("b", "Not/AZone")],
    )
    assert out is not None
    by_ds = {s["datastream"]: s for s in out["report_timezones"]}
    assert by_ds["a"]["utc_offset_label"] == "UTC+0"
    # Unresolvable IANA string -> label None, raw name kept (never invented).
    assert by_ds["b"]["utc_offset_label"] is None
    assert by_ds["b"]["report_timezone"] == "Not/AZone"


# ---------------------------------------------------------------------------
# AC1 -- message interpolates timezones + metric + no-realign statement (13)
# ---------------------------------------------------------------------------


def test_message_names_timezones_metric_and_no_realign():  # 13
    out = check_cross_source_day_offset(
        metric="revenue",
        streams=[_s("a", "Europe/Paris"), _s("b", "UTC")],
    )
    assert out is not None
    msg = out["message"]
    assert "Europe/Paris" in msg
    assert "UTC" in msg
    assert "revenue" in msg
    assert "realignement" in msg.lower() or "realign" in msg.lower()
    assert "DATE" in msg


# ---------------------------------------------------------------------------
# Determinism (14)
# ---------------------------------------------------------------------------


def test_deterministic_byte_identical():  # 14
    streams = [_s("z", "UTC"), _s("a", "Europe/Paris"), _s("m", "America/New_York")]
    a = check_cross_source_day_offset(metric="revenue", streams=list(streams))
    b = check_cross_source_day_offset(metric="revenue", streams=list(streams))
    assert a == b
    # per-stream order is sorted by datastream then tz
    assert [s["datastream"] for s in a["report_timezones"]] == ["a", "m", "z"]


# ---------------------------------------------------------------------------
# AD-2 -- no provider vocab in core (15)
# ---------------------------------------------------------------------------


def test_ad2_no_provider_names_in_engine():  # 15
    src = Path(__file__).resolve().parents[2] / "core" / "timezone_signal.py"
    raw = src.read_text(encoding="utf-8")
    text = raw.lower()
    for provider in (
        "meta-ads", "google-ads", "google-ad-manager", "tiktok", "linkedin",
        "shopify", "stripe", "woocommerce", "square", "supermetrics", "nango",
    ):
        assert provider not in text, f"provider name {provider!r} leaked into timezone_signal.py"
    # No provider FIELD names either (case-sensitive: 'timeZone' is GAM's metadata field;
    # 'currencyCode' is the money field). Our own words are lowercase 'timezone', so the
    # mixed-case provider fields are a precise, non-overlapping check.
    assert "timeZone" not in raw
    assert "currencyCode" not in raw
