"""toorow -- Datastream CRUD and backfill (Story 8.2, Epic 8).

Provides:
  list_datastreams(project_id, conn)         -> list[dict]
  get_datastream(ds_id, project_id, conn)    -> dict | None
  create_datastream(data, project_id, created_by, conn) -> dict
  update_datastream(ds_id, project_id, patch, conn)     -> dict | None
  enable_disable_datastream(ds_id, project_id, enabled, conn) -> dict | None
  delete_datastream(ds_id, project_id, conn) -> bool
  get_datastream_summaries(project_id, conn) -> list[dict]
  backfill_datastreams()                     -> dict

Design decisions:
  - AD-2: module_name is DATA (from manifests / callers), never hardcoded here.
  - AD-5: every SELECT carries WHERE project_id = %s; cross-project access returns None.
  - AD-7: datastream IDs are minted here with ds_<ULID> prefix.
  - delete_datastream: soft-archive (enabled=false) when pull_jobs reference the
    datastream; hard DELETE (mappings -> datastream) when unreferenced.
    Consistent with append-only patterns (AD-7): live pulls are preserved.
  - backfill_datastreams: reads manifests from core.main._loaded_modules (same
    pattern as _get_manifest_for_provider in queue.py) so no AD-2 violation.
    Idempotent throughout (INSERT ... ON CONFLICT DO NOTHING).

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ULID helpers
# ---------------------------------------------------------------------------


def _mint_ds_id() -> str:
    """Mint a new datastream ID with 'ds_' prefix."""
    from ulid import ULID  # noqa: PLC0415

    return f"ds_{ULID()}"


# ---------------------------------------------------------------------------
# Timestamp serialisation helper
# ---------------------------------------------------------------------------

_TS_COLS_DS = {
    "created_at",
    "updated_at",
    "archived_at",
    "next_run_at",
    "last_pull_completed_at",
    "published_at",
}


def _row_to_dict(cols: list[str], row: tuple) -> dict:
    """Serialise a datastreams row; ISO-format timestamps, str dates."""
    out: dict = {}
    for col, val in zip(cols, row):
        if col in _TS_COLS_DS and val is not None:
            out[col] = val.isoformat()
        else:
            out[col] = val
    if "id" in out and "project_id" in out and "name" in out:
        out.setdefault("current_plan_version_id", None)
        current_plan = out["current_plan_version_id"]
        versioned = current_plan is not None
        raw_kind = out.get("source_kind")
        out["versioned"] = versioned
        out["source_kind"] = raw_kind or "connector_pull"
        out["writer_kind"] = out.get("writer_kind") or "toorow"
        out["destination_policy"] = out.get("destination_policy") or "managed_raw"
        payload = out.get("intent_payload")
        if isinstance(payload, dict):
            out["cadence_mode"] = payload.get("schedule", {}).get("mode")
        else:
            _sm = out.get("schedule_mode")
            out["cadence_mode"] = (
                "daily" if _sm == "nightly" else "hourly" if _sm == "hourly" else "manual"
            )
        if "validation_state" not in out or out.get("validation_state") is None:
            if not versioned:
                out["validation_state"] = "legacy"
            else:
                out["validation_state"] = "executable" if out.get("executable") else "blocked"
    return out


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_datastreams(project_id: str, conn) -> list[dict]:
    """Return all datastreams for *project_id*, ordered by name.

    AD-5: always filtered by project_id.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ds.id, ds.project_id, ds.name, ds.module_name,
                   ds.connection_ref_id, ds.report_profile_id, ds.enabled,
                   ds.schedule_mode, ds.refetch_days, ds.date_window_days,
                   ds.config, ds.created_by, ds.created_at, ds.updated_at,
                   ds.source_kind, ds.data_role, ds.current_plan_version_id,
                   ds.archived_at,
                   ds.archived_by, pv.version_number AS current_plan_version,
                   pv.writer_kind, pv.destination_policy, pv.executable,
                   pv.validation_issues, pv.normalized_payload AS intent_payload,
                   ss.next_run_at,
                   pe.state AS published_state,
                   pe.state_changed_at AS published_at,
                   pe.row_count AS published_row_count
            FROM app.datastreams ds
            LEFT JOIN app.datastream_plan_versions pv
              ON pv.id = ds.current_plan_version_id
             AND pv.datastream_id = ds.id AND pv.project_id = ds.project_id
            LEFT JOIN app.datastream_schedule_state ss
              ON ss.plan_version_id = pv.id
            -- Epic 42 (Data overview fleet): lift the published execution's state,
            -- freshness and date onto the list row (was per-id read-model only).
            LEFT JOIN app.datastream_executions pe
              ON pe.id = ds.current_published_execution_id
             AND pe.project_id = ds.project_id
            WHERE ds.project_id = %s AND ds.archived_at IS NULL
            ORDER BY ds.name ASC
            """,
            (project_id,),
        )
        cols = [d[0] for d in cur.description]
        return [_row_to_dict(cols, row) for row in cur.fetchall()]


def get_datastream(ds_id: str, project_id: str, conn) -> dict | None:
    """Return one datastream by id, scoped to project_id.

    Returns None if not found OR if it belongs to a different project (AD-5).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ds.id, ds.project_id, ds.name, ds.module_name,
                   ds.connection_ref_id, ds.report_profile_id, ds.enabled,
                   ds.schedule_mode, ds.refetch_days, ds.date_window_days,
                   ds.config, ds.created_by, ds.created_at, ds.updated_at,
                   ds.source_kind, ds.current_plan_version_id, ds.archived_at,
                   ds.archived_by, pv.version_number AS current_plan_version,
                   pv.writer_kind, pv.destination_policy, pv.executable,
                   pv.validation_issues, pv.normalized_payload AS intent_payload,
                   ss.next_run_at
            FROM app.datastreams ds
            LEFT JOIN app.datastream_plan_versions pv
              ON pv.id = ds.current_plan_version_id
             AND pv.datastream_id = ds.id AND pv.project_id = ds.project_id
            LEFT JOIN app.datastream_schedule_state ss
              ON ss.plan_version_id = pv.id
            WHERE ds.id = %s AND ds.project_id = %s
            """,
            (ds_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return _row_to_dict(cols, row)


def create_datastream(
    data: dict,
    project_id: str,
    created_by: str,
    conn,
) -> dict:
    """INSERT a new datastream row; return the created dict.

    Required keys in data: name, module_name.
    Optional keys: connection_ref_id, report_profile_id, enabled,
                   schedule_mode, refetch_days, date_window_days, config.

    Raises psycopg.errors.UniqueViolation when (project_id, name) conflicts.
    Raises ValueError on missing required fields.
    """
    name = (data.get("name") or "").strip()
    source_kind = (data.get("source_kind") or "").strip() or None
    module_name = (data.get("module_name") or "").strip() or None
    if not name:
        raise ValueError("name est requis")
    if source_kind not in (None, "connector_pull", "external_bq", "managed_feed"):
        raise ValueError("source_kind invalide")
    if source_kind in (None, "connector_pull") and not module_name:
        raise ValueError("module_name est requis")
    if source_kind in ("external_bq", "managed_feed"):
        module_name = None

    ds_id = _mint_ds_id()
    connection_ref_id = data.get("connection_ref_id") or None
    report_profile_id = data.get("report_profile_id") or None
    enabled = bool(data.get("enabled", False if source_kind else True))
    schedule_mode = (data.get("schedule_mode") or ("manual" if source_kind else "nightly")).strip()
    if schedule_mode not in ("nightly", "manual", "hourly"):
        raise ValueError("schedule_mode doit etre 'nightly', 'manual' ou 'hourly'")
    refetch_days = int(data.get("refetch_days") or 3)
    date_window_days = int(data.get("date_window_days") or 30)
    config = data.get("config") or None

    # Story 34.2: refuse the (max+1)-th ACTIVE datastream for a trial org.
    # Bounds the creation only; drafts (enabled=False) do not consume the quota.
    if enabled:
        from core.trial_enforcement import check_datastream_limit  # noqa: PLC0415

        check_datastream_limit(project_id, conn, identity=created_by or "system")

    import json  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, connection_ref_id,
                 report_profile_id, enabled, schedule_mode, refetch_days,
                 date_window_days, config, created_by, source_kind)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            RETURNING id, project_id, name, module_name, connection_ref_id,
                      report_profile_id, enabled, schedule_mode, refetch_days,
                      date_window_days, config, created_by, created_at, updated_at,
                      source_kind, current_plan_version_id, archived_at, archived_by
            """,
            (
                ds_id,
                project_id,
                name,
                module_name,
                connection_ref_id,
                report_profile_id,
                enabled,
                schedule_mode,
                refetch_days,
                date_window_days,
                json.dumps(config) if config is not None else None,
                created_by,
                source_kind,
            ),
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        return _row_to_dict(cols, row)


def update_datastream(
    ds_id: str,
    project_id: str,
    patch: dict,
    conn,
) -> dict | None:
    """PATCH an existing datastream. Returns updated dict or None if not found / wrong project."""
    import json  # noqa: PLC0415

    existing = get_datastream(ds_id, project_id, conn)
    if existing is None:
        return None

    # Story 34.2 (F1 fix): enabling a draft is a 4th "creation" path. Without this
    # guard a trial org could create 3 enabled + 1 draft, then PATCH the draft
    # enabled to exceed the cap. Re-check the limit on a False->True transition
    # (idempotent True->True is NOT re-checked -- the datastream already counts).
    if "enabled" in patch and bool(patch["enabled"]) and not existing.get("enabled"):
        from core.trial_enforcement import check_datastream_limit  # noqa: PLC0415

        check_datastream_limit(project_id, conn, identity="system")

    # Build SET clause dynamically for allowed patchable fields.
    allowed = {
        "name",
        "connection_ref_id",
        "report_profile_id",
        "enabled",
        "schedule_mode",
        "refetch_days",
        "date_window_days",
        "config",
    }
    set_pairs: list[str] = []
    params: list = []

    for field in allowed:
        if field not in patch:
            continue
        val = patch[field]
        if field == "schedule_mode" and val not in ("nightly", "manual", "hourly"):
            raise ValueError("schedule_mode doit etre 'nightly', 'manual' ou 'hourly'")
        if field == "refetch_days":
            val = int(val)
        if field == "date_window_days":
            val = int(val)
        if field == "enabled":
            val = bool(val)
        if field == "config":
            set_pairs.append("config = %s::jsonb")
            params.append(json.dumps(val) if val is not None else None)
        else:
            set_pairs.append(f"{field} = %s")
            params.append(val)

    if not set_pairs:
        return existing  # Nothing to update.

    params.extend([ds_id, project_id])
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.datastreams
            SET {", ".join(set_pairs)}
            WHERE id = %s AND project_id = %s
            RETURNING id, project_id, name, module_name, connection_ref_id,
                      report_profile_id, enabled, schedule_mode, refetch_days,
                      date_window_days, config, created_by, created_at, updated_at
            """,  # noqa: S608
            params,
        )
        row = cur.fetchone()
        if row is None:
            return None
        col_names = [d[0] for d in cur.description]
        return _row_to_dict(col_names, row)


def enable_disable_datastream(
    ds_id: str,
    project_id: str,
    enabled: bool,
    conn,
) -> dict | None:
    """Enable or disable a datastream. Convenience wrapper around update_datastream."""
    return update_datastream(ds_id, project_id, {"enabled": enabled}, conn)


def delete_datastream(
    ds_id: str,
    project_id: str,
    conn,
    archived_by: str = "system",
) -> bool:
    """Delete a datastream.

    Strategy (consistent with AD-7 append-only principle):
      - If any pull_jobs row references this datastream: soft-archive
        (set enabled=false, config note). Never delete live history.
      - If no pull_jobs reference it: hard DELETE (mappings first, then datastream).

    Returns True when the row was found and actioned, False when not found / wrong project.
    """
    existing = get_datastream(ds_id, project_id, conn)
    if existing is None:
        return False

    # Check for pull_jobs references.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.pull_jobs WHERE datastream_id = %s",
            (ds_id,),
        )
        ref_count = cur.fetchone()[0]

    has_plan_version = False
    if ref_count == 0:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT current_plan_version_id
                FROM app.datastreams
                WHERE id = %s AND project_id = %s
                """,
                (ds_id, project_id),
            )
            pointer_row = cur.fetchone()
            has_plan_version = bool(pointer_row and pointer_row[0])

    if ref_count > 0 or has_plan_version:
        # Soft archive: immutable pull/plan history must remain reachable.
        import json  # noqa: PLC0415

        existing_config = existing.get("config") or {}
        existing_config["archived"] = True
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.datastreams
                SET enabled = FALSE,
                    archived_at = NOW(),
                    archived_by = %s,
                    config = %s::jsonb
                WHERE id = %s AND project_id = %s
                """,
                (archived_by, json.dumps(existing_config), ds_id, project_id),
            )
        logger.info(
            "datastreams: soft_archived ds_id=%s project_id=%s pull_job_refs=%d",
            ds_id,
            project_id,
            ref_count,
        )
    else:
        # Hard delete: mappings first (cascade would also handle this), then datastream.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.datastream_mappings WHERE datastream_id = %s",
                (ds_id,),
            )
            cur.execute(
                "DELETE FROM app.datastreams WHERE id = %s AND project_id = %s",
                (ds_id, project_id),
            )
            # Story 21.5 follow-up (review): sweep dangling per-flux resource grants.
            # resource_grants.scope_id is polymorphic (project id OR flux id) so it
            # carries NO FK -- a deleted flux would otherwise leave a stale
            # scope_type='flux' grant. to_regclass guards a pre-039 DB (table absent).
            cur.execute("SELECT to_regclass('app.resource_grants')")
            if cur.fetchone()[0] is not None:
                cur.execute(
                    "DELETE FROM app.resource_grants "
                    "WHERE scope_type = 'flux' AND scope_id = %s",
                    (ds_id,),
                )
        logger.info(
            "datastreams: hard_deleted ds_id=%s project_id=%s",
            ds_id,
            project_id,
        )

    return True


# ---------------------------------------------------------------------------
# Summaries helper (used by ledger story 8.3+)
# ---------------------------------------------------------------------------


def get_datastream_summaries(project_id: str, conn) -> list[dict]:
    """Return lightweight summaries for all datastreams in *project_id*.

    Joins with the latest pull_job per datastream to surface last pull info.
    Response shape per item:
      id, name, module_name, connection_ref_id, enabled, last_pull_id,
      last_pull_state, last_pull_completed_at
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                ds.id,
                ds.project_id,
                ds.name,
                ds.module_name,
                ds.connection_ref_id,
                ds.enabled,
                ds.schedule_mode,
                ds.refetch_days,
                ds.source_kind,
                ds.current_plan_version_id,
                pv.version_number AS current_plan_version,
                pv.writer_kind,
                pv.destination_policy,
                pv.executable,
                pv.validation_issues,
                pv.normalized_payload AS intent_payload,
                ss.next_run_at,
                cr.status   AS connection_status,
                lp.pull_id  AS last_pull_id,
                lp.state    AS last_pull_state,
                lp.completed_at AS last_pull_completed_at
            FROM app.datastreams ds
            LEFT JOIN app.datastream_plan_versions pv
              ON pv.id = ds.current_plan_version_id
             AND pv.datastream_id = ds.id AND pv.project_id = ds.project_id
            LEFT JOIN app.datastream_schedule_state ss
              ON ss.plan_version_id = pv.id
            LEFT JOIN app.connection_ref cr ON cr.id = ds.connection_ref_id
            LEFT JOIN LATERAL (
                SELECT pull_id, state, completed_at
                FROM app.pull_jobs
                WHERE datastream_id = ds.id
                ORDER BY enqueued_at DESC
                LIMIT 1
            ) lp ON true
            WHERE ds.project_id = %s AND ds.archived_at IS NULL
            ORDER BY ds.name ASC
            """,
            (project_id,),
        )
        cols = [d[0] for d in cur.description]
        return [_row_to_dict(cols, row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Backfill: one datastream per (connection_ref x report_profile)
# ---------------------------------------------------------------------------


def backfill_datastreams() -> dict:
    """Idempotent backfill: create one datastream per (connection_ref x report_profile).

    Reads live connection_ref rows from Postgres and manifest report_profiles from
    the loaded modules registry (core.main._loaded_modules). Skips modules with no
    report_profiles. Seeds app.datastream_mappings from each manifest's
    canonical_metric_mapping / canonical_dimension_mapping.

    Must be called after server startup (modules loaded) OR after explicitly loading
    the module registry. In batch mode, call:
        uv run python -c "
        import os; os.environ['PLATFORM_DB_URL'] = '<dsn>'
        # trigger module load
        import core.main  # noqa - loads _loaded_modules as side effect
        from core.datastreams import backfill_datastreams
        print(backfill_datastreams())
        "

    Returns a summary dict: {created: int, skipped: int, mappings_created: int, errors: list[str]}

    AD-2: module_name comes from connection_ref.provider (data), not hardcoded.
    Idempotent: uses INSERT ... ON CONFLICT DO NOTHING throughout.
    """
    from core.db import get_connection  # noqa: PLC0415

    # Build manifest lookup: provider -> manifest dict
    manifests: dict[str, dict] = {}
    try:
        from core.main import _loaded_modules  # noqa: PLC0415

        for mod in _loaded_modules:
            manifests[mod.name] = mod.manifest
    except Exception as exc:
        logger.warning("datastreams: backfill: manifest_load_failed: %s", exc)
        # Continue with empty manifests; connections will create datastreams
        # without report_profile bindings.

    created = 0
    skipped = 0
    mappings_created = 0
    errors: list[str] = []

    try:
        with get_connection() as conn:
            # Fetch all active connection_ref rows.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, provider, project_id
                    FROM app.connection_ref
                    WHERE status = 'active'
                    ORDER BY project_id, provider, id
                    """
                )
                connection_rows = cur.fetchall()

            for conn_id, provider, project_id in connection_rows:
                manifest = manifests.get(provider, {})
                report_profiles = manifest.get("report_profiles") or []

                if not report_profiles:
                    # No profiles: create one generic datastream (fallback).
                    report_profiles = [{"id": "default", "display_name": "Default"}]

                for profile in report_profiles:
                    profile_id = profile.get("id") or "default"
                    profile_display = profile.get("display_name") or profile_id
                    # Fix [MEDIUM #9]: two connections for the same provider+profile
                    # produced the same name and ON CONFLICT DO NOTHING seeded
                    # connection B's mappings into connection A's datastream.
                    # Disambiguate by appending the last 4 chars of connection_ref_id.
                    conn_suffix = conn_id[-4:] if len(conn_id) >= 4 else conn_id
                    ds_name = f"{provider} - {profile_display} [{conn_suffix}]"

                    ds_id = _mint_ds_id()

                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO app.datastreams
                                (id, project_id, name, module_name, connection_ref_id,
                                 report_profile_id, enabled, schedule_mode, refetch_days,
                                 date_window_days, config, created_by)
                            VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'nightly', 3, 30, NULL, 'system')
                            ON CONFLICT (project_id, name) DO NOTHING
                            RETURNING id
                            """,
                            (ds_id, project_id, ds_name, provider, conn_id, profile_id),
                        )
                        returned = cur.fetchone()

                    if returned is None:
                        skipped += 1
                        # Fetch the existing ds_id for mapping backfill (idempotent re-run).
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                SELECT id FROM app.datastreams
                                WHERE project_id = %s AND name = %s
                                """,
                                (project_id, ds_name),
                            )
                            existing_row = cur.fetchone()
                            if existing_row:
                                ds_id = existing_row[0]
                            else:
                                continue
                    else:
                        ds_id = returned[0]
                        created += 1
                        logger.info(
                            "datastreams: backfill_created ds_id=%s name=%r project=%s",
                            ds_id,
                            ds_name,
                            project_id,
                        )

                    # Seed mappings from manifest canonical mappings.
                    metric_mapping: dict = manifest.get("canonical_metric_mapping") or {}
                    dim_mapping: dict = manifest.get("canonical_dimension_mapping") or {}

                    for source_field, target_raw in metric_mapping.items():
                        # target_raw may be a string or a dict (gsc average_position).
                        if isinstance(target_raw, dict):
                            target_field = target_raw.get("canonical")
                        else:
                            target_field = target_raw

                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO app.datastream_mappings
                                    (datastream_id, source_field, target_field, is_key_column)
                                VALUES (%s, %s, %s, FALSE)
                                ON CONFLICT (datastream_id, source_field) DO NOTHING
                                """,
                                (ds_id, source_field, target_field or None),
                            )
                            if cur.rowcount:
                                mappings_created += 1

                    for source_field, target_field in dim_mapping.items():
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO app.datastream_mappings
                                    (datastream_id, source_field, target_field, is_key_column)
                                VALUES (%s, %s, %s, FALSE)
                                ON CONFLICT (datastream_id, source_field) DO NOTHING
                                """,
                                (ds_id, source_field, target_field or None),
                            )
                            if cur.rowcount:
                                mappings_created += 1

            conn.commit()

    except Exception as exc:
        msg = f"backfill_error: {exc}"
        errors.append(msg)
        logger.warning("datastreams: backfill_failed: %s", exc)

    summary = {
        "created": created,
        "skipped": skipped,
        "mappings_created": mappings_created,
        "errors": errors,
    }
    logger.info(
        "datastreams: backfill_complete created=%d skipped=%d mappings=%d errors=%d",
        created,
        skipped,
        mappings_created,
        len(errors),
    )
    return summary
