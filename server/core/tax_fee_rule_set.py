"""The Tax & Fee Rule Ladder: one more family on the generic Rule Set lifecycle.

Story 48.4 gives Governance "one stable Project ``TaxFeeRuleSet``" whose published
versions are immutable and carry complete evidence. That object is NOT a new
lifecycle: :mod:`core.governance_rule_sets` already owns heads, immutable
versions, three version pointers, profiles and interval-bound exceptions, and
Money, FX ingestion and Timezone are mounted on it as families. Tax & Fees is the
fourth, and the ordered ladder lives in the version's ``ordered_rules``.

What this module adds is the evidence Epic 41 never carried. ``app.fee_tax_rules``
validated the *arithmetic* of a rule thoroughly -- routed category/form/base
pairs, exact tiers, three-valued conditions -- and that work is reused verbatim
through :func:`core.fee_tax_rules.validate_rule`. What it could not express is
everything the audit asks for:

* **who says so.** A rate had an ``origin`` naming the channel that created it
  (``auto_country``, ``operator``, ``media_plan``, ``llm``) and nothing naming the
  authority behind it. "France, 3%" from a statute, from a platform's invoice
  practice, from an agency contract and from a client's own markup are four
  different claims that reconcile differently, and the column could not tell them
  apart. :data:`AUTHORITY_KINDS` does.
* **against which geography version.** A ``conditions.country`` entry was a bare
  string. Country membership is versioned by Story 48.2, so a rule matching "FR"
  changed meaning silently whenever the hierarchy was republished. A jurisdiction
  now pins the exact hierarchy version it was chosen under.
* **Rest of World and Unknown.** Neither existed. A rule that named three
  countries said nothing at all about the fourth, and a row whose country was
  absent or invalid was indistinguishable from a row in a country the ladder had
  deliberately excluded. Both postures are now DECLARED per rule, and a
  geography-dependent rule that declares neither is refused -- ``unresolved`` is
  available and is a different, visible statement from silence.
* **which money the base is.** A percentage applies to *something*; whether that
  something is the native source amount or the Project reporting amount changes
  the answer whenever they differ, and nothing recorded the choice.

Every one of those is a line in the capability's own ``Incomplete if`` list, which
is why they are refusals here rather than optional fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from core.country_registry import COUNTRY_OBJECT_KIND
from core.governance_rule_sets import (
    GovernedDependency,
    RuleSetError,
    RuleSetFormField,
    RuleSetFormOption,
    RuleSetProfile,
    active_version,
    register_profile,
)

FAMILY_TAX_FEE = "tax_fee"
PROFILE_TAX_FEE = "tax_fee_ladder_v1"

#: One ladder head per Project. A second named ladder would be a second ordering
#: authority over the same components, which is the defect this story removes
#: rather than parameterizes -- exactly as ``money_policy`` fixes its name.
LADDER_NAME = "project_ladder"

#: Who states the rule. AC3: "Statutory references, provider invoice practices,
#: agency contracts and client-defined markups remain distinguishable origins."
#: They must not collapse, because a statute is evidence about the world and a
#: markup is evidence about a contract -- and only the second may be renegotiated.
AUTHORITY_KINDS: tuple[str, ...] = (
    "statutory_reference",
    "provider_invoice_practice",
    "agency_contract",
    "client_defined_markup",
    "operator_declared",
)

#: AC6: "A rule declares whether its contractual base is native-source or Project
#: reporting money." An agency percentage written into a contract denominated in
#: the client's currency is not the same rule as one applied to the platform's
#: native spend, and the two diverge by exactly the FX movement.
MONEY_BASES: tuple[str, ...] = ("native_source", "project_reporting")

#: AC5. ``explicit_rule`` means "a different rule in this ladder covers the
#: remainder"; ``unresolved`` means "nobody has decided yet" and blocks a complete
#: headline total. They are deliberately not the same value.
REST_OF_WORLD_POSTURES: tuple[str, ...] = ("include", "exclude", "explicit_rule", "unresolved")

#: Unknown is absent, invalid or unresolved geography. It is NOT Rest of World:
#: Rest of World is a known place outside the named set; Unknown is no place at all.
UNKNOWN_POSTURES: tuple[str, ...] = ("include", "exclude", "unresolved")

JURISDICTION_KINDS: tuple[str, ...] = ("none", "country", "market", "region")

#: The condition keys that make a rule geography-dependent. Kept as data beside
#: the postures so adding a geographic predicate cannot forget to demand one.
_GEOGRAPHIC_CONDITION_KEYS = frozenset({"country", "market"})

ROUNDING_POLICIES: tuple[str, ...] = ("half_even", "half_up")

_RULE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

#: Exactly the keys one ordered rule may carry. A superset is refused rather than
#: dropped: a caller that sends ``jurisdiction_id`` instead of ``jurisdiction``
#: must be told, not silently published without a jurisdiction.
_LADDER_RULE_KEYS = frozenset(
    {
        "rule_key",
        "label",
        "scope_kind",
        "scope_ref",
        "category",
        "form",
        "rate",
        "amount_micros",
        "cpm_micros",
        "tiers",
        "currency",
        "base_target",
        "money_basis",
        "cascade_phase",
        "sequence_order",
        "conditions",
        "source_type_scope",
        "jurisdiction",
        # Derived, not declared. Accepted on input only so re-validating a
        # normalized rule round-trips (idempotence is what makes the content hash
        # comparable); the value is always RECOMPUTED and never trusted.
        "geography_dependent",
        "rest_of_world_posture",
        "unknown_posture",
        "effective_from",
        "effective_to",
        "authority_kind",
        "source_evidence",
        "origin",
    }
)

#: The keys :func:`core.fee_tax_rules.validate_rule` accepts. Everything else in a
#: ladder rule is governance evidence that module never modelled.
_ARITHMETIC_KEYS = frozenset(
    {
        "scope_kind",
        "scope_ref",
        "category",
        "form",
        "rate",
        "amount_micros",
        "cpm_micros",
        "tiers",
        "currency",
        "base_target",
        "cascade_phase",
        "sequence_order",
        "conditions",
        "source_type_scope",
        "effective_from",
        "effective_to",
        "origin",
        "label",
    }
)


class TaxFeeGap(RuntimeError):
    """A required governed Tax & Fee evidence is absent, unreadable or unconfirmed.

    Not a ``ValueError``, for the reason :class:`core.money_policy.PolicyGap`
    states: a caller catching bad input must not also swallow "this Project has no
    published ladder" and continue with an implicit empty one, which computes a
    gross invoice equal to net media and looks like a working answer.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Ladder-level policy. Small on purpose: everything a RULE can carry belongs to
# the rule, so two rules cannot be governed by a setting neither of them names.
# ---------------------------------------------------------------------------


def validate_ladder_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the ladder's own policy.

    ``rounding`` is here and nowhere else. AC6 requires "one declared rounding
    policy" for the whole composition: a per-rule rounding mode is how a ladder
    ends up with several rounding boundaries whose order changes the total.
    """

    if not isinstance(payload, Mapping):
        raise RuleSetError("a Tax & Fee ladder payload must be an object")
    unknown = sorted(
        set(payload)
        - {"rounding", "default_money_basis", "assumptions", "reconciliation_refs", "notes"}
    )
    if unknown:
        raise RuleSetError("ladder payload contains unsupported keys: " + ", ".join(unknown))

    rounding = str(payload.get("rounding") or "half_even")
    if rounding not in ROUNDING_POLICIES:
        raise RuleSetError(f"rounding must be one of {list(ROUNDING_POLICIES)}")

    default_basis = str(payload.get("default_money_basis") or "native_source")
    if default_basis not in MONEY_BASES:
        raise RuleSetError(f"default_money_basis must be one of {list(MONEY_BASES)}")

    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise RuleSetError("notes must be a string")

    return {
        "rounding": rounding,
        "default_money_basis": default_basis,
        "assumptions": [dict(item) for item in (payload.get("assumptions") or [])],
        "reconciliation_refs": sorted(
            str(item) for item in (payload.get("reconciliation_refs") or [])
        ),
        "notes": notes or None,
    }


# ---------------------------------------------------------------------------
# One ordered rule.
# ---------------------------------------------------------------------------


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleSetError(f"{label} is required")
    return value.strip()


def _as_date(value: Any, label: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise RuleSetError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


def _validate_jurisdiction(value: Any, *, rule_key: str) -> dict[str, Any]:
    """A jurisdiction names an owner object AND the version it was chosen under.

    AC5: "Country/Market/Region predicates pin the exact 48.2 hierarchy version.
    Changing membership creates reviewable semantic/rule impact without rewriting
    facts." An unpinned ``"FR"`` cannot do that: republishing the hierarchy with
    Monaco added to the France market changes which rows the rule matched, with no
    version anywhere to diff.
    """

    if value in (None, {}):
        return {"kind": "none", "id": None, "hierarchy_version_id": None, "label": None}
    if not isinstance(value, Mapping):
        raise RuleSetError(f"rule {rule_key}: jurisdiction must be an object")
    kind = str(value.get("kind") or "none")
    if kind not in JURISDICTION_KINDS:
        raise RuleSetError(
            f"rule {rule_key}: jurisdiction.kind must be one of {list(JURISDICTION_KINDS)}"
        )
    if kind == "none":
        return {"kind": "none", "id": None, "hierarchy_version_id": None, "label": None}
    return {
        "kind": kind,
        "id": _text(value.get("id"), f"rule {rule_key}: jurisdiction.id"),
        "hierarchy_version_id": _text(
            value.get("hierarchy_version_id"),
            f"rule {rule_key}: jurisdiction.hierarchy_version_id "
            "(a jurisdiction that does not pin its hierarchy version changes "
            "meaning whenever membership is republished)",
        ),
        # Carried for display only. It is NOT the identity: AC2 forbids referencing
        # "copied labels", so nothing may match on this.
        "label": str(value.get("label") or "").strip() or None,
    }


def _validate_source_evidence(
    value: Any, *, rule_key: str, authority_kind: str
) -> dict[str, Any]:
    """Who states this rate, where it is written down, and as of when.

    Required for every authority kind, including ``operator_declared``. The
    capability's ``Incomplete if`` list contains "a rule lacks source, jurisdiction,
    effective date or version evidence" with no exemption, and an operator-declared
    rate with no stated basis is precisely the row that becomes unexplainable six
    months later.
    """

    if not isinstance(value, Mapping):
        raise RuleSetError(
            f"rule {rule_key}: source_evidence is required -- a rule with no stated "
            "source cannot be reconciled against anything later"
        )
    unknown = sorted(
        set(value)
        - {
            "issuer",
            "reference",
            "reference_version",
            "authoritative_url",
            "document_ref",
            "preset_version_id",
            "published_on",
            "last_verified_on",
            "assumptions",
        }
    )
    if unknown:
        raise RuleSetError(
            f"rule {rule_key}: source_evidence contains unsupported keys: " + ", ".join(unknown)
        )

    url = str(value.get("authoritative_url") or "").strip() or None
    document = str(value.get("document_ref") or "").strip() or None
    preset_version_id = str(value.get("preset_version_id") or "").strip() or None
    if authority_kind == "statutory_reference" and not (url or document or preset_version_id):
        raise RuleSetError(
            f"rule {rule_key}: a statutory_reference must cite an authoritative_url, a "
            "document_ref or the governed preset version it was compiled from"
        )

    return {
        "issuer": _text(value.get("issuer"), f"rule {rule_key}: source_evidence.issuer"),
        "reference": _text(value.get("reference"), f"rule {rule_key}: source_evidence.reference"),
        # AC2 asks for a "source-reference version". A source with no version is a
        # source whose meaning can change under the rule without the rule changing.
        "reference_version": _text(
            value.get("reference_version"),
            f"rule {rule_key}: source_evidence.reference_version",
        ),
        "authoritative_url": url,
        "document_ref": document,
        "preset_version_id": preset_version_id,
        "published_on": (_as_date(value.get("published_on"), "published_on") or None),
        "last_verified_on": (_as_date(value.get("last_verified_on"), "last_verified_on") or None),
        "assumptions": [str(item) for item in (value.get("assumptions") or [])],
    }


def _is_geographic(conditions: Mapping[str, Any], jurisdiction: Mapping[str, Any]) -> bool:
    if jurisdiction.get("kind") != "none":
        return True
    return any(key in _GEOGRAPHIC_CONDITION_KEYS for key in conditions)


def validate_ladder_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize ONE ordered rule: Epic 41's arithmetic plus 48.4's evidence.

    Pure. No DB, no clock. Idempotent, so a re-published identical ladder hashes
    identically and "has the policy changed?" stays answerable.
    """

    from core.fee_tax_rules import (  # noqa: PLC0415 -- reuse, do not re-implement
        FeeTaxRuleValidationError,
        validate_rule,
    )

    if not isinstance(rule, Mapping):
        raise RuleSetError("an ordered rule must be an object")
    unknown = sorted(set(rule) - _LADDER_RULE_KEYS)
    if unknown:
        raise RuleSetError("rule contains unsupported keys: " + ", ".join(unknown))

    rule_key = _text(rule.get("rule_key"), "rule_key")
    if not _RULE_KEY_RE.fullmatch(rule_key):
        raise RuleSetError(
            f"rule_key {rule_key!r} must be lowercase snake_case, 2 to 63 characters"
        )

    arithmetic_input = {key: rule[key] for key in _ARITHMETIC_KEYS if key in rule}
    # `status` is deliberately not passed: a rule inside a PUBLISHED version is
    # active by virtue of the version, and a per-rule status would be a second
    # activation authority -- the exact defect AC1 removes.
    arithmetic_input.setdefault("origin", "operator")
    try:
        normalized = validate_rule(dict(arithmetic_input))
    except FeeTaxRuleValidationError as exc:
        raise RuleSetError(f"rule {rule_key}: {exc}") from exc

    money_basis = str(rule.get("money_basis") or "native_source")
    if money_basis not in MONEY_BASES:
        raise RuleSetError(f"rule {rule_key}: money_basis must be one of {list(MONEY_BASES)}")

    authority_kind = str(rule.get("authority_kind") or "").strip()
    if authority_kind not in AUTHORITY_KINDS:
        raise RuleSetError(
            f"rule {rule_key}: authority_kind must be one of {list(AUTHORITY_KINDS)} -- "
            "a statute, an invoice practice, a contract and a markup are different claims"
        )

    jurisdiction = _validate_jurisdiction(rule.get("jurisdiction"), rule_key=rule_key)
    conditions = normalized["conditions"]
    geographic = _is_geographic(conditions, jurisdiction)

    row_posture = rule.get("rest_of_world_posture")
    unknown_posture = rule.get("unknown_posture")
    if geographic:
        # The whole point of AC5: silence is not a posture. A rule naming three
        # countries and nothing else used to leave every other country, and every
        # row with no country at all, to whatever the evaluator happened to do.
        if row_posture is None or unknown_posture is None:
            raise RuleSetError(
                f"rule {rule_key} is geography-dependent, so it must declare both "
                "rest_of_world_posture and unknown_posture; use 'unresolved' when the "
                "decision has genuinely not been made -- it blocks a complete total "
                "instead of guessing one"
            )
        if str(row_posture) not in REST_OF_WORLD_POSTURES:
            raise RuleSetError(
                f"rule {rule_key}: rest_of_world_posture must be one of "
                f"{list(REST_OF_WORLD_POSTURES)}"
            )
        if str(unknown_posture) not in UNKNOWN_POSTURES:
            raise RuleSetError(
                f"rule {rule_key}: unknown_posture must be one of {list(UNKNOWN_POSTURES)}"
            )
        row_posture = str(row_posture)
        unknown_posture = str(unknown_posture)
    else:
        if row_posture is not None or unknown_posture is not None:
            raise RuleSetError(
                f"rule {rule_key} names no country, market or jurisdiction, so a Rest of "
                "World or Unknown posture on it would describe a geography it never reads"
            )
        row_posture = None
        unknown_posture = None

    source_evidence = _validate_source_evidence(
        rule.get("source_evidence"), rule_key=rule_key, authority_kind=authority_kind
    )

    return {
        "rule_key": rule_key,
        "label": normalized["label"] or rule_key,
        "scope_kind": normalized["scope_kind"],
        "scope_ref": normalized["scope_ref"],
        "category": normalized["category"],
        "form": normalized["form"],
        "rate": str(normalized["rate"]) if normalized["rate"] is not None else None,
        "amount_micros": normalized["amount_micros"],
        "cpm_micros": normalized["cpm_micros"],
        "tiers": normalized["tiers"],
        "currency": normalized["currency"],
        "base_target": normalized["base_target"],
        "money_basis": money_basis,
        "cascade_phase": normalized["cascade_phase"],
        "sequence_order": normalized["sequence_order"],
        "conditions": conditions,
        "source_type_scope": normalized["source_type_scope"],
        "jurisdiction": jurisdiction,
        "geography_dependent": geographic,
        "rest_of_world_posture": row_posture,
        "unknown_posture": unknown_posture,
        "effective_from": normalized["effective_from"].isoformat(),
        "effective_to": (
            normalized["effective_to"].isoformat() if normalized["effective_to"] else None
        ),
        "authority_kind": authority_kind,
        "source_evidence": source_evidence,
        "origin": normalized["origin"],
    }


def validate_ladder_rules(rules: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize and ORDER the ladder. The profile owns the ordering rule.

    Two rules sharing a ``(cascade_phase, sequence_order)`` are refused rather than
    tie-broken. A composition whose order depends on which row the database
    returned first produces a different gross total on a different day, and no
    amount of evidence downstream can reconstruct which one was meant.
    """

    normalized = [validate_ladder_rule(rule) for rule in rules]

    keys = [rule["rule_key"] for rule in normalized]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise RuleSetError("rule_key must be unique within a version: " + ", ".join(duplicates))

    positions: dict[tuple[int, int], str] = {}
    for rule in normalized:
        slot = (rule["cascade_phase"], rule["sequence_order"])
        clash = positions.get(slot)
        if clash is not None:
            raise RuleSetError(
                f"rules {clash} and {rule['rule_key']} both sit at phase "
                f"{slot[0]} order {slot[1]}; the composed total would depend on row order"
            )
        positions[slot] = rule["rule_key"]

    return sorted(
        normalized,
        key=lambda item: (item["cascade_phase"], item["sequence_order"], item["rule_key"]),
    )


def ladder_governed_dependencies(
    payload: Mapping[str, Any], ordered_rules: Sequence[Mapping[str, Any]]
) -> tuple[GovernedDependency, ...]:
    """Which Country nodes this ladder depends on (Story 37.9).

    THE GAP THIS CLOSES. ``core.market_governance`` could already refuse a market
    change that would silently re-mean a bound figure, and it had nothing to refuse:
    ``register_market_binding`` had no production caller, so ``fetch_used_by``
    truthfully returned an empty list and every market change was authorized. The
    guard reported safety it had never checked.

    A ladder rule pinned to a market is the dependent that matters most. Remove the
    market, or move a country out of it, and an already-published invoice figure
    means something else -- while the jurisdiction id still resolves and looks
    healthy, because binding on the id is exactly what makes a RELABEL safe.

    TWO SOURCES OF DEPENDENCY, and both are real:

    * ``jurisdiction`` of kind ``market`` or ``region`` -- the rule's own owner
      object, and the only one that pins its hierarchy version (AC5);
    * ``conditions.market`` -- the market ids the rule fires on. A rule that
      composes a fee only for `EMEA` depends on what `EMEA` contains just as much
      as one whose jurisdiction names it.

    ``jurisdiction`` of kind ``country`` DELIBERATELY yields nothing. A country is a
    ``child_value`` of the hierarchy, never a node, so there is no node id to depend
    on -- and inventing one would put a value in a table whose whole contract is
    governed identities. Membership changes reach the operator anyway: moving `FR`
    out of a market is assessed as ``country_removed_from_market`` against every
    dependent of that MARKET, which is where the impact actually lands.

    Pure: no connection, no clock. A duplicate (market, version) pair is collapsed
    because the used-by store is keyed on it -- two rules naming one market are one
    dependency, and reporting it twice would make the operator's list read as two
    different things to check.
    """

    seen: set[tuple[str, str | None]] = set()
    declared: list[GovernedDependency] = []

    def _add(node_id: str, hierarchy_version_id: str | None, label: str | None) -> None:
        node = str(node_id or "").strip()
        if not node:
            return
        key = (node, hierarchy_version_id)
        if key in seen:
            return
        seen.add(key)
        declared.append(
            GovernedDependency(
                object_kind=COUNTRY_OBJECT_KIND,
                node_id=node,
                hierarchy_version_id=hierarchy_version_id,
                label=label,
            )
        )

    for rule in ordered_rules:
        jurisdiction = rule.get("jurisdiction") or {}
        pinned = str(jurisdiction.get("hierarchy_version_id") or "").strip() or None
        if jurisdiction.get("kind") in _NODE_JURISDICTION_KINDS:
            _add(jurisdiction.get("id"), pinned, jurisdiction.get("label"))
        for market_id in (rule.get("conditions") or {}).get("market") or ():
            # The condition carries no pin of its own, so it inherits the rule's --
            # which is None for a rule whose jurisdiction is `none`. A null pin is
            # honest: it says "this dependency exists and names no version",
            # which is a weaker claim than a wrong version id.
            _add(market_id, pinned, None)

    _ = payload  # the ladder-level policy pins no node; only its rules do
    return tuple(declared)


#: Jurisdiction kinds that name a governed NODE. `country` names a `child_value`
#: and `none` names nothing, so neither can be a used-by dependency.
_NODE_JURISDICTION_KINDS = frozenset({"market", "region"})


#: The ladder's own policy — and only that.
#:
#: The RULES of a ladder are not here and must not be: they are proposed from
#: governed presets and confirmed through the adoption gesture next door
#: (`governance.md`, "A ladder is adopted where it is read"). A version composed
#: from this form carries the published rules forward untouched, so changing the
#: rounding of a ladder is not a way to empty it.
_LADDER_POLICY_FORM = (
    RuleSetFormField(
        key="rounding",
        question="How is each step of the ladder rounded?",
        kind="choice",
        options=(
            RuleSetFormOption(
                "half_even",
                "Half to even",
                "A half-way amount goes to the nearest even minor unit, so a long ladder does "
                "not drift upward step by step.",
            ),
            RuleSetFormOption(
                "half_up",
                "Half away from zero",
                "Familiar on an invoice, and it accumulates a small upward bias across the "
                "phases.",
            ),
        ),
        why=(
            "One rounding for the whole composition. A rounding chosen per rule is how a "
            "ladder ends up with several boundaries whose ORDER changes the total."
        ),
    ),
    RuleSetFormField(
        key="default_money_basis",
        question="In which money is a rule computed when it does not say?",
        kind="choice",
        options=(
            RuleSetFormOption(
                "native_source",
                "The currency the platform billed in",
                "No conversion happens before the fee is computed, so the fee matches the "
                "platform's own invoice.",
            ),
            RuleSetFormOption(
                "project_reporting",
                "This Project's reporting currency",
                "The amount is converted first, so the fee depends on the rate of the day.",
            ),
        ),
    ),
    RuleSetFormField(
        key="notes",
        question="What should a later reader know about this ladder version?",
        required=False,
    ),
)


register_profile(
    RuleSetProfile(
        key=PROFILE_TAX_FEE,
        family=FAMILY_TAX_FEE,
        label="Tax & Fee Rule Ladder",
        validate=validate_ladder_payload,
        validate_rules=validate_ladder_rules,
        form=_LADDER_POLICY_FORM,
        # AC6: every component is composed in exact money under one Money Policy.
        # A ladder that does not pin the policy version cannot state which currency
        # its reporting-basis rules were denominated in.
        required_reference_kinds=("money_policy_version",),
        # Story 37.9: the ladder declares what it depends on, at home, so the
        # Country used-by guard has something to refuse.
        governed_dependencies=ladder_governed_dependencies,
    )
)


# ---------------------------------------------------------------------------
# Resolution. Every read names the exact version it came from.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaxFeeLadder:
    """The published ladder of one Project, with the identity to reproduce it."""

    rule_set_id: str
    version_id: str
    version_number: int
    content_hash: str
    rounding: str
    default_money_basis: str
    rules: tuple[dict[str, Any], ...]

    @property
    def geography_dependent_rules(self) -> tuple[dict[str, Any], ...]:
        return tuple(rule for rule in self.rules if rule.get("geography_dependent"))

    @property
    def unresolved_geography_rules(self) -> tuple[dict[str, Any], ...]:
        """Rules whose Rest of World or Unknown posture is declared but undecided.

        These are what turn a headline total into a refusal (AC5, AC9): the ladder
        is complete as a document and incomplete as an answer, and those are two
        different states a surface must be able to show separately.
        """
        return tuple(
            rule
            for rule in self.geography_dependent_rules
            if rule.get("rest_of_world_posture") == "unresolved"
            or rule.get("unknown_posture") == "unresolved"
        )

    def pinned_hierarchy_versions(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    str(rule["jurisdiction"]["hierarchy_version_id"])
                    for rule in self.geography_dependent_rules
                    if rule["jurisdiction"].get("hierarchy_version_id")
                }
            )
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "rule_set_id": self.rule_set_id,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "content_hash": self.content_hash,
            "rounding": self.rounding,
            "default_money_basis": self.default_money_basis,
            "rule_count": len(self.rules),
            "rules": [dict(rule) for rule in self.rules],
        }


def try_resolve_tax_fee_ladder(conn, *, project_id: str) -> TaxFeeLadder | None:
    """The published ladder, or ``None`` when this Project has never published one.

    ``None`` means "no ladder", never "an empty ladder". An unreadable head raises
    through :mod:`core.governance_rule_sets`, so an outage cannot be read as a
    Project that composes no fees and reports gross equal to net.
    """

    resolved = active_version(
        conn, project_id=project_id, family=FAMILY_TAX_FEE, name=LADDER_NAME
    )
    if resolved is None:
        return None
    head, version = resolved
    payload = version.get("payload") or {}
    return TaxFeeLadder(
        rule_set_id=str(head["id"]),
        version_id=str(version["id"]),
        version_number=int(version["version_number"]),
        content_hash=str(version["content_hash"]),
        rounding=str(payload.get("rounding") or "half_even"),
        default_money_basis=str(payload.get("default_money_basis") or "native_source"),
        rules=tuple(dict(rule) for rule in (version.get("ordered_rules") or [])),
    )


def resolve_tax_fee_ladder(conn, *, project_id: str) -> TaxFeeLadder:
    """The published ladder, or a typed gap. Never an implicit empty one."""

    ladder = try_resolve_tax_fee_ladder(conn, project_id=project_id)
    if ladder is None:
        raise TaxFeeGap(
            "tax_fee_ladder_unpublished",
            "This Project has no published Tax & Fee Rule Ladder, so no component "
            "can be composed and no gross total can be stated.",
        )
    return ladder
