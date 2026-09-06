"""The governed Tax & Fee ladder, as ONE READ HELPER. No MCP tool lives here.

⚠️ THIS MODULE HELD FOUR TAX-SPECIFIC MCP TOOLS AND NO LONGER DOES (2026-08-04).
``get_fee_tax_rules``, ``auto_populate_tax_rules``, ``add_fee_tax_rule`` and
``update_fee_tax_rule`` are removed, with ``register()``: 621 lines. Three ratified
sources asked for exactly that, and none of them was an arbitration --

* ``capabilities/tax-fees.md`` requires MCP coverage for "inspect, auto-propose,
  prepare and human-confirm", and Story 48.4's AC10 says the GENERIC capability
  and Rule Set commands serve it: "the four planned Tax-specific CRUD tools and
  custom Tax App are not created";
* Story 41.8's canonical correction of 2026-08-01: do not build **or keep** them;
* Story 41.6 could not finish while they existed.

THE ORDER MATTERED, AND IT IS THE WHOLE LESSON OF THIS REMOVAL. Three of the four
verbs were already served generically (``read_project_capability``,
``prepare_project_capability_change``, ``confirm_project_capability_change``, the
last with ``confirmation_mode="human"``). **Auto-propose was served by nobody**:
its only implementation was ``auto_populate_tax_rules`` here, in a module the 41.6
cutover had already removed from ``main.py``. So a verb the ratified target
requires was silently uncovered while this file looked like dead weight waiting to
be deleted -- and deleting it first would have erased the only trace of the gap.
It is now a bounded block on the generic read
(``project_capabilities_mcp._foundation_summary``, ``capability_key == "tax_fees"``),
the same move Story 48.5 made when it retired ``brand_registry_mcp.py`` and its
twelve tools. ``server/tests/conformance/test_no_tax_specific_mcp_tools.py`` keeps
them from coming back.

WHAT SURVIVES, AND WHY. ``ladder_summary`` alone, with its helpers: it has a live
production caller in ``project_capabilities_mcp``, which is the generic surface.
Everything reachable only from the removed tools went with them.

WHAT THIS MODULE IS NOT ALLOWED TO BE. Story 49.4's own instruction is in the
shipped source it delegates to (``core/controls_quality_api.py``): *"The Console
and any future MCP surface must call the same application services. Do not add a
second MCP-specific control authority."* Nothing here drafts, publishes or
activates:

* the ordered rules live in a ``tax_fee`` Rule Set version's ``ordered_rules``,
  frozen by trigger at publication (Story 48.4). ``app.fee_tax_rules`` is the
  superseded store and this module never reads or writes it;
* activation is ``app.project_capabilities``, projected by
  ``app.project_tax_fee_activation_v``. ``fee_tax_rules.set_fee_tax_alignment``
  is a typed refusal, and the one door is a Project Change Set in Project
  Settings > Capabilities -- ``prepare_project_capability_change`` already
  covers it, so no tool here flips a capability;
* matching a rule to a Datastream is ``capability_compilers._match_ladder`` and
  nothing else. A second matcher would be a second answer to "does this rule
  apply here", and the two would diverge the day one of them is fixed.

WHY A RATE IS VISIBLE IN ONE PAYLOAD AND NOT THE OTHER. A bounded ladder rule
summary deliberately carries no ``rate``, no ``tiers`` and no ``conditions``: a
model that can see a rate and cannot see the qualifications behind it will quote
the rate, and the full rule is readable in Governance at its exact version, which
``version_id`` + ``content_hash`` make precise. A preset CANDIDATE may carry its
rate, because there it arrives inside a payload whose ``unproven_qualifications``
and ``operator_must_confirm`` are adjacent and non-optional.

WHAT THIS SURFACE MUST NEVER BE READ AS. The ladder is a DEFINITION, so no summary
here may read as a composed invoice total.

⚠️ The reason this paragraph used to give was measured FALSE on 2026-08-04: it said
``fee_tax_ladder_daily`` "has never executed -- ``fact_daily_kpi`` predates
``fx_rate`` and 21 staging models lack raw seeds". The whole fee-tax graph builds
(`PASS=191`), and ``+fact_daily_kpi`` builds too (`PASS=268`): ONE staging model was
missing, not 21, and its loader already shipped. The rule above stands on its own --
a definition is not a total -- and no longer leans on a blocker that had expired.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

# THE BOUNDS BELOW ARE BYTE BUDGETS, NOT ROW COUNTS, AND THAT IS THE POINT.
#
# A row count is a guess about width. Measured here: one bounded rule summary
# carrying the thirteen fields AC3 fixes serializes at 379 bytes with a 32-character
# label, and one preset candidate at about 1600. A fixed count tuned against those
# fixtures passes the test and then REFUSES at runtime -- `enforce_result_model_channel`
# raises rather than truncating -- the first time a Project uses longer labels. So
# each list is filled while it still fits, and what did not fit is stated.

#: What the rule list may spend inside `ladder_summary`, which also lands in
#: `read_project_capability`'s envelope -- an envelope this module does not build
#: and therefore cannot measure. Conservative on purpose for that reason.
#: `get_fee_tax_rules`, which DOES own its envelope, then re-fits against the
#: assembled payload (`_fit_to_channel`) so a Datastream block or a long refusal
#: list takes its room from the rule list rather than from the budget.
_LADDER_RULES_BYTE_BUDGET = 1600

#: A ceiling on top of the byte budget: past this many rules a reader is not
#: reading a ladder, they are reading Governance, and the version id says where.
_MAX_LADDER_RULES = 12

#: How many undecided rule keys are listed. The COUNT is what an operator acts on;
#: the keys are a pointer, and a rule key may be 63 characters.
_MAX_UNRESOLVED_KEYS = 5

#: What the per-Datastream matched/refused lists may spend. `_match_ladder` returns
#: one refusal per non-matching rule, so a 30-rule ladder against a narrow stream
#: produces 30 of them -- most of the channel, for a list whose count is the
#: actionable part.
_MATCHED_KEYS_BYTE_BUDGET = 300
_REFUSED_BYTE_BUDGET = 600

#: Left free under `MODEL_CHANNEL_MAX_BYTES` when re-fitting an assembled payload.
#: The guard measures the envelope, this measures `data`, and the difference is the
#: `meta`/`schema_version` wrapper plus room for a host that adds a field.
_CHANNEL_SAFETY_MARGIN = 400

#: A label is display, never identity -- nothing matches on it, and `rule_key`
#: plus `version_id` are what make a rule findable in Governance. Capped so one
#: verbose label cannot spend a tenth of the whole model channel; the truncation
#: is marked rather than silent.
_MAX_RULE_LABEL_CHARS = 48

#: How many preset candidates stay model-visible. Smaller than the rule bound
#: because a candidate carries its unanswered questions IN WORDS plus the whole
#: draft rule -- measured at roughly three times a rule summary -- and dropping
#: either would leave a rate with nothing beside it, which is the exact defect
#: Story 48.4 removed. `test_the_candidate_payload_fits_the_model_channel_budget`
#: is the measurement.
_CANDIDATES_BYTE_BUDGET = 2900

#: The same ceiling-on-top-of-a-budget as the ladder: a proposal list a model has
#: to page through is a list nobody reads.
_MAX_CANDIDATES = 6

#: How many reported jurisdiction codes stay model-visible. A Project may report on
#: the whole ISO vocabulary, and 250 codes is more than a third of the budget spent
#: on something `jurisdiction_code_count` already states.
_MAX_JURISDICTION_CODES = 12

#: Why this module ships no `confirmed_write`, in one paragraph, as a named
#: constant rather than as a comment: a fresh session must not have to
#: rediscover it, and a test asserts it is here.
CONFIRMED_WRITE_ABSENT_REASON = (
    "There is no confirm_fee_tax_change tool, and that is a decision rather than an "
    "omission. Confirming a Controls & Quality change set requires the single-use token "
    "controls_change_sets.prepare_change_set returns exactly once; nothing a host could "
    "pass would avoid landing in model-visible tool arguments, so the token cannot be a "
    "tool argument. The alternative -- a presence-bound confirmation resolved server-side "
    "-- is unwired: entry_confirmations.ENTRY_COMMANDS declares no Controls & Quality "
    "command, and bind_confirmation_presence has no caller anywhere in the tree, so "
    "resolve_presence_bound_confirmation can never succeed and even the catalogue's "
    "existing confirmed_write on that path is unreachable. A fifth tool here would be a "
    "second dead ceremony. The human confirmation happens on the Console, which is a "
    "trusted interactive surface and is where the token is issued. A future story that "
    "wants an MCP confirmation must, in order: mint presence evidence from the Console, "
    "add a Controls & Quality command constant, add a presence-bound branch to "
    "confirm_change_set, and only then declare the tool."
)

#: The typed gap vocabulary of this surface. Reused verbatim from `core.tax_evidence`
#: where it already exists (`tax_evidence_unobserved` is the compiler's own blocker
#: code); a synonym would be a second name for one condition.
GAP_LADDER_UNPUBLISHED = "tax_fee_ladder_unpublished"
GAP_MONEY_POLICY_UNCONFIRMED = "money_policy_unconfirmed"
GAP_CAPABILITY_INACTIVE = "capability_inactive"
GAP_TAX_EVIDENCE_UNOBSERVED = "tax_evidence_unobserved"
GAP_LADDER_GEOGRAPHY_UNRESOLVED = "ladder_geography_unresolved"
GAP_COUNTRY_MEANING_UNPUBLISHED = "country_meaning_unpublished"
GAP_NO_PUBLISHED_PRESETS = "no_published_tax_fee_presets"

#: The repair sentence the compiler already emits for an unobserved Datastream.
#: Copied verbatim from `capability_compilers.TaxFeesCompiler.assess` so the two
#: surfaces cannot tell an operator two different things to do.
_TAX_EVIDENCE_REPAIR = (
    "Run this Datastream so its published rows record their source type, tax posture "
    "and required inputs."
)

_PRESET_IMPORT_REPAIR = (
    "No Tax & Fee preset version is published for this organization. Import the shipped "
    "reference seed with `python scripts/import_tax_fee_presets.py --publish`, then read "
    "each candidate before adopting it."
)

#: Read by explicit column list. The view's column order is append-only by
#: contract (migration 148), but a positional read would still break silently the
#: first time that contract is relaxed.
_ACTIVATION_COLUMNS = (
    "project_id",
    "project_configuration_version_id",
    "capability_state",
    "tax_fees_active",
    "rule_set_id",
    "rule_set_version_id",
    "rule_set_content_hash",
    "rounding",
    "default_money_basis",
    "rule_count",
    "reporting_currency",
    "money_policy_version_id",
    "money_policy_content_hash",
)


# ---------------------------------------------------------------------------
# Envelope, refusal, identity, result. Local -- no coupling to core.main.
# ---------------------------------------------------------------------------








def console_deep_link(project_id: str, *, object_id: str | None = None) -> dict[str, Any]:
    """An authenticated console reference to Governance > Controls & Quality.

    Semantic, never a raw URL: the console builds the href from the canonical
    navigation registry, and handing a host a hard-coded path would freeze a route
    this module does not own.
    """
    from core.project_overview import owner_reference  # noqa: PLC0415

    # NO TAB. `rule-sets` is a LENS of Controls & Quality, not a tab of the
    # `rule-set` object (whose tabs are overview / rules / effective-dates /
    # approvals-exceptions / versions). Naming it as a tab produced an address
    # the console refuses, and refused it in silence. The object opens on its
    # declared default instead. Found by
    # `tests/conformance/test_owner_refs_resolve_against_navigation`.
    reference = owner_reference(
        "governance",
        "controls-quality",
        object_type="rule-set",
        object_id=object_id,
    )
    return {
        "kind": "console",
        "requires_authenticated_session": True,
        "project_id": project_id,
        "owner_reference": reference,
    }








def _gap(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


# ---------------------------------------------------------------------------
# Activation. Read, never written: this surface is not an activation authority.
# ---------------------------------------------------------------------------


def _activation(conn, project_id: str) -> dict[str, Any]:
    """One row of ``app.project_tax_fee_activation_v``, plus the capability pin.

    The view answers "is Tax & Fees on?" as a conjunction of four facts and does
    not expose which of them failed. The two that belong to the ACTIVATION
    authority -- the capability state and whether its pin names the Project's
    current configuration version -- are read separately from
    ``app.project_capabilities`` so a refusal can name the right door. A Project
    with no projected row reads OFF, the same fail-closed default as before.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_ACTIVATION_COLUMNS)} "
            "FROM app.project_tax_fee_activation_v WHERE project_id = %s",
            (project_id,),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT state, active_version_id FROM app.project_capabilities "
            "WHERE project_id = %s AND capability_key = 'tax_fees'",
            (project_id,),
        )
        capability = cur.fetchone()

    record: dict[str, Any] = (
        dict(zip(_ACTIVATION_COLUMNS, row))
        if row is not None
        else {column: None for column in _ACTIVATION_COLUMNS}
    )
    record["project_id"] = project_id
    record["capability_state"] = str(record.get("capability_state") or "disabled")
    record["tax_fees_active"] = bool(record.get("tax_fees_active"))
    record["rule_count"] = int(record.get("rule_count") or 0)
    state = str(capability[0]) if capability else "disabled"
    pinned = str(capability[1]) if capability and capability[1] else None
    current = record.get("project_configuration_version_id")
    # The activation half, on its own: enabled AND pinned to the CURRENT Project
    # Configuration Version. A capability pinned to a superseded configuration is
    # not active, and that is a Project Change Set's problem, not this surface's.
    record["capability_pinned"] = bool(
        state != "disabled" and pinned is not None and current is not None and pinned == current
    )
    return record


def _money_policy(conn, project_id: str) -> tuple[str, str] | None:
    """``(money_policy_rule_set_id, money_policy_version_id)`` or ``None``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT money_policy_rule_set_id, money_policy_version_id "
            "FROM app.project_money_policy_v WHERE project_id = %s",
            (project_id,),
        )
        row = cur.fetchone()
    if row is None or not row[0] or not row[1]:
        return None
    return str(row[0]), str(row[1])


# ---------------------------------------------------------------------------
# The bounded ladder summary. ONE function, two surfaces (AC14).
# ---------------------------------------------------------------------------


def _rule_summary(rule: dict[str, Any]) -> dict[str, Any]:
    """The model-visible projection of one ordered rule.

    No ``rate``, no ``tiers``, no ``conditions``, no full ``source_evidence`` --
    see the module docstring. ``source_evidence.reference_version`` survives
    because a source with no version is a source whose meaning can change under
    the rule without the rule changing, and that is the one field a reader needs
    to decide whether to go and look.
    """
    evidence = rule.get("source_evidence") or {}
    label = str(rule.get("label") or "")
    if len(label) > _MAX_RULE_LABEL_CHARS:
        label = label[: _MAX_RULE_LABEL_CHARS - 3] + "..."
    return {
        "rule_key": rule.get("rule_key"),
        "label": label,
        "category": rule.get("category"),
        "form": rule.get("form"),
        "cascade_phase": rule.get("cascade_phase"),
        "sequence_order": rule.get("sequence_order"),
        "base_target": rule.get("base_target"),
        "money_basis": rule.get("money_basis"),
        "authority_kind": rule.get("authority_kind"),
        "geography_dependent": bool(rule.get("geography_dependent")),
        "rest_of_world_posture": rule.get("rest_of_world_posture"),
        "unknown_posture": rule.get("unknown_posture"),
        "source_reference_version": evidence.get("reference_version"),
    }


def _bounded(items: list[Any], limit: int) -> tuple[list[Any], int]:
    """Bound a list by COUNT and say what was dropped. A silent cap reads as coverage."""
    if len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


def _bounded_by_bytes(items: list[Any], *, budget: int, limit: int) -> tuple[list[Any], int]:
    """Bound a list by MEASURED SIZE first, count second, and state the remainder.

    A count tuned against a fixture is a guess about how wide a real row is. The
    shared guard REFUSES an over-budget result rather than truncating it, so a
    guess that is wrong for one Project turns a read into an error. Measuring here
    means the answer degrades by saying "N withheld" instead of failing.
    """
    from core.model_channel import serialized_bytes  # noqa: PLC0415

    kept: list[Any] = []
    spent = 2  # the enclosing brackets
    for item in items[:limit]:
        cost = serialized_bytes(item) + 1
        if kept and spent + cost > budget:
            break
        kept.append(item)
        spent += cost
    return kept, len(items) - len(kept)


def ladder_summary(conn, project_id: str) -> dict[str, Any]:
    """The bounded governed summary of one Project's Tax & Fee ladder.

    EXPORTED on purpose: ``project_capabilities_mcp._foundation_summary`` calls
    exactly this function for ``capability_key == "tax_fees"``, so the generic
    capability read and the specific Tax & Fee read cannot disagree. AC10 of Story
    48.1 keeps the projection on the GENERIC tool -- a second tool family would be
    a second place to ask one question -- and this is how both stay true at once.

    Raises nothing for an absent ladder: ``None`` is reported as a typed gap. It
    DOES raise through on an unreadable owner, because an outage read as "this
    Project composes no fees" reports gross equal to net.
    """
    from core.tax_fee_rule_set import try_resolve_tax_fee_ladder  # noqa: PLC0415

    activation = _activation(conn, project_id)
    ladder = try_resolve_tax_fee_ladder(conn, project_id=project_id)
    money = _money_policy(conn, project_id)

    gaps: list[dict[str, str]] = []
    if ladder is None:
        gaps.append(
            _gap(
                GAP_LADDER_UNPUBLISHED,
                "This Project has no published Tax & Fee Rule Ladder, so no component "
                "can be composed and no gross total can be stated.",
            )
        )
    if money is None:
        gaps.append(
            _gap(
                GAP_MONEY_POLICY_UNCONFIRMED,
                "No Money Policy is confirmed, so the ladder has no reporting currency "
                "to state a total in and Tax & Fees cannot become active.",
            )
        )
    if not activation["tax_fees_active"]:
        gaps.append(
            _gap(
                GAP_CAPABILITY_INACTIVE,
                "Tax & Fees is not active for this Project. The ladder below is readable "
                "as a document; it composes nothing until the capability is active.",
            )
        )

    ladder_block: dict[str, Any] | None = None
    if ladder is not None:
        unresolved = [rule["rule_key"] for rule in ladder.unresolved_geography_rules]
        visible, withheld = _bounded_by_bytes(
            [_rule_summary(rule) for rule in ladder.rules],
            budget=_LADDER_RULES_BYTE_BUDGET,
            limit=_MAX_LADDER_RULES,
        )
        # Bounded for the same reason as the rules, and with the same honesty: a
        # ladder where EVERY rule is undecided would otherwise spend the budget
        # repeating keys the caller already has in `rules`.
        unresolved_head, unresolved_withheld = _bounded(unresolved, _MAX_UNRESOLVED_KEYS)
        ladder_block = {
            "rule_set_id": ladder.rule_set_id,
            "version_id": ladder.version_id,
            "version_number": ladder.version_number,
            "content_hash": ladder.content_hash,
            "rounding": ladder.rounding,
            "default_money_basis": ladder.default_money_basis,
            # ALWAYS the true total, whatever the bound did.
            "rule_count": len(ladder.rules),
            "rules": visible,
            "rules_withheld": withheld,
            "unresolved_geography_rule_keys": unresolved_head,
            "unresolved_geography_rule_count": len(unresolved),
            "unresolved_geography_keys_withheld": unresolved_withheld,
            "pinned_hierarchy_version_ids": list(ladder.pinned_hierarchy_versions()),
        }
        if unresolved:
            gaps.append(
                _gap(
                    GAP_LADDER_GEOGRAPHY_UNRESOLVED,
                    f"{len(unresolved)} rule(s) leave Rest of World or Unknown undecided, "
                    "so this ladder is complete as a document and incomplete as an answer.",
                )
            )

    headline = _headline(activation, ladder_block, money)
    return {
        "capability": "tax_fees",
        "confirmed": ladder is not None,
        "headline": headline,
        "activation": {
            "tax_fees_active": activation["tax_fees_active"],
            "capability_state": activation["capability_state"],
            "capability_pinned_to_current_configuration": activation["capability_pinned"],
            "project_configuration_version_id": activation[
                "project_configuration_version_id"
            ],
            "reporting_currency": activation["reporting_currency"],
            "money_policy_version_id": activation["money_policy_version_id"],
        },
        "ladder": ladder_block,
        "gaps": gaps,
        "owner_reference": console_deep_link(
            project_id, object_id=(ladder.rule_set_id if ladder else None)
        )["owner_reference"],
    }


def _headline(
    activation: dict[str, Any], ladder: dict[str, Any] | None, money: tuple[str, str] | None
) -> str:
    """One sentence that never reads as a composed invoice total.

    It names the DEFINITION -- a version, a rule count, whether a currency exists,
    how many rules are undecided -- and no amount. `fee_tax_ladder_daily` has never
    executed, so there is no composed figure this surface could honestly report.
    """
    if ladder is None:
        return (
            "No Tax & Fee Rule Ladder version is published for this Project; nothing is "
            "composed and no rule is in force."
        )
    state = "active" if activation["tax_fees_active"] else "not active"
    currency = activation["reporting_currency"] or (
        "no confirmed reporting currency" if money is None else "an unnamed currency"
    )
    undecided = ladder["unresolved_geography_rule_count"]
    sentence = (
        f"Ladder version {ladder['version_number']} defines {ladder['rule_count']} ordered "
        f"rule(s) under {currency}; the capability is {state}."
    )
    if undecided:
        sentence += (
            f" {undecided} rule(s) leave Rest of World or Unknown undecided, so no complete "
            "headline total can be stated."
        )
    return sentence


# ---------------------------------------------------------------------------
# get_fee_tax_rules.
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# auto_populate_tax_rules -- proposals. Zero writes.
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# add_fee_tax_rule / update_fee_tax_rule -- prepare only.
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


