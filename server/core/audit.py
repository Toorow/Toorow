"""toorow -- On-Behalf-Of audit trail writer and query API (Story 2.6).

Records every data-authorization and pull event attributed to a human identity,
satisfying FR12, AD-8, and AD-14.

Design decisions (recorded per T1.4 / Dev Agent Record):
  - DB driver: psycopg (v3, sync) -- chosen over asyncpg because:
      1. Neither psycopg nor asyncpg was in the venv before this story.
      2. asyncpg is async-only; using it from sync handlers requires asyncio.run()
         which raises RuntimeError when called from inside an already-running event
         loop (nested-loop risk flagged in the pre-story review).
      3. psycopg[binary] is the sync-first driver; no asyncio.run() wrapper needed.
  - ULID: python-ulid (already in venv as python-ulid 3.1.0, used by connection_ref).
  - No module-level DB connections (stateless module, like nango_client.py).
  - Env vars read at call time, not module level.

Callers:
  - Story 2.4 connection endpoints (create / revoke): call write_audit_row with
    ACTION_CONNECTION_CREATED / ACTION_CONNECTION_REVOKED after DB write succeeds.
    TODO: wire up from Story 2.4 create-connection and revoke-connection handlers.
  - Story 2.7 pull trigger: call write_audit_row with ACTION_PULL_TRIGGERED.
    TODO: wire up from Story 2.7 pull trigger handler.

AD-3: this module NEVER logs or stores token values. Identity (a subject string)
is logged as an event attribute only -- never the token itself.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from typing import Any

from ulid import ULID  # package python-ulid, module name is ulid

logger = logging.getLogger(__name__)

# psycopg is imported at module level so unit tests can patch `core.audit.psycopg`.
# It is listed as a dependency in server/pyproject.toml (psycopg[binary]>=3.1).
# If psycopg is not installed (should not happen in production), a clear error
# will surface at import time rather than at first DB call.
try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Environment variable name
# ---------------------------------------------------------------------------

PLATFORM_DB_URL_ENV = "PLATFORM_DB_URL"

# ---------------------------------------------------------------------------
# Action code constants (public API -- treat as stable once rows exist).
# Renaming any of these requires a DB migration. Add new ones; never rename.
# ---------------------------------------------------------------------------

ACTION_CONNECTION_CREATED = "connection.created"
ACTION_CONNECTION_REVOKED = "connection.revoked"
ACTION_PULL_TRIGGERED = "pull.triggered"
ACTION_PULL_COMPLETED = "pull.completed"
ACTION_PULL_FAILED = "pull.failed"
ACTION_CONTEXT_EVENT_CREATED = "context_event.created"
# Story 5.3 (AC5, T5.6) -- business threshold alert definition lifecycle
ACTION_ALERT_DEF_CREATED = "alert_def.created"
ACTION_ALERT_DEF_UPDATED = "alert_def.updated"
ACTION_ALERT_DEF_DELETED = "alert_def.deleted"
# Story 5.5 (AC3) -- user feedback submitted via submit_feedback tool
ACTION_FEEDBACK_SUBMITTED = "feedback.submitted"
# Story 6.5 (AC3, AC4) -- notebook lifecycle
ACTION_NOTEBOOK_CREATED = "notebook_created"
ACTION_NOTEBOOK_RUN = "notebook_run"
ACTION_NOTEBOOK_DELETED = "notebook_deleted"
# Story 21.1 (AC4) -- organization lifecycle (Epic 21, FR37/CAP-25)
ACTION_ORG_CREATED = "org_created"
ACTION_ORG_UPDATED = "org_updated"
ACTION_ORG_MEMBER_ADDED = "org_member_added"
# Story 21.5 follow-up -- org membership management (remove / change role|status)
ACTION_ORG_MEMBER_REMOVED = "org_member_removed"
ACTION_ORG_MEMBER_UPDATED = "org_member_updated"
# Story 21.3 -- cross-org credential account sharing (per-account grants)
ACTION_ACCOUNT_EXPOSED = "credential_account_exposed"
ACTION_ACCOUNT_GRANT_REVOKED = "credential_account_grant_revoked"
# Story 24.5 -- dataset marts access grants (BigQuery IAM, per-org)
ACTION_DATASET_ACCESS_GRANTED = "dataset_access.granted"
ACTION_DATASET_ACCESS_REVOKED = "dataset_access.revoked"
# Story 21.4 -- flux (app.datastreams) linked to / unlinked from a project (M:N)
ACTION_FLUX_LINKED = "flux_linked_to_project"
ACTION_FLUX_UNLINKED = "flux_unlinked_from_project"
# Story 7.1 (AC3, AC4) -- project lifecycle
ACTION_PROJECT_CREATED = "project_created"
ACTION_PROJECT_ARCHIVED = "project_archived"
ACTION_PROJECT_GEOGRAPHIC_POSTURE_UPDATED = "project.geographic_posture.updated"
ACTION_PROJECT_GEOGRAPHIC_CHANGE_CONFIRMED = "project.geographic_change.confirmed"
ACTION_CONNECTION_REVOKED_ON_ARCHIVE = "connection_revoked_on_archive"
# Story 7.3 (AC3, AC4, AC5, AC6) -- per-tenant key lifecycle + connection revocation
ACTION_CONNECTION_REVOKED = "connection.revoked"
ACTION_KEY_CREATED = "key_created"
ACTION_KEY_ROTATED = "key_rotated"
ACTION_KEY_DELETED = "key_deleted"
# Story 7.4 (AC4, AC7) -- a caller attempted to reach a resource outside its
# resolved project scope. Written on every rejected cross-scope access so that
# refused access is observable (FR12, AD-5, AD-8). The attempt is rejected with
# 404 (existence not disclosed) or 403; the audit row records what was refused.
ACTION_CROSS_SCOPE_ATTEMPT = "access_denied"
# Story 8.2 (AC5, AC6) -- datastream lifecycle (create / update / delete / run).
ACTION_DATASTREAM_CREATED = "datastream.created"
ACTION_DATASTREAM_UPDATED = "datastream.updated"
ACTION_DATASTREAM_DELETED = "datastream.deleted"
ACTION_DATASTREAM_RUN = "datastream.run"
ACTION_DATASTREAM_INTENT_VERSIONED = "datastream.intent.versioned"
ACTION_DATASTREAM_MAPPING_VERSIONED = "datastream.mapping.versioned"
# Story 12.5 -- atomic candidate publication (candidate registry + pointer swap).
ACTION_DATASTREAM_PUBLISHED = "datastream.published"
ACTION_DATASTREAM_PUBLICATION_FAILED = "datastream.publication.failed"
ACTION_DATASTREAM_EXECUTION_STATE_CHANGED = "datastream.execution.state_changed"
# Story 12.7 -- register an existing BigQuery object read-only (no ownership).
# The observation of an external object + the virtual pull commit it mints.
ACTION_EXTERNAL_BQ_OBSERVED = "datastream.external_bq.observed"
ACTION_EXTERNAL_BQ_OBSERVATION_FAILED = "datastream.external_bq.observation.failed"
# Story 11.1 -- context topic and procedure lifecycle
ACTION_CONTEXT_TOPIC_CREATED = "context_topic.created"
ACTION_CONTEXT_TOPIC_UPDATED = "context_topic.updated"
ACTION_CONTEXT_TOPIC_ARCHIVED = "context_topic.archived"
ACTION_PROCEDURE_CREATED = "procedure.created"
ACTION_PROCEDURE_UPDATED = "procedure.updated"
ACTION_PROCEDURE_ARCHIVED = "procedure.archived"
# Story 11.4 -- context graph edge lifecycle
ACTION_GRAPH_EDGE_CREATED = "context_graph.edge.created"
ACTION_GRAPH_EDGE_DELETED = "context_graph.edge.deleted"

# Story 22.1: media plans (FR38 / CAP-26).
ACTION_MEDIA_PLAN_CREATED = "media_plan.created"
ACTION_MEDIA_PLAN_VERSION_CREATED = "media_plan.version.created"
ACTION_MEDIA_PLAN_VERSION_PUBLISHED = "media_plan.version.published"
# Story 22.3: N:M line<->campaign mapping, splits & orphan status (FR38 / CAP-26).
ACTION_MEDIA_PLAN_MAPPING_SET = "media_plan.mapping.set"
ACTION_MEDIA_PLAN_MAPPING_ORPHANED = "media_plan.mapping.orphaned"
# Story 22.3 review F-1/F-2: après un orphelinage/réactivation, la répartition des
# mappings actifs restants d'une campagne est recalculée en équiréparti (défaut
# ajustable) pour préserver SUM(split_weight)=1.0. Écrit une ligne d'audit par
# (plan, connector, campaign_ref) rééquilibrée pour rendre le changement traçable (AD-9).
ACTION_MEDIA_PLAN_MAPPING_REBALANCED = "media_plan.mapping.rebalanced"
# Story 22.2: Excel multi-sheet versioned import (FR38 / CAP-26).
ACTION_MEDIA_PLAN_IMPORT_CONTRACT_SET = "media_plan.import_contract.set"
ACTION_MEDIA_PLAN_IMPORTED = "media_plan.imported"
# Story 24.2 (AC5, T7) -- org data-plane warehouse schema lifecycle (Epic 24 RGPD).
ACTION_ORG_SCHEMAS_PROVISIONED = "org_schemas_provisioned"
ACTION_ORG_SCHEMAS_DROPPED = "org_schemas_dropped"
ACTION_ORG_DELETED = "org_deleted"
# Story 13.5 volet (b) -- partage tokenise des snapshots rendus (O1, AD-20 ratifie).
ACTION_SNAPSHOT_SHARED = "render_snapshot.shared"
ACTION_SNAPSHOT_SHARE_REVOKED = "render_snapshot.share_revoked"
ACTION_SNAPSHOT_SHARE_ACCESSED = "render_snapshot.share_accessed"
# Story 12.9 -- CSV/Excel governed import (FR27, FR30, FR31, FR32, FR35).
ACTION_MANAGED_FEED_IMPORT_STARTED = "managed_feed.import.started"
ACTION_MANAGED_FEED_IMPORT_COMPLETED = "managed_feed.import.completed"
ACTION_MANAGED_FEED_IMPORT_FAILED = "managed_feed.import.failed"
ACTION_MANAGED_FEED_IMPORT_BLOCKED = "managed_feed.import.blocked"
ACTION_CSV_EXCEL_CONTRACT_VERSIONED = "managed_feed.import_contract.versioned"
# Story 38.2 (AC1) -- connector installation lifecycle (platform-level).
# Convention: "<domain>.<noun>.<verb>" lower-snake.
ACTION_CONNECTOR_INSTALL_APPLIED = "connector.install.applied"
ACTION_CONNECTOR_STATE_CHANGED = "connector.state.changed"
ACTION_CONNECTOR_INSTALL_DENIED = "connector.install.denied"
# Story 38.3 (AC1) -- connector domain and adapter-route configuration (platform-level).
ACTION_CONNECTOR_DOMAIN_CONFIGURED = "connector.domain.configured"
ACTION_CONNECTOR_DOMAIN_CONFIG_DENIED = "connector.domain.config.denied"
# Story 38.4 (AC1, AC5) -- connector verification runs and synthetic delivery (platform-level).
# Convention: "<domain>.<noun>.<verb>" lower-snake. No provider vocabulary (AD-2).
ACTION_CONNECTOR_VERIFICATION_RAN = "connector.verification.ran"
ACTION_CONNECTOR_VERIFICATION_DENIED = "connector.verification.denied"
# Story 38.5 (AC1, AC2) -- connector activation/deactivation lifecycle (org-scoped).
# Convention: "<domain>.<noun>.<verb>" lower-snake. No provider vocabulary (AD-2).
ACTION_CONNECTOR_ACTIVATION_ACTIVATED = "connector.activation.activated"
ACTION_CONNECTOR_ACTIVATION_DEACTIVATED = "connector.activation.deactivated"
ACTION_CONNECTOR_ACTIVATION_DENIED = "connector.activation.denied"
# Story 38.6 (AC1, AC5) -- inbound managed-feed Datastream creation from template.
# Convention: "<domain>.<noun>.<verb>" lower-snake. No provider vocabulary (AD-2).
ACTION_INBOUND_DATASTREAM_CREATED = "inbound.datastream.created"
# Story 38.7 (AC1, AC2) -- inbound delivery credential lifecycle (issue/rotate/revoke).
# Convention: "<domain>.<noun>.<verb>" lower-snake. No provider vocabulary (AD-2).
# Note: the raw token is NEVER in any audit payload (E38-NFR03, show-once invariant).
ACTION_INBOUND_CREDENTIAL_ISSUED = "inbound.credential.issued"
ACTION_INBOUND_CREDENTIAL_ROTATED = "inbound.credential.rotated"
ACTION_INBOUND_CREDENTIAL_REVOKED = "inbound.credential.revoked"
ACTION_INBOUND_CREDENTIAL_DENIED = "inbound.credential.denied"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _db_url() -> str:
    """Return the Postgres DSN, reading env var at call time (stateless pattern).

    Priority:
      1. PLATFORM_DB_URL env var (explicit override for CI / alternate envs).
      2. Default: built from the platform-db service credentials from
         infra/nango/docker-compose.yml (user=connector, db=connector,
         host=localhost:5432, password from PLATFORM_DB_PASSWORD or literal dev default).
    """
    override = os.environ.get(PLATFORM_DB_URL_ENV, "").strip()
    if override:
        return override
    password = os.environ.get("PLATFORM_DB_PASSWORD", "connector_dev_only")
    return f"postgresql://connector:{password}@localhost:5432/connector"


def _mint_audit_id() -> str:
    """Mint a new ULID with 'audit_' prefix."""
    return f"audit_{ULID()}"


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------


def insert_audit_row(
    conn,
    *,
    identity: str,
    action: str,
    provider_account: str,
    connection_ref: str,
    metadata: dict | None = None,
) -> str:
    """Insert one audit row on an existing transaction and propagate failures.

    Transactional domain mutations use this seam so their state change and audit
    evidence either commit together or roll back together. The legacy public
    wrapper below remains best-effort for callers that do not own a transaction.
    """

    row_id = _mint_audit_id()
    metadata_json = json.dumps(metadata) if metadata is not None else None
    connection_ref_value = connection_ref if connection_ref else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.audit_log
                (id, identity, action, provider_account, connection_ref,
                 metadata, created_at)
            VALUES
                (%s, %s, %s, %s, %s, %s::jsonb, now())
            """,
            (
                row_id,
                identity,
                action,
                provider_account,
                connection_ref_value,
                metadata_json,
            ),
        )
    return row_id


def write_audit_row(
    identity: str,
    action: str,
    provider_account: str,
    connection_ref: str,
    metadata: dict | None = None,
) -> None:
    """Write one audit row to app.audit_log. Never raises -- logs on failure.

    This is the single shared write path (AC2). All callers (connection create,
    connection revoke, pull trigger) MUST use this function -- no inline SQL
    in callers.

    Args:
        identity:        Subject string from access token (pass "anonymous" in
                         disabled mode). AD-3: do NOT pass token values here.
        action:          One of the ACTION_* constants in this module.
        provider_account: Nango provider_config_key (the integration key from Nango).
        connection_ref:  The conn_ ULID of the connection in app.connection_ref.
        metadata:        Optional free-form dict stored as JSONB. May be None.

    Returns:
        None (always -- AC6: audit failures never propagate to callers).
    """
    try:
        with psycopg.connect(_db_url()) as conn:
            insert_audit_row(
                conn,
                identity=identity,
                action=action,
                provider_account=provider_account,
                connection_ref=connection_ref,
                metadata=metadata,
            )
            conn.commit()

    except Exception as exc:
        # AC6: audit write failure must NEVER block the caller's tool call.
        # Log at WARNING so operators see it, but do not re-raise.
        logger.warning("audit_write_failed: %s", exc)


# ---------------------------------------------------------------------------
# Public query API
# ---------------------------------------------------------------------------


def query_audit_log(
    start: str | None = None,
    end: str | None = None,
    action: str | None = None,
    connection_ref: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Query app.audit_log with optional filters. Returns newest-first, max `limit`.

    Args:
        start:          ISO-8601 date/datetime string (inclusive lower bound on
                        created_at). None = no lower bound.
        end:            ISO-8601 date/datetime string (inclusive upper bound on
                        created_at). None = no upper bound.
        action:         Exact match on action code (e.g. 'connection.created').
        connection_ref: Exact match on connection_ref ULID.
        limit:          Maximum rows to return (default 500, hard cap).

    Returns:
        List of dicts with keys: id, identity, action, provider_account,
        connection_ref, metadata, created_at (ISO-8601 string).

    Raises:
        Exception: propagates DB errors to the API layer (unlike write_audit_row).
    """
    # CF-2.5-limit-query-param: enforce the documented hard cap regardless of
    # what the caller passes (protects the endpoint from limit=10**9).
    limit = max(1, min(int(limit), 500))

    clauses: list[str] = []
    params: list[Any] = []

    if start is not None:
        clauses.append("created_at >= %s::timestamptz")
        params.append(start)

    if end is not None:
        clauses.append("created_at <= %s::timestamptz")
        params.append(end)

    if action is not None:
        clauses.append("action = %s")
        params.append(action)

    if connection_ref is not None:
        clauses.append("connection_ref = %s")
        params.append(connection_ref)

    where_clause = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    sql = f"""
        SELECT id, identity, action, provider_account, connection_ref,
               metadata, created_at
        FROM app.audit_log
        {where_clause}
        ORDER BY created_at DESC
        LIMIT %s
    """

    rows: list[dict[str, Any]] = []
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                record: dict[str, Any] = {}
                for col, val in zip(cols, row):
                    if col == "created_at" and val is not None:
                        record[col] = val.isoformat()
                    elif col == "metadata" and val is not None:
                        # psycopg3 returns JSONB as dict already; normalise
                        record[col] = val if isinstance(val, dict) else json.loads(val)
                    else:
                        record[col] = val
                rows.append(record)

    return rows


# ---------------------------------------------------------------------------
# CSV serialisation helper (used by the /api/audit?format=csv endpoint)
# ---------------------------------------------------------------------------

AUDIT_CSV_COLUMNS = [
    "id",
    "identity",
    "action",
    "provider_account",
    "connection_ref",
    "metadata",
    "created_at",
]


def _csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection (review-2-6 F-04, OWASP).

    identity comes from an external IdP: a sub claim starting with = + - @
    would execute as a formula when the export opens in Excel/Sheets.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _sanitize_metadata(obj: Any) -> Any:
    """Recursively sanitise every leaf string in a metadata dict/list.

    Applies _csv_safe to each string leaf so that nested values like
    {"label": "=HYPERLINK(...)"} cannot inject formulas via json.dumps output.
    Non-string leaves (int, float, bool, None) are preserved as-is.
    """
    if isinstance(obj, dict):
        return {k: _sanitize_metadata(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_metadata(item) for item in obj]
    if isinstance(obj, str):
        return _csv_safe(obj)
    return obj


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    """Serialise audit rows to CSV string (header + data rows).

    Uses Python's stdlib csv.writer -- no third-party CSV library.
    Columns in canonical order: id, identity, action, provider_account,
    connection_ref, metadata, created_at. Cell values are formula-injection
    neutralised via _csv_safe.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(AUDIT_CSV_COLUMNS)
    for row in rows:
        writer.writerow(
            [
                _csv_safe(row.get("id", "")),
                _csv_safe(row.get("identity", "")),
                _csv_safe(row.get("action", "")),
                _csv_safe(row.get("provider_account", "")),
                _csv_safe(row.get("connection_ref", "")),
                _csv_safe(
                    json.dumps(_sanitize_metadata(row["metadata"]))
                    if row.get("metadata") is not None
                    else ""
                ),
                row.get("created_at", ""),
            ]
        )
    return buf.getvalue()
