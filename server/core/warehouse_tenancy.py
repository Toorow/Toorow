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

#: WHERE EVERY DATASET OF THIS PLATFORM LIVES -- one name, because the second
#: place that decided it decided differently and nothing could see it.
#: `raw_landing._land_bigquery` created the raw zone by ID with no location, so
#: BigQuery applied its own default (US) while this module pinned EU on the
#: zones IT provisions ("decision actée spike §4 + archi §6") and the nightly's
#: dbt profile queries in EU. BigQuery does not read across locations: measured
#: 2026-08-24, the two live `raw_proj_*` zones are US, everything else is EU, and
#: the nightly answered `Not found: Dataset toorow:raw_proj_… was not found in
#: location EU` on the only staging model the live Project has (AI-314).
BIGQUERY_LOCATION = "EU"

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

    @property
    def expected_marts(self) -> str:
        """The ONE marts dataset an org-scoped outbound read may ever open."""
        return f"org_{self.warehouse_slug}_marts"


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
# The tenancy the LIVE deployment uses -- per PROJECT (AI-166, 2026-08-17)
# ---------------------------------------------------------------------------
#
# `list_active_orgs` above answers "which orgs exist" and composes
# ``org_<wslug>_raw`` / ``org_<wslug>_marts``. Those datasets are provisioned
# shells: measured 2026-08-17, `org_toorow_raw` and `org_toorow_marts` hold ZERO
# tables, while the rows live in `raw_proj_*` -- the shape the resolvers below
# return with TOOROW_ORG_SCHEMAS OFF, which is the flag's live value.
#
# A nightly build addressed at the org zones therefore reads an empty dataset and
# writes an empty one. `list_active_projects` returns the zones the read layer
# actually addresses, composed by the SAME resolvers the readers call -- so if
# the flag is ever flipped ON, the builder follows the readers instead of
# disagreeing with them.


@dataclass(frozen=True)
class ProjectZones:
    """The three warehouse zones of ONE project, named as the READ layer names them.

    Every field comes from the ``bigquery_*_dataset`` resolvers below -- never
    composed here (single naming point, epic-24 invariant).
    """

    project_id: str
    raw: str
    staging: str
    marts: str


#: Active projects of active orgs. An archived project (or a project of an
#: archived org) keeps its datasets until the human-gated drop but is not
#: rebuilt nightly -- the same rule `_SQL_ACTIVE_ORGS` applies one level up.
_SQL_ACTIVE_PROJECTS = """
    SELECT p.id
    FROM app.projects p
    JOIN app.organizations o ON o.id = p.org_id
    WHERE p.status = 'active' AND o.status = 'active'
    ORDER BY p.id
"""


def list_active_projects(conn=None) -> list[ProjectZones]:
    """Return the warehouse zones of every ACTIVE project (AI-166 fan-out).

    NEVER raises: on any DB error (or psycopg absent) returns [] with a WARNING,
    exactly like ``list_active_orgs`` -- the nightly loop degrades to "no
    project fan-out" instead of crashing.
    """
    try:
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(_SQL_ACTIVE_PROJECTS)
                rows = cur.fetchall()
        else:
            from core.db import get_connection  # noqa: PLC0415 -- optional dep

            with get_connection() as own_conn, own_conn.cursor() as cur:
                cur.execute(_SQL_ACTIVE_PROJECTS)
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- degradation contract (never crash nightly)
        _warn_once(
            "listactiveprojects:dberr",
            "warehouse_tenancy: list_active_projects failed: %s (no project fan-out)",
            exc,
        )
        return []

    zones: list[ProjectZones] = []
    for row in rows:
        project_id = str(row[0])
        zones.append(
            ProjectZones(
                project_id=project_id,
                raw=bigquery_raw_dataset(project_id, conn=conn),
                staging=bigquery_staging_dataset(project_id, conn=conn),
                marts=bigquery_marts_dataset(project_id, conn=conn),
            )
        )
    return zones


# ---------------------------------------------------------------------------
# The marts zone of ONE project, for a governed OUTBOUND read (story 62.3)
# ---------------------------------------------------------------------------
#
# THE PHYSICS DECIDES WHETHER PROJECT SCOPE EXISTS AT ALL, and it is not the
# same answer under the two values of TOOROW_ORG_SCHEMAS:
#
#   flag OFF (the live value)  -> `bigquery_marts_dataset` returns
#       ``marts_<project_id>``: one dataset per PROJECT, so a reader added to it
#       reads that project and nothing else. Project scope is real.
#   flag ON                    -> it returns ``org_<wslug>_marts``: ONE dataset
#       per ORG holding every project of that org. A "project" grant on it would
#       hand the neighbour's rows to the same reader. Project scope is NOT
#       representable, and the only honest answer is a refusal that says so --
#       never a grant that quietly opens more than it names.
#
# `project_marts_scope` returns exactly one of those two verdicts. It NEVER
# invents a per-project dataset that the read layer does not address.


@dataclass(frozen=True)
class ProjectMarts:
    """The marts dataset of ONE project, named as the READ layer names it."""

    project_id: str
    org_id: str
    warehouse_slug: str
    marts: str

    @property
    def expected_marts(self) -> str:
        """The ONE marts dataset a project-scoped outbound read may ever open."""
        return f"marts_{self.project_id}"


#: Why a resolved dataset cannot carry a durable outbound read. Each value is a
#: SENTENCE for the person, naming the gesture -- never a state of deployment.
PROJECT_SCOPE_UNAVAILABLE = (
    "This deployment keeps one marts dataset per organization "
    "({dataset}), which holds every project of the organization. A read opened "
    "from this project would also open its neighbours, so it is refused. Open "
    "the read from Organization settings, or ask for per-project marts datasets "
    "before granting from here."
)


def project_marts_scope(project_id: str, conn=None) -> tuple["ProjectMarts | None", str | None]:
    """Return the project marts zone, or the sentence that refuses project scope.

    Exactly one of the two is not None. A read path: nothing is written here.
    """
    if not project_id:
        return None, "Name the project whose published marts are being opened."
    resolved = resolve_org_schemas(project_id=project_id, conn=conn, fresh=True)
    dataset = bigquery_marts_dataset(project_id, conn=conn)
    if dataset != f"marts_{project_id}":
        return None, PROJECT_SCOPE_UNAVAILABLE.format(dataset=dataset)
    if resolved is None:
        return None, (
            "This project is not attached to an organization yet, so its marts "
            "dataset cannot be named. Attach the project to an organization, "
            "then request the read again."
        )
    return (
        ProjectMarts(
            project_id=project_id,
            org_id=resolved.org_id,
            warehouse_slug=resolved.warehouse_slug,
            marts=dataset,
        ),
        None,
    )


#: A name that says "this will be thrown away". A dashboard bound to one of
#: these breaks the week it is rebuilt, which is the open question the reference
#: case never answered -- so the product answers it by refusing the target.
_SLATE_TOKENS = ("sandbox", "scratch", "tmp", "temp", "throwaway", "wip")
_SLATE_DATE_SUFFIX = re.compile(
    r"_(?:19|20)\d{2}(?:[_-]?(?:0[1-9]|1[0-2])(?:[_-]?(?:0[1-9]|[12]\d|3[01]))?)$"
)


def slate_dataset_reason(dataset_id: str) -> str | None:
    """Return the refusal sentence when *dataset_id* is a slate name, else None."""
    name = (dataset_id or "").strip().lower()
    if not name:
        return "Name the dataset the read opens."
    tokens = set(name.split("_"))
    hit = next((token for token in _SLATE_TOKENS if token in tokens), None)
    if hit is not None:
        return (
            f"{dataset_id!r} is a scratch name (it contains {hit!r}), and a "
            "dashboard bound to it breaks the day it is rebuilt. Rename the "
            "project or organization to the name the reporting should keep, "
            "then request the read again."
        )
    if _SLATE_DATE_SUFFIX.search(name):
        return (
            f"{dataset_id!r} ends in a date, so it names one build rather than a "
            "durable table. Rename the project or organization to a name without "
            "a date, then request the read again."
        )
    return None


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


def configured_bigquery_project() -> str:
    """Return the explicitly configured security boundary for BigQuery writes."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for BigQuery mutations")
    return project


def _provision_bigquery_datasets(schemas: OrgSchemas) -> None:  # pragma: no cover -- AI-08 gate
    """Create BigQuery datasets for *schemas* (gated by TOOROW_BQ_PROVISION_ENABLED).

    Import is LOCAL so google.cloud is NEVER imported in DuckDB mode (AC6).
    Location EU: decision actée spike §4 + archi §6.
    """
    from google.cloud import bigquery  # noqa: PLC0415

    project = configured_bigquery_project()
    client = bigquery.Client(project=project)
    for dataset_id in (schemas.raw, schemas.marts):
        ds = bigquery.Dataset(f"{project}.{dataset_id}")
        ds.location = BIGQUERY_LOCATION
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

    project = configured_bigquery_project()
    client = bigquery.Client(project=project)
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


def _validate_dataset_access_target(org_schemas: OrgSchemas | ProjectMarts) -> None:
    """Refuse every dataset except the resolved marts dataset of THIS scope.

    Two scopes reach here and each one names its own single legal dataset
    through ``expected_marts``: an organization opens ``org_<wslug>_marts``, a
    project opens ``marts_<project_id>``. Nothing else passes -- not raw, not a
    mirror, not staging, not a neighbour's dataset, and not a scratch name.
    """
    if "_raw" in org_schemas.marts:
        raise ValueError(
            f"dataset access: refusing raw dataset {org_schemas.marts!r}"
        )
    if "mirror_" in org_schemas.marts:
        raise ValueError(
            f"dataset access: refusing mirror dataset {org_schemas.marts!r}"
        )
    if "staging" in org_schemas.marts:
        raise ValueError(
            f"dataset access: refusing staging dataset {org_schemas.marts!r}"
        )
    slate = slate_dataset_reason(org_schemas.marts)
    if slate is not None:
        raise ValueError(f"dataset access: {slate}")
    expected = getattr(org_schemas, "expected_marts", None)
    if expected is None:  # pragma: no cover -- defensive: an unknown scope object
        expected = f"org_{org_schemas.warehouse_slug}_marts"
    if org_schemas.marts != expected:
        raise ValueError(
            f"dataset access: refusing foreign or malformed dataset "
            f"{org_schemas.marts!r}; resolved marts is {expected!r}"
        )


def mutate_bigquery_dataset_access(
    org_schemas: OrgSchemas | ProjectMarts,
    principal: str,
    action: str,
    *,
    client=None,
) -> dict[str, Any]:
    """Grant or revoke read access on exactly one resolved marts dataset.

    BigQuery's Python client exposes dataset ACLs through ``access_entries``;
    its IAM policy methods are table-only.  The dataset object returned by
    ``get_dataset`` carries the current ETag, and passing that same object to
    ``update_dataset`` makes the replacement conditional while preserving all
    unrelated access entries.
    """
    if action not in {"grant", "revoke"}:
        raise ValueError("dataset access action must be 'grant' or 'revoke'")
    _validate_dataset_access_target(org_schemas)

    try:
        principal_kind, principal_id = principal.split(":", 1)
    except ValueError as exc:
        raise ValueError("dataset access principal must be type:identifier") from exc
    entity_type = {
        "user": "userByEmail",
        "serviceAccount": "userByEmail",
        "group": "groupByEmail",
    }.get(principal_kind)
    if entity_type is None or not principal_id:
        raise ValueError("unsupported dataset access principal")

    from google.cloud import bigquery  # noqa: PLC0415

    project = configured_bigquery_project()
    if client is None:
        client = bigquery.Client(project=project)
    dataset_ref = f"{project}.{org_schemas.marts}"
    dataset = client.get_dataset(dataset_ref)
    entries = list(dataset.access_entries or ())

    def is_target(entry) -> bool:
        return (
            entry.role == "READER"
            and entry.entity_type == entity_type
            and entry.entity_id == principal_id
        )

    changed = False
    if action == "grant":
        if not any(is_target(entry) for entry in entries):
            entries.append(bigquery.AccessEntry("READER", entity_type, principal_id))
            changed = True
    else:
        retained = [entry for entry in entries if not is_target(entry)]
        changed = len(retained) != len(entries)
        entries = retained

    if changed:
        if not getattr(dataset, "etag", None):
            raise RuntimeError("BigQuery dataset response has no ETag; refusing ACL update")
        dataset.access_entries = entries
        client.update_dataset(dataset, ["access_entries"])
    logger.info(
        "dataset_access: %s roles/bigquery.dataViewer on %s for %s changed=%s",
        action,
        dataset_ref,
        principal,
        changed,
    )
    return {
        "dataset_id": org_schemas.marts,
        "dataset_ref": dataset_ref,
        "role": "roles/bigquery.dataViewer",
        "changed": changed,
    }


def read_bigquery_dataset_access(
    org_schemas: OrgSchemas | ProjectMarts,
    principal: str,
    *,
    client=None,
) -> dict[str, Any]:
    """Read the exact reader entry used to reconcile an ambiguous mutation."""
    _validate_dataset_access_target(org_schemas)
    try:
        principal_kind, principal_id = principal.split(":", 1)
    except ValueError as exc:
        raise ValueError("dataset access principal must be type:identifier") from exc
    entity_type = {
        "user": "userByEmail",
        "serviceAccount": "userByEmail",
        "group": "groupByEmail",
    }.get(principal_kind)
    if entity_type is None or not principal_id:
        raise ValueError("unsupported dataset access principal")

    from google.cloud import bigquery  # noqa: PLC0415

    project = configured_bigquery_project()
    if client is None:
        client = bigquery.Client(project=project)
    dataset_ref = f"{project}.{org_schemas.marts}"
    dataset = client.get_dataset(dataset_ref)
    present = any(
        entry.role == "READER"
        and entry.entity_type == entity_type
        and entry.entity_id == principal_id
        for entry in (dataset.access_entries or ())
    )
    return {
        "dataset_id": org_schemas.marts,
        "dataset_ref": dataset_ref,
        "role": "roles/bigquery.dataViewer",
        "present": present,
    }


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


def bigquery_raw_dataset(project_id: str, conn=None) -> str:
    """BigQuery RAW dataset id for *project_id* (the landing zone, AD-7).

    The exact mirror of ``bigquery_marts_dataset`` for the other half of the
    warehouse. Marts had a resolver and raw did not, which was fine for as long
    as nothing could write raw to BigQuery at all.

    Flag OFF (default): the legacy ``raw_<project_id>``.
    Flag ON: ``org_<wslug>_raw``; unresolvable -> legacy + WARNING.
    """
    if not org_schemas_enabled():
        return f"raw_{project_id}"
    resolved = resolve_org_schemas(project_id=project_id, conn=conn)
    if resolved is None:
        _warn_once(
            f"noschema:raw_dataset:{project_id}",
            "warehouse_tenancy: raw dataset unresolvable for project_id=%s"
            " -- falling back to legacy",
            project_id,
        )
        return f"raw_{project_id}"
    return resolved.raw


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


# ---------------------------------------------------------------------------
# The two zones the read layer could not address until story 58.3
# ---------------------------------------------------------------------------
#
# Marts had a prefix and a dataset, raw had a dataset, and STAGING had nothing at
# all -- `grep -rn "main_staging\|_staging\." server/core/*.py` answered 0 on
# 2026-08-06. That was fine while nothing read below the mart; the day-grain
# `Collected` / `Mapped` reading has to address both zones, and composing their
# names at the call site is exactly what this module exists to prevent.
#
# THE RULE IS THE DBT MACRO'S, NOT AN INVENTION. `dbt/macros/generate_schema_name.sql`
# is "the ONLY place in the whole dbt project where the org_<wslug>_<custom>
# composition lives", and it states both halves: without `var('org')` dbt's own
# behaviour (dbt-duckdb: ``main`` / ``main_staging`` / ``main_marts``), with it
# ``org_<wslug>[_<custom>]``. Verified on a built fixture the same day: the schemas
# present are `main`, `main_staging`, `main_marts`, `org_test_staging`,
# `org_test_marts`, and the raw relations sit in `main` WITHOUT a prefix.
#
# The DuckDB raw zone resolves to ``OrgSchemas.raw`` rather than to the macro's
# bare ``org_<wslug>``, because that is where the rows actually are:
# `warehouse_write.open_raw_writer` puts every connector's landing on that schema
# (search_path), and a reader must address the writer's zone, not the one a model
# would default to.

#: Exact legacy DuckDB schemas of the two zones (dbt-duckdb, no ``--vars``).
LEGACY_DUCKDB_RAW_SCHEMA = "main"
LEGACY_DUCKDB_STAGING_SCHEMA = "main_staging"


def raw_schema(project_id: str | None = None, conn=None) -> str:
    """DuckDB schema holding the RAW landings of *project_id*.

    Flag OFF (default): ``main`` -- where every connector lands today.
    Flag ON: ``org_<wslug>_raw``, the schema `open_raw_writer` writes into;
    unresolvable -> legacy + WARNING, like every other resolver here.
    """
    if not org_schemas_enabled():
        return LEGACY_DUCKDB_RAW_SCHEMA
    resolved = resolve_org_schemas(project_id=project_id, conn=conn)
    if resolved is None:
        _warn_once(
            f"noschema:raw_schema:{project_id}",
            "warehouse_tenancy: raw schema unresolvable for project_id=%s"
            " -- falling back to legacy",
            project_id,
        )
        return LEGACY_DUCKDB_RAW_SCHEMA
    return resolved.raw


def staging_schema(project_id: str | None = None, conn=None) -> str:
    """DuckDB schema holding the STAGING models of *project_id*.

    Flag OFF (default): ``main_staging``. Flag ON: ``org_<wslug>_staging``, the
    name the dbt macro composes for a node whose custom schema is ``staging``.
    """
    if not org_schemas_enabled():
        return LEGACY_DUCKDB_STAGING_SCHEMA
    resolved = resolve_org_schemas(project_id=project_id, conn=conn)
    if resolved is None:
        _warn_once(
            f"noschema:staging_schema:{project_id}",
            "warehouse_tenancy: staging schema unresolvable for project_id=%s"
            " -- falling back to legacy",
            project_id,
        )
        return LEGACY_DUCKDB_STAGING_SCHEMA
    return f"org_{resolved.warehouse_slug}_staging"


def raw_prefix(project_id: str | None = None, conn=None) -> str:
    """``raw_schema`` as a query prefix, trailing dot included -- `mart_prefix`'s twin."""
    return f"{raw_schema(project_id, conn)}."


def staging_prefix(project_id: str | None = None, conn=None) -> str:
    """``staging_schema`` as a query prefix, trailing dot included."""
    return f"{staging_schema(project_id, conn)}."


def bigquery_staging_dataset(project_id: str, conn=None) -> str:
    """BigQuery staging dataset id for *project_id*.

    The third member of the family, composed the same way as its two siblings:
    legacy ``staging_<project_id>`` beside ``raw_<project_id>`` and
    ``marts_<project_id>``; ``org_<wslug>_staging`` under the flag, which is what
    the dbt macro writes.

    NOT EXERCISED BY ANY TEST OF THIS REPOSITORY, and that is stated rather than
    implied: there is no GCP dataset to point at here, and a mocked client would
    prove only that the name can be formatted.
    """
    if not org_schemas_enabled():
        return f"staging_{project_id}"
    resolved = resolve_org_schemas(project_id=project_id, conn=conn)
    if resolved is None:
        return f"staging_{project_id}"
    return f"org_{resolved.warehouse_slug}_staging"
