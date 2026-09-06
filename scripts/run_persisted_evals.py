"""Run the deterministic benchmark, then persist its Postgres projection.

This command is the production caller Story 45.8 lacked.  It intentionally sits
beside, rather than inside, ``run_evals.py``: the original runner remains a pure
offline/DuckDB gate, while this explicit operator command owns the Postgres
transaction and the governed missing-link producer.

Usage::

    PLATFORM_DB_URL=postgresql://... \
      uv run python scripts/run_persisted_evals.py --project-id proj_123
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parent
_SERVER = _REPO / "server"
for _path in (str(_SERVER), str(_SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import run_evals  # noqa: E402
from core.db import get_connection  # noqa: E402
from core.eval_benchmark_persistence import persist_benchmark_evaluation  # noqa: E402


def _run_at(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--run-at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--run-at must include a timezone")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the offline benchmark and persist its governed evidence."
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--run-at",
        type=_run_at,
        default=None,
        help="ISO-8601 timestamp; defaults to now.",
    )
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--only", default=None, help="Optional corpus tag or surface filter.")
    args = parser.parse_args(argv)
    run_at = args.run_at or dt.datetime.now(dt.UTC)
    if not os.environ.get("PLATFORM_DB_URL", "").strip():
        parser.error("PLATFORM_DB_URL is required for persisted evaluation runs")

    corpus = run_evals.load_corpus()
    artifact = run_evals.run(corpus, as_of_override=args.as_of, only=args.only)
    with get_connection() as conn:
        persisted = persist_benchmark_evaluation(
            conn,
            project_id=args.project_id,
            run_at=run_at,
            corpus=corpus,
            artifact=artifact,
        )
        conn.commit()

    print(json.dumps(persisted, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
