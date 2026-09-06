"""Generate the server-owned one-path, three-surface parity fixture."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from tests.fixture_generators.observed_ai_path_parity import build_fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    ROOT
    / "ui/cards/shell/src/viz/__tests__/fixtures/observedAiPathParity.json"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        parser.error("TEST_POSTGRES_DSN is required; use a disposable migrated PostgreSQL")
    with tempfile.TemporaryDirectory(prefix="toorow-ai-path-parity-") as runtime_dir:
        fixture = asyncio.run(build_fixture(dsn, Path(runtime_dir)))
    rendered = json.dumps(fixture, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        return 0 if FIXTURE_PATH.exists() and FIXTURE_PATH.read_text("utf-8") == rendered else 1
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
