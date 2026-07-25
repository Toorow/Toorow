"""Regenerate the mediaplan-pacing card fixture from the REAL resolver (AI-54).

Review 22.5 F-4: the widget fixture must be a snapshot of the real
``_resolve_mediaplan_pacing_card`` output on faithful warehouse stand-ins --
never hand-written. This script reuses the pytest stand-ins of
``server/tests/core/test_mediaplan_pacing_card.py`` (same DDL/rows as the 22.4
dbt marts), runs the real resolver, and writes the envelope snapshot to
``ui/cards/mediaplan-pacing/src/__fixtures__/envelope.json``.

Usage (from repo root):
    python scripts/gen_mediaplan_pacing_fixture.py

Stdout is ASCII-only (AI-03).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO / "server" / "tests" / "core"))

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def main() -> int:
    import test_mediaplan_pacing_card as fixture_mod  # noqa: PLC0415

    tmp = tempfile.mkdtemp(prefix="mediaplan_fixture_")
    db_path = os.path.join(tmp, "standin.duckdb")
    fixture_mod._seed_card_db(db_path)

    os.environ["TOOROW_DB_MODE"] = "duckdb"
    os.environ["TOOROW_DUCKDB_PATH"] = db_path

    from core import cards  # noqa: PLC0415

    tpl = next(t for t in cards.CARD_TEMPLATES if t.id == "mediaplan_pacing")

    # Postgres is absent offline: stub get_plan with the seeded plan metadata so
    # the snapshot carries a stable name/version (same values as the stand-ins).
    plan_meta = {
        "id": fixture_mod.PLAN_ID,
        "project_id": fixture_mod.PROJECT_ID,
        "name": "Plan Test 22.5",
        "active_version": {"id": fixture_mod.VERSION_ID},
    }
    conn_ctx = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=MagicMock())
    conn_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=conn_ctx),
        patch("core.mediaplan_store.get_plan", return_value=plan_meta),
    ):
        _summary, envelope, _uri = cards._resolve_mediaplan_pacing_card(
            tpl,
            fixture_mod.PROJECT_ID,
            fixture_mod.PLAN_ID,
            context_events=[],
            alerts=[],
            trace_id=None,
        )

    out_dir = REPO / "ui" / "cards" / "mediaplan-pacing" / "src" / "__fixtures__"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "envelope.json"
    out_path.write_text(
        json.dumps(envelope, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out_path} (lines={len(envelope['data'].get('composition', []))} blocks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
