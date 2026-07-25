"""Read-through cache ROUTING tests -- Story 19.2 (CAP-22 / AD-22).

The cache is interposed UNDER the public ``warehouse.query_*`` functions (AD-2-style:
signatures intact, ``rollup``/``cards``/``reports``/``main.py`` untouched). This suite
proves the story contract:

  * EQUIVALENCE (invariant a = CRITICAL): a cache-hit returns the SAME rows as the
    origin path, LINE FOR LINE -- values + pull_id + loaded_at + ORDER -- for the
    additive fact table, the non-additive semantic view, the composite view, and
    dedup_estimate. A single family of ``_build_*`` builders serves both files.
  * MISS-WINDOW: a window that overflows the cache falls through to the origin
    (never a truncated hit) -- proven identical to the origin path.
  * BYPASS-STALE (invariant c): a cache older than the last expected nightly / TTL
    is bypassed -- never frais mensonger.
  * MISS-PROJECT / AD-5: a project not covered by the manifest cannot read another
    project's cached rows -- it falls through to the origin (cross-project denial).
  * DEGRADE-NEVER-RAISE (invariant f): absent / corrupt / locked cache -> transparent
    fallthrough + structured warning; no read path raises.
  * DISABLE SWITCH: TOOROW_CACHE_ENABLED=false -> no cache path is ever taken.
  * TRACE (AD-13 / NFR7): every decision is counted in get_cache_stats().
  * TRANSPARENCY SEAM: query_daily_report keeps its exact signature/return shape.

Dev simulation ``origin-duckdb -> cache-duckdb`` on TWO DISTINCT tmp files (the real
BigQuery extract is Phase B / AI-08 BLOCKED). We reuse the 19.1 seed DDL/helpers.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

# Reuse the 19.1 seed helpers (origin DDL + row seeding) verbatim -- one seed source.
from tests.core.test_cache_warehouse import _make_origin_db


def _rebuild_and_manifest(today: date, project_ids=None) -> dict:
    """Rebuild the cache from the configured origin and return the build result."""
    from core import cache_warehouse

    result = cache_warehouse.rebuild_cache(project_ids=project_ids, today=today)
    assert result["status"] == "ok", result
    return result


@pytest.fixture
def routing_env(tmp_path, monkeypatch):
    """Origin duckdb + DISTINCT cache path, cache enabled, deterministic freshness.

    Returns (origin_path, cache_path, today). The scheduler nightly anchor is pinned
    (02:00 UTC) and ``today`` is set so a cache built "now" is fresh by both the
    nightly-anchor and TTL rules unless a test deliberately ages it.
    """
    origin_path = str(tmp_path / "local.duckdb")
    cache_path = str(tmp_path / "cache_warehouse.duckdb")
    assert origin_path != cache_path

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin_path)
    monkeypatch.setenv("TOOROW_CACHE_PATH", cache_path)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "true")
    monkeypatch.setenv("TOOROW_CACHE_WINDOW_DAYS", "30")
    # Deterministic freshness anchor (UTC 02:00).
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "UTC")
    monkeypatch.setenv("SCHEDULER_NIGHTLY_HOUR", "2")
    monkeypatch.setenv("SCHEDULER_NIGHTLY_MINUTE", "0")

    from core import warehouse

    warehouse.reset_cache_stats()

    today = date(2026, 7, 19)
    return origin_path, cache_path, today


def _query_origin(monkeypatch, fn, *args, **kwargs):
    """Run *fn* with the cache DISABLED so it reads straight from the origin."""
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")
    try:
        return fn(*args, **kwargs)
    finally:
        monkeypatch.setenv("TOOROW_CACHE_ENABLED", "true")


# ---------------------------------------------------------------------------
# EQUIVALENCE (invariant a) -- cache-hit == origin, line for line.
# ---------------------------------------------------------------------------


def test_equivalence_fact_daily_kpi_additive(routing_env, monkeypatch):
    """query_daily_report: cache-hit rows == origin rows, incl. pull_id/loaded_at/order."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a", "proj_b"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=6)).isoformat()
    end = (today - timedelta(days=1)).isoformat()

    # Origin path (cache off) vs cache path (cache on), same signature/args.
    origin_rows = _query_origin(
        monkeypatch, warehouse.query_daily_report, "proj_a", start, end, None
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_daily_report("proj_a", start, end, None)

    # Line-for-line identity (values + pull_id + loaded_at + ORDER).
    assert cache_rows == origin_rows
    assert len(cache_rows) > 0  # the assertion has teeth
    # The decision was a HIT (AD-13 trace).
    assert warehouse.get_cache_stats().get("hit", 0) == 1


def test_equivalence_multi_metric_report_additive(routing_env, monkeypatch):
    """query_report additive path: cache-hit == origin, multi-day multi-metric."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch,
        warehouse.query_report,
        "proj_a", "ga4", ["sessions"], ["date"], start, end,
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_report(
        "proj_a", "ga4", ["sessions"], ["date"], start, end
    )

    assert cache_rows == origin_rows
    assert len(cache_rows) > 0
    assert warehouse.get_cache_stats().get("hit", 0) == 1


def test_equivalence_composite_non_additive_position(routing_env, monkeypatch):
    """query_composite_positions (non-additive AD-4): cache-hit == origin."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=4)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch,
        warehouse.query_composite_positions,
        "proj_a", "country>device", start, end, None,
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_composite_positions(
        "proj_a", "country>device", start, end, None
    )

    assert cache_rows == origin_rows
    assert len(cache_rows) > 0
    assert warehouse.get_cache_stats().get("hit", 0) == 1


def test_equivalence_dedup_estimate(routing_env, monkeypatch):
    """query_dedup_estimate (Epic 17 view): cache-hit == origin, line for line."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=6)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch, warehouse.query_dedup_estimate, "proj_a", start, end
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_dedup_estimate("proj_a", start, end)

    assert cache_rows == origin_rows
    assert len(cache_rows) > 0
    assert warehouse.get_cache_stats().get("hit", 0) == 1


def test_equivalence_transaction_reconciliation(routing_env, monkeypatch):
    """query_transaction_reconciliation_daily (17.4): cache-hit == origin."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=6)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch,
        warehouse.query_transaction_reconciliation_daily,
        "proj_a", start, end,
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_transaction_reconciliation_daily("proj_a", start, end)

    assert cache_rows == origin_rows
    assert len(cache_rows) > 0
    assert warehouse.get_cache_stats().get("hit", 0) == 1


# ---------------------------------------------------------------------------
# EQUIVALENCE (invariant a) -- cache-hit for the non-additive semantic_ctr family.
# ---------------------------------------------------------------------------


def test_equivalence_semantic_ctr(routing_env, monkeypatch):
    """query_report(metrics=["ctr"]): cache-hit == origin, line for line (F-2 / invariant a).

    HOW THE SEMANTIC PATH IS FORCED:
    ``ctr`` is declared non-additive (additive=false) in dbt/seeds/dim_metric.csv.
    ``report_dictionary.is_non_additive("ctr")`` therefore returns True, which causes
    ``query_report`` to route to ``_query_duckdb_routed`` with
    ``relation="semantic_ctr"`` (f"semantic_{metric.lower()}") and builder
    ``_build_semantic_view_query``. With the cache enabled and ``semantic_ctr`` in the
    default allowlist (fix F-1), the manifest lists the relation and the router
    evaluates to a HIT -- never a ``miss-relation`` fallthrough.

    The equivalence assertion ``cache_rows == origin_rows`` proves invariant (a)
    "memes chiffres": a single ``_build_semantic_view_query`` builder runs on both the
    origin (prefix "main_marts.") and the cache (prefix ""), returning identical rows.
    The ``loaded_at`` column is absent from semantic_ctr (ratio view, not a raw fact
    table), so ``query_report`` sets it to None via ``row.setdefault("loaded_at", None)``.
    Both origin and cache paths go through this same setdefault, so the rows match.
    """
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a", "proj_b"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=6)).isoformat()
    end = today.isoformat()

    # Disable the lru_cache on report_dictionary so the CSV is re-read from disk
    # (defensive: another test may have populated the cache before dim_metric.csv
    # was on the import path).
    try:
        from core import report_dictionary as _rd
        _rd._load_metric_dictionary.cache_clear()
    except Exception:  # noqa: BLE001
        pass

    # Origin path (cache off): query_report routes ctr through _build_semantic_view_query
    # on origin prefix "main_marts." -> reads main_marts.semantic_ctr.
    origin_rows = _query_origin(
        monkeypatch,
        warehouse.query_report,
        "proj_a", "google_ads", ["ctr"], ["date"], start, end,
    )
    warehouse.reset_cache_stats()
    # Cache path (cache on): same router, same builder, cache prefix "" -> hits
    # the semantic_ctr table in cache_warehouse.duckdb.
    cache_rows = warehouse.query_report(
        "proj_a", "google_ads", ["ctr"], ["date"], start, end
    )

    # Line-for-line equivalence: values + pull_id + loaded_at (None) + ORDER (AD-13).
    assert cache_rows == origin_rows
    assert len(cache_rows) > 0  # assertion has teeth: 7-day window x 1 row/day
    # The decision must be a HIT (not miss-relation, which would mean the allowlist
    # fix F-1 was not applied or semantic_ctr was not exported into the cache).
    assert warehouse.get_cache_stats().get("hit", 0) == 1
    assert warehouse.get_cache_stats().get("miss-relation", 0) == 0
    # Structural check: each row carries the expected keys and loaded_at=None
    # (semantic view has no loaded_at -- set by query_report.setdefault).
    for row in cache_rows:
        assert row["metric"] == "ctr"
        assert row["loaded_at"] is None
        assert row["pull_id"] is not None


# ---------------------------------------------------------------------------
# MISS-WINDOW -- a window that overflows the cache falls through (never truncated).
# ---------------------------------------------------------------------------


def test_window_overflow_falls_through_not_truncated(routing_env, monkeypatch):
    """A [J-45, J-1] request overflows a 30-day cache -> miss-window -> origin path.

    The critical safety property: the result must equal the FULL origin result
    (45 days of history), never a hit truncated to the 30-day cache window.
    """
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=50, projects=["proj_a"])
    _rebuild_and_manifest(today)  # cache covers only the last 30 days

    from core import warehouse

    start = (today - timedelta(days=45)).isoformat()
    end = (today - timedelta(days=1)).isoformat()

    origin_rows = _query_origin(
        monkeypatch, warehouse.query_daily_report, "proj_a", start, end, None
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_daily_report("proj_a", start, end, None)

    # Same rows as the origin (full history), and it was a documented MISS, not a hit.
    assert cache_rows == origin_rows
    assert warehouse.get_cache_stats().get("hit", 0) == 0
    assert warehouse.get_cache_stats().get("miss-window", 0) == 1
    # Sanity: the result really spans beyond the cache window (proves no truncation).
    dates = {r["date"] for r in cache_rows}
    assert start in {d if isinstance(d, str) else d.isoformat() for d in dates}


def test_widened_delta_window_reaching_prior_period_is_a_miss(routing_env, monkeypatch):
    """The EFFECTIVE (widened) window governs hit/miss (not the requested one).

    query_daily_report(include_prior_period=True) widens the fetch to a prior period
    of equal length. A request whose widened start dips below the cache min_date must
    be a miss-window, proving the router respects _widen_to_prior.
    """
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=50, projects=["proj_a"])
    _rebuild_and_manifest(today)  # cache = last 30 days

    from core import warehouse

    # Requested window [J-25, J-1] (25 days). Widened by an equal prior period the
    # fetch reaches back to ~J-50, well outside the 30-day cache -> must miss.
    start = (today - timedelta(days=25)).isoformat()
    end = (today - timedelta(days=1)).isoformat()

    origin_rows = _query_origin(
        monkeypatch, warehouse.query_daily_report, "proj_a", start, end, None
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_daily_report("proj_a", start, end, None)

    assert cache_rows == origin_rows
    assert warehouse.get_cache_stats().get("miss-window", 0) == 1
    assert warehouse.get_cache_stats().get("hit", 0) == 0


# ---------------------------------------------------------------------------
# BYPASS-STALE (invariant c) -- a stale cache is bypassed, never frais mensonger.
# ---------------------------------------------------------------------------


def test_stale_cache_bypassed(routing_env, monkeypatch):
    """A cache built well before the last nightly / beyond TTL is bypassed (invariant c)."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import cache_warehouse, warehouse

    # Force _cache_is_fresh to see an ancient cache_built_at (10 days old) by patching
    # read_manifest -- the manifest content is the freshness source of truth (19.1).
    real_manifest = cache_warehouse.read_manifest(cache_path)
    stale = dict(real_manifest)
    stale["cache_built_at"] = (
        datetime.now(tz=timezone.utc) - timedelta(days=10)
    ).isoformat()
    monkeypatch.setattr(
        "core.cache_warehouse.read_manifest", lambda *a, **k: stale
    )

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch, warehouse.query_daily_report, "proj_a", start, end, None
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_daily_report("proj_a", start, end, None)

    # Bypassed to the origin -> same data, decision = bypass-stale (never a hit).
    assert cache_rows == origin_rows
    assert warehouse.get_cache_stats().get("bypass-stale", 0) == 1
    assert warehouse.get_cache_stats().get("hit", 0) == 0


def test_fresh_cache_is_a_hit_freshness_unit(routing_env):
    """_cache_is_fresh: fresh instant passes both bounds; ancient instant fails."""
    from core import warehouse

    now = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    # after today's 02:00 nightly -> fresh
    fresh_built = datetime(2026, 7, 19, 2, 5, tzinfo=timezone.utc).isoformat()
    # 2 nightlies ago -> stale
    stale_built = datetime(2026, 7, 17, 2, 5, tzinfo=timezone.utc).isoformat()
    assert warehouse._cache_is_fresh(fresh_built, now=now) is True
    assert warehouse._cache_is_fresh(stale_built, now=now) is False
    # Built BEFORE today's nightly (01:00) while now is after 02:00 -> stale.
    before_nightly = datetime(2026, 7, 19, 1, 0, tzinfo=timezone.utc).isoformat()
    assert warehouse._cache_is_fresh(before_nightly, now=now) is False
    # Unparseable / missing -> fail closed (not fresh).
    assert warehouse._cache_is_fresh(None, now=now) is False
    assert warehouse._cache_is_fresh("not-a-date", now=now) is False


# ---------------------------------------------------------------------------
# AD-5 isolation -- a project not covered by the manifest cannot read the cache.
# ---------------------------------------------------------------------------


def test_uncovered_project_denied_cache_read(routing_env, monkeypatch):
    """A cache pinned to proj_a must NOT serve proj_b from the cache (AD-5).

    proj_b IS present in the origin. Build a cache covering ONLY proj_a, then query
    proj_b: the router must return miss-project and fall through to the origin, where
    proj_b's own rows are read -- never proj_a's cached rows.
    """
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a", "proj_b"])
    _rebuild_and_manifest(today, project_ids=["proj_a"])  # cache covers ONLY proj_a

    from core import warehouse

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch, warehouse.query_daily_report, "proj_b", start, end, None
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_daily_report("proj_b", start, end, None)

    assert cache_rows == origin_rows
    assert len(cache_rows) > 0
    # No proj_a leakage: the equality with the origin's proj_b rows above is the
    # real guard (query_daily_report rows do not expose project_id).
    assert warehouse.get_cache_stats().get("miss-project", 0) == 1
    assert warehouse.get_cache_stats().get("hit", 0) == 0


def test_covered_project_still_hits_when_pinned(routing_env, monkeypatch):
    """The pinned project (proj_a) DOES hit even though the cache excludes proj_b."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a", "proj_b"])
    _rebuild_and_manifest(today, project_ids=["proj_a"])

    from core import warehouse

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch, warehouse.query_daily_report, "proj_a", start, end, None
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_daily_report("proj_a", start, end, None)

    assert cache_rows == origin_rows
    assert warehouse.get_cache_stats().get("hit", 0) == 1


# ---------------------------------------------------------------------------
# MISS-RELATION -- a relation absent from the manifest allowlist falls through.
# ---------------------------------------------------------------------------


def test_relation_not_in_allowlist_falls_through(routing_env, monkeypatch):
    """A query for a semantic view NOT cached (allowlist miss) falls through cleanly."""
    origin_path, cache_path, today = routing_env
    # Restrict the allowlist so only fact_daily_kpi is cached.
    monkeypatch.setenv("TOOROW_CACHE_TABLES", "fact_daily_kpi")
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=4)).isoformat()
    end = today.isoformat()

    # composite view is NOT in the (restricted) allowlist -> miss-relation.
    origin_rows = _query_origin(
        monkeypatch,
        warehouse.query_composite_positions,
        "proj_a", "country>device", start, end, None,
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_composite_positions(
        "proj_a", "country>device", start, end, None
    )

    assert cache_rows == origin_rows
    assert warehouse.get_cache_stats().get("miss-relation", 0) == 1
    assert warehouse.get_cache_stats().get("hit", 0) == 0


# ---------------------------------------------------------------------------
# DEGRADE-NEVER-RAISE (invariant f) -- absent / corrupt cache -> fallthrough.
# ---------------------------------------------------------------------------


def test_absent_cache_falls_through_without_raising(routing_env, monkeypatch):
    """No cache file at all -> fallthrough to origin, structured warning, no raise."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    # Deliberately DO NOT build the cache -> the file is absent.

    from core import warehouse

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch, warehouse.query_daily_report, "proj_a", start, end, None
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_daily_report("proj_a", start, end, None)

    assert cache_rows == origin_rows
    assert len(cache_rows) > 0
    # read_manifest returns None for an absent cache -> fallthrough-error decision.
    assert warehouse.get_cache_stats().get("fallthrough-error", 0) == 1
    assert warehouse.get_cache_stats().get("hit", 0) == 0


def test_corrupt_cache_falls_through_without_raising(routing_env, monkeypatch, caplog):
    """A corrupt cache file -> fallthrough + warning, never an exception."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    # Write garbage where the cache file should be -> read_manifest returns None.
    with open(cache_path, "wb") as fh:
        fh.write(b"not a duckdb file at all")

    from core import warehouse

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch, warehouse.query_daily_report, "proj_a", start, end, None
    )
    warehouse.reset_cache_stats()
    with caplog.at_level(logging.WARNING):
        cache_rows = warehouse.query_daily_report("proj_a", start, end, None)

    assert cache_rows == origin_rows
    assert len(cache_rows) > 0
    assert warehouse.get_cache_stats().get("hit", 0) == 0


def test_cache_read_raises_is_caught_and_falls_through(routing_env, monkeypatch):
    """If the cache read itself explodes on a hit, the router still falls through (invariant f)."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import warehouse

    def _boom(*a, **k):
        raise RuntimeError("simulated cache file lock/corruption mid-read")

    monkeypatch.setattr(warehouse, "_read_from_cache", _boom)

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch, warehouse.query_daily_report, "proj_a", start, end, None
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_daily_report("proj_a", start, end, None)

    assert cache_rows == origin_rows
    assert warehouse.get_cache_stats().get("fallthrough-error", 0) == 1
    assert warehouse.get_cache_stats().get("hit", 0) == 0


# ---------------------------------------------------------------------------
# DISABLE SWITCH -- TOOROW_CACHE_ENABLED=false takes no cache path at all.
# ---------------------------------------------------------------------------


def test_cache_disabled_never_touches_cache(routing_env, monkeypatch):
    """With the cache disabled, no manifest is read and no decision is recorded."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)  # a valid cache EXISTS...

    from core import cache_warehouse, warehouse

    # ...but disabling the switch must prevent even a manifest read.
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")

    called = {"read_manifest": 0}
    real = cache_warehouse.read_manifest

    def _spy(*a, **k):
        called["read_manifest"] += 1
        return real(*a, **k)

    monkeypatch.setattr("core.cache_warehouse.read_manifest", _spy)

    warehouse.reset_cache_stats()
    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()
    rows = warehouse.query_daily_report("proj_a", start, end, None)

    assert len(rows) > 0
    assert called["read_manifest"] == 0  # the cache path was never entered
    assert warehouse.get_cache_stats() == {}  # no decision recorded when disabled


# ---------------------------------------------------------------------------
# TRANSPARENCY SEAM (AD-2) -- the public signature/return shape is unchanged.
# ---------------------------------------------------------------------------


def test_transparency_hit_and_origin_have_identical_shape(routing_env, monkeypatch):
    """Consumers see IDENTICAL shape whether served from cache or origin (AD-2)."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    origin_rows = _query_origin(
        monkeypatch, warehouse.query_daily_report, "proj_a", start, end, None
    )
    cache_rows = warehouse.query_daily_report("proj_a", start, end, None)

    assert origin_rows and cache_rows
    # Same keys, same order of keys, same types for every row.
    for o, c in zip(origin_rows, cache_rows):
        assert list(o.keys()) == list(c.keys())
        assert o == c
    # The public function still returns a plain list[dict] with the fact columns.
    expected_keys = {
        "date", "connector", "metric", "breakdown_dimension",
        "breakdown_value", "value", "pull_id", "loaded_at",
    }
    assert set(cache_rows[0].keys()) == expected_keys


# ---------------------------------------------------------------------------
# spec F1 (19.4 review) -- future-timestamp guard in _cache_is_fresh.
# ---------------------------------------------------------------------------


def test_cache_is_fresh_future_timestamp_beyond_tolerance_is_bypass(routing_env):
    """cache_built_at more than 120 s in the future -> NOT fresh (spec F1, fail-closed).

    Without the future-guard, a manifest with a shifted/tampered cache_built_at
    in the future passes both the TTL check (negative age) and the nightly-anchor
    check (built > anchor), resulting in a false hit. The guard rejects it.
    """
    from core import warehouse

    now = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    # +1h in the future: beyond 120 s tolerance -> NOT fresh (bypass).
    far_future = (now + timedelta(hours=1)).isoformat()
    assert warehouse._cache_is_fresh(far_future, now=now) is False


def test_cache_is_fresh_future_timestamp_within_tolerance_is_ok(routing_env):
    """cache_built_at <= now + 120 s (clock skew tolerance) -> still treated as fresh.

    30 s in the future is within the 120 s tolerance window and must not be rejected
    (normal clock drift between builder and router hosts).
    """
    from core import warehouse

    now = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    # +30 s: within tolerance. Also must pass TTL and nightly-anchor checks.
    # Use a built time that is after today's 02:00 nightly but slightly in the future.
    within_tolerance = (now + timedelta(seconds=30)).isoformat()
    # now=10:00 UTC, built=10:00:30 UTC (30 s future). Nightly anchor is 02:00 today.
    # built (10:00:30) > anchor (02:00) -> anchor check passes.
    # TTL: age = -30 s < 26h -> TTL check passes.
    # Future guard: 30 s <= 120 s -> guard passes.
    assert warehouse._cache_is_fresh(within_tolerance, now=now) is True


# ---------------------------------------------------------------------------
# spec F2 (19.4 review) -- schema_version validation in _cache_decision.
# ---------------------------------------------------------------------------


def test_schema_version_mismatch_is_miss_schema(routing_env, monkeypatch):
    """A manifest with schema_version != CACHE_SCHEMA_VERSION -> miss-schema, not hit.

    spec F2: _cache_decision validates schema_version BEFORE relation/window/freshness.
    A tampered or stale manifest (version 0 or absent) must produce 'miss-schema'
    and fall through to the origin -- never a hit.
    """
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import cache_warehouse, warehouse

    # Patch read_manifest to return a manifest with wrong schema_version.
    real_manifest = cache_warehouse.read_manifest(cache_path)
    tampered = dict(real_manifest)
    tampered["schema_version"] = 0  # wrong version

    monkeypatch.setattr("core.cache_warehouse.read_manifest", lambda *a, **k: tampered)
    # Also patch the in-memory cache to bypass the mtime check.
    monkeypatch.setattr(warehouse, "_read_manifest_cached", lambda p: tampered)

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    warehouse.reset_cache_stats()
    # Should fall through to origin (no hit).
    rows = warehouse.query_daily_report("proj_a", start, end, None)

    assert len(rows) > 0
    assert warehouse.get_cache_stats().get("miss-schema", 0) == 1
    assert warehouse.get_cache_stats().get("hit", 0) == 0


def test_schema_version_absent_is_miss_schema(routing_env, monkeypatch):
    """A manifest without schema_version key -> miss-schema (spec F2, fail-closed)."""
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import cache_warehouse, warehouse

    real_manifest = cache_warehouse.read_manifest(cache_path)
    tampered = {k: v for k, v in real_manifest.items() if k != "schema_version"}

    monkeypatch.setattr("core.cache_warehouse.read_manifest", lambda *a, **k: tampered)
    monkeypatch.setattr(warehouse, "_read_manifest_cached", lambda p: tampered)

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    warehouse.reset_cache_stats()
    rows = warehouse.query_daily_report("proj_a", start, end, None)

    assert len(rows) > 0
    assert warehouse.get_cache_stats().get("miss-schema", 0) == 1
    assert warehouse.get_cache_stats().get("hit", 0) == 0


# ---------------------------------------------------------------------------
# data F-2 (19.4 review): semantic_cpa and semantic_roas equivalence cache-HIT.
# Mirrors test_equivalence_semantic_ctr exactly (fix 3).
# ---------------------------------------------------------------------------


def test_equivalence_semantic_cpa(routing_env, monkeypatch):
    """query_report(metrics=["cpa"]): cache-hit == origin, line for line (data F-2).

    semantic_cpa is in the default allowlist and is routed by query_report for
    metric="cpa" (non-additive, AD-4). Column schema: project_id, date, connector,
    breakdown_dimension, breakdown_value, cpa (as value), pull_id. No loaded_at.
    """
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=6)).isoformat()
    end = today.isoformat()

    try:
        from core import report_dictionary as _rd
        _rd._load_metric_dictionary.cache_clear()
    except Exception:  # noqa: BLE001
        pass

    origin_rows = _query_origin(
        monkeypatch,
        warehouse.query_report,
        "proj_a", "google_ads", ["cpa"], ["date"], start, end,
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_report(
        "proj_a", "google_ads", ["cpa"], ["date"], start, end
    )

    assert cache_rows == origin_rows
    assert len(cache_rows) > 0
    assert warehouse.get_cache_stats().get("hit", 0) == 1
    assert warehouse.get_cache_stats().get("miss-relation", 0) == 0
    for row in cache_rows:
        assert row["metric"] == "cpa"
        assert row["loaded_at"] is None
        assert row["pull_id"] is not None


def test_equivalence_semantic_roas(routing_env, monkeypatch):
    """query_report(metrics=["roas"]): cache-hit == origin, line for line (data F-2).

    semantic_roas is in the default allowlist and is routed by query_report for
    metric="roas" (non-additive, AD-4). Column schema: project_id, date, connector,
    breakdown_dimension, breakdown_value, roas (as value), pull_id. No loaded_at.
    """
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=6)).isoformat()
    end = today.isoformat()

    try:
        from core import report_dictionary as _rd
        _rd._load_metric_dictionary.cache_clear()
    except Exception:  # noqa: BLE001
        pass

    origin_rows = _query_origin(
        monkeypatch,
        warehouse.query_report,
        "proj_a", "google_ads", ["roas"], ["date"], start, end,
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_report(
        "proj_a", "google_ads", ["roas"], ["date"], start, end
    )

    assert cache_rows == origin_rows
    assert len(cache_rows) > 0
    assert warehouse.get_cache_stats().get("hit", 0) == 1
    assert warehouse.get_cache_stats().get("miss-relation", 0) == 0
    for row in cache_rows:
        assert row["metric"] == "roas"
        assert row["loaded_at"] is None
        assert row["pull_id"] is not None


# ---------------------------------------------------------------------------
# spec F4 (19.4 review): average_position routes to semantic_avg_position (debt resolved).
# ---------------------------------------------------------------------------


def test_equivalence_average_position_routes_semantic_avg_position(routing_env, monkeypatch):
    """query_report(metrics=["average_position"]): cache-hit on semantic_avg_position.

    spec F4 debt resolved: warehouse._SEMANTIC_VIEW_BY_METRIC maps average_position
    -> semantic_avg_position (the real dbt view). The router relation is now
    "semantic_avg_position" (not the non-existent "semantic_average_position").
    With semantic_avg_position in the default allowlist (cache_warehouse.py), the
    decision is a HIT and cache_rows == origin_rows (invariant a).

    Column schema mirrors semantic_avg_position.sql output:
    date, connector, breakdown_dimension, breakdown_value, average_position (as value),
    pull_id. loaded_at is set to None by query_report.setdefault (semantic view
    carries loaded_at but query_report only passes pull_id through setdefault).
    """
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import warehouse

    start = (today - timedelta(days=6)).isoformat()
    end = today.isoformat()

    try:
        from core import report_dictionary as _rd
        _rd._load_metric_dictionary.cache_clear()
    except Exception:  # noqa: BLE001
        pass

    origin_rows = _query_origin(
        monkeypatch,
        warehouse.query_report,
        "proj_a", "gsc", ["average_position"], ["page"], start, end,
    )
    warehouse.reset_cache_stats()
    cache_rows = warehouse.query_report(
        "proj_a", "gsc", ["average_position"], ["page"], start, end
    )

    assert cache_rows == origin_rows
    assert len(cache_rows) > 0
    # The decision must be a HIT (semantic_avg_position is in the allowlist).
    assert warehouse.get_cache_stats().get("hit", 0) == 1
    # No miss-relation: the fixed routing hits semantic_avg_position, not the
    # non-existent semantic_average_position.
    assert warehouse.get_cache_stats().get("miss-relation", 0) == 0
    for row in cache_rows:
        assert row["metric"] == "average_position"
        assert row["pull_id"] is not None


# ---------------------------------------------------------------------------
# perf F-1/F-2 (19.4 review): in-memory manifest cache avoids repeated DuckDB opens.
# ---------------------------------------------------------------------------


def test_manifest_memory_cache_avoids_repeated_disk_reads(routing_env, monkeypatch):
    """Two successive routed queries reuse the in-memory manifest (perf F-1/F-2).

    Strategy: spy on cache_warehouse.read_manifest (the disk reader). With the
    memory cache active, only the FIRST call should reach disk; the second call
    must be served from memory (mtime unchanged -> no disk read).
    """
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import cache_warehouse, warehouse

    read_manifest_calls = {"count": 0}
    real_read_manifest = cache_warehouse.read_manifest

    def _spy_read_manifest(*a, **k):
        read_manifest_calls["count"] += 1
        return real_read_manifest(*a, **k)

    monkeypatch.setattr(cache_warehouse, "read_manifest", _spy_read_manifest)
    # Clear the in-memory manifest cache so the first call goes to disk.
    with warehouse._MANIFEST_MEM_CACHE_LOCK:
        warehouse._MANIFEST_MEM_CACHE.clear()

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    warehouse.reset_cache_stats()
    warehouse.query_daily_report("proj_a", start, end, None)
    warehouse.query_daily_report("proj_a", start, end, None)

    # Both queries hit the cache (2 hits total).
    assert warehouse.get_cache_stats().get("hit", 0) == 2
    # read_manifest (disk read) called only ONCE -- second query served from memory.
    assert read_manifest_calls["count"] == 1


def test_manifest_memory_cache_invalidated_after_rebuild(routing_env, monkeypatch):
    """After a rebuild (mtime changes), the next query re-reads manifest from disk.

    perf F-1/F-2 invalidation rule: os.stat().st_mtime_ns comparison. A rebuild
    that uses os.replace() writes a new file -> new mtime -> memory cache invalidated.
    """
    origin_path, cache_path, today = routing_env
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])
    _rebuild_and_manifest(today)

    from core import cache_warehouse, warehouse

    read_manifest_calls = {"count": 0}
    real_read_manifest = cache_warehouse.read_manifest

    def _spy_read_manifest(*a, **k):
        read_manifest_calls["count"] += 1
        return real_read_manifest(*a, **k)

    monkeypatch.setattr(cache_warehouse, "read_manifest", _spy_read_manifest)
    # Clear in-memory cache to start clean.
    with warehouse._MANIFEST_MEM_CACHE_LOCK:
        warehouse._MANIFEST_MEM_CACHE.clear()

    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    warehouse.reset_cache_stats()
    # First query: reads manifest from disk.
    warehouse.query_daily_report("proj_a", start, end, None)
    assert read_manifest_calls["count"] == 1

    # Rebuild: produces a new cache file via os.replace -> new mtime.
    _rebuild_and_manifest(today)

    # Second query: mtime changed -> memory cache invalidated -> reads from disk again.
    warehouse.query_daily_report("proj_a", start, end, None)
    assert read_manifest_calls["count"] == 2
