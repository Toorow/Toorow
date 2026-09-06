#!/usr/bin/env python3
"""The one deterministic path from a reference snapshot to both its consumers.

    python scripts/export_reference_vocabulary.py currency --source-version 2026-07
    python scripts/export_reference_vocabulary.py timezone
    python scripts/export_reference_vocabulary.py --check          # drift, no writes

Story 48.3 (AC1) requires the runtime currency and timezone vocabularies to be
owned by Governance and the dbt seeds to be *generated, hash-pinned projections*
of them -- not second, independently editable authorities.

``scripts/export_country_vocabulary.py`` established this flow for Country. This
script is the same flow for the two Story 48.3 vocabularies, parameterized rather
than copied: a third copy of "import, re-render, pin" is exactly the duplication
the repository's own rules forbid.

  1. read the reviewed snapshot (the seed for currency; the local tz database
     for timezone -- both offline, no ISO and no IANA service is contacted);
  2. import it into ``app.master_data_vocabulary_versions`` as an IMMUTABLE
     version -- idempotent by content hash, so re-running changes nothing;
  3. re-render the dbt seed **from the stored version**, plus a provenance
     sidecar naming the exact version id and content hash.

After step 3 the seed is derived, and ``--check`` proves it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from core import currency_vocabulary, timezone_vocabulary  # noqa: E402
from core.master_data import fetch_vocabulary_version  # noqa: E402


@dataclass(frozen=True)
class Target:
    name: str
    key: str
    seed: Path
    pin: Path
    entries: Callable[[], list[dict[str, Any]]]
    render: Callable[[Any], str]
    provenance: Callable[[Any], dict[str, Any]]
    importer: Callable[..., dict[str, Any]]
    #: Currency snapshots are dated by the operator; the tz release names itself.
    source_version_required: bool


TARGETS = {
    "currency": Target(
        name="currency",
        key=currency_vocabulary.CURRENCY_VOCABULARY_KEY,
        seed=ROOT / "dbt" / "seeds" / "dim_currency.csv",
        pin=ROOT / "dbt" / "seeds" / "dim_currency.provenance.json",
        entries=currency_vocabulary.vocabulary_entries,
        render=currency_vocabulary.render_seed_csv,
        provenance=currency_vocabulary.seed_provenance,
        importer=currency_vocabulary.import_currency_vocabulary,
        source_version_required=True,
    ),
    "timezone": Target(
        name="timezone",
        key=timezone_vocabulary.TIMEZONE_VOCABULARY_KEY,
        seed=ROOT / "dbt" / "seeds" / "dim_timezone.csv",
        pin=ROOT / "dbt" / "seeds" / "dim_timezone.provenance.json",
        entries=timezone_vocabulary.vocabulary_entries,
        render=timezone_vocabulary.render_seed_csv,
        provenance=timezone_vocabulary.seed_provenance,
        importer=timezone_vocabulary.import_timezone_vocabulary,
        source_version_required=False,
    ),
}


def _connect():
    import psycopg  # noqa: PLC0415 -- optional at import time, required to run

    dsn = os.getenv("PLATFORM_DB_URL") or os.getenv("PLATFORM_DATABASE_URL")
    if not dsn:
        raise SystemExit("export requires PLATFORM_DB_URL")
    return psycopg.connect(dsn, connect_timeout=15)


def check_one(target: Target) -> int:
    """Prove the seed still equals what the governed entries render to."""

    expected = target.render(target.entries())
    actual = target.seed.read_text(encoding="utf-8") if target.seed.exists() else ""
    if expected != actual:
        print(
            f"{target.name} seed drift: {target.seed.relative_to(ROOT)} is not the "
            f"rendered projection.\nRun scripts/export_reference_vocabulary.py "
            f"{target.name} to regenerate it.",
            file=sys.stderr,
        )
        return 1
    if not target.pin.exists():
        print(f"{target.name} seed has no provenance pin; run the export", file=sys.stderr)
        return 1
    pin = json.loads(target.pin.read_text(encoding="utf-8"))
    if pin.get("vocabulary_key") != target.key:
        print(f"{target.name} seed provenance names another vocabulary", file=sys.stderr)
        return 1
    print(
        f"{target.name} vocabulary OK: {len(target.entries())} entries, "
        f"pinned to {pin['vocabulary_version_id']}"
    )
    return 0


def export_one(target: Target, *, source_version: str | None, effective: str, actor: str) -> int:
    kwargs: dict[str, Any] = {"actor": actor, "effective_date": effective}
    if source_version:
        kwargs["source_version"] = source_version
    elif target.source_version_required:
        raise SystemExit(
            f"--source-version is required for {target.name}: an import records which "
            "snapshot it is"
        )

    with _connect() as conn:
        vocabulary = target.importer(conn, **kwargs)
        conn.commit()
        stored = fetch_vocabulary_version(conn, vocabulary_version_id=vocabulary["id"])

    if stored is None:  # pragma: no cover - only under a concurrent delete
        raise SystemExit("the vocabulary version disappeared immediately after import")

    target.seed.write_text(target.render(stored["entries"]), encoding="utf-8")
    target.pin.write_text(
        json.dumps(target.provenance(stored), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"{target.name} vocabulary imported: {stored['entry_count']} entries, "
        f"version {stored['id']} ({stored['content_hash'][:12]}...)"
    )
    print(f"wrote {target.seed.relative_to(ROOT)} and {target.pin.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "vocabulary",
        nargs="?",
        choices=sorted(TARGETS),
        help="Which vocabulary to export. Omit with --check to verify both.",
    )
    parser.add_argument(
        "--source-version",
        help="The snapshot this import represents, e.g. 2026-07. Required for currency.",
    )
    parser.add_argument("--effective-date", default=date.today().isoformat())
    parser.add_argument("--actor", default="build")
    parser.add_argument("--check", action="store_true", help="verify, write nothing")
    args = parser.parse_args(argv)

    selected = [TARGETS[args.vocabulary]] if args.vocabulary else list(TARGETS.values())

    if args.check:
        return max(check_one(target) for target in selected)
    if not args.vocabulary:
        parser.error("naming the vocabulary is required to write")
    return export_one(
        selected[0],
        source_version=args.source_version,
        effective=args.effective_date,
        actor=args.actor,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
