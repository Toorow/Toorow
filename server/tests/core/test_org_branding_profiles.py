"""Tests for Story 21.2 -- org branding + global user profiles (AC2, AC3, AC5).

Offline: hex-colour and length validation (handlers reject before touching the DB).
Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): branding persists on the
real app.organizations columns (migration 036) and a profile is per-identity on
app.user_profiles.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_AUTH = ("core.admin_api._check_auth", (True, "tester@example.com"))


def _body_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.path_params = {}
    return req


def _org_patch_request(org_id: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _get_org_request(org_id: str) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    return req


def _noargs_request() -> MagicMock:
    req = MagicMock()
    req.path_params = {}
    return req


def _drop_org_by_slug(slug: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.organizations WHERE slug = %s", (slug,))
        conn.commit()


def _drop_profile(identity: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.user_profiles WHERE identity = %s", (identity,))
        conn.commit()


# ---------------------------------------------------------------------------
# Offline validation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_org_invalid_hex_422():
    from core.admin_api import _create_org

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_org(
            _body_request({"name": "Acme", "brand_primary": "#ZZZ"})
        )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_patch_org_invalid_hex_422():
    from core.admin_api import _patch_org

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _patch_org(
            _org_patch_request("org_x", {"brand_accent": "123456"})  # missing '#'
        )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_patch_my_profile_display_name_too_long_422():
    from core.admin_api import _patch_my_profile

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _patch_my_profile(_body_request({"display_name": "x" * 256}))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_get_my_profile_requires_auth():
    from core.admin_api import _get_my_profile

    with patch(_AUTH[0], return_value=(False, "")):
        resp = await _get_my_profile(_noargs_request())
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Live Postgres
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_create_org_with_branding_persists():
    from core.admin_api import _create_org, _get_org

    slug = f"brand-{uuid.uuid4().hex[:8]}"
    _drop_org_by_slug(slug)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_org(
                _body_request(
                    {
                        "name": "Brandy",
                        "slug": slug,
                        "brand_primary": "#1A2B3C",
                        "brand_secondary": "#FFFFFF",
                        "brand_accent": "#00ff88",
                        "logo_url": "https://cdn.example/logo.png",
                    }
                )
            )
            assert resp.status_code == 201
            org = json.loads(resp.body)
            assert org["brand_primary"] == "#1A2B3C"
            assert org["logo_url"] == "https://cdn.example/logo.png"

            got = json.loads((await _get_org(_get_org_request(org["id"]))).body)
            assert got["brand_secondary"] == "#FFFFFF"
            assert got["brand_accent"] == "#00ff88"
    finally:
        _drop_org_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_patch_org_branding_persists():
    from core.admin_api import _create_org, _patch_org

    slug = f"brandp-{uuid.uuid4().hex[:8]}"
    _drop_org_by_slug(slug)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_org(_body_request({"name": "P", "slug": slug}))
            oid = json.loads(r.body)["id"]
            resp = await _patch_org(
                _org_patch_request(oid, {"brand_primary": "#abcdef"})
            )
        assert resp.status_code == 200
        assert json.loads(resp.body)["brand_primary"] == "#abcdef"
    finally:
        _drop_org_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_profile_upsert_and_get_defaults_source_self():
    from core.admin_api import _get_my_profile, _patch_my_profile

    ident = f"user-{uuid.uuid4().hex[:8]}@e.com"
    _drop_profile(ident)
    try:
        with patch(_AUTH[0], return_value=(True, ident)):
            p = await _patch_my_profile(
                _body_request(
                    {"display_name": "Carole", "avatar_url": "https://img/av.png"}
                )
            )
            assert p.status_code == 200
            body = json.loads(p.body)
            assert body["identity"] == ident
            assert body["display_name"] == "Carole"
            assert body["avatar_url"] == "https://img/av.png"
            assert body["avatar_source"] == "self"  # defaulted

            got = json.loads((await _get_my_profile(_noargs_request())).body)
            assert got["display_name"] == "Carole"
    finally:
        _drop_profile(ident)


@pg_available
@pytest.mark.anyio
async def test_profile_is_per_identity():
    from core.admin_api import _get_my_profile, _patch_my_profile

    id_a = f"a-{uuid.uuid4().hex[:8]}@e.com"
    id_b = f"b-{uuid.uuid4().hex[:8]}@e.com"
    _drop_profile(id_a)
    _drop_profile(id_b)
    try:
        with patch(_AUTH[0], return_value=(True, id_a)):
            await _patch_my_profile(_body_request({"display_name": "Alice"}))
        with patch(_AUTH[0], return_value=(True, id_b)):
            await _patch_my_profile(_body_request({"display_name": "Bob"}))
            got_b = json.loads((await _get_my_profile(_noargs_request())).body)
        with patch(_AUTH[0], return_value=(True, id_a)):
            got_a = json.loads((await _get_my_profile(_noargs_request())).body)
        assert got_a["display_name"] == "Alice"
        assert got_b["display_name"] == "Bob"
    finally:
        _drop_profile(id_a)
        _drop_profile(id_b)


@pg_available
def test_org_branding_hex_check_rejects_invalid_at_db():
    """AC1: the DB CHECK on brand_primary rejects a non-hex value (defence in depth)."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    with get_connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.organizations "
                    "(id, name, slug, created_by, brand_primary) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (f"org_hc_{suffix}", "HC", f"hc-{suffix}", "system", "not-a-hex"),
                )
        conn.rollback()
