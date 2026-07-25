"""toorow -- context-event helpers (Story 4.3; extracted 5.1/AI-27).

Extracted verbatim from ``core.main`` in Story 5.1 (AI-27 decomposition; Story 4.3
AC7 anticipated this move with a "TODO: extract _fetch_context_events" note). Pure
behaviour-preserving move: ``main.py`` re-exports ``_fetch_context_events`` and
``_validate_event_input`` for backward compatibility with existing imports/tests.

AD-12 / NFR6: the warehouse (governed ``mirror.context_events``) is the single
analytical read path; this module never reads Postgres directly for the fetch.

AD-2: source-agnostic -- no module-specific strings.
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def persist_context_event(
    *,
    project_id: str,
    event_date: str,
    type: str,
    label: str,
    description: str,
    created_by: str,
    platform: str | None = None,
    value: float | None = None,
    source: str = "manual",
) -> str:
    """Validate + persist one context event to app.context_events; return its id.

    Extracted from ``core.main.add_context_event`` in Story 5.1 (AI-27). Mints a
    prefixed ULID (``evt_``), inserts the row, and writes the audit row (never
    raises -- AC6 in audit.py). Raises ``fastmcp.exceptions.ToolError`` on invalid
    input or a DB insert error, with a French-first message (UX-DR10).

    AD-2: source-agnostic -- no module-specific strings.
    """
    from ulid import ULID  # noqa: PLC0415

    from core.audit import ACTION_CONTEXT_EVENT_CREATED, write_audit_row  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    # Validate inputs (shared with REST endpoint)
    validate_event_input(label, event_date)
    # Epic 31: enforce the canonical event vocabulary for connector events.
    validate_event_type(type, source)

    # Mint prefixed ULID (AD-14 / ARCHITECTURE-SPINE §IDs: evt_)
    evt_id = f"evt_{ULID()}"

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Epic 31: platform/value/source are additive nullable columns
                # (migration 055). platform = level 1 of platform>type>label,
                # value = optional MMM regressor magnitude, source = manual|<connector>.
                cur.execute(
                    """
                    INSERT INTO app.context_events
                        (id, project_id, event_date, type, label, description,
                         created_by, platform, value, source)
                    VALUES (%s, %s, %s::date, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        evt_id, project_id, event_date, type, label, description or None,
                        created_by, platform, value, source,
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.error("add_context_event: db_insert_error: %s", exc)
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Erreur base de données : {exc}"})
        ) from exc

    # Write audit row (never raises — AC6 in audit.py)
    write_audit_row(
        identity=created_by,
        action=ACTION_CONTEXT_EVENT_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "event_id": evt_id,
            "project_id": project_id,
            "type": type,
            "label": label,
        },
    )
    return evt_id


def delete_connector_events_in_window(
    *,
    project_id: str,
    source: str,
    event_type: str,
    date_from: str,
    date_to: str,
) -> int:
    """Delete connector-emitted context events in a window; return rows deleted (Epic 31.3).

    Idempotence primitive for connector event pulls (H1). A connector that
    re-pulls the same window daily would otherwise INSERT duplicate markers on
    every run (persist_context_event inserts unconditionally), doubling the MMM
    regressor and breaking the honesty invariant. The fix is a delete-by-source-
    window BEFORE the re-insert: a re-pull of the same (project_id, source,
    event_type, [date_from, date_to]) becomes idempotent WITHOUT a new migration
    and WITHOUT a DB-level unique constraint.

    Scoped defensively (AD-8 project-scoped, additive):
      * project_id   -- never touches another project's events.
      * source       -- never deletes MANUAL events (source='manual') nor another
                        connector's events; only rows this connector wrote.
      * event_type   -- only the type this profile re-emits (a mixed connector
                        with several event profiles clears each independently).
      * [date_from, date_to] -- only the re-pulled window.

    No-op (returns 0) when the DB is unreachable is NOT swallowed here: a delete
    failure must surface (unlike a best-effort read) so a caller never re-inserts
    on top of stale rows. Raises fastmcp.exceptions.ToolError on a DB error.
    """
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM app.context_events
                    WHERE project_id = %s
                      AND source = %s
                      AND type = %s
                      AND event_date BETWEEN %s::date AND %s::date
                    """,
                    (project_id, source, event_type, date_from, date_to),
                )
                deleted = cur.rowcount if cur.rowcount is not None else 0
            conn.commit()
    except Exception as exc:
        logger.error("delete_connector_events_in_window: db_error: %s", exc)
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Erreur base de données : {exc}"})
        ) from exc

    logger.info(
        "delete_connector_events_in_window: deleted=%d project_id=%s source=%s "
        "type=%s window=[%s,%s]",
        deleted, project_id, source, event_type, date_from, date_to,
    )
    return deleted


# ISO-8601 date pattern (YYYY-MM-DD) -- shared with main._ISO_DATE_RE semantics.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_landing(profile: dict, module_kind: str | None) -> str:
    """Return where a profile lands: 'fact_daily_kpi' | 'context_events' (Epic 31).

    Per-profile 'landing' wins; absent, it is DERIVED from module_kind
    (module_kind=='context' -> context_events, else fact_daily_kpi). This is the
    single routing decision -- module_kind no longer decides on its own, so a
    connector may mix kpi and event profiles. Pure function (no I/O).
    """
    landing = (profile or {}).get("landing")
    if landing in ("fact_daily_kpi", "context_events"):
        return landing
    return "context_events" if module_kind == "context" else "fact_daily_kpi"


def validate_event_type(event_type: str, source: str) -> None:
    """Enforce the canonical event vocabulary for CONNECTOR events (Epic 31).

    A connector-emitted event (source != 'manual') MUST carry an event_type known
    to dim_event_type.csv -- an unknown type is a drift signal (typed refusal),
    NEVER a silent drop, exactly like an unknown metric. Manual events
    (source == 'manual') keep free-form types (backward compatibility with the
    console; only a debug log on an unknown type).

    Raises fastmcp.exceptions.ToolError on connector drift.
    """
    from core.report_dictionary import is_known_event_type  # noqa: PLC0415

    if is_known_event_type(event_type):
        return
    if source == "manual":
        logger.debug("context_event: manual event with non-canonical type %r", event_type)
        return
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    raise ToolError(
        json.dumps(
            {
                "code": "unknown_event_type",
                "message": (
                    f"event_type inconnu du dictionnaire canonique (dim_event_type.csv) : "
                    f"{event_type!r} (source={source!r}) -- drift, jamais un drop silencieux."
                ),
            }
        )
    )


def validate_event_input(label: str, event_date: str) -> None:
    """Validate shared inputs for context events (MCP tool + REST endpoint).

    Raises:
        fastmcp.exceptions.ToolError: with French error message on failure.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    if len(label) > 120:
        raise ToolError(
            json.dumps({"code": "invalid_input", "message": "label trop long (max 120 caractères)"})
        )
    if not _ISO_DATE_RE.match(event_date):
        raise ToolError(
            json.dumps(
                {
                    "code": "invalid_input",
                    "message": f"event_date invalide (format attendu YYYY-MM-DD) : {event_date!r}",
                }
            )
        )


# Columns exposed by the analytical read path, in select order.
# Epic 31 (migration 055) adds platform/value/source so the overlay (annotation
# markers) AND the MMM feature-builder see the full event identity
# (platform>type>label) + the optional regressor magnitude (value) + provenance
# (source). These are additive/nullable: legacy rows carry platform/value NULL and
# source='manual', and a pre-055 DuckDB mirror simply lacks the columns (handled
# below by intersecting with the table's actual columns -- backward compatible).
_BASE_EVENT_COLS = ("id", "project_id", "event_date", "type", "label")
_MMM_EVENT_COLS = ("platform", "value", "source")


def fetch_context_events(project_id: str, start: str, end: str) -> list[dict]:
    """Fetch context events from mirror.context_events (DuckDB warehouse) for the given project.

    AD-12 / NFR6: the warehouse is the single analytical read path. This function
    reads from the governed mirror schema (mirror.context_events) populated by
    mirror_sync.py (Story 4.4, AC6). It does NOT read Postgres directly.

    Epic 31.2: also exposes ``platform``, ``value`` and ``source`` (migration 055,
    mirrored by mirror_sync's SELECT *) so the same single read path feeds BOTH the
    annotation overlay AND the MMM feature-builder (§10 of epic-31). The columns are
    selected defensively -- only those actually present in the mirror table are read
    -- so a pre-055 mirror (or a test fixture with the legacy 5-column shape) still
    returns the base columns without error (AD-9: no black box, graceful).

    Returns rows WHERE project_id = ? AND event_date BETWEEN ? AND ?
    ordered by event_date ASC.

    Returns [] if the mirror has not been synced yet (table absent in DuckDB) — logs
    a warning so the absence is visible in logs without crashing the report.

    AD-2: no module-specific strings — purely generic project-scoped parameters.
    """
    import duckdb  # noqa: PLC0415

    db_path = os.environ.get("TOOROW_DUCKDB_PATH", "")
    if not db_path or not os.path.exists(db_path):
        logger.debug(
            "context_events_mirror: DuckDB not available (path=%r) -- returning []", db_path
        )
        return []

    try:
        con = duckdb.connect(db_path, read_only=True)
        try:
            # Resolve which MMM columns actually exist in the mirror table so a
            # pre-055 mirror does not raise a BinderException on an absent column.
            available = {
                r[0]
                for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'mirror' AND table_name = 'context_events'"
                ).fetchall()
            }
            select_cols = list(_BASE_EVENT_COLS) + [
                c for c in _MMM_EVENT_COLS if c in available
            ]
            col_sql = ", ".join(select_cols)
            rel = con.execute(
                f"""
                SELECT {col_sql}
                FROM mirror.context_events
                WHERE project_id = ?
                  AND event_date BETWEEN ? AND ?
                ORDER BY event_date ASC
                """,  # noqa: S608 -- col names are from a fixed allowlist, not user input
                [project_id, start, end],
            )
            cols = [d[0] for d in rel.description]
            events: list[dict] = []
            for row in rel.fetchall():
                record: dict = {}
                for col, val in zip(cols, row):
                    if col == "event_date" and val is not None:
                        record[col] = str(val)
                    elif col == "value" and val is not None:
                        # NUMERIC -> float for JSON/MMM; NULL stays None (unit pulse, AD-9).
                        record[col] = float(val)
                    else:
                        record[col] = val
                events.append(record)
            return events
        finally:
            con.close()
    except duckdb.CatalogException:
        # Table does not exist yet — mirror has not been synced.
        logger.warning(
            "mirror.context_events not yet synced -- context missing "
            "(run mirror_sync.sync_tables() or set SYNC_ENABLED=true)"
        )
        return []
    except Exception as exc:
        logger.warning("context_events_mirror_fetch_failed: project_id=%s: %s", project_id, exc)
        return []
