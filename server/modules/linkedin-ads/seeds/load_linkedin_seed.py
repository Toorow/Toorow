"""LinkedIn Ads seed loader — Story 15.3 (Epic 15).

Atterrit un jeu deterministe de lignes ``raw_linkedin_ads_daily`` dans un fichier
DuckDB pour la boucle dev locale (et le test d'integration seed->mart).

Miroir de la forme des loaders meta-ads / tiktok-ads / shopify. Append-only (AD-7) :
chaque invocation mint un nouveau pull_id et n'ecrase jamais les lignes existantes.
Stdout ASCII-only (AI-03).

Usage :
    uv run python server/modules/linkedin-ads/seeds/load_linkedin_seed.py \\
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import os
import sys as _sys
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

_sys.path.insert(0, str(Path(__file__).parent))
from generate_linkedin_seed import (  # noqa: E402
    generate_multigrain_rows,
    generate_rows,
)

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_linkedin_ads_daily (
    date                                VARCHAR,
    data_level                          VARCHAR,
    campaign_id                         VARCHAR,
    campaign_group_id                   VARCHAR,
    costInLocalCurrency                 DOUBLE,
    impressions                         INTEGER,
    clicks                              INTEGER,
    externalWebsiteConversions          INTEGER,
    leadGenerationMailContactInfoShares INTEGER,
    pull_id                             VARCHAR,
    loaded_at                           VARCHAR,
    project_id                          VARCHAR,
    cost_source_currency                VARCHAR DEFAULT 'EUR'
)
"""

_INSERT_SQL = """
INSERT INTO raw_linkedin_ads_daily
    (date, data_level, campaign_id, campaign_group_id,
     costInLocalCurrency, impressions, clicks,
     externalWebsiteConversions, leadGenerationMailContactInfoShares,
     pull_id, loaded_at, project_id, cost_source_currency)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def load_duckdb(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
    project_id: str = "default",
) -> int:
    """Insere *rows* dans raw_linkedin_ads_daily (DuckDB, append-only)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    con.execute(_CREATE_DDL)
    values = [
        (
            r["date"],
            r.get("data_level"),
            r.get("campaign_id"),
            r.get("campaign_group_id"),
            float(r["costInLocalCurrency"]) if r.get("costInLocalCurrency") is not None else None,
            int(r["impressions"]) if r.get("impressions") is not None else None,
            int(r["clicks"]) if r.get("clicks") is not None else None,
            int(r["externalWebsiteConversions"])
            if r.get("externalWebsiteConversions") is not None else None,
            int(r["leadGenerationMailContactInfoShares"])
            if r.get("leadGenerationMailContactInfoShares") is not None else None,
            pull_id,
            loaded_at,
            project_id,
            r.get("cost_source_currency", "EUR"),
        )
        for r in rows
    ]
    con.executemany(_INSERT_SQL, values)
    con.close()
    return len(values)


def run(
    duckdb_path: str,
    days: int = 40,
    project_id: str = "default",
    grains: str = "campaign",
) -> tuple[str, int]:
    """Genere + charge les lignes LinkedIn. Retourne (pull_id, row_count).

    grains='campaign' atterrit le grain campaign seul ;
    grains='multi' atterrit les deux grains coexistant (F-1 anti-grain-bleed).
    """
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if grains == "multi":
        rows = generate_multigrain_rows(days=days)
    else:
        rows = generate_rows(days=days)
    count = load_duckdb(rows, pull_id, loaded_at, duckdb_path, project_id=project_id)
    return pull_id, count


def main() -> None:
    here = Path(__file__).parent
    default_duckdb = os.environ.get(
        "TOOROW_DUCKDB_PATH",
        str(here.parents[1] / "google-analytics" / "seeds" / "local.duckdb"),
    )
    parser = argparse.ArgumentParser(description="Load LinkedIn Ads seed rows into DuckDB")
    parser.add_argument("--duckdb-path", default=default_duckdb, help="DuckDB file path")
    parser.add_argument("--days", type=int, default=40, help="Number of days to seed")
    parser.add_argument(
        "--grains",
        choices=["campaign", "multi"],
        default="campaign",
        help="campaign = grain campaign seul ; multi = 2 grains coexistant (F-1)",
    )
    args = parser.parse_args()

    pull_id, count = run(args.duckdb_path, days=args.days, grains=args.grains)
    print(
        f"Loaded {count} LinkedIn rows ({args.grains} grains)  "
        f"pull_id={pull_id}  -> {args.duckdb_path}"
    )


if __name__ == "__main__":
    main()
