"""Story 48.4: the Tax & Fees paths that only a live PostgreSQL can prove.

`test_tax_fee_rule_set.py` has named this file in its own docstring since the
story's first commit -- "the database-bound halves ... are exercised by
`test_tax_fees_governance_pg.py`, gated on a live PostgreSQL" -- and the file did
not exist. A docstring that promises a proof is worth less than no docstring: it
is read as evidence.

What it proves, and why each one needs a database rather than a fixture:

1. **`TaxFeesCompiler.project_evidence` REACHES the preset proposals.** This is the
   reachability half of completeness criterion [0], and it is exactly the failure
   `scripts/ledger_evidence_reachability.py` exists to catch: `propose_from_presets`
   was correct, tested and unreachable from the activation path for five days. A
   fixture asserting the compiler's `assess` behaviour cannot see that, because it
   is handed the evidence dict the store was supposed to fill.
2. **A preset that matches no jurisdiction of this Project is NOT proposed**, on
   real rows read back through `list_preset_versions`, so the narrowing is proved
   against the query and not against a Python list.
3. **An unpublished preset never reaches an operator**, which is the difference
   between shared reference material and Project policy.

Run it with `python scripts/disposable_postgres.py up` and the DSN it prints.
Without a database every test here SKIPS -- and a skip is not a pass.
"""

from __future__ import annotations

import os
import uuid

import pytest
from core.capability_compilers import TaxFeesCompiler, _tax_fee_preset_proposals
from core.tax_fee_presets import record_preset_version

TEST_ORG_ID = "org_test_fixture"


def _pg_state() -> tuple[bool, bool]:
    """(postgres reachable, the Story 48.4 preset relation present)."""
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False, False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('app.tax_fee_preset_versions') IS NOT NULL")
                applied = bool(cur.fetchone()[0])
        return True, applied
    except Exception:
        return False, False


_PG_REACHABLE, _PG_HAS_PRESETS = _pg_state()

tax_fee_schema = pytest.mark.skipif(
    not (_PG_REACHABLE and _PG_HAS_PRESETS),
    reason="app.tax_fee_preset_versions is not present on TEST_POSTGRES_DSN",
)


def _new_id(prefix: str) -> str:
    """Crockford base32, 26 chars: `governance_rule_sets` CHECKs the ULID shape,
    so a hex id is refused by the database rather than by a fixture assertion."""
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}_{ULID()}"


def _slug(stem: str) -> str:
    """`governance_rule_sets.name` CHECKs ^[a-z][a-z0-9_]{0,126}$, so an id is
    not a legal name and a ULID is not a legal slug."""
    return f"{stem}_{uuid.uuid4().hex[:10]}"


def _seed_project(conn) -> str:
    project_id = _new_id("proj")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s, 'test-48-4')",
            (project_id, TEST_ORG_ID, project_id, project_id.replace("_", "-").lower()),
        )
    return project_id


def _preset_payload(**overrides):
    payload = {
        "preset_key": f"dst_fr_{uuid.uuid4().hex[:8]}",
        "label": "France DST pass-through",
        "issuer": "Direction generale des Finances publiques",
        "source_reference": "Digital services tax, headline rate table",
        "source_reference_version": "2026-01",
        "authoritative_url": "https://example.com/dst",
        "jurisdiction_kind": "country",
        "jurisdiction_code": "FR",
        "taxable_subject": "Digital advertising services supplied in France",
        "category": "REGULATORY_TAX",
        "form": "PERCENTAGE",
        "rate": "0.030000",
        "base_target": "NET_MEDIA",
        "qualifications": [
            {
                "code": "provider_passes_through",
                "question": "Does the platform pass this through on its invoice?",
            }
        ],
        "effective_from": "2026-01-01",
    }
    payload.update(overrides)
    return payload


def _jurisdiction_free_payload(**overrides):
    """An agency fee: no country governs it, so it is a candidate everywhere."""
    payload = _preset_payload(
        preset_key=f"agency_fee_{uuid.uuid4().hex[:8]}",
        label="Agency fee",
        issuer="Agency",
        source_reference="Master services agreement",
        source_reference_version="v3",
        authoritative_url=None,
        document_ref="MSA-2026-B",
        jurisdiction_kind="none",
        jurisdiction_code=None,
        taxable_subject="Media buying services",
        category="AGENCY_FEE",
        qualifications=[],
    )
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Criterion [0], reachability half: does the ACTIVATION path ask?
# ---------------------------------------------------------------------------


@tax_fee_schema
def test_project_evidence_reaches_the_preset_proposals(live_postgres):
    """The defect this pins: `propose_from_presets` was reachable only from
    `core.fee_tax_mcp`, so enabling the capability could not offer what the
    repository already knew how to propose."""
    conn = live_postgres
    project_id = _seed_project(conn)
    record_preset_version(
        conn,
        payload=_jurisdiction_free_payload(),
        org_id=TEST_ORG_ID,
        actor="owner@example.com",
        publish=True,
    )

    evidence = TaxFeesCompiler().project_evidence(
        conn, project_id=project_id, org_id=TEST_ORG_ID
    )

    assert "preset_proposals" in evidence
    proposals = evidence["preset_proposals"]
    assert proposals, "a published jurisdiction-free preset must be a candidate"
    assert proposals[0]["preset"]["issuer"] == "Agency"
    assert proposals[0]["draft_rule"]["category"] == "AGENCY_FEE"


@tax_fee_schema
def test_the_activation_verdict_offers_that_proposal_rather_than_an_empty_page(
    live_postgres,
):
    """End to end across the seam: store -> evidence -> assess. Neither half alone
    proves criterion [0], because the defect lived exactly between them."""
    from core.capability_proposals import CompileContext

    conn = live_postgres
    project_id = _seed_project(conn)
    record_preset_version(
        conn,
        payload=_jurisdiction_free_payload(),
        org_id=TEST_ORG_ID,
        actor="owner@example.com",
        publish=True,
    )
    evidence = TaxFeesCompiler().project_evidence(
        conn, project_id=project_id, org_id=TEST_ORG_ID
    )
    context = CompileContext(
        project_id=project_id,
        org_id=TEST_ORG_ID,
        capability_key="tax_fees",
        change_set_id="pcs_EXAMPLE",
        intent={"capabilities": {"tax_fees": "enabled"}},
        actor="owner@example.com",
        requested_state="enabled",
        project_evidence=evidence,
    )

    verdict = TaxFeesCompiler().assess(
        conn, context=context, datastream={"id": _new_id("dstr")}
    )

    assert [b.code for b in verdict.blockers] == ["qualified_proposal_available"]
    assert verdict.detected_support_selection["state"] == "proposed"
    # Still not a decision: nothing composes until an operator publishes a ladder.
    assert verdict.coverage_state == "unavailable"
    assert verdict.detected_support_selection["selected"] is False


@tax_fee_schema
def test_a_preset_for_a_country_this_project_does_not_govern_is_not_proposed(
    live_postgres,
):
    """Narrowing proved against the QUERY, not against a Python list. The Project
    seeded here has no Country hierarchy, so it governs no jurisdiction at all --
    and a country-scoped preset must stay out while the agency fee comes through."""
    conn = live_postgres
    project_id = _seed_project(conn)
    record_preset_version(
        conn,
        payload=_preset_payload(),
        org_id=TEST_ORG_ID,
        actor="owner@example.com",
        publish=True,
    )
    record_preset_version(
        conn,
        payload=_jurisdiction_free_payload(),
        org_id=TEST_ORG_ID,
        actor="owner@example.com",
        publish=True,
    )

    proposals = _tax_fee_preset_proposals(
        conn, project_id=project_id, org_id=TEST_ORG_ID
    )

    kinds = {p["preset"]["jurisdiction_code"] for p in proposals}
    assert "FR" not in kinds, "a French rule was offered to a Project with no France"
    assert None in kinds, "the jurisdiction-free agency fee must still be a candidate"


@tax_fee_schema
def test_an_unpublished_preset_is_never_offered(live_postgres):
    """Shared reference material is not Project policy until someone publishes it."""
    conn = live_postgres
    project_id = _seed_project(conn)
    record_preset_version(
        conn,
        payload=_jurisdiction_free_payload(),
        org_id=TEST_ORG_ID,
        actor="owner@example.com",
        publish=False,
    )

    proposals = _tax_fee_preset_proposals(
        conn, project_id=project_id, org_id=TEST_ORG_ID
    )

    assert proposals == []


# ---------------------------------------------------------------------------
# Criterion [0], the VISIBLE half: the Rule Ladder workbench must be able to
# offer what the activation path now computes.
#
# `TaxFeeLadderTabs.tsx` renders an EmptyState whose own text, written the same
# day from the other side of this defect, says: "The proposal path
# (auto_populate_tax_rules over the governed presets) has no reachable caller in
# this build: its MCP tools are not registered and no REST route exposes them."
# That was true when written. These tests are what make it false: the Rule Set
# read path serves the qualified proposals, so the screen has something to offer
# instead of naming an owner and stopping.
# ---------------------------------------------------------------------------


def _rule_set_facets(conn, project_id: str, rule_set_id: str):
    from core.governance_read_model import _rule_set_facets as facets  # noqa: PLC0415

    return facets(conn, project_id, rule_set_id)


def _seed_empty_tax_fee_rule_set(conn, project_id: str) -> str:
    """A `tax_fee` Rule Set head with NO published version -- the blank editor."""
    rule_set_id = _new_id("grs")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.governance_rule_sets "
            "(id, org_id, project_id, family, name, label, lifecycle_status, created_by) "
            "VALUES (%s, %s, %s, 'tax_fee', %s, 'Tax & fee ladder', 'draft', 'test-48-4')",
            (rule_set_id, TEST_ORG_ID, project_id, _slug("tax_fee_ladder")),
        )
    return rule_set_id


@tax_fee_schema
def test_an_empty_tax_fee_ladder_carries_its_qualified_proposals(live_postgres):
    conn = live_postgres
    project_id = _seed_project(conn)
    rule_set_id = _seed_empty_tax_fee_rule_set(conn, project_id)
    record_preset_version(
        conn,
        payload=_jurisdiction_free_payload(),
        org_id=TEST_ORG_ID,
        actor="owner@example.com",
        publish=True,
    )

    facets = _rule_set_facets(conn, project_id, rule_set_id)

    assert facets["ordered_rules"] == []
    proposals = facets["preset_proposals"]
    assert proposals, "an empty ladder with a matching preset must offer it"
    assert proposals[0]["preset"]["issuer"] == "Agency"
    # The operator must see WHY and WHAT is unproven, not just a count.
    assert "why_it_may_apply" in proposals[0]
    assert "draft_rule" in proposals[0]


@tax_fee_schema
def test_a_ladder_that_carries_rules_is_offered_nothing(live_postgres):
    """Once a Project has decided, proposals are noise. Bounded payload (AC10):
    the expensive read does not run on the common path at all."""
    conn = live_postgres
    project_id = _seed_project(conn)
    rule_set_id = _seed_empty_tax_fee_rule_set(conn, project_id)
    record_preset_version(
        conn,
        payload=_jurisdiction_free_payload(),
        org_id=TEST_ORG_ID,
        actor="owner@example.com",
        publish=True,
    )
    with conn.cursor() as cur:
        version_id = _new_id("grsv")
        cur.execute(
            # Every NOT NULL column without a default, named once rather than
            # discovered one error at a time.
            "INSERT INTO app.governance_rule_set_versions "
            "(id, rule_set_id, project_id, version_number, status, family, profile, "
            " label, ordered_rules, payload, content_hash, created_by) "
            "VALUES (%s, %s, %s, 1, 'published', 'tax_fee', 'tax_fee_ladder', "
            "        'Tax & fee ladder', %s::jsonb, '{}'::jsonb, %s, 'test-48-4')",
            (version_id, rule_set_id, project_id, '[{"rule_key": "agency_fee"}]', "a" * 64),
        )
        cur.execute(
            "UPDATE app.governance_rule_sets SET current_version_id = %s WHERE id = %s",
            (version_id, rule_set_id),
        )

    facets = _rule_set_facets(conn, project_id, rule_set_id)

    assert facets["ordered_rules"]
    assert facets["preset_proposals"] == []


@tax_fee_schema
def test_a_non_tax_family_is_never_offered_tax_presets(live_postgres):
    """The Rule Set lifecycle is family-opaque and must stay that way: a Money
    Policy head asking for tax proposals would be the leak this profile forbids."""
    conn = live_postgres
    project_id = _seed_project(conn)
    record_preset_version(
        conn,
        payload=_jurisdiction_free_payload(),
        org_id=TEST_ORG_ID,
        actor="owner@example.com",
        publish=True,
    )
    rule_set_id = _new_id("grs")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.governance_rule_sets "
            "(id, org_id, project_id, family, name, label, lifecycle_status, created_by) "
            "VALUES (%s, %s, %s, 'money_policy', %s, 'Money policy', 'draft', 'test-48-4')",
            (rule_set_id, TEST_ORG_ID, project_id, _slug("money_policy")),
        )

    facets = _rule_set_facets(conn, project_id, rule_set_id)

    assert facets["preset_proposals"] == []


# ---------------------------------------------------------------------------
# The MCP half of the ratified target, served GENERICALLY.
#
# `capabilities/tax-fees.md`'s Required-view-coverage table asks MCP for
# "Inspect, auto-propose, prepare and human-confirm rule changes". Three of the
# four are already served by the generic capability tools registered in
# `project_capabilities_mcp`: read / prepare / confirm (the last with
# `confirmation_mode="human"`).
#
# AUTO-PROPOSE WAS SERVED BY NOBODY. Its only implementation was
# `fee_tax_mcp.auto_populate_tax_rules`, in a module the 41.6 cutover removed from
# `main.py` -- so the verb the target requires was silently uncovered, and the
# module that once carried it looked like dead weight. That is the whole reason
# this surface reads as mount-then-dismantle churn.
#
# It arrives here as ONE BOUNDED BLOCK on the generic read, exactly as Story 48.3
# put money and time evidence there. `_foundation_summary`'s own docstring states
# the rule: "AC10 keeps this on the GENERIC tool ... rather than a
# `read_money_policy` sibling that would immediately be a second place to ask the
# same question."
# ---------------------------------------------------------------------------


@tax_fee_schema
def test_the_generic_capability_read_carries_the_tax_proposals(live_postgres):
    from core.project_capabilities_mcp import _foundation_summary  # noqa: PLC0415

    conn = live_postgres
    project_id = _seed_project(conn)
    record_preset_version(
        conn,
        payload=_jurisdiction_free_payload(),
        org_id=TEST_ORG_ID,
        actor="owner@example.com",
        publish=True,
    )

    block = _foundation_summary(conn, project_id, "tax_fees")

    assert block["capability"] == "tax_fees"
    assert block["proposal_count"] == 1
    assert block["headline"]
    [proposal] = block["preset_proposals"]
    assert proposal["preset"]["issuer"] == "Agency"
    assert "operator_must_confirm" in proposal


@tax_fee_schema
def test_that_block_says_so_when_there_is_nothing_to_propose(live_postgres):
    """A gap code, not an empty list read as 'no fee applies here'."""
    from core.project_capabilities_mcp import _foundation_summary  # noqa: PLC0415

    conn = live_postgres
    project_id = _seed_project(conn)

    block = _foundation_summary(conn, project_id, "tax_fees")

    assert block["proposal_count"] == 0
    assert block["gap_code"] == "no_qualified_tax_proposal"


@tax_fee_schema
def test_the_block_is_bounded(live_postgres):
    """AC10: model-visible content stays bounded. A Project in an organization with
    fifty published presets must not put fifty of them in a model's context."""
    from core.project_capabilities_mcp import (
        _MAX_TAX_PROPOSALS,  # noqa: PLC0415
        _foundation_summary,  # noqa: PLC0415
    )

    conn = live_postgres
    project_id = _seed_project(conn)
    for _ in range(_MAX_TAX_PROPOSALS + 3):
        record_preset_version(
            conn,
            payload=_jurisdiction_free_payload(),
            org_id=TEST_ORG_ID,
            actor="owner@example.com",
            publish=True,
        )

    block = _foundation_summary(conn, project_id, "tax_fees")

    assert len(block["preset_proposals"]) == _MAX_TAX_PROPOSALS
    # And the count is the TRUE one, so a reader is never told the cap is the total.
    assert block["proposal_count"] > _MAX_TAX_PROPOSALS


@tax_fee_schema
def test_no_preset_at_all_leaves_the_blank_editor_in_place(live_postgres):
    """The other half of the conditional, on a real database: with nothing to
    propose, saying so is correct. Inventing a proposal would be the worse defect."""
    conn = live_postgres
    project_id = _seed_project(conn)

    proposals = _tax_fee_preset_proposals(
        conn, project_id=project_id, org_id=_new_id("org")
    )

    assert proposals == []
