"""Story 24.4 -- offline tests: per-org dbt orchestration + cross-project fan-out.

Offline only (no dbt, no Postgres, no DuckDB): everything is mocked. Covers
  * warehouse_tenancy.list_active_orgs -- active-org listing + degradation contract,
  * scheduler.run_dbt_per_org -- one dbt build per active org, isolation, guard,
  * anomaly_alerts._mart_prefixes_for_scan -- flag OFF bit-identical (resolver
    NOT called) vs flag ON fan-out over active orgs (AC5, CONDITION DE FLIP),
  * cache_warehouse._mart_schema_prefixes -- same flag OFF/ON contract.

The dbt-local equivalence proof (AC4) and the seed loop (AC8) live in
server/tests/integration/test_seed_to_mart_loop.py (skipped without dbt).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from core import anomaly_alerts, cache_warehouse, scheduler
from core import warehouse_tenancy as wt


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    monkeypatch.delenv("DBT_NIGHTLY_ENABLED", raising=False)
    wt._reset_cache()
    yield
    wt._reset_cache()


def _org(org_id: str, slug: str) -> wt.OrgSchemas:
    built = wt._build(org_id, slug)
    assert built is not None
    return built


def _connection_patch(rows):
    """Patch core.db.get_connection to yield a cursor whose fetchall returns *rows*."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return patch("core.db.get_connection", return_value=cm)


# ---------------------------------------------------------------------------
# list_active_orgs (AC3 source-of-truth)
# ---------------------------------------------------------------------------


def test_list_active_orgs_resolves_each_active_org():
    with _connection_patch([("org_1", "acme-media"), ("org_2", "beta_co")]):
        orgs = wt.list_active_orgs()
    assert [o.warehouse_slug for o in orgs] == ["acme_media", "beta_co"]
    assert [o.marts for o in orgs] == ["org_acme_media_marts", "org_beta_co_marts"]
    assert [o.raw for o in orgs] == ["org_acme_media_raw", "org_beta_co_raw"]


def test_list_active_orgs_skips_unsanitisable_slug():
    # 'a b' has a space -> not [A-Za-z0-9_] after '-'->'_' -> skipped, never crashes.
    with _connection_patch([("org_ok", "acme"), ("org_bad", "a b")]):
        orgs = wt.list_active_orgs()
    assert [o.warehouse_slug for o in orgs] == ["acme"]


def test_list_active_orgs_degrades_to_empty_on_db_error():
    with patch("core.db.get_connection", side_effect=RuntimeError("pg down")):
        orgs = wt.list_active_orgs()
    assert orgs == []  # never raises -- nightly loop degrades to "no fan-out"


# ---------------------------------------------------------------------------
# scheduler.run_dbt_per_org (AC3)
# ---------------------------------------------------------------------------


def test_run_dbt_per_org_disabled_by_default():
    res = scheduler.run_dbt_per_org()
    assert res["status"] == "disabled"
    assert res["orgs"] == 0


def test_run_dbt_per_org_runs_once_per_org_with_bare_slug(monkeypatch):
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")
    calls: list[list[str]] = []

    def _runner(cmd, **_kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="OK ok\n", stderr="")

    orgs = [_org("org_1", "acme"), _org("org_2", "beta")]
    res = scheduler.run_dbt_per_org(_runner=_runner, _list_orgs=lambda: orgs)

    assert res["status"] == "ok"
    assert res["orgs"] == 2 and res["ok"] == 2 and res["failed"] == 0
    # dbt invoked exactly twice, each with build + the BARE warehouse_slug in --vars.
    assert len(calls) == 2
    assert calls[0] == ["dbt", "build", "--vars", '{"org": "acme"}']
    assert calls[1] == ["dbt", "build", "--vars", '{"org": "beta"}']


def test_run_dbt_per_org_isolates_one_org_failure(monkeypatch):
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")

    def _runner(cmd, **_kw):
        # 'acme' fails (exit 1), 'beta' succeeds -- one failure must not abort the other.
        rc = 1 if '"org": "acme"' in cmd[-1] else 0
        return MagicMock(returncode=rc, stdout="", stderr="")

    orgs = [_org("org_1", "acme"), _org("org_2", "beta")]
    with patch("core.scheduler._insert_meta_alert") as meta:
        res = scheduler.run_dbt_per_org(_runner=_runner, _list_orgs=lambda: orgs)

    assert res["orgs"] == 2 and res["ok"] == 1 and res["failed"] == 1
    statuses = {r["warehouse_slug"]: r["status"] for r in res["results"]}
    assert statuses == {"acme": "failed", "beta": "ok"}
    meta.assert_called()  # a meta-alert was raised for the failing org


def test_run_dbt_per_org_skipped_when_no_active_org(monkeypatch):
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")
    res = scheduler.run_dbt_per_org(_list_orgs=lambda: [])
    assert res["status"] == "skipped" and res["orgs"] == 0


# ---------------------------------------------------------------------------
# anomaly_alerts fan-out helper (AC5, CONDITION DE FLIP)
# ---------------------------------------------------------------------------


def test_anomaly_prefixes_flag_off_is_single_legacy_prefix(monkeypatch):
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    with patch("core.warehouse_tenancy.list_active_orgs") as lst:
        prefixes = anomaly_alerts._mart_prefixes_for_scan(None)
    assert prefixes == [wt.LEGACY_DUCKDB_MART_PREFIX]  # 'main_marts.'
    lst.assert_not_called()  # OFF path must NOT list active orgs (bit-identical)


def test_anomaly_prefixes_flag_on_fans_out_per_active_org(monkeypatch):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    orgs = [_org("org_1", "acme"), _org("org_2", "beta")]
    with patch("core.warehouse_tenancy.list_active_orgs", return_value=orgs):
        prefixes = anomaly_alerts._mart_prefixes_for_scan(None)
    assert prefixes == ["org_acme_marts.", "org_beta_marts."]


def test_anomaly_prefixes_flag_on_scoped_project_stays_single(monkeypatch):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    # A pinned project_id never fans out -- it resolves ONE prefix via mart_prefix.
    with patch("core.warehouse_tenancy.mart_prefix", return_value="org_x_marts.") as mp:
        with patch("core.warehouse_tenancy.list_active_orgs") as lst:
            prefixes = anomaly_alerts._mart_prefixes_for_scan("proj_1")
    assert prefixes == ["org_x_marts."]
    mp.assert_called_once_with("proj_1")
    lst.assert_not_called()


# ---------------------------------------------------------------------------
# cache_warehouse fan-out helper (AC5)
# ---------------------------------------------------------------------------


def test_cache_prefixes_flag_off_is_single_legacy_prefix(monkeypatch):
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    with patch("core.warehouse_tenancy.list_active_orgs") as lst:
        prefixes = cache_warehouse._mart_schema_prefixes()
    assert prefixes == [wt.LEGACY_DUCKDB_MART_PREFIX]
    lst.assert_not_called()


def test_cache_prefixes_flag_on_fans_out_per_active_org(monkeypatch):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    orgs = [_org("org_1", "acme"), _org("org_2", "beta")]
    with patch("core.warehouse_tenancy.list_active_orgs", return_value=orgs):
        prefixes = cache_warehouse._mart_schema_prefixes()
    assert prefixes == ["org_acme_marts.", "org_beta_marts."]
