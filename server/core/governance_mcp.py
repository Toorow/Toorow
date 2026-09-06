"""toorow -- Governance-profile MCP surface (Story 36.18, Epic 36).

The Governance capability profile: the ONE place a 'ready' agentic mapping proposal
(Story 36.17) becomes LIVE, via the AD-27 prepare-confirm-commit publication that
advances ``app.datastreams.current_mapping_version_id`` atomically with audit +
outbox and leaves the prior version rollbackable. Four tools -- three on the mapping
pointer, and one on the Semantic Model change set (see the section at the end of
this docstring):

  * ``review_agent_change``   -- Governance, READ. Assemble the governed review
                                 object (profile, scope, versions, diff, interval/
                                 destination, impact, checks, rollback, expiry). The
                                 opaque confirmation secret is minted server-side and
                                 NEVER placed in model/MCP output; only a bounded,
                                 non-secret ``confirmation_handle`` is surfaced.
  * ``confirm_agent_change``  -- Governance, WRITE, confirmation_mode="human". Verify
                                 the (server-verified / trusted-console) opaque secret,
                                 recheck expired/used/replayed/actor-or-workspace-
                                 mismatch/stale/changed-pointer/failed-check, then route
                                 EXACTLY ONE durable publication operation.
  * ``rollback_agent_change`` -- Governance, WRITE, confirmation_mode="human". A
                                 DISTINCT confirmed idempotent operation with its own
                                 audit + outbox that re-points to the prior version.
  * ``list_semantic_metric_presets`` -- Governance, READ, confirmation_mode="none".
                                 What preconfigured metrics of the delivered catalogue
                                 this Project could adopt, with the exact
                                 ``create_concept`` intent for one of them.
  * ``publish_semantic_model_change`` -- Governance, WRITE, confirmation_mode="human".
  * ``propose_shared_identities`` -- Governance, READ, confirmation_mode="none" (2026-09-05).
  * ``publish_shared_identity``  -- Governance, WRITE, confirmation_mode="human" (2026-09-05).
                                 Declare or correct a Semantic Concept / Semantic View
                                 through ONE change set: create from an exact base,
                                 prepare, confirm -- the three functions of
                                 ``core.semantic_model`` the console dialogs call.

INVARIANTS (E36-FR07, E36-NFR07, AD-27/AD-28)
---------------------------------------------
* The confirmation secret is OPAQUE and MODEL-HIDDEN. ``review_agent_change`` never
  returns it; ``confirm_agent_change`` receives it out-of-band from the trusted
  console / server-verified in-host presence, verifies its one-way hash, and never
  echoes it back. This module NEVER writes the raw secret into any ToolResult.
* Every tool re-authorizes at call time through ``project_access`` on the governance
  floor (``manage``); a foreign/absent resource is ``not_found`` (existence-hiding).
* The write path routes through ``governed_publication`` -> ``execute_operation``
  (atomic pointer + audit + outbox, idempotent replay). E36-NFR05: no parallel store.

Mirrors metric_semantics_mcp / operations_mcp: ``_identity`` / ``_tool_error`` /
``_result`` / ``_envelope``, lazy ``core.*`` imports inside function bodies (no
cycle), English ToolError microcopy, ASCII-only. Registered via
``mcp_profiles.register_profiled`` so the tools carry their capability declaration and
pass ``validate_catalog``; the capability middleware hides them from discovery and
denies direct calls unless a governance opt-in is present.

THE FOURTH TOOL -- ``publish_semantic_model_change`` (2026-08-25)
----------------------------------------------------------------
WHY IT IS HERE AND NOT IN A MODULE OF ITS OWN. ``known-debt.json`` recorded the
residue that retiring ``metric_definition_upsert`` made visible: *"an agent has no
MCP door onto the Semantic Model change sets -- governance_mcp.py registers three
tools and none creates a change set"*, and ``governance.md`` says the same at its
last amendment. This module IS the Governance profile's door, and the act it
already serves is the AD-27 ceremony: assemble, then confirm exactly once. A
Semantic Model change set is the same ceremony on a different authority
(``core.semantic_model`` rather than ``core.governed_publication``), reached from
the same surface, at the same capability floor family. A second module would have
split one profile's doors across two files without splitting a responsibility.

ONE STATE, TWO DOORS (AD-1), and it is literal here. The tool calls
``create_change_set`` -> ``prepare_change_set`` -> ``confirm_change_set``, the
three functions of ``core.semantic_model`` that the four routes of
``semantic_model_api.py`` call and that ``NewConceptDialog`` / ``NewSemanticViewDialog``
drive in that order. No SQL, no validation, no diff, no test-gate reading and no
version numbering lives in this module: re-implementing any of them would produce a
second answer to "what does this metric mean", which is exactly the defect the
Semantic Model exists to remove.

WHY ONE TOOL FOR THREE CALLS, AND WHY THE TOKEN NEVER REACHES THE MODEL.
``prepare_change_set`` mints a single-use confirmation token and returns it ONCE.
The console round-trips it through the browser; here the same call sequence happens
inside one server-side transaction, so the token is minted and consumed without ever
entering model context -- strictly stronger than the console path, and the same
posture ``review_agent_change`` holds above. Splitting the tool in three would have
FORCED the opposite: a prepare tool would have to hand the token to the model, and
no out-of-band retrieval exists for semantic change sets the way
``publication_confirmations`` has one. A drafted-but-unpublished change set would
also be a dead end -- no console screen resumes one by id; both dialogs keep the id
in local state only.

WHAT IT DOES NOT DO. It never publishes what preparation refused: a change set that
comes back ``publishable=False`` is returned WITH its named refusals and its Test
gate, and confirm is not called. That is the branch the dialogs take, not a new
regime. Retrying with the SAME ``idempotency_key`` resumes the same change set
(``create_change_set`` returns the existing row, ``prepare_change_set`` accepts a
``prepared`` one), which is what makes the reasoned Test-gate override work here
exactly as the 2026-08-18 amendment describes it: a RE-prepare of the same change
set.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"


# ---------------------------------------------------------------------------
# Envelope + error + identity + result helpers (local -- no coupling to main).
# ---------------------------------------------------------------------------


def _envelope(data: dict) -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _host_context() -> dict:
    """Return the server-verified host/workspace context from the capability token.

    Used to bind the confirmation to a server-verified in-host human presence
    (workspace mismatch is a confirm refusal). Sourced ONLY from the verified
    capability context claim, never from a model-controlled tool argument.
    """
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    try:
        token = get_access_token()
        if token is None:
            return {}
        context = (token.claims or {}).get("capability_context")
        if isinstance(context, dict):
            return {
                k: context.get(k)
                for k in ("host", "workspace_id", "workspace_type", "client_id")
                if context.get(k) is not None
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("governance_mcp: host context resolution failed (%s)", exc)
    return {}


def _result(summary: str, data: dict):
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


# ---------------------------------------------------------------------------
# Access guard -- MIRRORS the strict AD-5 seam on the governance floor (manage).
# ---------------------------------------------------------------------------


def _guard_datastream(datastream_id: str, identity: str, *, minimum_capability: str):
    """Return an ``AccessDecision`` or raise not_found (existence-hiding).

    Wraps ``project_access.resolve_strict_resource_access`` on the Datastream scope.
    Governance publication requires ``manage``. Denial for ANY reason surfaces a
    single ``not_found`` -- we NEVER disclose whether the Datastream exists
    (E36-NFR02). Fail-closed on a guard failure (never proceed unguarded).
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                datastream_id=datastream_id,
                minimum_capability=minimum_capability,
            )
    except Exception as exc:  # noqa: BLE001 -- fail-closed (never unguarded).
        logger.error("governance_mcp: access guard failed ds=%s: %s", datastream_id, exc)
        raise _tool_error("not_found", "Datastream not found.") from exc
    if not decision.allowed or not decision.org_id:
        raise _tool_error("not_found", "Datastream not found.")
    return decision


def _proposal_datastream(conn, proposal_id: str) -> tuple[str | None, str | None]:
    """Resolve (datastream_id, org_id) for a proposal so we can guard its resource."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT datastream_id, org_id FROM app.mapping_proposals WHERE id = %s",
            (proposal_id,),
        )
        row = cur.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


# ---------------------------------------------------------------------------
# register(mcp) -- define each handler locally + register_profiled.
# ---------------------------------------------------------------------------


# ---- Read: assemble the governed review (secret stays model-hidden) ----
def review_agent_change(proposal_id: str):
    """Open the governed review of a READY mapping proposal (Story 36.18).

    Assembles the review object as a READ: profile (governance), actor/resource
    scope, versions (candidate + prior), mapping diff (via diff_mappings, with no
    raw provider values), destination (mapping pointer advance), expected impact,
    recorded checks, rollback path (prior version) and expiry. The confirmation
    secret is minted server-side and stays MODEL-HIDDEN: it NEVER appears in the
    output; only a non-secret 'confirmation_id' is surfaced. Strict AD-5 guard
    (manage); a foreign proposal or datastream yields not_found. Creates NO durable
    operation and advances NO pointer.
    """
    proposal_id = (proposal_id or "").strip() or None
    if proposal_id is None:
        raise _tool_error("missing_param", "proposal_id is required.")
    identity = _identity()

    from core.db import request_connection  # noqa: PLC0415
    from core.governed_publication import (  # noqa: PLC0415
        PublicationReviewUnavailable,
        prepare_publication_review,
    )

    try:
        with request_connection(identity) as conn:
            datastream_id, _org = _proposal_datastream(conn, proposal_id)
            if datastream_id is None:
                raise _tool_error("not_found", "Proposal not found.")
            decision = _guard_datastream(datastream_id, identity, minimum_capability="manage")
            review = prepare_publication_review(
                conn,
                proposal_id=proposal_id,
                actor=identity,
                org_id=str(decision.org_id),
                host_context=_host_context(),
            )
            conn.commit()
    except PublicationReviewUnavailable as exc:
        # Existence-hiding: a not-ready/out-of-scope proposal is not disclosed.
        raise _tool_error("not_found", "Proposal not found or not publishable.") from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("governance_mcp: review failed prop=%s: %s", proposal_id, exc)
        raise _tool_error("server_error", "Review unavailable.") from exc

    # Return ONLY the model-safe review object. The confirmation secret
    # (review.confirmation_secret) is DELIBERATELY dropped here so it never enters
    # model context; the trusted console retrieves it out-of-band.
    data = dict(review.review)
    data["confirmation_secret_present"] = False  # explicit: secret is model-hidden
    summary = (
        f"Governed review of proposal {proposal_id!r}: profile 'governance', "
        f"mapping pointer advance (pending confirmation, secret not exposed to "
        f"the model)."
    )
    return _result(summary, data)


# ---- Write: confirm + publish -> ONE durable operation (AD-27 commit) ----
def confirm_agent_change(confirmation_id: str, confirmation_secret: str):
    """Confirm and PUBLISH a proposal: ONE durable operation (Story 36.18).

    Verifies the OPAQUE confirmation secret (trusted console, or server-verified
    in-host human presence; compares a one-way digest and NEVER echoes the secret),
    then RE-CHECKS: expired / already used / replayed / actor mismatch / workspace
    mismatch / stale versions / changed pointer / changed policy / proposal not
    ready -> NO operation is created or dispatched. Otherwise it routes EXACTLY ONE
    durable operation (Story 36.2) whose mutation advances
    current_mapping_version_id (pointer + log + audit + outbox, atomically) and
    leaves the prior version ROLLBACKABLE. A duplicate confirm or a timeout returns
    the ORIGINAL operation (never a duplicate). On failure / outcome_unknown,
    reconciliation uses the original references (never a blind fresh mutation).
    Strict AD-5 guard (manage); a foreign confirmation yields not_found.
    """
    confirmation_id = (confirmation_id or "").strip() or None
    confirmation_secret = confirmation_secret or ""
    if confirmation_id is None:
        raise _tool_error("missing_param", "confirmation_id is required.")
    if not confirmation_secret.strip():
        raise _tool_error("missing_param", "confirmation_secret is required.")
    identity = _identity()

    from core.db import request_connection  # noqa: PLC0415
    from core.governed_publication import (  # noqa: PLC0415
        PublicationConfirmationRefused,
        confirm_and_publish,
    )

    try:
        with request_connection(identity) as conn:
            confirmation = _load_confirmation_scope(conn, confirmation_id)
            if confirmation is None:
                raise _tool_error("not_found", "Confirmation not found.")
            decision = _guard_datastream(
                confirmation["datastream_id"], identity, minimum_capability="manage"
            )
            if str(decision.org_id) != str(confirmation["org_id"]):
                raise _tool_error("not_found", "Confirmation not found.")
            result = confirm_and_publish(
                conn,
                confirmation_id=confirmation_id,
                confirmation_secret=confirmation_secret,
                actor=identity,
                org_id=str(decision.org_id),
                host_context=_host_context(),
            )
            conn.commit()
    except PublicationConfirmationRefused as exc:
        logger.info(
            "governance_mcp: confirm refused conf=%s code=%s", confirmation_id, exc.code
        )
        raise _tool_error(exc.code, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("governance_mcp: confirm failed conf=%s: %s", confirmation_id, exc)
        raise _tool_error("server_error", "Confirmation unavailable.") from exc

    # NEVER echo the secret. Return only the operation outcome + versions.
    data = {
        "confirmation_id": result.confirmation_id,
        "operation_id": result.operation_id,
        "outcome": result.outcome,
        "replayed": result.replayed,
        "current_mapping_version_id": result.current_mapping_version_id,
        "prior_mapping_version_id": result.prior_mapping_version_id,
        "prior_version_rollbackable": result.prior_mapping_version_id is not None,
    }
    replayed = " (replay of the original operation)" if result.replayed else ""
    summary = (
        f"Publication confirmed: operation {result.operation_id} "
        f"({result.outcome}){replayed}. Mapping pointer advanced; the prior "
        f"version stays rollbackable."
    )
    return _result(summary, data)


# ---- Write: rollback -> a DISTINCT confirmed idempotent operation ----
def rollback_agent_change(confirmation_id: str, target_mapping_version_id: str):
    """Undo a publication: a DISTINCT idempotent operation (Story 36.18).

    Re-points current_mapping_version_id at the prior version (captured at
    publication time). This is a SEPARATE durable operation (never a replay of the
    publication operation) with its OWN audit + outbox. The target must be the
    version this confirmation replaced; any other target is refused. Idempotent on
    replay. Strict AD-5 guard (manage); a foreign confirmation yields not_found.
    """
    confirmation_id = (confirmation_id or "").strip() or None
    target_mapping_version_id = (target_mapping_version_id or "").strip() or None
    if confirmation_id is None:
        raise _tool_error("missing_param", "confirmation_id is required.")
    if target_mapping_version_id is None:
        raise _tool_error("missing_param", "target_mapping_version_id is required.")
    identity = _identity()

    from core.db import request_connection  # noqa: PLC0415
    from core.governed_publication import (  # noqa: PLC0415
        PublicationConfirmationRefused,
        rollback_publication,
    )

    try:
        with request_connection(identity) as conn:
            confirmation = _load_confirmation_scope(conn, confirmation_id)
            if confirmation is None:
                raise _tool_error("not_found", "Confirmation not found.")
            decision = _guard_datastream(
                confirmation["datastream_id"], identity, minimum_capability="manage"
            )
            if str(decision.org_id) != str(confirmation["org_id"]):
                raise _tool_error("not_found", "Confirmation not found.")
            result = rollback_publication(
                conn,
                confirmation_id=confirmation_id,
                target_mapping_version_id=target_mapping_version_id,
                actor=identity,
                org_id=str(decision.org_id),
                host_context=_host_context(),
            )
            conn.commit()
    except PublicationConfirmationRefused as exc:
        logger.info(
            "governance_mcp: rollback refused conf=%s code=%s", confirmation_id, exc.code
        )
        raise _tool_error(exc.code, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("governance_mcp: rollback failed conf=%s: %s", confirmation_id, exc)
        raise _tool_error("server_error", "Rollback unavailable.") from exc

    data = {
        "confirmation_id": result.confirmation_id,
        "operation_id": result.operation_id,
        "outcome": result.outcome,
        "replayed": result.replayed,
        "current_mapping_version_id": result.current_mapping_version_id,
        "prior_mapping_version_id": result.prior_mapping_version_id,
        "distinct_operation": True,
    }
    replayed = " (replay of the original operation)" if result.replayed else ""
    summary = (
        f"Rollback confirmed: distinct operation {result.operation_id} "
        f"({result.outcome}){replayed}. Mapping pointer moved back to "
        f"{target_mapping_version_id!r}."
    )
    return _result(summary, data)


# ---- Read: what preconfigured metrics this Project could adopt ----
def list_semantic_metric_presets(project_id: str, name: str | None = None):
    """The preconfigured metrics of the delivered catalogue, judged for this Project.

    A READ. It writes nothing and advances no pointer: adopting one is
    `publish_semantic_model_change` with the intent this tool hands back.

    Without `name`: the whole offer, one line each -- state, whether the
    metric is calculated from other Concepts, and for a blocked one the
    Concept to declare first. No intents, because twelve typed trees do not
    belong in model context to answer "what is available".

    With `name`: that one preset, WITH its `intent` -- the exact
    `create_concept` payload, operands already pinned to a concept id and a
    version id in this Project. Pass it to `publish_semantic_model_change`
    unchanged: rebuilding it would publish a formula the server never
    resolved.

    Three states, and only `adoptable` carries an intent. `already_declared`
    means this Project already reads a Concept of that name; `blocked` means
    one of its operands does not exist here yet, and `gesture` names it.
    """
    project_id = (project_id or "").strip()
    if not project_id:
        raise _tool_error("missing_param", "project_id is required.")
    wanted = (name or "").strip() or None
    identity = _identity()

    from core.db import request_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.semantic_metric_presets import presets_for_project  # noqa: PLC0415

    with request_connection(identity) as conn:
        try:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                # `view`: seeing what could be adopted is not adopting it,
                # and the write door beside this one already holds `edit`.
                minimum_capability="view",
                hold_access=True,
            )
        except Exception as exc:  # noqa: BLE001 -- an outage fails CLOSED
            logger.error("governance_mcp: access resolution failed: %s", exc)
            raise _tool_error("project_not_found", "Project not found or archived.") from exc
        if not decision.allowed or not decision.org_id:
            raise _tool_error("project_not_found", "Project not found or archived.")
        catalogue = presets_for_project(conn, project_id)

    if wanted is not None:
        matching = [row for row in catalogue["presets"] if row["name"] == wanted]
        if not matching:
            raise _tool_error(
                "not_found",
                f"{wanted!r} is not in the delivered metric catalogue "
                f"({catalogue['source']}).",
            )
        preset = matching[0]
        data = {"project_id": project_id, "source": catalogue["source"], "preset": preset}
        if preset["state"] == "adoptable":
            summary = (
                f"{preset['name']} can be adopted by this Project. Pass `preset.intent` to "
                "publish_semantic_model_change with object_type 'semantic-concept'."
            )
        elif preset["state"] == "blocked":
            summary = f"{preset['name']} cannot be adopted yet. {preset['gesture']}"
        else:
            summary = (
                f"{preset['name']} is already readable in this Project at "
                f"{preset['declared_scope']} scope; adopting it again would shadow it."
            )
        return _result(summary, data)

    # The list stays a LIST: no intent, no expression, no version ids. A
    # discovery answer that carries every payload it could ever be asked for
    # spends the model channel on trees nobody has chosen yet.
    rows = [
        {
            "name": row["name"],
            "label": row["label"],
            "state": row["state"],
            "calculated": row["calculated"],
            "dependencies": row["dependencies"],
            "gesture": row["gesture"],
        }
        for row in catalogue["presets"]
    ]
    counts = catalogue["counts"]
    data = {
        "project_id": project_id,
        "source": catalogue["source"],
        "counts": counts,
        "presets": rows,
    }
    summary = (
        f"{counts['adoptable']} preconfigured metric(s) adoptable, {counts['blocked']} "
        f"blocked on a Concept this Project has not declared, "
        f"{counts['already_declared']} already readable here. Call again with `name` "
        "for the intent of one."
    )
    return _result(summary, data)


# ---- Write: declare meaning -> ONE Semantic Model change set, published ----
def publish_semantic_model_change(
    project_id: str,
    object_type: str,
    intent: dict,
    idempotency_key: str,
    object_id: str | None = None,
    base_version_id: str | None = None,
    test_gate_override_reason: str | None = None,
):
    """Declare or correct a Semantic Concept or Semantic View, through a change set.

    The ONE governed path: create the change set from an EXACT base, prepare it
    (validation, diff, used-by impact, Test gate) and confirm it -- the same three
    functions the console dialogs call, in the same order.

    - `object_type`: 'semantic-concept' or 'semantic-view'.
    - `intent`: {'action': one of create_concept | edit_concept | create_view |
      edit_view | archive_object, plus the payload nested under 'concept' or
      'view'}. A flat payload is refused by name rather than published empty.
    - `object_id` + `base_version_id`: REQUIRED together for an edit or an
      archive. Editing "the current version" is refused: it would rebase onto
      whatever became current since it was read.
    - `idempotency_key`: retrying with the same key resumes the same change set
      instead of opening a second one.
    - `test_gate_override_reason`: >= 20 characters. A publication whose Test gate
      is not 'pass' travels only on a written, attributed override.

    Nothing is published when preparation refuses: the refusals and the gate come
    back and no version is written. No version is ever rewritten -- a correction
    appends version N+1 and the base stays readable.
    """
    project_id = (project_id or "").strip()
    object_type = (object_type or "").strip()
    idempotency_key = (idempotency_key or "").strip()
    if not project_id:
        raise _tool_error("missing_param", "project_id is required.")
    if not object_type:
        raise _tool_error(
            "missing_param",
            "object_type is required: 'semantic-concept' or 'semantic-view'.",
        )
    if not isinstance(intent, dict) or not intent:
        raise _tool_error(
            "missing_param",
            "intent is required and carries its fields under 'concept' or 'view'.",
        )
    # BEFORE the guard, and it says nothing about the Project. An idempotency
    # key is what the caller failed to send; refusing it here cannot tell
    # anyone whether `project_id` exists.
    if not idempotency_key:
        raise _tool_error(
            "missing_idempotency_key",
            "An idempotency key is required so a retried call resumes the same "
            "change set instead of opening a second one.",
        )
    identity = _identity()
    if not identity or identity == "anonymous":
        # Existence-hiding, exactly like the REST door's 401 -- but the MCP
        # envelope is the one a refused Project gets, never a distinct word.
        raise _tool_error("project_not_found", "Project not found or archived.")

    from core.db import request_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.semantic_model import (  # noqa: PLC0415
        SemanticNotFound,
        SemanticRefused,
        SemanticStale,
        confirm_change_set,
        create_change_set,
        prepare_change_set,
    )

    # THE RANK IS READ FROM THE TWIN DOOR, NOT RESTATED. `edit` and not
    # `manage`: publishing meaning is not an administrative act, and it is
    # never available to a viewer either -- the sentence is at
    # `semantic_model_api._WRITE_CAPABILITY`. Two ranks for one gesture would
    # be two answers to "who may declare a metric here".
    from core.semantic_model_api import _WRITE_CAPABILITY  # noqa: PLC0415

    override = None
    if (test_gate_override_reason or "").strip():
        override = {"reason": test_gate_override_reason}

    with request_connection(identity) as conn:
        try:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                minimum_capability=_WRITE_CAPABILITY,
                hold_access=True,
            )
        except Exception as exc:  # noqa: BLE001 -- an outage fails CLOSED
            logger.error("governance_mcp: access resolution failed: %s", exc)
            raise _tool_error("project_not_found", "Project not found or archived.") from exc
        if not decision.allowed or not decision.org_id:
            raise _tool_error("project_not_found", "Project not found or archived.")

        try:
            change_set = create_change_set(
                conn,
                project_id,
                actor=identity,
                object_type=object_type,
                object_id=object_id,
                base_version_id=base_version_id,
                intent=intent,
                idempotency_key=idempotency_key,
            )
            prepared = prepare_change_set(
                conn,
                project_id,
                change_set.id,
                actor=identity,
                allow_test_override=override,
            )
            validation = prepared.get("validation") or {}
            if validation.get("publishable") is not True:
                # THE REFUSED CHANGE SET IS KEPT, and that is what makes the
                # override loop work: the same idempotency key resumes THIS
                # row, and a RE-prepare carrying a written reason is the
                # escape hatch `governance.md` describes for a gate nobody
                # can answer. Discarding it would force a second change set
                # for the same act.
                conn.commit()
                refusals = prepared.get("refusals") or validation.get("refusals") or []
                gate = validation.get("test_gate") or {}
                data = {
                    "change_set_id": change_set.id,
                    "published": False,
                    "state": prepared.get("state"),
                    "refusals": refusals,
                    "test_gate": gate,
                    "used_by_impact": validation.get("used_by_impact"),
                    "diff": prepared.get("diff"),
                }
                named = ", ".join(
                    str(r.get("code")) for r in refusals if isinstance(r, dict)
                )
                summary = (
                    f"Nothing was published. Change set {change_set.id} did not "
                    f"clear preparation"
                    + (f": {named}." if named else ".")
                    + (
                        f" Test gate is {gate.get('state')!r}; publishing it "
                        "requires test_gate_override_reason (20 characters or "
                        "more) on a retry with the same idempotency_key."
                        if gate.get("state") not in (None, "pass", "overridden")
                        else ""
                    )
                )
                return _result(summary, data)

            result = confirm_change_set(
                conn,
                project_id,
                change_set.id,
                actor=identity,
                confirmation_token=str(prepared.get("confirmation_token") or ""),
                org_id=str(decision.org_id),
            )
            conn.commit()
        except SemanticNotFound as exc:
            # Existence-hiding, identically to the read surface: an object of
            # another Project and one that never existed answer the same.
            raise _tool_error("not_found", "Semantic Model object not found.") from exc
        except SemanticStale as exc:
            # SemanticStale IS a SemanticRefused -- caught first, or drift and
            # expiry would arrive wearing the wrong code.
            raise _tool_error(exc.code, json.dumps(exc.as_dict())) from exc
        except SemanticRefused as exc:
            raise _tool_error(exc.code, json.dumps(exc.as_dict())) from exc
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error(
                "governance_mcp: semantic change failed project=%s: %s", project_id, exc
            )
            raise _tool_error("server_error", "Semantic Model is unavailable.") from exc

    data = {
        "change_set_id": change_set.id,
        "published": True,
        "object_type": object_type,
        "result_version_id": result.get("result_version_id"),
        "replayed": bool(result.get("replayed")),
        "change_set": result.get("change_set"),
    }
    replayed = " (replay -- the same change set was already confirmed)" if data[
        "replayed"
    ] else ""
    summary = (
        f"Published: {object_type} version {data['result_version_id']} from change "
        f"set {change_set.id}{replayed}. No earlier version was rewritten."
    )
    return _result(summary, data)


def _compact_proposal(proposal: dict) -> dict:
    """One line per identity: what it is, where it stands, what it is pinned to. No carriers."""
    return {
        "identity": proposal.get("identity"),
        "role": proposal.get("role"),
        "canonical_field_id": proposal.get("canonical_field_id"),
        "canonical_name": proposal.get("canonical_name"),
        "carriers": int(proposal.get("carrier_count") or 0),
        "pinned": len(proposal.get("already_pinned") or []),
        "to_pin": len(proposal.get("to_pin") or []),
        "pending": len(proposal.get("pending_publication") or []),
        "aka": list(proposal.get("also_known_as") or [])[:6],
    }


def _detailed_proposal(proposal: dict) -> dict:
    """One identity with its carriers -- exactly what `publish_shared_identity` needs."""
    to_pin = set(proposal.get("to_pin") or [])
    carriers = [c for c in (proposal.get("carriers") or []) if isinstance(c, dict)]
    return {
        **_compact_proposal(proposal),
        "aggregation": proposal.get("aggregation"),
        "gesture": proposal.get("gesture"),
        "carriers_detail": [
            {
                "datastream_id": c.get("datastream_id"),
                "name": c.get("datastream_name"),
                "column": c.get("column"),
                "pinned_to": c.get("pinned_to"),
                "pending_to": c.get("pending_to"),
            }
            for c in carriers
        ],
        "to_pin_carriers": [
            {"datastream_id": c.get("datastream_id"), "column": c.get("column")}
            for c in carriers
            if c.get("datastream_id") in to_pin
        ],
    }


def propose_shared_identities(
    project_id: str,
    identity: str | None = None,
    role: str | None = None,
    limit: int = 12,
):
    """Which identities this Project's flows share -- dimensions and measures -- and what is left to pin.

    One read, project-wide, derived from the mapping version in force of every
    Datastream and never stored (`governance.md`, amendments of 2026-09-04 and
    2026-09-05). Without `identity`: one compact line per identity -- name,
    `role` (dimension | metric), the canonical field proposed (or none: then
    declare one), how many flows carry it, are pinned, are still to pin -- at
    most `limit` lines (dimensions first), with `role` to narrow. With
    `identity`: that identity in full, its carriers with their column names and
    `to_pin_carriers`, exactly what `publish_shared_identity` takes. A dimension
    is proposed when two flows carry it; a measure as soon as one does, because
    a crossing selects it and does not join on it.
    """
    project_id = (project_id or "").strip()
    if not project_id:
        raise _tool_error("missing_param", "project_id is required.")
    wanted = (identity or "").strip() or None
    role = (role or "").strip() or None
    if role and role not in {"dimension", "metric"}:
        raise _tool_error("missing_param", "role is 'dimension' or 'metric'.")
    try:
        limit = max(1, min(int(limit or 12), 40))
    except (TypeError, ValueError):
        limit = 12
    identity = _identity()
    if not identity or identity == "anonymous":
        raise _tool_error("project_not_found", "Project not found or archived.")

    from core.db import request_connection  # noqa: PLC0415
    from core.mdm_common_keys import propose_shared_identities as _propose  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    with request_connection(identity) as conn:
        try:
            decision = resolve_strict_resource_access(
                identity, conn, project_id=project_id, minimum_capability="view"
            )
        except Exception as exc:  # noqa: BLE001 -- an outage fails CLOSED
            logger.error("governance_mcp: access resolution failed: %s", exc)
            raise _tool_error("project_not_found", "Project not found or archived.") from exc
        if not decision.allowed or not decision.org_id:
            raise _tool_error("project_not_found", "Project not found or archived.")
        try:
            proposed = _propose(conn, project_id=project_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("governance_mcp: shared identities failed project=%s: %s", project_id, exc)
            raise _tool_error("server_error", "Shared identities are unavailable.") from exc

    proposals = [p for p in (proposed.get("proposals") or []) if isinstance(p, dict)]
    unmapped = [u.get("name") for u in (proposed.get("unmapped_datastreams") or []) if isinstance(u, dict)]
    if wanted:
        found = next((p for p in proposals if p.get("identity") == wanted), None)
        if found is None:
            raise _tool_error(
                "not_found",
                f"No proposal is named `{wanted}` in this Project: call without `identity` to list them.",
            )
        detail = _detailed_proposal(found)
        summary = detail.get("gesture") or f"`{wanted}`: {detail['pinned']} pinned, {detail['to_pin']} to pin."
        return _result(summary, {"state": proposed.get("state"), "proposal": detail, "datastreams_read": proposed.get("datastreams_read", 0)})
    if role:
        proposals = [p for p in proposals if p.get("role") == role]
    dimensions = sum(1 for p in proposals if p.get("role") == "dimension")
    to_pin = sum(len(p.get("to_pin") or []) for p in proposals)
    head = proposals[:limit]
    data = {
        "state": proposed.get("state"),
        "proposals": [_compact_proposal(p) for p in head],
        "listed": len(head),
        "total": len(proposals),
        "truncated": bool(proposed.get("truncated")) or len(proposals) > len(head),
        "datastreams_read": proposed.get("datastreams_read", 0),
        "unmapped_datastreams": unmapped[:6],
        "next": "call again with identity=<name> for its carriers and to_pin_carriers",
    }
    summary = (
        f"{len(proposals)} identit{'y' if len(proposals) == 1 else 'ies'} across "
        f"{proposed.get('datastreams_read', 0)} flow(s): {dimensions} dimension(s), "
        f"{len(proposals) - dimensions} measure(s); {to_pin} pin(s) still to make."
        + (f" Listing the first {len(head)}." if len(head) < len(proposals) else "")
        + (" Some Datastreams publish no mapping and were not counted." if unmapped else "")
    )
    return _result(summary, data)


def publish_shared_identity(project_id: str, intent: dict, idempotency_key: str):
    """Pin columns of named flows to a canonical field, or declare a common key over canonical fields.

    THE model's door onto what `propose_shared_identities` proposes -- the same
    functions the console's Mapping tab and Common keys panel call, never a second
    implementation (`mcp-tool-surface.md`, 2026-09-05).

    - `intent.action = "pin_identity"`: `carriers` = [{datastream_id, column}, ...]
      and either `canonical_field_id` (an active field of the Project or the
      platform) or `canonical` = {name, concept_kind: metric | dimension,
      value_type, aggregation?, description?} to declare the Project's own field
      first. Each pin is a governed mapping change on the flow's version in
      force: binding-only, so it is published as an overlay and re-pulls nothing;
      the answer says, per flow, `pinned` / `already_pinned` / `refused`.
    - `intent.action = "declare_common_key"`: `name`, `canonical_field_ids`
      (ordered, dimensions only), `description?`. A key already active under that
      name is returned as a replay, never duplicated.
    - `idempotency_key`: a retried pin with the same key resumes the same change
      per flow instead of opening a second one.

    A flat payload is refused by name. No confirmation secret ever enters the
    answer: it is minted and consumed inside one transaction.
    """
    project_id = (project_id or "").strip()
    idempotency_key = (idempotency_key or "").strip()
    if not project_id:
        raise _tool_error("missing_param", "project_id is required.")
    if not isinstance(intent, dict) or not intent:
        raise _tool_error(
            "missing_param",
            "intent is required: {action: pin_identity | declare_common_key, ...}.",
        )
    action = str(intent.get("action") or "").strip()
    if action not in {"pin_identity", "declare_common_key"}:
        raise _tool_error(
            "unknown_action",
            "intent.action is 'pin_identity' or 'declare_common_key'.",
        )
    if not idempotency_key:
        raise _tool_error(
            "missing_idempotency_key",
            "An idempotency key is required so a retried call resumes the same "
            "change instead of opening a second one.",
        )
    identity = _identity()
    if not identity or identity == "anonymous":
        raise _tool_error("project_not_found", "Project not found or archived.")

    from core.canonical_field_registry import CanonicalFieldError, declare_project_field  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.mdm_common_keys import (  # noqa: PLC0415
        CommonKeyRefused,
        create_common_key,
        list_common_keys,
        read_common_key,
    )
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.semantic_model_api import _WRITE_CAPABILITY  # noqa: PLC0415
    from core.shared_identity_pins import SharedIdentityRefused, pin_shared_identity  # noqa: PLC0415

    with request_connection(identity) as conn:
        try:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                minimum_capability=_WRITE_CAPABILITY,
                hold_access=True,
            )
        except Exception as exc:  # noqa: BLE001 -- an outage fails CLOSED
            logger.error("governance_mcp: access resolution failed: %s", exc)
            raise _tool_error("project_not_found", "Project not found or archived.") from exc
        if not decision.allowed or not decision.org_id:
            raise _tool_error("project_not_found", "Project not found or archived.")

        try:
            if action == "pin_identity":
                canonical_field_id = str(intent.get("canonical_field_id") or "").strip()
                declared = None
                canonical = intent.get("canonical")
                if not canonical_field_id and isinstance(canonical, dict) and canonical:
                    declared = declare_project_field(
                        conn,
                        project_id=project_id,
                        canonical_name=str(canonical.get("name") or ""),
                        concept_kind=str(canonical.get("concept_kind") or ""),
                        value_type=str(canonical.get("value_type") or ""),
                        actor=identity,
                        aggregation=canonical.get("aggregation"),
                        description=canonical.get("description"),
                    )
                    canonical_field_id = str(declared.get("id") or "")
                if not canonical_field_id:
                    raise _tool_error(
                        "missing_param",
                        "pin_identity needs canonical_field_id, or canonical {name, concept_kind, value_type}.",
                    )
                pinned = pin_shared_identity(
                    conn,
                    project_id=project_id,
                    canonical_field_id=canonical_field_id,
                    carriers=intent.get("carriers") or [],
                    actor=identity,
                    idempotency_key=idempotency_key,
                    reason=intent.get("reason"),
                )
                conn.commit()
                data = {
                    "action": action,
                    "declared_canonical_field": (
                        {"id": declared.get("id"), "canonical_name": declared.get("canonical_name")} if declared else None
                    ),
                    **pinned,
                }
                summary = (
                    f"`{pinned.get('canonical_name')}`: {pinned['pinned']} flow(s) pinned, "
                    f"{pinned['already_pinned']} already pinned, {pinned['refused']} refused."
                    + (" The canonical field was declared first." if declared else "")
                )
                return _result(summary, data)

            name = str(intent.get("name") or "").strip()
            existing = next(
                (k for k in list_common_keys(conn, project_id=project_id) if str(k.get("name")) == name),
                None,
            )
            if existing:
                sheet = read_common_key(conn, project_id=project_id, common_key_id=str(existing["id"]))
                conn.commit()
                version = sheet.get("current_version") if isinstance(sheet, dict) else None
                return _result(
                    f"Common key `{name}` already exists in this Project (replay): nothing was declared twice.",
                    {
                        "action": action,
                        "replayed": True,
                        "common_key": {
                            "id": sheet.get("id"),
                            "name": sheet.get("name"),
                            "version_id": (version or {}).get("id") if isinstance(version, dict) else None,
                        },
                    },
                )
            created = create_common_key(
                conn,
                project_id=project_id,
                name=name,
                canonical_field_ids=intent.get("canonical_field_ids") or [],
                actor=identity,
                description=intent.get("description"),
            )
            conn.commit()
            return _result(
                f"Common key `{name}` declared, version 1.",
                {
                    "action": action,
                    "replayed": False,
                    "common_key": {
                        "id": created.get("id"),
                        "name": created.get("name"),
                        "version_id": created.get("version_id") or ((created.get("current_version") or {}).get("id")),
                    },
                },
            )
        except SharedIdentityRefused as exc:
            raise _tool_error(exc.code, exc.message) from exc
        except CommonKeyRefused as exc:
            raise _tool_error(exc.code, exc.message) from exc
        except CanonicalFieldError as exc:
            raise _tool_error("canonical_field_refused", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("governance_mcp: shared identity write failed project=%s: %s", project_id, exc)
            raise _tool_error("server_error", "Shared identities are unavailable.") from exc


def register(mcp) -> None:
    """Register the Governance-profile MCP tools on *mcp* (Story 36.18).

    Called once from core.main AFTER the mapping_proposal_mcp registration and BEFORE
    ``validate_catalog()`` so the boot validator sees these tools. Every tool is
    registered via ``mcp_profiles.register_profiled`` under ``profile="governance"``
    so the capability middleware hides it unless a governance opt-in is present. The
    read tool is ``effect="read"``/``confirmation_mode="none"``; both write tools are
    ``effect="confirmed_write"``/``confirmation_mode="human"``.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp, review_agent_change,
        profile="governance", effect="read",
        data_class="sensitive", confirmation_mode="none",
    )
    register_profiled(
        mcp, confirm_agent_change,
        profile="governance", effect="confirmed_write",
        data_class="sensitive", confirmation_mode="human",
    )
    register_profiled(
        mcp, rollback_agent_change,
        profile="governance", effect="confirmed_write",
        data_class="sensitive", confirmation_mode="human",
    )
    register_profiled(
        mcp, list_semantic_metric_presets,
        # A READ, and `none` is the honest confirmation mode: this tool changes
        # nothing an operator would want to be asked about. What it discovers is
        # adopted through the confirmed write below.
        profile="governance", effect="read",
        data_class="sensitive", confirmation_mode="none",
    )
    register_profiled(
        mcp, propose_shared_identities,
        # A READ of a derived proposal: nothing an operator would be asked about.
        profile="governance", effect="read",
        data_class="sensitive", confirmation_mode="none",
    )
    register_profiled(
        mcp, publish_shared_identity,
        # A pin decides which canonical field a column MEANS in every crossing,
        # and a key decides what two flows join on: the rank of its neighbour
        # `publish_semantic_model_change`, for the same reason (2026-09-05).
        profile="governance", effect="confirmed_write",
        data_class="sensitive", confirmation_mode="human",
    )
    register_profiled(
        mcp, publish_semantic_model_change,
        profile="governance", effect="confirmed_write",
        # `human`, the rank its two neighbours here hold and the one
        # `set_dimension_label` holds for a strictly smaller act. Publishing a
        # Concept decides what a number MEANS in every render of the Project, and
        # correcting one appends a version every pinned consumer will resolve
        # against. `host` -- what `declare_entity_type` asks -- would be enough
        # for minting an owner nobody reads yet; it is not enough for the answer
        # itself.
        data_class="sensitive", confirmation_mode="human",
    )


def _load_confirmation_scope(conn, confirmation_id: str) -> dict | None:
    """Resolve (datastream_id, org_id) for a confirmation so we can guard its resource.

    A lightweight scope read used only to run the strict AD-5 guard BEFORE the domain
    module re-loads the full confirmation FOR UPDATE. Existence-hiding is preserved:
    an absent row yields ``not_found`` at the caller.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT datastream_id, org_id FROM app.publication_confirmations WHERE id = %s",
            (confirmation_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"datastream_id": row[0], "org_id": row[1]}
