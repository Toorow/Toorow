"""Adopting a preset into a ladder, proved against a live PostgreSQL.

WHAT THIS EXISTS TO CATCH. Until 2026-08-17 the console could READ a Rule Set
and nothing more: `grep -rn "ordered_rules" ui/admin/src` returned five hits and
every one was a read, while `controls_owner_commands._apply_rule_set` had been
able to publish a version the whole time. The ladder tab told an operator that
adopting a preset "is a Change Set prepared by this ladder's owner", and no
screen in the product let that owner prepare one.

Every fact here is a database fact -- a published version, a pinned money policy
version, a content-addressed change set -- so a mocked test would prove nothing.
Gated on `TEST_POSTGRES_DSN` through the repository's own `live_postgres`, which
refuses a DSN that is not demonstrably disposable.

Contract: `docs/product-architecture/governance.md`, "A ladder is adopted where
it is read" (2026-08-17).
"""

from __future__ import annotations

import uuid

import pytest

TEST_ORG_ID = "org_adoption_test"


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
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s)",
            (org_id, "Adoption test org", org_id.replace("_", "-").lower(), "adoption-test"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (
                project_id,
                org_id,
                "Adoption test project",
                project_id.replace("_", "-").lower(),
                "adoption-test",
            ),
        )
    return {"org_id": org_id, "project_id": project_id}


@pytest.fixture()
def money_policy(conn, scope):
    """A REAL published money policy. Fabricating an id would prove the transport
    works against data the product cannot produce, and the ladder's whole point is
    that its arithmetic pins an exact version rather than "the latest"."""
    from core.currency_vocabulary import import_currency_vocabulary
    from core.governance_rule_sets import draft_version, ensure_rule_set, publish_version
    from core.money_policy import FAMILY_MONEY, POLICY_NAME, PROFILE_MONEY

    vocabulary = import_currency_vocabulary(
        conn, actor="test", source_version="ISO-4217:2026-01", effective_date="2026-01-01"
    )
    head = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family=FAMILY_MONEY,
        # `resolve_money_policy` looks the head up by (family, POLICY_NAME). A
        # fixture that invents its own name publishes a version nothing resolves.
        name=POLICY_NAME,
        label="Money policy",
        actor="test",
    )
    draft = draft_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=head["id"],
        profile=PROFILE_MONEY,
        label="v1",
        payload={
            "reporting_currency": "EUR",
            "rounding": "half_even",
            "max_staleness_days": 7,
            "rate_source_priority": ["ecb"],
        },
        requires=[
            {
                "kind": "currency_vocabulary_version",
                "object_id": str(vocabulary["vocabulary_key"]),
                "version_id": str(vocabulary["id"]),
            }
        ],
        actor="test",
    )
    published = publish_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=head["id"],
        version_id=draft["id"],
        actor="test",
    )
    return {"object_id": head["id"], "version_id": published["id"]}


@pytest.fixture()
def ladder(conn, scope):
    """The head only. An unpublished ladder is the normal state for an adoption --
    it is the empty ladder that has proposals to offer."""
    from core.governance_rule_sets import ensure_rule_set
    from core.tax_fee_rule_set import FAMILY_TAX_FEE, LADDER_NAME

    head = ensure_rule_set(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        family=FAMILY_TAX_FEE,
        name=LADDER_NAME,
        label="Tax and fee ladder",
        actor="test",
    )
    return head["id"]


def _agency_preset(conn, scope, **overrides):
    """A jurisdiction-free preset: no country governs it, so it qualifies for any
    Project without needing a Country registry the fixture would have to invent."""
    from core.tax_fee_presets import record_preset_version

    payload = {
        "preset_key": f"agency_fee_{uuid.uuid4().hex[:8]}",
        "label": "Agency fee",
        "issuer": "Agency",
        "source_reference": "Master services agreement",
        "source_reference_version": "v3",
        "document_ref": "MSA-2026-B",
        "jurisdiction_kind": "none",
        "jurisdiction_code": None,
        "taxable_subject": "Media buying services",
        "category": "AGENCY_FEE",
        "form": "PERCENTAGE",
        "rate": "0.030000",
        "base_target": "NET_MEDIA",
        "qualifications": [
            {
                "code": "contractual_basis",
                "question": "Does the master services agreement actually invoice this fee?",
            }
        ],
        "effective_from": "2026-01-01",
    }
    payload.update(overrides)
    stored = record_preset_version(
        conn,
        payload=payload,
        org_id=scope["org_id"],
        actor="owner@example.com",
        publish=True,
    )
    return stored


def _preset_version_id(stored) -> str:
    return str(stored["id"] if isinstance(stored, dict) else stored)


# ---------------------------------------------------------------------------
# The plan is a READ, and it says what would be published.
# ---------------------------------------------------------------------------


def test_the_plan_names_every_decision_before_anything_is_written(
    conn, scope, ladder, money_policy
):
    from core.rule_set_adoption import plan_adoption

    preset = _agency_preset(conn, scope)
    plan = plan_adoption(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        rule_set_id=ladder,
        preset_version_id=_preset_version_id(preset),
    )

    # The resulting ladder, in order -- not a count of it.
    assert len(plan["resulting_rules"]) == 1
    assert plan["resulting_rules"][0]["category"] == "AGENCY_FEE"

    # The money policy is PINNED in the plan, so the operator sees the version
    # the arithmetic would be composed under before agreeing to it.
    assert plan["money_policy"]["version_id"] == money_policy["version_id"]

    # One unproven qualification, and no posture question: this preset names no
    # geography, so a Rest of World posture on it would describe a geography it
    # never reads.
    kinds = [decision["kind"] for decision in plan["decisions_required"]]
    assert kinds == ["qualification"]
    assert "invoice this fee" in plan["decisions_required"][0]["question"]

    # Nothing was published by planning.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_version_id FROM app.governance_rule_sets WHERE id = %s",
            (ladder,),
        )
        assert cur.fetchone()[0] is None


def test_a_project_with_no_money_policy_is_refused_with_the_gesture_that_fixes_it(
    conn, scope, ladder
):
    """A fee ladder whose money policy is "the latest" has no defined arithmetic,
    so this refuses rather than composing under whatever is current at confirm."""
    from core.rule_set_adoption import AdoptionRefused, plan_adoption

    preset = _agency_preset(conn, scope)
    with pytest.raises(AdoptionRefused) as raised:
        plan_adoption(
            conn,
            project_id=scope["project_id"],
            org_id=scope["org_id"],
            rule_set_id=ladder,
            preset_version_id=_preset_version_id(preset),
        )
    assert raised.value.code == "money_policy_not_confirmed"
    assert "Currency & FX" in str(raised.value)


# ---------------------------------------------------------------------------
# The write. Three recorded operations, and a published version at the end.
# ---------------------------------------------------------------------------


def test_adopting_publishes_a_version_readable_by_the_capability_resolver(
    conn, scope, ladder, money_policy
):
    """A version only the change set can see is not a published ladder, so this
    reads back through `resolve_tax_fee_ladder` -- the code the compiler and the
    Datastream panel use."""
    from core.rule_set_adoption import adopt_preset
    from core.tax_fee_rule_set import resolve_tax_fee_ladder

    preset = _agency_preset(conn, scope)
    outcome = adopt_preset(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        rule_set_id=ladder,
        preset_version_id=_preset_version_id(preset),
        answers={"qualification:0": "confirmed"},
        actor="operator@example.com",
    )
    assert outcome["change_set"]["state"] == "confirmed"
    version_id = outcome["change_set"]["result_version_id"]
    assert version_id, "no Project can have a published ladder if this is NULL"

    resolved = resolve_tax_fee_ladder(conn, project_id=scope["project_id"])
    assert resolved.version_id == version_id
    assert len(resolved.rules) == 1
    rule = resolved.rules[0]
    assert rule["category"] == "AGENCY_FEE"
    # The day someone checked -- which an adoption IS, since every unproven
    # qualification had to be answered to get here.
    assert rule["source_evidence"]["last_verified_on"] is not None
    assert rule["source_evidence"]["preset_version_id"] == _preset_version_id(preset)


def test_an_unanswered_qualification_refuses_rather_than_adopting_at_eighty_percent(
    conn, scope, ladder, money_policy
):
    from core.rule_set_adoption import AdoptionRefused, adopt_preset

    preset = _agency_preset(conn, scope)
    with pytest.raises(AdoptionRefused) as raised:
        adopt_preset(
            conn,
            project_id=scope["project_id"],
            org_id=scope["org_id"],
            rule_set_id=ladder,
            preset_version_id=_preset_version_id(preset),
            answers={},
            actor="operator@example.com",
        )
    assert raised.value.code == "decision_unanswered"

    # And nothing was published on the way to refusing.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_version_id FROM app.governance_rule_sets WHERE id = %s",
            (ladder,),
        )
        assert cur.fetchone()[0] is None


def test_answering_a_qualification_with_no_does_not_adopt_it_anyway(
    conn, scope, ladder, money_policy
):
    """"No, or not yet" is an answer the question offers, and it stops the
    adoption. Recording it as adopted-with-a-caveat is how a wrong invoice is
    published with a paper trail that says someone knew."""
    from core.rule_set_adoption import AdoptionRefused, adopt_preset

    preset = _agency_preset(conn, scope)
    with pytest.raises(AdoptionRefused) as raised:
        adopt_preset(
            conn,
            project_id=scope["project_id"],
            org_id=scope["org_id"],
            rule_set_id=ladder,
            preset_version_id=_preset_version_id(preset),
            answers={"qualification:0": "not_confirmed"},
            actor="operator@example.com",
        )
    assert raised.value.code == "qualification_not_confirmed"


def test_the_same_gesture_twice_publishes_one_version(conn, scope, ladder, money_policy):
    """Idempotent by the key its three operations share, exactly as
    `arm_event_stream` is: a retried click replays instead of minting a rival."""
    from core.rule_set_adoption import adopt_preset

    preset = _agency_preset(conn, scope)
    args = {
        "project_id": scope["project_id"],
        "org_id": scope["org_id"],
        "rule_set_id": ladder,
        "preset_version_id": _preset_version_id(preset),
        "answers": {"qualification:0": "confirmed"},
        "actor": "operator@example.com",
        "idempotency_key": "adopt-once",
    }
    first = adopt_preset(conn, **args)
    again = adopt_preset(conn, **args)

    assert again["change_set"]["id"] == first["change_set"]["id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.governance_rule_set_versions "
            "WHERE rule_set_id = %s AND status = 'published'",
            (ladder,),
        )
        assert cur.fetchone()[0] == 1


def test_a_rule_key_already_published_is_refused_as_a_rewrite(
    conn, scope, ladder, money_policy
):
    from core.rule_set_adoption import AdoptionRefused, adopt_preset, plan_adoption

    preset = _agency_preset(conn, scope)
    adopt_preset(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        rule_set_id=ladder,
        preset_version_id=_preset_version_id(preset),
        answers={"qualification:0": "confirmed"},
        actor="operator@example.com",
    )
    with pytest.raises(AdoptionRefused) as raised:
        plan_adoption(
            conn,
            project_id=scope["project_id"],
            org_id=scope["org_id"],
            rule_set_id=ladder,
            preset_version_id=_preset_version_id(preset),
        )
    assert raised.value.code == "rule_key_already_published"


def test_a_preset_that_no_longer_qualifies_is_refused_at_write_time(
    conn, scope, ladder, money_policy
):
    """The offer is recomputed here, never trusted from the browser: a tab held
    open across a registry change would otherwise adopt a dead candidate."""
    from core.rule_set_adoption import AdoptionRefused, adopt_preset

    with pytest.raises(AdoptionRefused) as raised:
        adopt_preset(
            conn,
            project_id=scope["project_id"],
            org_id=scope["org_id"],
            rule_set_id=ladder,
            preset_version_id="tfp_does_not_exist",
            answers={},
            actor="operator@example.com",
        )
    assert raised.value.code == "proposal_not_qualified"


def test_a_second_adoption_carries_the_published_rounding_forward(
    conn, scope, ladder, money_policy
):
    """The defect this pins: posting an empty payload lets `validate_ladder_payload`
    re-default `rounding` to `half_even`. A ladder published `half_up` would have
    had the rounding boundary of every total silently changed by a gesture that
    claimed only to add one rule."""
    from core.governance_rule_sets import draft_version, publish_version
    from core.rule_set_adoption import adopt_preset
    from core.tax_fee_rule_set import PROFILE_TAX_FEE, resolve_tax_fee_ladder

    seed = draft_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=ladder,
        profile=PROFILE_TAX_FEE,
        label="Ladder with half_up",
        payload={"rounding": "half_up", "default_money_basis": "native_source"},
        ordered_rules=[],
        requires=[
            {
                "kind": "money_policy_version",
                "object_id": money_policy["object_id"],
                "version_id": money_policy["version_id"],
            }
        ],
        actor="test",
    )
    publish_version(
        conn,
        project_id=scope["project_id"],
        rule_set_id=ladder,
        version_id=seed["id"],
        actor="test",
    )

    preset = _agency_preset(conn, scope)
    adopt_preset(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        rule_set_id=ladder,
        preset_version_id=_preset_version_id(preset),
        answers={"qualification:0": "confirmed"},
        actor="operator@example.com",
    )

    resolved = resolve_tax_fee_ladder(conn, project_id=scope["project_id"])
    assert resolved.rounding == "half_up", (
        "adopting one rule must not reset the ladder's rounding policy"
    )
