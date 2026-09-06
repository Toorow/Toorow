"""Posting a fixed FX rate over MCP -- the door onto :mod:`core.fx_fixed_rates`.

MCP FIRST AND NO SCREEN, on the Tax & Fees precedent. The fee/tax capability was
built as a governed surface with no console screen of its own and that posture is
ratified; this follows it rather than inventing a second habit. A screen may come
later and will call the same primitive.

WHY A TOOL OF ITS OWN, when `test_no_tax_specific_mcp_tools` says a second tool
family is a second place to ask one question. The line that test draws is between
RULES and MASTER DATA, not between domains:

  * Tax & Fee RULES are a governed Rule Set, and the generic capability commands
    (`prepare_project_capability_change` / `confirm_project_capability_change`)
    already carry any rule set -- so bespoke tax CRUD tools were removed.
  * COUNTRY master data has its own trio (`master_data_mcp`), because a hierarchy
    of values is not a rule set and no generic command writes one.

A rate is master data of exactly that second kind: a value, versioned and
published, not a policy. `app.fx_rate_observations` is its store and no generic
command writes there. So this is the country door's shape, applied to rates --
same guard, same non-disclosing refusal, same envelope.

ONE TOOL, NOT THREE. The country trio exists because its REST door distinguishes
draft / prepare / publish and one `confirmation_mode` cannot be honest for all
three. Posting a rate is a single act: there is no draft to save and nothing to
prepare -- the value IS the decision. It is declared `confirmation_mode="host"`,
like `edit_country_master_data`: an agent setting the rate a Project's money is
converted at is exactly the consequential mutation the AD-27 ceremony exists for,
so the host asks before it runs.

AND STILL ONE TOOL AFTER THE CONDITIONAL RULE (2026-08-17). The amendment allows a
rate to be set "as a value in the table -- or as a simple conditional rule", and the
temptation was a second tool named for the second half. It was refused, because the
two halves are not two acts: an unconditional rate is one whose condition is empty,
which is how `core.fx_fixed_rates` stores it, how `resolve_rate` ranks it, and how a
person reading a spreadsheet thinks about it -- the `IF` is a column on the row, not
a different kind of row. A `set_conditional_fx_rate` beside this one would ask the
same question in two places, which is the exact shape `test_no_tax_specific_mcp_tools`
was written to prevent, and it would put the caller in front of a choice
(`which tool do I want?`) whose answer is already implied by whether they have a
condition. So `conditions` is one optional argument, empty by default, and the
signature stays honest: every parameter is still a property of the one rate posted.

Deny-by-default like its neighbours: an unauthorized caller gets the canonical
`project_not_found`, never `forbidden`, and a guard that cannot run answers the
same thing -- the rates a Project posts are the map of the currencies it trades
in, so the existence of another Project's rates is itself sensitive.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Posting a rate changes what every monetary figure of the Project means, so it
#: is the `manage` capability -- the same one the country PUBLISH takes, not the
#: `edit` a draft takes.
MINIMUM_CAPABILITY = "manage"


def _tool_error(code: str, message: str):
    import json  # noqa: PLC0415

    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _project_not_found():
    """The CANONICAL refusal for a Project the caller may not see.

    Reused, never re-spelled: `project_resolver` owns the code and the sentence.
    A tool that invented its own envelope would be an enumeration oracle wearing a
    security word -- comparing two refusals would tell a stranger which Project
    exists, and the rates a Project posts are the map of the currencies it trades
    in. `tests/isolation/test_mcp_tool_scope_refusal.py` proves this tool answers
    with the same envelope as every other scoped one.
    """
    from core.project_resolver import (  # noqa: PLC0415
        PROJECT_NOT_FOUND_CODE,
        PROJECT_NOT_FOUND_MESSAGE,
    )

    return _tool_error(PROJECT_NOT_FOUND_CODE, PROJECT_NOT_FOUND_MESSAGE)


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _envelope(text: str, structured: dict[str, Any]) -> dict[str, Any]:
    """Split the short model channel from the full canonical envelope (AD-1)."""
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured}


def _post(
    project_id: str,
    base_currency: str,
    quote_currency: str,
    rate: str,
    valid_from: str,
    valid_to: str,
    note: str,
    conditions: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Authorize, then hand over to the one writer. No second validator here."""
    from core.db import request_connection  # noqa: PLC0415
    from core.fx_fixed_rates import FixedRateRefused, post_fixed_rate  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    actor = _identity()
    if not actor or actor == "anonymous":
        raise _project_not_found()

    with request_connection(actor) as conn:
        # Fail-closed, and INDISTINGUISHABLE from a denial. A guard that cannot
        # run has granted nothing, and if a breakdown answered differently from a
        # refusal, dropping the database would become the way to ask whether a
        # Project exists.
        try:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability=MINIMUM_CAPABILITY,
                hold_access=True,
            )
        except Exception as exc:  # noqa: BLE001 -- fail-closed, never unguarded.
            logger.error(
                "fx_fixed_rate_mcp: access guard failed: %s", type(exc).__name__
            )
            raise _project_not_found() from exc
        if not decision.allowed or not decision.org_id:
            raise _project_not_found()
        try:
            result = post_fixed_rate(
                conn,
                org_id=str(decision.org_id),
                project_id=project_id,
                actor=actor,
                base_currency=base_currency,
                quote_currency=quote_currency,
                rate=rate,
                valid_from=valid_from,
                valid_to=valid_to,
                note=note,
                conditions=conditions,
            )
        except FixedRateRefused as exc:
            # The typed refusal travels whole: the caller is told which rule it
            # broke and what would satisfy it, not that "something was invalid".
            conn.rollback()
            raise _tool_error(exc.code, str(exc)) from exc
        conn.commit()
    return result


def _when(result: dict[str, Any]) -> str:
    """The condition, as the person who set it would say it. Empty when there is none.

    Spelled into the model-facing line rather than left in the structured payload:
    a rate that applies only in France and one that applies everywhere are different
    decisions, and a summary that reads identically for both invites the caller to
    confirm the wrong one.
    """
    conditions = result.get("conditions") or {}
    if not conditions:
        return ""
    clauses = [f"{key} is {' or '.join(conditions[key])}" for key in sorted(conditions)]
    return " when " + " and ".join(clauses)


def _summary(project_id: str, result: dict[str, Any]) -> str:
    if result.get("replayed"):
        return (
            f"{result.get('base_currency', 'rate')}/{result.get('quote_currency', '')} "
            f"already posted on {project_id}{_when(result)} "
            f"-- content_hash {result.get('content_hash')}"
        )
    carried = result.get("carried_forward") or 0
    also = f", {carried} existing rate(s) carried forward" if carried else ""
    return (
        f"{result['base_currency']}/{result['quote_currency']} fixed at {result['rate']} "
        f"on {project_id}{_when(result)} from {result['valid_from']} to "
        f"{result['valid_to']} -- method fixed, content_hash {result['content_hash']}"
        f"{also}"
    )


def set_fixed_fx_rate(
    project_id: str,
    base_currency: str,
    quote_currency: str,
    rate: str,
    valid_from: str,
    valid_to: str,
    note: str = "",
    conditions: dict[str, list[str]] | None = None,
):
    """Set the conversion rate for one currency pair over one period, as a value.

    The rate is recorded under the governed `fixed` method with who posed it
    and when it holds -- never as an observed quotation. `rate` is a string so
    it is stored exactly.

    `conditions` optionally names the case the rate holds for, in the spirit of a
    spreadsheet IF -- for example `{"country": ["FR", "BE"]}` or
    `{"connector": ["google_ads"]}`. Each key admits a LIST of values and the
    rate applies when every key matches. Omit it and the rate applies to the
    pair unconditionally, which is also what a conditional rate falls back to.
    Admitted keys: connector, country, market, datastream.

    Posting a rate for a pair and condition that already carries one REPLACES
    that rule and leaves the others standing.
    """
    result = _post(
        project_id,
        base_currency,
        quote_currency,
        rate,
        valid_from,
        valid_to,
        note,
        conditions,
    )
    return _envelope(_summary(project_id, result), result)


def register(mcp) -> None:
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        set_fixed_fx_rate,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="host",
    )
