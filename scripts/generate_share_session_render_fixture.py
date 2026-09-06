"""Generate the server-owned frozen Share session fixture (Story 65.4)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from tests.fixture_generators.share_session_render import normalize_envelope

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    ROOT
    / "ui/cards/shell/src/viz/__tests__/fixtures/shareSessionRender.json"
)
def build_fixture(dsn: str) -> dict[str, Any]:
    """Traverse the real Share writer, exchange route and frozen replay route."""
    os.environ.setdefault("TOOROW_RENDER_SHARE_PEPPER", "render-share-test-pepper-0123456789")
    os.environ.setdefault("TOOROW_RENDER_SHARE_ORIGIN", "https://share.example.com")
    previous_platform_db = os.environ.get("PLATFORM_DB_URL")
    os.environ["PLATFORM_DB_URL"] = dsn

    import psycopg  # noqa: PLC0415
    from core.render_shares_api import render_share_routes  # noqa: PLC0415
    from starlette.applications import Starlette  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415
    from tests.integration.test_render_shares_postgres import Chain  # noqa: PLC0415

    conn = psycopg.connect(dsn)
    try:
        chain = Chain(conn, with_ai_path=True).build()
        created = chain.share()
        conn.commit()
        bearer = created.delivery_url.split("#render=")[1]
        http = TestClient(
            Starlette(routes=render_share_routes),
            base_url="https://testserver",
            raise_server_exceptions=False,
        )
        exchange = http.post("/api/render-shares/exchange", json={"bearer": bearer})
        if exchange.status_code != 200:
            raise RuntimeError(f"Share exchange failed: {exchange.status_code} {exchange.text}")
        response = http.get("/api/render-shares/session/render")
        if response.status_code != 200:
            raise RuntimeError(f"Share replay failed: {response.status_code} {response.text}")
        body = json.loads(response.content)
        if "ai_path_evidence" not in body:
            raise RuntimeError("Share replay omitted ai_path_evidence")
        return normalize_envelope(body, chain)
    finally:
        conn.close()
        if previous_platform_db is None:
            os.environ.pop("PLATFORM_DB_URL", None)
        else:
            os.environ["PLATFORM_DB_URL"] = previous_platform_db


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        parser.error("TEST_POSTGRES_DSN is required; use a disposable migrated PostgreSQL")
    rendered = json.dumps(build_fixture(dsn), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        return 0 if FIXTURE_PATH.exists() and FIXTURE_PATH.read_text("utf-8") == rendered else 1
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
