"""Export app.project_preferences from Postgres to dbt/seeds/project_preferences.csv.

SUPERSEDED by mirror_sync.py (Story 4.4). This script is kept as a fallback for
environments without Postgres. In normal operation, use:
    uv run python -m core.mirror_sync

The governed mirror sync (server/core/mirror_sync.py) replaces this interim CSV
export path. The seed CSV (dbt/seeds/project_preferences.csv) is a CI fallback only
when SYNC_ENABLED=false and Postgres is unavailable.

Story 4.2 — AD-8 Interim bridge (superseded by Story 4.4 mirror_sync).

Usage (fallback only):
    uv run python scripts/export_project_preferences.py

Environment variables:
    PLATFORM_DB_URL — Postgres connection URL (e.g. postgresql://connector:connector@localhost:5432/connector)
                       Defaults to docker-compose default if unset.
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
OUTPUT_CSV = REPO_ROOT / "dbt" / "seeds" / "project_preferences.csv"

# Default Postgres URL for local docker-compose environment
_DEFAULT_DB_URL = "postgresql://connector:connector@localhost:5432/connector"

_COLUMNS = ["project_id", "canonical_currency", "reporting_timezone"]

_SELECT_SQL = """
SELECT project_id, canonical_currency, reporting_timezone
FROM app.project_preferences
ORDER BY project_id
"""


def export(db_url: str | None = None) -> int:
    """Export project_preferences to CSV. Returns row count written."""
    url = db_url or os.environ.get("PLATFORM_DB_URL", _DEFAULT_DB_URL)
    try:
        import psycopg2  # noqa: PLC0415
    except ImportError as exc:
        print(f"ERROR: psycopg2 not installed — cannot export from Postgres: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        conn = psycopg2.connect(url)
    except Exception as exc:
        print(f"ERROR: cannot connect to Postgres ({url}): {exc}", file=sys.stderr)
        print(
            "Hint: ensure PLATFORM_DB_URL is set or docker-compose postgres is running.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT_SQL)
            rows = cur.fetchall()
    finally:
        conn.close()

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(zip(_COLUMNS, row)))

    print(f"Exported {len(rows)} row(s) to {OUTPUT_CSV}")
    return len(rows)


def main() -> None:
    db_url = os.environ.get("PLATFORM_DB_URL")
    export(db_url)


if __name__ == "__main__":
    main()
