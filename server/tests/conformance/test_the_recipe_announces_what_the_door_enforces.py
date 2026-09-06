"""The recipe may not promise a contract the publish gate does not enforce (AI-274).

TWO PLACES SAID THE SAME THING AND HAD ALREADY DRIFTED. `daily_insights_recipe`
tells the agent its rules; `daily_insights_schema` is what `_validate_payload`
actually applies. Story 53.4 hardened the evidence obligation -- an insight must
cite at least one server-measured fact, and a ref is `<kind>:<id>` with `kind`
from a closed set that deliberately excludes the card template -- and the recipe
never mentioned it. An agent following the recipe to the letter was refused by
the door, with a rule it had never been told.

This is the shape story 60.2 removed one workspace over, where a dialog offered
operations the server had never had. The repair is the same and so is the guard:
the announced text DERIVES from the applied contract, and this file is what
notices when a bound moves in one place only.

IT READS THE RENDERED RULES, not the source. A guard asserting that
`_announced_rules` calls `schema.MAX_INSIGHTS_PER_DAY` would pass on a function
that computed the value and then printed something else; asserting that the
SENTENCE carries the number is what a reader of the recipe actually gets.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from core import daily_insights_schema as schema  # noqa: E402
from core.daily_insights_recipe import build_task_recipe  # noqa: E402


def _rules() -> list[str]:
    recipe = build_task_recipe(
        project_id="proj_EXAMPLE",
        timezone="Europe/Paris",
        hour_local=7,
        contract_version="v1",
    )
    return list(recipe["rules"])


def test_the_recipe_states_the_evidence_obligation() -> None:
    """The rule 53.4 added to the door and the recipe did not carry."""
    text = " ".join(_rules())
    assert "evidenceRefs" in text, (
        "the recipe never names the field the gate requires; an agent following "
        "it is refused by a rule it was not told"
    )


def test_the_recipe_names_every_evidence_kind_the_door_accepts() -> None:
    text = " ".join(_rules())
    for kind in schema.EVIDENCE_KINDS:
        assert f"{kind}:" in text, (
            f"the door accepts `{kind}:<id>` and the recipe does not mention it"
        )


def test_the_recipe_does_not_offer_a_kind_the_door_refuses() -> None:
    """`card:<template>` was a third kind and was removed for being a tautology.

    Offering it again would make the requirement satisfiable by citing the form
    that displays the number instead of the number.
    """
    text = " ".join(_rules())
    assert "card:" not in text
    assert "card template" in text or "template you render" in text, (
        "the recipe should say that the template is NOT evidence -- the mistake "
        "the removed kind invited"
    )


def test_the_daily_slot_bound_is_the_schema_s_own() -> None:
    """A number copied by hand is a number that drifts. This one is derived."""
    text = " ".join(_rules())
    assert f"0..{schema.MAX_INSIGHTS_PER_DAY}" in text, text


def test_the_card_modes_named_are_the_schema_s_own() -> None:
    text = " ".join(_rules())
    assert f"'{schema.CARD_MODE_TEMPLATE}'" in text
    assert f"'{schema.CARD_MODE_COMPOSE}'" in text
    # And the disabled one is named as disabled, not merely absent: an agent
    # that never hears of `compose` will invent it.
    assert "disabled" in text
