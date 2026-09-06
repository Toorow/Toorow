"""The third level of the media-plan match: which PLACEMENTS a plan line bought.

Story 61.1. Pure business logic over a psycopg connection, on the pattern of
`core.mediaplan_mapping`: the caller owns the transaction lifecycle.

WHAT A PLACEMENT IS HERE -- arbitrage A3, and it is not a new vocabulary.
A placement is the pair `(breakdown_dimension, breakdown_value)` of
`fact_daily_kpi`, exactly the shape migration 041 already uses for
`campaign_ref` ("fact_daily_kpi.breakdown_value WHERE
breakdown_dimension='campaign_id'"). The dimension travels beside the value
because it differs per connector, and it is read from the module manifests --
files, not rows -- so no second store of vocabulary is created. Measured over
the 39 manifests of `server/modules` on 2026-08-09, exactly TWO declare a
placement dimension in `canonical_dimension_mapping`: `cm360` (`placement_id`)
and `x-ads` (`placement`). The other 37 declare none, and nothing may be
attached on them.

WHAT THIS MODULE REFUSES, AND IT IS THE ONE GUARD THE DATABASE CANNOT MAKE.
Migration 244 enforces the parent row, the uniqueness and the status vocabulary
and nothing else -- a CHECK cannot read a manifest. So `attach_placement` is the
only place that answers "is this dimension one this connector declares", and it
refuses when it is not. Free-text placement identities were option (c) of
arbitrage A3 and were refused for having no proof of observation behind them.

NO WEIGHT, AND THEREFORE NO SECOND VENTILATION. The money of a campaign is
ventilated across plan lines by `app.plan_line_mappings.split_weight`, under the
aggregate invariant `SUM(split_weight) = 1.0` per (plan_id, connector,
campaign_ref). Nothing here carries a weight, no mart reads this table, and
attaching or detaching a placement changes no number anywhere.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.audit import (
    declare_action,
    insert_audit_row,
)
from core.mediaplan_store import (
    MediaPlanNotFoundError,
    MediaPlanValidationError,
)

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_MEDIA_PLAN_PLACEMENT_ATTACHED = declare_action("media_plan.placement.attached")
ACTION_MEDIA_PLAN_PLACEMENT_DETACHED = declare_action("media_plan.placement.detached")


logger = logging.getLogger(__name__)

#: The canonical dimension targets that MEAN "a placement", measured from the 39
#: manifests rather than chosen. `cm360` maps `placement_id -> placement_id` and
#: `x-ads` maps `placement -> placement`; no other manifest carries either. A
#: substring test (`"placement" in target`) would have swept in any future
#: `placement_group` or `placement_type` and matched them silently, so the
#: vocabulary is closed and this constant is what a later connector is added to.
PLACEMENT_CANONICAL_TARGETS: tuple[str, ...] = ("placement", "placement_id")

#: Why a connector has nothing to attach. Not an error, not an emptiness: 37 of
#: the 39 connectors are in this state permanently, and the surface says it.
NO_PLACEMENT_DIMENSION_REASON = (
    "This connector declares no placement dimension in its manifest, so no placement "
    "can be observed on it and none can be attached. Only 2 of the 39 connectors "
    "declare one."
)

#: The status vocabulary of migration 244, mirrored. The same two words
#: `app.plan_line_mappings` uses, so a reader of the two tables reads one word.
PLACEMENT_STATUSES: tuple[str, ...] = ("active", "orphaned")


class PlacementValidationError(MediaPlanValidationError):
    """A caller-supplied placement is one this connector cannot carry (422)."""

    code = "invalid_placement"


class PlacementNotFoundError(MediaPlanNotFoundError):
    """The attachment, or the (line, campaign) it would hang from, does not exist (404)."""

    code = "placement_not_found"


# ---------------------------------------------------------------------------
# Which dimension a connector calls a placement -- read from the manifests.
# ---------------------------------------------------------------------------

#: `{connector -> {"dimension": canonical, "source_field": provider field}}`,
#: computed once. The manifests are files on disk that never change inside a
#: process, and this is read on every load of the Placements tab; re-parsing 39
#: JSON files per request is a cost nobody agreed to pay.
_PLACEMENT_INDEX: dict[str, dict[str, dict[str, str]]] = {}


def _modules_dir(modules_dir: str | Path | None) -> Path:
    if modules_dir is not None:
        return Path(modules_dir)
    return Path(__file__).resolve().parents[1] / "modules"


def placement_dimension_index(
    modules_dir: str | Path | None = None,
) -> dict[str, dict[str, str]]:
    """`{connector -> {"dimension", "source_field"}}` for every connector that declares one.

    Built from `dimension_lineage.read_manifest_dimension_mappings`, which is the
    module that already reads `canonical_dimension_mapping` as data and already
    keys on the module DIRECTORY name -- the same identity
    `app.datastreams.module_name` and `fact_daily_kpi.connector` carry. A second
    manifest reader here would be a second answer to "what does this connector
    declare", and the two would diverge at the first manifest that grows.
    """
    key = str(_modules_dir(modules_dir))
    cached = _PLACEMENT_INDEX.get(key)
    if cached is not None:
        return cached

    from core.dimension_lineage import (  # noqa: PLC0415
        read_manifest_dimension_mappings,
    )

    index: dict[str, dict[str, str]] = {}
    for connector, mapping in read_manifest_dimension_mappings(key).items():
        for source_field, canonical in sorted(mapping.items()):
            if canonical in PLACEMENT_CANONICAL_TARGETS:
                index[connector] = {"dimension": canonical, "source_field": source_field}
                break
    _PLACEMENT_INDEX[key] = index
    return index


def placement_dimension_for(
    connector: str, modules_dir: str | Path | None = None
) -> dict[str, Any]:
    """What this connector calls a placement, or the reason it calls nothing one.

    Never raises and never guesses: an unknown connector and a connector that
    declares no placement dimension are the SAME answer here -- there is nothing
    to observe -- and both carry the reason rather than an empty dict.
    """
    entry = placement_dimension_index(modules_dir).get(str(connector or ""))
    if entry is None:
        return {
            "declared": False,
            "dimension": None,
            "source_field": None,
            "reason": NO_PLACEMENT_DIMENSION_REASON,
        }
    return {
        "declared": True,
        "dimension": entry["dimension"],
        "source_field": entry["source_field"],
        "reason": None,
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

_LIST_SQL = """
    SELECT id, line_key, campaign_ref, breakdown_dimension, breakdown_value,
           status, created_by, created_at
    FROM app.plan_line_placement_mappings
    WHERE plan_id = %s AND connector = %s
    ORDER BY line_key, campaign_ref, breakdown_dimension, breakdown_value
"""


def list_placements(conn: Any, *, plan_id: str, connector: str) -> list[dict[str, Any]]:
    """Every placement attached on this plan FOR THIS CONNECTOR, in one statement.

    Scoped by connector because the tab is (arbitrage A6): a Datastream shows the
    placements of its own connector and of no other, and filtering after reading
    the whole plan would carry another connector's rows across the wire.
    """
    with conn.cursor() as cur:
        cur.execute(_LIST_SQL, (plan_id, connector))
        rows = cur.fetchall()
    return [
        {
            "id": str(row[0]),
            "line_key": row[1],
            "campaign_ref": row[2],
            "breakdown_dimension": row[3],
            "breakdown_value": row[4],
            "status": row[5],
            "created_by": row[6],
            "created_at": row[7],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

_PARENT_SQL = """
    SELECT 1
    FROM app.plan_line_mappings
    WHERE plan_id = %s AND line_key = %s AND connector = %s AND campaign_ref = %s
"""

_ATTACH_SQL = """
    INSERT INTO app.plan_line_placement_mappings
        (plan_id, line_key, connector, campaign_ref,
         breakdown_dimension, breakdown_value, created_by)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (plan_id, line_key, connector, campaign_ref,
                 breakdown_dimension, breakdown_value)
    DO UPDATE SET status = 'active', updated_at = now()
    RETURNING id, (xmax = 0) AS created
"""

_DETACH_SQL = """
    DELETE FROM app.plan_line_placement_mappings
    WHERE id = %s AND plan_id = %s AND connector = %s
    RETURNING line_key, campaign_ref, breakdown_dimension, breakdown_value
"""

#: How many placements the same (line, campaign) already carries. Read BEFORE the
#: delete, because a confirmation that names the count AFTER the deletion names a
#: scope nobody was asked about.
_SIBLING_COUNT_SQL = """
    SELECT COUNT(*)
    FROM app.plan_line_placement_mappings
    WHERE plan_id = %s AND connector = %s AND line_key = %s AND campaign_ref = %s
"""


def attach_placement(
    conn: Any,
    *,
    plan_id: str,
    line_key: str,
    connector: str,
    campaign_ref: str,
    breakdown_dimension: str,
    breakdown_value: str,
    actor: str,
    modules_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Attach one observed placement to a (line, campaign) already matched.

    THREE REFUSALS, EACH FOR A DIFFERENT REPAIR:

      * the connector declares no placement dimension -> nothing here can be
        observed, and inventing an identity was refused by arbitrage A3;
      * the dimension is not the one it declares -> a value stored under the
        wrong dimension can never be matched against a fact again;
      * the (line, campaign) pair carries no mapping -> a placement hanging from
        a campaign this line does not buy is a row with no meaning. The database
        refuses it too, through the foreign key; this refusal exists so the
        caller reads a sentence instead of an integrity error.

    Idempotent by the unique index: attaching twice reactivates rather than
    duplicating, and the return says which of the two happened.
    """
    declared = placement_dimension_for(connector, modules_dir)
    if not declared["declared"]:
        raise PlacementValidationError(
            f"{connector} declares no placement dimension, so no placement can be attached "
            "to a plan line of this connector."
        )
    if str(breakdown_dimension) != declared["dimension"]:
        raise PlacementValidationError(
            f"{connector} observes placements on `{declared['dimension']}`, not on "
            f"`{breakdown_dimension}`."
        )
    if not str(breakdown_value or "").strip():
        raise PlacementValidationError("A placement value is required.")

    with conn.cursor() as cur:
        cur.execute(_PARENT_SQL, (plan_id, line_key, connector, campaign_ref))
        if cur.fetchone() is None:
            raise PlacementNotFoundError(
                f"No mapping attaches campaign {campaign_ref} of {connector} to plan line "
                f"{line_key}, so no placement can hang from it."
            )
        cur.execute(
            _ATTACH_SQL,
            (
                plan_id,
                line_key,
                connector,
                campaign_ref,
                declared["dimension"],
                str(breakdown_value).strip(),
                actor,
            ),
        )
        placement_id, created = cur.fetchone()

    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_MEDIA_PLAN_PLACEMENT_ATTACHED,
        provider_account=connector,
        connection_ref="",
        metadata={
            "plan_id": str(plan_id),
            "line_key": line_key,
            "campaign_ref": campaign_ref,
            "breakdown_dimension": declared["dimension"],
            "breakdown_value": str(breakdown_value).strip(),
            "created": bool(created),
        },
    )
    return {
        "id": str(placement_id),
        "created": bool(created),
        "line_key": line_key,
        "campaign_ref": campaign_ref,
        "breakdown_dimension": declared["dimension"],
        "breakdown_value": str(breakdown_value).strip(),
    }


def count_placements_on_campaign(
    conn: Any, *, plan_id: str, connector: str, line_key: str, campaign_ref: str
) -> int:
    """How many placements this (line, campaign) carries RIGHT NOW.

    Exists so a detach confirmation can name the scope it is about to change with
    the count BEFORE the change. It is a plain count over a table that exists, so
    `0` here is a measurement.
    """
    with conn.cursor() as cur:
        cur.execute(_SIBLING_COUNT_SQL, (plan_id, connector, line_key, campaign_ref))
        return int(cur.fetchone()[0])


def detach_placement(
    conn: Any, *, plan_id: str, connector: str, placement_id: str, actor: str
) -> dict[str, Any]:
    """Detach one placement, by its own identifier.

    SCOPED BY plan AND connector in the WHERE clause, not only by `id`. The
    identifier alone would let an attachment of another Project's plan be removed
    by anyone who guessed a UUID; the two extra predicates are what makes the
    Project scope of the route real at the row.
    """
    with conn.cursor() as cur:
        cur.execute(_DETACH_SQL, (placement_id, plan_id, connector))
        row = cur.fetchone()
    if row is None:
        raise PlacementNotFoundError("This placement attachment does not exist on this plan.")

    line_key, campaign_ref, dimension, value = row
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_MEDIA_PLAN_PLACEMENT_DETACHED,
        provider_account=connector,
        connection_ref="",
        metadata={
            "plan_id": str(plan_id),
            "line_key": line_key,
            "campaign_ref": campaign_ref,
            "breakdown_dimension": dimension,
            "breakdown_value": value,
        },
    )
    return {
        "id": str(placement_id),
        "line_key": line_key,
        "campaign_ref": campaign_ref,
        "breakdown_dimension": dimension,
        "breakdown_value": value,
    }


__all__ = [
    "NO_PLACEMENT_DIMENSION_REASON",
    "PLACEMENT_CANONICAL_TARGETS",
    "PLACEMENT_STATUSES",
    "PlacementNotFoundError",
    "PlacementValidationError",
    "attach_placement",
    "count_placements_on_campaign",
    "detach_placement",
    "list_placements",
    "placement_dimension_for",
    "placement_dimension_index",
]
