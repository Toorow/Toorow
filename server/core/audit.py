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
# AN ACTION IS DECLARED WHERE IT IS WRITTEN -- AD-42, 2026-08-12.
#
# WHAT THIS REPLACES AND WHY. This file held 96 `ACTION_` constants, and every
# feature that ever audited anything had to open it to append one: 43 edits from
# 29 distinct subjects since June, **34 of them adding nothing but a constant**.
# That is a crossroads -- a file on everybody's path -- and the cost is not only
# the conflicts. Measured on the live `app.audit_log` the day this changed:
#
#     96   constants declared here
#     64   action values actually written in production
#     29   of those 64 -- 45 % -- declared NOWHERE: the module types the string
#          in by hand, because the list was too far away to bother with
#     61   of the 96 declared and never written once
#
# So the central list had already failed at the one thing it was for. It was not
# a vocabulary; it was a list that 45 % of writers bypassed, with no check able
# to see the difference between an action and a typo.
#
# THE INVERSION. `declare_action` is called by the module that WRITES the action,
# beside the code that writes it. Nothing has to open this file to add one.
# `write_audit_row` then refuses an action nobody declared -- which is the
# guarantee the list was supposed to give and never did.
#
#     # in core/datastreams.py, next to the write
#     ACTION_DATASTREAM_CREATED = declare_action("datastream.created")
#
# WHAT STAYS DECLARED HERE. Only the actions written by more than one module --
# a shared vocabulary has no single owner, and putting it in one of the writers
# would make the other import from a module it has no other reason to know.
# ---------------------------------------------------------------------------

#: Every action any module has declared, in declaration order. Populated at
#: import time by the owners, never written by hand.
_DECLARED_ACTIONS: dict[str, str] = {}


class UndeclaredAuditAction(ValueError):
    """An action nobody declared. Names the gesture, not the constant."""

    def __init__(self, action: str) -> None:
        super().__init__(
            f"{action!r} is not a declared audit action. Declare it beside the "
            "code that writes it: `ACTION_X = declare_action(\"" + action + "\")`."
        )
        self.action = action


def declare_action(code: str) -> str:
    """Declare an audit action, and return it so the caller can name it.

    Called at IMPORT time by the module that writes it, which is why the check in
    `write_audit_row` is sound: a module that writes an action has been imported,
    so its declaration has run.

    Re-declaring the SAME code from the SAME module is allowed (a module reloaded
    under a test harness); re-declaring it from a different one is refused, which
    is the collision the central list could never catch by eye.
    """
    import inspect  # noqa: PLC0415 -- only on the declaration path, never on write

    frame = inspect.currentframe()
    owner = "?"
    if frame is not None and frame.f_back is not None:
        owner = frame.f_back.f_globals.get("__name__", "?")
    previous = _DECLARED_ACTIONS.get(code)
    if previous is not None and previous != owner:
        raise ValueError(
            f"audit action {code!r} is declared by {previous} and by {owner}. "
            "One action, one owner -- or declare it in `core.audit` if two "
            "modules genuinely write it."
        )
    _DECLARED_ACTIONS[code] = owner
    return code


def declared_actions() -> dict[str, str]:
    """Every declared action and the module that owns it. A reading, not a store."""
    return dict(_DECLARED_ACTIONS)


# ---------------------------------------------------------------------------
# Actions written by MORE THAN ONE module -- they stay here, and only they.
# Renaming any of these requires a DB migration. Add new ones; never rename.
# ---------------------------------------------------------------------------

ACTION_CONNECTION_CREATED = declare_action("connection.created")
ACTION_CONNECTION_REVOKED = declare_action("connection.revoked")
ACTION_PULL_TRIGGERED = declare_action("pull.triggered")
ACTION_CONTEXT_EVENT_CREATED = declare_action("context_event.created")
# Story 7.4 (AC4, AC7) -- a caller attempted to reach a resource outside its
# resolved project scope. Written on every rejected cross-scope access so that
# refused access is observable (FR12, AD-5, AD-8). The attempt is rejected with
# 404 (existence not disclosed) or 403; the audit row records what was refused.
ACTION_CROSS_SCOPE_ATTEMPT = declare_action("access_denied")
# Story 8.2 (AC5, AC6) -- datastream lifecycle (create / update / delete / run).
ACTION_DATASTREAM_CREATED = declare_action("datastream.created")
ACTION_DATASTREAM_UPDATED = declare_action("datastream.updated")
ACTION_DATASTREAM_RUN = declare_action("datastream.run")
# Story 12.5 -- atomic candidate publication (candidate registry + pointer swap).
ACTION_DATASTREAM_PUBLISHED = declare_action("datastream.published")
ACTION_DATASTREAM_PUBLICATION_FAILED = declare_action("datastream.publication.failed")

# AD-43 : deux ecrivains depuis que l effacement d organisation a quitte
# `admin_api` -- la porte de partage de dataset, et `org_lifecycle`.
ACTION_DATASET_ACCESS_REVOKED = declare_action("dataset_access.revoked")
ACTION_ORG_SCHEMAS_DROPPED = declare_action("org_schemas_dropped")
# Story 13.5 volet (b) -- partage tokenise des snapshots rendus (O1, AD-20 ratifie).


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

    AN UNDECLARED ACTION IS REFUSED HERE (AD-42). Sound because the module that
    writes an action is the module that declares it, and it has been imported by
    the time it writes. This is the check the 96-constant list never provided:
    45 % of the values in the live journal were declared nowhere, and nothing
    could tell an action from a typo. `logger.warning` rather than a raise would
    be a guard nobody reads -- but neither may an audit break the write it
    records, so the caller-facing wrapper below still swallows it.
    """
    if action not in _DECLARED_ACTIONS:
        raise UndeclaredAuditAction(action)

    row_id = _mint_audit_id()
    # `default=str` : un metadata qui porte un `datetime` faisait lever
    # `TypeError: Object of type datetime is not JSON serializable` ICI --
    # apres que l'ecriture metier soit passee. L'appelant recevait
    # `db_error / Context Hub is temporarily unavailable`, reessayait, et
    # dupliquait. Mesure du 2026-08-03 en creant un Business Domain par l'API.
    # Un audit ne doit jamais faire echouer ce qu'il enregistre.
    metadata_json = (
        json.dumps(metadata, default=str) if metadata is not None else None
    )
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
    project_id: str,
    start: str | None = None,
    end: str | None = None,
    action: str | None = None,
    connection_ref: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Query app.audit_log with optional filters. Returns newest-first, max `limit`.

    Args:
        project_id:    Required exact project scope. Legacy rows without this
                       persisted scope are deliberately excluded.
        start:          ISO-8601 date/datetime string (inclusive lower bound on
                        created_at). None = no lower bound.
        end:            ISO-8601 date/datetime string (inclusive upper bound on
                        created_at). None = no upper bound.
        action:         Exact match on action code (e.g. 'connection.created').
        connection_ref: Exact match on connection_ref ULID.
        limit:          Maximum rows to return (default 500, hard cap).

    Returns:
        List of dicts with keys: id, identity, action, provider_account,
        connection_ref, outcome, trace_id, resource, created_at.

    Raises:
        Exception: propagates DB errors to the API layer (unlike write_audit_row).
    """
    # CF-2.5-limit-query-param: enforce the documented hard cap regardless of
    # what the caller passes (protects the endpoint from limit=10**9).
    limit = max(1, min(int(limit), 500))

    if not project_id:
        raise ValueError("project_id is required")

    # Project scope is mandatory and deliberately the first predicate. Rows
    # written before project-scoped audit metadata existed are excluded.
    clauses: list[str] = ["metadata->>'project_id' = %s"]
    params: list[Any] = [project_id]

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
               metadata->>'outcome' AS outcome,
               metadata->>'trace_id' AS trace_id,
               COALESCE(metadata->'resource_path', metadata->'resource') AS resource,
               created_at
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
                    elif col == "resource" and val is not None:
                        record[col] = (
                            val if isinstance(val, (dict, list)) else json.loads(val)
                        )
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
    "outcome",
    "trace_id",
    "resource",
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
    connection_ref, outcome, trace_id, resource, created_at. Cell values are formula-injection
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
                _csv_safe(row.get("outcome", "")),
                _csv_safe(row.get("trace_id", "")),
                _csv_safe(
                    json.dumps(_sanitize_metadata(row["resource"]))
                    if row.get("resource") is not None
                    else ""
                ),
                row.get("created_at", ""),
            ]
        )
    return buf.getvalue()
