"""Regenerate the google-analytics widget fixture from the REAL tool (AI-54).

Audit C15: ``ui/widgets/google-analytics/src/fixture.ts`` was hand-written with
synthetic ``pull_FIXTURE...`` ids and an assumed envelope shape ("The real
payload arrives at runtime"). AI-54 requires a widget fixture to mirror what
the server ACTUALLY emits. This script follows the pattern of
``scripts/gen_mediaplan_pacing_fixture.py``: it runs the REAL
``get_daily_report`` tool (``core.reporting_mcp``) against the real local
warehouse built by ``server/modules/google-analytics/seeds/run_local_loop.py``
(the full generate -> load -> dbt run pipeline), with the same offline-Postgres
stubs the unit tests use, and snapshots ``result.structured_content``.

What the widget sees at runtime is the rehydrated envelope
(``ui/shell/src/mcpApp.ts`` ``rehydrateEnvelope`` puts back what the Story 50.6
model-channel split moved to ``_meta``), which is exactly the full
``structured_content`` this script captures -- so the snapshot is the shape the
widget consumes.

Prerequisite: build the local warehouse once (real dbt loop, ~minutes):

    uv run python server/modules/google-analytics/seeds/run_local_loop.py

Usage (from repo root):

    uv run python scripts/gen_ga_widget_fixture.py [--days N] [--out PATH]

Writes:
    ui/widgets/google-analytics/src/__fixtures__/envelope.json   (real snapshot)
    ui/widgets/google-analytics/src/fixture.ts                   (thin generated module)

Stdout is ASCII-only (AI-03).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

WAREHOUSE = REPO / "server" / "modules" / "google-analytics" / "seeds" / "local.duckdb"
WIDGET_SRC = REPO / "ui" / "widgets" / "google-analytics" / "src"

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

FIXTURE_TS_TEMPLATE = '''/**
 * Standalone dev/test fixture (Data Delivery to Widget - Dev Notes).
 *
 * GENERATED FILE - do not hand-edit (AI-54: a widget fixture mirrors what the
 * server ACTUALLY emits, never the speculated shape). Regenerate with:
 *
 *     uv run python scripts/gen_ga_widget_fixture.py
 *
 * The payload in __fixtures__/envelope.json is a snapshot of the REAL
 * get_daily_report structuredContent against the real local warehouse (seed ->
 * dbt -> fact_daily_kpi), captured by the generator script above. What the
 * widget receives at runtime is this same envelope, rehydrated from the
 * Story 50.6 model-channel split (ui/shell/src/mcpApp.ts rehydrateEnvelope).
 *
 * Used only when NO structuredContent is delivered by the host (i.e. the widget
 * is opened outside an MCP host, e.g. `vite preview` or a Node smoke render).
 */

import type { DailyReportEnvelope } from "./types";
import envelope from "./__fixtures__/envelope.json";

export const FIXTURE_ENVELOPE: DailyReportEnvelope =
  envelope as unknown as DailyReportEnvelope;
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=10,
        help="Window length in days ending at the warehouse max date. Default 10: "
        "the full 90-day real payload is ~4 MB compact, and CI asserts the "
        "AD-11 single-file bundle stays < 1.5 MB uncompressed (14 days already "
        "lands at ~1.57 MB). Pass 0 for full coverage (NOT committable).",
    )
    parser.add_argument(
        "--out",
        default=str(WIDGET_SRC / "__fixtures__" / "envelope.json"),
        help="Output JSON path (default: the widget's __fixtures__/envelope.json).",
    )
    args = parser.parse_args()

    if not WAREHOUSE.exists():
        print(f"error: warehouse not found: {WAREHOUSE}")
        print("build it first: uv run python "
              "server/modules/google-analytics/seeds/run_local_loop.py")
        return 1

    # Work on a COPY: the warehouse stays untouched (and unlocked), and dbt
    # bakes the database name into views, so the copy must keep the same stem.
    tmp = Path(tempfile.mkdtemp(prefix="ga_widget_fixture_"))
    db_copy = tmp / WAREHOUSE.name
    shutil.copy2(WAREHOUSE, db_copy)
    wal = WAREHOUSE.with_suffix(".duckdb.wal")
    if wal.exists():
        shutil.copy2(wal, tmp / wal.name)

    os.environ["TOOROW_DB_MODE"] = "duckdb"
    os.environ["TOOROW_DUCKDB_PATH"] = str(db_copy)

    # Resolve the window from the warehouse itself (the real coverage).
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(str(db_copy), read_only=True)
    try:
        min_date, max_date = con.execute(
            "SELECT MIN(date), MAX(date) FROM main_marts.fact_daily_kpi "
            "WHERE connector = 'google-analytics' "
            "AND breakdown_dimension = 'device_category'"
        ).fetchone()
    finally:
        con.close()
    if not min_date or not max_date:
        print("error: no google-analytics rows in the warehouse mart")
        return 1
    end = str(max_date)
    if args.days > 0:
        from datetime import date, timedelta  # noqa: PLC0415

        start = (date.fromisoformat(end) - timedelta(days=args.days - 1)).isoformat()
        if start < str(min_date):
            start = str(min_date)
    else:
        start = str(min_date)

    # Offline-Postgres stubs, same seams as tests/core/test_get_daily_report.py:
    # every platform-DB read degrades gracefully (health, confidence, branding,
    # briefing, alerts), which is the honest offline-dev shape.
    from unittest.mock import patch  # noqa: PLC0415

    import core.db  # noqa: PLC0415
    import core.main as core_main  # noqa: PLC0415

    def _offline_connection(*a, **kw):
        raise RuntimeError("platform database unavailable in fixture generation")

    with (
        patch.object(core_main, "_resolve_project", lambda project_id, identity=None: project_id),
        patch.object(core.db, "get_connection", _offline_connection),
        patch.object(core.db, "request_connection", _offline_connection),
    ):
        result = core_main.get_daily_report(
            project_id="default",
            date_range={"start": start, "end": end},
            connectors=["google-analytics"],
        )

    if result.is_error:
        print(f"error: get_daily_report returned isError: {result.content[0].text}")
        return 1

    envelope = result.structured_content
    rows = envelope["data"]["rows"]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Compact separators: this file is inlined into the AD-11 single-file
    # bundle, whose CI budget is 1.5 MB uncompressed.
    payload = (
        json.dumps(envelope, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
    )
    out_path.write_text(payload, encoding="utf-8")

    (WIDGET_SRC / "fixture.ts").write_text(FIXTURE_TS_TEMPLATE, encoding="utf-8")

    dims = sorted({r["breakdown_dimension"] for r in rows})
    metrics = sorted({r["metric"] for r in rows})
    print(f"window: {start} .. {end}")
    print(f"wrote {out_path} ({len(payload.encode('utf-8'))} bytes, {len(rows)} rows)")
    print(f"metrics: {metrics}")
    print(f"breakdown_dimensions: {dims}")
    print(f"pull_ids: {sorted({r['pull_id'] for r in rows})}")
    print(f"meta keys: {sorted(envelope['meta'].keys())}")
    print(f"data keys: {sorted(envelope['data'].keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
