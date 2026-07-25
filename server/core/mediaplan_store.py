"""toorow -- Media plan domain service (Story 22.1, FR38 / CAP-26).

Pure business logic over a psycopg connection (pattern: core.context_store).
The caller owns the transaction lifecycle (commit / rollback). Postgres is the
sole writer of the plan (AD-8); dbt reads a governed mirror.

Key invariants (proven by tests):
  * SUM(plan_allocation_daily.amount) == line budget, AT THE CENT. Rounding
    remainders are distributed by a DETERMINISTIC largest-remainder rule so two
    identical publications produce byte-identical allocations.
  * Versions are append-only; exactly one active version per plan; the active
    pointer flips atomically at publication.
  * Publishing reprocesses the WHOLE range of EVERY line (decision 3, past
    included) -- the plan is a join table, not a hybrid freeze.

Business errors are typed (subclasses of ValueError carrying a ``code``); the
API layer maps them to 4xx and NEVER leaks str(exc) into a 5xx body (lesson 12.3).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from core.audit import (
    ACTION_MEDIA_PLAN_CREATED,
    ACTION_MEDIA_PLAN_VERSION_CREATED,
    ACTION_MEDIA_PLAN_VERSION_PUBLISHED,
    insert_audit_row,
)
from core.reshape import CENT as _CENT
from core.reshape import ReshapeValidationError
from core.reshape import compute_spread as _reshape_compute_spread

# ``_CENT`` (the exact one-cent Decimal quantum) now has a single source of truth
# in ``core.reshape`` (Story 22.9). Re-exported here so existing importers of
# ``core.mediaplan_store._CENT`` (e.g. mediaplan_import) keep working unchanged.


class MediaPlanError(ValueError):
    """Base class for typed media-plan business errors (carries a stable code)."""

    code = "media_plan_error"


class MediaPlanValidationError(MediaPlanError):
    """A caller-supplied value is invalid (maps to 422)."""

    code = "invalid_param"


class MediaPlanNotFoundError(MediaPlanError):
    """A referenced plan / version does not exist (maps to 404)."""

    code = "not_found"


class MediaPlanStateError(MediaPlanError):
    """An operation is illegal in the current state (e.g. publishing twice)."""

    code = "invalid_state"


# ---------------------------------------------------------------------------
# Deterministic linear spread (the cent-exact core)
# ---------------------------------------------------------------------------


def compute_spread(
    budget: Decimal, start_date: date, end_date: date
) -> list[tuple[date, Decimal]]:
    """Spread ``budget`` linearly across [start_date, end_date] at the cent.

    Story 22.9: the mechanics now live in the shared ``core.reshape`` engine
    (AD-5) so the media plan and the file-source producer share ONE cent-exact,
    sum-preserving, deterministic largest-remainder spread. This thin wrapper
    preserves the media-plan contract UNCHANGED: reshape's typed validation error
    is re-raised as ``MediaPlanValidationError`` so the API layer still maps a bad
    budget / inverted dates to 422 (never a 500).

    Raises MediaPlanValidationError on start_date > end_date, non-Decimal /
    non-finite / negative budget.
    """
    try:
        return _reshape_compute_spread(budget, start_date, end_date)
    except ReshapeValidationError as exc:
        raise MediaPlanValidationError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _fmt(val: Any) -> Any:
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Decimal):
        # Render money as a stable string (JSON-safe, no float rounding).
        return format(val, "f")
    return val


def _row_to_dict(row: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    return {col: _fmt(val) for col, val in zip(cols, row)}


def _parse_date(value: Any, *, field: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise MediaPlanValidationError(
                f"Le champ « {field} » doit être une date ISO (AAAA-MM-JJ)."
            ) from exc
    raise MediaPlanValidationError(
        f"Le champ « {field} » doit être une date ISO (AAAA-MM-JJ)."
    )


def _parse_budget(value: Any) -> Decimal:
    try:
        budget = Decimal(str(value))
    except Exception as exc:
        raise MediaPlanValidationError(
            "Le budget doit être un nombre décimal."
        ) from exc
    if not budget.is_finite():
        raise MediaPlanValidationError("Le budget doit être un nombre fini.")
    if budget < 0:
        raise MediaPlanValidationError("Le budget ne peut pas être négatif.")
    # Normalise to the cent so storage and spread agree exactly.
    return budget.quantize(_CENT)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

_PLAN_COLS = [
    "id",
    "project_id",
    "name",
    "currency",
    "created_by",
    "created_at",
    "updated_at",
    "archived_at",
]


def create_plan(
    conn: Any,
    *,
    project_id: str,
    name: str,
    currency: str = "EUR",
    created_by: str,
) -> dict[str, Any]:
    """Create a media plan (no version yet)."""
    if not isinstance(name, str) or not name.strip():
        raise MediaPlanValidationError("Le nom du plan ne peut pas être vide.")
    clean_name = name.strip()
    clean_currency = (currency or "EUR").strip() or "EUR"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.media_plans
                (project_id, name, currency, created_by, created_at, updated_at)
            VALUES (%s, %s, %s, %s, now(), now())
            RETURNING id, project_id, name, currency, created_by,
                      created_at, updated_at, archived_at
            """,
            (project_id, clean_name, clean_currency, created_by),
        )
        row = cur.fetchone()
        plan = _row_to_dict(row, _PLAN_COLS)

        insert_audit_row(
            conn,
            identity=created_by,
            action=ACTION_MEDIA_PLAN_CREATED,
            provider_account="platform",
            connection_ref="",
            metadata={"plan_id": plan["id"], "project_id": project_id, "name": clean_name},
        )
    return plan


def list_plans(conn: Any, *, project_id: str) -> list[dict[str, Any]]:
    """List (non-archived) plans of a project, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, name, currency, created_by,
                   created_at, updated_at, archived_at
            FROM app.media_plans
            WHERE project_id = %s AND archived_at IS NULL
            ORDER BY created_at DESC
            """,
            (project_id,),
        )
        return [_row_to_dict(r, _PLAN_COLS) for r in cur.fetchall()]


def get_plan(conn: Any, *, plan_id: str) -> dict[str, Any] | None:
    """Fetch a plan with its active version (+ that version's lines).

    Returns None if the plan does not exist. When no version is active,
    ``active_version`` is None and ``lines`` is an empty list.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, name, currency, created_by,
                   created_at, updated_at, archived_at
            FROM app.media_plans
            WHERE id = %s
            """,
            (plan_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        plan = _row_to_dict(row, _PLAN_COLS)

    active = _get_active_version(conn, plan_id=plan_id)
    plan["active_version"] = active
    plan["lines"] = (
        list_lines(conn, version_id=active["id"]) if active is not None else []
    )
    return plan


# ---------------------------------------------------------------------------
# Versions & lines
# ---------------------------------------------------------------------------

_VERSION_COLS = [
    "id",
    "plan_id",
    "version_number",
    "status",
    "is_active",
    "source_note",
    "created_by",
    "created_at",
]

_LINE_COLS = [
    "id",
    "version_id",
    "line_key",
    "label",
    "channel",
    "start_date",
    "end_date",
    "budget",
    "buy_mode",
    "is_plan_only",
    "sort_order",
]


def _get_active_version(conn: Any, *, plan_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, plan_id, version_number, status, is_active,
                   source_note, created_by, created_at
            FROM app.media_plan_versions
            WHERE plan_id = %s AND is_active
            """,
            (plan_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(row, _VERSION_COLS) if row else None


def list_versions(conn: Any, *, plan_id: str) -> list[dict[str, Any]]:
    """List every version of a plan, newest version_number first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, plan_id, version_number, status, is_active,
                   source_note, created_by, created_at
            FROM app.media_plan_versions
            WHERE plan_id = %s
            ORDER BY version_number DESC
            """,
            (plan_id,),
        )
        return [_row_to_dict(r, _VERSION_COLS) for r in cur.fetchall()]


def list_lines(conn: Any, *, version_id: str) -> list[dict[str, Any]]:
    """List the lines of a version, in sort order."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, version_id, line_key, label, channel, start_date,
                   end_date, budget, buy_mode, is_plan_only, sort_order
            FROM app.media_plan_lines
            WHERE version_id = %s
            ORDER BY sort_order ASC, line_key ASC
            """,
            (version_id,),
        )
        return [_row_to_dict(r, _LINE_COLS) for r in cur.fetchall()]


def _normalise_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate + normalise the caller line payload.

    Each version carries the COMPLETE set of its lines. line_key is the stable
    upsert key; a payload may not repeat a line_key (that is the "upsert within
    the payload" the spec asks for -- last-writer wins would hide data, so a
    duplicate is rejected loudly).
    """
    if not isinstance(lines, list) or not lines:
        raise MediaPlanValidationError("La version doit contenir au moins une ligne.")

    seen: set[str] = set()
    normalised: list[dict[str, Any]] = []
    for idx, raw in enumerate(lines):
        if not isinstance(raw, dict):
            raise MediaPlanValidationError("Chaque ligne doit être un objet.")

        line_key = (raw.get("line_key") or "").strip()
        if not line_key:
            raise MediaPlanValidationError(
                "La clé de ligne (line_key) ne peut pas être vide."
            )
        if line_key in seen:
            raise MediaPlanValidationError(
                f"La clé de ligne « {line_key} » est dupliquée dans la version."
            )
        seen.add(line_key)

        start = _parse_date(raw.get("start_date"), field="start_date")
        end = _parse_date(raw.get("end_date"), field="end_date")
        if start > end:
            raise MediaPlanValidationError(
                "La date de début doit précéder la date de fin."
            )
        budget = _parse_budget(raw.get("budget"))

        normalised.append(
            {
                "line_key": line_key,
                "label": str(raw.get("label") or ""),
                "channel": (raw.get("channel") or None) or None,
                "start_date": start,
                "end_date": end,
                "budget": budget,
                "buy_mode": (raw.get("buy_mode") or None) or None,
                "is_plan_only": bool(raw.get("is_plan_only", False)),
                "sort_order": int(raw.get("sort_order", idx)),
            }
        )
    return normalised


def create_version_with_lines(
    conn: Any,
    *,
    plan_id: str,
    lines: list[dict[str, Any]],
    source_note: str | None = None,
    created_by: str,
) -> dict[str, Any]:
    """Create a CANDIDATE version carrying the complete set of ``lines``.

    version_number = max(existing) + 1. The version is created inactive and
    unpublished; publish_version() materialises allocations and flips the pointer.
    """
    normalised = _normalise_lines(lines)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.media_plans WHERE id = %s AND archived_at IS NULL",
            (plan_id,),
        )
        if cur.fetchone() is None:
            raise MediaPlanNotFoundError("Plan introuvable.")

        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0)
            FROM app.media_plan_versions
            WHERE plan_id = %s
            """,
            (plan_id,),
        )
        next_number = int(cur.fetchone()[0]) + 1

        try:
            cur.execute(
                """
                INSERT INTO app.media_plan_versions
                    (plan_id, version_number, status, is_active, source_note,
                     created_by, created_at)
                VALUES (%s, %s, 'candidate', false, %s, %s, now())
                RETURNING id, plan_id, version_number, status, is_active,
                          source_note, created_by, created_at
                """,
                (plan_id, next_number, source_note, created_by),
            )
        except psycopg.errors.UniqueViolation as exc:
            # Concurrent create computed the same MAX(version_number)+1 (F-1):
            # the UNIQUE (plan_id, version_number) makes the loser retryable.
            raise MediaPlanStateError(
                "Modification concurrente du plan, veuillez réessayer."
            ) from exc
        version = _row_to_dict(cur.fetchone(), _VERSION_COLS)
        version_id = version["id"]

        for line in normalised:
            cur.execute(
                """
                INSERT INTO app.media_plan_lines
                    (version_id, line_key, label, channel, start_date, end_date,
                     budget, buy_mode, is_plan_only, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    line["line_key"],
                    line["label"],
                    line["channel"],
                    line["start_date"],
                    line["end_date"],
                    line["budget"],
                    line["buy_mode"],
                    line["is_plan_only"],
                    line["sort_order"],
                ),
            )

        insert_audit_row(
            conn,
            identity=created_by,
            action=ACTION_MEDIA_PLAN_VERSION_CREATED,
            provider_account="platform",
            connection_ref="",
            metadata={
                "plan_id": plan_id,
                "version_id": version_id,
                "version_number": next_number,
                "line_count": len(normalised),
            },
        )

    version["lines"] = list_lines(conn, version_id=version_id)
    return version


def publish_version(
    conn: Any, *, version_id: str, published_by: str
) -> dict[str, Any]:
    """Publish a candidate version in a SINGLE transaction unit.

    Steps (all in the caller's transaction):
      (a) materialise the daily spread for EVERY line (full reprocess, past
          included -- decision 3), including plan_only lines (their budget still
          exists; only the pacing does not apply -- 22.4);
      (b) assert SUM(allocations) == line budget per line (defensive invariant);
      (c) status candidate -> published;
      (d) flip the active pointer atomically: the previous active version of the
          same plan is set is_active=false, this version to true.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.plan_id, v.version_number, v.status, v.is_active,
                   p.archived_at
            FROM app.media_plan_versions v
            JOIN app.media_plans p ON p.id = v.plan_id
            WHERE v.id = %s
            FOR UPDATE OF v
            """,
            (version_id,),
        )
        vrow = cur.fetchone()
        if vrow is None:
            raise MediaPlanNotFoundError("Version introuvable.")
        _, plan_id, version_number, status, _is_active, plan_archived_at = vrow
        if plan_archived_at is not None:
            # F-5: a candidate must never be published onto an archived plan.
            raise MediaPlanStateError(
                "Impossible de publier une version d'un plan archivé."
            )
        if status != "candidate":
            raise MediaPlanStateError("Seule une version candidate peut être publiée.")

        cur.execute(
            """
            SELECT id, budget, start_date, end_date
            FROM app.media_plan_lines
            WHERE version_id = %s
            ORDER BY sort_order ASC, line_key ASC
            """,
            (version_id,),
        )
        line_rows = cur.fetchall()
        if not line_rows:
            raise MediaPlanStateError(
                "Impossible de publier une version sans ligne."
            )

        # (a) + (b): materialise + invariant check per line.
        for line_id, budget, start_date, end_date in line_rows:
            allocations = compute_spread(budget, start_date, end_date)
            total = sum((amt for _day, amt in allocations), Decimal("0"))
            if total != budget:
                # Defensive: compute_spread guarantees this; a mismatch is a bug.
                raise MediaPlanStateError(
                    "Invariant d'allocation rompu : la somme diffère du budget."
                )
            for day, amount in allocations:
                cur.execute(
                    """
                    INSERT INTO app.plan_allocation_daily
                        (version_id, line_id, day, amount)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (version_id, line_id, day, amount),
                )

        # (d) atomic pointer flip: demote the current active version FIRST.
        cur.execute(
            """
            UPDATE app.media_plan_versions
            SET is_active = false
            WHERE plan_id = %s AND is_active AND id <> %s
            """,
            (plan_id, version_id),
        )
        # (c) + promote this version to published + active.
        try:
            cur.execute(
                """
                UPDATE app.media_plan_versions
                SET status = 'published', is_active = true
                WHERE id = %s
                RETURNING id, plan_id, version_number, status, is_active,
                          source_note, created_by, created_at
                """,
                (version_id,),
            )
        except psycopg.errors.UniqueViolation as exc:
            # Concurrent first-publish race (F-1): the partial unique index
            # (plan_id) WHERE is_active blocks the loser -- surface a clean 409.
            raise MediaPlanStateError(
                "Publication concurrente sur ce plan, veuillez réessayer."
            ) from exc
        version = _row_to_dict(cur.fetchone(), _VERSION_COLS)

        insert_audit_row(
            conn,
            identity=published_by,
            action=ACTION_MEDIA_PLAN_VERSION_PUBLISHED,
            provider_account="platform",
            connection_ref="",
            metadata={
                "plan_id": plan_id,
                "version_id": version_id,
                "version_number": version_number,
            },
        )

    # Story 22.3: recompute mapping orphan status against the NEW active version,
    # in THIS transaction, AFTER the pointer flip. A line dropped from the newly
    # published version has its mappings flagged 'orphaned'; a line that came back
    # is reactivated. Local import avoids the mediaplan_store<->mediaplan_mapping
    # import cycle (mapping imports the typed errors from this module).
    from core.mediaplan_mapping import refresh_orphan_status  # noqa: PLC0415

    refresh_orphan_status(conn, plan_id=plan_id)

    return version


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

_DIFF_FIELDS = ("budget", "start_date", "end_date", "label", "channel")


def diff_versions(
    conn: Any, *, version_a: str, version_b: str
) -> dict[str, Any]:
    """Diff two versions by line_key.

    Returns a serialisable structure:
      {
        "added":   [line, ...],           # in B, not in A
        "removed": [line, ...],           # in A, not in B
        "changed": [{"line_key": ..., "changes": {field: {"from":.., "to":..}}}],
      }
    Compared fields: budget, start_date, end_date, label, channel.
    """
    lines_a = {ln["line_key"]: ln for ln in list_lines(conn, version_id=version_a)}
    lines_b = {ln["line_key"]: ln for ln in list_lines(conn, version_id=version_b)}

    keys_a = set(lines_a)
    keys_b = set(lines_b)

    added = [lines_b[k] for k in sorted(keys_b - keys_a)]
    removed = [lines_a[k] for k in sorted(keys_a - keys_b)]

    changed: list[dict[str, Any]] = []
    for key in sorted(keys_a & keys_b):
        a = lines_a[key]
        b = lines_b[key]
        field_changes: dict[str, dict[str, Any]] = {}
        for field in _DIFF_FIELDS:
            if a.get(field) != b.get(field):
                field_changes[field] = {"from": a.get(field), "to": b.get(field)}
        if field_changes:
            changed.append({"line_key": key, "changes": field_changes})

    return {"added": added, "removed": removed, "changed": changed}
