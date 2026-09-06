"""Adopting a governed preset into a Tax & Fee ladder, as a Change Set.

THE GAP THIS CLOSES, MEASURED. `governance.md:835` says a Rule Set *shows*
scope, order, effective dates, versions, approvals and exceptions -- and the
console did exactly that and nothing more. `grep -rn "ordered_rules"
ui/admin/src` returned five hits on 2026-08-17 and every one was a read. The
server could author a version the whole time: a change set of `object_type:
"rule-set"` reaches `controls_owner_commands._apply_rule_set`, which drafts and
publishes. Nothing had ever created one. So the ladder tab told an operator
"adopting one is a Change Set prepared by this ladder's owner", and no screen in
the product let that owner prepare it.

READ BEFORE WRITE, ONE ADDRESS. :func:`plan_adoption` returns what would be
published -- the resulting ordered ladder, the pinned Money Policy version, and
the decisions still owed. :func:`adopt_preset` performs it. A gesture whose
consequence cannot be read first is a gesture people click to find out what it
does, which is the same reason `event_stream_arming` is shaped this way.

IT IS STILL A CHANGE SET, and that is the whole point of putting it here.
"Never a click on this screen" (`TaxFeeLadderTabs.tsx:521`) was about the SHAPE
of the act, not its address: what is forbidden is adopting a preset by clicking
it. Create, prepare, confirm -- three recorded operations under one idempotency
stem, exactly as `arm_event_stream` does, so a retried gesture replays the same
three instead of minting a rival version.

NOTHING HERE COMPUTES A TOTAL. The rules are validated by the `tax_fee` profile
and published by `governance_rule_sets`; this module assembles an intent and
refuses the three things an operator cannot be asked to notice.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

__all__ = [
    "AdoptionRefused",
    "plan_adoption",
    "adopt_preset",
]

#: The two decisions a geography-dependent rule cannot be published without, with
#: the vocabulary the profile enforces. Kept as data so the console's options and
#: the server's refusals come from one list.
_POSTURE_QUESTIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "rest_of_world_posture",
        "question": "Every country this rule does not name — does it apply there?",
        "why": (
            "Rest of World is a known place outside the named set. Silence used to "
            "leave it to whatever the evaluator happened to do."
        ),
        "options": (
            {
                "value": "include",
                "label": "Yes, it applies everywhere else too",
            },
            {
                "value": "exclude",
                "label": "No, only in the countries it names",
            },
            {
                "value": "explicit_rule",
                "label": "Another rule in this ladder covers the remainder",
            },
            {
                "value": "unresolved",
                "label": "Not decided yet",
                "consequence": (
                    "The ladder states no complete headline total until this is "
                    "answered. That is deliberate: it blocks a total instead of "
                    "guessing one."
                ),
            },
        ),
    },
    {
        "key": "unknown_posture",
        "question": "A row whose country is absent, invalid or unresolved — does it apply there?",
        "why": (
            "Unknown is not Rest of World: Rest of World is a known place outside "
            "the named set, Unknown is no place at all."
        ),
        "options": (
            {"value": "include", "label": "Yes, apply it"},
            {"value": "exclude", "label": "No, leave those rows out"},
            {
                "value": "unresolved",
                "label": "Not decided yet",
                "consequence": (
                    "The ladder states no complete headline total until this is "
                    "answered."
                ),
            },
        ),
    },
)


class AdoptionRefused(Exception):
    """The adoption cannot proceed. Carries the code the screen renders."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self)}


def _rule_set_head(conn, *, project_id: str, rule_set_id: str) -> dict[str, Any]:
    from core.governance_rule_sets import fetch_rule_set  # noqa: PLC0415

    head = fetch_rule_set(conn, project_id=project_id, rule_set_id=rule_set_id)
    if head is None:
        raise AdoptionRefused(
            "rule_set_not_found",
            "This Rule Set is not in this Project.",
        )
    if str(head.get("family")) != "tax_fee":
        raise AdoptionRefused(
            "family_not_adoptable",
            f"Presets are published for Tax & Fee ladders. This Rule Set is a "
            f"`{head.get('family')}` one, and nothing proposes rules for it.",
        )
    return head


def _published_version(conn, *, project_id: str, head: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ladder as published today. ``None`` -- an empty ladder -- is normal here.

    The head carries no `profile` and no `payload`; both live on the version, and
    an unpublished ladder has neither. That is why the profile below falls back to
    the family's registered one rather than to a string typed somewhere.
    """

    current = head.get("current_version_id")
    if not current:
        return None
    from core.governance_rule_sets import fetch_version  # noqa: PLC0415

    return fetch_version(conn, project_id=project_id, version_id=str(current))


def _published_rules(version: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    rules = (version or {}).get("ordered_rules")
    return [dict(rule) for rule in rules] if isinstance(rules, list) else []


def _carried_payload(version: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ladder's own policy, CARRIED FORWARD rather than re-defaulted.

    `validate_ladder_payload` defaults `rounding` to `half_even` when the payload
    is empty. Posting `{}` on an adoption would therefore reset a ladder that had
    been published `half_up` -- a silent change to the rounding boundary of every
    total, made by a gesture that claimed only to add one rule.
    """

    payload = (version or {}).get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _find_proposal(
    conn, *, project_id: str, org_id: str, preset_version_id: str
) -> dict[str, Any]:
    """The offer, RECOMPUTED. Never trusted from the browser.

    A proposal qualifies on the Project's governed geography as it stands now. A
    browser holding a tab open across a Country registry change would otherwise
    adopt a candidate that no longer qualifies.
    """

    from core.capability_compilers import _tax_fee_preset_proposals  # noqa: PLC0415

    proposals = _tax_fee_preset_proposals(conn, project_id=project_id, org_id=org_id)
    for proposal in proposals:
        preset = proposal.get("preset") or {}
        if str(preset.get("preset_version_id")) == preset_version_id:
            return proposal
    raise AdoptionRefused(
        "proposal_not_qualified",
        "This preset is not a qualified proposal for this Project any more. It "
        "may have been superseded, or the geography this Project governs may "
        "have changed. Reopen the ladder to see what is offered now.",
    )


def _money_policy_reference(conn, *, project_id: str) -> dict[str, Any]:
    """The pinned Money Policy version, or the refusal that names the gesture."""

    from core.money_policy import try_resolve_money_policy  # noqa: PLC0415

    policy = try_resolve_money_policy(conn, project_id=project_id)
    if policy is None:
        raise AdoptionRefused(
            "money_policy_not_confirmed",
            "This Project has no confirmed Money Policy, and a fee ladder whose "
            "money policy is 'the latest' has no defined arithmetic. Confirm the "
            "Currency & FX policy in Controls & Quality, then adopt.",
        )
    return {
        "kind": "money_policy_version",
        "object_id": policy.rule_set_id,
        "version_id": policy.version_id,
        "reporting_currency": policy.reporting_currency,
    }


def _decisions_required(draft_rule: Mapping[str, Any], proposal: Mapping[str, Any]) -> list[dict]:
    """What the operator owes, in the order the screen asks it.

    The postures come first because they are answerable from the rule alone; a
    qualification may send someone to read a contract.
    """

    decisions: list[dict[str, Any]] = []
    if draft_rule.get("rest_of_world_posture") is not None:
        decisions.extend(
            {
                "kind": "posture",
                "key": question["key"],
                "question": question["question"],
                "why": question["why"],
                "options": [dict(option) for option in question["options"]],
            }
            for question in _POSTURE_QUESTIONS
        )
    for index, item in enumerate(proposal.get("unproven_qualifications") or []):
        decisions.append(
            {
                "kind": "qualification",
                "key": f"qualification:{index}",
                "question": str(item.get("question") or ""),
                "why": str(item.get("basis") or item.get("note") or ""),
                "options": [
                    {"value": "confirmed", "label": "Yes, this holds for this Project"},
                    {
                        "value": "not_confirmed",
                        "label": "No, or not yet",
                        "consequence": (
                            "The preset cannot be adopted while one of its "
                            "qualifications is unanswered — an 80 % proven "
                            "candidate still produces a wrong invoice if the "
                            "remaining 20 % is whether it is charged at all."
                        ),
                    },
                ],
            }
        )
    return decisions


def _resulting_rule(
    draft_rule: Mapping[str, Any],
    *,
    published: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any],
    verified_on: date,
) -> dict[str, Any]:
    """The draft rule with the operator's answers, placed at the end of its phase."""

    rule = dict(draft_rule)
    if rule.get("rest_of_world_posture") is not None:
        rule["rest_of_world_posture"] = answers.get("rest_of_world_posture")
        rule["unknown_posture"] = answers.get("unknown_posture")
    same_phase = [
        item
        for item in published
        if int(item.get("cascade_phase") or 0) == int(rule.get("cascade_phase") or 0)
    ]
    # Order is content, not a display choice: a clash of (phase, order) is refused
    # rather than tie-broken, so an adoption lands after everything already in its
    # phase instead of contending for a seat.
    rule["sequence_order"] = (
        max((int(item.get("sequence_order") or 0) for item in same_phase), default=-1) + 1
    )
    evidence = dict(rule.get("source_evidence") or {})
    # What this field has always meant: the day someone checked. An adoption IS
    # that check -- every unproven qualification was answered to get here.
    evidence["last_verified_on"] = verified_on.isoformat()
    rule["source_evidence"] = evidence
    return rule


def plan_adoption(
    conn,
    *,
    project_id: str,
    org_id: str,
    rule_set_id: str,
    preset_version_id: str,
) -> dict[str, Any]:
    """What adopting this preset would publish, and what it still needs.

    Raises :class:`AdoptionRefused` for the three states the screen must render
    as refusals rather than as an empty form.
    """

    head = _rule_set_head(conn, project_id=project_id, rule_set_id=rule_set_id)
    proposal = _find_proposal(
        conn, project_id=project_id, org_id=org_id, preset_version_id=preset_version_id
    )
    draft_rule = dict(proposal.get("draft_rule") or {})
    if not draft_rule.get("rule_key"):
        raise AdoptionRefused(
            "preset_has_no_rule",
            "This preset does not compile to a ladder rule, so there is nothing "
            "to adopt from it.",
        )
    version = _published_version(conn, project_id=project_id, head=head)
    published = _published_rules(version)
    if any(str(item.get("rule_key")) == str(draft_rule["rule_key"]) for item in published):
        raise AdoptionRefused(
            "rule_key_already_published",
            f"This ladder already publishes a rule under `{draft_rule['rule_key']}`. "
            "A second rule under the same key is a rewrite wearing an adoption's "
            "clothes; open the existing rule instead.",
        )
    money = _money_policy_reference(conn, project_id=project_id)

    from core.tax_fee_rule_set import PROFILE_TAX_FEE  # noqa: PLC0415

    return {
        "rule_set": {
            "id": str(head["id"]),
            "label": str(head.get("label") or head.get("name") or ""),
            # An unpublished ladder has no version, so no profile of its own. The
            # family's registered profile is the answer, not a guess.
            "profile": str((version or {}).get("profile") or PROFILE_TAX_FEE),
            "family": str(head.get("family") or ""),
            "current_version_id": head.get("current_version_id"),
            "published_rule_count": len(published),
            "payload": _carried_payload(version),
        },
        "proposal": proposal,
        "money_policy": money,
        "decisions_required": _decisions_required(draft_rule, proposal),
        # Shown, not summarized: the operator sees the ladder that would exist,
        # in order, before anything is written.
        "resulting_rules": [
            *[dict(rule) for rule in published],
            _resulting_rule(
                draft_rule,
                published=published,
                answers={
                    "rest_of_world_posture": "unresolved",
                    "unknown_posture": "unresolved",
                },
                verified_on=date.today(),
            ),
        ],
    }


def _validated_answers(
    decisions: Sequence[Mapping[str, Any]], answers: Mapping[str, Any]
) -> dict[str, Any]:
    """Every decision answered with a value its own question offers. No defaults.

    A missing posture is refused rather than defaulted to `unresolved`: the value
    is legal, but sliding into it is how a ladder acquires a posture nobody chose.
    """

    from core.tax_fee_rule_set import (  # noqa: PLC0415
        REST_OF_WORLD_POSTURES,
        UNKNOWN_POSTURES,
    )

    allowed = {
        "rest_of_world_posture": REST_OF_WORLD_POSTURES,
        "unknown_posture": UNKNOWN_POSTURES,
    }
    resolved: dict[str, Any] = {}
    for decision in decisions:
        key = str(decision["key"])
        value = answers.get(key)
        if value in (None, ""):
            raise AdoptionRefused(
                "decision_unanswered",
                f"{decision['question']} — this has to be answered before the "
                "preset can be adopted. 'Not decided yet' is one of the answers; "
                "leaving it blank is not.",
            )
        value = str(value)
        if decision["kind"] == "posture":
            if value not in allowed[key]:
                raise AdoptionRefused(
                    "decision_invalid",
                    f"`{value}` is not an answer this question offers.",
                )
        elif value != "confirmed":
            raise AdoptionRefused(
                "qualification_not_confirmed",
                f"{decision['question']} — the preset cannot be adopted while "
                "this is unanswered. A candidate that is 80 % proven still "
                "produces a wrong invoice if the remaining 20 % is whether the "
                "platform charges it at all.",
            )
        resolved[key] = value
    return resolved


def adopt_preset(
    conn,
    *,
    project_id: str,
    org_id: str,
    rule_set_id: str,
    preset_version_id: str,
    answers: Mapping[str, Any],
    actor: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create, prepare and confirm one rule-set Change Set. Three recorded steps.

    The plan is recomputed here rather than taken from the caller: a browser that
    held its tab open across a Country registry change would otherwise adopt a
    candidate that no longer qualifies, under a Money Policy version that is no
    longer current.
    """

    from core.controls_change_sets import (  # noqa: PLC0415
        confirm_change_set,
        create_change_set,
        find_by_idempotency_key,
        prepare_change_set,
    )
    from core.controls_owner_commands import owner_command  # noqa: PLC0415

    stem = idempotency_key or f"adopt-{rule_set_id}-{preset_version_id}"
    # THE REPLAY IS ANSWERED BEFORE THE PLAN, and a test found out why. Planning
    # refuses `rule_key_already_published` on the rule the FIRST run published, so
    # a retried gesture -- a double click, a lost response -- read as a rewrite
    # attempt instead of replaying. `create_change_set` is idempotent by key, but
    # it is reached too late to help: the intent cannot be composed at all.
    replay = find_by_idempotency_key(
        conn, project_id=project_id, idempotency_key=f"{stem}-create"
    )
    if replay is not None and str(replay.get("state")) == "confirmed":
        return {"change_set": replay, "replayed": True}

    plan = plan_adoption(
        conn,
        project_id=project_id,
        org_id=org_id,
        rule_set_id=rule_set_id,
        preset_version_id=preset_version_id,
    )
    resolved = _validated_answers(plan["decisions_required"], answers)
    draft_rule = dict(plan["proposal"]["draft_rule"])
    published = [dict(rule) for rule in plan["resulting_rules"][:-1]]
    adopted = _resulting_rule(
        draft_rule, published=published, answers=resolved, verified_on=date.today()
    )
    money = plan["money_policy"]

    answered = [
        f"{decision['question']} -> {resolved[str(decision['key'])]}"
        for decision in plan["decisions_required"]
    ]
    intent = {
        "profile": plan["rule_set"]["profile"],
        "label": f"Adopted {draft_rule.get('label') or draft_rule['rule_key']}",
        # Carried, never re-defaulted -- see `_carried_payload`.
        "payload": plan["rule_set"]["payload"],
        "ordered_rules": [*published, adopted],
        "requires": [
            {
                "kind": money["kind"],
                "object_id": money["object_id"],
                "version_id": money["version_id"],
            }
        ],
        "description": "\n".join(
            [
                f"Adopted preset {preset_version_id} into this ladder.",
                *answered,
            ]
        )[:4000],
        # Kept whole so the audit trail carries WHO answered WHAT, not only the
        # total that came out of it.
        "adoption_answers": resolved,
        "adopted_preset_version_id": preset_version_id,
    }

    record = create_change_set(
        conn,
        project_id=project_id,
        object_type="rule-set",
        object_id=rule_set_id,
        base_version_id=plan["rule_set"]["current_version_id"],
        intent=intent,
        actor=actor,
        idempotency_key=f"{stem}-create",
    )
    change_set_id = str(record["id"])
    if str(record.get("state")) == "confirmed":
        # Idempotent replay: the same gesture returns the version it published
        # rather than minting a rival one.
        return {"change_set": record, "replayed": True}

    _, token = prepare_change_set(
        conn, project_id=project_id, change_set_id=change_set_id, actor=actor
    )
    confirmed = confirm_change_set(
        conn,
        project_id=project_id,
        change_set_id=change_set_id,
        confirmation_token=token,
        actor=actor,
        apply=owner_command(actor=actor, org_id=org_id),
    )
    return {"change_set": confirmed, "replayed": bool(confirmed.get("replayed"))}
