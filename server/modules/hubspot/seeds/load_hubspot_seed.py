"""HubSpot CRM seed loader -- Story 15.5 (Epic 15).

Charge un ensemble deterministe de lignes ``raw_hubspot_contacts_daily`` et
``raw_hubspot_deals_daily`` dans un fichier DuckDB pour le loop local dev/CI
et le test d'integration seed->mart. Mirrors la forme du loader klaviyo /
shopify. Append-only (AD-7) : chaque invocation forge un nouveau pull_id et
n'ecrase jamais les lignes existantes. ASCII-only stdout (AI-03).

Usage :
    uv run python server/modules/hubspot/seeds/load_hubspot_seed.py \\
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
from generate_hubspot_seed import generate_rows  # noqa: E402

# F-4 : colonne truncated BOOLEAN (defaut FALSE pour les donnees de seed).
_CREATE_CONTACTS_DDL = """
CREATE TABLE IF NOT EXISTS raw_hubspot_contacts_daily (
    date          VARCHAR,
    new_contacts  INTEGER,
    truncated     BOOLEAN DEFAULT FALSE,
    pull_id       VARCHAR,
    loaded_at     VARCHAR,
    project_id    VARCHAR
)
"""

_INSERT_CONTACTS_SQL = """
INSERT INTO raw_hubspot_contacts_daily
    (date, new_contacts, truncated, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?)
"""

_CREATE_DEALS_DDL = """
CREATE TABLE IF NOT EXISTS raw_hubspot_deals_daily (
    date           VARCHAR,
    deals_created  INTEGER,
    deals_closed   INTEGER,
    deal_amount    DOUBLE,
    currency       VARCHAR,
    truncated      BOOLEAN DEFAULT FALSE,
    pull_id        VARCHAR,
    loaded_at      VARCHAR,
    project_id     VARCHAR
)
"""

_INSERT_DEALS_SQL = """
INSERT INTO raw_hubspot_deals_daily
    (date, deals_created, deals_closed, deal_amount, currency,
     truncated, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def load_duckdb(
    contacts_rows: list[dict],
    deals_rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
    project_id: str = "default",
) -> int:
    """Insere contacts + deals dans DuckDB (append-only, AD-7).

    NULL honnete (AD-9) : les None restent NULL -- jamais 0.
    Retourne le total de lignes inserees (contacts + deals).
    """
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    con.execute(_CREATE_CONTACTS_DDL)
    con.execute(_CREATE_DEALS_DDL)

    # Contacts -- F-4 : truncated=False pour les donnees de seed (pas de cap API).
    contact_values = [
        (
            r["date"],
            r.get("new_contacts"),
            False,  # truncated=False : seed synthetique, pas de cap API
            pull_id,
            loaded_at,
            project_id,
        )
        for r in contacts_rows
    ]
    if contact_values:
        con.executemany(_INSERT_CONTACTS_SQL, contact_values)

    # Deals -- NULL honnete pour deal_amount (AD-9) ; truncated=False pour seed.
    deals_values = [
        (
            r["date"],
            r.get("deals_created"),
            r.get("deals_closed"),
            # deal_amount peut etre None (AD-9): NULL si aucun deal ferme/sans montant.
            r.get("deal_amount"),
            r.get("currency"),
            False,  # truncated=False : seed synthetique, pas de cap API
            pull_id,
            loaded_at,
            project_id,
        )
        for r in deals_rows
    ]
    if deals_values:
        con.executemany(_INSERT_DEALS_SQL, deals_values)

    con.close()
    return len(contact_values) + len(deals_values)


def run(
    duckdb_path: str,
    days: int = 30,
    project_id: str = "default",
) -> tuple[str, int]:
    """Genere + charge les lignes de seed HubSpot CRM. Retourne (pull_id, row_count)."""
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    contacts_rows, deals_rows = generate_rows(days=days)
    count = load_duckdb(
        contacts_rows, deals_rows, pull_id, loaded_at, duckdb_path, project_id=project_id
    )
    return pull_id, count


def main() -> None:
    here = Path(__file__).parent
    default_duckdb = os.environ.get(
        "TOOROW_DUCKDB_PATH",
        str(here.parents[1] / "google-analytics" / "seeds" / "local.duckdb"),
    )
    parser = argparse.ArgumentParser(description="Load HubSpot CRM seed rows into DuckDB")
    parser.add_argument("--duckdb-path", default=default_duckdb, help="Chemin DuckDB")
    parser.add_argument("--days", type=int, default=30, help="Nombre de jours a seeder")
    args = parser.parse_args()

    pull_id, count = run(args.duckdb_path, days=args.days)
    print(f"Loaded {count} HubSpot rows  pull_id={pull_id}  -> {args.duckdb_path}")


if __name__ == "__main__":
    main()
