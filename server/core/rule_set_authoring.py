"""Composing a Rule Set version from the console, for every family, one door.

Story: `docs/product-architecture/governance.md`, "A Rule Set version is drafted,
then published" (2026-08-24).

WHAT THIS EXISTS TO CLOSE. The Rule Sets lens lists every family a Project
carries and, until today, exactly two of them could be written from the console:
`tax_fee` through the preset adoption of 2026-08-17, and `source_currency`
through the mapping screen's conflict dialog. The other six rendered a read-only
panel whose empty state said *"there is no governed setting to read"* and named
no gesture that would fill it -- the same shape as the ladder tab before the
adoption existed, which told an operator to prepare a Change Set on a screen that
did not exist. Repairing that one family at a time is the defect `CLAUDE.md`
names by its name; this module is the repair made once, for the class.

THREE PROPERTIES, AND THEY ARE THE WHOLE MODULE:

* **The family declares, the console renders.** Every question, answer and
  consequence comes from :attr:`~core.governance_rule_sets.RuleSetProfile.form`,
  declared beside the validator that refuses a wrong answer. Nothing here knows
  what a Money Policy is, and nothing in the front end does either.
* **Drafting and publishing are two acts.** :func:`draft_from_answers` stores an
  immutable draft and changes nothing in force; :func:`publish_draft` puts it in
  force through a Change Set -- created, prepared, confirmed -- so who composed a
  version and who adopted it are two records and may be two people.
* **What is carried forward is carried, never re-defaulted.** The ordered rules
  and the pinned references of the version in force travel into the new one.
  Changing a ladder's rounding must not be a way to empty its ladder, and a
  profile that requires a pinned dependency must not lose it passing through a
  generic transport -- the failure `controls_owner_commands` already names.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from core.governance_rule_sets import (
    RuleSetError,
    RuleSetFormField,
    RuleSetNotFound,
    RuleSetProfile,
    fetch_rule_set,
    fetch_version,
    registered_profiles,
    require_version,
)

logger = logging.getLogger(__name__)


class AuthoringRefused(Exception):
    """The version cannot be composed or published, and the sentence says why.

    Every message names the gesture that clears it. A refusal that states a cause
    and no repair sends a person hunting, which is the one thing an operator
    holding a half-composed policy cannot afford.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self)}


# ---------------------------------------------------------------------------
# Where a REQUIRED pin comes from when there is no version in force to carry one.
#
# This is a fact about provenance, not about what a family means, which is why it
# is here and not on the profile: the profile stays a pure validator, as its own
# module's contract says. Each entry answers one question -- "the first version of
# this family needs a pinned X; which X, and who made it?" -- and a kind with no
# entry is refused by name rather than dropped, because `draft_version` would
# otherwise refuse it with a sentence naming a column.
# ---------------------------------------------------------------------------


def _vocabulary_pin(conn, *, kind: str, vocabulary_key: str) -> dict[str, Any] | None:
    from core.master_data import fetch_vocabulary_version  # noqa: PLC0415

    version = fetch_vocabulary_version(conn, vocabulary_key=vocabulary_key)
    if version is None:
        return None
    # The version CURRENT at composition time, pinned exactly. "The latest" is
    # what the pin exists to refuse; resolving it once, here, is the opposite.
    return {
        "kind": kind,
        "object_id": str(version["vocabulary_key"]),
        "version_id": str(version["id"]),
    }


def _currency_vocabulary_pin(conn, *, project_id: str) -> dict[str, Any] | None:
    from core.currency_vocabulary import CURRENCY_VOCABULARY_KEY  # noqa: PLC0415

    return _vocabulary_pin(
        conn, kind="currency_vocabulary_version", vocabulary_key=CURRENCY_VOCABULARY_KEY
    )


def _timezone_vocabulary_pin(conn, *, project_id: str) -> dict[str, Any] | None:
    from core.timezone_vocabulary import TIMEZONE_VOCABULARY_KEY  # noqa: PLC0415

    return _vocabulary_pin(
        conn, kind="timezone_vocabulary_version", vocabulary_key=TIMEZONE_VOCABULARY_KEY
    )


def _money_policy_pin(conn, *, project_id: str) -> dict[str, Any] | None:
    from core.governance_rule_sets import active_version  # noqa: PLC0415
    from core.money_policy import FAMILY_MONEY, POLICY_NAME  # noqa: PLC0415

    found = active_version(conn, project_id=project_id, family=FAMILY_MONEY, name=POLICY_NAME)
    if found is None:
        return None
    _head, version = found
    return {
        "kind": "money_policy_version",
        "object_id": str(version["rule_set_id"]),
        "version_id": str(version["id"]),
    }


#: kind -> (resolver, the gesture that supplies it when the resolver finds none).
_REQUIRED_PINS: dict[str, tuple[Any, str]] = {
    "currency_vocabulary_version": (
        _currency_vocabulary_pin,
        "No ISO 4217 vocabulary snapshot has been imported into this deployment yet, so a "
        "reporting currency could not be pinned to the list it was chosen from. Import the "
        "currency vocabulary in Governance › Master Data first.",
    ),
    "timezone_vocabulary_version": (
        _timezone_vocabulary_pin,
        "No IANA timezone vocabulary snapshot has been imported into this deployment yet, so "
        "a reporting timezone could not be pinned to the list it was chosen from. Import the "
        "timezone vocabulary in Governance › Master Data first.",
    ),
    "money_policy_version": (
        _money_policy_pin,
        "This Project has no confirmed Money Policy, and a ladder whose money policy is 'the "
        "latest' has no defined arithmetic. Publish this Project's Money Policy version "
        "first, on its own Rule Set.",
    ),
}


# ---------------------------------------------------------------------------
# Resolving what is being authored.
# ---------------------------------------------------------------------------


def _profile_for(head: Mapping[str, Any], version: Mapping[str, Any] | None) -> RuleSetProfile:
    """The profile this rule set is authored under.

    Taken from the version in force when there is one -- a head does not carry a
    profile, and inferring one where the stored version names it would be a
    second answer free to disagree. Otherwise the single profile registered for
    the family; a family with several would be ambiguous and says so.
    """

    from core.governance_rule_sets import get_profile  # noqa: PLC0415

    if version is not None and version.get("profile"):
        return get_profile(str(version["profile"]))
    family = str(head["family"])
    candidates = [spec for spec in registered_profiles() if spec.family == family]
    if not candidates:
        raise AuthoringRefused(
            "no_profile",
            f"No Rule Set profile is registered for the {family!r} family in this build, so "
            "nothing here knows what a version of it may contain. This is a deployment gap, "
            "not something to answer on this screen.",
        )
    if len(candidates) > 1:
        raise AuthoringRefused(
            "ambiguous_profile",
            f"The {family!r} family registers {len(candidates)} profiles and this rule set "
            "has no published version naming which one it is authored under.",
        )
    return candidates[0]


def _require_head(conn, *, project_id: str, rule_set_id: str) -> dict[str, Any]:
    head = fetch_rule_set(conn, project_id=project_id, rule_set_id=rule_set_id)
    if head is None:
        raise AuthoringRefused("not_found", "That Rule Set is not in this Project.")
    return head


def _open_draft(conn, *, project_id: str, head: Mapping[str, Any]) -> dict[str, Any] | None:
    """The pending draft, when it is still a draft.

    `pending_version_id` is cleared on publication, so a stale pointer is not a
    thing this reads around; what it does guard is a pointer at a version whose
    status has moved on, which would put a `published` version under a "not in
    force yet" heading.
    """

    pending = head.get("pending_version_id")
    if not pending:
        return None
    version = fetch_version(conn, project_id=project_id, version_id=str(pending))
    if version is None or version.get("status") != "draft":
        return None
    return version


# ---------------------------------------------------------------------------
# Read before write.
# ---------------------------------------------------------------------------


def _field_value(field: RuleSetFormField, payload: Mapping[str, Any]) -> Any:
    """The answer currently recorded for one field, or ``None``.

    Read out of the NORMALIZED payload of a stored version, which is why the
    field key is a payload key: what is offered and what is stored are the same
    word or the form is showing something else than the policy.
    """

    return payload.get(field.key)


def plan_authoring(conn, *, project_id: str, rule_set_id: str) -> dict[str, Any]:
    """What could be composed here, read before anything is written.

    Returns the declared form with the answers in force, the open draft if one
    exists, and what a new version would carry forward untouched.
    """

    head = _require_head(conn, project_id=project_id, rule_set_id=rule_set_id)
    current = (
        fetch_version(conn, project_id=project_id, version_id=str(head["current_version_id"]))
        if head.get("current_version_id")
        else None
    )
    spec = _profile_for(head, current)
    draft = _open_draft(conn, project_id=project_id, head=head)

    # The draft's answers win over the published ones: the draft IS what would be
    # published next, and showing the version in force under an open draft would
    # invite someone to re-answer a question they already answered.
    source = (draft or current or {}).get("payload") or {}
    carried = current or {}
    rules = carried.get("ordered_rules") or []
    requires = carried.get("requires") or []

    return {
        "rule_set": {
            "id": str(head["id"]),
            "label": str(head.get("label") or head["name"]),
            "family": str(head["family"]),
            "profile": spec.key,
            "profile_label": spec.label,
            "lifecycle_status": str(head.get("lifecycle_status") or "draft"),
        },
        "in_force": (
            {
                "version_id": str(current["id"]),
                "version_number": current["version_number"],
                "content_hash": current.get("content_hash"),
                "rule_count": len(rules),
            }
            if current
            else None
        ),
        "draft": (
            {
                "version_id": str(draft["id"]),
                "version_number": draft["version_number"],
                "label": draft.get("label"),
                "content_hash": draft.get("content_hash"),
                "created_by": draft.get("created_by"),
            }
            if draft
            else None
        ),
        # A family with no form says where its versions ARE composed, by name.
        # Empty on both counts is the honest third answer and the screen prints
        # it as one, rather than as a form with nothing in it.
        "composed_by": spec.composed_by,
        "fields": [
            {**field.as_dict(), "value": _field_value(field, source)} for field in spec.form
        ],
        # What a new version keeps without being asked. Stated, because a person
        # about to change a rounding needs to know the ladder survives it.
        "carries_forward": {
            "ordered_rule_count": len(rules),
            "pinned_references": [dict(item) for item in requires],
        },
    }


# ---------------------------------------------------------------------------
# Drafting.
# ---------------------------------------------------------------------------


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _coerce(field: RuleSetFormField, value: Any) -> Any:
    """One answer, in the shape the validator expects, or a refusal by question.

    The refusal quotes the QUESTION rather than the key: `max_staleness_days must
    be a non-negative integer` is the validator's sentence and belongs to the
    validator; a person who typed "seven days" into a box needs to be told which
    box.
    """

    if field.kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
        raise AuthoringRefused(
            "answer_invalid", f"“{field.question}” is answered yes or no."
        )
    if field.kind == "integer":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise AuthoringRefused(
                "answer_invalid", f"“{field.question}” is answered with a whole number."
            ) from exc
    if field.kind == "decimal":
        try:
            # Through Decimal, not float: the answer is read exactly as typed and
            # only then handed to a validator that may narrow it.
            return float(Decimal(str(value).strip()))
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise AuthoringRefused(
                "answer_invalid", f"“{field.question}” is answered with a number."
            ) from exc
    if field.kind == "text_list":
        if isinstance(value, str):
            entries = [item.strip() for item in value.split(",")]
        elif isinstance(value, (list, tuple)):
            entries = [str(item).strip() for item in value]
        else:
            raise AuthoringRefused(
                "answer_invalid", f"“{field.question}” is answered with a list."
            )
        return [item for item in entries if item]
    if field.kind == "choice":
        allowed = {option.value for option in field.options}
        text = str(value).strip()
        if text not in allowed:
            raise AuthoringRefused(
                "answer_invalid",
                f"“{field.question}” — that is not one of the answers it offers.",
            )
        return text
    return str(value).strip()


def _payload_from_answers(
    spec: RuleSetProfile, answers: Mapping[str, Any], *, carried: Mapping[str, Any]
) -> dict[str, Any]:
    """The proposed payload: the declared answers over what the version in force held.

    MERGED, not replaced. A profile normalizes keys its form does not offer -- a
    DQ baseline, a ladder's assumptions, a policy's reconciliation references --
    and a form that replaced the payload wholesale would silently drop every one
    of them, which is a change nobody asked for recorded under an immutable hash.
    """

    declared = {field.key: field for field in spec.form}
    unknown = sorted(set(answers) - set(declared))
    if unknown:
        raise AuthoringRefused(
            "answer_unknown",
            "This version has no question about " + ", ".join(unknown) + ".",
        )

    payload = dict(carried)
    for key, field in declared.items():
        if key not in answers:
            continue
        value = answers[key]
        if _blank(value):
            if field.required:
                raise AuthoringRefused(
                    "answer_missing",
                    f"“{field.question}” has to be answered before this version can be "
                    "composed. Leaving it blank is not one of the answers.",
                )
            # An optional answer cleared is an answer: the key is removed so the
            # validator re-derives its own default rather than storing an empty
            # string that reads as a chosen value.
            payload.pop(key, None)
            continue
        payload[key] = _coerce(field, value)

    missing = [
        field.question
        for key, field in declared.items()
        if field.required and _blank(payload.get(key))
    ]
    if missing:
        raise AuthoringRefused(
            "answer_missing",
            "This version cannot be composed until these are answered: " + " ".join(missing),
        )
    return payload


def _pins_for(
    conn, *, project_id: str, spec: RuleSetProfile, carried: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The pinned references the new version carries.

    Carried from the version in force first -- a pin chosen once is not re-chosen
    behind someone's back. Only a REQUIRED kind that no version in force supplies
    is resolved here, and a kind nothing can supply is refused with the gesture
    that supplies it, not with the column name the generic publisher would name.
    """

    pins = {str(item.get("kind")): dict(item) for item in carried if item.get("kind")}
    for kind in spec.required_reference_kinds:
        if kind in pins:
            continue
        entry = _REQUIRED_PINS.get(kind)
        if entry is None:
            raise AuthoringRefused(
                "pin_unavailable",
                f"A version of this family must pin a {kind!r}, and this build has no way to "
                "resolve one from the console.",
            )
        resolver, gesture = entry
        resolved = resolver(conn, project_id=project_id)
        if resolved is None:
            raise AuthoringRefused("pin_unavailable", gesture)
        pins[kind] = resolved
    return sorted(pins.values(), key=lambda item: (item["kind"], item["object_id"]))


def draft_from_answers(
    conn,
    *,
    project_id: str,
    rule_set_id: str,
    answers: Mapping[str, Any],
    actor: str,
    label: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Compose a draft version. Nothing in force changes.

    Idempotent by content, because :func:`~core.governance_rule_sets.draft_version`
    is: composing the same answers twice returns the draft already stored rather
    than minting a rival version of identical content.
    """

    from core.governance_rule_sets import draft_version  # noqa: PLC0415

    head = _require_head(conn, project_id=project_id, rule_set_id=rule_set_id)
    current = (
        require_version(conn, project_id=project_id, version_id=str(head["current_version_id"]))
        if head.get("current_version_id")
        else None
    )
    spec = _profile_for(head, current)
    if not spec.form:
        raise AuthoringRefused(
            "not_composable_here",
            spec.composed_by
            or f"No version of the {head['family']!r} family can be composed in the console.",
        )

    carried_payload = (current or {}).get("payload") or {}
    payload = _payload_from_answers(spec, answers, carried=carried_payload)
    rules = (current or {}).get("ordered_rules") or []
    pins = _pins_for(
        conn, project_id=project_id, spec=spec, carried=(current or {}).get("requires") or []
    )

    try:
        return draft_version(
            conn,
            project_id=project_id,
            rule_set_id=rule_set_id,
            profile=spec.key,
            label=label or f"{spec.label} composed in the console",
            payload=payload,
            # Carried, never re-derived: see the module header.
            ordered_rules=[dict(rule) for rule in rules],
            requires=pins,
            description=description,
            actor=actor,
        )
    except RuleSetNotFound as exc:
        raise AuthoringRefused("not_found", str(exc)) from exc
    except RuleSetError as exc:
        # The profile's own sentence. It names what it refused and why, and a
        # paraphrase would lose the only actionable thing on the screen.
        raise AuthoringRefused("refused_by_profile", str(exc)) from exc


# ---------------------------------------------------------------------------
# Publishing. A second act, and a Change Set like every other governed change.
# ---------------------------------------------------------------------------


def publish_draft(
    conn,
    *,
    project_id: str,
    org_id: str,
    rule_set_id: str,
    version_id: str,
    actor: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Put an existing draft in force, through a created, prepared, confirmed Change Set.

    The confirmation publishes THIS draft rather than composing a second one, so
    what was read on the screen is what goes in force -- and the audit trail keeps
    the two acts and their two actors apart.
    """

    from core.controls_change_sets import (  # noqa: PLC0415
        confirm_change_set,
        create_change_set,
        find_by_idempotency_key,
        prepare_change_set,
    )
    from core.controls_owner_commands import OwnerCommandError, owner_command  # noqa: PLC0415

    head = _require_head(conn, project_id=project_id, rule_set_id=rule_set_id)
    version = fetch_version(conn, project_id=project_id, version_id=version_id)
    if version is None or str(version["rule_set_id"]) != str(rule_set_id):
        raise AuthoringRefused("not_found", "That version is not a version of this Rule Set.")
    if version["status"] == "published":
        # Not an error: the gesture's outcome is already true. Saying "already
        # published" and refusing would send someone looking for a second button.
        return {"version": version, "change_set": None, "replayed": True}
    if version["status"] not in {"draft", "candidate"}:
        raise AuthoringRefused(
            "not_a_draft",
            f"That version is {version['status']}, and a superseded or archived version is "
            "never put back in force. Compose a new one from the version in force.",
        )

    stem = idempotency_key or f"publish-{rule_set_id}-{version_id}"
    replay = find_by_idempotency_key(
        conn, project_id=project_id, idempotency_key=f"{stem}-create"
    )
    if replay is not None and str(replay.get("state")) == "confirmed":
        return {
            "version": require_version(conn, project_id=project_id, version_id=version_id),
            "change_set": replay,
            "replayed": True,
        }

    record = create_change_set(
        conn,
        project_id=project_id,
        object_type="rule-set",
        object_id=rule_set_id,
        base_version_id=head.get("current_version_id"),
        intent={
            "profile": str(version["profile"]),
            # The one key that makes this a PUBLICATION of a composed draft and
            # not a second composition. `_apply_rule_set` reads it.
            "version_id": str(version["id"]),
            "label": version.get("label"),
            "description": f"Published draft v{version['version_number']} from the console.",
        },
        actor=actor,
        idempotency_key=f"{stem}-create",
    )
    change_set_id = str(record["id"])
    if str(record.get("state")) == "confirmed":
        return {
            "version": require_version(conn, project_id=project_id, version_id=version_id),
            "change_set": record,
            "replayed": True,
        }

    _, token = prepare_change_set(
        conn, project_id=project_id, change_set_id=change_set_id, actor=actor
    )
    try:
        confirmed = confirm_change_set(
            conn,
            project_id=project_id,
            change_set_id=change_set_id,
            confirmation_token=token,
            actor=actor,
            apply=owner_command(actor=actor, org_id=org_id),
        )
    except OwnerCommandError as exc:
        raise AuthoringRefused("refused_by_owner", str(exc)) from exc
    return {
        "version": require_version(conn, project_id=project_id, version_id=version_id),
        "change_set": confirmed,
        "replayed": bool(confirmed.get("replayed")),
    }
