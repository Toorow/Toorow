"""Fail-closed connector-logo sync contract tests.

Guards the managed shared logo system: the canonical source is
``web/src/data/connector-identities.json`` + ``web/public/connectors/``; the
admin front is a DERIVED mirror produced by ``scripts/sync_connector_logos.py``.
These tests prove the mirror is byte-current, the resolver map handles both the
module id and the file stem, and the sync fails closed on drift.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
SCRIPT_PATH = ROOT / "scripts" / "sync_connector_logos.py"
ADMIN_PUBLIC_DIR = ROOT / "ui" / "admin" / "public" / "connectors"


def _load():
    spec = importlib.util.spec_from_file_location("sync_connector_logos", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_canonical_plan_is_complete_and_checksum_verified():
    # _canonical_plan raises on any missing file or sha256 drift, so a clean
    # return already proves every official asset matches its registry checksum.
    sync = _load()
    files, resolver = sync._canonical_plan()
    assert "generic.svg" in files
    # 37 modules; some share a brand mark (meta-ads -> meta.svg) so file count <= 37.
    assert 30 <= len(files) <= 37
    # Aliased ids resolve via BOTH id and stem to the same asset.
    assert resolver["meta-ads"] == resolver["meta"] == "/connectors/meta.svg"
    assert resolver["gsc"] == resolver["google-search-console"]


def test_plan_is_deterministic():
    sync = _load()
    first_files, first_map = sync._canonical_plan()
    second_files, second_map = sync._canonical_plan()
    assert first_files == second_files
    assert sync._serialize_map(first_map) == sync._serialize_map(second_map)


def test_committed_admin_mirror_is_in_sync():
    sync = _load()
    files, resolver = sync._canonical_plan()
    assert sync._drift(files, resolver) == []


def test_check_cli_passes_on_committed_state():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_drift_is_detected_when_admin_asset_is_removed():
    sync = _load()
    files, resolver = sync._canonical_plan()
    victim = ADMIN_PUBLIC_DIR / "generic.svg"
    backup = victim.read_bytes()
    victim.unlink()
    try:
        problems = sync._drift(files, resolver)
        assert any("generic.svg" in line for line in problems)
    finally:
        victim.write_bytes(backup)
    # Restored -> clean again.
    assert sync._drift(files, resolver) == []


def test_write_then_check_is_idempotent():
    env = {**os.environ, "UV_CACHE_DIR": str(ROOT / ".uv-cache")}
    write = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--write"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert write.returncode == 0, write.stderr
    check = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
