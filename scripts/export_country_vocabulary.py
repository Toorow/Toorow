#!/usr/bin/env python3
"""The one deterministic path from an ISO snapshot to both Country consumers.

    python scripts/export_country_vocabulary.py --source-version 2026-07
    python scripts/export_country_vocabulary.py --check     # drift, no writes

Story 48.2 (AC2) requires the runtime Country vocabulary to be owned by
Governance and the dbt seed to be a *generated, hash-pinned projection* of it --
not a second, independently editable authority. Before this script, the seed was
the source: anyone could edit a CSV and change what "a valid country" means for
every Project, with no version, no provenance and no way to notice.

The flow this script performs, in order:

  1. read the reviewed snapshot (the current seed on first run);
  2. import it into ``app.master_data_vocabulary_versions`` as an IMMUTABLE
     version -- idempotent by content hash, so re-running changes nothing;
  3. re-render the dbt seed **from the stored version**, plus a provenance
     sidecar naming the exact version id and content hash.

After step 3 the seed is derived, and ``--check`` proves it: it re-renders and
compares, so a hand edit is a non-zero exit rather than a silent divergence.

No ISO, UN or CLDR service is contacted. Refreshing the snapshot is a deliberate
build operation someone performs and reviews, which is exactly why the source
version is a required argument and never a default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from core.country_registry import (  # noqa: E402
    COUNTRY_VOCABULARY_KEY,
    import_country_vocabulary,
    render_seed_csv,
    seed_provenance,
    vocabulary_entries,
)
from core.master_data import fetch_vocabulary_version  # noqa: E402

SEED_PATH = ROOT / "dbt" / "seeds" / "dim_country.csv"
PIN_PATH = ROOT / "dbt" / "seeds" / "dim_country.provenance.json"


def _connect():
    import psycopg  # noqa: PLC0415 -- optional at import time, required to run

    dsn = os.getenv("PLATFORM_DB_URL") or os.getenv("PLATFORM_DATABASE_URL")
    if not dsn:
        raise SystemExit("export requires PLATFORM_DB_URL")
    return psycopg.connect(dsn, connect_timeout=10)


def _render(entries) -> str:
    return render_seed_csv(entries)


def check_only() -> int:
    """Prove the seed still equals what the governed entries render to."""

    entries = vocabulary_entries()
    expected = _render(entries)
    actual = SEED_PATH.read_text(encoding="utf-8") if SEED_PATH.exists() else ""
    if expected != actual:
        print(
            "country seed drift: dbt/seeds/dim_country.csv is not the rendered projection.\n"
            "Run scripts/export_country_vocabulary.py to regenerate it.",
            file=sys.stderr,
        )
        return 1
    if not PIN_PATH.exists():
        print("country seed has no provenance pin; run the export", file=sys.stderr)
        return 1
    pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    if pin.get("vocabulary_key") != COUNTRY_VOCABULARY_KEY:
        print("country seed provenance names another vocabulary", file=sys.stderr)
        return 1
    print(f"country vocabulary OK: {len(entries)} entries, pinned to {pin['vocabulary_version_id']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-version",
        help="The ISO snapshot this import represents, e.g. 2026-07. Required to write.",
    )
    parser.add_argument("--effective-date", default=date.today().isoformat())
    parser.add_argument("--actor", default="build")
    parser.add_argument("--check", action="store_true", help="verify, write nothing")
    args = parser.parse_args(argv)

    if args.check:
        return check_only()
    if not args.source_version:
        parser.error("--source-version is required: an import records which snapshot it is")

    entries = vocabulary_entries()
    with _connect() as conn:
        vocabulary = import_country_vocabulary(
            conn,
            actor=args.actor,
            source_version=args.source_version,
            effective_date=args.effective_date,
        )
        conn.commit()
        stored = fetch_vocabulary_version(conn, vocabulary_version_id=vocabulary["id"])

    if stored is None:  # pragma: no cover - only under a concurrent delete
        raise SystemExit("the vocabulary version disappeared immediately after import")

    SEED_PATH.write_text(_render(stored["entries"]), encoding="utf-8")
    PIN_PATH.write_text(
        json.dumps(seed_provenance(stored), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"country vocabulary imported: {stored['entry_count']} entries, "
        f"version {stored['id']} ({stored['content_hash'][:12]}...)"
    )
    print(f"wrote {SEED_PATH.relative_to(ROOT)} and {PIN_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
