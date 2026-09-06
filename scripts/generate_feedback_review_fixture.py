"""Generate the server-owned Story 65.8 feedback-review admin fixture."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
from pathlib import Path

from tests.fixture_generators.feedback_review import build_fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "ui/admin/src/__tests__/fixtures/feedbackReviewExact.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        parser.error("TEST_POSTGRES_DSN is required; use a disposable migrated PostgreSQL")
    rendered = json.dumps(asyncio.run(build_fixture(dsn)), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        existing = FIXTURE_PATH.read_text("utf-8") if FIXTURE_PATH.exists() else ""
        if existing == rendered:
            return 0
        print(
            "".join(
                list(
                    difflib.unified_diff(
                        existing.splitlines(keepends=True),
                        rendered.splitlines(keepends=True),
                        fromfile=str(FIXTURE_PATH),
                        tofile="generated",
                    )
                )[:80]
            ),
            end="",
        )
        return 1
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
