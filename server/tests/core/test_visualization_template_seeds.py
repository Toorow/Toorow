"""Story 72.4 -- the shipped Chart Template catalogue, judged offline.

WHAT IS PROVED HERE. That the code IS the catalogue: every one of the ten
`CardTemplate` is accounted for, every seed the catalogue declares is a document
the grammar of 72.2 accepts, every seed carries the requirements the card it came
from demanded, and nothing that AD-2 closes crosses over. The projection into
PostgreSQL is proved in `test_template_seed_projection_pg.py`; nothing here
touches a database.

WHY IT IS A GUARD AND NOT A FIXTURE COMPARISON. The catalogue is DERIVED at
import: change a `CardTemplate`, change the family registry, change the template
grammar, and these documents change with them. A committed expected-document
would be a second declaration of the same fact and would have to be re-typed on
every legitimate move. What is pinned instead is the RULE -- ten cards in, ten
accounted for, every derived value read back to the card it came from.
"""

from __future__ import annotations

import pytest
from core import visualization_template_seeds as seeds
from core.cards import ANY_METRIC, CARD_TEMPLATES, CardTemplate
from core.visualization_families import SPEC_SELECTABLE_FAMILY_IDS
from core.visualization_templates import (
    CHART_TEMPLATE_CONTRACT_VERSION,
    CHART_TEMPLATE_SCHEMA_VERSION,
    validate_template_document,
)


def _card(card_id: str) -> CardTemplate:
    match = [card for card in CARD_TEMPLATES if card.id == card_id]
    assert match, f"no CardTemplate named {card_id!r}"
    return match[0]


# ---------------------------------------------------------------------------
# AC13 / AC14. Ten cards in; ten accounted for, each in exactly one column.
# ---------------------------------------------------------------------------


def test_every_card_template_is_either_projected_or_named():
    """No card is omitted in silence -- the ratified criterion's own words."""
    accounted = {seed.seed_id for seed in seeds.PLATFORM_SEEDS} | {
        card.card_id for card in seeds.NOT_PROJECTABLE
    }
    assert accounted == {card.id for card in CARD_TEMPLATES}
    #  And never both: a card is a seed or it is named, and a set union would
    #  have hidden a duplicate.
    assert len(seeds.PLATFORM_SEEDS) + len(seeds.NOT_PROJECTABLE) == len(CARD_TEMPLATES)


def test_an_unprojectable_card_is_named_with_at_least_one_reason():
    """AC14. Listed by its name, with why -- never a bare absence."""
    assert seeds.NOT_PROJECTABLE, "the measured catalogue has cards the registry cannot draw"
    for card in seeds.NOT_PROJECTABLE:
        assert card.title
        assert card.reasons, f"{card.card_id} is named as unprojectable with no reason"
        for reason in card.reasons:
            assert reason.strip()


def test_a_card_is_never_projected_onto_a_family_it_did_not_name():
    """No neighbouring family is substituted. The candidate comes from the card."""
    for seed in seeds.PLATFORM_SEEDS:
        card = _card(seed.seed_id)
        composed = {
            seeds._BLOCK_FAMILY_ALIASES.get(str(block.get("type")), str(block.get("type")))
            for block in card.composition or ()
        }
        assert seed.family in composed, (
            f"{seed.seed_id} was projected onto `{seed.family}`, which its own "
            f"composition never names"
        )
        assert seed.family in SPEC_SELECTABLE_FAMILY_IDS


# ---------------------------------------------------------------------------
# The grammar guard on every declared document.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", seeds.PLATFORM_SEEDS, ids=lambda s: s.seed_id)
def test_a_declared_seed_is_a_legal_chart_template_document(seed):
    """The validator of 72.2 accepts it, and re-accepting it is a fixed point.

    Re-validating the NORMALIZED document must return the same hash: that is
    what makes the projection idempotent, and what makes a stored version
    comparable to the code by its `content_hash` alone.
    """
    revalidated = validate_template_document(seed.document)
    assert revalidated.content_hash == seed.content_hash
    assert revalidated.document == seed.document
    assert revalidated.family == seed.family
    assert seed.document["spec_contract_version"] == CHART_TEMPLATE_CONTRACT_VERSION
    assert seed.document["schema_version"] == CHART_TEMPLATE_SCHEMA_VERSION


@pytest.mark.parametrize("seed", seeds.PLATFORM_SEEDS, ids=lambda s: s.seed_id)
def test_a_declared_seed_names_no_data_and_no_widget(seed):
    """AD-2 and the unbound rule, checked on the serialized document.

    `widget_uri` is the one thing the ratified decision says does NOT cross: it
    names a widget that draws itself, which is the second engine AD-2 closes.
    """
    flat = repr(seed.document)
    for forbidden in ("widget_uri", "bindings", "member_id", "result_id", "query_spec"):
        assert forbidden not in flat, f"{seed.seed_id} carries `{forbidden}`"


# ---------------------------------------------------------------------------
# What the card demanded is what the template requires.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", seeds.PLATFORM_SEEDS, ids=lambda s: s.seed_id)
def test_the_requirements_of_the_card_cross_over_unchanged(seed):
    card = _card(seed.seed_id)

    named_metrics = set(card.required_metrics) - {ANY_METRIC}
    expected_measures = (
        max(len(named_metrics), 1) if ANY_METRIC in card.required_metrics else len(named_metrics)
    )
    assert seed.requires["measure"]["min"] == expected_measures

    if card.required_dimensions:
        assert seed.requires["dimension"]["min"] == len(card.required_dimensions)
    else:
        assert "dimension" not in seed.requires

    #  A well the card did not demand is ABSENT: the grammar has no "require zero
    #  of these", and inventing one would make the template stricter than the
    #  card it was projected from.
    assert set(seed.requires) <= {"measure", "dimension"}


@pytest.mark.parametrize("seed", seeds.PLATFORM_SEEDS, ids=lambda s: s.seed_id)
def test_the_question_and_the_rank_cross_over_unchanged(seed):
    card = _card(seed.seed_id)
    assert seed.answers_question == card.answers_question
    assert seed.document["answers_question"] == card.answers_question
    assert seed.fallback_rank == card.fallback_rank
    assert seed.label == card.title


def test_the_catalogue_is_ordered_by_the_rank_it_carries():
    """The rank lives in the code, and it is what orders the catalogue.

    There is no column for it: storing a rank would put a second ranking
    authority in a table, when the ratified sentence is that the deterministic
    rules are authoritative and live in the code.
    """
    ordered = [(-seed.fallback_rank, seed.seed_id) for seed in seeds.PLATFORM_SEEDS]
    assert ordered == sorted(ordered)


# ---------------------------------------------------------------------------
# A malformed declaration is a RED, never a skip.
# ---------------------------------------------------------------------------


def test_a_declaration_the_grammar_refuses_raises_rather_than_being_dropped():
    """Requirement of the story: never a silent skip.

    A question longer than the label bound is not a projectability verdict -- no
    family would fix it -- so it must stop the catalogue rather than quietly
    remove one entry from it.
    """
    broken = CardTemplate(
        id="broken_seed",
        title="Broken",
        answers_question="x" * 400,
        widget_uri="ui://example/broken",
        required_metrics=("clicks",),
        composition=({"type": "kpi_row", "binding": {"metrics": "*"}},),
    )
    with pytest.raises(seeds.SeedCatalogueDefect) as raised:
        seeds._project_card(broken)
    assert "broken_seed" in str(raised.value)
    assert "answers_question" in str(raised.value)


def test_a_card_no_family_can_hold_is_named_and_does_not_raise():
    """The other half of the same fork: a fit failure is a verdict, not a defect."""
    unfittable = CardTemplate(
        id="two_measures_no_dimension",
        title="Two measures, no dimension",
        answers_question="How do two measures move together?",
        widget_uri="ui://example/unfittable",
        required_metrics=("sessions", "conversions"),
        required_dimensions=(),
        composition=({"type": "kpi_row", "binding": {"metrics": "*"}},),
    )
    outcome = seeds._project_card(unfittable)
    assert isinstance(outcome, seeds.UnprojectableCard)
    assert any("kpi" in reason for reason in outcome.reasons)


# ---------------------------------------------------------------------------
# The derived identities the projection depends on.
# ---------------------------------------------------------------------------


def test_seed_identities_are_derived_and_stable():
    """A platform seed is identified by its id and by nothing else.

    `ck_visualization_templates_seed_pair` reserves `seed_module_name` /
    `seed_template_id` for `connector_seed`, so a random ULID here would make the
    projection unable to answer "is this row one of mine?" -- and `--check` could
    never speak in the code-to-table direction.
    """
    first = seeds.seed_head_id("kpi", "proj_EXAMPLE")
    assert first == seeds.seed_head_id("kpi", "proj_EXAMPLE")
    assert first != seeds.seed_head_id("kpi", "proj_OTHER")
    assert first != seeds.seed_head_id("videos", "proj_EXAMPLE")

    version = seeds.seed_version_id("kpi", "proj_EXAMPLE", 2)
    assert version == seeds.seed_version_id("kpi", "proj_EXAMPLE", 2)
    assert version != seeds.seed_version_id("kpi", "proj_EXAMPLE", 3)
    #  `app.is_exact_pin` refuses six placeholder words; a derived id is never one.
    assert version.lower() not in {"legacy", "current", "deferred", "latest", "unknown", "none"}


def test_the_catalogue_payload_names_both_columns():
    payload = seeds.catalogue_payload()
    assert payload["seed_origin"] == "platform_seed"
    assert len(payload["seeds"]) == len(seeds.PLATFORM_SEEDS)
    assert len(payload["not_projectable"]) == len(seeds.NOT_PROJECTABLE)
    assert payload["contract_version"] == CHART_TEMPLATE_CONTRACT_VERSION
