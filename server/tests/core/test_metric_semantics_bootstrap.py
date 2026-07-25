"""Tests for core.metric_semantics_bootstrap (Story 27.2).

Two layers:
  - Offline (no DB): read_manifest_metric_mappings on fixture directories + AD-2 check.
  - Offline idempotence: bootstrap_org_source_mappings with a fake store (patch).
  - Pg-gated: skipped when TEST_POSTGRES_DSN is absent / unreachable.

Pattern follows test_orgs_api.py: pg_available guard, offline with MagicMock/patch,
teardown CASCADE in finally.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Pg guard
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_modules_dir(modules: dict[str, dict | None]) -> Path:
    """Create a temp directory tree with fake modules.

    modules = {
        "connector-a": {"canonical_metric_mapping": {"field": "canonical"}, ...},
        "connector-b": None,   # no manifest.json
    }
    Returns the temp base dir.
    """

    base = Path(tempfile.mkdtemp())
    for name, manifest in modules.items():
        subdir = base / name
        subdir.mkdir()
        if manifest is not None:
            (subdir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
    return base


# ---------------------------------------------------------------------------
# A.1 Offline -- read_manifest_metric_mappings
# ---------------------------------------------------------------------------


def test_read_manifest_basic():
    """One entry per (connector, source_field); connector == dir name."""
    import shutil

    from core.metric_semantics_bootstrap import read_manifest_metric_mappings

    base = _make_modules_dir(
        {
            "alpha": {"canonical_metric_mapping": {"spend": "cost", "clicks": "clicks"}},
            "beta": {"canonical_metric_mapping": {"revenue": "revenue"}},
        }
    )
    try:
        entries = read_manifest_metric_mappings(modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert len(entries) == 3
    connectors = [e["connector"] for e in entries]
    assert "alpha" in connectors
    assert "beta" in connectors
    for e in entries:
        assert "connector" in e
        assert "source_field" in e
        assert "canonical_name" in e


def test_read_manifest_no_manifest_file_skipped():
    """A module directory without manifest.json is silently skipped."""
    import shutil

    from core.metric_semantics_bootstrap import read_manifest_metric_mappings

    base = _make_modules_dir({"no-manifest": None})
    try:
        entries = read_manifest_metric_mappings(modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert entries == []


def test_read_manifest_no_canonical_metric_mapping_skipped():
    """A module with manifest.json but no canonical_metric_mapping is skipped."""
    import shutil

    from core.metric_semantics_bootstrap import read_manifest_metric_mappings

    base = _make_modules_dir({"no-mapping": {"name": "No Mapping Module"}})
    try:
        entries = read_manifest_metric_mappings(modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert entries == []


def test_read_manifest_null_mapping_skipped():
    """canonical_metric_mapping: null -> skipped (not an error)."""
    import shutil

    from core.metric_semantics_bootstrap import read_manifest_metric_mappings

    base = _make_modules_dir({"null-mapping": {"canonical_metric_mapping": None}})
    try:
        entries = read_manifest_metric_mappings(modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert entries == []


def test_read_manifest_non_dict_mapping_skipped():
    """canonical_metric_mapping: non-dict (e.g. list) -> skipped."""
    import shutil

    from core.metric_semantics_bootstrap import read_manifest_metric_mappings

    base = _make_modules_dir({"bad-mapping": {"canonical_metric_mapping": ["x"]}})
    try:
        entries = read_manifest_metric_mappings(modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert entries == []


def test_read_manifest_deterministic_order():
    """Output is sorted: dir name asc, then source_field asc within each dir."""
    import shutil

    from core.metric_semantics_bootstrap import read_manifest_metric_mappings

    base = _make_modules_dir(
        {
            "zeta": {"canonical_metric_mapping": {"zzz": "x", "aaa": "y"}},
            "alpha": {"canonical_metric_mapping": {"bbb": "b", "aaa": "a"}},
        }
    )
    try:
        entries = read_manifest_metric_mappings(modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    connectors = [e["connector"] for e in entries]
    # alpha before zeta (dir name asc)
    assert connectors.index("alpha") < connectors.index("zeta")
    # Within alpha: aaa before bbb
    alpha_entries = [e for e in entries if e["connector"] == "alpha"]
    assert alpha_entries[0]["source_field"] == "aaa"
    assert alpha_entries[1]["source_field"] == "bbb"
    # Within zeta: aaa before zzz
    zeta_entries = [e for e in entries if e["connector"] == "zeta"]
    assert zeta_entries[0]["source_field"] == "aaa"
    assert zeta_entries[1]["source_field"] == "zzz"


def test_read_manifest_real_meta_ads():
    """Smoke test on the real meta-ads manifest: spend->cost entry must be present."""
    from core.metric_semantics_bootstrap import read_manifest_metric_mappings

    entries = read_manifest_metric_mappings()
    meta_entries = [e for e in entries if e["connector"] == "meta-ads"]
    spend_entry = next(
        (e for e in meta_entries if e["source_field"] == "spend"), None
    )
    assert spend_entry is not None, "meta-ads/spend->cost must appear in manifest scan"
    assert spend_entry["canonical_name"] == "cost"


def test_read_manifest_ad2_no_hardcoded_names():
    """AD-2: bootstrap module contains zero hard-coded provider/connector names.

    Grep the source for known provider strings that must NOT appear.
    """
    import importlib.util

    spec = importlib.util.find_spec("core.metric_semantics_bootstrap")
    assert spec is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    # Provider names that must never appear hard-coded in the bootstrap module.
    forbidden = [
        "meta-ads",
        "meta_ads",
        "google-ads",
        "google_ads",
        "shopify",
        "adjust",
        "doubleverify",
        "pinterest",
        "microsoft",
        "amazon",
        "tiktok",
    ]
    for name in forbidden:
        assert name not in source, (
            f"AD-2 violation: provider name {name!r} found in metric_semantics_bootstrap"
        )


# ---------------------------------------------------------------------------
# Offline idempotence (fake store via patch).
# Patch paths: functions are imported lazily inside bootstrap_org_source_mappings
# from core.metric_semantics and core.db, so we patch at the source modules.
# ---------------------------------------------------------------------------


def _make_fake_store(
    platform_defs: dict,  # {canonical_name: "def_id"}
    existing_mappings: dict | None = None,  # {(connector, def_id): {status, ...}}
):
    """Return (upsert_calls, fake_get_definition, fake_select_mapping, fake_upsert)."""
    existing = existing_mappings or {}
    upsert_calls: list[dict] = []

    def fake_get_metric_definition(*, scope_level, canonical_name, **_kwargs):
        if scope_level == "PLATFORM" and canonical_name in platform_defs:
            return {"id": platform_defs[canonical_name], "canonical_name": canonical_name}
        return None

    def fake_select_mapping(
        _conn, *, scope_level, org_id, project_id, metric_definition_id, connector, **_kw
    ):
        return existing.get((connector, metric_definition_id))

    def fake_upsert(*, metric_definition_id, connector, status, created_by, **kwargs):
        upsert_calls.append(
            {
                "metric_definition_id": metric_definition_id,
                "connector": connector,
                "status": status,
                "created_by": created_by,
            }
        )
        return {
            "id": "smm_new",
            "metric_definition_id": metric_definition_id,
            "connector": connector,
            "status": status,
            "scope_level": "ORG",
            "org_id": "org_test",
            "project_id": None,
        }

    return upsert_calls, fake_get_metric_definition, fake_select_mapping, fake_upsert


def _fake_conn_ctx():
    """Return a context-manager-compatible MagicMock for get_connection()."""
    fake_conn = MagicMock()
    fake_conn.__enter__ = lambda s: fake_conn
    fake_conn.__exit__ = MagicMock(return_value=False)
    return fake_conn


def test_bootstrap_first_run_proposes():
    """First bootstrap: each entry with a known PLATFORM def -> upsert proposed."""
    import shutil

    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    base = _make_modules_dir(
        {
            "connector-x": {
                "canonical_metric_mapping": {"field_a": "cost", "field_b": "clicks"}
            },
        }
    )
    upsert_calls, fake_get_def, fake_select, fake_upsert = _make_fake_store(
        platform_defs={"cost": "def_cost", "clicks": "def_clicks"},
    )
    try:
        with (
            patch("core.metric_semantics.get_metric_definition", fake_get_def),
            patch("core.metric_semantics._select_mapping", fake_select),
            patch("core.metric_semantics.upsert_source_metric_mapping", fake_upsert),
            patch("core.db.get_connection", return_value=_fake_conn_ctx()),
        ):
            report = bootstrap_org_source_mappings(org_id="org_test", modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert report["proposed"] == 2
    assert report["unchanged"] == 0
    assert report["preserved"] == 0
    assert report["skipped"] == 0
    assert len(upsert_calls) == 2
    for call in upsert_calls:
        assert call["status"] == "proposed"


def test_bootstrap_unknown_canonical_skipped():
    """An entry whose canonical has no PLATFORM definition -> skipped, no upsert."""
    import shutil

    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    base = _make_modules_dir(
        {"connector-x": {"canonical_metric_mapping": {"unknown_field": "no_such_metric"}}},
    )
    upsert_calls, fake_get_def, fake_select, fake_upsert = _make_fake_store(
        platform_defs={},
    )
    try:
        with (
            patch("core.metric_semantics.get_metric_definition", fake_get_def),
            patch("core.metric_semantics._select_mapping", fake_select),
            patch("core.metric_semantics.upsert_source_metric_mapping", fake_upsert),
            patch("core.db.get_connection", return_value=_fake_conn_ctx()),
        ):
            report = bootstrap_org_source_mappings(org_id="org_test", modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert report["skipped"] == 1
    assert report["proposed"] == 0
    assert len(upsert_calls) == 0


def test_bootstrap_second_run_unchanged():
    """Second bootstrap with same proposed mapping -> unchanged, store still called."""
    import shutil

    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    existing = {
        ("connector-x", "def_cost"): {
            "id": "smm_fake",
            "status": "proposed",
            "metric_definition_id": "def_cost",
            "connector": "connector-x",
            "source_field_path": "field_a",
        }
    }
    base = _make_modules_dir(
        {"connector-x": {"canonical_metric_mapping": {"field_a": "cost"}}},
    )
    upsert_calls, fake_get_def, fake_select, fake_upsert = _make_fake_store(
        platform_defs={"cost": "def_cost"},
        existing_mappings=existing,
    )
    try:
        with (
            patch("core.metric_semantics.get_metric_definition", fake_get_def),
            patch("core.metric_semantics._select_mapping", fake_select),
            patch("core.metric_semantics.upsert_source_metric_mapping", fake_upsert),
            patch("core.db.get_connection", return_value=_fake_conn_ctx()),
        ):
            report = bootstrap_org_source_mappings(org_id="org_test", modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert report["unchanged"] == 1
    assert report["proposed"] == 0
    assert report["preserved"] == 0
    # The store is still called -- it uses _mapping_changed to suppress the audit.
    assert len(upsert_calls) == 1


def test_bootstrap_confirmed_preserved():
    """A mapping with status='confirmed' -> preserved; no upsert emitted."""
    import shutil

    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    existing = {
        ("connector-x", "def_cost"): {
            "id": "smm_fake",
            "status": "confirmed",
            "metric_definition_id": "def_cost",
            "connector": "connector-x",
            "source_field_path": "field_a",
        }
    }
    base = _make_modules_dir(
        {"connector-x": {"canonical_metric_mapping": {"field_a": "cost"}}},
    )
    upsert_calls, fake_get_def, fake_select, fake_upsert = _make_fake_store(
        platform_defs={"cost": "def_cost"},
        existing_mappings=existing,
    )
    try:
        with (
            patch("core.metric_semantics.get_metric_definition", fake_get_def),
            patch("core.metric_semantics._select_mapping", fake_select),
            patch("core.metric_semantics.upsert_source_metric_mapping", fake_upsert),
            patch("core.db.get_connection", return_value=_fake_conn_ctx()),
        ):
            report = bootstrap_org_source_mappings(org_id="org_test", modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert report["preserved"] == 1
    assert report["proposed"] == 0
    assert len(upsert_calls) == 0  # NEVER write over curated


def test_bootstrap_renamed_preserved():
    """A mapping with status='renamed' -> preserved; no upsert emitted."""
    import shutil

    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    existing = {
        ("connector-x", "def_cost"): {
            "id": "smm_fake",
            "status": "renamed",
            "metric_definition_id": "def_cost",
            "connector": "connector-x",
            "source_field_path": "field_a",
        }
    }
    base = _make_modules_dir(
        {"connector-x": {"canonical_metric_mapping": {"field_a": "cost"}}},
    )
    upsert_calls, fake_get_def, fake_select, fake_upsert = _make_fake_store(
        platform_defs={"cost": "def_cost"},
        existing_mappings=existing,
    )
    try:
        with (
            patch("core.metric_semantics.get_metric_definition", fake_get_def),
            patch("core.metric_semantics._select_mapping", fake_select),
            patch("core.metric_semantics.upsert_source_metric_mapping", fake_upsert),
            patch("core.db.get_connection", return_value=_fake_conn_ctx()),
        ):
            report = bootstrap_org_source_mappings(org_id="org_test", modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert report["preserved"] == 1
    assert len(upsert_calls) == 0


def test_bootstrap_rejected_preserved():
    """A mapping with status='rejected' -> preserved; no upsert emitted."""
    import shutil

    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    existing = {
        ("connector-x", "def_cost"): {
            "id": "smm_fake",
            "status": "rejected",
            "metric_definition_id": "def_cost",
            "connector": "connector-x",
            "source_field_path": "field_a",
        }
    }
    base = _make_modules_dir(
        {"connector-x": {"canonical_metric_mapping": {"field_a": "cost"}}},
    )
    upsert_calls, fake_get_def, fake_select, fake_upsert = _make_fake_store(
        platform_defs={"cost": "def_cost"},
        existing_mappings=existing,
    )
    try:
        with (
            patch("core.metric_semantics.get_metric_definition", fake_get_def),
            patch("core.metric_semantics._select_mapping", fake_select),
            patch("core.metric_semantics.upsert_source_metric_mapping", fake_upsert),
            patch("core.db.get_connection", return_value=_fake_conn_ctx()),
        ):
            report = bootstrap_org_source_mappings(org_id="org_test", modules_dir=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert report["preserved"] == 1
    assert len(upsert_calls) == 0


def test_bootstrap_connector_filter():
    """connectors=[...] -> only entries of listed connectors are processed."""
    import shutil

    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    base = _make_modules_dir(
        {
            "connector-a": {"canonical_metric_mapping": {"field": "cost"}},
            "connector-b": {"canonical_metric_mapping": {"field": "clicks"}},
        }
    )
    upsert_calls, fake_get_def, fake_select, fake_upsert = _make_fake_store(
        platform_defs={"cost": "def_cost", "clicks": "def_clicks"},
    )
    try:
        with (
            patch("core.metric_semantics.get_metric_definition", fake_get_def),
            patch("core.metric_semantics._select_mapping", fake_select),
            patch("core.metric_semantics.upsert_source_metric_mapping", fake_upsert),
            patch("core.db.get_connection", return_value=_fake_conn_ctx()),
        ):
            report = bootstrap_org_source_mappings(
                org_id="org_test", connectors=["connector-a"], modules_dir=base
            )
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert report["proposed"] == 1
    assert len(upsert_calls) == 1


def test_bootstrap_identity_propagated():
    """identity parameter is propagated to created_by in upsert calls."""
    import shutil

    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    base = _make_modules_dir(
        {"connector-x": {"canonical_metric_mapping": {"spend": "cost"}}}
    )
    upsert_calls, fake_get_def, fake_select, fake_upsert = _make_fake_store(
        platform_defs={"cost": "def_cost"},
    )
    try:
        with (
            patch("core.metric_semantics.get_metric_definition", fake_get_def),
            patch("core.metric_semantics._select_mapping", fake_select),
            patch("core.metric_semantics.upsert_source_metric_mapping", fake_upsert),
            patch("core.db.get_connection", return_value=_fake_conn_ctx()),
        ):
            bootstrap_org_source_mappings(
                org_id="org_test", identity="curator@example.com", modules_dir=base
            )
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert len(upsert_calls) == 1
    assert upsert_calls[0]["created_by"] == "curator@example.com"


# ---------------------------------------------------------------------------
# Pg-gated tests (require TEST_POSTGRES_DSN + migration 049 applied)
# ---------------------------------------------------------------------------


def _create_test_org(slug: str | None = None) -> str:
    """Insert a minimal org and return its id."""
    from core.db import get_connection
    from ulid import ULID

    org_id = f"org_{ULID()}"
    slug = slug or f"test-{uuid.uuid4().hex[:8]}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_at) "
                "VALUES (%s, %s, %s, 'active', now())",
                (org_id, f"Test org {slug}", slug),
            )
        conn.commit()
    return org_id


def _delete_test_org(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        conn.commit()


@pg_available
def test_pg_bootstrap_proposes_rows():
    """Live DB: bootstrap -> proposed>0; rows in source_metric_mappings with status=proposed."""
    from core.metric_semantics import import_platform_defaults
    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    import_platform_defaults(identity="system")
    org_id = _create_test_org()
    try:
        report = bootstrap_org_source_mappings(org_id=org_id, identity="tester@example.com")
        assert report["proposed"] > 0, "Expected at least one proposed mapping"
        assert isinstance(report["connectors"], list)

        from core.db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.source_metric_mappings "
                    "WHERE scope_level = 'ORG' AND org_id = %s AND status = 'proposed'",
                    (org_id,),
                )
                count = cur.fetchone()[0]
        assert count > 0, "ORG source_metric_mappings must exist after bootstrap"
    finally:
        _delete_test_org(org_id)


@pg_available
def test_pg_bootstrap_second_run_unchanged():
    """Live DB: second bootstrap -> proposed=0, unchanged>0, no new audit rows."""
    from core.db import get_connection
    from core.metric_semantics import import_platform_defaults
    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    import_platform_defaults(identity="system")
    org_id = _create_test_org()
    try:
        bootstrap_org_source_mappings(org_id=org_id, identity="system")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.metric_semantics_audit "
                    "WHERE entity_type = 'source_metric_mapping'",
                )
                audit_before = cur.fetchone()[0]

        report2 = bootstrap_org_source_mappings(org_id=org_id, identity="system")
        assert report2["proposed"] == 0
        assert report2["unchanged"] > 0

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.metric_semantics_audit "
                    "WHERE entity_type = 'source_metric_mapping'",
                )
                audit_after = cur.fetchone()[0]

        assert audit_after == audit_before, "Re-bootstrap must not emit new audit rows"
    finally:
        _delete_test_org(org_id)
