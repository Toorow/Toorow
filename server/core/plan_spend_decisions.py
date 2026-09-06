"""Accepting spend a media plan never covered -- story 61.2, arbitrage A3 (a).

THE GESTURE THAT HAD NO STORE. « Permet : sur une dépense hors plan, la
rattacher **ou** l'accepter comme telle » (`epic-61:103-104`). Re-attaching has a
path since story 22.3; accepting had none, and measured before this module was
written it could not have had one: `mediaplan_mapping.list_unmapped_actuals`
DERIVES that spend from the warehouse at every read, so there is no row to
decorate, and the 10 `ACTION_MEDIA_PLAN_*` actions of `core/audit.py` carry no
acceptance.

THE MODULE IS NAMED AT THE CAMPAIGN GRAIN AND SO IS THE TABLE -- arbitrage A4,
and it is an amendment to the plan. `epic-61:36` calls the fourth state
`Unmapped source placement`; the word `placement` there is wrong, and the
measurement is `list_unmapped_actuals` itself, which returns
`{connector, campaign_ref, spend, reason}`. The placement reading beside it
(`plan_line_placements` / `warehouse.query_breakdown_values`) carries a
`row_count`, not a spend. A decision offered per placement could not state the
amount it is about.

WHAT IT NEVER TOUCHES. No amount, no weight, no ventilation. This table is
absent from `mirror_sync.py`'s explicit table list, so no dbt model can reference
it, and `dbt/models/marts/plan_vs_actual_daily.sql` keeps reading
`mirror.plan_line_mappings WHERE status = 'active'` and nothing else. An accepted
campaign STAYS in the out-of-plan panel with its spend: accepting says "this
spend was not planned, and we know", never "this spend was planned after all".

THE FIRST DECISION IS THE ONE THAT STANDS. The insert is
`ON CONFLICT DO NOTHING`, and a second acceptance returns the row already there
with its original author and date. Overwriting them would let the second clicker
become the person who decided, which is the one thing a dated human act must not
allow.
"""

from __future__ import annotations

import logging
from typing import Any

from core.audit import declare_action, insert_audit_row
from core.mediaplan_store import MediaPlanNotFoundError, MediaPlanValidationError
from core.plan_matching_states import MATCHING_STATE_ACCEPTED

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_MEDIA_PLAN_SPEND_ACCEPTED = declare_action("media_plan.spend.accepted")


logger = logging.getLogger(__name__)

#: The longest reason accepted. A bound rather than none, for the same motive
#: `cleanup_rules` bounds its pattern: an unbounded free-text column that reaches
#: a screen is a column somebody eventually pastes a document into.
MAX_REASON_LENGTH = 500

#: Said when a reason is missing. An acceptance with no reason is
#: indistinguishable from a row somebody clicked past, and the database refuses
#: it too (`CHECK (btrim(reason) <> '')`); this exists so the caller reads a
#: sentence instead of an integrity error.
REASON_REQUIRED_MESSAGE = (
    "Accepting spend as unplanned requires a reason: it is what makes the row a decision "
    "rather than a click."
)


class SpendDecisionValidationError(MediaPlanValidationError):
    """The acceptance is missing what makes it one (422)."""

    code = "invalid_spend_decision"


class SpendDecisionNotFoundError(MediaPlanNotFoundError):
    """The plan this acceptance names is not this Project's, or is not there (404)."""

    code = "plan_not_found"


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

_LIST_SQL = """
    SELECT campaign_ref, reason, decided_by, decided_at
    FROM app.plan_unmatched_spend_decisions
    WHERE plan_id = %s AND connector = %s
    ORDER BY campaign_ref
"""


def list_decisions(conn: Any, *, plan_id: str, connector: str) -> dict[str, dict[str, Any]]:
    """`{campaign_ref -> decision}` for this plan and THIS connector, in one statement.

    Keyed by `campaign_ref` because that is how the caller uses it: the
    out-of-plan panel is a derivation the warehouse produces, and this is the
    annotation joined onto it in memory. Scoped by connector for the reason the
    whole tab is (arbitrage A6 of story 61.1): another connector's decisions have
    no business on this Datastream's wire.
    """
    with conn.cursor() as cur:
        cur.execute(_LIST_SQL, (plan_id, connector))
        rows = cur.fetchall()
    return {
        str(row[0]): {
            "campaign_ref": str(row[0]),
            "reason": row[1],
            "decided_by": row[2],
            "decided_at": row[3],
        }
        for row in rows
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

#: The plan must be THIS Project's. `plan_id` arrives in the request body, and
#: without this read a member of one Project could write a decision onto another
#: Project's plan from inside their own Datastream's address.
_PLAN_IN_PROJECT_SQL = """
    SELECT 1
    FROM app.media_plans
    WHERE id = %s AND project_id = %s AND archived_at IS NULL
"""

_ACCEPT_SQL = """
    INSERT INTO app.plan_unmatched_spend_decisions
        (plan_id, connector, campaign_ref, reason, decided_by)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (plan_id, connector, campaign_ref) DO NOTHING
    RETURNING id, reason, decided_by, decided_at
"""

_EXISTING_SQL = """
    SELECT id, reason, decided_by, decided_at
    FROM app.plan_unmatched_spend_decisions
    WHERE plan_id = %s AND connector = %s AND campaign_ref = %s
"""


def plan_belongs_to_project(conn: Any, *, plan_id: str, project_id: str) -> bool:
    """Is this plan a live plan of this Project? One statement, no exception.

    Exported because three write paths of the `Placements` tab need the same
    answer -- attaching a placement, detaching one and accepting spend all take a
    `plan_id` from the caller -- and three spellings of one scope check is how one
    of them ends up not having it.
    """
    with conn.cursor() as cur:
        cur.execute(_PLAN_IN_PROJECT_SQL, (str(plan_id or ""), str(project_id or "")))
        return cur.fetchone() is not None


def accept_unmatched_spend(
    conn: Any,
    *,
    project_id: str,
    plan_id: str,
    connector: str,
    campaign_ref: str,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    """Accept one campaign's out-of-plan spend as unplanned. Idempotent.

    THREE REFUSALS, EACH FOR A DIFFERENT REPAIR:

      * the plan is not this Project's -- refused as NOT FOUND, and with the same
        sentence a plan that never existed gets: telling a caller that a plan
        exists elsewhere is telling them something about another Project;
      * a campaign reference or a connector is missing -- the key
        `(plan_id, connector, campaign_ref)` is the whole identity of the
        decision, and a decision keyed on a blank is a decision about everything;
      * the reason is blank or over `MAX_REASON_LENGTH`.

    Returns the decision, and `created` says whether this call wrote it. The
    second acceptance of the same spend returns the FIRST one, author and date
    included.
    """
    plan_id = str(plan_id or "").strip()
    connector = str(connector or "").strip()
    campaign_ref = str(campaign_ref or "").strip()
    reason = str(reason or "").strip()
    actor = str(actor or "").strip()

    if not plan_id or not connector or not campaign_ref:
        raise SpendDecisionValidationError(
            "A plan, a connector and a campaign are required to accept spend as unplanned."
        )
    if not reason:
        raise SpendDecisionValidationError(REASON_REQUIRED_MESSAGE)
    if len(reason) > MAX_REASON_LENGTH:
        raise SpendDecisionValidationError(
            f"A reason is at most {MAX_REASON_LENGTH} characters; this one is {len(reason)}."
        )
    if not actor:
        raise SpendDecisionValidationError("An acceptance carries the identity that took it.")

    if not plan_belongs_to_project(conn, plan_id=plan_id, project_id=project_id):
        raise SpendDecisionNotFoundError("This media plan does not exist on this Project.")

    with conn.cursor() as cur:
        cur.execute(_ACCEPT_SQL, (plan_id, connector, campaign_ref, reason, actor))
        row = cur.fetchone()
        created = row is not None
        if row is None:
            cur.execute(_EXISTING_SQL, (plan_id, connector, campaign_ref))
            row = cur.fetchone()
    if row is None:  # pragma: no cover -- the unique index would have to disagree with itself
        raise SpendDecisionNotFoundError("This acceptance could not be read back after writing it.")

    decision_id, stored_reason, decided_by, decided_at = row
    # THE JOURNAL RECORDS THE ACT, NOT THE CLICK. `created` travels in the
    # metadata so a second acceptance is legible as what it was -- somebody
    # re-taking a decision already taken -- instead of looking like two decisions.
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_MEDIA_PLAN_SPEND_ACCEPTED,
        provider_account=connector,
        connection_ref="",
        metadata={
            "plan_id": plan_id,
            "campaign_ref": campaign_ref,
            "reason": stored_reason,
            "created": bool(created),
            "decided_by": decided_by,
        },
    )
    return {
        "id": str(decision_id),
        "created": bool(created),
        "state": MATCHING_STATE_ACCEPTED,
        "plan_id": plan_id,
        "connector": connector,
        "campaign_ref": campaign_ref,
        "reason": stored_reason,
        "decided_by": decided_by,
        "decided_at": decided_at,
    }


__all__ = [
    "MAX_REASON_LENGTH",
    "REASON_REQUIRED_MESSAGE",
    "SpendDecisionNotFoundError",
    "SpendDecisionValidationError",
    "accept_unmatched_spend",
    "list_decisions",
    "plan_belongs_to_project",
]
