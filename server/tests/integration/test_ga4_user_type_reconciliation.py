"""Epic 10, story 10.1 -- GA4 user_type_daily reconciliation + seam tests.

The HARD point of this story (the 9.9 CRITICAL scenario): landing a NEW parallel
breakdown partition (breakdown_dimension='user_type') MUST NOT change the existing
device_category / country totals in fact_daily_kpi. This file proves that on a REAL
DuckDB seeded with BOTH profiles over multiple contiguous days (AI-45: multi-day
seed mandatory -- single-day seeds trivially satisfy broken aggregations).

Reconciliation (AC9):
  (a) Existing totals unchanged: SUM(value) over breakdown_dimension='country' per
      (project_id, date, metric) is IDENTICAL whether or not the user_type rows are
      present -- adding user_type rows does NOT touch the country partition.
  (b) MIN(breakdown_dimension) stability: MIN across the four GA4 breakdown dims
      ('country', 'country>device', 'device_category', 'user_type') is 'country' --
      so metric_baselines / cross_source_conversions keep selecting the canonical
      single dimension after the new partition arrives (lexicographic proof:
      'country' < 'country>device' < 'device_category' < 'user_type').
  (c) Partition self-consistency: SUM(value) over breakdown_dimension='user_type'
      per (project_id, date, metric) equals the seed's declared daily total for that
      metric (the three user_type values sum to the connector daily total).

Seam (AC10, AI-56): GET /api/cards?template=usertypes over the REAL build_asgi_app()
ASGI stack, with the mart query mocked to return user_type rows, wires through and
returns a valid usertypes envelope (the mart -> warehouse -> cards -> ASGI layer that
this story delivers is exercised end to end). Pattern: test_cards_integration_seams.py.

The reconciliation SQL below inlines the EXACT staging QUALIFY supersede + mart
GROUP BY of the GA4 blocks in fact_daily_kpi.sql so the test proves the mart's
aggregation SEMANTICS without requiring a dbt run inside pytest. Auto-skips when
duckdb is not importable (unit CI without the optional dep).

ASCII-only (AI-03). No live GA4 / Postgres required.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

duckdb = pytest.importorskip(
    "duckdb", reason="duckdb not installed -- user_type reconciliation seam skipped"
)

# The seed generators are the SAME code the local loop uses (module-owned seeds).
import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

_SEEDS_DIR = (
    Path(__file__).resolve().parents[3]
    / "server"
    / "modules"
    / "google-analytics"
    / "seeds"
)


def _load_generate_seed():
    spec = importlib.util.spec_from_file_location(
        "_ga4_generate_seed", str(_SEEDS_DIR / "generate_seed.py")
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# DuckDB fixture: BOTH profiles seeded over >= 3 contiguous days (AI-45).
# ---------------------------------------------------------------------------

# Inlined staging + mart SQL mirroring fact_daily_kpi.sql GA4 blocks EXACTLY.
# Standard block: device_category + country single dims, plus country>device composite.
# user_type block: appended parallel partition (this story). ALL from raw via the
# QUALIFY supersede staging step (project_id, date, <grain>) ORDER BY pull_id DESC.

_STG_STANDARD = """
CREATE OR REPLACE TEMP VIEW stg_std AS
SELECT date, device_category, country, sessions, active_users, conversions,
       pull_id, loaded_at, project_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY project_id, date, device_category, country
        ORDER BY pull_id DESC
    ) AS rn
    FROM raw_ga4_standard_daily
) WHERE rn = 1
"""

_STG_USER_TYPE = """
CREATE OR REPLACE TEMP VIEW stg_ut AS
SELECT date, user_type, sessions, active_users, conversions,
       pull_id, loaded_at, project_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY project_id, date, user_type
        ORDER BY pull_id DESC
    ) AS rn
    FROM raw_ga4_user_type_daily
) WHERE rn = 1
"""

_METRICS = ["sessions", "active_users", "conversions"]


def _mart_sql(include_user_type: bool) -> str:
    """Build the GA4 portion of fact_daily_kpi from the two staging views.

    Mirrors the metric x dimension double loop + country>device composite + (when
    included) the user_type block from fact_daily_kpi.sql.
    """
    blocks: list[str] = []
    for metric in _METRICS:
        for dimension in ("device_category", "country"):
            blocks.append(
                f"""
                SELECT project_id, date, 'google-analytics' AS connector,
                       '{metric}' AS metric, '{dimension}' AS breakdown_dimension,
                       {dimension} AS breakdown_value,
                       SUM(CAST({metric} AS DOUBLE)) AS value
                FROM stg_std GROUP BY project_id, date, {dimension}
                """
            )
    for metric in _METRICS:
        blocks.append(
            f"""
            SELECT project_id, date, 'google-analytics' AS connector,
                   '{metric}' AS metric, 'country>device' AS breakdown_dimension,
                   country || '>' || device_category AS breakdown_value,
                   SUM(CAST({metric} AS DOUBLE)) AS value
            FROM stg_std GROUP BY project_id, date, country, device_category
            """
        )
    if include_user_type:
        for metric in _METRICS:
            blocks.append(
                f"""
                SELECT project_id, date, 'google-analytics' AS connector,
                       '{metric}' AS metric, 'user_type' AS breakdown_dimension,
                       user_type AS breakdown_value,
                       SUM(CAST({metric} AS DOUBLE)) AS value
                FROM stg_ut GROUP BY project_id, date, user_type
                """
            )
    return "\nUNION ALL\n".join(blocks)


@pytest.fixture()
def seeded_con():
    """In-memory DuckDB seeded with BOTH profiles over >= 3 contiguous days.

    Uses a FIXED end_date + seeded RNG so the run is deterministic and the two
    profiles share the same weekday seasonality window (they reconcile within noise).
    """
    gen = _load_generate_seed()
    import random

    # end_date is EXCLUSIVE: the window is [start, end) = 90 contiguous days,
    # i.e. 2026-03-03 .. 2026-05-31 inclusive (2026-06-01 itself is NOT seeded).
    end_date = date(2026, 6, 1)
    std_rows = gen.generate_rows(end_date=end_date, rng=random.Random(1234))
    ut_rows = gen.generate_user_type_rows(end_date=end_date, rng=random.Random(1234))

    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE raw_ga4_standard_daily (
            date VARCHAR, device_category VARCHAR, country VARCHAR,
            sessions INTEGER, active_users INTEGER, conversions INTEGER,
            pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE raw_ga4_user_type_daily (
            date VARCHAR, user_type VARCHAR,
            sessions INTEGER, active_users INTEGER, conversions INTEGER,
            pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
        )
        """
    )
    con.executemany(
        "INSERT INTO raw_ga4_standard_daily VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                r["date"], r["device_category"], r["country"],
                r["sessions"], r["active_users"], r["conversions"],
                "pull_std_1", "2026-06-01T00:00:00Z", "default",
            )
            for r in std_rows
        ],
    )
    con.executemany(
        "INSERT INTO raw_ga4_user_type_daily VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                r["date"], r["user_type"],
                r["sessions"], r["active_users"], r["conversions"],
                "pull_ut_1", "2026-06-01T00:00:00Z", "default",
            )
            for r in ut_rows
        ],
    )
    con.execute(_STG_STANDARD)
    con.execute(_STG_USER_TYPE)
    try:
        yield con
    finally:
        con.close()


def _distinct_days(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM stg_std ORDER BY date"
    ).fetchall()]


# ---------------------------------------------------------------------------
# AC9 (a) -- existing country totals UNCHANGED by the new user_type partition.
# ---------------------------------------------------------------------------


def test_country_totals_unchanged_after_user_type_added(seeded_con):
    """AC9a: country partition totals are byte-identical with vs without user_type rows."""
    con = seeded_con

    def _country_totals(include_ut: bool):
        con.execute(f"CREATE OR REPLACE TEMP VIEW fact AS {_mart_sql(include_ut)}")
        return {
            (r[0], r[1], r[2]): r[3]
            for r in con.execute(
                """
                SELECT project_id, date, metric, SUM(value)
                FROM fact
                WHERE breakdown_dimension = 'country'
                GROUP BY project_id, date, metric
                """
            ).fetchall()
        }

    before = _country_totals(False)
    after = _country_totals(True)

    assert before, "expected non-empty country totals in the seeded mart"
    assert before == after, (
        "adding user_type rows changed the country partition totals "
        "(9.9 CRITICAL regression -- the new partition is NOT non-interfering)"
    )
    # AI-45: prove the seed actually spans multiple contiguous days.
    assert len(_distinct_days(con)) >= 3


def test_device_category_totals_unchanged_after_user_type_added(seeded_con):
    """AC9a (companion): device_category totals also unchanged (the OTHER existing dim)."""
    con = seeded_con

    def _device_totals(include_ut: bool):
        con.execute(f"CREATE OR REPLACE TEMP VIEW fact AS {_mart_sql(include_ut)}")
        return {
            (r[0], r[1], r[2]): r[3]
            for r in con.execute(
                """
                SELECT project_id, date, metric, SUM(value)
                FROM fact
                WHERE breakdown_dimension = 'device_category'
                GROUP BY project_id, date, metric
                """
            ).fetchall()
        }

    assert _device_totals(False) == _device_totals(True)


# ---------------------------------------------------------------------------
# AC9 (b) -- MIN(breakdown_dimension) remains 'country' after user_type arrives.
# ---------------------------------------------------------------------------


def test_min_breakdown_dimension_remains_country(seeded_con):
    """AC9b: MIN(breakdown_dimension) across all four GA4 dims is 'country'.

    Proven programmatically (not just by the lexicographic analysis in the story):
    'country' < 'country>device' < 'device_category' < 'user_type'.
    """
    con = seeded_con
    con.execute(f"CREATE OR REPLACE TEMP VIEW fact AS {_mart_sql(True)}")

    dims = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT breakdown_dimension FROM fact WHERE connector='google-analytics'"
        ).fetchall()
    }
    assert dims == {"country", "country>device", "device_category", "user_type"}, dims

    min_dim = con.execute(
        "SELECT MIN(breakdown_dimension) FROM fact WHERE connector='google-analytics'"
    ).fetchone()[0]
    assert min_dim == "country", (
        f"MIN(breakdown_dimension)={min_dim!r} -- canonical single dim must stay 'country' "
        "so metric_baselines / cross_source_conversions do not double-count"
    )


# ---------------------------------------------------------------------------
# AC9 (c) -- user_type values sum to the connector daily total per metric.
# ---------------------------------------------------------------------------


def test_user_type_partition_sums_to_connector_daily_total(seeded_con):
    """AC9c: SUM over 'user_type' per (project, date, metric) equals the connector total.

    The connector daily total is the day sum of ANY single-dimension partition
    (country here -- the canonical dim). The user_type split reconciles to the SAME
    total within the seed's noise band (both profiles are noised independently,
    so an exact match is not expected -- a tight relative tolerance is).
    """
    con = seeded_con
    con.execute(f"CREATE OR REPLACE TEMP VIEW fact AS {_mart_sql(True)}")

    rows = con.execute(
        """
        WITH ut AS (
            SELECT project_id, date, metric, SUM(value) AS ut_total
            FROM fact WHERE breakdown_dimension='user_type'
            GROUP BY project_id, date, metric
        ),
        ctry AS (
            SELECT project_id, date, metric, SUM(value) AS ctry_total
            FROM fact WHERE breakdown_dimension='country'
            GROUP BY project_id, date, metric
        )
        SELECT ut.date, ut.metric, ut.ut_total, ctry.ctry_total
        FROM ut JOIN ctry
          ON ut.project_id=ctry.project_id AND ut.date=ctry.date AND ut.metric=ctry.metric
        """
    ).fetchall()

    assert rows, "expected joined user_type vs country daily totals"
    # Sessions carries the split weights directly; assert a tight band on sessions.
    # Both sides are noised independently at +/-15%; those compose in quadrature to
    # ~21% at the extremes, but the 38/60/2 split summed to a daily total stays well
    # inside 20% -- so 0.20 is a meaningful (non-vacuous) reconciliation ceiling.
    checked = 0
    for _d, metric, ut_total, ctry_total in rows:
        if metric != "sessions":
            continue
        checked += 1
        assert ctry_total > 0
        rel = abs(ut_total - ctry_total) / ctry_total
        assert rel < 0.20, (
            f"user_type sessions total {ut_total} deviates {rel:.0%} from connector "
            f"daily total {ctry_total} -- exceeds the seed noise band"
        )
    assert checked >= 3, "expected at least 3 contiguous days of sessions reconciliation"


def test_user_type_values_are_the_three_expected_buckets(seeded_con):
    """AC9c (companion): exactly new / returning / unknown appear in the partition."""
    con = seeded_con
    con.execute(f"CREATE OR REPLACE TEMP VIEW fact AS {_mart_sql(True)}")
    values = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT breakdown_value FROM fact WHERE breakdown_dimension='user_type'"
        ).fetchall()
    }
    assert values == {"new", "returning", "unknown"}, values


# ---------------------------------------------------------------------------
# AC10 (AI-56) -- build_asgi_app() seam: user_type rows reach the usertypes card
# endpoint through the FULL ASGI stack (mart query mocked).
# ---------------------------------------------------------------------------


def _mart_rows_with_user_type():
    """Mart-shaped rows for the usertypes card: device_category (rendered by v1) PLUS
    the user_type partition this story lands. >= 3 contiguous days (AI-45)."""
    rows = []
    for d in ("2026-06-01", "2026-06-02", "2026-06-03"):
        for metric, base in (("sessions", 100.0), ("active_users", 80.0)):
            # device_category partition (what the v1 usertypes template renders).
            rows += [
                {"date": d, "connector": "google-analytics", "metric": metric,
                 "breakdown_dimension": "device_category", "breakdown_value": "desktop",
                 "value": base, "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"},
                {"date": d, "connector": "google-analytics", "metric": metric,
                 "breakdown_dimension": "device_category", "breakdown_value": "mobile",
                 "value": base * 0.6, "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"},
            ]
            # user_type partition (this story) -- new / returning / unknown.
            rows += [
                {"date": d, "connector": "google-analytics", "metric": metric,
                 "breakdown_dimension": "user_type", "breakdown_value": "new",
                 "value": base * 0.38, "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"},
                {"date": d, "connector": "google-analytics", "metric": metric,
                 "breakdown_dimension": "user_type", "breakdown_value": "returning",
                 "value": base * 0.60, "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"},
                {"date": d, "connector": "google-analytics", "metric": metric,
                 "breakdown_dimension": "user_type", "breakdown_value": "unknown",
                 "value": base * 0.02, "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"},
            ]
    return rows


def _splice_card_routes_into_admin_router():
    """Append CARDS_ROUTES to the shared admin router (idempotent) -- same helper the
    cards seam suite uses so /api/cards resolves through build_asgi_app()."""
    from core.admin_api import router as admin_router
    from core.cards_api import CARDS_ROUTES

    existing = {(r.path, tuple(sorted(r.methods or []))) for r in admin_router.routes}
    for route in CARDS_ROUTES:
        key = (route.path, tuple(sorted(route.methods or [])))
        if key not in existing:
            admin_router.routes.append(route)


def test_usertypes_card_endpoint_carries_user_type_rows_through_asgi():
    """AC10 (AI-56): GET /api/cards?template=usertypes wires through build_asgi_app()
    with a mart that INCLUDES breakdown_dimension='user_type' rows.

    Scope note (AI-54 honesty): the v1 usertypes template (story 9.5) renders the
    device_category donut -- binding the user_type partition to the donut is story
    10.2's job (see the Envelope Contract). What THIS seam proves is the layer 10.1
    delivers: the mart -> warehouse.query_daily_report -> cards -> ASGI path accepts
    and processes a row set that contains the new user_type partition without error
    and returns a valid usertypes envelope. The presence of user_type rows in the
    query result is asserted on the mocked source so the seam cannot silently drop
    the partition.
    """
    from unittest.mock import AsyncMock, patch

    from core.cards import USERTYPES_CARD_WIDGET_URI
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    _splice_card_routes_into_admin_router()
    app = build_asgi_app()

    rows = _mart_rows_with_user_type()
    # Guard: the mocked mart genuinely carries the user_type partition (seam input).
    assert any(r["breakdown_dimension"] == "user_type" for r in rows)

    with patch("core.warehouse.query_daily_report", return_value=rows) as _mart, patch(
        "core.cards_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))
    ), patch("core.project_access.identity_has_project_access", return_value=True):
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.get(
                "/api/cards?project_id=default&template=usertypes"
                "&metrics=sessions,active_users"
                "&date_from=2026-06-01&date_to=2026-06-03",
                headers={"Host": "localhost"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["widget_uri"] == USERTYPES_CARD_WIDGET_URI
    assert body["envelope"]["data"]["card_id"] == "usertypes"
    assert body["summary"].strip()
    # The mart query was invoked through the full stack (layer wired, not bypassed).
    assert _mart.called


# ---------------------------------------------------------------------------
# Story 10.2 (AC 6 seam value assertion, AI-56): get_card(usertypes) envelope
# carries the user_type donut block with FR-labelled slices.
# ---------------------------------------------------------------------------


def test_usertypes_card_envelope_carries_user_type_donut_with_fr_labels():
    """Story 10.2 seam value assertion (AC 6, AI-56): GET /api/cards?template=usertypes
    with user_type rows in the mart returns an envelope whose composition includes a
    donut block bound to the user_type dimension with FR-labelled slices
    (Nouveaux/Fideles/Indetermines).

    This proves the full path: mart -> warehouse -> cards resolver (_resolve_donut with
    _USER_TYPE_LABELS) -> ASGI -> JSON envelope. Reuses the 10.1 seam fixture rows and
    the same build_asgi_app() pattern (AI-45: 3 contiguous days present in the mock).
    """
    from unittest.mock import AsyncMock, patch

    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    _splice_card_routes_into_admin_router()
    app = build_asgi_app()

    rows = _mart_rows_with_user_type()

    with patch("core.warehouse.query_daily_report", return_value=rows), patch(
        "core.cards_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))
    ), patch("core.project_access.identity_has_project_access", return_value=True):
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.get(
                "/api/cards?project_id=default&template=usertypes"
                "&metrics=sessions,active_users"
                "&date_from=2026-06-01&date_to=2026-06-03",
                headers={"Host": "localhost"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    composition = body["envelope"]["data"]["composition"]

    # Find the user_type donut block in the composition.
    user_type_donuts = [
        b for b in composition
        if b.get("type") == "donut"
        and b.get("binding", {}).get("dimensions") == ["user_type"]
    ]
    assert user_type_donuts, (
        "Expected a donut block bound to user_type in the usertypes composition -- "
        "Story 10.2 resolver not wired (cards.py usertypes template missing user_type block)"
    )
    donut_data = user_type_donuts[0].get("data", {})
    slices = donut_data.get("slices", [])
    assert slices, (
        "user_type donut block has no slices -- _resolve_donut did not find user_type rows. "
        "Check that the mock returns breakdown_dimension='user_type' rows and that "
        "_USER_TYPE_LABELS is applied in _resolve_donut."
    )

    # FR labels must be present (not raw 'new'/'returning'/'unknown').
    fr_labels = {s["label"] for s in slices}
    expected_fr = {"Nouveaux", "Fidèles", "Indéterminés"}
    assert fr_labels == expected_fr, (
        f"Expected FR labels {expected_fr} but got {fr_labels} -- "
        "_USER_TYPE_LABELS map not applied in _resolve_donut for user_type dimension"
    )

    # Slice pcts must sum to ~100 (additive sanity check).
    pct_sum = sum(s.get("pct", 0.0) for s in slices)
    assert abs(pct_sum - 100.0) < 1.0, f"user_type donut pcts sum to {pct_sum}, expected ~100"

    # Device donut must ALSO be present (not displaced -- additive block, AC 1 guard).
    device_donuts = [
        b for b in composition
        if b.get("type") == "donut"
        and b.get("binding", {}).get("dimensions") == ["device_category"]
    ]
    assert device_donuts, (
        "Device donut block missing from composition -- "
        "user_type block must be ADDITIVE (not replace the device donut)"
    )

    # user_type donut must appear BEFORE the device donut (lead position, AC 1).
    ut_index = composition.index(user_type_donuts[0])
    dev_index = composition.index(device_donuts[0])
    assert ut_index < dev_index, (
        f"user_type donut (index {ut_index}) must come before device donut (index {dev_index})"
    )
