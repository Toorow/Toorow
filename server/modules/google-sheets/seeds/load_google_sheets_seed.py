"""Google Sheets seed loader -- Story 15.6 (Epic 15).

Charge un ensemble deterministe de lignes ``raw_google_sheets_daily`` dans un
fichier DuckDB pour le loop local dev/CI et le test d'integration seed->mart.
Mirrors le loader klaviyo / linkedin-ads. Append-only (AD-7) : chaque invocation
forge un nouveau pull_id et n'ecrase jamais les lignes existantes.
ASCII-only stdout (AI-03).

Usage :
    uv run python server/modules/google-sheets/seeds/load_google_sheets_seed.py \\
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
from generate_google_sheets_seed import generate_rows  # noqa: E402

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_google_sheets_daily (
    date                VARCHAR,
    sheet_row_id        VARCHAR,
    budget_declared     DOUBLE,
    target_revenue      DOUBLE,
    target_conversions  DOUBLE,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR,
    spreadsheet_id      VARCHAR,
    sheet_name          VARCHAR
)
"""

_INSERT_SQL = """
INSERT INTO raw_google_sheets_daily
    (date, sheet_row_id, budget_declared, target_revenue, target_conversions,
     pull_id, loaded_at, project_id, spreadsheet_id, sheet_name)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def load_duckdb(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
    project_id: str = "default",
    spreadsheet_id: str = "seed_spreadsheet",
    sheet_name: str = "seed_sheet",
) -> int:
    """Insere *rows* dans raw_google_sheets_daily dans un fichier DuckDB (append-only).

    NULL honnete (AD-9) : les valeurs None dans les lignes de seed restent NULL --
    jamais remplacees par 0. Le loader propage les None tels quels via DuckDB.
    """
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    con.execute(_CREATE_DDL)

    def _coerce_float(v: float | None) -> float | None:
        if v is None:
            return None
        return float(v)

    values = [
        (
            r["date"],
            r.get("sheet_row_id", ""),
            _coerce_float(r.get("budget_declared")),
            _coerce_float(r.get("target_revenue")),
            _coerce_float(r.get("target_conversions")),
            pull_id,
            loaded_at,
            project_id,
            spreadsheet_id,
            sheet_name,
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
    """Genere + charge les lignes de seed Google Sheets. Retourne (pull_id, row_count)."""
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
    parser = argparse.ArgumentParser(description="Load Google Sheets seed rows into DuckDB")
    parser.add_argument("--duckdb-path", default=default_duckdb, help="Chemin DuckDB")
    parser.add_argument("--days", type=int, default=40, help="Nombre de jours a seeder")
    args = parser.parse_args()

    pull_id, count = run(args.duckdb_path, days=args.days)
    print(f"Loaded {count} Google Sheets rows  pull_id={pull_id}  -> {args.duckdb_path}")


if __name__ == "__main__":
    main()
