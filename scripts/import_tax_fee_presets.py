#!/usr/bin/env python3
"""Import the shipped country tax seed as governed Tax & Fee preset versions.

    python scripts/import_tax_fee_presets.py --dsn "$PLATFORM_DB_URL"
    python scripts/import_tax_fee_presets.py --dsn "$PLATFORM_DB_URL" --publish

Without this, ``app.tax_fee_preset_versions`` stays empty in every database and
``auto_populate_tax_rules`` has exactly one possible answer -- the typed gap
``no_published_tax_fee_presets``. The logic lives in
``core.tax_fee_preset_seeding``; this file is a thin runner so the import can be
unit-tested offline against a temporary CSV.

``--publish`` is REQUIRED to publish, and there is no default that publishes
silently: a published preset is what ``propose_from_presets`` narrows over, so
publishing is an operator act about rates nobody here verified. Every imported
preset carries ``verification_status='unverified'`` and the seed row's own
sentence -- "confirm against your platform invoice and your tax advisor before
use" -- verbatim in its assumptions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from core.tax_fee_preset_seeding import import_seed_presets  # noqa: E402


def _connect(dsn: str | None):
    import psycopg  # noqa: PLC0415 -- optional at import time, required to run

    resolved = dsn or os.getenv("PLATFORM_DB_URL") or os.getenv("PLATFORM_DATABASE_URL")
    if not resolved:
        raise SystemExit("this import requires --dsn or PLATFORM_DB_URL")
    return psycopg.connect(resolved, connect_timeout=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=None, help="Postgres DSN (else PLATFORM_DB_URL)")
    parser.add_argument(
        "--actor", default="operator", help="who is recorded as having imported the rows"
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="publish the rows this run creates (they are drafts otherwise)",
    )
    parser.add_argument(
        "--seed-path", default=None, help="override the seed CSV (defaults to the dbt seed)"
    )
    args = parser.parse_args(argv)

    seed_path = Path(args.seed_path) if args.seed_path else None
    with _connect(args.dsn) as conn:
        result = import_seed_presets(
            conn, actor=args.actor, publish=args.publish, path=seed_path
        )
        conn.commit()

    # The ids are useful to a follow-up query and useless to read; the counts are
    # the answer, so they are printed on their own line first.
    print(
        f"rows={result['rows']} created={result['created']} "
        f"unchanged={result['unchanged']} published={result['published']} "
        f"source={result['source_reference_version']}"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "preset_version_ids"}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover -- operator entry point
    raise SystemExit(main())
