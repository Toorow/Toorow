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
    declare_action,
    insert_audit_row,
)
from core.reshape import CENT as _CENT
from core.reshape import ReshapeValidationError
from core.reshape import compute_spread as _reshape_compute_spread

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_MEDIA_PLAN_CREATED = declare_action("media_plan.created")
ACTION_MEDIA_PLAN_VERSION_CREATED = declare_action("media_plan.version.created")
ACTION_MEDIA_PLAN_VERSION_PUBLISHED = declare_action("media_plan.version.published")


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


class MediaPlanAmountError(MediaPlanValidationError):
    """The amount a caller sent is not the amount that would land (maps to 422).

    THE MONEY INVARIANT OF A CANDIDATE, AND IT HAS EXACTLY ONE IMPLEMENTATION.
    The file path proves `file total == landed + rejected` before it lands
    (`file_source_resolution.prepare_for_landing`, `amount_reconciliation_failed`)
    precisely so that no plan line lands money the source did not carry. Below
    that check, `_parse_budget` used to `quantize(_CENT)` in silence: a row
    carrying 10000.005 landed 10000.00 and NOTHING said so -- on BOTH doors, the
    file one included, because the store is where the quantization happens.

    A rounded amount is the same defect the file-side invariant exists to refuse,
    one layer lower. It is refused here, in the ONE seam both doors go through
    (`create_version_with_lines`), so the JSON door and `run_import`'s
    plan-store landing refuse it with the same code and the same sentence -- which
    `test_mediaplan_candidate_seam.py` asserts by comparing the two messages
    character for character rather than by trusting this note.
    """

    code = "amount_not_to_the_cent"


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
                f"Field '{field}' must be an ISO date (YYYY-MM-DD)."
            ) from exc
    raise MediaPlanValidationError(
        f"Field '{field}' must be an ISO date (YYYY-MM-DD)."
    )


def _parse_budget(value: Any) -> Decimal:
    try:
        budget = Decimal(str(value))
    except Exception as exc:
        raise MediaPlanValidationError(
            "The budget must be a decimal number."
        ) from exc
    if not budget.is_finite():
        raise MediaPlanValidationError("The budget must be a finite number.")
    if budget < 0:
        raise MediaPlanValidationError("The budget cannot be negative.")
    # Normalise to the cent so storage and spread agree exactly -- and REFUSE
    # rather than round. `quantize` alone turned 10000.005 into 10000.00 and
    # returned it as if the caller had sent that: the version then carried money
    # the source never declared, which is the very thing the file path's
    # `file total == landed + rejected` invariant exists to make impossible.
    # Value equality, not exponent equality: "100.5" and "100.50" are the same
    # money and only a genuine sub-cent digit refuses.
    landed = budget.quantize(_CENT)
    if landed != budget:
        raise MediaPlanAmountError(
            f"The amount {value} is not an exact number of cents: it would land "
            f"as {format(landed, 'f')}. Send the amount to the cent."
        )
    return landed


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
    # WHICH DATASTREAM CARRIES THIS PLAN (migration 303, ratified 2026-08-24).
    # It travels on EVERY plan read and not only on the creation reply: the
    # console follows it to open the carrier's Workbench, and a link that only
    # existed on the reply to the write would be unreadable to every screen that
    # did not perform the write.
    "carrier_datastream_id",
]


class MediaPlanCarrierTakenError(MediaPlanStateError):
    """This Datastream already carries a live plan (maps to 409).

    ITS OWN TYPE because it is the ONE refusal the ratified relation produces,
    and it is not a validation fault: the caller asked for something coherent and
    the answer is that the carrier is taken. The sentence names the gesture that
    repairs -- create the second plan on a second Datastream -- because "each plan
    on its own carrier" is precisely what makes a second plan possible.
    """

    code = "carrier_already_carries_a_plan"


def create_plan(
    conn: Any,
    *,
    project_id: str,
    name: str,
    currency: str = "EUR",
    created_by: str,
    carrier_datastream_id: str | None = None,
) -> dict[str, Any]:
    """Create a media plan (no version yet), on the Datastream that carries it.

    ``carrier_datastream_id`` is the file-source Datastream whose Workbench hosts
    this plan (ratified 2026-08-24). It is OPTIONAL at this seam and not at the
    console's: the older ``POST /api/projects/{id}/mediaplans`` contract predates
    the decision and its callers must keep working, while the Workbench always
    sends one. Nothing here provisions a Datastream -- the carrier is a
    Datastream a person already created, and a plan whose carrier is absent is a
    plan that arrived by the older path, said as such rather than repaired by
    inventing one.

    Raises ``MediaPlanCarrierTakenError`` when that Datastream already carries a
    live plan: one carrier holds one plan, and a second plan of the same project
    lives on a second Datastream with its own template.
    """
    if not isinstance(name, str) or not name.strip():
        raise MediaPlanValidationError("The plan name cannot be empty.")
    clean_name = name.strip()
    clean_currency = (currency or "EUR").strip() or "EUR"
    carrier = (carrier_datastream_id or "").strip() or None

    # ASKED BEFORE THE INSERT, so the refusal is a sentence and not a constraint
    # violation surfacing as a 500. The unique index stays the authority -- two
    # concurrent creations still meet it -- and this read is what lets the answer
    # name the plan already sitting there.
    if carrier is not None:
        existing = get_carrier_plan(conn, datastream_id=carrier)
        if existing is not None:
            raise MediaPlanCarrierTakenError(
                f"This Datastream already carries the media plan "
                f"'{existing['name']}'. A second plan of this project is created "
                f"on a second file-source Datastream, with its own template."
            )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.media_plans
                (project_id, name, currency, created_by, created_at, updated_at,
                 carrier_datastream_id)
            VALUES (%s, %s, %s, %s, now(), now(), %s)
            RETURNING id, project_id, name, currency, created_by,
                      created_at, updated_at, archived_at, carrier_datastream_id
            """,
            (project_id, clean_name, clean_currency, created_by, carrier),
        )
        row = cur.fetchone()
        plan = _row_to_dict(row, _PLAN_COLS)

        insert_audit_row(
            conn,
            identity=created_by,
            action=ACTION_MEDIA_PLAN_CREATED,
            provider_account="platform",
            connection_ref="",
            metadata={
                "plan_id": plan["id"],
                "project_id": project_id,
                "name": clean_name,
                "carrier_datastream_id": carrier,
            },
        )
    return plan


def list_plans(conn: Any, *, project_id: str) -> list[dict[str, Any]]:
    """List (non-archived) plans of a project, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, name, currency, created_by,
                   created_at, updated_at, archived_at, carrier_datastream_id
            FROM app.media_plans
            WHERE project_id = %s AND archived_at IS NULL
            ORDER BY created_at DESC
            """,
            (project_id,),
        )
        return [_row_to_dict(r, _PLAN_COLS) for r in cur.fetchall()]


def get_carrier_plan(conn: Any, *, datastream_id: str) -> dict[str, Any] | None:
    """The live plan this Datastream carries, or ``None``.

    ONE ROW BY CONSTRUCTION (`uq_media_plans_carrier_datastream`): a carrier holds
    one live plan. `LIMIT 1` is not a tie-break, it is the shape the index already
    guarantees, kept explicit so a reader does not have to go and check.
    """
    clean = (datastream_id or "").strip()
    if not clean:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, name, currency, created_by,
                   created_at, updated_at, archived_at, carrier_datastream_id
            FROM app.media_plans
            WHERE carrier_datastream_id = %s AND archived_at IS NULL
            LIMIT 1
            """,
            (clean,),
        )
        row = cur.fetchone()
    return _row_to_dict(row, _PLAN_COLS) if row else None


def list_carrier_datastreams(conn: Any, *, project_id: str) -> list[dict[str, Any]]:
    """The file-source Datastreams of this project able to carry a media plan.

    A CARRIER IS A DATASTREAM WHOSE TEMPLATE SAYS SO -- `landing_target =
    'plan_store'`, read from the sealed contract `import_runner` routes on. It is
    not a naming convention and not a flag of its own: one declaration, one
    answer, and a screen that offered a door to a Datastream the import would
    refuse would be the dead door this decision was taken to end.

    Each row says whether it already carries a plan, because that is what decides
    which gesture the door leads to: an empty carrier is where a plan is CREATED,
    a taken one is where its next dated revision is imported.

    Returns ``[]`` -- never raises -- when the project has no file source at all;
    the caller then names the gesture that makes one rather than a broken door.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, current_mapping_version_id
            FROM app.datastreams
            WHERE project_id = %s
              AND source_kind = 'managed_feed'
              AND archived_at IS NULL
            ORDER BY name
            """,
            (project_id,),
        )
        rows = cur.fetchall()

    from core.file_source_resolution import (  # noqa: PLC0415
        resolve_file_source_producer,
    )
    from core.file_source_template import LANDING_PLAN_STORE  # noqa: PLC0415

    carriers: list[dict[str, Any]] = []
    for row in rows:
        datastream_id = str(row[0])
        # NO PRIVATE SWALLOW IN THE LOOP. A failed statement aborts the whole
        # Postgres transaction, so a `continue` past an exception would keep
        # reading a connection that answers nothing -- and hand back a SHORTER
        # list of carriers than the project has, which is a door quietly missing
        # rather than an error. `resolve_file_source_producer` returns `None`
        # for an absent or unreadable binding, which is the case that matters.
        producer = resolve_file_source_producer(
            conn,
            project_id=project_id,
            datastream_id=datastream_id,
            mapping_version_id=row[2],
        )
        contract = (getattr(producer, "template", None) or {}).get("contract") or {}
        if contract.get("landing_target") != LANDING_PLAN_STORE:
            continue
        plan = get_carrier_plan(conn, datastream_id=datastream_id)
        carriers.append(
            {
                "datastream_id": datastream_id,
                "name": str(row[1]),
                "plan_id": plan["id"] if plan else None,
                "plan_name": plan["name"] if plan else None,
            }
        )
    return carriers


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
        raise MediaPlanValidationError("The version must contain at least one row.")

    seen: set[str] = set()
    normalised: list[dict[str, Any]] = []
    for idx, raw in enumerate(lines):
        if not isinstance(raw, dict):
            raise MediaPlanValidationError("Each row must be an object.")

        line_key = (raw.get("line_key") or "").strip()
        if not line_key:
            raise MediaPlanValidationError(
                "The row key (line_key) cannot be empty."
            )
        if line_key in seen:
            raise MediaPlanValidationError(
                f"Row key '{line_key}' is duplicated in the version."
            )
        seen.add(line_key)

        start = _parse_date(raw.get("start_date"), field="start_date")
        end = _parse_date(raw.get("end_date"), field="end_date")
        if start > end:
            raise MediaPlanValidationError(
                "The start date must precede the end date."
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

    THE ONE SEAM ONTO A CANDIDATE, AND THERE ARE TWO DOORS ONTO IT (2026-08-31,
    AI-331). The file door is `import_runner.run_import` ->
    `import_landing.land_plan_store_rows`; the JSON door is
    `POST /api/mediaplans/{plan_id}/versions`. Everything that governs the shape
    of a version -- the zero-line refusal, the duplicate-key refusal, the date
    order, the money invariant, the audit row -- lives HERE and not in either
    caller, so the two doors cannot drift into two rules. What the file door adds
    on top of this seam is the ledger, the rejection gate and the drift gate:
    those govern a FILE (a content hash, a rejected-row ratio, a confirmed
    mapping), and a JSON body is not a file.
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
                "Concurrent change on the plan, please try again."
            ) from exc
        version = _row_to_dict(cur.fetchone(), _VERSION_COLS)
        version_id = version["id"]

        # PUBLICATION IS NEVER A SIDE EFFECT OF CREATION. The INSERT above writes
        # 'candidate'/false literally, so this reads the row BACK from the
        # database rather than re-asserting the tuple this function just built --
        # a guard that reads its own copy proves nothing. What it catches is the
        # only way a candidate could ever come out published without anyone
        # deciding to: a column default, a trigger or a migration that changes
        # what the write means. It fails closed, inside the caller's transaction,
        # so nothing is committed.
        cur.execute(
            "SELECT status, is_active FROM app.media_plan_versions WHERE id = %s",
            (version_id,),
        )
        stored_status, stored_active = cur.fetchone()
        if stored_status != "candidate" or stored_active:
            raise MediaPlanStateError(
                "A new version must be created as a candidate; publication is a "
                "separate act."
            )

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
                # THE MONEY, IN THE AUDIT ROW. A line count says a version was
                # created; it does not say what it was worth, so a candidate that
                # changed a plan's budget left no readable trace of by how much.
                # Written by BOTH doors, because it is written by the seam.
                "total_budget": format(
                    sum((line["budget"] for line in normalised), Decimal("0.00")),
                    "f",
                ),
                "created_status": "candidate",
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
                "Cannot publish a version of an archived plan."
            )
        if status != "candidate":
            raise MediaPlanStateError("Only a candidate version can be published.")

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
                "Cannot publish a version with no row."
            )

        # (a) + (b): materialise + invariant check per line.
        for line_id, budget, start_date, end_date in line_rows:
            allocations = compute_spread(budget, start_date, end_date)
            total = sum((amt for _day, amt in allocations), Decimal("0"))
            if total != budget:
                # Defensive: compute_spread guarantees this; a mismatch is a bug.
                raise MediaPlanStateError(
                    "Allocation invariant broken: the sum differs from the budget."
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
                "Concurrent publication on this plan, please try again."
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
