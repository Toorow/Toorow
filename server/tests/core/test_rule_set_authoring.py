"""Composing a Rule Set version from the console, for every family.

WHAT THIS EXISTS TO CATCH. Completeness criterion [24] of `governance.md` --
*"a Rule Set can be read but no version of it can be authored anywhere in the
console"* -- was closed for `tax_fee` on 2026-08-17 and stayed OPEN for everyone
else: `money_policy`, `fx_ingestion`, `timezone_policy`, `dq_policy` and
`metric_reconciliation` rendered a read-only panel whose empty state named no
gesture that would fill it. This file pins the door that closes it for the class,
and the three properties that make it a governed act rather than a form:

  - the QUESTIONS come from the profile, never from the console, so a family and
    the validator that judges its answers cannot drift apart;
  - composing changes NOTHING in force, and putting in force is a second act,
    through a Change Set, with its own actor;
  - a new version CARRIES the ordered rules and the pinned references of the one
    it replaces, so correcting a ladder's rounding is not a way to empty it.

Every fact here is a database fact -- an immutable version, a moved head pointer,
a content-addressed change set -- so a mocked test would prove nothing. Gated on
`TEST_POSTGRES_DSN` through the repository's own `live_postgres`, which refuses a
DSN that is not demonstrably disposable.

Contract: `docs/product-architecture/governance.md`, "A Rule Set version is
drafted, then published" (2026-08-24).
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def conn(live_postgres):
    return live_postgres


def _new_id(prefix: str) -> str:
    """Crockford base32, 26 chars: `governance_rule_sets` CHECKs the ULID shape."""
    from ulid import ULID

    return f"{prefix}_{ULID()}"


@pytest.fixture()
def scope(conn):
    org_id = _new_id("org")
    project_id = _new_id("proj")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s, %s, %s, %s)",
            (org_id, "Authoring test org", org_id.replace("_", "-").lower(), "authoring-test"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (
                project_id,
                org_id,
                "Authoring test project",
                project_id.replace("_", "-").lower(),
                "authoring-test",
            ),
        )
    return {"org_id": org_id, "project_id": project_id}


@pytest.fixture()
def currency_vocabulary(conn):
    """A REAL vocabulary snapshot. The Money Policy profile requires a pinned one,
    and the whole point of the pin is that it names a snapshot that exists."""
    from core.currency_vocabulary import import_currency_vocabulary

    return import_currency_vocabulary(
        conn, actor="test", source_version="ISO-4217:2026-01", effective_date="2026-01-01"
    )


@pytest.fixture()
def money_head(conn, scope):
    """The head only. An unpublished policy is the state criterion [24] complains
    about: the Rule Set is readable and no version of it can be composed."""
    from core.governance_rule_sets import ensure_rule_set
    from core.money_policy import FAMILY_MONEY, POLICY_NAME

    head = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family=FAMILY_MONEY,
        name=POLICY_NAME,
        label="Money policy",
        actor="test",
    )
    return str(head["id"])


# ---------------------------------------------------------------------------
# The declaration lives with the family, and it covers every family.
# ---------------------------------------------------------------------------


#: The one registered family with no console door, and the reason it is allowed
#: to have none: an `entity_derivation` version is a tree of ordered match rules
#: rather than a policy, AND no product path creates one -- `create_entity_rule_set`
#: and `publish_entity_rule_set` have callers in tests only, so no Project carries
#: such a rule set and nothing in the console reads one. A NINTH family fails this
#: test until it declares a form or names the gesture that composes it: that is
#: what keeps criterion [24] closed rather than closed once.
_FAMILIES_WITH_NO_CONSOLE_DOOR = {"entity_derivation"}


def test_every_registered_family_declares_how_its_version_is_composed():
    from core.governance_rule_sets import registered_profiles

    undeclared = {
        profile.family
        for profile in registered_profiles()
        if not profile.form and not profile.composed_by
    }
    assert undeclared == _FAMILIES_WITH_NO_CONSOLE_DOOR, (
        "every Rule Set family readable in the console must declare a form or name the "
        f"gesture that composes it elsewhere; undeclared: {sorted(undeclared)}"
    )


def test_a_declared_field_names_a_key_the_validator_actually_normalizes(currency_vocabulary):
    """A field whose key is not a payload key offers a question nothing stores.

    Measured against the profile's OWN normalizer rather than against a list
    written here: the whole reason the form lives beside the validator is that
    one of them cannot move without the other noticing.
    """

    from core.governance_rule_sets import get_profile
    from core.money_policy import PROFILE_MONEY

    spec = get_profile(PROFILE_MONEY)
    normalized = spec.validate(
        {
            "reporting_currency": "EUR",
            "rounding": "half_even",
            "max_staleness_days": 7,
            "rate_source_priority": ["ecb"],
        }
    )
    offered = {field.key for field in spec.form}
    assert offered <= set(normalized), sorted(offered - set(normalized))


def test_a_profile_cannot_declare_a_form_and_delegate_at_once():
    from core.governance_rule_sets import (
        RuleSetError,
        RuleSetFormField,
        RuleSetProfile,
        register_profile,
    )

    with pytest.raises(RuleSetError, match="one place it is written"):
        register_profile(
            RuleSetProfile(
                key="two_doors_v1",
                family="two_doors",
                label="Two doors",
                validate=dict,
                form=(RuleSetFormField(key="a", question="A?"),),
                composed_by="somewhere else",
            )
        )


# ---------------------------------------------------------------------------
# Read before write.
# ---------------------------------------------------------------------------


def test_the_plan_offers_the_questions_the_family_declares(conn, scope, money_head):
    from core.rule_set_authoring import plan_authoring

    plan = plan_authoring(conn, project_id=scope["project_id"], rule_set_id=money_head)

    assert plan["in_force"] is None
    assert plan["draft"] is None
    assert plan["composed_by"] is None
    keys = [field["key"] for field in plan["fields"]]
    assert "reporting_currency" in keys and "rounding" in keys
    rounding = next(field for field in plan["fields"] if field["key"] == "rounding")
    # A choice states what each answer costs WHERE it is chosen.
    assert {option["value"] for option in rounding["options"]} == {"half_even", "half_up"}
    assert all(option["consequence"] for option in rounding["options"])
    # And the question is asked in a person's words, not a column's.
    assert "reporting_currency" not in rounding["question"]


def test_a_family_composed_elsewhere_names_that_gesture_rather_than_offering_a_form(
    conn, scope
):
    """`source_currency` is written on the mapping screen, and says so.

    The alternative -- a second editor here -- would be two places to write one
    fact, which the family's own validator refuses by refusing every payload key.
    """

    from core.governance_rule_sets import ensure_rule_set
    from core.rule_set_authoring import plan_authoring
    from core.source_currency_bindings import BINDING_NAME, FAMILY_SOURCE_CURRENCY

    head = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family=FAMILY_SOURCE_CURRENCY,
        name=BINDING_NAME,
        label="Declared source currencies",
        actor="test",
    )
    plan = plan_authoring(conn, project_id=scope["project_id"], rule_set_id=str(head["id"]))
    assert plan["fields"] == []
    assert "mapping screen" in (plan["composed_by"] or "")


# ---------------------------------------------------------------------------
# Composing. Two acts, and the first one changes nothing.
# ---------------------------------------------------------------------------


def _compose_money(conn, scope, money_head, **overrides):
    from core.rule_set_authoring import draft_from_answers

    answers = {
        "reporting_currency": "EUR",
        "rounding": "half_even",
        "max_staleness_days": "7",
        "rate_source_priority": "ecb, bloomberg",
        "allow_triangulation": "false",
        "allow_carry_forward": "true",
    }
    answers.update(overrides)
    return draft_from_answers(
        conn,
        project_id=scope["project_id"],
        rule_set_id=money_head,
        answers=answers,
        actor="owner@example.com",
    )


def test_composing_stores_a_draft_and_puts_nothing_in_force(
    conn, scope, money_head, currency_vocabulary
):
    from core.governance_rule_sets import fetch_rule_set

    draft = _compose_money(conn, scope, money_head)

    assert draft["status"] == "draft"
    head = fetch_rule_set(conn, project_id=scope["project_id"], rule_set_id=money_head)
    # THE PROPERTY THE WHOLE SPLIT EXISTS FOR: nothing this Project computes with
    # has changed. A door that composed and published in one call could not be
    # read before it was taken.
    assert head["current_version_id"] is None
    assert str(head["pending_version_id"]) == str(draft["id"])
    # The answers went through the profile: the text answers are typed values and
    # the derived keys the form never offered are present.
    assert draft["payload"]["max_staleness_days"] == 7
    assert draft["payload"]["rate_source_priority"] == ["ecb", "bloomberg"]
    assert draft["payload"]["internal_scale"] == "micros"


def test_the_required_pin_is_resolved_for_the_first_version(
    conn, scope, money_head, currency_vocabulary
):
    """Nobody is asked for a `currency_vocabulary_version`, and one is pinned.

    The profile refuses a Money Policy that does not name the exact vocabulary
    snapshot its currency was chosen from -- and that refusal names a column, not
    a gesture. Resolving the CURRENT snapshot at composition time pins it exactly,
    which is the opposite of depending on "the latest".
    """

    draft = _compose_money(conn, scope, money_head)
    pins = {item["kind"]: item for item in draft["requires"]}
    assert pins["currency_vocabulary_version"]["version_id"] == str(currency_vocabulary["id"])


def test_a_required_pin_nothing_can_supply_is_refused_with_the_gesture(monkeypatch):
    """The refusal names the gesture, not the column.

    `draft_version` refuses a Money Policy with no pinned vocabulary version by
    naming the KIND -- correct for a publisher, useless to the person holding the
    form. Every entry of the pin table therefore carries the sentence that clears
    it, and this asserts the table cannot grow an entry without one.

    Written against the resolver rather than against an empty database: whether a
    vocabulary snapshot happens to be imported is shared state of the test
    database, and a test that skips when it is proves nothing on the day it
    matters.
    """

    from core import rule_set_authoring as authoring
    from core.governance_rule_sets import get_profile
    from core.money_policy import PROFILE_MONEY

    assert all(gesture.strip() for _, gesture in authoring._REQUIRED_PINS.values())

    monkeypatch.setitem(
        authoring._REQUIRED_PINS,
        "currency_vocabulary_version",
        (lambda conn, *, project_id: None, authoring._REQUIRED_PINS[
            "currency_vocabulary_version"
        ][1]),
    )
    with pytest.raises(authoring.AuthoringRefused) as caught:
        authoring._pins_for(
            None, project_id="proj_x", spec=get_profile(PROFILE_MONEY), carried=[]
        )
    assert "Master Data" in str(caught.value)


def test_a_blank_required_answer_is_refused_by_its_question(
    conn, scope, money_head, currency_vocabulary
):
    from core.rule_set_authoring import AuthoringRefused

    with pytest.raises(AuthoringRefused) as caught:
        _compose_money(conn, scope, money_head, reporting_currency="")
    # The QUESTION, not the key: a person who left a box empty needs to be told
    # which box, in the words the box used.
    assert "Which currency does this Project report in?" in str(caught.value)


def test_the_profile_refusal_reaches_the_caller_whole(
    conn, scope, money_head, currency_vocabulary
):
    from core.rule_set_authoring import AuthoringRefused

    with pytest.raises(AuthoringRefused) as caught:
        _compose_money(conn, scope, money_head, reporting_currency="XAU")
    # `XAU` is a metal code: the profile's own sentence explains why it cannot be
    # a reporting currency, and a paraphrase would lose the only useful half.
    assert "legal tender" in str(caught.value)


def test_an_answer_the_family_never_asked_for_is_refused(
    conn, scope, money_head, currency_vocabulary
):
    from core.rule_set_authoring import AuthoringRefused, draft_from_answers

    with pytest.raises(AuthoringRefused, match="no question about"):
        draft_from_answers(
            conn,
            project_id=scope["project_id"],
            rule_set_id=money_head,
            answers={"internal_scale": "cents"},
            actor="owner@example.com",
        )


# ---------------------------------------------------------------------------
# Publishing. The second act, through a Change Set.
# ---------------------------------------------------------------------------


def test_publishing_a_draft_puts_that_exact_version_in_force(
    conn, scope, money_head, currency_vocabulary
):
    from core.governance_rule_sets import fetch_rule_set
    from core.money_policy import resolve_money_policy
    from core.rule_set_authoring import publish_draft

    draft = _compose_money(conn, scope, money_head)
    outcome = publish_draft(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        rule_set_id=money_head,
        version_id=str(draft["id"]),
        actor="approver@example.com",
    )

    assert outcome["version"]["status"] == "published"
    # THE EXACT DRAFT, not a second composition of the same answers: what was
    # read on the screen is what went in force.
    assert str(outcome["version"]["id"]) == str(draft["id"])
    head = fetch_rule_set(conn, project_id=scope["project_id"], rule_set_id=money_head)
    assert str(head["current_version_id"]) == str(draft["id"])
    assert head["pending_version_id"] is None

    # And the runtime reader -- the one that fails closed rather than defaulting
    # to EUR -- now resolves it. A version nothing resolves proves a transport.
    policy = resolve_money_policy(conn, project_id=scope["project_id"])
    assert policy.reporting_currency == "EUR"
    assert str(policy.version_id) == str(draft["id"])


def test_the_two_acts_record_two_actors(conn, scope, money_head, currency_vocabulary):
    from core.rule_set_authoring import publish_draft

    draft = _compose_money(conn, scope, money_head)
    outcome = publish_draft(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        rule_set_id=money_head,
        version_id=str(draft["id"]),
        actor="approver@example.com",
    )
    change_set = outcome["change_set"]

    assert draft["created_by"] == "owner@example.com"
    assert change_set["state"] == "confirmed"
    assert str(change_set["result_version_id"]) == str(draft["id"])
    # The publication went through a Change Set like every other governed change,
    # and the intent NAMES the draft rather than carrying a second payload.
    assert change_set["intent"]["version_id"] == str(draft["id"])
    assert "payload" not in change_set["intent"]


def test_publishing_the_same_draft_twice_publishes_one_version(
    conn, scope, money_head, currency_vocabulary
):
    from core.governance_rule_sets import list_versions
    from core.rule_set_authoring import publish_draft

    draft = _compose_money(conn, scope, money_head)
    for _ in range(2):
        publish_draft(
            conn,
            project_id=scope["project_id"],
            org_id=scope["org_id"],
            rule_set_id=money_head,
            version_id=str(draft["id"]),
            actor="approver@example.com",
        )
    versions = list_versions(conn, project_id=scope["project_id"], rule_set_id=money_head)
    assert len(versions) == 1


def test_a_superseded_version_is_never_put_back_in_force(
    conn, scope, money_head, currency_vocabulary
):
    from core.rule_set_authoring import AuthoringRefused, publish_draft

    first = _compose_money(conn, scope, money_head)
    publish_draft(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        rule_set_id=money_head,
        version_id=str(first["id"]),
        actor="approver@example.com",
    )
    second = _compose_money(conn, scope, money_head, rounding="half_up")
    publish_draft(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        rule_set_id=money_head,
        version_id=str(second["id"]),
        actor="approver@example.com",
    )

    with pytest.raises(AuthoringRefused, match="superseded"):
        publish_draft(
            conn,
            project_id=scope["project_id"],
            org_id=scope["org_id"],
            rule_set_id=money_head,
            version_id=str(first["id"]),
            actor="approver@example.com",
        )


# ---------------------------------------------------------------------------
# What a new version carries, it carries.
# ---------------------------------------------------------------------------


def test_changing_a_ladders_rounding_does_not_empty_its_ladder(
    conn, scope, currency_vocabulary
):
    """The one that would have been a silent data loss.

    A ladder's POLICY is composed here; its RULES are proposed from governed
    presets and confirmed by the adoption gesture next door. A form that replaced
    the version wholesale would publish an empty ladder under an immutable hash,
    and every fee this Project charges would quietly become zero.
    """

    from core.governance_rule_sets import draft_version, ensure_rule_set, publish_version
    from core.money_policy import FAMILY_MONEY, POLICY_NAME
    from core.rule_set_authoring import draft_from_answers, plan_authoring, publish_draft
    from core.tax_fee_rule_set import FAMILY_TAX_FEE, LADDER_NAME, PROFILE_TAX_FEE

    # A published Money Policy, because the ladder profile requires a pinned one.
    money = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family=FAMILY_MONEY,
        name=POLICY_NAME,
        label="Money policy",
        actor="test",
    )
    money_draft = draft_from_answers(
        conn,
        project_id=scope["project_id"],
        rule_set_id=str(money["id"]),
        answers={
            "reporting_currency": "EUR",
            "rounding": "half_even",
            "max_staleness_days": "7",
            "rate_source_priority": "ecb",
        },
        actor="owner@example.com",
    )
    publish_draft(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        rule_set_id=str(money["id"]),
        version_id=str(money_draft["id"]),
        actor="owner@example.com",
    )

    ladder = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family=FAMILY_TAX_FEE,
        name=LADDER_NAME,
        label="Tax and fee ladder",
        actor="test",
    )
    rule = {
        "rule_key": "agency_fee",
        "label": "Agency fee",
        "category": "AGENCY_FEE",
        "form": "PERCENTAGE",
        "rate": "0.030000",
        "base_target": "NET_MEDIA",
        "cascade_phase": 5,
        "sequence_order": 1,
        "scope_kind": "project",
        "effective_from": "2026-01-01",
        "authority_kind": "agency_contract",
        "origin": "operator",
        "source_evidence": {
            "issuer": "Agency",
            "reference": "Master services agreement",
            "reference_version": "v3",
        },
    }
    first = draft_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=str(ladder["id"]),
        profile=PROFILE_TAX_FEE,
        label="v1",
        payload={"rounding": "half_even", "default_money_basis": "native_source"},
        ordered_rules=[rule],
        requires=[
            {
                "kind": "money_policy_version",
                "object_id": str(money["id"]),
                "version_id": str(money_draft["id"]),
            }
        ],
        actor="test",
    )
    publish_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=str(ladder["id"]),
        version_id=str(first["id"]),
        actor="test",
    )

    # The plan STATES what would be carried, before anything is composed.
    plan = plan_authoring(conn, project_id=scope["project_id"], rule_set_id=str(ladder["id"]))
    assert plan["carries_forward"]["ordered_rule_count"] == 1
    assert plan["carries_forward"]["pinned_references"][0]["kind"] == "money_policy_version"

    changed = draft_from_answers(
        conn,
        project_id=scope["project_id"],
        rule_set_id=str(ladder["id"]),
        answers={"rounding": "half_up", "default_money_basis": "native_source"},
        actor="owner@example.com",
    )
    assert changed["payload"]["rounding"] == "half_up"
    assert len(changed["ordered_rules"]) == 1
    assert changed["ordered_rules"][0]["rule_key"] == "agency_fee"
    assert changed["requires"][0]["version_id"] == str(money_draft["id"])


def test_a_key_the_form_never_offers_survives_a_composition(
    conn, scope, currency_vocabulary
):
    """A DQ baseline is not a question, and it must not be dropped by answering one.

    `schema` and `volume` refuse a version with no frozen baseline; the baseline
    itself is an observation, not an answer. A form that replaced the payload
    would delete it and the next version would be refused -- or worse, accepted
    with an empty comparison that passes by agreeing with anything.
    """

    from core.controls_quality import FAMILY_DQ, PROFILE_DQ
    from core.governance_rule_sets import draft_version, ensure_rule_set, publish_version
    from core.rule_set_authoring import draft_from_answers

    head = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family=FAMILY_DQ,
        name="project_dq_policy",
        label="Data quality policy",
        actor="test",
    )
    baseline = {"columns": ["spend", "clicks"], "frozen_on": "2026-08-01"}
    first = draft_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=str(head["id"]),
        profile=PROFILE_DQ,
        label="v1",
        payload={
            "check": "schema",
            "severity": "blocking",
            "window_days": 1,
            "baseline": baseline,
        },
        actor="test",
    )
    publish_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=str(head["id"]),
        version_id=str(first["id"]),
        actor="test",
    )

    changed = draft_from_answers(
        conn,
        project_id=scope["project_id"],
        rule_set_id=str(head["id"]),
        answers={"severity": "degrading"},
        actor="owner@example.com",
    )
    assert changed["payload"]["severity"] == "degrading"
    assert changed["payload"]["baseline"] == baseline
    assert changed["payload"]["check"] == "schema"
