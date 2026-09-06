"""Project capability MCP surface -- true lifecycle parity, not a second store (Story 48.1).

Six tools cover the same lifecycle Console covers, against the SAME application
handlers and the SAME serializer:

    read_project_capability                    effect: read
    preview_project_capability_impact          effect: read
    prepare_project_capability_change          effect: prepare
    confirm_project_capability_change          effect: confirmed_write
    prepare_project_capability_row_decision    effect: prepare
    confirm_project_capability_row_decision    effect: confirmed_write

The last two are story 71.4 and they decide ONE ROW of a capability, never the
capability's activation. The long comment above them says why that is a second
pair rather than a fifth intent on the first, and why both pairs are one
ceremony. Neither is capability-specific: they take a ``capability_key`` and
dispatch, exactly as ``_foundation_summary`` does, on the doctrine the header of
``core/fee_tax_mcp.py`` states -- a capability gets no tool family of its own
where the generic surface serves the verb.

There is no MCP-owned coverage logic, no MCP-only table and no parallel proposal
shape. Every function below delegates to ``core.project_settings`` and
``core.capability_proposals``, so an equivalent request over REST and over MCP
produces byte-identical proposal and impact hashes. That equality is the whole
point of the parity claim, and it is asserted by a test rather than described.

Three gates, in this order:

* **Profile.** Every tool is declared under ``governance`` and stays invisible
  until an authorized, endpoint/workspace-bound capability context opts in.
* **Interactive presence.** ``confirm`` is a ``confirmed_write``, so the shared
  middleware hides it and denies a direct call unless the server has minted
  presence evidence for this endpoint. A non-interactive host may read and
  prepare, and receives an authenticated console deep link instead.
* **Confirmation secrecy.** The trusted confirmation record is opaque and never
  model-visible. ``prepare`` returns a review reference -- a payload hash, not a
  credential -- and ``confirm`` accepts a confirmation the console issued. No
  tool result, ``structuredContent`` field, prompt, URL or audit payload here
  carries confirmation material.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

CAPABILITY_IMPACT_APP_URI = "ui://core/project-capability-impact"

#: The bounded payload the MCP App and the Console impact matrix both render.
#: Bounded on purpose: a review surface that streams every proposal field would
#: exceed a host's payload budget precisely when a Project has enough Datastreams
#: for the review to matter.
_MAX_MATRIX_ROWS = 60

#: AC10: model-visible content stays bounded. An organization with fifty published
#: presets must not put fifty of them in a model's context; the block carries the
#: TRUE count beside the capped list, so the cap is never read as the total.
_MAX_TAX_PROPOSALS = 8


class ProjectCapabilityToolError(RuntimeError):
    """A safe, structured refusal carrying ``{code, message}``."""


def _tool_error(code: str, message: str):
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _capability_grants() -> dict[str, Any]:
    from core.mcp_profiles import _capability_context  # noqa: PLC0415

    _identity_value, _host_context, grants = _capability_context()
    return grants


def console_deep_link(
    project_id: str, *, section: str = "changes", change_set_id: str | None = None
) -> dict[str, Any]:
    """Return an authenticated console reference -- semantic, never a raw URL.

    The console builds the href from the canonical navigation registry. Handing a
    host a hard-coded path would freeze a route this module does not own, and a
    non-interactive host needs a destination it can still reach after Epic 49
    renames a screen.
    """
    from core.project_overview import owner_reference  # noqa: PLC0415

    reference = owner_reference("overview", "project-overview")
    reference.update(
        {
            "surface": "global",
            "workspace": None,
            "section": None,
            "global_surface": "project-settings",
            "global_section": section,
        }
    )
    return {
        "kind": "console",
        "requires_authenticated_session": True,
        "project_id": project_id,
        "change_set_id": change_set_id,
        "owner_reference": reference,
    }


def _authorize(project_id: str, *, minimum_capability: str) -> tuple[str, str]:
    """Resolve identity and organization, fail closed on any missing grant."""
    from core.admin_api import _strict_project_capability_allowed  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415

    identity = _identity()
    if not identity or identity == "anonymous":
        raise _tool_error("not_found", "Resource not found.")
    with request_connection(identity) as conn:
        if not _strict_project_capability_allowed(
            conn,
            identity=identity,
            project_id=project_id,
            minimum_capability=minimum_capability,
            hold_access=minimum_capability == "manage",
        ):
            # Indistinguishable from absence: existence is sensitive here.
            raise _tool_error("not_found", "Resource not found.")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id FROM app.projects WHERE id = %s AND status = 'active'",
                (project_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise _tool_error("not_found", "Resource not found.")
    return identity, str(row[0])


def _envelope(text: str, structured: dict[str, Any]) -> dict[str, Any]:
    """Split the short model channel from the full canonical envelope (AD-1)."""
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured}


# ---------------------------------------------------------------------------
# read -- one capability across every Datastream.
# ---------------------------------------------------------------------------


def _read_project_capability(
    project_id: str,
    capability_key: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One capability, its coverage, and the bounded block its key earns.

    ``options`` is the ONE extra argument, and it is a bag rather than four named
    parameters because it belongs to whichever key is being read: Analytics
    Alignment answers about a PAIR and a BREAKDOWN, and money and time answer
    about neither. Four top-level parameters would put an alignment word on the
    signature of a read of `tax_fees`, and the next capability would put a fifth
    there. Unknown keys inside it are ignored by the block that does not own them.
    """
    from core.capability_proposals import read_project_capability  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415

    identity, _org_id = _authorize(project_id, minimum_capability="view")
    with request_connection(identity) as conn:
        projection = read_project_capability(
            conn, project_id=project_id, capability_key=capability_key
        )
        foundation = _foundation_summary(conn, project_id, capability_key, options=options)
    coverage = projection["coverage"]
    summary = (
        f"{capability_key}: {coverage['label']} over {coverage['applicable']} applicable "
        f"Datastream(s); {coverage['not_applicable']} not applicable."
    )
    if foundation.get("headline"):
        summary = f"{summary} {foundation['headline']}"
    return _envelope(
        summary,
        {
            **projection,
            **({"foundation": foundation} if foundation else {}),
            "console": console_deep_link(project_id, section="capabilities"),
            # Story 50.6 -- REMOVED: "app_resource": CAPABILITY_IMPACT_APP_URI.
            # A model-visible field advertising an app resource is the wrong
            # channel twice over: wrong key (a widget resource belongs in the
            # declaration or in result `_meta.ui`, never in structuredContent)
            # and wrong tool (this is a data tool; only the render tool
            # advertises a widget). The resource itself stays registered.
        },
    )


def _foundation_summary(
    conn,
    project_id: str,
    capability_key: str,
    *,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The bounded Story 48.3 summary for the two always-present foundations.

    AC10 keeps this on the GENERIC tool: "no new tool family or authority is
    introduced". So the money and time evidence arrives as one extra block on
    ``read_project_capability`` rather than as a `read_money_policy` sibling that
    would immediately be a second place to ask the same question.

    Bounded on purpose. The active policy version, the coverage-relevant state and
    the rate freshness fit in a model's context; the rate table and the
    per-Datastream matrix do not, and they stay behind the App resource already
    referenced above. A read failure returns an empty block rather than raising:
    the capability projection is still worth answering, and the caller sees the
    absence rather than a partial claim.
    """

    if capability_key == "currency_fx":
        try:
            from core.fx_rate_sets import rate_freshness  # noqa: PLC0415
            from core.money_policy import try_resolve_money_policy  # noqa: PLC0415

            policy = try_resolve_money_policy(conn, project_id=project_id)
            freshness = rate_freshness(conn, project_id=project_id)
        except Exception as exc:  # noqa: BLE001 -- an unreadable owner is not a crash
            logger.warning("capability_mcp: money foundation read failed: %s", exc)
            return {}
        if policy is None:
            return {
                "capability": "currency_fx",
                "confirmed": False,
                "headline": "No Money Policy is confirmed, so no monetary value converts.",
                "gap_code": "money_policy_unconfirmed",
            }
        return {
            "capability": "currency_fx",
            "confirmed": True,
            "headline": (
                f"Reporting currency {policy.reporting_currency} under policy "
                f"{policy.version_id}; rates {freshness.get('state', 'unknown')}."
            ),
            "reporting_currency": policy.reporting_currency,
            "money_policy_version_id": policy.version_id,
            "rounding": policy.rounding,
            "max_staleness_days": policy.max_staleness_days,
            "allow_triangulation": policy.allow_triangulation,
            "allow_carry_forward": policy.allow_carry_forward,
            "rate_freshness": freshness,
            "owner_reference": policy.owner_reference(),
        }

    if capability_key == "reporting_timezone":
        try:
            from core.money_policy import try_resolve_timezone_policy  # noqa: PLC0415

            policy = try_resolve_timezone_policy(conn, project_id=project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("capability_mcp: timezone foundation read failed: %s", exc)
            return {}
        if policy is None:
            return {
                "capability": "reporting_timezone",
                "confirmed": False,
                "headline": (
                    "No Reporting Timezone Policy is confirmed, so no cross-source day "
                    "equivalence can be asserted."
                ),
                "gap_code": "timezone_policy_unconfirmed",
            }
        return {
            "capability": "reporting_timezone",
            "confirmed": True,
            "headline": (
                f"Reporting day drawn on {policy.reporting_timezone} under policy "
                f"{policy.version_id} (tzdb {policy.tzdb_version})."
            ),
            "reporting_timezone": policy.reporting_timezone,
            "timezone_policy_version_id": policy.version_id,
            "tzdb_version": policy.tzdb_version,
            "timestamp_derivation": policy.timestamp_derivation,
            "dst_gap_policy": policy.dst_gap_policy,
            "dst_overlap_policy": policy.dst_overlap_policy,
            "assumptions": [dict(item) for item in policy.assumptions],
            "owner_reference": policy.owner_reference(),
        }

    if capability_key == "tax_fees":
        # AUTO-PROPOSE, the fourth verb `capabilities/tax-fees.md` asks of MCP
        # ("Inspect, auto-propose, prepare and human-confirm rule changes").
        #
        # The other three are the generic tools registered below: read, prepare and
        # confirm. Auto-propose was served by `fee_tax_mcp.auto_populate_tax_rules`
        # alone -- in a module the 41.6 cutover removed from `main.py`. So a verb
        # the ratified target requires was SILENTLY UNCOVERED, while the module
        # that once carried it looked like dead weight waiting to be deleted.
        # Deleting it first would have removed the only trace of the gap.
        #
        # It arrives here for the same reason money, time and competitors do: a
        # second tool family is a second place to ask one question. Story 48.5 set
        # the precedent when it removed `brand_registry_mcp.py` and its twelve
        # bespoke tools; this is that move for Tax & Fees.
        try:
            from core.capability_compilers import (  # noqa: PLC0415
                _tax_fee_preset_proposals,
            )

            org_id = _project_org_id(conn, project_id)
            proposals = _tax_fee_preset_proposals(
                conn, project_id=project_id, org_id=org_id
            )
        except Exception as exc:  # noqa: BLE001 -- an unreadable owner is not a zero
            logger.warning("capability_mcp: tax proposal read failed: %s", exc)
            return {}
        if not proposals:
            return {
                "capability": "tax_fees",
                "proposal_count": 0,
                "preset_proposals": [],
                "headline": (
                    "No published preset covers a jurisdiction this Project governs, "
                    "so there is nothing to propose."
                ),
                # Not an empty list left to be read as "no fee applies here". The
                # two are different claims and only one of them is knowable.
                "gap_code": "no_qualified_tax_proposal",
            }
        return {
            "capability": "tax_fees",
            # The TRUE count, beside a capped list: a reader must never be shown
            # the cap and told it is the total.
            "proposal_count": len(proposals),
            "preset_proposals": proposals[:_MAX_TAX_PROPOSALS],
            "headline": (
                f"{len(proposals)} qualified rule proposal(s) match this Project's "
                "jurisdictions; each carries its source and what is still unproven. "
                "Narrowing by Country does not prove that a rule applies."
            ),
        }

    if capability_key == "competitors":
        # Story 48.5 removed `brand_registry_mcp.py` and its twelve bespoke
        # `registry_*` tools, which wrote to the old mutable stores directly,
        # bypassed the Project fan-out lifecycle and spoke French to the model.
        # The projection arrives here for the same reason the two foundations
        # above do: a second tool family is a second place to ask one question.
        try:
            from core.entity_bindings import list_bindings  # noqa: PLC0415
            from core.tracked_entities import list_decisions, project_roles  # noqa: PLC0415

            org_id = _project_org_id(conn, project_id)
            roles = project_roles(conn, project_id=project_id)
            bindings = list_bindings(conn, project_id=project_id)
            pending = [
                item
                for item in list_decisions(conn, org_id=org_id, project_id=project_id)
                if item["state"] == "proposed"
            ]
        except Exception as exc:  # noqa: BLE001 -- an unreadable owner is not a crash
            logger.warning("capability_mcp: competitors projection read failed: %s", exc)
            return {}
        published = [item for item in bindings if item["application_state"] == "published"]
        excluded = [item for item in bindings if item["application_state"] == "excluded"]
        by_role: dict[str, int] = {}
        for role in roles:
            key = str(role["project_role"])
            by_role[key] = by_role.get(key, 0) + 1
        return {
            "capability": "competitors",
            "confirmed": bool(roles),
            "headline": (
                f"{len(roles)} tracked identity/identities in this Project; "
                f"{len(published)} published binding(s), {len(pending)} candidate(s) "
                "awaiting a decision."
            ),
            "entities_by_role": by_role,
            "published_binding_count": len(published),
            "candidate_binding_count": len(bindings) - len(published) - len(excluded),
            "excluded_binding_count": len(excluded),
            "pending_decision_count": len(pending),
            # Stated on every read, because it is the inference this capability
            # exists to prevent: knowing WHO was observed is not permission to
            # compare WHAT was measured.
            "comparison_note": (
                "Identity alignment resolves which entity a source value denotes. "
                "Comparing metrics across sources additionally requires a published "
                "Semantic View and an explicit commensurability decision."
            ),
            "owner_reference": {
                "surface": "project",
                "workspace": "governance",
                "section": "master-data",
                "lens": "competitor-registry",
            },
        }

    if capability_key == "analytics_alignment":
        # Story 71.4 -- the FIRST CALLER, and it arrives here rather than as an
        # `read_analytics_alignment` sibling for the reason the header of
        # `core/fee_tax_mcp.py` spells out at length: a capability gets NO
        # capability-specific tool where the GENERIC surface already serves the
        # verb. `capabilities/analytics-alignment.md` asks MCP for two things --
        # "read the alignment" and "read a split and the volume it rode on" --
        # and both are reads of one capability, which is what this tool is.
        #
        # It is also the first caller of the measurement grain: the `breakdown`
        # option is refused by name when no governed grain relates the dimension
        # to the metric (`governance.md` § Amendment 2026-08-27, last clause of
        # its `Incomplete if`).
        try:
            from core.analytics_alignment_read import read_alignment  # noqa: PLC0415
            from core.project_capability_states import (  # noqa: PLC0415
                read_capability_state,
            )

            state = read_capability_state(
                conn, project_id=project_id, capability_key="analytics_alignment"
            )
            selected = options or {}
            breakdown = selected.get("breakdown")
            return read_alignment(
                conn,
                project_id=project_id,
                capability_state=state,
                left_datastream_id=selected.get("left_datastream_id"),
                right_datastream_id=selected.get("right_datastream_id"),
                breakdown=breakdown if isinstance(breakdown, dict) else None,
            )
        except Exception as exc:  # noqa: BLE001 -- an unreadable owner is not a zero
            logger.warning("capability_mcp: alignment read failed: %s", exc)
            return {}

    if capability_key == "tax_fees":
        # Story 41.8. The SAME bounded summary `get_fee_tax_rules` returns, called
        # here rather than re-derived, so the generic capability read and the
        # specific Tax & Fee read cannot disagree about a version, a rule count or
        # a posture. AC10's rule is unchanged: this stays on the generic tool.
        try:
            from core.fee_tax_mcp import ladder_summary  # noqa: PLC0415

            return ladder_summary(conn, project_id)
        except Exception as exc:  # noqa: BLE001 -- an unreadable owner is not a crash
            logger.warning("capability_mcp: tax_fees ladder read failed: %s", exc)
            return {}

    return {}


def _project_org_id(conn, project_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if row is None:
        raise LookupError("project not found")
    return str(row[0])


# ---------------------------------------------------------------------------
# read -- the frozen impact of a prepared Change Set.
# ---------------------------------------------------------------------------


def _bounded_matrix(matrix: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Bound the matrix and say what was dropped. A silent cap reads as coverage."""
    if len(matrix) <= _MAX_MATRIX_ROWS:
        return matrix, 0
    return matrix[:_MAX_MATRIX_ROWS], len(matrix) - _MAX_MATRIX_ROWS


def _preview_project_capability_impact(project_id: str, change_set_id: str) -> dict[str, Any]:
    from core.db import request_connection  # noqa: PLC0415
    from core.project_settings import read_change_set_impact  # noqa: PLC0415

    identity, _org_id = _authorize(project_id, minimum_capability="view")
    with request_connection(identity) as conn:
        impact = read_change_set_impact(
            conn, project_id=project_id, change_set_id=change_set_id
        )
    matrix, withheld = _bounded_matrix(impact.get("matrix") or [])
    blockers = impact.get("blockers") or []
    summary = (
        f"Change Set {change_set_id} is {impact['state']}: {len(matrix)} proposal row(s) "
        f"shown, {len(blockers)} blocker(s). This preview authorizes nothing."
    )
    if withheld:
        summary += f" {withheld} further row(s) withheld by the payload bound."
    return _envelope(
        summary,
        {
            **impact,
            "matrix": matrix,
            "matrix_rows_withheld": withheld,
            "console": console_deep_link(
                project_id, section="changes", change_set_id=change_set_id
            ),
            # Story 50.6 -- REMOVED: "app_resource": CAPABILITY_IMPACT_APP_URI.
            # A model-visible field advertising an app resource is the wrong
            # channel twice over: wrong key (a widget resource belongs in the
            # declaration or in result `_meta.ui`, never in structuredContent)
            # and wrong tool (this is a data tool; only the render tool
            # advertises a widget). The resource itself stays registered.
        },
    )


# ---------------------------------------------------------------------------
# prepare -- compile and freeze. Non-authorizing, by contract.
# ---------------------------------------------------------------------------


def _prepare_project_capability_change(
    project_id: str, intent: dict[str, Any], idempotency_key: str
) -> dict[str, Any]:
    from core.db import request_connection  # noqa: PLC0415
    from core.main import get_loaded_modules  # noqa: PLC0415
    from core.project_settings import (  # noqa: PLC0415
        ProjectSettingsValidationError,
        create_change_set,
        prepare_change_set,
    )

    identity, _org_id = _authorize(project_id, minimum_capability="edit")
    if not isinstance(intent, dict) or not intent:
        raise _tool_error("invalid_intent", "The intent must be a non-empty object.")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise _tool_error("missing_idempotency_key", "An idempotency key is required.")
    try:
        with request_connection(identity) as conn:
            created = create_change_set(
                conn,
                project_id=project_id,
                intent=intent,
                actor=identity,
                idempotency_key=idempotency_key.strip(),
            )
            prepared = prepare_change_set(
                conn,
                project_id=project_id,
                change_set_id=created["id"],
                actor=identity,
                loaded_modules=get_loaded_modules(),
            )
            conn.commit()
    except ProjectSettingsValidationError as exc:
        raise _tool_error("invalid_project_settings", str(exc)) from exc
    matrix, withheld = _bounded_matrix((prepared.get("impact") or {}).get("matrix") or [])
    blockers = prepared.get("blockers") or []
    interactive = _presence_available()
    summary = (
        f"Prepared Change Set {created['id']} with {len(matrix)} proposal row(s) and "
        f"{len(blockers)} blocker(s). Nothing is active: a separate trusted "
        "confirmation is required."
    )
    if not interactive:
        summary += " This host cannot confirm; open the console to review and confirm."
    return _envelope(
        summary,
        {
            "schema": "project_capability_prepare.v1",
            "project_id": project_id,
            "change_set_id": created["id"],
            "state": "blocked" if blockers else "prepared",
            # A review reference, not a credential: it identifies the frozen payload
            # and cannot be replayed as approval.
            "review_reference": prepared["prepared_payload_hash"],
            "authorizing": False,
            "coverage": (prepared.get("impact") or {}).get("coverage"),
            "matrix": matrix,
            "matrix_rows_withheld": withheld,
            "blockers": blockers,
            "exceptions": prepared.get("exceptions", []),
            "referenced_versions": prepared.get("referenced_versions", []),
            "confirmation_available": interactive,
            "console": console_deep_link(
                project_id, section="changes", change_set_id=created["id"]
            ),
            # Story 50.6 -- REMOVED: "app_resource": CAPABILITY_IMPACT_APP_URI.
            # A model-visible field advertising an app resource is the wrong
            # channel twice over: wrong key (a widget resource belongs in the
            # declaration or in result `_meta.ui`, never in structuredContent)
            # and wrong tool (this is a data tool; only the render tool
            # advertises a widget). The resource itself stays registered.
        },
    )


def _presence_available() -> bool:
    from core.mcp_profiles import interactive_presence_verified  # noqa: PLC0415

    try:
        return interactive_presence_verified(_capability_grants())
    except Exception as exc:  # noqa: BLE001 -- fail closed to non-interactive.
        logger.debug("project_capabilities_mcp: presence resolution failed (%s)", exc)
        return False


# ---------------------------------------------------------------------------
# confirmed_write -- invokes an existing single-use trusted confirmation only.
# ---------------------------------------------------------------------------


def _confirm_project_capability_change(
    project_id: str,
    change_set_id: str,
    review_reference: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Activate a Project Configuration Version through the existing ceremony.

    This tool takes **no confirmation material** -- deliberately. There is nothing
    a host could pass that would not land in model-visible tool arguments, so the
    server resolves the trusted confirmation from the presence evidence it minted
    for this endpoint and workspace: evidence the host cannot forge and the model
    never sees. ``review_reference`` is the frozen payload hash, which identifies
    *which* review is being confirmed and authorizes nothing on its own.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.entry_confirmations import (  # noqa: PLC0415
        EntryConfirmationRefused,
        resolve_presence_bound_confirmation,
    )
    from core.main import get_loaded_modules  # noqa: PLC0415
    from core.project_settings import (  # noqa: PLC0415
        PROJECT_SETTINGS_COMMAND,
        ProjectSettingsBlocked,
        ProjectSettingsStale,
        confirm_change_set,
    )
    from core.tracing import current_trace_id_hex  # noqa: PLC0415

    grants = _capability_grants()
    if not _presence_available():
        # Belt and braces: the shared middleware already hides and denies this tool.
        raise _tool_error("not_found", "Resource not found.")
    identity, org_id = _authorize(project_id, minimum_capability="manage")
    if not isinstance(review_reference, str) or len(review_reference) != 64:
        raise _tool_error("invalid_review_reference", "The review reference is not valid.")
    with request_connection(identity) as conn:
        person_id = _person_for_identity(conn, identity)
        change_set = _load_confirmable_change_set(conn, project_id, change_set_id)
        if change_set["prepared_payload_hash"] != review_reference:
            raise _tool_error(
                "stale_change_set", "The review no longer matches the prepared state."
            )
        request_payload = {
            "project_id": project_id,
            "org_id": org_id,
            "change_set_id": change_set_id,
            "prepared_payload_hash": change_set["prepared_payload_hash"],
            "dependency_fingerprint": change_set["dependency_fingerprint"],
        }
        try:
            confirmation_id = resolve_presence_bound_confirmation(
                conn,
                command_type=PROJECT_SETTINGS_COMMAND,
                request_payload=request_payload,
                presence_evidence_hash=str(grants.get("interactive_presence_evidence_hash")),
                endpoint_binding=str(grants.get("endpoint_binding")),
                workspace_evidence_hash=str(grants.get("workspace_evidence_hash")),
            )
        except EntryConfirmationRefused as exc:
            raise _tool_error(
                "confirmation_required",
                "No trust confirmation is bound to this review.",
            ) from exc
        try:
            result = confirm_change_set(
                conn,
                project_id=project_id,
                org_id=org_id,
                change_set_id=change_set_id,
                actor_person_id=person_id,
                confirmation_id=confirmation_id,
                confirmation_secret=None,
                prepared_payload_hash=review_reference,
                idempotency_key=idempotency_key,
                host_context={"host": "mcp-app", "workspace_id": "mcp"},
                trace_id=current_trace_id_hex(),
                loaded_modules=get_loaded_modules(),
            )
            conn.commit()
        except ProjectSettingsStale as exc:
            conn.rollback()
            raise _tool_error("stale_change_set", str(exc)) from exc
        except ProjectSettingsBlocked as exc:
            conn.rollback()
            raise _tool_error("change_set_blocked", str(exc)) from exc
        except Exception as exc:
            conn.rollback()
            # The class only: an exception message here could carry bindings.
            logger.error(
                "project_capabilities_mcp: confirm failed (%s)", type(exc).__name__
            )
            raise _tool_error("confirmation_refused", "Confirmation refused.") from exc
    outcome = result.get("outcome")
    return _envelope(
        f"Change Set {change_set_id} {outcome}. No Data publication pointer moved.",
        {
            "schema": "project_capability_confirm.v1",
            "project_id": project_id,
            "change_set_id": change_set_id,
            "operation_id": result.get("operation_id"),
            "outcome": outcome,
            "result": result.get("result", {}),
            "idempotent_replay": bool(result.get("idempotent_replay")),
            "console": console_deep_link(
                project_id, section="changes", change_set_id=change_set_id
            ),
        },
    )


def _person_for_identity(conn, identity: str) -> str:
    """Resolve the canonical person behind the authenticated MCP subject."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT person_id FROM app.person_identities WHERE subject = %s",
            (identity,),
        )
        row = cur.fetchone()
    if row is None:
        raise _tool_error("not_found", "Resource not found.")
    return str(row[0])


def _load_confirmable_change_set(conn, project_id: str, change_set_id: str) -> dict[str, Any]:
    """Read the prepared bindings a confirmation must match. Never recompiles."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT state, prepared_payload_hash, dependency_fingerprint
            FROM app.project_change_sets WHERE id = %s AND project_id = %s
            """,
            (change_set_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise _tool_error("not_found", "Resource not found.")
    if row[0] != "prepared":
        raise _tool_error(
            "change_set_blocked", "Only a prepared, unblocked Change Set can be confirmed."
        )
    return {
        "state": row[0],
        "prepared_payload_hash": row[1],
        "dependency_fingerprint": row[2],
    }


# ---------------------------------------------------------------------------
# prepare / confirm -- ONE ROW of a capability, not the capability's activation.
#
# Story 71.4. `capabilities/analytics-alignment.md` asks MCP to "prepare and
# human-confirm an arbitration, never write one silently". Two shapes were open,
# and the story asked for whichever keeps ONE confirmation ceremony:
#
#   (a) extend the Change Set pair above with `intent.kind = "alignment_decision"`;
#   (b) a second prepare/confirm pair, generic in shape, here.
#
# (b), and the reason is what a Change Set IS. `project_settings._validate_intent`
# accepts `{defaults, capabilities, selected_owner_ids}` and nothing else, and
# `confirm_change_set` ACTIVATES a Project Configuration Version. An arbitration on
# one row changes no configuration and must mint no version, so routing it through
# that pair would make one Change Set mean two different things and would put a
# Configuration Version behind a click that moved no setting. It would also require
# editing `project_settings.py`, whose authority boundary is the point of that
# validator.
#
# What IS shared is the ceremony, which is the part the doctrine cares about: the
# same three gates (profile, interactive presence, confirmation secrecy), the same
# `effect="prepare"` / `effect="confirmed_write"` declarations, and the same
# contract that a review reference is a payload HASH and not a credential. And the
# pair is generic in shape -- it takes `capability_key` and dispatches, exactly as
# `_foundation_summary` does, so the next capability with a row-level act arrives
# as a branch and not as a third tool family.
# ---------------------------------------------------------------------------

#: The capabilities that have a row-level act at all. A key absent from here is
#: refused by name rather than silently answering "nothing to decide".
_ROW_DECISION_CAPABILITIES = ("analytics_alignment",)


def _refuse_unless_row_decision_capability(capability_key: str) -> None:
    if capability_key not in _ROW_DECISION_CAPABILITIES:
        raise _tool_error(
            "capability_has_no_row_decision",
            f"{capability_key} has no per-row decision. The capabilities that do are "
            f"{', '.join(_ROW_DECISION_CAPABILITIES)}.",
        )


def _prepare_project_capability_row_decision(
    project_id: str, capability_key: str, selector: dict[str, Any]
) -> dict[str, Any]:
    """Freeze the row a confirmation would settle. Writes nothing, authorizes nothing."""
    from core.analytics_alignment import AlignmentRefused  # noqa: PLC0415
    from core.analytics_alignment_read import prepare_row_decision  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415

    _refuse_unless_row_decision_capability(capability_key)
    if not isinstance(selector, dict) or not selector:
        raise _tool_error("invalid_selector", "The selector must be a non-empty object.")
    identity, _org_id = _authorize(project_id, minimum_capability="edit")
    try:
        with request_connection(identity) as conn:
            prepared = prepare_row_decision(
                conn,
                project_id=project_id,
                left_datastream_id=str(selector.get("left_datastream_id") or ""),
                right_datastream_id=str(selector.get("right_datastream_id") or ""),
                left_row_key=str(selector.get("left_row_key") or ""),
            )
    except AlignmentRefused as exc:
        raise _tool_error(exc.code, exc.message) from exc
    interactive = _presence_available()
    if prepared["already_decided"]:
        summary = (
            f"Row {prepared['left_row_key']} is already decided; the FIRST decision "
            f"stands, so a confirmation would write nothing."
        )
    else:
        summary = (
            f"Prepared a decision on row {prepared['left_row_key']}. Nothing is written: "
            f"a separate human confirmation is required."
        )
    if not interactive:
        summary += " This host cannot confirm; open the console to review and confirm."
    return _envelope(
        summary,
        {
            "schema": "project_capability_row_prepare.v1",
            "project_id": project_id,
            "capability_key": capability_key,
            **prepared,
            "confirmation_available": interactive,
            "console": console_deep_link(project_id, section="capabilities"),
        },
    )


def _confirm_project_capability_row_decision(
    project_id: str,
    capability_key: str,
    review_reference: str,
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Write ONE decision, under an existing human confirmation. The first stands.

    NO ``idempotency_key``, and that is deliberate rather than an omission. The
    store's own unique index plus ``ON CONFLICT DO NOTHING`` makes the write
    idempotent by the ROW's identity -- which is stronger than a caller-chosen key,
    because two callers with two different keys still leave one decision on one
    row. A key here would be a second, weaker idempotency nobody could reconcile
    with the first.
    """
    from core.analytics_alignment import (  # noqa: PLC0415
        AlignmentDecision,
        AlignmentRefused,
        record_alignment_decision,
    )
    from core.analytics_alignment_read import (  # noqa: PLC0415
        prepare_row_decision,
        refuse_undecidable_decision,
        review_reference_for,
    )
    from core.db import request_connection  # noqa: PLC0415

    _refuse_unless_row_decision_capability(capability_key)
    if not _presence_available():
        # Belt and braces: the shared middleware already hides and denies this tool.
        raise _tool_error("not_found", "Resource not found.")
    if not isinstance(review_reference, str) or len(review_reference) != 64:
        raise _tool_error("invalid_review_reference", "The review reference is not valid.")
    if not isinstance(decision, dict) or not decision:
        raise _tool_error("invalid_decision", "The decision must be a non-empty object.")
    identity, org_id = _authorize(project_id, minimum_capability="manage")

    left = str(decision.get("left_datastream_id") or "")
    right = str(decision.get("right_datastream_id") or "")
    row_key = str(decision.get("left_row_key") or "")
    try:
        with request_connection(identity) as conn:
            person_id = _person_for_identity(conn, identity)
            current = review_reference_for(
                conn,
                project_id=project_id,
                left_datastream_id=left,
                right_datastream_id=right,
                left_row_key=row_key,
            )
            if current != review_reference:
                raise _tool_error(
                    "stale_review",
                    "The row moved since it was prepared: a decision landed on it, or the "
                    "pair no longer crosses on the same key version. Prepare it again.",
                )
            prepared = prepare_row_decision(
                conn,
                project_id=project_id,
                left_datastream_id=left,
                right_datastream_id=right,
                left_row_key=row_key,
            )
            pair = prepared["pair"]
            # THE ROW AND THE ENTITY IT PICKS ARE CHECKED BEFORE THE INSERT.
            # Without this an arbitration naming a row the right side does not
            # publish is stored, and `run_cascade` then refuses the pair on every
            # later read -- for ever, because the first decision stands and nothing
            # withdraws one. Measured 2026-08-28; the domain owns the rule
            # (`analytics_alignment_read.refuse_undecidable_decision`) so a console
            # door gets the same one rather than a second copy.
            verified = refuse_undecidable_decision(
                conn,
                project_id=project_id,
                left_datastream_id=left,
                right_datastream_id=right,
                left_row_key=row_key,
                decision=str(decision.get("decision") or ""),
                right_row_key=(
                    str(decision["right_row_key"])
                    if decision.get("right_row_key") is not None
                    else None
                ),
            )
            requested = AlignmentDecision(
                left_row_key=row_key,
                decision=str(decision.get("decision") or ""),
                right_row_key=(
                    str(decision["right_row_key"])
                    if decision.get("right_row_key") is not None
                    else None
                ),
                reason=(
                    str(decision["reason"]) if decision.get("reason") is not None else None
                ),
                decided_by=person_id,
            )
            stored = record_alignment_decision(
                conn,
                org_id=org_id,
                project_id=project_id,
                left_datastream_id=str(pair["left_datastream_id"]),
                right_datastream_id=str(pair["right_datastream_id"]),
                common_key_version_id=str(pair["common_key_version_id"]),
                decision=requested,
                actor=identity,
            )
            conn.commit()
    except AlignmentRefused as exc:
        raise _tool_error(exc.code, exc.message) from exc
    # THE FIRST DECISION STANDS. When the row was already settled the store returns
    # the FIRST author and the FIRST date, and this says so rather than reporting a
    # write that did not happen.
    #
    # Taken from the PREPARE, which read the row's identity in this same
    # transaction, and no longer inferred by comparing fields. The comparison
    # missed `reason`: the same person re-confirming the same pick with a NEW
    # reason got `already_decided: false` while nothing had been written and the
    # OLD reason came back on the payload. A row either already carried a decision
    # or it did not, and only the store can answer that.
    already = bool(prepared["already_decided"])
    return _envelope(
        (
            f"Row {row_key} stands as {stored.decision} by {stored.decided_by}."
            + (" It was already decided; nothing was written." if already else "")
        ),
        {
            "schema": "project_capability_row_confirm.v1",
            "project_id": project_id,
            "capability_key": capability_key,
            "pair": pair,
            "decision": {
                "left_row_key": stored.left_row_key,
                "decision": stored.decision,
                "right_row_key": stored.right_row_key,
                "reason": stored.reason,
                "decided_by": stored.decided_by,
                "decided_at": stored.decided_at,
            },
            "already_decided": already,
            # What the cascade thought of the row this decision settles, so the
            # payload says WHICH row was verified and not merely that one was.
            "verified_row": verified["row"],
            "console": console_deep_link(project_id, section="capabilities"),
        },
    )


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def read_project_capability(
    project_id: str, capability_key: str, options: dict | None = None
):
    """Read one Project capability's exact per-Datastream coverage and owners.

    ``options`` carries what only some capabilities answer about. Analytics
    Alignment reads ``{"left_datastream_id", "right_datastream_id",
    "breakdown": {"metric", "dimensions"}}``; a breakdown by a dimension no
    governed measurement grain relates to the metric is refused by name.
    """
    return _read_project_capability(project_id, capability_key, options)


def preview_project_capability_impact(project_id: str, change_set_id: str):
    """Read the frozen impact of a prepared capability Change Set. Authorizes nothing."""
    return _preview_project_capability_impact(project_id, change_set_id)


def prepare_project_capability_change(
    project_id: str, intent: dict, idempotency_key: str
):
    """Compile and freeze a capability change for review. Non-authorizing."""
    return _prepare_project_capability_change(project_id, intent, idempotency_key)


def confirm_project_capability_change(
    project_id: str,
    change_set_id: str,
    review_reference: str,
    idempotency_key: str,
):
    """Activate a reviewed capability change using an existing trusted confirmation."""
    return _confirm_project_capability_change(
        project_id, change_set_id, review_reference, idempotency_key
    )


def prepare_project_capability_row_decision(
    project_id: str, capability_key: str, selector: dict
):
    """Freeze one row of a capability for a human decision. Non-authorizing."""
    return _prepare_project_capability_row_decision(project_id, capability_key, selector)


def confirm_project_capability_row_decision(
    project_id: str, capability_key: str, review_reference: str, decision: dict
):
    """Record one reviewed row decision. The FIRST decision on a row stands."""
    return _confirm_project_capability_row_decision(
        project_id, capability_key, review_reference, decision
    )


def register(mcp) -> None:
    """Register the six capability tools under the governance profile."""
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        read_project_capability,
        profile="governance",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        preview_project_capability_impact,
        profile="governance",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    # ONE CALL PER TOOL BELOW, and a second `for handler in (...)` loop is exactly
    # what this must not become. `tests/conformance/test_mcp_tools_resolve_project_scope.py`
    # reads the loop form, but only the FIRST loop of a module: its `_registered_tools`
    # discards the loop variable on the first `for` it meets and then skips every later
    # one, whose target is no longer in the set. Measured 2026-08-28 -- folding these
    # four into two loops made all four vanish from `scoped`, and the project-scope
    # ratchet reported them as REPAIRED rather than as unseen. A guard that stops
    # counting a tool is worse than one that fails on it.
    register_profiled(
        mcp,
        prepare_project_capability_change,
        profile="governance",
        effect="prepare",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        prepare_project_capability_row_decision,
        profile="governance",
        effect="prepare",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        confirm_project_capability_change,
        profile="governance",
        effect="confirmed_write",
        data_class="sensitive",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        confirm_project_capability_row_decision,
        profile="governance",
        effect="confirmed_write",
        data_class="sensitive",
        confirmation_mode="human",
    )
