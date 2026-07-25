"""Klaviyo seed loader -- Story 15.8 (Epic 15).

Charge un ensemble deterministe de lignes ``raw_klaviyo_daily`` dans un fichier
DuckDB pour le loop local dev/CI et le test d'integration seed->mart.
Mirrors la forme du loader meta-ads / shopify. Append-only (AD-7) : chaque
invocation forge un nouveau pull_id et n'ecrase jamais les lignes existantes.
ASCII-only stdout (AI-03).

Usage :
    uv run python server/modules/klaviyo/seeds/load_klaviyo_seed.py \\
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

# Reutilise le generateur pour que les lignes de seed aient la forme canonique
# du parsing (AI-54 : fixture generee depuis le connecteur, pas a la main).
sys.path.insert(0, str(Path(__file__).parent))
from generate_klaviyo_seed import generate_rows  # noqa: E402

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_klaviyo_daily (
    date                     VARCHAR,
    campaign_id              VARCHAR,
    campaign_name            VARCHAR,
    flow_id                  VARCHAR,
    flow_name                VARCHAR,
    sends                    DOUBLE,
    opens                    DOUBLE,
    clicks                   DOUBLE,
    attributed_conversions   DOUBLE,
    attributed_revenue       DOUBLE,
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""

_INSERT_SQL = """
INSERT INTO raw_klaviyo_daily
    (date, campaign_id, campaign_name, flow_id, flow_name,
     sends, opens, clicks, attributed_conversions, attributed_revenue,
     pull_id, loaded_at, project_id)
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
    """Insere *rows* dans raw_klaviyo_daily dans un fichier DuckDB (append-only).

    NULL honnete (AD-9) : les valeurs None dans les lignes de seed restent NULL --
    jamais remplacees par 0. Le loader propage les None tels quels via DuckDB.
    """
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    con.execute(_CREATE_DDL)

    def _coerce_float(v: float | None) -> float | None:
        """None reste None ; sinon cast float."""
        if v is None:
            return None
        return float(v)

    values = [
        (
            r["date"],
            r.get("campaign_id") or None,
            r.get("campaign_name") or None,
            r.get("flow_id") or None,
            r.get("flow_name") or None,
            _coerce_float(r.get("sends")),
            _coerce_float(r.get("opens")),
            _coerce_float(r.get("clicks")),
            # REGLE : attributed_conversions stocke en colonne propre (AD-4).
            _coerce_float(r.get("attributed_conversions")),
            # REGLE : attributed_revenue stocke en colonne propre (AD-4).
            _coerce_float(r.get("attributed_revenue")),
            pull_id,
            loaded_at,
            project_id,
        )
        for r in rows
    ]
    if values:
        con.executemany(_INSERT_SQL, values)
    con.close()
    return len(values)


def run(
    duckdb_path: str,
    days: int = 40,
    project_id: str = "default",
) -> tuple[str, int]:
    """Genere + charge les lignes de seed Klaviyo. Retourne (pull_id, row_count)."""
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    rows = generate_rows(days=days)
    count = load_duckdb(rows, pull_id, loaded_at, duckdb_path, project_id=project_id)
    return pull_id, count


def main() -> None:
    here = Path(__file__).parent
    default_duckdb = os.environ.get(
        "TOOROW_DUCKDB_PATH",
        str(here.parents[1] / "google-analytics" / "seeds" / "local.duckdb"),
    )
    parser = argparse.ArgumentParser(description="Load Klaviyo seed rows into DuckDB")
    parser.add_argument("--duckdb-path", default=default_duckdb, help="Chemin DuckDB")
    parser.add_argument("--days", type=int, default=40, help="Nombre de jours a seeder")
    args = parser.parse_args()

    pull_id, count = run(args.duckdb_path, days=args.days)
    print(f"Loaded {count} Klaviyo rows  pull_id={pull_id}  -> {args.duckdb_path}")


if __name__ == "__main__":
    main()
