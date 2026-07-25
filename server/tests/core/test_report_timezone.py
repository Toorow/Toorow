"""Tests for Story 39.7 -- the per-source time-context CAPTURE contract.

Offline (no DB, no dbt, no network): the contract brain is pure, so every AC is provable
directly:
  * is_valid_iana: real IANA zones (incl. offset zones + UTC) true; null/empty/offset-strings/
    made-up names false (stdlib zoneinfo, no third-party tz lib).
  * resolve_capture: valid capture => passthrough; locus='fixed' => declared; undetermined +
    fallback='assume' => TAGGED assumption (assumed=True); undetermined + gap/none/no-decl =>
    the typed gap (report_timezone None, gap True).
  * AC3 fail-closed (LOAD-BEARING, E39-NFR02): NO input yields a non-null zone with
    assumed=False unless the capture itself was valid IANA or locus='fixed' -- the never-silent-
    UTC invariant, proven exhaustively over the fallback/none/no-declaration matrix.
  * declared_zone / report_timezone_for_datastream: a declaration DETERMINISTICALLY fixes only
    fixed/assume zones; a live-read locus / none / no declaration => None (never a default).
  * timezone_gap: emits a TIMEZONE_GAP (shape-aligned with CURRENCY_GAP) when no used-by stream
    carries a resolvable zone; None when one does.
  * AD-2 grep: report_timezone.py names no connector/provider and no provider field.

Pattern: env header + no DB, mirrors test_money.py / test_metric_semantics.py.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import report_timezone as rtz  # noqa: E402

# ---------------------------------------------------------------------------
# (a) is_valid_iana -- the stdlib zoneinfo validator.
# ---------------------------------------------------------------------------


def test_is_valid_iana_accepts_real_zones():
    assert rtz.is_valid_iana("America/New_York")
    assert rtz.is_valid_iana("Europe/Paris")
    assert rtz.is_valid_iana("Australia/Eucla")  # a :45 offset zone -- still a real IANA name
    assert rtz.is_valid_iana("UTC")  # UTC is a legitimate IANA zone (not a silent default here)


def test_is_valid_iana_rejects_null_empty_and_bogus():
    assert not rtz.is_valid_iana(None)
    assert not rtz.is_valid_iana("")
    assert not rtz.is_valid_iana("   ")
    assert not rtz.is_valid_iana("Not/AZone")
    assert not rtz.is_valid_iana("GMT+2")  # an offset string is NOT an IANA zone
    assert not rtz.is_valid_iana(123)  # type: ignore[arg-type]  -- non-str => False


# ---------------------------------------------------------------------------
# (b) resolve_capture -- valid captured zone passes through.
# ---------------------------------------------------------------------------


def test_resolve_capture_valid_zone_passthrough():
    declared = {"locus": "network", "fallback": "gap"}
    out = rtz.resolve_capture(declared, "America/New_York")
    assert out == {"report_timezone": "America/New_York", "assumed": False, "gap": False}


def test_resolve_capture_strips_whitespace_on_valid_capture():
    out = rtz.resolve_capture({"locus": "property", "fallback": "gap"}, "  Europe/Paris  ")
    assert out["report_timezone"] == "Europe/Paris"
    assert out["assumed"] is False and out["gap"] is False


# ---------------------------------------------------------------------------
# (c) resolve_capture -- locus='fixed' => declared, deterministic, no gap.
# ---------------------------------------------------------------------------


def test_resolve_capture_fixed_is_declared_deterministic():
    declared = {"locus": "fixed", "fallback": "gap", "fixed_zone": "America/Los_Angeles"}
    # captured_zone is IGNORED for fixed -- the provider always reports in the declared zone.
    out = rtz.resolve_capture(declared, None)
    assert out == {"report_timezone": "America/Los_Angeles", "assumed": False, "gap": False}
    out2 = rtz.resolve_capture(declared, "Europe/Berlin")
    assert out2["report_timezone"] == "America/Los_Angeles"  # declaration wins, deterministic


def test_resolve_capture_fixed_with_invalid_zone_fails_closed():
    out = rtz.resolve_capture(
        {"locus": "fixed", "fallback": "gap", "fixed_zone": "Bogus/Zone"}, None
    )
    assert out == {"report_timezone": None, "assumed": False, "gap": True}


# ---------------------------------------------------------------------------
# (d) resolve_capture -- undetermined + fallback='assume' => TAGGED assumption.
# ---------------------------------------------------------------------------


def test_resolve_capture_assume_records_tagged_assumption():
    declared = {"locus": "account", "fallback": "assume", "assumed_zone": "Europe/Paris"}
    out = rtz.resolve_capture(declared, None)  # live zone undetermined
    assert out == {"report_timezone": "Europe/Paris", "assumed": True, "gap": False}
    # ...and the assumption is TAGGED, never silent:
    assert out["assumed"] is True
    assert out["report_timezone"] == "Europe/Paris"


def test_resolve_capture_assume_but_invalid_assumed_zone_fails_closed():
    declared = {"locus": "network", "fallback": "assume", "assumed_zone": "Nope/Nope"}
    out = rtz.resolve_capture(declared, None)
    assert out == {"report_timezone": None, "assumed": False, "gap": True}


def test_resolve_capture_valid_capture_wins_over_assume():
    # A valid live capture is source truth even under an 'assume' posture (E39-AD2 immutable).
    declared = {"locus": "network", "fallback": "assume", "assumed_zone": "Europe/Paris"}
    out = rtz.resolve_capture(declared, "America/New_York")
    assert out == {"report_timezone": "America/New_York", "assumed": False, "gap": False}


# ---------------------------------------------------------------------------
# (e) AC3 fail-closed (LOAD-BEARING) -- the never-silent-UTC invariant.
# ---------------------------------------------------------------------------


def test_resolve_capture_gap_posture_null_capture_is_typed_gap():
    out = rtz.resolve_capture({"locus": "network", "fallback": "gap"}, None)
    assert out == {"report_timezone": None, "assumed": False, "gap": True}
    assert out["report_timezone"] is None
    assert out["report_timezone"] != "UTC"


def test_resolve_capture_locus_none_is_gap():
    out = rtz.resolve_capture({"locus": "none", "fallback": "gap"}, None)
    assert out == {"report_timezone": None, "assumed": False, "gap": True}


def test_resolve_capture_no_declaration_is_gap():
    for declared in (None, {}, {"fallback": "gap"}, "notadict"):  # type: ignore[list-item]
        out = rtz.resolve_capture(declared, None)  # type: ignore[arg-type]
        assert out["gap"] is True
        assert out["report_timezone"] is None


def test_resolve_capture_never_silently_writes_utc_or_default():
    """The load-bearing AC3 proof: no undetermined capture yields a non-null zone with
    assumed=False. A non-null report_timezone with assumed=False may ONLY come from a valid
    IANA capture or a valid locus='fixed' zone -- NEVER from a null/invalid capture under
    gap/none/no-declaration, and NEVER 'UTC'/a default."""
    # UNDETERMINED = null / empty / non-IANA. NOTE: an explicitly-captured "UTC" (or "UTC "
    # which strips to it) is a VALID IANA zone and a legitimate passthrough -- it is NOT a
    # silent default, so it is deliberately excluded from this "undetermined" set.
    undetermined_captures = [None, "", "   ", "Not/AZone", "GMT+2"]
    postures = [
        {"locus": "network", "fallback": "gap"},
        {"locus": "property", "fallback": "gap"},
        {"locus": "account", "fallback": "gap"},
        {"locus": "none", "fallback": "gap"},
        {"locus": "none", "fallback": "assume"},  # 'none' is a gap regardless of posture path
        None,
        {},
        {"locus": "network", "fallback": "assume", "assumed_zone": "Bogus/Zone"},
    ]
    for declared in postures:
        for cap in undetermined_captures:
            out = rtz.resolve_capture(declared, cap)  # type: ignore[arg-type]
            # Either a typed gap, or -- if it resolved a zone -- it must be TAGGED assumed.
            if out["report_timezone"] is not None:
                assert out["assumed"] is True, (declared, cap, out)
            else:
                assert out["gap"] is True and out["assumed"] is False, (declared, cap, out)
            # NEVER a silent UTC/default:
            assert not (out["report_timezone"] == "UTC" and out["assumed"] is False)


# ---------------------------------------------------------------------------
# (f) resolve_capture -- captured present but INVALID => treated as undetermined.
# ---------------------------------------------------------------------------


def test_resolve_capture_invalid_capture_never_persisted_as_is():
    declared = {"locus": "network", "fallback": "gap"}
    out = rtz.resolve_capture(declared, "Totally/Bogus")
    assert out["report_timezone"] is None and out["gap"] is True  # invalid never persisted
    # Under an 'assume' posture the invalid capture yields the declared assumption (tagged):
    declared2 = {"locus": "network", "fallback": "assume", "assumed_zone": "Europe/Paris"}
    out2 = rtz.resolve_capture(declared2, "Totally/Bogus")
    assert out2 == {"report_timezone": "Europe/Paris", "assumed": True, "gap": False}


# ---------------------------------------------------------------------------
# declared_zone -- the pure declaration read (no live capture / no warehouse).
# ---------------------------------------------------------------------------


def test_declared_zone_fixed_and_assume_only():
    assert (
        rtz.declared_zone({"locus": "fixed", "fallback": "gap", "fixed_zone": "Asia/Tokyo"})
        == "Asia/Tokyo"
    )
    assert (
        rtz.declared_zone(
            {"locus": "network", "fallback": "assume", "assumed_zone": "Europe/Paris"}
        )
        == "Europe/Paris"
    )
    # A live-read locus with 'gap' posture is NOT fixed by the declaration => None (no default).
    assert rtz.declared_zone({"locus": "network", "fallback": "gap"}) is None
    assert rtz.declared_zone({"locus": "property", "fallback": "gap"}) is None
    assert rtz.declared_zone({"locus": "none", "fallback": "gap"}) is None
    assert rtz.declared_zone(None) is None
    assert rtz.declared_zone({"locus": "fixed", "fallback": "gap", "fixed_zone": "Bogus/Z"}) is None


# ---------------------------------------------------------------------------
# (g) timezone_gap -- typed refusal, shape-aligned with CURRENCY_GAP.
# ---------------------------------------------------------------------------


def test_timezone_gap_emits_when_no_stream_carries_a_zone():
    field = {"name": "ad_revenue", "monetary": True}
    used_by = [
        {"module_name": "mod_a", "datastream_name": "ds_a"},
        {"module_name": "mod_b", "datastream_name": "ds_b"},
    ]
    gap = rtz.timezone_gap(field, used_by)
    assert gap is not None
    assert gap["code"] == "TIMEZONE_GAP"
    assert gap["severity"] == "refusal"
    assert gap["resolvable_via"] == "source_timezone_declaration"
    assert gap["metric"] == "ad_revenue"
    assert gap["affected_streams"] == ["ds_a", "ds_b"]
    # shape-aligned with CURRENCY_GAP: carries the same keys.
    for key in ("code", "message", "affected_streams", "severity", "metric", "resolvable_via"):
        assert key in gap


def test_timezone_gap_none_when_a_stream_carries_a_zone():
    field = {"name": "ad_revenue", "monetary": True}
    used_by = [
        {"module_name": "mod_a", "datastream_name": "ds_a", "report_timezone": "America/New_York"},
        {"module_name": "mod_b", "datastream_name": "ds_b"},
    ]
    assert rtz.timezone_gap(field, used_by) is None


def test_used_by_has_timezone_fail_closed_on_blank():
    assert rtz.used_by_has_timezone({"report_timezone": "Europe/Paris"}) is True
    assert rtz.used_by_has_timezone({"report_timezone": "  "}) is False
    assert rtz.used_by_has_timezone({"report_timezone": None}) is False
    assert rtz.used_by_has_timezone({}) is False


# ---------------------------------------------------------------------------
# (h) AD-2 grep -- report_timezone.py names no connector/provider / no provider field.
# ---------------------------------------------------------------------------


def test_report_timezone_module_names_no_provider():
    """AD-2 / E39-NFR05: the core helper names ZERO connectors/providers and no provider field.

    The abstract enum values (network/property/account/fixed/none) are the ALLOWED locus
    vocabulary; a provider NAME or a provider FIELD spelling is not."""
    src_orig = Path(rtz.__file__).read_text(encoding="utf-8")
    src = src_orig.lower()
    forbidden_lower = [
        "gam",
        "google-ad-manager",
        "google_ad_manager",
        "ga4",
        "google-analytics",
        "google_analytics",
        "shopify",
    ]
    for token in forbidden_lower:
        assert token not in src, f"AD-2 violation: report_timezone.py names {token!r}"
    # The camelCase provider field spelling 'timeZone' must never appear (original case).
    assert "timeZone" not in src_orig, "AD-2 violation: names a provider field 'timeZone'"
