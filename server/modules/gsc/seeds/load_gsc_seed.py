"""GSC seed loader — Story 6.2 (extended Story 6.3).

Lands a deterministic set of ``raw_gsc_daily`` rows into a DuckDB file so the
local dev loop (and the seed->mart integration test) can prove the GSC join
end-to-end: rows appear in ``fact_daily_kpi`` with ``connector = 'gsc'`` after
``dbt run``.

Story 6.3 extension: 90 days x 10 pages with realistic position variance and
week-over-week drift to enable WoW delta assertions in ``test_seo_reports.py``.

Seed design decisions (Story 6.3, AC6):
  - 10 pages covering common site sections (home, blog, docs, pricing, etc.).
  - Position distribution: home ranks 2-4; blog 5-10; docs 8-15; pricing 12-20;
    others 20-50.
  - WoW variance: positions drift ±2-5 every 7 days so delta_position can be
    asserted. At least 3 pages are guaranteed to have |delta| >= 2 in at least
    one week pair (seeded deterministically using day offset modular arithmetic).
  - Impressions correlated with position: lower position (better rank) ->
    higher impressions (realistic CTR distribution).
  - Two context_events of type 'deployment' seeded at day-14 and day-35 before
    today for post_deploy_regressions tests (inserted into mirror.context_events
    directly, since mirror_sync.py is responsible for the read path in prod).

Weighted average position proof (preserved from Story 6.2):
  - blog:  impressions=850, position~7.3
  - docs:  impressions=200, position~3.1
  - Naive AVG: (7.3 + 3.1) / 2 = 5.2  <- WRONG
  - Weighted: (7.3*850 + 3.1*200) / (850+200) ~= 6.5  <- CORRECT
  These diverge whenever positions differ (which they do in the seed data).

AD-4: average_position stored raw per row -- NOT pre-aggregated.
AD-7: each invocation mints a fresh pull_id (append-only).

Usage:
    uv run python server/modules/gsc/seeds/load_gsc_seed.py \\
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ulid import ULID

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_gsc_daily (
    date                VARCHAR,
    page                VARCHAR,
    country             VARCHAR,
    device              VARCHAR,
    clicks              INTEGER,
    impressions         INTEGER,
    average_position    DOUBLE,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR
)
"""

_INSERT_SQL = """
INSERT INTO raw_gsc_daily
    (date, page, country, device, clicks, impressions, average_position,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Story 10.5 + GSC full coverage: additive column guards + query-aware INSERT for the
# cannibalisation seed. Mirrors migrations 027 and 043 (nullable/additive). The
# page/country/device rows above leave query NULL; only the _CANNIB_QUERIES rows below
# populate it. search_type/search_appearance/hour stay NULL in seeds (NULL == 'web'
# for the staging filters), matching legacy raw rows.
_ADD_QUERY_COLUMN_DDL = "ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS query VARCHAR"
_ADD_COVERAGE_COLUMN_DDLS = (
    "ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS search_type VARCHAR",
    "ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS search_appearance VARCHAR",
    "ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS hour VARCHAR",
)

_CANNIB_INSERT_SQL = """
INSERT INTO raw_gsc_daily
    (date, page, country, device, query, clicks, impressions, average_position,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# ---------------------------------------------------------------------------
# Cannibalisation seed (Story 10.5, AC10 fixture honesty).
#
# Two queries, each split across 2 pages, landing at the JOINT (query, page) grain
# (query column populated) so they flow into stg_gsc_query_page_daily -> the
# fact_daily_kpi 'query>page' composite -> the cannibalisation resolver.
#
#   "chaussures de sport": /sport/ ~55% share @ pos 4.5, /running/ ~45% share @ pos 8.5.
#       Both pages >= 20% share; weighted-position gap = 8.5 - 4.5 = 4.0 >= 2.0 -> FLAGGED.
#   "blog": /blog/ ~90% share @ pos 3.0, /articles/ ~10% share @ pos 6.0.
#       /articles/ 10% < 20% share threshold -> only ONE qualifying page -> NOT FLAGGED.
#
# Impressions encode the share directly (550/450 and 900/100). Deterministic, flat over
# the seed window (no drift needed -- cannibalisation is a within-day split, not a WoW move).
# ---------------------------------------------------------------------------
_CANNIB_QUERIES = [
    {
        "query": "chaussures de sport",
        "page": "https://example.com/sport/",
        "country": "fra", "device": "desktop",
        "impressions": 550, "clicks": 44, "average_position": 4.5,
    },
    {
        "query": "chaussures de sport",
        "page": "https://example.com/running/",
        "country": "fra", "device": "desktop",
        "impressions": 450, "clicks": 18, "average_position": 8.5,
    },
    {
        "query": "blog",
        "page": "https://example.com/blog/",
        "country": "fra", "device": "desktop",
        "impressions": 900, "clicks": 90, "average_position": 3.0,
    },
    {
        "query": "blog",
        "page": "https://example.com/articles/",
        "country": "fra", "device": "desktop",
        "impressions": 100, "clicks": 5, "average_position": 6.0,
    },
]

# ---------------------------------------------------------------------------
# Seed page catalog (Story 6.3, AC6): 10 pages with realistic position ranges.
#
# base_position: the "average" position for the page over the seed window.
# Position distribution:
#   home:       2-4  (strong brand signal)
#   blog:       5-10 (content authority)
#   docs:       8-15 (long-tail informational)
#   pricing:    12-20 (commercial, competitive)
#   others:     20-50 (weaker pages)
#
# WoW drift: positions shift ±drift_amplitude every 7 days (sinusoidal).
# Pages with drift_amplitude >= 2 produce |delta_position| >= 2 in week pairs,
# satisfying AC6's "at least 3 pages with |delta| >= 2".
# ---------------------------------------------------------------------------
_PAGES = [
    {
        "page": "https://example.com/",
        "country": "fra",
        "device": "desktop",
        "base_position": 2.8,
        "drift_amplitude": 1.0,   # small drift — home page is stable
        "base_impressions": 4200,
        "base_clicks": 210,
    },
    {
        "page": "https://example.com/blog/",
        "country": "fra",
        "device": "desktop",
        "base_position": 7.3,
        "drift_amplitude": 3.0,   # substantial drift -> WoW delta >= 2
        "base_impressions": 850,
        "base_clicks": 42,
    },
    {
        "page": "https://example.com/docs/",
        "country": "fra",
        "device": "desktop",
        "base_position": 3.1,
        "drift_amplitude": 2.5,   # drift -> WoW delta >= 2
        "base_impressions": 200,
        "base_clicks": 15,
    },
    {
        "page": "https://example.com/pricing/",
        "country": "gbr",
        "device": "mobile",
        "base_position": 5.4,
        "drift_amplitude": 2.0,   # moderate drift -> WoW delta = 2
        "base_impressions": 180,
        "base_clicks": 8,
    },
    {
        "page": "https://example.com/contact/",
        "country": "fra",
        "device": "desktop",
        "base_position": 25.0,
        "drift_amplitude": 4.0,   # larger drift at lower rank
        "base_impressions": 60,
        "base_clicks": 2,
    },
    {
        "page": "https://example.com/about/",
        "country": "fra",
        "device": "desktop",
        "base_position": 18.0,
        "drift_amplitude": 3.5,
        "base_impressions": 90,
        "base_clicks": 4,
    },
    {
        "page": "https://example.com/products/",
        "country": "gbr",
        "device": "desktop",
        "base_position": 12.0,
        "drift_amplitude": 2.5,
        "base_impressions": 320,
        "base_clicks": 18,
    },
    {
        "page": "https://example.com/faq/",
        "country": "fra",
        "device": "mobile",
        "base_position": 30.0,
        "drift_amplitude": 5.0,
        "base_impressions": 45,
        "base_clicks": 1,
    },
    {
        "page": "https://example.com/api/",
        "country": "fra",
        "device": "desktop",
        "base_position": 8.5,
        "drift_amplitude": 2.0,
        "base_impressions": 150,
        "base_clicks": 9,
    },
    {
        "page": "https://example.com/changelog/",
        "country": "gbr",
        "device": "desktop",
        "base_position": 22.0,
        "drift_amplitude": 3.0,
        "base_impressions": 75,
        "base_clicks": 3,
    },
]

# ---------------------------------------------------------------------------
# Context events for post_deploy_regressions (Story 6.3, AC6).
# Seeded directly into mirror.context_events (the DuckDB mirror read path).
# Two events: day-14 and day-35 before today.
# ---------------------------------------------------------------------------
_CONTEXT_EVENTS_DDL = """
CREATE SCHEMA IF NOT EXISTS mirror;
CREATE TABLE IF NOT EXISTS mirror.context_events (
    id          VARCHAR,
    project_id  VARCHAR,
    event_date  VARCHAR,
    type        VARCHAR,
    label       VARCHAR,
    description VARCHAR,
    created_by  VARCHAR
)
"""

_CONTEXT_EVENT_INSERT = """
INSERT INTO mirror.context_events
    (id, project_id, event_date, type, label, description, created_by)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def _position_for_day(page_cfg: dict, day_offset: int) -> float:
    """Compute sinusoidal position drift for a given day offset.

    Uses a 7-day period sine wave so week-over-week deltas are predictable
    and deterministic. Positions are clamped to [1.0, 100.0].
    """
    amplitude = page_cfg["drift_amplitude"]
    base = page_cfg["base_position"]
    # Full sine period = 14 days so consecutive weeks have opposite phases.
    drift = amplitude * math.sin(2 * math.pi * day_offset / 14)
    return max(1.0, min(100.0, round(base + drift, 1)))


def _impressions_for_position(base_impr: int, position: float) -> int:
    """Impressions decrease as position worsens (higher position number = worse)."""
    # Exponential decay: impressions ~ base * exp(-0.08 * (position - 1))
    factor = math.exp(-0.08 * max(0.0, position - 1))
    return max(1, int(round(base_impr * factor)))


def _clicks_for_impressions(base_clk: int, impressions: int, base_impr: int) -> int:
    """Scale clicks proportionally to impressions."""
    if base_impr <= 0:
        return base_clk
    return max(0, int(round(base_clk * impressions / base_impr)))


def generate_rows(days: int = 90, project_id: str = "default") -> list[dict]:
    """Return deterministic raw GSC rows for the last *days* days (90d x 10 pages).

    Positions drift sinusoidally with a 14-day period so week-over-week deltas
    are predictable and at least 3 pages produce |delta| >= 2 per week pair.
    """
    rows: list[dict] = []
    end = date.today() - timedelta(days=1)
    for offset in range(days):
        d = end - timedelta(days=offset)
        d_str = d.isoformat()
        for p in _PAGES:
            position = _position_for_day(p, offset)
            impressions = _impressions_for_position(p["base_impressions"], position)
            clicks = _clicks_for_impressions(p["base_clicks"], impressions, p["base_impressions"])
            rows.append(
                {
                    "page": p["page"],
                    "country": p["country"],
                    "device": p["device"],
                    "clicks": clicks,
                    "impressions": impressions,
                    "average_position": position,
                    "date": d_str,
                    "project_id": project_id,
                }
            )
    return rows


def generate_cannib_rows(days: int = 30, project_id: str = "default") -> list[dict]:
    """Return deterministic (query, page)-grain cannibalisation seed rows (Story 10.5).

    30 days x _CANNIB_QUERIES, flat (no drift). Each row carries a populated ``query`` so
    it lands in stg_gsc_query_page_daily (query IS NOT NULL) and the query>page composite.
    """
    rows: list[dict] = []
    end = date.today() - timedelta(days=1)
    for offset in range(days):
        d_str = (end - timedelta(days=offset)).isoformat()
        for q in _CANNIB_QUERIES:
            rows.append(
                {
                    "date": d_str,
                    "page": q["page"],
                    "country": q["country"],
                    "device": q["device"],
                    "query": q["query"],
                    "clicks": q["clicks"],
                    "impressions": q["impressions"],
                    "average_position": q["average_position"],
                    "project_id": project_id,
                }
            )
    return rows


def load_cannib_duckdb(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
    project_id: str = "default",
) -> int:
    """Insert cannibalisation (query, page)-grain rows into raw_gsc_daily (append-only)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    con.execute(_CREATE_DDL)
    con.execute(_ADD_QUERY_COLUMN_DDL)  # additive/nullable (migration 027 companion)
    for ddl in _ADD_COVERAGE_COLUMN_DDLS:  # migration 043 companion
        con.execute(ddl)
    values = [
        (
            r["date"],
            r["page"],
            r["country"],
            r["device"],
            r["query"],
            int(r["clicks"]),
            int(r["impressions"]),
            float(r["average_position"]),
            pull_id,
            loaded_at,
            r.get("project_id", project_id),
        )
        for r in rows
    ]
    con.executemany(_CANNIB_INSERT_SQL, values)
    con.close()
    return len(values)


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _mint_evt_id() -> str:
    return f"evt_{ULID()}"


def load_duckdb(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
    project_id: str = "default",
) -> int:
    """Insert *rows* into raw_gsc_daily in a DuckDB file (append-only)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    con.execute(_CREATE_DDL)
    # Story 10.5: keep the schema consistent whether or not cannib rows are loaded next.
    con.execute(_ADD_QUERY_COLUMN_DDL)
    for ddl in _ADD_COVERAGE_COLUMN_DDLS:  # migration 043 companion
        con.execute(ddl)
    values = [
        (
            r["date"],
            r["page"],
            r["country"],
            r["device"],
            int(r["clicks"]),
            int(r["impressions"]),
            float(r["average_position"]),
            pull_id,
            loaded_at,
            r.get("project_id", project_id),
        )
        for r in rows
    ]
    con.executemany(_INSERT_SQL, values)
    con.close()
    return len(values)


def seed_context_events(duckdb_path: str, project_id: str = "default") -> int:
    """Seed two deployment context events into mirror.context_events.

    Events at day-14 and day-35 before today. Returns count of seeded events.
    Used by the post_deploy_regressions test path (Story 6.3, AC6).
    """
    import duckdb  # noqa: PLC0415

    today = date.today()
    events = [
        {
            "id": _mint_evt_id(),
            "project_id": project_id,
            "event_date": (today - timedelta(days=14)).isoformat(),
            "type": "deployment",
            "label": "Deploy v2.1",
            "description": "Release 2.1 — new landing page",
            "created_by": "seed",
        },
        {
            "id": _mint_evt_id(),
            "project_id": project_id,
            "event_date": (today - timedelta(days=35)).isoformat(),
            "type": "release",
            "label": "Release v2.0",
            "description": "Major release — blog redesign",
            "created_by": "seed",
        },
    ]
    con = duckdb.connect(duckdb_path)
    for stmt in _CONTEXT_EVENTS_DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)
    # Avoid duplicate events on re-seed.
    con.execute(
        "DELETE FROM mirror.context_events WHERE project_id = ? AND created_by = 'seed'",
        [project_id],
    )
    for evt in events:
        con.execute(
            _CONTEXT_EVENT_INSERT,
            [
                evt["id"],
                evt["project_id"],
                evt["event_date"],
                evt["type"],
                evt["label"],
                evt["description"],
                evt["created_by"],
            ],
        )
    con.close()
    return len(events)


def run(duckdb_path: str, days: int = 90, project_id: str = "default") -> tuple[str, int]:
    """Generate + load GSC seed rows (page grain + Story 10.5 cannibalisation grain).

    Returns (pull_id, total_row_count). Both blocks share the same pull_id.
    """
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    rows = generate_rows(days=days, project_id=project_id)
    count = load_duckdb(rows, pull_id, loaded_at, duckdb_path, project_id=project_id)
    # Story 10.5: cannibalisation (query, page)-grain rows (30 days).
    cannib_rows = generate_cannib_rows(days=min(days, 30), project_id=project_id)
    count += load_cannib_duckdb(
        cannib_rows, pull_id, loaded_at, duckdb_path, project_id=project_id
    )
    seed_context_events(duckdb_path, project_id=project_id)
    return pull_id, count


def main() -> None:
    here = Path(__file__).parent
    default_duckdb = os.environ.get(
        "TOOROW_DUCKDB_PATH",
        str(here.parents[1] / "google-analytics" / "seeds" / "local.duckdb"),
    )
    parser = argparse.ArgumentParser(description="Load GSC seed rows into DuckDB")
    parser.add_argument("--duckdb-path", default=default_duckdb, help="DuckDB file path")
    parser.add_argument("--days", type=int, default=90, help="Number of days to seed")
    args = parser.parse_args()

    pull_id, count = run(args.duckdb_path, days=args.days)
    print(f"Loaded {count} GSC rows  pull_id={pull_id}  -> {args.duckdb_path}")
    print()
    print("Seed design (Story 6.3): 90 days x 10 pages with WoW position drift.")
    print("At least 3 pages have |delta_position| >= 2 per week pair.")
    print("Two deployment context events seeded into mirror.context_events.")
    print()
    print("Weighted avg position proof (blog + docs, fra, desktop):")
    print("  Page /blog/: impressions~850, position~7.3")
    print("  Page /docs/: impressions~200, position~3.1")
    print("  Naive AVG:    (7.3 + 3.1) / 2 = 5.2  <- WRONG")
    print("  Weighted AVG: (7.3*850 + 3.1*200) / (850+200) ~= 6.50  <- CORRECT")


if __name__ == "__main__":
    main()
