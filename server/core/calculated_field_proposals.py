"""Story 75-1 -- a calculation found while exploring becomes a governed proposal.

WHAT WAS MEASURED, 2026-09-05, before this module existed. `context_review.py`
carries `PROPOSAL_KINDS = ("business_link",)`: one typed payload, and no way to
offer a calculated field. The authoring of a formula lived only in the
governance workbenches (`ui/admin/src/governance/NewConceptDialog.tsx`), so a
person or a model that found a useful calculation while reading a Result had no
rail back into the shared model.

WHY THIS IS A NEIGHBOUR MODULE AND NOT A FOURTH `node_type`. A fact of the
incumbent schema, not a preference: `app.context_review_requests.node_version`
is NOT NULL and its open-uniqueness index keys on
`(node_type, node_id, node_version)`. A calculation discovered in an exploration
names no Hub node, so filing it there would mean inventing a node to point at
and a version for it. What IS reused, verbatim, is the vocabulary -- `open |
accepted | declined`, `human | agent`, and the rule that rail states at its own
`resolve_request`: ACCEPTING AND APPLYING ARE NOT THE SAME FACT, so what an
acceptance produced is written in `applied_ref`.

THREE PROPERTIES THIS MODULE EXISTS TO GUARANTEE:

1. **Nothing arbitrary is stored.** The expression is a typed tree walked by
   `semantic_expressions.validate_expression` against THIS Project's
   `concept_resolver`: allowlisted operations only, the type and unit inferred
   rather than declared, and every reference an exact `(concept_id,
   version_id)` pin. A `concept_name` leaf parses -- the workbench needs to show
   pending work -- and is refused for a promotion, because a reference that
   follows `latest` is not a reference.
2. **A promotion says where it came from.** `provenance` is NOT NULL and the
   rows it names are verified to belong to this Project in the same
   transaction. A promotion that cannot name the exploration that produced it
   is a guess with better manners.
3. **A machine never publishes.** `resolve(..., status="accepted")` opens a
   semantic change-set and PREPARES it -- diff, used-by impact, Test gate --
   and stops there. The single-use confirmation token `prepare_change_set`
   mints is deliberately dropped on the floor: a person re-prepares and
   confirms on the governance surface.

WHAT A PREPARED CHANGE-SET STILL LACKS, AND WHY THAT IS NOT A DEFECT. A Concept
declares HOW IT AGGREGATES, and `validate_aggregation` refuses
`undeclared_aggregation` when neither an aggregation nor non-additivity is
stated. A proposal carries the formula, not that declaration -- deriving one
from the tree would be choosing on someone's behalf. So the prepared change-set
of a fresh promotion carries that refusal in its validation, visibly, and the
person who confirms declares it in the workbench. `resolve` returns the
refusals for exactly that reason.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ulid import ULID

from core.audit import declare_action, insert_audit_row
from core.context_review import (
    ORIGINS,
    RESOLUTION_STATUSES,
    STATUS_ACCEPTED,
    STATUS_OPEN,
    is_resolution,
    open_status_predicate,
)

logger = logging.getLogger(__name__)

#: Declared here, written here (AD-42). The three acts of the rail, and no
#: fourth: an action nobody declares is indistinguishable from a typo.
ACTION_CALCULATED_FIELD_PROPOSED = declare_action("calculated_field.proposed")
ACTION_CALCULATED_FIELD_RESOLVED = declare_action("calculated_field.resolved")

#: The mark a promoted calculation carries, wherever it is carried.
PROVENANCE_ORIGIN = "exploration"

#: The canonical machine name of a Concept, copied from the CHECK migration 142
#: put on `app.semantic_concepts.name`. It is asserted at PROPOSAL time so a
#: queue entry that could never be confirmed is never filed.
CONCEPT_NAME = re.compile(r"[a-z][a-z0-9_]{0,126}")

#: Only a metric can carry an expression; a dimension has none. Named rather
#: than passed in, because this rail promotes calculations and nothing else.
PROMOTED_KIND = "metric"

_COLUMNS = (
    "id", "org_id", "project_id", "status", "origin", "name", "description",
    "expression", "value_type", "unit", "currency", "dependencies", "provenance",
    "requested_by", "created_at", "resolved_by", "resolved_at", "applied_ref",
)

#: The two provenance keys this rail understands. `result_id` is the usual one
#: -- an exploration IS a Result -- and the query spec version is derived from
#: it when it is not stated, so the pin is exact either way.
_PROVENANCE_KEYS = ("result_id", "query_spec_version_id")


class CalculatedFieldProposalError(ValueError):
    """A refusal that NAMES itself. Never a generic 'invalid proposal'.

    `code` is the word a door repeats to its caller and a screen turns into a
    sentence; `refusals` carries the expression contract's own named reasons
    when the formula is what was refused, so twelve mistakes are learnt in one
    turn instead of twelve.
    """

    def __init__(
        self, code: str, message: str, refusals: list[dict[str, str]] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.refusals = refusals or []

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.refusals:
            body["refusals"] = self.refusals
        return body


class ProposalAlreadyResolvedError(CalculatedFieldProposalError):
    """A second verdict on a closed proposal is a CONFLICT, not an absence --
    the id exists; it is its state that refuses. Same distinction the Hub review
    queue draws with `ReviewRequestAlreadyResolvedError`."""


def _row(row: Any) -> dict[str, Any]:
    return dict(zip(_COLUMNS, row, strict=False))


def _json_load(value: Any) -> Any:
    """psycopg returns jsonb as a Python object already; a str means a driver
    that does not adapt it, and reading both is cheaper than depending on one."""
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return value


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------


def _checked_provenance(
    conn: Any, *, org_id: str, project_id: str, provenance: Any
) -> dict[str, Any]:
    """The exploration this calculation came from, VERIFIED in this Project.

    Reading the Result also PINS the query spec version when the caller did not
    state one: the Result knows which version produced it, and a version the
    caller typed could name a different execution of the same spec.
    """
    if not isinstance(provenance, dict) or not any(
        str(provenance.get(key) or "").strip() for key in _PROVENANCE_KEYS
    ):
        raise CalculatedFieldProposalError(
            "missing_provenance",
            "A promotion names the exploration it came from: a `result_id`, or "
            "the `query_spec_version_id` that was executed.",
        )
    result_id = str(provenance.get("result_id") or "").strip() or None
    spec_version_id = str(provenance.get("query_spec_version_id") or "").strip() or None

    with conn.cursor() as cur:
        if result_id:
            cur.execute(
                """
                SELECT query_spec_version_id FROM app.query_results
                 WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (result_id, org_id, project_id),
            )
            found = cur.fetchone()
            if found is None:
                raise CalculatedFieldProposalError(
                    "unknown_provenance",
                    f"Result {result_id} is not readable in this Project. It may "
                    "not exist, or it may belong elsewhere.",
                )
            if not spec_version_id:
                spec_version_id = str(found[0]) if found[0] else None
        if spec_version_id:
            cur.execute(
                """
                SELECT 1 FROM app.query_spec_versions
                 WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (spec_version_id, org_id, project_id),
            )
            if cur.fetchone() is None:
                raise CalculatedFieldProposalError(
                    "unknown_provenance",
                    f"Query spec version {spec_version_id} is not readable in "
                    "this Project.",
                )

    checked = {key: value for key, value in provenance.items() if key not in _PROVENANCE_KEYS}
    checked["origin"] = PROVENANCE_ORIGIN
    if result_id:
        checked["result_id"] = result_id
    if spec_version_id:
        checked["query_spec_version_id"] = spec_version_id
    return checked


def _analyzed(conn: Any, *, project_id: str, name: str, expression: Any) -> Any:
    """Walk the tree once, and refuse by NAME. The only writer of `expression`."""
    from core.semantic_expressions import validate_expression  # noqa: PLC0415
    from core.semantic_model import concept_resolver  # noqa: PLC0415

    if not isinstance(expression, dict) or not expression:
        raise CalculatedFieldProposalError(
            "empty_expression",
            "A calculated field is a typed expression tree. Executable SQL is "
            "not accepted here, and an empty formula calculates nothing.",
        )
    analysis = validate_expression(
        expression,
        concept_resolver(conn, project_id),
        owning_concept_name=name,
        # A promotion declares no owning type: the type is what the walk
        # INFERS, and a declared one would let a caller call money a count.
        owning_value_type=None,
    )
    if analysis.refusals:
        raise CalculatedFieldProposalError(
            "invalid_expression",
            "This formula is not expressible in the governed contract.",
            [refusal.as_dict() for refusal in analysis.refusals],
        )
    if analysis.unresolved_names:
        raise CalculatedFieldProposalError(
            "unresolved_reference",
            "This formula still refers to "
            + ", ".join(sorted(set(analysis.unresolved_names)))
            + " by name. A reference without a version follows `latest` and is "
            "not a reference: pin the exact Concept versions.",
        )
    if analysis.result is None:
        # Belt to the contract's braces: `ok` is derived from BOTH halves, and a
        # tree that yielded no type without saying why must not be stored as if
        # it had one.
        raise CalculatedFieldProposalError(
            "empty_expression",
            "This formula yielded no type. It calculates nothing that can be "
            "governed.",
        )
    return analysis


def propose(
    conn: Any,
    *,
    org_id: str,
    project_id: str,
    name: str,
    expression: Any,
    provenance: Any,
    origin: str,
    requested_by: str,
    description: str | None = None,
) -> dict[str, Any]:
    """File one calculated-field proposal. ONE transaction, or nothing.

    Every refusal below happens BEFORE the insert, and the insert, the trace row
    and the audit row share a savepoint: a refused piece leaves nothing written.
    """
    clean_name = (name or "").strip()
    if not clean_name:
        raise CalculatedFieldProposalError(
            "missing_name", "A proposed field carries the name it would be known by."
        )
    if len(clean_name) > 120:
        raise CalculatedFieldProposalError(
            "name_too_long", "A field name is at most 120 characters."
        )
    if not CONCEPT_NAME.fullmatch(clean_name):
        # THE SCHEMA'S POLICY, NOT A NEW ONE. `app.semantic_concepts.name`
        # carries `CHECK (name ~ '^[a-z][a-z0-9_]{0,126}$')` since migration 142.
        # A proposal that cannot satisfy it is a proposal nobody could ever
        # confirm, and refusing it here costs one sentence instead of a queue
        # entry that dies at the last step.
        raise CalculatedFieldProposalError(
            "invalid_name",
            "A field name is the canonical name every mapping and view joins "
            "on: lower-case letters, digits and underscores, starting with a "
            "letter (for example `cost_per_click`). Change the description to "
            "change what people read.",
        )
    if origin not in ORIGINS:
        raise CalculatedFieldProposalError(
            "unknown_origin", "origin must be " + " or ".join(f"'{o}'" for o in ORIGINS)
        )
    if not (requested_by or "").strip():
        raise CalculatedFieldProposalError(
            "missing_author", "A proposal says who filed it."
        )
    clean_description = (description or "").strip() or None
    if clean_description and len(clean_description) > 4000:
        raise CalculatedFieldProposalError(
            "description_too_long", "A description is at most 4000 characters."
        )

    analysis = _analyzed(conn, project_id=project_id, name=clean_name, expression=expression)
    checked_provenance = _checked_provenance(
        conn, org_id=org_id, project_id=project_id, provenance=provenance
    )

    proposal_id = f"cfp_{ULID()}"
    result = analysis.result
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.calculated_field_proposals
                    (id, org_id, project_id, status, origin, name, description,
                     expression, value_type, unit, currency, dependencies,
                     provenance, requested_by)
                VALUES (%s, %s, %s, 'open', %s, %s, %s, %s::jsonb, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s)
                RETURNING {", ".join(_COLUMNS)}
                """,
                (
                    proposal_id, org_id, project_id, origin, clean_name,
                    clean_description, json.dumps(expression),
                    result.value_type, result.unit, result.currency,
                    json.dumps([ref.as_dict() for ref in analysis.dependencies]),
                    json.dumps(checked_provenance), requested_by,
                ),
            )
            row = _row(cur.fetchone())
        _trace(
            conn,
            proposal=row,
            event="proposed",
            actor=requested_by,
            fact={
                "value_type": row["value_type"],
                "unit": row["unit"],
                "dependencies": _json_load(row["dependencies"]),
                "provenance": _json_load(row["provenance"]),
            },
        )
        insert_audit_row(
            conn,
            identity=requested_by,
            action=ACTION_CALCULATED_FIELD_PROPOSED,
            provider_account="platform",
            connection_ref="",
            metadata={
                "effective_org_id": org_id,
                "project_id": project_id,
                "resource_id": proposal_id,
                "origin": origin,
                "name": clean_name,
                "provenance": checked_provenance,
            },
        )
    return row


def _trace(
    conn: Any, *, proposal: dict[str, Any], event: str, actor: str, fact: dict[str, Any]
) -> None:
    """One append-only row per act. Never an update of the previous one."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.calculated_field_proposal_events
                (id, proposal_id, org_id, project_id, event, fact, actor)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                f"cfpe_{ULID()}", proposal["id"], proposal["org_id"],
                proposal["project_id"], event, json.dumps(fact), actor,
            ),
        )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def list_open(conn: Any, *, org_id: str, project_id: str) -> list[dict[str, Any]]:
    """The queue: what is left to decide in this Project, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_COLUMNS)} FROM app.calculated_field_proposals
             WHERE org_id = %s AND project_id = %s AND {open_status_predicate()}
             ORDER BY created_at DESC
            """,
            (org_id, project_id),
        )
        return [_row(row) for row in cur.fetchall()]


def project_org_id(conn: Any, project_id: str) -> str | None:
    """The organization of an active Project, or None.

    IT LIVES HERE AND NOT IN THE ROUTE MODULE, and that is criterion 4 of
    `module-boundaries.md` measured by `scripts/api_sql_census.py --gate`: a
    `*_api.py` module parses, authorizes and calls a service. One `SELECT` in a
    handler is how a route module becomes a second data layer, and the census
    exists because thirty-six of them already had.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.projects WHERE id = %s AND status = 'active'",
            (project_id,),
        )
        row = cur.fetchone()
    return str(row[0]) if row is not None and row[0] else None


def get(conn: Any, *, proposal_id: str, org_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_COLUMNS)} FROM app.calculated_field_proposals
             WHERE id = %s AND org_id = %s
            """,
            (proposal_id, org_id),
        )
        row = cur.fetchone()
    return _row(row) if row is not None else None


# ---------------------------------------------------------------------------
# Resolve -- and the change-set an acceptance PREPARES, never confirms
# ---------------------------------------------------------------------------


def _concept_intent(proposal: dict[str, Any]) -> dict[str, Any]:
    """The `create_concept` intent a promotion opens, with its mark.

    THE MARK SITS AT THE TOP LEVEL of the intent, not inside `concept`.
    Measured on `semantic_model._apply_concept`: it writes
    `semantic_concept_versions.provenance` itself, from a fixed shape
    (`change_set_id`, `base_version_id`, `authored_by`), and reads no
    free-form slot off the payload -- so a mark placed under `concept` would be
    validated, diffed, and then silently dropped at publication. At the top
    level `create_change_set` stores it verbatim (`canonical_json(dict(intent))`)
    and no reader downstream strips it, so the trail from a published version
    back to the exploration runs through the change-set. The remaining hop is
    named as an open assumption in `docs/product-architecture/context-hub.md`.
    """
    return {
        "action": "create_concept",
        "concept": {
            "kind": PROMOTED_KIND,
            "name": proposal["name"],
            "label": proposal["name"],
            "definition": proposal.get("description"),
            "value_type": proposal["value_type"],
            "unit": proposal.get("unit"),
            "expression": _json_load(proposal["expression"]),
        },
        "provenance": {
            "origin": PROVENANCE_ORIGIN,
            "proposal_id": proposal["id"],
            **{
                key: value
                for key, value in (_json_load(proposal["provenance"]) or {}).items()
                if key != "origin"
            },
        },
    }


def _prepare_promotion(
    conn: Any, *, proposal: dict[str, Any], resolved_by: str
) -> tuple[str, list[dict[str, Any]]]:
    """Open a change-set for the promoted field and leave it PREPARED.

    The confirmation token `prepare_change_set` returns is minted once and
    dropped here on purpose: this rail may not confirm, and a token that never
    leaves the function cannot be replayed by anything that reads the queue.
    """
    from core.semantic_model import create_change_set, prepare_change_set  # noqa: PLC0415

    change_set = create_change_set(
        conn,
        proposal["project_id"],
        actor=resolved_by,
        object_type="semantic-concept",
        object_id=None,
        base_version_id=None,
        intent=_concept_intent(proposal),
        # DERIVED FROM THE PROPOSAL, so accepting twice cannot open two
        # change-sets for one calculation -- the same reason
        # `propose_missing_link` derives its note from the observed fact.
        idempotency_key=f"calculated-field-proposal:{proposal['id']}",
    )
    prepared = prepare_change_set(
        conn, proposal["project_id"], change_set.id, actor=resolved_by
    )
    return change_set.id, list(prepared.get("refusals") or [])


def resolve(
    conn: Any, *, proposal_id: str, org_id: str, status: str, resolved_by: str
) -> dict[str, Any] | None:
    """Close a proposal. `accepted` or `declined` -- never a silence.

    `accepted` opens and prepares the change-set, then writes its id in
    `applied_ref`. `declined` touches the status, its author and its time, and
    nothing else. Both halves are ONE transaction: a promotion whose change-set
    is refused leaves the proposal open, because a proposal marked accepted with
    nothing behind it is the one state nobody can act on.
    """
    if not is_resolution(status):
        raise CalculatedFieldProposalError(
            "invalid_status",
            "status must be " + " or ".join(f"'{value}'" for value in RESOLUTION_STATUSES),
        )
    if not (resolved_by or "").strip():
        raise CalculatedFieldProposalError(
            "missing_author", "A verdict says who pronounced it."
        )

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE app.calculated_field_proposals
                   SET status = %s, resolved_by = %s, resolved_at = now()
                 WHERE id = %s AND org_id = %s AND {open_status_predicate()}
                RETURNING {", ".join(_COLUMNS)}
                """,
                (status, resolved_by, proposal_id, org_id),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT status FROM app.calculated_field_proposals "
                    "WHERE id = %s AND org_id = %s",
                    (proposal_id, org_id),
                )
                existing = cur.fetchone()
                if existing is not None:
                    raise ProposalAlreadyResolvedError(
                        "proposal_already_resolved",
                        f"Proposal '{proposal_id}' has already been resolved "
                        f"(current status: '{existing[0]}').",
                    )
                return None
        proposal = _row(row)

        refusals: list[dict[str, Any]] = []
        if status == STATUS_ACCEPTED:
            change_set_id, refusals = _prepare_promotion(
                conn, proposal=proposal, resolved_by=resolved_by
            )
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.calculated_field_proposals SET applied_ref = %s "
                    "WHERE id = %s AND org_id = %s",
                    (change_set_id, proposal_id, org_id),
                )
            proposal["applied_ref"] = change_set_id

        _trace(
            conn,
            proposal=proposal,
            event=status,
            actor=resolved_by,
            fact={
                "applied_ref": proposal["applied_ref"],
                # What the prepared change-set still refuses, kept beside the
                # verdict: a reader of the trace learns why a promotion is not
                # confirmable yet without re-preparing it.
                "prepare_refusals": [item.get("code") for item in refusals],
            },
        )
        insert_audit_row(
            conn,
            identity=resolved_by,
            action=ACTION_CALCULATED_FIELD_RESOLVED,
            provider_account="platform",
            connection_ref="",
            metadata={
                "effective_org_id": org_id,
                "project_id": proposal["project_id"],
                "resource_id": proposal_id,
                "status": status,
                "applied_ref": proposal["applied_ref"],
            },
        )
    proposal["prepare_refusals"] = refusals
    return proposal


#: The state a promoted change-set is left in. Named so a door, a screen and a
#: test all say the same word, and so a change of posture is one edit here
#: rather than four hand-typed strings.
PREPARED_STATE = "prepared"

#: The status a fresh proposal carries, re-exported so a reader of this module
#: never has to reach into the Hub queue for the word.
OPEN_STATUS = STATUS_OPEN
