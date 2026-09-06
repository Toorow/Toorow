"""Generate the real PG/FastMCP/HTTP AI Path branch parity fixture."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from tests.fixture_generators.ai_path_branch_evidence import build_fixture  # noqa: E402

FIXTURE_PATH = (
    ROOT
    / "ui/cards/shell/src/viz/__tests__/fixtures/aiPathBranchEvidence.json"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        parser.error("TEST_POSTGRES_DSN is required; use a disposable migrated PostgreSQL")

    fixture = asyncio.run(build_fixture(dsn))
    rendered = json.dumps(fixture, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        return (
            0
            if FIXTURE_PATH.exists()
            and FIXTURE_PATH.read_text(encoding="utf-8") == rendered
            else 1
        )
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
