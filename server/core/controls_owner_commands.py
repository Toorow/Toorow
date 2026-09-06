"""The owner commands a confirmed Controls & Quality change set actually runs.

Story 49.4 AC7: *"State, pointer, decision, audit, outbox and idempotency commit
atomically"* and *"Mapping approval calls the existing Data owner service exactly
once and preserves Data current/last-known-good state on failure."*

`controls_change_sets.confirm_change_set` takes an ``apply`` callable and was
shipped without one wired to the route, so confirming a change set moved its
state to ``confirmed``, left ``result_version_id`` NULL, and published nothing.
The machinery around it -- single-use token, expiry, drift recheck -- was real
and guarded an operation that never happened. That is worse than an unbuilt
command: it looks delivered.

This module is that missing half, and it lives apart from
`controls_change_sets` on purpose. The change-set service must not know what a
Rule Set or a Datastream mapping IS; it arbitrates confirmation. Here is where
Governance hands off to an owner, and the handoff is the whole subject:

* **Governance owns the decision, never the owner's state.** A Rule Set version
  is published through `governance_rule_sets`, a monitor version through
  `dq_governance`, a mapping through Data's own `confirm_and_publish`. Nothing
  below writes an owner's table directly.
* **A failed handoff is recorded as failed and advances nothing.** That is
  `control_cases.decide`'s rule and it is not softened here.
* **Exactly once.** The Data publication is routed through the owner's own
  durable operation, which is idempotent on its confirmation; a replayed confirm
  cannot publish twice.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Mapping

logger = logging.getLogger(__name__)


class OwnerCommandError(Exception):
    """The intent cannot be applied. The caller sees 422, not a 500."""

    def as_dict(self) -> dict[str, Any]:
        return {"code": "owner_command_refused", "message": str(self)}


def _require(intent: Mapping[str, Any], key: str) -> Any:
    value = intent.get(key)
    if value in (None, "", [], {}):
        raise OwnerCommandError(f"the intent must carry {key!r} to be applied")
    return value


def _as_date(value: Any, *, default: date | None = None) -> date:
    if value in (None, ""):
        if default is not None:
            return default
        raise OwnerCommandError("a date is required")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise OwnerCommandError(f"invalid date {value!r}: {exc}") from exc


def owner_command(*, actor: str, org_id: str):
    """Return the ``apply`` callable `confirm_change_set` expects.

    It receives ``(conn, record)`` and returns the id of whatever the confirmation
    produced -- a version id, or a decision id -- which the change set stores as
    ``result_version_id``. Returning None would leave a confirmed change set that
    points at nothing, which is the defect this module exists to close.
    """

    def apply(conn, record: Mapping[str, Any]) -> str:
        object_type = str(record["object_type"])
        intent = record["intent"] if isinstance(record["intent"], dict) else {}
        project_id = str(record["project_id"])
        handler = _HANDLERS.get(object_type)
        if handler is None:
            raise OwnerCommandError(f"no owner command is defined for {object_type}")
        return handler(
            conn,
            project_id=project_id,
            object_id=record["object_id"],
            base_version_id=record.get("base_version_id"),
            intent=intent,
            actor=actor,
            org_id=org_id,
        )

    return apply


# ---------------------------------------------------------------------------
# Rule Set. The one that unblocks Story 48.4: without it no Project can publish
# a tax-and-fee ladder, and the Governance surface offers a Rules tab over an
# object nothing can ever advance.
# ---------------------------------------------------------------------------


def _apply_rule_set(
    conn,
    *,
    project_id: str,
    object_id: Any,
    base_version_id: Any,
    intent: Mapping[str, Any],
    actor: str,
    org_id: str,
) -> str:
    from core.governance_rule_sets import (  # noqa: PLC0415
        RuleSetError,
        draft_version,
        publish_version,
    )

    if not object_id:
        raise OwnerCommandError("a rule-set change set must name the rule set it changes")

    # TWO INTENTS, ONE COMMAND. An intent that NAMES a version publishes THAT
    # draft; the console composes it as its own recorded act and confirms it as a
    # second one (`governance.md`, "A Rule Set version is drafted, then
    # published"). Composing a second version here would publish something other
    # than what was read on the screen before confirming -- and would mint a
    # rival version of identical content on every replay.
    existing = str(intent.get("version_id") or "").strip()
    if existing:
        try:
            published = publish_version(
                conn,
                project_id=project_id,
                rule_set_id=str(object_id),
                version_id=existing,
                actor=actor,
            )
        except RuleSetError as exc:
            raise OwnerCommandError(str(exc)) from exc
        return str(published["id"])

    try:
        draft = draft_version(
            conn,
            project_id=project_id,
            rule_set_id=str(object_id),
            profile=str(_require(intent, "profile")),
            label=str(intent.get("label") or "Published from a change set"),
            payload=intent.get("payload") or {},
            ordered_rules=intent.get("ordered_rules") or [],
            # The pinned dependencies a profile may REQUIRE. The Tax & Fee ladder
            # requires a `money_policy_version`, and rightly refuses without one:
            # a fee ladder whose money policy is "the latest" has no defined
            # arithmetic. Dropping this on the way through would have made the
            # generic transport quietly weaker than the owner it calls.
            requires=intent.get("requires") or [],
            effective_from=intent.get("effective_from"),
            effective_to=intent.get("effective_to"),
            description=intent.get("description"),
            actor=actor,
        )
        # Drafting and publishing are one confirmation here on purpose: the
        # change set IS the review, and it already carried the server-derived
        # diff, the impact and the dependency fingerprint that a separate
        # publish step would have had to recompute against a world that moved.
        published = publish_version(
            conn,
            project_id=project_id,
            rule_set_id=str(object_id),
            version_id=str(draft["id"]),
            actor=actor,
        )
    except RuleSetError as exc:
        # A typed validation refusal from the profile: the caller's intent is
        # wrong, not the service. Surface the profile's own sentence.
        raise OwnerCommandError(str(exc)) from exc
    return str(published["id"])


# ---------------------------------------------------------------------------
# DQ Monitor.
# ---------------------------------------------------------------------------


def _apply_dq_monitor(
    conn,
    *,
    project_id: str,
    object_id: Any,
    base_version_id: Any,
    intent: Mapping[str, Any],
    actor: str,
    org_id: str,
) -> str:
    from core.dq_governance import DqGovernanceError, publish_version  # noqa: PLC0415

    if not object_id:
        raise OwnerCommandError("a dq-monitor change set must name the monitor it changes")

    try:
        version = publish_version(
            conn,
            project_id=project_id,
            monitor_id=str(object_id),
            check_profile=str(_require(intent, "check_profile")),
            severity=str(intent.get("severity") or "degrading"),
            baseline=intent.get("baseline"),
            parameters=intent.get("parameters"),
            schedule=intent.get("schedule"),
            window_days=int(intent.get("window_days") or 1),
            applicability=intent.get("applicability"),
            target_selectors=intent.get("target_selectors"),
            rule_set_id=intent.get("rule_set_id"),
            rule_set_version_id=intent.get("rule_set_version_id"),
            actor=actor,
        )
    except DqGovernanceError as exc:
        raise OwnerCommandError(str(exc)) from exc
    return str(version["id"])


# ---------------------------------------------------------------------------
# Control Case. The only one with a handoff OUT of Governance.
# ---------------------------------------------------------------------------


def _apply_control_case(
    conn,
    *,
    project_id: str,
    object_id: Any,
    base_version_id: Any,
    intent: Mapping[str, Any],
    actor: str,
    org_id: str,
) -> str:
    from core.control_cases import OWNER_OUTCOMES, ControlCaseError, decide  # noqa: PLC0415

    if not object_id:
        raise OwnerCommandError("a control-case change set must name the case it decides")

    decision_kind = str(_require(intent, "decision_kind"))
    effective_from = _as_date(intent.get("effective_from"), default=date.today())

    # The owner's vocabulary, from the owner. A decision that calls no owner
    # command reports the third word -- "nobody was asked" -- and never invents a
    # fourth one, which is exactly how `"applied"` reached `decide` and turned a
    # publication that HAD happened into a 422.
    _, _, not_asked = OWNER_OUTCOMES
    owner_outcome = not_asked
    owner_result: dict[str, Any] | None = None
    if decision_kind == "approve_mapping":
        owner_outcome, owner_result = _hand_off_to_data(
            conn, intent=intent, actor=actor, org_id=org_id
        )

    try:
        decision_id = decide(
            conn,
            project_id=project_id,
            case_id=str(object_id),
            decision_kind=decision_kind,
            actor=actor,
            reason=str(_require(intent, "reason")),
            effective_from=effective_from,
            owner_outcome=owner_outcome,
            owner_result=owner_result,
            candidate_id=intent.get("candidate_id"),
            expires_at=_as_date(intent.get("expires_at")) if intent.get("expires_at") else None,
        )
    except ControlCaseError as exc:
        raise OwnerCommandError(str(exc)) from exc
    return str(decision_id)


def _hand_off_to_data(
    conn, *, intent: Mapping[str, Any], actor: str, org_id: str
) -> tuple[str, dict[str, Any] | None]:
    """Call Data's publication service EXACTLY ONCE and report what happened.

    Governance does not advance a Datastream pointer, and it does not mint Data's
    confirmation either: the caller presents the confirmation Data itself issued.
    That keeps the two authorities separate -- Governance decides that a mapping
    SHOULD be approved, Data decides whether this exact publication may proceed,
    and either can refuse without the other pretending it did not.

    A refusal comes back as ``failed`` rather than as an exception, because a
    failed handoff is a real recorded result: `decide` writes it and does NOT
    advance the case. Raising here instead would roll the whole confirmation back
    and leave no trace that the owner was ever asked.

    TWO DEFECTS CLOSED HERE ON 2026-08-31, and both were shapes the console could
    not have rendered:

    * **The refusal's SENTENCE was dropped.** This function stored
      ``{"refusal": <code>}`` and nothing else, while `CaseDecisionDialog`
      reads a ``message`` key -- so every refused publication rendered *"The Data
      owner refused the publication and gave no reason"*, and `governance.md`'s
      own rule (*"the owner's refusal is quoted, never paraphrased"*) was
      unmeetable because the words never reached the database. The code AND the
      sentence are stored now: a code routes, a sentence is what a person acts
      on.
    * **A success reported a word the vocabulary does not hold.** It returned
      ``"applied"`` where `control_cases.OWNER_OUTCOMES` is
      ``("succeeded", "failed", "not_applicable")``, so `decide` raised
      `ControlCaseError` -> `OwnerCommandError` -> 422 **after Data had already
      published**: the console said *"Nothing was recorded"* about a publication
      that happened. The vocabulary is imported from its owner below and never
      spelled again here -- a literal is how the two drifted apart in the first
      place.
    """
    from core.control_cases import OWNER_OUTCOMES  # noqa: PLC0415
    from core.governed_publication import (  # noqa: PLC0415
        PublicationConfirmationRefused,
        confirm_and_publish,
    )

    succeeded, failed, not_asked = OWNER_OUTCOMES

    confirmation_id = intent.get("data_confirmation_id")
    confirmation_secret = intent.get("data_confirmation_secret")
    if not confirmation_id or not confirmation_secret:
        # Not an error: a mapping approval may be recorded before Data has
        # prepared its own confirmation. `not_applicable` says "the owner was not
        # asked", which is different from "the owner said no".
        return not_asked, None

    try:
        result = confirm_and_publish(
            conn,
            confirmation_id=str(confirmation_id),
            confirmation_secret=str(confirmation_secret),
            actor=actor,
            org_id=org_id,
        )
    except PublicationConfirmationRefused as exc:
        logger.info(
            "controls_owner_commands: data refused the publication confirmation=%s: %s",
            confirmation_id,
            type(exc).__name__,
        )
        # THE CODE ROUTES, THE SENTENCE IS READ. Data's refusals are written to be
        # read by a person ("the mapping version is no longer the published one"),
        # and the console quotes this key whole. The code stays beside it because
        # a screen that has to parse prose to tell two refusals apart is the next
        # defect.
        return failed, {"refusal": getattr(exc, "code", "refused"), "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- an owner outage is a failed handoff
        logger.warning(
            "controls_owner_commands: data publication failed confirmation=%s: %s",
            confirmation_id,
            type(exc).__name__,
        )
        # An OUTAGE, and it says so as an outage rather than as a refusal: nobody
        # decided anything, and the repair is to try again once Data answers. The
        # exception's own text is not shown -- it is a stack-trace sentence, not
        # something a person can act on.
        return failed, {
            "refusal": type(exc).__name__,
            "message": (
                "The Data owner could not be reached, so the mapping was not "
                "published. The decision is recorded. Try the publication again "
                "once Data answers."
            ),
        }

    # The secret is never echoed back, and neither is anything derived from it.
    return succeeded, {
        "operation_id": result.operation_id,
        "outcome": result.outcome,
        "replayed": result.replayed,
        # The pointer Data moved, and the one it can roll back to. Both are
        # already public on the Data surface; neither derives from the secret.
        "current_mapping_version_id": result.current_mapping_version_id,
        "prior_mapping_version_id": result.prior_mapping_version_id,
    }


_HANDLERS = {
    "rule-set": _apply_rule_set,
    "dq-monitor": _apply_dq_monitor,
    "control-case": _apply_control_case,
}
