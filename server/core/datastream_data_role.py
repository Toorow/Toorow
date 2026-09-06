"""Changing a Datastream's data role, governed — amendment 9 of the 2026-08-11 review.

THE AMENDMENT, in the words that ratified it
(`docs/product-architecture/datastream-workbench-and-wizard.md`):

  « **Le rôle et le mode s'éditent depuis le Workbench**, par le même changement
  gouverné que le reste : un changement préparé, une confirmation qui nomme ce
  qui bouge en aval, une version. Ce que la ré-écriture entraîne — un mode qui
  change de source de collecte, un rôle qui change la cascade de frais — se dit
  dans la confirmation avant d'être écrit, jamais après. »

WHAT THE REVIEW MEASURED, AND WHAT RE-MEASURING FOUND (2026-08-31). The review
grepped `UPDATE … SET data_role` over `server/core/` and found one writer: the
`completing` branch of `datastream_activation`. That grep is right and its
conclusion — "aucune porte du produit ne peut plus les changer" — is not:
`core.datastreams.update_datastream` lists `data_role` among its allowed fields
and builds its `SET` clause at runtime, so no literal exists for a grep to find,
and `PATCH /api/datastreams/{id}` has passed the whole body to it all along.

So the door was open and UNGOVERNED, which is worse than the closed door the
review described. Anybody could move a Datastream from `Spend` to `Context` with
one field, with no base stated, no refusal when somebody else had just moved it,
and no sentence anywhere naming what the move does to the fee ladder. This
module is that door, governed, and the PATCH seam now refuses the ungoverned one.

WHAT « CE QUI BOUGE EN AVAL » ACTUALLY IS, measured rather than asserted. The
role is the second member of the pair `fee_tax_source_types.derive_source_type`
resolves a `source_type` from; the first is the connector manifest's
`public_catalog.category`. Only five pairs agree
(`_AGREEMENT`); every other pair is `UNKNOWN`, and `UNKNOWN` is EXCLUDED from
the cost cascade. So a role change can silently take a Datastream out of the
cost ladder, or put it back in, and that is the fact the confirmation states —
computed for every offered role, before the click, from the same table the
ladder itself reads.

WHAT THIS MODULE DOES NOT CHANGE, and the doc carries the arbitrage. The mode
(`source_kind`) and the connector (`module_name`) are the other half of amendment
9 and they are NOT delivered here. Measured while building this:
`datastream_change._append_plan_version` refuses a proposal whose
`source.kind` differs from the base version's, by name — "Processing changes
cannot change source ownership" — so re-sourcing a Datastream is a new change
KIND with its own review of what a collected relation becomes, not a column
write. Writing `app.datastreams.source_kind` without it would leave every landed
day, every plan version and every mart key describing a mode the row no longer
claims.
"""

from __future__ import annotations

import logging
from typing import Any

from core.datastreams import DATA_ROLES
from core.fee_tax_source_types import (
    UNKNOWN,
    derive_source_type,
    exclusion_reason_for,
    is_in_cost_cascade,
)

logger = logging.getLogger(__name__)


class DataRoleChangeRefused(ValueError):
    """A governed role change that must not be written, with the reason a person reads."""

    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


#: What a caller must send back for the change to be governed. Named on the wire
#: rather than implied: it is the value the caller READ, and the whole point of
#: the field is that stating it is a claim about what was on screen.
EXPECTED_FIELD = "expected_data_role"

#: The absence of a role, on the wire. `app.datastreams.data_role` is NULLable
#: and 093's regex backfill wrote `Operational` into every row it could not
#: place, so "no role" and "a role" are two different readings and the empty
#: string is how the first one is stated. A caller that omits the field entirely
#: is stating nothing, which is exactly what this door refuses.
NO_ROLE = ""


def _category(conn, *, project_id: str, datastream_id: str) -> tuple[str | None, str]:
    """The manifest category of this Datastream's connector, and why it is absent.

    Fail-soft, like every read of `fee_tax_source_types`: a category that could
    not be read is published as unknown WITH its reason, never as a category. A
    confirmation that guessed the first half of the pair would state a fee-ladder
    consequence nobody measured.
    """
    from core.fee_tax_source_types import fetch_derivation_signals  # noqa: PLC0415

    try:
        category, _role = fetch_derivation_signals(
            conn, datastream_id=datastream_id, project_id=project_id
        )
    except Exception as exc:  # noqa: BLE001 -- a review must not fail on a manifest
        logger.warning("datastream_data_role: category unreadable ds=%s: %s", datastream_id, exc)
        return None, "connector_unreadable"
    if category:
        return category, ""
    return None, "no_module"


#: Why a role offers no fee-ladder consequence at all. Two different absences,
#: because they name two different gestures.
_CATEGORY_ABSENCE = {
    "no_module": (
        "This Datastream declares no connector, so no manifest category exists to "
        "pair the role with and the fee ladder does not read it either way. The "
        "role still decides how this Datastream joins the streams that name its "
        "values."
    ),
    "connector_unreadable": (
        "The connector's manifest could not be read, so what this role does to the "
        "fee ladder cannot be stated. Nothing is guessed here: the change is still "
        "available, and its ladder effect is unknown until the manifest reads."
    ),
}


def read_role_change(
    conn, *, project_id: str, datastream_id: str, record: dict[str, Any]
) -> dict[str, Any]:
    """What a person must be told BEFORE a role is written — the whole review.

    Every option carries its own downstream fact rather than one sentence about
    "the fee ladder": the consequence differs per role because the agreement
    table is a table of PAIRS, and a screen given one generic warning would make
    the person guess which of the seven it applies to.
    """
    current = (record.get("data_role") or "").strip() or None
    category, absence = _category(conn, project_id=project_id, datastream_id=datastream_id)
    before = derive_source_type(category, current)

    options = []
    for role in DATA_ROLES:
        after = derive_source_type(category, role)
        options.append(
            {
                "value": role,
                "source_type": after,
                "in_cost_cascade": is_in_cost_cascade(after),
                # The one sentence that decides whether this move is safe, and it
                # is per role because the pair is what resolves, not the role.
                "effect": _effect(before, after, category, absence),
                "is_current": role == current,
            }
        )

    return {
        "current": current,
        # WHAT THE CALLER MUST SEND BACK. Published beside the value so a console
        # cannot invent it: it is the base of the change, and a base a screen
        # composed itself is not a claim about what was read.
        "expected_data_role": current or NO_ROLE,
        "category": category,
        "category_reason": absence or None,
        "source_type": before,
        "in_cost_cascade": is_in_cost_cascade(before),
        "options": options,
        # THE OTHER HALF OF AMENDMENT 9, and it says what it is rather than
        # nothing. A screen that offered a mode selector here would compose a
        # change no writer executes; one that said nothing would leave the
        # amendment's own bullet unanswered on the screen it names.
        "mode_and_connector": {
            "changeable": False,
            "mode": record.get("source_kind") or "connector_pull",
            "connector": record.get("module_name"),
            "reason": (
                "The mode and the connector are not changed here. Re-sourcing a "
                "Datastream rewrites what every collected day, plan version and "
                "mart key describes, so it is a governed change of its own and "
                "not a field: the change seam refuses a proposal whose source "
                "kind differs from its base, by name."
            ),
            "gesture": (
                "Create the Datastream under the mode and connector it should "
                "have, and archive this one — its history stays readable."
            ),
        },
    }


def _effect(before: str, after: str, category: str | None, absence: str) -> str:
    """What choosing this role does, in the words of the ladder that reads it."""
    if category is None:
        return _CATEGORY_ABSENCE.get(absence, _CATEGORY_ABSENCE["no_module"])
    if after == before:
        return (
            "This is the pairing in force; the fee ladder reads this Datastream as it "
            "does today."
        )
    if after == UNKNOWN:
        return (
            f"« {category} » and this role are not one of the pairs the fee ladder "
            "recognises, so this Datastream would leave the cost cascade: "
            + exclusion_reason_for(after)
        )
    if before == UNKNOWN:
        return (
            f"« {category} » and this role agree, so this Datastream would ENTER "
            f"the cost cascade as {after}. Every cost already collected is read "
            "under that ladder from then on."
        )
    return (
        f"« {category} » and this role resolve to {after} instead of {before}, so "
        "the fee ladder this Datastream's costs are read under changes."
    )


def change_data_role(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    proposed: str | None,
    expected: str | None,
    actor: str,
) -> dict[str, Any]:
    """Write one role, against the base the caller states, under a row lock.

    THE BASE IS CHECKED TWICE, and both readings are needed — the same shape
    `master_data_commands` settled for a rename. Once here under `FOR UPDATE`,
    which is the only reading that can decide a RACE: without the lock two
    changes read the same base, both pass, and the second silently wins. And the
    lock is taken on the Datastream row itself, because that is the row the write
    lands on.

    Raises `DataRoleChangeRefused` and never a bare `ValueError`: each refusal
    names the gesture that repairs it, and a 500 that names nothing is what a
    CHECK-constraint violation would have produced.
    """
    role = (proposed or "").strip() or None
    if role is not None and role not in DATA_ROLES:
        raise DataRoleChangeRefused(
            "That is not one of the roles this product knows: " + ", ".join(DATA_ROLES),
            code="invalid_data_role",
            details={"roles": list(DATA_ROLES)},
        )
    if expected is None:
        raise DataRoleChangeRefused(
            "Send back the role you read before changing it. A change written "
            "without the value it started from cannot tell a stale screen from a "
            "deliberate one.",
            code="expected_data_role_required",
            details={"field": EXPECTED_FIELD},
        )
    stated = (expected or "").strip() or None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_role, source_kind, module_name, archived_at, lifecycle_state"
            "  FROM app.datastreams WHERE id=%s AND project_id=%s FOR UPDATE",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise DataRoleChangeRefused(
            "This Datastream does not exist in this Project.",
            code="not_found",
        )
    stored = (row[0] or "").strip() or None
    if row[3] is not None or str(row[4] or "").lower() == "archived":
        # The same sentence the change seam gives an archived Datastream: it is
        # restored, it is not edited from here.
        raise DataRoleChangeRefused(
            "An archived Datastream is restored before it is changed.",
            code="archived",
        )
    if stored != stated:
        raise DataRoleChangeRefused(
            "This Datastream's role has been changed since you read it — it now "
            f"reads « {stored or 'no role'} ». Reopen it and make the change again "
            "against what is there now.",
            code="stale_data_role",
            details={"current": stored, "stated": stated},
        )
    if stored == role:
        raise DataRoleChangeRefused(
            f"This Datastream already carries « {role or 'no role'} ».",
            code="no_change",
        )

    record_before = {"data_role": stored, "source_kind": row[1], "module_name": row[2]}
    review_before = read_role_change(
        conn, project_id=project_id, datastream_id=datastream_id, record=record_before
    )

    from core.datastreams import update_datastream  # noqa: PLC0415

    updated = update_datastream(datastream_id, project_id, {"data_role": role}, conn)
    if updated is None:
        raise DataRoleChangeRefused(
            "This Datastream does not exist in this Project.",
            code="not_found",
        )

    chosen = next(
        (option for option in review_before["options"] if option["value"] == role),
        None,
    )
    # AUDITED WITH WHAT MOVED, not just that something did: the ladder effect is
    # the whole reason this change is governed, and an audit row that recorded
    # only the two words would not let anybody answer, later, why a cost report
    # changed shape on that day.
    from core.audit import ACTION_DATASTREAM_UPDATED, insert_audit_row  # noqa: PLC0415

    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=ACTION_DATASTREAM_UPDATED,
        provider_account="",
        connection_ref="",
        metadata={
            "datastream_id": datastream_id,
            "project_id": project_id,
            "operation": "datastream_data_role_change",
            "data_role_before": stored,
            "data_role_after": role,
            "source_type_before": review_before["source_type"],
            "source_type_after": (chosen or {}).get("source_type"),
        },
    )
    return {
        "data_role": role,
        "data_role_before": stored,
        "source_type_before": review_before["source_type"],
        "source_type_after": (chosen or {}).get("source_type"),
        "effect": (chosen or {}).get("effect"),
    }
