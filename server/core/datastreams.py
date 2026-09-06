"""toorow -- Datastream CRUD and backfill (Story 8.2, Epic 8).

Provides:
  list_datastreams(project_id, conn)         -> list[dict]
  get_datastream(ds_id, project_id, conn)    -> dict | None
  create_datastream(data, project_id, created_by, conn) -> dict
  update_datastream(ds_id, project_id, patch, conn)     -> dict | None
  enable_disable_datastream(ds_id, project_id, enabled, conn) -> dict | None
  delete_datastream(ds_id, project_id, conn) -> "archived" | "deleted" | None
  restore_datastream(ds_id, project_id, conn) -> dict | None
  get_datastream_summaries(project_id, conn) -> list[dict]
  backfill_datastreams()                     -> dict

Design decisions:
  - AD-2: module_name is DATA (from manifests / callers), never hardcoded here.
  - AD-5: every SELECT carries WHERE project_id = %s; cross-project access returns None.
  - AD-7: datastream IDs are minted here with ds_<ULID> prefix.
  - delete_datastream: soft-archive (enabled=false) when IMMUTABLE HISTORY
    references the datastream -- a pull job, an inbound receipt, or a published
    plan version; hard DELETE (mappings -> datastream) when unreferenced.
    Consistent with append-only patterns (AD-7): live history is preserved.
    It returns the disposition it chose, because its caller cannot re-derive a
    decision it did not make (AI-200).
  - backfill_datastreams: reads manifests from core.main._loaded_modules (same
    pattern as _get_manifest_for_module in queue.py) so no AD-2 violation.
    Idempotent throughout (INSERT ... ON CONFLICT DO NOTHING).

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# What a datastream is FOR. Mirrors app.datastreams.data_role's CHECK exactly
# (migration 093) -- kept here as the single place the API validates against, so
# an invalid value is a 400 that names the allowed set rather than a CheckViolation
# surfacing as a 500. Any change to the CHECK must be made here in the same commit.
DATA_ROLES: tuple[str, ...] = (
    "Spend",
    "Performance",
    "Revenue & conversions",
    "Forecast & plan",
    "Context",
    "Reference & targets",
    "Operational",
)

# ---------------------------------------------------------------------------
# The cadence, under its two names -- AI-217
# ---------------------------------------------------------------------------
#
# The plan intent says `daily`; the stable row says `nightly`. They are ONE
# setting under two names, and the translation between them used to live in four
# private dictionaries -- three forward (`datastream_activation` twice,
# `admin_api` once) and one backward (`_row_to_dict` below). `weekly` was missing
# from all four, so a cadence the CHECK constraint allows, the Workbench offers
# and the dispatcher runs was turned into `manual` on the way in and reported as
# `manual` on the way out: a Datastream that pulls once a week told every screen
# it never runs at all.
#
# One table now, both directions, so the next cadence added to the constraint is
# added once rather than forgotten three times.

#: Plan-intent cadence -> `app.datastreams.schedule_mode`.
_SCHEDULE_MODE_BY_CADENCE = {
    "daily": "nightly",
    "nightly": "nightly",
    "weekly": "weekly",
    "hourly": "hourly",
    "manual": "manual",
}

#: `app.datastreams.schedule_mode` -> plan-intent cadence.
_CADENCE_BY_SCHEDULE_MODE = {
    "nightly": "daily",
    "weekly": "weekly",
    "hourly": "hourly",
    "manual": "manual",
}


def schedule_mode_for_cadence(cadence: str | None) -> str:
    """The column value a plan cadence activates as.

    An unknown word still reads as `manual`, deliberately: a cadence nothing can
    honour must not be written as one that runs.
    """
    return _SCHEDULE_MODE_BY_CADENCE.get(str(cadence or "manual"), "manual")


def cadence_for_schedule_mode(mode: str | None) -> str:
    """The plan cadence a column value means, for a screen or an API to read."""
    return _CADENCE_BY_SCHEDULE_MODE.get(str(mode or "manual"), "manual")


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
            out["cadence_mode"] = cadence_for_schedule_mode(out.get("schedule_mode"))
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
                   ds.connection_ref_id, ds.source_account_id, ds.report_profile_id, ds.enabled,
                   ds.schedule_mode, ds.refetch_days, ds.date_window_days,
                   ds.window_offset_days, ds.config, ds.created_by, ds.created_at, ds.updated_at,
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
                   ds.connection_ref_id, ds.source_account_id, ds.report_profile_id, ds.enabled,
                   ds.schedule_mode, ds.refetch_days, ds.date_window_days,
                   ds.window_offset_days, ds.config, ds.created_by, ds.created_at, ds.updated_at,
                   ds.source_kind, ds.current_plan_version_id,
                   ds.current_mapping_version_id, ds.archived_at,
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
    Optional keys: connection_ref_id, source_account_id, report_profile_id,
                   enabled, schedule_mode, refetch_days, date_window_days,
                   config, data_role.

    ``source_account_id`` is WHICH account of the authorization this Datastream
    reads (``app.credential_accounts.source_account_id``). Omitting it on a
    connector_pull Datastream leaves the extraction guessing from the
    authorization-wide selection, which is how every Datastream of one Google
    consent ended up reading the same property (migration 211). The composite FK
    refuses an account that belongs to a different authorization.

    Raises psycopg.errors.UniqueViolation when (project_id, name) conflicts.
    Raises ValueError on missing required fields.
    """
    name = (data.get("name") or "").strip()
    source_kind = (data.get("source_kind") or "").strip() or None
    module_name = (data.get("module_name") or "").strip() or None
    # What the data is FOR (decision Jean, 2026-07-27: "faut lui dire a quoi il
    # sert"). Migration 093 created app.datastreams.data_role with this exact
    # CHECK, and backfilled it ONCE by running a regular expression over
    # module_name -- then nothing was ever able to write it again: `data_role`
    # appeared in a single SELECT and in no INSERT or UPDATE anywhere in the
    # server. Every datastream was therefore stuck with whatever that regex
    # guessed, and the person who actually knows the answer had no way to give
    # it. This is that write path.
    data_role = (data.get("data_role") or "").strip() or None
    if data_role is not None and data_role not in DATA_ROLES:
        # Rejected here rather than at the CHECK: a 400 naming the allowed set is
        # actionable, a psycopg CheckViolation surfacing as a 500 is not.
        raise ValueError("data_role invalide")
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
    source_account_id = data.get("source_account_id") or None
    if source_account_id and not connection_ref_id:
        raise ValueError("source_account_id requires connection_ref_id")
    report_profile_id = data.get("report_profile_id") or None
    enabled = bool(data.get("enabled", False if source_kind else True))
    schedule_mode = (data.get("schedule_mode") or ("manual" if source_kind else "nightly")).strip()
    if schedule_mode not in ("nightly", "manual", "hourly", "weekly"):
        raise ValueError("schedule_mode must be 'nightly', 'weekly', 'manual' or 'hourly'")
    refetch_days = int(data.get("refetch_days") or 3)
    date_window_days = int(data.get("date_window_days") or 30)
    window_offset_days = int(data.get("window_offset_days") or 1)
    if window_offset_days < 1 or window_offset_days > 90:
        raise ValueError("window_offset_days must be between 1 and 90")
    config = data.get("config") or None

    # Story 34.2: refuse the (max+1)-th ACTIVE datastream for a trial org.
    # Bounds the creation only; drafts (enabled=False) do not consume the quota.
    if enabled:
        from core.trial_enforcement import check_datastream_limit  # noqa: PLC0415

        check_datastream_limit(project_id, conn, identity=created_by or "system")

    import json  # noqa: PLC0415

    with conn.cursor() as cur:
        # L'organisation du flux, derivee de son projet. C'est SUR ELLE que se
        # joue l'isolation data (architecture-org-tenancy 3.8), et la colonne est
        # NOT NULL depuis la migration 100 : sans cette derivation, toute creation
        # de datastream repondait 500. Le projet en a forcement une -- meme
        # migration -- donc la sous-requete ne peut pas rendre NULL.
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, org_id, name, module_name, connection_ref_id,
                 source_account_id,
                 report_profile_id, enabled, schedule_mode, refetch_days,
                 date_window_days, window_offset_days, config, created_by, source_kind, data_role)
            VALUES (%s, %s,
                    (SELECT org_id FROM app.projects WHERE id = %s),
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING id, project_id, name, module_name, connection_ref_id,
                      source_account_id,
                      report_profile_id, enabled, schedule_mode, refetch_days,
                      date_window_days, window_offset_days, config, created_by,
                      created_at, updated_at,
                      source_kind, current_plan_version_id, archived_at, archived_by,
                      data_role
            """,
            (
                ds_id,
                project_id,
                project_id,  # -> sous-requete org_id
                name,
                module_name,
                connection_ref_id,
                source_account_id,
                report_profile_id,
                enabled,
                schedule_mode,
                refetch_days,
                date_window_days,
                window_offset_days,
                json.dumps(config) if config is not None else None,
                created_by,
                source_kind,
                data_role,
            ),
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        created = _row_to_dict(cols, row)

        # Link the Datastream to the project that owns it.
        #
        # Every detail route resolves a Datastream through
        # `_resolve_datastream_route_scope`, which JOINs app.project_flux -- the
        # table that says which projects may see this flux. Creation never wrote
        # that row, so a Datastream was invisible to its OWN screens the second
        # it existed: overview, data, mapping, runs and sample all answered 404
        # "Flux de donnees introuvable" for an object that was right there in
        # app.datastreams. The link is part of creating it, not a later step.
        cur.execute(
            """
            INSERT INTO app.project_flux (project_id, flux_id, org_id)
            SELECT %s, %s, org_id FROM app.datastreams
            -- project_flux.org_id est NOT NULL : sans ce filtre, un projet
            -- historique sans organisation ferait echouer la creation entiere
            -- en 500 au lieu de simplement ne pas etre indexe.
            WHERE id = %s AND org_id IS NOT NULL
            ON CONFLICT DO NOTHING
            """,
            (project_id, created["id"], created["id"]),
        )
        return created


def update_datastream(
    ds_id: str,
    project_id: str,
    patch: dict,
    conn,
) -> dict | None:
    """PATCH an existing datastream. Returns updated dict or None if not found / wrong project."""
    import json  # noqa: PLC0415

    governed_fields = {"enabled", "schedule_mode"}
    if governed_fields & set(patch):
        raise ValueError("Lifecycle and cadence changes require a reviewed Datastream operation")

    existing = get_datastream(ds_id, project_id, conn)
    if existing is None:
        return None

    # Build SET clause dynamically for allowed patchable fields. "enabled" and
    # "schedule_mode" are NOT here: the governed-fields guard above rejects them
    # before this point, so listing them would be dead configuration.
    allowed = {
        "name",
        "connection_ref_id",
        # Re-pointing a Datastream at another account of the same authorization
        # is a normal edit; the composite FK refuses one from a different grant.
        "source_account_id",
        "report_profile_id",
        "refetch_days",
        "date_window_days",
        "window_offset_days",
        "config",
        # WHAT THE DATA IS FOR, and it had to become patchable for one reason:
        # nothing could write it after creation. `flows.upsert_flow` builds
        # `scalar["data_role"]` and hands it here, where it was silently dropped --
        # the API accepted the field and changed nothing, which is worse than
        # refusing it. Measured on 2026-08-12: a project whose facts carry 519
        # video ids had exactly one stream able to name them, and no person and no
        # model could declare it as the reference, because this set did not hold
        # the word. The join between a fact stream and the stream that names its
        # values is derived from `data_role` + the canonical target
        # (`core.dimension_reference`), so a role nobody can set is a join nobody
        # can make.
        "data_role",
    }
    set_pairs: list[str] = []
    params: list = []

    for field in allowed:
        if field not in patch:
            continue
        val = patch[field]
        if field == "refetch_days":
            val = int(val)
        if field == "date_window_days":
            val = int(val)
        if field == "window_offset_days":
            val = int(val)
            if val < 1 or val > 90:
                raise ValueError("window_offset_days must be between 1 and 90")
        if field == "data_role":
            # Validated HERE and not left to the CHECK of migration 093: a
            # constraint violation surfaces as a 500 that names nothing, and the
            # person who mistyped a role needs the list of the seven.
            val = (val or "").strip() or None
            if val is not None and val not in DATA_ROLES:
                raise ValueError(
                    "data_role invalide: " + ", ".join(DATA_ROLES)
                )
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
                      source_account_id,
                      report_profile_id, enabled, schedule_mode, refetch_days,
                      date_window_days, window_offset_days, config, created_by,
                      created_at, updated_at
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


def _restricting_references(conn, ds_id: str) -> list[tuple[str, int]]:
    """Which tables hold a row for this Datastream, asked of the catalogue.

    Only the foreign keys that would REFUSE the delete: a cascading key erases
    itself and a nullable one lets go, so neither decides anything here. The
    answer is (table, count) pairs so the archive can say what held it rather
    than reporting a number nobody can trace back to a table.

    Costs one query. A table added by a later migration is included the day it
    exists, which is the whole point: the previous hand-kept list of three named
    two tables and one POINTER, and was wrong for 31 others without ever saying so.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT
                   (SELECT relname FROM pg_class WHERE oid = c.conrelid) AS table_name,
                   a.attname AS column_name
              FROM pg_constraint c
              JOIN pg_attribute a
                ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
             WHERE c.contype = 'f'
               AND c.confrelid = 'app.datastreams'::regclass
               AND c.confdeltype IN ('a', 'r')
               AND a.attname LIKE '%%datastream%%'
             ORDER BY 1, 2
            """
        )
        targets = [(str(row[0]), str(row[1])) for row in cur.fetchall()]

    # A composite key names the project too, so the column is chosen by name
    # rather than by position: `datastream_id`, and the spellings that qualify it
    # (`origin_datastream_id`, `materialized_datastream_id`).
    held: list[tuple[str, int]] = []
    for table, target_column in targets:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM app.{table} WHERE {target_column} = %s",  # noqa: S608
                (ds_id,),
            )
            count = int(cur.fetchone()[0])
        if count:
            held.append((table, count))

    # AND THE ONE KEY THAT LETS GO INSTEAD OF REFUSING. `pull_jobs` references a
    # Datastream with ON DELETE SET NULL, so a hard delete does not fail -- it
    # leaves the pull history alive and orphaned, attributable to nothing. AD-7
    # asks the opposite, so a Datastream that has ever pulled is archived.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM app.pull_jobs WHERE datastream_id = %s", (ds_id,))
        pulls = int(cur.fetchone()[0])
    if pulls:
        held.append(("pull_jobs", pulls))
    return held


def delete_datastream(
    ds_id: str,
    project_id: str,
    conn,
    archived_by: str = "system",
) -> str | None:
    """Delete a datastream, and SAY WHICH OF THE TWO THINGS IT DID.

    Strategy (consistent with AD-7 append-only principle):
      - If immutable history references this datastream -- a pull job, an inbound
        receipt, or a published plan version -- soft-archive it (enabled=false,
        `archived_at`). Never delete history.
      - Otherwise: hard DELETE (mappings first, then the datastream).

    Returns ``"archived"`` or ``"deleted"``, and ``None`` when the row was not
    found or belongs to another project.

    WHY A STRING AND NOT A BOOLEAN. The disposition was decided here, on three
    conditions, and re-derived by the HTTP route from ONE of them -- the pull-job
    count. So a datastream whose only history was an inbound receipt got archived
    here and reported as ``"deleted"`` to the caller: the API answered something
    that had not happened. A caller cannot re-derive a decision it did not make;
    it has to be told.
    """
    existing = get_datastream(ds_id, project_id, conn)
    if existing is None:
        return None

    # WHAT HOLDS THIS DATASTREAM IS A QUESTION FOR THE DATABASE, not a list kept
    # by hand here. This used to name three things -- pull jobs, inbound receipts,
    # and the CURRENT PLAN POINTER -- while 34 tables carry a restricting foreign
    # key to `app.datastreams`. Two consequences, both measured in production on
    # 2026-08-12 while trying to clear a project of 37 Datastreams:
    #
    #   * The pointer is not the row. A Datastream that never published has a plan
    #     VERSION and a NULL pointer, so the check said "no history", the hard
    #     delete ran, and `fk_datastream_plan_datastream_scope` refused it. 27 of
    #     28 deletions answered 500 `db_error`. Every Datastream born in the setup
    #     wizard was therefore undeletable, and a Fleet could only grow.
    #   * Each new table with such a key silently joined that set. The list could
    #     not be kept correct by anyone, because nothing made it wrong out loud.
    #
    # `_restricting_references` asks the catalogue which tables reference this row
    # and looks in each. Adding a table changes nothing here.
    held_by = _restricting_references(conn, ds_id)
    ref_count = sum(count for _, count in held_by)

    if ref_count > 0:
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
            "datastreams: soft_archived ds_id=%s project_id=%s held_by=%s",
            ds_id,
            project_id,
            ", ".join(f"{table}:{count}" for table, count in held_by) or "none",
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
                    "DELETE FROM app.resource_grants WHERE scope_type = 'flux' AND scope_id = %s",
                    (ds_id,),
                )
        logger.info(
            "datastreams: hard_deleted ds_id=%s project_id=%s",
            ds_id,
            project_id,
        )
        return "deleted"

    return "archived"


class NotArchivedError(Exception):
    """A restore was asked of a Datastream that is not archived.

    A restore is NOT idempotent on purpose, and the precedent is
    `core.context_store.restore_topic`, which refuses the same way for the same
    reason: answering "done" to a gesture that did nothing tells a person their
    click landed. Two people clearing a Fleet would each read a success and only
    one of them would have restored anything.
    """


class NameTakenError(Exception):
    """A live Datastream of this project already holds the archived one's name.

    THIS IS THE COST OF MIGRATION 256, PAID DELIBERATELY. That migration made
    `uq_datastreams_project_name` partial -- unique over LIVE rows only -- so an
    archived Datastream stops reserving its name and the same feed can be rebuilt
    under the name it deserves. The consequence lands exactly here: rebuilding it
    is what makes the archived one's name unavailable, and a restore that ignored
    the index would surface as an opaque `500`. The message names the gesture,
    which is renaming one of the two.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def restore_datastream(
    ds_id: str,
    project_id: str,
    conn,
) -> dict | None:
    """Undo a soft archive, and undo EXACTLY what the soft archive wrote.

    `delete_datastream` writes four things when it archives: `enabled = FALSE`,
    `archived_at`, `archived_by`, and `config.archived = true`. This clears the
    last three and DELIBERATELY LEAVES `enabled` FALSE.

    WHY THE CLOCK DOES NOT COME BACK WITH THE ROW. Restoring answers "this
    Datastream exists again"; starting it answers "and it collects from now on".
    They are two consents and the second one has a cost -- a provider call, a
    quota, a bill -- so a restore that re-armed the clock would spend money on a
    gesture nobody read as spending. It also has an owner already:
    `PUT /api/datastreams/{id}/schedule` is the single writer of the armed flag
    (`schedule_mcp`), and a second route setting `enabled` would be the "second
    activation authority" the Workbench surface forbids by name. So a restored
    Datastream lands in `paused` -- `schedule_mcp._run_state` reads the same two
    columns and answers "Configured, not collecting", which is true -- and the
    Schedule panel's own `Start collecting` is what finishes the journey.

    `lifecycle_state` IS NOT TOUCHED, because the archive did not touch it. A
    Datastream archived while it was a `draft` comes back a draft, and one
    archived while active comes back active-but-stopped. Writing `'active'` here
    would ACTIVATE something that was never published.

    Raises `NotArchivedError` when the row is not archived, `NameTakenError` when
    a live sibling holds its name. Returns the restored row, or None when the id
    does not name a Datastream of this project (AD-5).
    """
    import json  # noqa: PLC0415

    existing = get_datastream(ds_id, project_id, conn)
    if existing is None:
        return None
    if existing.get("archived_at") is None:
        raise NotArchivedError(ds_id)

    config = existing.get("config") or {}
    config.pop("archived", None)

    # THE COLLISION IS ASKED OF THE DATABASE, NOT PRE-CHECKED HERE. A SELECT then
    # an UPDATE is two statements a concurrent rebuild can slip between, and the
    # partial unique index is the only thing that cannot be raced. `SAVEPOINT`
    # keeps the caller's transaction usable after the refusal -- without it the
    # connection is aborted and the route's own rollback is the only way out.
    from psycopg import errors as pg_errors  # noqa: PLC0415

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.datastreams
                    SET archived_at = NULL,
                        archived_by = NULL,
                        config = %s::jsonb
                    WHERE id = %s AND project_id = %s
                    """,
                    (json.dumps(config), ds_id, project_id),
                )
    except pg_errors.UniqueViolation as exc:
        raise NameTakenError(str(existing.get("name") or "")) from exc

    logger.info("datastreams: restored ds_id=%s project_id=%s", ds_id, project_id)
    return get_datastream(ds_id, project_id, conn)


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

                    # THE FOURTH WRITER OF `enabled = TRUE` MEETS THE SAME GUARD.
                    #
                    # This INSERT writes the flag LITERALLY, so a maintenance
                    # sweep could hand a trial org more running Datastreams than
                    # its plan allows -- silently, because no person is at the
                    # other end of it.
                    #
                    # Asked only for a row that does NOT exist yet. The INSERT is
                    # `ON CONFLICT DO NOTHING` and the sweep is meant to be re-run:
                    # counting an already-existing Datastream against the cap would
                    # turn a clean idempotent second run into a page of refusals
                    # for rows nobody was creating.
                    #
                    # And ONE row is refused, not the sweep: this loop serves every
                    # project of every org, so aborting on the first bounded org
                    # would leave every org after it unbackfilled.
                    with conn.cursor() as cur:
                        # LIVENESS, NOT EXISTENCE -- the same predicate as the
                        # index, for the same reason. Migration 256 made an
                        # archived Datastream FREE its name, so "a row with this
                        # name exists" and "this name is taken" stopped being the
                        # same question. Without the predicate an archived
                        # namesake made `row_exists` true, the trial cap below was
                        # skipped, and the INSERT then succeeded anyway: a
                        # Datastream over the cap, silently. Unreachable while the
                        # INSERT crashed on its conflict target; arming the INSERT
                        # is what made it live, which is why it is repaired in the
                        # same breath.
                        cur.execute(
                            "SELECT 1 FROM app.datastreams "
                            "WHERE project_id = %s AND name = %s "
                            "AND archived_at IS NULL",
                            (project_id, ds_name),
                        )
                        row_exists = cur.fetchone() is not None
                    if not row_exists:
                        from core.trial_enforcement import (  # noqa: PLC0415
                            TrialDatastreamLimitError,
                            check_datastream_limit,
                        )

                        try:
                            check_datastream_limit(project_id, conn, identity="system")
                        except TrialDatastreamLimitError as limit_exc:
                            errors.append(f"trial_limit {project_id}: {limit_exc.message}")
                            skipped += 1
                            continue

                    with conn.cursor() as cur:
                        # THE PREDICATE IS PART OF THE INFERENCE. The unique index
                        # on (project_id, name) is PARTIAL --
                        # `uq_datastreams_project_name_live ... WHERE archived_at
                        # IS NULL` -- so a bare `ON CONFLICT (project_id, name)`
                        # matches no index and Postgres raises
                        # `InvalidColumnReference` BEFORE inserting anything.
                        # Measured 2026-08-21 on the disposable cluster: the bare
                        # form does not even EXPLAIN. Archiving a Datastream frees
                        # its name, which is why the index is partial; the conflict
                        # target has to say the same thing.
                        cur.execute(
                            """
                            INSERT INTO app.datastreams
                                (id, project_id, org_id, name, module_name,
                                 connection_ref_id, report_profile_id, enabled,
                                 schedule_mode, refetch_days, date_window_days,
                                 config, created_by)
                            VALUES (%s, %s,
                                    (SELECT org_id FROM app.projects WHERE id = %s),
                                    %s, %s, %s, %s, TRUE, 'nightly', 3, 30, NULL, 'system')
                            ON CONFLICT (project_id, name)
                                WHERE archived_at IS NULL DO NOTHING
                            RETURNING id
                            """,
                            (ds_id, project_id, project_id, ds_name, provider, conn_id, profile_id),
                        )
                        returned = cur.fetchone()

                    if returned is None:
                        skipped += 1
                        # Fetch the existing ds_id for mapping backfill (idempotent re-run).
                        with conn.cursor() as cur:
                            # SAME PREDICATE, SAME REASON, AND HERE IT CHOOSES AN
                            # ID. With one archived and one live row of the same
                            # name this returned the ARCHIVED id -- no predicate,
                            # no ORDER BY -- and that id is what the mapping
                            # backfill then writes against. A read that picks the
                            # wrong row is worse than one that finds none.
                            cur.execute(
                                """
                                SELECT id FROM app.datastreams
                                WHERE project_id = %s AND name = %s
                                  AND archived_at IS NULL
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
                        # Second chemin de creation d'un Datastream : il doit
                        # ecrire le lien project_flux comme create_datastream le
                        # fait, sinon les flux backfilles sont listes mais
                        # repondent 404 sur chacun de leurs ecrans de detail.
                        with conn.cursor() as link_cur:
                            link_cur.execute(
                                """
                                INSERT INTO app.project_flux (project_id, flux_id, org_id)
                                SELECT %s, %s, org_id FROM app.datastreams
                                WHERE id = %s AND org_id IS NOT NULL
                                ON CONFLICT DO NOTHING
                                """,
                                (project_id, ds_id, ds_id),
                            )
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
