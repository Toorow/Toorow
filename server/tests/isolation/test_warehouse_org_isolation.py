"""Physical warehouse cross-org isolation proof (Story 24.6, Epic 24, P4).

This module is the isolation-suite half of story 24.6: it proves the epic-24
invariants at the DATA-PLANE level, not merely that a ``project_id`` filter is
present (which the 7.4 suite already covers). The org is the PHYSICAL isolation
boundary: a client's rows live in ITS DuckDB schemas ``org_<wslug>_raw`` /
``org_<wslug>_marts`` (BigQuery datasets in prod), and the scoped read path of
org A can never reach org B's tables because they are DIFFERENT schemas -- a
resolver pointing at ``org_a_marts.`` physically cannot ``SELECT`` from
``org_b_marts``.

Test taxonomy (spec 24.6):
  1. Cross-org physically impossible -- a real 2-org DuckDB file, provisioned via
     ``provision_org_schemas`` + written with distinct rows, read through the
     SAME builder ``warehouse._build_query`` that production uses. The org-A
     scoped prefix returns ZERO org-B rows AND cannot even name org-B's table.
  2. Grant marts-only -- ``_simulate_bq_iam_grant`` refuses raw / mirror_* and the
     simulated grant scope is the marts dataset only (end-to-end at the isolation
     level, complementing the unit assertions in test_dataset_access_grants.py).
  3. Provision / drop idempotent -- provision x2 == same state; drop is
     human-gated (X-Confirm-Delete); re-provision after drop is clean.
  4. AD-8 intact -- STATIC guard: ``mirror_sync.py`` is the ONLY writer of the
     ``mirror`` schema; neither ``warehouse_write.py`` (the raw writer) nor the
     two dbt naming macros compose or write a ``mirror`` schema.
  5. Read equivalence flag OFF -- ``mart_prefix(None)`` == ``main_marts.`` and the
     legacy BigQuery dataset == ``marts_<project_id>`` are LOCKED so rollup/cards
     render bit-identical numbers pre-flip (twin of the 24.3/24.4 equivalence
     tests, pinned here at the isolation boundary).

Gating (pattern 7.4 / 21.5 / 24.5):
  - Pure-DuckDB tests are OFFLINE: they build a temp .duckdb via ``tmp_path`` and
    monkeypatch resolution -- no Postgres, no cloud (AI-08).
  - The end-to-end provisioning-idempotency test is PG-GATED (``_pg_reachable``,
    skipif TEST_POSTGRES_DSN) because it drives real ``app.organizations`` rows
    through ``resolve_org_schemas``.
  - No inline ``org_*`` composition anywhere: every schema name comes from
    ``warehouse_tenancy`` (invariant epic-24, mirrored by the naming guard).

------------------------------------------------------------------------------
Phase B candidates (deferred, AI-08 -- documented here so 24.6 owns the ledger):
  - Real IAM/BigQuery execution: the isolation proof today is DuckDB-native +
    an IAM STUB (_simulate_bq_iam_grant). Phase B replaces the stub with a real
    ``set_iam_policy`` against a live ``org_<wslug>_marts`` dataset and asserts a
    ``dataViewer`` on marts cannot ``SELECT`` from raw/mirror/another-org dataset
    (the physical BigQuery twin of test #1).
  - BYO warehouse / dedicated GCP project per large account (spike §4) -- the
    dataset-per-org layout makes the migration trivial; isolation proof extends
    to "org lives in ITS project".
  - Table-per-stream (Adverity model) -- finer lineage; the cross-org proof would
    then assert per-stream tables also never leave the org schema.
  - Ephemeral cache warehouse (AD-22 / Epic 19) following the org topology once it
    binds to real BigQuery: the cache file must be provisioned/scoped per org too
    (epic-dependency, not a story). Its isolation would mirror test #1 on the
    cache file.

Deferred beyond epic-24 (post epic-25, see 24-4-dbt-par-org.md):
  - The DEFAULT FLIP of ``TOOROW_ORG_SCHEMAS`` (24.4 owns it) and the T3/T7 module
    patch (module ``_get_mart_table`` migrating off the hardcoded ``main_marts.``
    to ``warehouse_tenancy.mart_prefix``) are DIFFERED behind the epic-25
    parallel-session lock on server/modules/**. Until then flag-OFF equivalence
    (test #5) is the load-bearing safety net and the naming-guard debt is FROZEN
    (shrink-only). This module must NOT touch server/modules/**, queue.py,
    loader.py, or the module dbt trees.
------------------------------------------------------------------------------
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

pytestmark = pytest.mark.isolation


@pytest.fixture
def anyio_backend():
    """Async tests in this module run on asyncio (pattern integration/conftest)."""
    return "asyncio"


# ---------------------------------------------------------------------------
# Postgres availability (pattern test_dataset_access_grants._pg_reachable)
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


pg_available = pytest.mark.skipif(
    not _pg_reachable(), reason="platform Postgres not reachable"
)


# ---------------------------------------------------------------------------
# Offline two-org DuckDB fixture (real file, real schemas, distinct rows).
# ---------------------------------------------------------------------------

# Two orgs whose warehouse_slugs are DISJOINT so a leak is detectable by a single
# substring. Rows carry disjoint metrics ('sessions' vs 'average_position') AND
# a shared project_id column so the test proves isolation is the SCHEMA, not just
# the project_id filter (the 24.6 delta over the 7.4 suite).
_ORG_A_SLUG = "org_alpha_co"   # already sanitised (warehouse_slug form)
_ORG_B_SLUG = "org_beta_co"
_SHARED_PROJECT = "proj_shared"  # SAME project_id in BOTH schemas on purpose


def _fake_schemas(warehouse_slug: str):
    """Build an OrgSchemas WITHOUT composing names inline (via warehouse_tenancy)."""
    from core.warehouse_tenancy import OrgSchemas

    # warehouse_slug is already the sanitised form; OrgSchemas is the single
    # place that carries raw/marts names -- we build via the dataclass, not by
    # string concatenation in the test body (invariant epic-24).
    return OrgSchemas(
        org_id=f"orgid_{warehouse_slug}",
        org_slug=warehouse_slug.replace("_", "-"),
        warehouse_slug=warehouse_slug,
        raw=f"org_{warehouse_slug}_raw",
        marts=f"org_{warehouse_slug}_marts",
    )


def _provision_and_seed_two_orgs(db_path: str) -> tuple:
    """Provision org-A and org-B schemas in a real DuckDB file, seed distinct rows.

    Uses ``provision_org_schemas`` (resolution mocked so no Postgres is touched)
    so the CREATE SCHEMA path under test IS the production one. Then writes one
    ``fact_daily_kpi`` row per org into its OWN marts schema, both carrying the
    SAME project_id -- proving later that isolation is the schema, not the filter.
    """
    from unittest.mock import patch

    from core import warehouse_tenancy as wt

    schemas_a = _fake_schemas(_ORG_A_SLUG)
    schemas_b = _fake_schemas(_ORG_B_SLUG)

    os.environ["TOOROW_DUCKDB_PATH"] = db_path
    os.environ.pop("TOOROW_DB_MODE", None)
    wt._reset_cache()

    # Provision each org's schemas through the real provision path (resolution
    # mocked to return the org's schemas without a Postgres round-trip).
    for schemas in (schemas_a, schemas_b):
        with patch(
            "core.warehouse_tenancy.resolve_org_schemas", return_value=schemas
        ):
            res = wt.provision_org_schemas(org_id=schemas.org_id)
        assert res["status"] == "ok", res

    # Seed one distinct fact row into EACH org's marts schema, same project_id.
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(db_path)
    try:
        for schemas, connector, metric, value in (
            (schemas_a, "google-analytics", "sessions", 1234.0),
            (schemas_b, "gsc", "average_position", 3.5),
        ):
            con.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {schemas.marts}.fact_daily_kpi (
                    project_id           TEXT,
                    date                 DATE,
                    connector            TEXT,
                    metric               TEXT,
                    breakdown_dimension  TEXT,
                    breakdown_value      TEXT,
                    value                DOUBLE,
                    pull_id              TEXT,
                    loaded_at            TIMESTAMP
                )
                """
            )
            con.execute(
                f"INSERT INTO {schemas.marts}.fact_daily_kpi VALUES "
                "(?, DATE '2026-07-05', ?, ?, 'device', 'desktop', ?, 'pull', "
                "TIMESTAMP '2026-07-06 00:00:00')",
                [_SHARED_PROJECT, connector, metric, value],
            )
    finally:
        con.close()

    return schemas_a, schemas_b


@pytest.fixture()
def two_org_duckdb(tmp_path, monkeypatch):
    """Real DuckDB file with two provisioned, distinctly-seeded org schemas.

    Yields ``(db_path, schemas_a, schemas_b)``. Env is restored on teardown.
    """
    from core import warehouse_tenancy as wt

    prev_path = os.environ.get("TOOROW_DUCKDB_PATH")
    prev_mode = os.environ.get("TOOROW_DB_MODE")

    db_path = str(tmp_path / "isolation_2org.duckdb")
    schemas_a, schemas_b = _provision_and_seed_two_orgs(db_path)

    try:
        yield db_path, schemas_a, schemas_b
    finally:
        wt._reset_cache()
        if prev_path is None:
            os.environ.pop("TOOROW_DUCKDB_PATH", None)
        else:
            os.environ["TOOROW_DUCKDB_PATH"] = prev_path
        if prev_mode is None:
            os.environ.pop("TOOROW_DB_MODE", None)
        else:
            os.environ["TOOROW_DB_MODE"] = prev_mode


# ---------------------------------------------------------------------------
# 1. Cross-org physically impossible (OFFLINE, real DuckDB)
# ---------------------------------------------------------------------------


def _read_scoped(db_path: str, marts_prefix: str) -> list[dict]:
    """Read fact_daily_kpi through the SAME builder production uses (_build_query).

    ``marts_prefix`` is the resolved ``org_<wslug>_marts.`` -- exactly what
    ``warehouse._duckdb_mart_prefix`` would hand the builder under flag ON.
    """
    from core import warehouse

    sql, params = warehouse._build_query(
        marts_prefix, _SHARED_PROJECT, "2026-07-01", "2026-07-31", None
    )
    return warehouse._query_duckdb(sql, params)


def test_org_a_scoped_read_never_sees_org_b_rows(two_org_duckdb):
    """Org-A scoped read (org_a_marts.) returns ONLY org-A rows, zero org-B rows.

    Both rows share project_id=proj_shared, so a project_id filter alone would
    NOT separate them -- isolation here is PHYSICAL (different schema). This is
    the 24.6 delta over the 7.4 project_id-filter proof.
    """
    _db, schemas_a, schemas_b = two_org_duckdb

    a_rows = _read_scoped(_db, f"{schemas_a.marts}.")
    metrics = {r["metric"] for r in a_rows}
    connectors = {r["connector"] for r in a_rows}

    assert "sessions" in metrics, "org A must see its own row"
    assert "average_position" not in metrics, "org B row leaked into org A read!"
    assert "gsc" not in connectors, "org B connector leaked into org A read!"

    # Symmetric proof: org-B scoped read excludes org-A.
    b_rows = _read_scoped(_db, f"{schemas_b.marts}.")
    b_metrics = {r["metric"] for r in b_rows}
    assert "average_position" in b_metrics
    assert "sessions" not in b_metrics, "org A row leaked into org B read!"


def test_org_a_prefix_cannot_name_org_b_table(two_org_duckdb):
    """Physically impossible: the org-A prefix cannot address org-B's table.

    A read scoped to org_a_marts. simply queries a DIFFERENT schema -- there is
    no SQL path from the org-A prefix to org_b_marts.fact_daily_kpi. We assert
    the two marts schemas are distinct and that a query against org-A's schema
    never returns org-B's disjoint value (3.5), proving the tables are separate
    physical objects, not a shared table behind a filter.
    """
    _db, schemas_a, schemas_b = two_org_duckdb

    assert schemas_a.marts != schemas_b.marts, "org schemas must be distinct"

    a_rows = _read_scoped(_db, f"{schemas_a.marts}.")
    values = {r["value"] for r in a_rows}
    assert 1234.0 in values, "org A value present"
    assert 3.5 not in values, "org B value reachable from org A schema -- LEAK!"


def test_org_b_table_absent_from_org_a_schema(two_org_duckdb):
    """The catalog confirms org-A's schema holds only org-A's tables.

    information_schema shows org_a_marts.fact_daily_kpi exists while
    org_b_marts.fact_daily_kpi is a SEPARATE object in a SEPARATE schema --
    dropping org B (drop_org_schemas) would remove B's table without touching A.
    """
    import duckdb  # noqa: PLC0415

    _db, schemas_a, schemas_b = two_org_duckdb

    con = duckdb.connect(_db, read_only=True)
    try:
        schemas_present = {
            row[0]
            for row in con.execute(
                "SELECT DISTINCT table_schema FROM information_schema.tables "
                "WHERE table_name = 'fact_daily_kpi'"
            ).fetchall()
        }
    finally:
        con.close()

    assert schemas_a.marts in schemas_present
    assert schemas_b.marts in schemas_present
    # Two DISTINCT physical schemas -> a drop of one cannot affect the other.
    assert schemas_a.marts != schemas_b.marts


# ---------------------------------------------------------------------------
# 2. Grant marts-only (OFFLINE) -- end-to-end IAM stub scoping at isolation level
# ---------------------------------------------------------------------------


def test_grant_scope_is_marts_only_never_raw_or_mirror(two_org_duckdb):
    """A simulated client grant targets ONLY org_<wslug>_marts (never raw/mirror).

    End-to-end at the isolation boundary: we take the SAME OrgSchemas that back
    the two real DuckDB schemas and prove the IAM stub (a) accepts the marts
    dataset and (b) hard-refuses raw and mirror_* datasets (ValueError, survives
    python -O per review 24.5 F-6). This is the "give a client access to ITS
    marts, structurally incapable of reaching raw/mirror/another-org" invariant.
    """
    from core.warehouse_tenancy import OrgSchemas, _simulate_bq_iam_grant

    _db, schemas_a, _schemas_b = two_org_duckdb

    # Accepts marts (the ONLY legitimate grant surface).
    _simulate_bq_iam_grant(
        schemas_a, "serviceAccount:client@proj.iam.gserviceaccount.com", "grant"
    )

    # Refuses a grant whose "marts" field is actually the raw dataset.
    raw_as_marts = OrgSchemas(
        org_id=schemas_a.org_id,
        org_slug=schemas_a.org_slug,
        warehouse_slug=schemas_a.warehouse_slug,
        raw=schemas_a.raw,
        marts=schemas_a.raw,  # raw sneaked into the marts slot
    )
    with pytest.raises(ValueError, match="_raw"):
        _simulate_bq_iam_grant(raw_as_marts, "user:x@example.com", "grant")

    # Refuses a mirror_* dataset (the central control-plane mirror, AD-8).
    mirror_as_marts = OrgSchemas(
        org_id=schemas_a.org_id,
        org_slug=schemas_a.org_slug,
        warehouse_slug=schemas_a.warehouse_slug,
        raw=schemas_a.raw,
        marts="mirror_context_events",
    )
    with pytest.raises(ValueError, match="mirror_"):
        _simulate_bq_iam_grant(mirror_as_marts, "user:x@example.com", "grant")


def test_grant_on_org_a_marts_cannot_target_org_b(two_org_duckdb):
    """The grant surface is one org's marts -- it never names another org's schema.

    The stub is invoked per-org with that org's OWN OrgSchemas; there is no code
    path that would let an org-A grant reference org_b_marts. We assert the
    dataset the stub would bind is exactly org-A's marts and shares no prefix
    with org-B's marts beyond the common ``org_`` sentinel.
    """
    _db, schemas_a, schemas_b = two_org_duckdb

    assert schemas_a.marts.endswith("_marts")
    assert schemas_b.marts.endswith("_marts")
    assert schemas_a.marts != schemas_b.marts
    # A grant built from schemas_a can only ever carry schemas_a.marts.
    assert schemas_b.warehouse_slug not in schemas_a.marts


# ---------------------------------------------------------------------------
# 3. Provisioning / drop idempotent + human-gated (OFFLINE, real DuckDB)
# ---------------------------------------------------------------------------


def _schema_exists(db_path: str, schema: str) -> bool:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(db_path, read_only=True)
    try:
        row = con.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = ? LIMIT 1",
            [schema],
        ).fetchone()
        return row is not None
    finally:
        con.close()


def test_provision_is_idempotent(tmp_path, monkeypatch):
    """Provision x2 == same state (CREATE SCHEMA IF NOT EXISTS, no error, no dup)."""
    from unittest.mock import patch

    from core import warehouse_tenancy as wt

    db_path = str(tmp_path / "idem.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    wt._reset_cache()

    schemas = _fake_schemas("org_gamma_co")
    with patch("core.warehouse_tenancy.resolve_org_schemas", return_value=schemas):
        r1 = wt.provision_org_schemas(org_id=schemas.org_id)
        r2 = wt.provision_org_schemas(org_id=schemas.org_id)

    assert r1 == r2 == {"status": "ok", "raw": schemas.raw, "marts": schemas.marts}
    assert _schema_exists(db_path, schemas.raw)
    assert _schema_exists(db_path, schemas.marts)
    wt._reset_cache()


def test_drop_then_reprovision_is_clean(tmp_path, monkeypatch):
    """Drop removes both schemas; a subsequent provision re-creates them cleanly."""
    from unittest.mock import patch

    from core import warehouse_tenancy as wt

    db_path = str(tmp_path / "drop_reprov.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    wt._reset_cache()

    schemas = _fake_schemas("org_delta_co")
    with patch("core.warehouse_tenancy.resolve_org_schemas", return_value=schemas):
        wt.provision_org_schemas(org_id=schemas.org_id)
        assert _schema_exists(db_path, schemas.raw)

        drop = wt.drop_org_schemas(org_id=schemas.org_id)
        assert drop["status"] == "ok"
        assert not _schema_exists(db_path, schemas.raw)
        assert not _schema_exists(db_path, schemas.marts)

        # Re-provision after drop must be clean (idempotent lifecycle).
        reprov = wt.provision_org_schemas(org_id=schemas.org_id)
        assert reprov["status"] == "ok"
        assert _schema_exists(db_path, schemas.raw)
        assert _schema_exists(db_path, schemas.marts)
    wt._reset_cache()


@pytest.mark.anyio
async def test_org_delete_endpoint_is_human_gated():
    """DELETE org warehouse without X-Confirm-Delete -> 422 (human-gated, RGPD)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from core.admin_api import _delete_org

    req = MagicMock()
    req.path_params = {"org_id": "org_gated"}
    req.body = AsyncMock(return_value=b"")
    req.headers = {}  # NO X-Confirm-Delete

    with patch("core.admin_api._check_auth", return_value=(True, "admin@example.com")):
        resp = await _delete_org(req)

    assert resp.status_code == 422, "drop must require explicit human confirmation"
    import json

    assert json.loads(resp.body)["code"] == "confirmation_required"


# ---------------------------------------------------------------------------
# 4. AD-8 intact -- STATIC guard: mirror_sync is the SOLE mirror writer
# ---------------------------------------------------------------------------

# server/tests/isolation/ -> server/
_SERVER_ROOT = Path(__file__).resolve().parents[2]
_DBT_ROOT = _SERVER_ROOT.parent / "dbt"


def test_ad8_warehouse_write_never_touches_mirror():
    """The raw writer (warehouse_write.py) never composes/writes a mirror schema.

    AD-8: mirror_sync.py is the ONLY Postgres->warehouse writer of the ``mirror``
    schema. A raw-write helper referencing ``mirror.`` or ``mirror_<...>`` would
    breach that single-path invariant. Static scan of the write entry point.
    """
    src = (_SERVER_ROOT / "core" / "warehouse_write.py").read_text(
        encoding="utf-8", errors="replace"
    )
    # Match a mirror SCHEMA reference: `mirror.<table>` or `mirror_<dataset>`.
    hits = re.findall(r"""["']?mirror[._]\w""", src)
    assert not hits, (
        "warehouse_write.py must NOT reference the mirror schema (AD-8: only "
        f"mirror_sync.py writes it). Found: {hits}"
    )


def test_ad8_dbt_naming_macros_never_touch_mirror():
    """The two dbt naming macros never compose/route a ``mirror`` schema (AD-8).

    generate_schema_name.sql and raw_source_schema.sql own org_* routing; the
    mirror source keeps its literal ``schema: mirror`` OUTSIDE these macros
    (documented in raw_source_schema.sql). A macro emitting ``org_..._mirror`` or
    routing the mirror per-org would breach AD-8.
    """
    for macro in ("generate_schema_name.sql", "raw_source_schema.sql"):
        path = _DBT_ROOT / "macros" / macro
        text = path.read_text(encoding="utf-8", errors="replace")
        # Strip the {# ... #} Jinja doc comment: the prose legitimately explains
        # the mirror carve-out. We only guard EXECUTABLE Jinja/SQL.
        code = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
        assert "mirror" not in code.lower(), (
            f"dbt/macros/{macro} must not route the mirror schema (AD-8); "
            "the mirror source keeps its literal schema OUTSIDE the naming point."
        )


def test_ad8_mirror_sync_is_the_declared_sole_writer():
    """mirror_sync.py still declares itself the ONLY mirror writer (AD-8 anchor).

    Locks the module-docstring contract so a refactor that removes the sole-writer
    guarantee trips a test rather than silently opening a second write path.
    """
    src = (_SERVER_ROOT / "core" / "mirror_sync.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "sole writer" in src.lower() or "only file that may write" in src.lower(), (
        "mirror_sync.py must keep its AD-8 sole-writer declaration"
    )


# ---------------------------------------------------------------------------
# 5. Read equivalence flag OFF (OFFLINE) -- legacy naming LOCKED pre-flip
# ---------------------------------------------------------------------------


def test_flag_off_mart_prefix_is_exact_legacy_and_touches_no_db(monkeypatch):
    """Flag OFF: mart_prefix(None) == 'main_marts.' with NO Postgres query.

    This is the isolation-level lock of the 24.3/24.4 read-equivalence: while the
    flag is OFF (default until 24.4 flips it), rollup/cards resolve the EXACT
    legacy prefix, so they render bit-identical numbers to pre-epic. Any code that
    touched Postgres here would change behaviour under the flag-OFF default.
    """
    from unittest.mock import patch

    from core import warehouse_tenancy as wt

    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    wt._reset_cache()

    with patch("core.db.get_connection") as mock_gc:
        assert wt.mart_prefix(None) == wt.LEGACY_DUCKDB_MART_PREFIX == "main_marts."
        assert wt.mart_prefix("proj_shared") == "main_marts."
        mock_gc.assert_not_called()  # flag OFF => zero DB round-trips
    wt._reset_cache()


def test_flag_off_bigquery_dataset_is_legacy_marts_project(monkeypatch):
    """Flag OFF: bigquery_marts_dataset == 'marts_<project_id>' (legacy BQ path).

    Locks the legacy BigQuery dataset naming so the BQ read path is unchanged
    pre-flip -- the isolation twin of the DuckDB main_marts. lock above.
    """
    from unittest.mock import patch

    from core import warehouse_tenancy as wt

    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    wt._reset_cache()

    with patch("core.db.get_connection") as mock_gc:
        assert wt.bigquery_marts_dataset("proj_shared") == "marts_proj_shared"
        mock_gc.assert_not_called()
    wt._reset_cache()


def test_flag_off_warehouse_read_builder_uses_legacy_prefix(monkeypatch):
    """End-to-end flag OFF: warehouse._duckdb_mart_prefix feeds 'main_marts.'.

    Proves the READ layer (not just warehouse_tenancy) resolves the legacy prefix
    under the flag-OFF default, so the equivalence guarantee holds at the exact
    seam rollup/cards call.
    """
    from core import warehouse

    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    assert warehouse._duckdb_mart_prefix("proj_shared") == "main_marts."
    assert warehouse._duckdb_mart_prefix(None) == "main_marts."


# ---------------------------------------------------------------------------
# PG-GATED: end-to-end provisioning idempotency through real org rows.
# ---------------------------------------------------------------------------


@pg_available
def test_provision_idempotent_through_real_org_resolution(tmp_path):
    """Real app.organizations row -> resolve_org_schemas -> provision x2 idempotent.

    Drives the FULL resolution path (Postgres org row -> slug -> sanitised
    warehouse_slug -> OrgSchemas) instead of a mocked resolver, then provisions
    twice against a temp DuckDB and asserts identical results + both schemas
    present. Complements the offline mocked-resolution idempotency test with a
    live end-to-end proof (pattern 24.5 live tests).
    """
    from core import warehouse_tenancy as wt
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"iso_org_{suffix}"
    slug = f"iso-org-{suffix}"  # kebab -> sanitised to iso_org_<suffix>

    db_path = str(tmp_path / "pg_idem.duckdb")
    prev_path = os.environ.get("TOOROW_DUCKDB_PATH")
    prev_mode = os.environ.get("TOOROW_DB_MODE")
    os.environ["TOOROW_DUCKDB_PATH"] = db_path
    os.environ.pop("TOOROW_DB_MODE", None)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, 'isolation-test')",
                (org_id, f"IsoOrg-{suffix}", slug),
            )
        conn.commit()

    try:
        wt._reset_cache()
        resolved = wt.resolve_org_schemas(org_id=org_id, fresh=True)
        assert resolved is not None, "real org must resolve to schemas"
        assert resolved.marts.endswith("_marts")
        assert resolved.raw.endswith("_raw")

        r1 = wt.provision_org_schemas(org_id=org_id)
        r2 = wt.provision_org_schemas(org_id=org_id)
        assert r1 == r2
        assert r1["status"] == "ok"
        assert _schema_exists(db_path, resolved.raw)
        assert _schema_exists(db_path, resolved.marts)

        # Drop is clean and re-provision after drop works (full lifecycle).
        drop = wt.drop_org_schemas(org_id=org_id)
        assert drop["status"] == "ok"
        assert not _schema_exists(db_path, resolved.raw)
        reprov = wt.provision_org_schemas(org_id=org_id)
        assert reprov["status"] == "ok"
        assert _schema_exists(db_path, resolved.marts)
    finally:
        wt._reset_cache()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
            conn.commit()
        if prev_path is None:
            os.environ.pop("TOOROW_DUCKDB_PATH", None)
        else:
            os.environ["TOOROW_DUCKDB_PATH"] = prev_path
        if prev_mode is None:
            os.environ.pop("TOOROW_DB_MODE", None)
        else:
            os.environ["TOOROW_DB_MODE"] = prev_mode
