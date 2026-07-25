"""toorow -- single point of warehouse topology naming (Story 24.1, Epic 24).

Every schema/dataset name of the DATA PLANE is resolved HERE and nowhere else
(same "single decider" discipline as project_access.py for access): the org is
the physical isolation boundary (P4, architecture-org-tenancy §6), a client's
data lives in ITS schemas ``org_<wslug>_raw`` / ``org_<wslug>_marts``.

Dual-mode (AC2): the ``TOOROW_ORG_SCHEMAS`` env flag defaults to OFF, in which
case every helper returns the EXACT legacy names (``main_marts.`` in DuckDB,
dataset ``marts_<project_id>`` in BigQuery) and NO database is touched -- the
whole platform behaves bit-identically to pre-24.1. Story 24.4 flips the
default once dbt materialises per-org schemas. With the flag ON, resolution
follows ``app.projects.org_id -> app.organizations.slug``; any failure
(project without org, unknown org, DB error) degrades to the legacy name with
a WARNING -- a read path NEVER crashes because of naming (same degradation
contract as branding.py).

Naming constraint: BigQuery dataset IDs only allow ``[A-Za-z0-9_]``, so the
kebab-case org slug is sanitised ``-`` -> ``_`` (``warehouse_slug``). The
sanitised form can collide (``a-b`` vs ``a_b``): org creation rejects such
collisions (admin_api, AC4) and the slug is IMMUTABLE after creation.

This module must stay source-agnostic (AD-2): no connector vocabulary, no
imports from server/modules.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flag & legacy constants (AC2)
# ---------------------------------------------------------------------------

_FLAG_ENV = "TOOROW_ORG_SCHEMAS"

#: Exact legacy DuckDB prefix (dbt-duckdb materialises marts into main_marts).
LEGACY_DUCKDB_MART_PREFIX = "main_marts."

#: BigQuery dataset id charset (dataset ids reject '-', hence sanitisation).
_WAREHOUSE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

#: Short process-local TTL so warehouse reads do not pay one Postgres query
#: each (AC1). 60 s keeps org renames impossible anyway (slug is immutable).
_CACHE_TTL_SECONDS = 60.0


def org_schemas_enabled() -> bool:
    """Return True when the org-partitioned topology is active (flag ON).

    Default OFF: story 24.4 owns the flip (dbt must materialise per-org first).
    """
    return os.environ.get(_FLAG_ENV, "0").strip().lower() in ("1", "true")


# ---------------------------------------------------------------------------
# Slug sanitisation (BigQuery dataset charset)
# ---------------------------------------------------------------------------


def sanitize_warehouse_slug(slug: str | None) -> str | None:
    """Kebab-case org slug -> warehouse-safe form (``-`` -> ``_``), or None.

    Returns None when the sanitised form still violates ``[A-Za-z0-9_]+``
    (defensive: the admin slug regex should already prevent this).
    """
    if not slug:
        return None
    candidate = slug.replace("-", "_")
    if not _WAREHOUSE_NAME_RE.match(candidate):
        return None
    return candidate


# ---------------------------------------------------------------------------
# Resolution app.projects.org_id -> app.organizations.slug (AC1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrgSchemas:
    """Resolved warehouse topology of one organization."""

    org_id: str
    org_slug: str
    warehouse_slug: str
    raw: str
    marts: str


_SQL_BY_PROJECT = """
    SELECT o.id, o.slug
    FROM app.projects p
    JOIN app.organizations o ON o.id = p.org_id
    WHERE p.id = %s
"""

_SQL_BY_ORG = "SELECT id, slug FROM app.organizations WHERE id = %s"

#: Active orgs only (status='active'); archived orgs keep their schemas until the
#: human-gated drop (24.2) but are NOT materialised / scanned nightly (24.4).
_SQL_ACTIVE_ORGS = "SELECT id, slug FROM app.organizations WHERE status = 'active'"

# key -> (monotonic_ts, OrgSchemas | None). None IS cached (negative caching:
# a legacy project without org must not re-query Postgres on every read).
# Known window (review 24.1, LOW): attaching an org to a legacy project keeps
# resolving None for up to the TTL -- no invalidation hook on org attachment.
# Acceptable eventual consistency; 24.2 provisioning must not rely on an
# immediate flip within the same process.
_CACHE: dict[tuple[str, str], tuple[float, OrgSchemas | None]] = {}
_CACHE_LOCK = threading.Lock()

# WARNING dedup: warn once per degradation key, not once per read.
_WARNED: set[str] = set()


def _reset_cache() -> None:
    """Test seam: clear the resolution cache and the warn-once set."""
    with _CACHE_LOCK:
        _CACHE.clear()
    _WARNED.clear()


def _warn_once(key: str, message: str, *args) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, *args)


def _build(org_id: str, org_slug: str) -> OrgSchemas | None:
    wslug = sanitize_warehouse_slug(org_slug)
    if wslug is None:
        _warn_once(
            f"badslug:{org_id}",
            "warehouse_tenancy: org %s slug %r not sanitisable to [A-Za-z0-9_]",
            org_id,
            org_slug,
        )
        return None
    return OrgSchemas(
        org_id=org_id,
        org_slug=org_slug,
        warehouse_slug=wslug,
        raw=f"org_{wslug}_raw",
        marts=f"org_{wslug}_marts",
    )


def _fetch_one(sql: str, param: str, conn=None):
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, (param,))
            return cur.fetchone()
    from core.db import get_connection  # noqa: PLC0415 -- optional dep (psycopg)

    with get_connection() as own_conn, own_conn.cursor() as cur:
        cur.execute(sql, (param,))
        return cur.fetchone()


def resolve_org_schemas(
    project_id: str | None = None,
    org_id: str | None = None,
    conn=None,
    fresh: bool = False,
) -> OrgSchemas | None:
    """Resolve the org warehouse schemas for *project_id* OR *org_id*.

    Returns None on: no identifier, project without org (legacy ``org_id``
    NULL), unknown org, unsanitisable slug, or ANY DB error -- always with a
    WARNING (once per key), NEVER an exception (read paths degrade to legacy
    naming, AC1). Both ``active`` and ``archived`` orgs resolve: an archived
    org keeps its schemas until the human-gated drop of story 24.2.

    ``fresh=True`` bypasses the cache read (always queries Postgres) but still
    writes the fresh result back -- use for provision/drop paths to avoid a
    stale negative-cache entry blocking a legitimate operation (F-5).
    """
    if project_id:
        key = ("project", project_id)
        sql, param = _SQL_BY_PROJECT, project_id
    elif org_id:
        key = ("org", org_id)
        sql, param = _SQL_BY_ORG, org_id
    else:
        return None

    now = time.monotonic()
    if not fresh:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
                return hit[1]

    try:
        row = _fetch_one(sql, param, conn=conn)
    except Exception as exc:  # noqa: BLE001 -- degradation contract (AC1)
        _warn_once(
            f"dberr:{key[0]}:{key[1]}",
            "warehouse_tenancy: resolution failed for %s=%s: %s",
            key[0],
            key[1],
            exc,
        )
        return None

    if row is None:
        _warn_once(
            f"miss:{key[0]}:{key[1]}",
            "warehouse_tenancy: no org resolvable for %s=%s (legacy naming used)",
            key[0],
            key[1],
        )
        resolved = None
    else:
        resolved = _build(str(row[0]), str(row[1]))

    with _CACHE_LOCK:
        _CACHE[key] = (now, resolved)
    return resolved


def list_active_orgs(conn=None) -> list[OrgSchemas]:
    """Return the ``OrgSchemas`` of every ACTIVE org (Story 24.4 fan-out).

    Reads ``app.organizations`` DIRECTLY in Postgres (decision: the nightly
    orchestrator already holds ``get_connection``; the control plane is NOT
    widened into the mirror just for this). Archived orgs are excluded: they
    keep their schemas until the human-gated drop (24.2) but are not
    materialised / scanned nightly.

    NEVER raises: on any DB error (or psycopg absent) returns [] with a WARNING,
    so the nightly loop degrades to "no org fan-out" instead of crashing (same
    degradation contract as resolve_org_schemas). An org whose slug is not
    sanitisable is skipped (``_build`` warns once).

    The returned list is the ONLY source of the warehouse_slug for the per-org
    dbt run and the cross-project fan-out -- callers never compose ``org_*``.
    """
    try:
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(_SQL_ACTIVE_ORGS)
                rows = cur.fetchall()
        else:
            from core.db import get_connection  # noqa: PLC0415 -- optional dep

            with get_connection() as own_conn, own_conn.cursor() as cur:
                cur.execute(_SQL_ACTIVE_ORGS)
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- degradation contract (never crash nightly)
        _warn_once(
            "listactive:dberr",
            "warehouse_tenancy: list_active_orgs failed: %s (no org fan-out)",
            exc,
        )
        return []

    resolved: list[OrgSchemas] = []
    for row in rows:
        schemas = _build(str(row[0]), str(row[1]))
        if schemas is not None:
            resolved.append(schemas)
    return resolved


# ---------------------------------------------------------------------------
# Data-plane schema provisioning / drop (Story 24.2)
# ---------------------------------------------------------------------------

#: Lock for DuckDB write operations in this module.  Mirrors the pattern of
#: mirror_sync._duckdb_write_lock but kept private here to avoid circular
#: imports (mirror_sync owns the Postgres->warehouse sync path, AD-8).
_PROVISION_LOCK = threading.Lock()

_BQ_PROVISION_ENV = "TOOROW_BQ_PROVISION_ENABLED"
_DUCKDB_PATH_ENV = "TOOROW_DUCKDB_PATH"
_DB_MODE_ENV = "TOOROW_DB_MODE"


def _duckdb_path() -> str | None:
    """Return the DuckDB file path from env, or None when not configured."""
    return os.environ.get(_DUCKDB_PATH_ENV, "").strip() or None


def bq_provisioning_enabled() -> bool:
    """True when creating an org must create its BigQuery datasets.

    Deliberately INDEPENDENT of ``TOOROW_DB_MODE``. Those are two different
    questions and conflating them made the feature unreachable:

      * where an ORG'S DATASETS LIVE -- on Cloud Run the container disk is
        ephemeral, so DuckDB is not a real target and a deployed org's
        warehouse is BigQuery;
      * where a PULL LANDS -- still DuckDB, because eight connectors hard-raise
        on any non-duckdb mode.

    While the two were tied together, "create an org, get its warehouse" could
    only be turned on by flipping the whole platform to a landing mode that
    breaks ingestion -- so it was never turned on at all. Splitting them lets
    provisioning ship now and the landing migration proceed on its own clock.
    """
    return os.environ.get(_BQ_PROVISION_ENV, "0").strip().lower() in ("1", "true")


def _provision_bigquery_datasets(schemas: OrgSchemas) -> None:  # pragma: no cover -- AI-08 gate
    """Create BigQuery datasets for *schemas* (gated by TOOROW_BQ_PROVISION_ENABLED).

    Import is LOCAL so google.cloud is NEVER imported in DuckDB mode (AC6).
    Location EU: decision actée spike §4 + archi §6.
    """
    from google.cloud import bigquery  # noqa: PLC0415

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "toorow")
    client = bigquery.Client()
    for dataset_id in (schemas.raw, schemas.marts):
        ds = bigquery.Dataset(f"{project}.{dataset_id}")
        ds.location = "EU"
        client.create_dataset(ds, exists_ok=True)
    logger.info(
        "warehouse_tenancy: bigquery datasets provisioned raw=%s marts=%s",
        schemas.raw,
        schemas.marts,
    )


def _drop_bigquery_datasets(schemas: OrgSchemas) -> None:  # pragma: no cover -- AI-08 gate
    """Drop BigQuery datasets for *schemas* (gated by TOOROW_BQ_PROVISION_ENABLED).

    delete_contents=True matches the DROP SCHEMA ... CASCADE semantics.
    """
    from google.cloud import bigquery  # noqa: PLC0415

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "toorow")
    client = bigquery.Client()
    for dataset_id in (schemas.raw, schemas.marts):
        client.delete_dataset(
            f"{project}.{dataset_id}",
            delete_contents=True,
            not_found_ok=True,
        )
    logger.info(
        "warehouse_tenancy: bigquery datasets dropped raw=%s marts=%s",
        schemas.raw,
        schemas.marts,
    )


def _simulate_bq_iam_grant(
    org_schemas: OrgSchemas, principal: str, action: str
) -> None:
    """Story 24.5 (AC6-AC7): log-only stub for BigQuery IAM grant/revoke.

    Validates that the target dataset is exclusively ``marts`` (never raw,
    never mirror_*) then emits an INFO trace.  action ∈ "grant" | "revoke".

    The real SDK call is commented out below (AI-08 gate -- Phase B only).
    Import of google.cloud is intentionally absent: this stub MUST NOT import
    the SDK regardless of env flags so that tests can assert_not_called without
    needing to patch sys.modules.
    """
    # Guard: dataset must be the marts schema only (AC7 invariant). raise, not
    # assert -- the guard must survive `python -O` (review 24.5 F-6).
    if not org_schemas.marts.endswith("_marts"):
        raise ValueError(
            f"bq_iam_simulate: dataset {org_schemas.marts!r} does not end with"
            " '_marts' -- only marts datasets may receive IAM grants"
        )
    if "_raw" in org_schemas.marts:
        raise ValueError(
            f"bq_iam_simulate: refusing to grant on raw dataset {org_schemas.marts!r}"
        )
    if "mirror_" in org_schemas.marts:
        raise ValueError(
            f"bq_iam_simulate: refusing to grant on mirror dataset {org_schemas.marts!r}"
        )
    logger.info(
        "bq_iam_simulate: %s role/bigquery.dataViewer on %s for %s",
        action,
        org_schemas.marts,
        principal,
    )
    # Phase B: appel réel (AI-08 -- non exécuté en dev/CI)
    # from google.cloud import bigquery_v2  # noqa: PLC0415
    # client = bigquery_v2.AnalyticsHubServiceClient()
    # project = os.environ.get("GOOGLE_BQ_PROJECT", "toorow")
    # resource = f"projects/{project}/datasets/{org_schemas.marts}"
    # policy = client.get_iam_policy(resource=resource)
    # member = principal  # e.g. "serviceAccount:sa@project.iam.gserviceaccount.com"
    # if action == "grant":
    #     policy.bindings.add(role="roles/bigquery.dataViewer", members=[member])
    # else:
    #     # revoke: remove member from binding
    #     for b in policy.bindings:
    #         if b.role == "roles/bigquery.dataViewer":
    #             b.members.discard(member)
    # client.set_iam_policy(resource=resource, policy=policy)


def provision_org_schemas(org_id: str, conn=None) -> dict[str, Any]:
    """Create warehouse schemas for *org_id* (idempotent, AC1-AC2).

    Uses ``resolve_org_schemas`` so schema names are NEVER composed inline.
    DuckDB: ``CREATE SCHEMA IF NOT EXISTS`` (idempotent).  Non-blocking by
    contract: the caller must never let provisioning failure block a 201.

    Returns a dict with ``status`` in ``{"ok", "skipped"}`` and, on skip,
    a ``reason`` key.  On DuckDB I/O failure the exception propagates so the
    caller can log it at WARNING and still return its 201 (AC1).
    """
    schemas = resolve_org_schemas(org_id=org_id, conn=conn, fresh=True)
    if schemas is None:
        logger.warning(
            "warehouse_tenancy: provision_org_schemas org=%s slug not resolvable -- skip",
            org_id,
        )
        return {"status": "skipped", "reason": "unresolvable", "org_id": org_id}

    if bq_provisioning_enabled():
        _provision_bigquery_datasets(schemas)
        return {"status": "ok", "raw": schemas.raw, "marts": schemas.marts}

    db_mode = os.environ.get(_DB_MODE_ENV, "").strip().lower()
    if db_mode == "bigquery":
        logger.info(
            "warehouse_tenancy: bigquery provisioning gated AI-08 org=%s", org_id
        )
        return {"status": "skipped", "reason": "bq_gated_ai08", "org_id": org_id}

    # DuckDB path
    db_path = _duckdb_path()
    if not db_path:
        logger.warning(
            "warehouse_tenancy: provision_org_schemas org=%s TOOROW_DUCKDB_PATH not set -- skip",
            org_id,
        )
        return {"status": "skipped", "reason": "no_duckdb_path", "org_id": org_id}

    import duckdb  # noqa: PLC0415

    with _PROVISION_LOCK:
        conn_duck = duckdb.connect(db_path)
        try:
            conn_duck.execute(f"CREATE SCHEMA IF NOT EXISTS {schemas.raw}")
            conn_duck.execute(f"CREATE SCHEMA IF NOT EXISTS {schemas.marts}")
        finally:
            conn_duck.close()

    logger.info(
        "warehouse_tenancy: schemas provisioned org=%s raw=%s marts=%s",
        org_id,
        schemas.raw,
        schemas.marts,
    )
    return {"status": "ok", "raw": schemas.raw, "marts": schemas.marts}


def drop_org_schemas(org_id: str, conn=None) -> dict[str, Any]:
    """Drop warehouse schemas for *org_id* (human-gated, AC5).

    Unlike provisioning this is a BLOCKING operation (the caller must not
    delete the Postgres row if the warehouse drop cannot be confirmed).

    Returns ``{"status": "ok", ...}`` on success.
    Returns ``{"status": "skipped", "reason": "no_duckdb_path"}`` when DuckDB
    is not configured (no path) -- the caller treats this as OK (nothing to drop).
    Raises on any I/O failure so the caller can block the Postgres DELETE (RGPD).
    """
    schemas = resolve_org_schemas(org_id=org_id, conn=conn, fresh=True)
    if schemas is None:
        logger.warning(
            "warehouse_tenancy: drop_org_schemas org=%s slug not resolvable -- cannot drop",
            org_id,
        )
        # Return a skipped-unresolvable so the caller can decide (RGPD: caller blocks).
        return {"status": "skipped", "reason": "unresolvable", "org_id": org_id}

    # Symmetry is mandatory here: if creation provisioned BigQuery datasets, the
    # RGPD drop must remove those same datasets, never silently fall through to
    # the DuckDB branch and report a success it did not perform.
    if bq_provisioning_enabled():
        _drop_bigquery_datasets(schemas)
        return {"status": "ok", "raw": schemas.raw, "marts": schemas.marts}

    db_mode = os.environ.get(_DB_MODE_ENV, "").strip().lower()
    if db_mode == "bigquery":
        logger.info(
            "warehouse_tenancy: bigquery drop gated AI-08 org=%s", org_id
        )
        return {"status": "skipped", "reason": "bq_gated_ai08", "org_id": org_id}

    # DuckDB path
    db_path = _duckdb_path()
    if not db_path:
        logger.warning(
            "warehouse_tenancy: drop_org_schemas org=%s TOOROW_DUCKDB_PATH not set"
            " -- skip (nothing to drop)",
            org_id,
        )
        return {"status": "skipped", "reason": "no_duckdb_path", "org_id": org_id}

    import duckdb  # noqa: PLC0415

    with _PROVISION_LOCK:
        conn_duck = duckdb.connect(db_path)
        try:
            conn_duck.execute(f"DROP SCHEMA IF EXISTS {schemas.raw} CASCADE")
            conn_duck.execute(f"DROP SCHEMA IF EXISTS {schemas.marts} CASCADE")
        finally:
            conn_duck.close()

    logger.info(
        "warehouse_tenancy: schemas dropped org=%s raw=%s marts=%s",
        org_id,
        schemas.raw,
        schemas.marts,
    )
    return {"status": "ok", "raw": schemas.raw, "marts": schemas.marts}


# ---------------------------------------------------------------------------
# The two naming entry points consumed by the read layer (AC2/AC3/AC5)
# ---------------------------------------------------------------------------


def mart_prefix(project_id: str | None = None, conn=None) -> str:
    """DuckDB marts schema prefix for *project_id* (trailing dot included).

    Flag OFF (default): the exact legacy ``main_marts.`` -- no DB touched.
    Flag ON: ``org_<wslug>_marts.``; unresolvable -> legacy + WARNING.
    """
    if not org_schemas_enabled():
        return LEGACY_DUCKDB_MART_PREFIX
    resolved = resolve_org_schemas(project_id=project_id, conn=conn)
    if resolved is None:
        if project_id is None:
            _warn_once(
                "noproject:mart_prefix",
                "warehouse_tenancy: mart_prefix called without project_id under "
                "TOOROW_ORG_SCHEMAS=1 (legacy prefix used)",
            )
        return LEGACY_DUCKDB_MART_PREFIX
    return f"{resolved.marts}."


def bigquery_marts_dataset(project_id: str, conn=None) -> str:
    """BigQuery marts dataset id for *project_id*.

    Flag OFF (default): the exact legacy ``marts_<project_id>``.
    Flag ON: ``org_<wslug>_marts``; unresolvable -> legacy + WARNING.
    """
    if not org_schemas_enabled():
        return f"marts_{project_id}"
    resolved = resolve_org_schemas(project_id=project_id, conn=conn)
    if resolved is None:
        return f"marts_{project_id}"
    return resolved.marts
