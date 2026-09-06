"""Story 72.4 -- the platform Chart Template catalogue. The CODE is the catalogue.

WHAT THIS OWNS. The Chart Templates this product SHIPS, declared here, in code,
and the derivation that produces them from the ten `CardTemplate` of
`core.cards`. It owns no database access: `app.visualization_templates` and
`app.visualization_template_versions` are the PROJECTION of this module, written
by `scripts/register_visualization_template_seeds.py`, exactly as
`app.renderer_runtime_builds` is the projection of the shipped renderer registry.

WHY IT IS CODE AND NOT A TABLE. `CLAUDE.md`: *"Ce qui est du code reste du code.
Un catalogue livré avec le produit ne se lit pas dans une table."* And the
ratified invariant of the epic says the same thing with its consequence attached:
*"Une table qu'un connecteur pourrait écrire serait de la métadonnée de
présentation exécutable, et AD-2 l'interdit."* A row is a projection of this
file; it is never the authority for what this deployment ships.

D2, RATIFIED 2026-08-31. The ten `CardTemplate` (`core/cards.py:305`) carry the
typed selection that works -- the question each answers, the metrics and
dimensions each demands, its fallback rank -- and a `widget_uri` that names a
widget drawing itself, which is the second engine AD-2 closes. The decision is
form *(b)*: PROJECT them. The question, the requirements and the rank cross over;
the `widget_uri` does not. The visual family of the SHIPPED registry replaces it,
or the card is not projectable and is NAMED as such (AC14). The ten
`CardTemplate` stay exactly where they are for their current consumers
(`list_card_templates`, `get_card`, `answerable_topics`): this projection ADDS.

---------------------------------------------------------------------------
THE DERIVATION, RULE BY RULE. Nothing below is typed; every value is read off a
`CardTemplate` or off the shipped family registry.
---------------------------------------------------------------------------

1. THE QUESTION crosses unchanged. `card.answers_question` becomes the
   document's `answers_question`, which the grammar of 72.2 declares as a
   `label` leaf. It is the sentence the list screen states and the workbench
   Overview shows.

2. `requires` IS WHAT THE CARD DEMANDED, and only that.
   `required_metrics` becomes `requires.measure.min`; `required_dimensions`
   becomes `requires.dimension.min`. `ANY_METRIC` ("*") counts as one measure,
   because that is what it means: at least one canonical measure.

   `optional_metrics` and `optional_dimensions` DO NOT CROSS, and the reason is
   measured rather than stylistic: on the card path those tuples are a hint for
   a resolver, not a typed declaration -- `conversions` lists `cost` among its
   optional DIMENSIONS and `journey` lists `active_users`, and both are
   measures. Deriving a `max` from them would bound a governed predicate with a
   card-path imprecision. A well left without `max`, `accepts` or
   `max_cardinality` means "whatever this family's own well allows", which is
   the honest statement of what the card said.

3. THE FAMILY IS THE CARD'S OWN INTENT, TRANSLATED THROUGH THE SHIPPED
   REGISTRY. Candidates are read off `card.composition` IN ORDER -- the order in
   which the card declares how it wants to be shown -- and each block type is
   matched against `SPEC_SELECTABLE_FAMILY_IDS`, with one named alias
   (`kpi_row` -> `kpi`). The first candidate whose wells the card's requirements
   satisfy wins. No other family is ever considered: a family the card does not
   name is a "neighbouring family", and the ratified criterion forbids
   substituting one.

4. THE FIT IS JUDGED BY THE VALIDATOR OF 72.2, NOT BY A SECOND RULE.
   `validate_template_document` already refuses a template that requires a well
   its family has not, more members than the well holds, or that fails to
   require a well the family needs. Re-implementing those five verdicts here
   would be a second compatibility authority, and the second one drifts. So a
   candidate is TRIED, and its refusals are read.

   The refusal CODES are what separate the two outcomes, and they must never
   blur:
     * a fit code (`_FIT_REFUSAL_CODES`) means this family cannot hold this
       card -- the next candidate is tried, and if none is left the card is
       reported NOT PROJECTABLE with every reason (AC14);
     * any other code means the catalogue itself is malformed -- a question over
       the label bound, a contract literal that moved -- and that is a defect of
       this file. It raises `SeedCatalogueDefect`, which the projection script
       turns into a RED. A card whose document does not pass the grammar is
       never skipped in silence.

5. THE RANK STAYS IN THE CODE, deliberately, and there is no column for it.
   `fallback_rank` orders `PLATFORM_SEEDS` and reaches the model through this
   module (72.7 serves "id, question, family, rank" from the catalogue). Storing
   it would put a second ranking authority in a table, when the ratified
   sentence is that the deterministic rules are authoritative and live in the
   code -- and `CLAUDE.md` adds the general form: *"une valeur dérivable ne se
   stocke pas"*.

---------------------------------------------------------------------------
WHY THERE IS NO EMITTED JSON MANIFEST, unlike the renderer ledger.
---------------------------------------------------------------------------
`scripts/register_renderer_builds.py` reads a COMMITTED manifest
(`rendererBuilds.generated.json`) because its catalogue is TypeScript and the
machine that deploys has no node toolchain. That is a language boundary, not a
part of the pattern. This catalogue is Python, and the projection script imports
it directly: the manifest IS this module, emitted by the interpreter from the
declarations above, never typed by a hand. A committed copy would be a second
place for the same fact to live, and it would drift the first time a card moved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.cards import ANY_METRIC, CARD_TEMPLATES, CardTemplate
from core.visualization_families import SPEC_SELECTABLE_FAMILY_IDS
from core.visualization_templates import (
    CHART_TEMPLATE_CONTRACT_VERSION,
    CHART_TEMPLATE_SCHEMA_VERSION,
    VisualizationRefusal,
    VisualizationSpecRefused,
    validate_template_document,
)

__all__ = [
    "NOT_PROJECTABLE",
    "PLATFORM_SEEDS",
    "PLATFORM_SEED_AUTHOR",
    "PLATFORM_SEED_ORIGIN",
    "PLATFORM_SEED_PROPOSED_BY",
    "PlatformSeed",
    "SeedCatalogueDefect",
    "UnprojectableCard",
    "catalogue_payload",
    "seed_head_id",
    "seed_version_id",
]

#: The `seed_origin` a projected platform template carries. Migration 333 admits
#: three values and this is the one the ratified amendment names for "platform".
#: Note what it forbids: `ck_visualization_templates_seed_pair` allows
#: `seed_module_name`/`seed_template_id` ONLY for `connector_seed`, so a platform
#: seed is identified by its id and by nothing else -- which is why the id below
#: is derived and stable rather than a fresh ULID.
PLATFORM_SEED_ORIGIN = "platform_seed"

#: `created_by` on both rows. Not a person and not a service account: the author
#: of a platform seed is the deployment that shipped it.
PLATFORM_SEED_AUTHOR = "platform"

#: `proposed_by` on the version. Migration 333 admits `person` or `model`. This
#: catalogue is written by hand, in code, reviewed like code -- so `person`. A
#: model never proposed one of these.
PLATFORM_SEED_PROPOSED_BY = "person"

#: Block types a card composes that are NOT a family id but name one. The single
#: entry is the card path's own word for a KPI row. Anything else must MATCH a
#: shipped family id exactly: an alias table is where a "neighbouring family"
#: would sneak in.
_BLOCK_FAMILY_ALIASES: dict[str, str] = {"kpi_row": "kpi"}

#: Refusal codes that mean "this family cannot hold this card", as opposed to
#: "this catalogue is malformed". Every one of them is produced by
#: `_check_requires_against_family` or by the family verdict of
#: `core.visualization_templates`; read from there rather than guessed.
_FIT_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "missing_requirement",
        "role_mismatch",
        "role_unavailable",
        "too_many_members",
        "cardinality_over_limit",
        "family_not_drawn",
        "family_not_fillable",
    }
)


class SeedCatalogueDefect(RuntimeError):
    """A declared seed produced a document the grammar of 72.2 refuses.

    Not a projectability verdict: this is a defect of THIS FILE, and the
    projection script exits non-zero on it. Raised rather than logged, because a
    catalogue that silently drops one of its own entries is the failure the
    ratified criterion names -- "omis sans être nommé".
    """


@dataclass(frozen=True)
class PlatformSeed:
    """One Chart Template this deployment ships, proven legal before it exists."""

    #: The `CardTemplate` id it was projected from. Stable across deployments,
    #: and the only thing that identifies a platform seed in the table.
    seed_id: str
    #: The head's `label`, in the words of the card.
    label: str
    #: The question it declares it answers. Also inside the document, where the
    #: grammar puts it; the head carries no second copy.
    answers_question: str
    family: str
    #: The card path's deterministic rank. Lives here, not in a column.
    fallback_rank: int
    #: The NORMALIZED document -- what `validate_template_document` returned, so
    #: the projection stores exactly what the validator accepted.
    document: dict[str, Any]
    content_hash: str
    #: `{well: {min, max, accepts, max_cardinality}}`, carried so a caller does
    #: not re-read the document to state the predicates.
    requires: dict[str, Any]


@dataclass(frozen=True)
class UnprojectableCard:
    """A `CardTemplate` the shipped registry cannot draw, and every reason why.

    AC14. Named, one line per family the card itself proposed, never rendered by
    an approximate family and never omitted.
    """

    card_id: str
    title: str
    #: One sentence per candidate the card named, in the card's own order.
    reasons: tuple[str, ...]


def seed_head_id(seed_id: str, project_id: str) -> str:
    """The head id of one platform seed in one Project. Derived, never random.

    A platform seed may not carry `seed_module_name`/`seed_template_id` -- the
    CHECK of migration 333 reserves that pair for `connector_seed` -- so the id
    is the only thing that says WHICH seed a row is. Deriving it is what makes
    the projection idempotent and makes `--check` able to answer in both
    directions without parsing a document.
    """
    return f"vtpl_pseed_{seed_id}__{project_id}"


def seed_version_id(seed_id: str, project_id: str, version_number: int) -> str:
    """The version id of one platform seed version. Derived for the same reason."""
    return f"vtv_pseed_{seed_id}__{project_id}__v{version_number}"


# ---------------------------------------------------------------------------
# The derivation.
# ---------------------------------------------------------------------------


def _required_measures(card: CardTemplate) -> int:
    """How many measures this card demands. `ANY_METRIC` means "at least one"."""
    named = set(card.required_metrics) - {ANY_METRIC}
    if ANY_METRIC in card.required_metrics:
        return max(len(named), 1)
    return len(named)


def _requires_block(card: CardTemplate) -> dict[str, Any]:
    """`requires`, from what the card demanded and from nothing else.

    A well the card did not demand is ABSENT, which is the grammar's way of
    saying "not required" -- there is no "require zero of these". `max`,
    `accepts` and `max_cardinality` are left out on purpose: see rule 2 of the
    module docstring.
    """
    requires: dict[str, Any] = {}
    measures = _required_measures(card)
    if measures:
        requires["measure"] = {"min": measures}
    if card.required_dimensions:
        requires["dimension"] = {"min": len(card.required_dimensions)}
    return requires


def _candidate_families(card: CardTemplate) -> tuple[str, ...]:
    """The families the CARD ITSELF names, in the order it declares them.

    Deduplicated while keeping first appearance: a card that composes three
    tables proposes `table` once. A block type that is neither a shipped family
    nor the one alias contributes nothing -- and the caller reports it by name,
    so "the funnel this card leads with is not a family this deployment draws"
    is a sentence a reader gets rather than a silence.
    """
    seen: list[str] = []
    for block in card.composition or ():
        block_type = str((block or {}).get("type") or "")
        family = _BLOCK_FAMILY_ALIASES.get(block_type, block_type)
        if family in SPEC_SELECTABLE_FAMILY_IDS and family not in seen:
            seen.append(family)
    return tuple(seen)


def _undrawable_block_types(card: CardTemplate) -> tuple[str, ...]:
    """Block types this card composes that name no family this deployment draws.

    `comment` is excluded: it is prose attached to a card, never a drawing, so
    naming it as an undrawn family would be a false absence.
    """
    seen: list[str] = []
    for block in card.composition or ():
        block_type = str((block or {}).get("type") or "")
        if not block_type or block_type == "comment":
            continue
        family = _BLOCK_FAMILY_ALIASES.get(block_type, block_type)
        if family not in SPEC_SELECTABLE_FAMILY_IDS and block_type not in seen:
            seen.append(block_type)
    return tuple(seen)


def _document(card: CardTemplate, family: str, requires: Mapping[str, Any]) -> dict[str, Any]:
    """The proposed document. Identity keys imported, never transcribed."""
    return {
        "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
        "schema_version": CHART_TEMPLATE_SCHEMA_VERSION,
        "family": family,
        "answers_question": card.answers_question,
        "requires": dict(requires),
    }


def _refusal_line(family: str, refusals: Sequence[VisualizationRefusal]) -> str:
    """One sentence naming the family and why it cannot hold this card."""
    reasons = "; ".join(refusal.message for refusal in refusals)
    return f"`{family}`: {reasons}"


def _project_card(card: CardTemplate) -> PlatformSeed | UnprojectableCard:
    """One `CardTemplate`, projected -- or named, with every reason it was not."""
    requires = _requires_block(card)
    reasons: list[str] = []

    for block_type in _undrawable_block_types(card):
        reasons.append(
            f"`{block_type}`: not a visual family this deployment declares, so the "
            f"card's own presentation for that block cannot be projected"
        )

    for family in _candidate_families(card):
        document = _document(card, family, requires)
        try:
            validated = validate_template_document(document)
        except VisualizationSpecRefused as refused:
            fit = [r for r in refused.refusals if r.code in _FIT_REFUSAL_CODES]
            other = [r for r in refused.refusals if r.code not in _FIT_REFUSAL_CODES]
            if other:
                #  Not a projectability verdict: the catalogue itself is wrong.
                #  Raised so the projection script goes RED rather than quietly
                #  shipping nine seeds where ten were declared.
                detail = "; ".join(f"{r.subject or '/'} {r.message}" for r in other)
                raise SeedCatalogueDefect(
                    f"the seed derived from card `{card.id}` for family `{family}` is not a "
                    f"legal Chart Template document: {detail}"
                ) from refused
            reasons.append(_refusal_line(family, fit))
            continue

        return PlatformSeed(
            seed_id=card.id,
            label=card.title,
            answers_question=validated.answers_question,
            family=validated.family,
            fallback_rank=card.fallback_rank,
            document=validated.document,
            content_hash=validated.content_hash,
            requires=validated.requires,
        )

    if not reasons:
        #  A card composing nothing at all. No such card exists today; named
        #  rather than left to produce an empty explanation.
        reasons.append(
            "this card composes no block, so it proposes no family to project it onto"
        )
    return UnprojectableCard(card_id=card.id, title=card.title, reasons=tuple(reasons))


def _derive() -> tuple[tuple[PlatformSeed, ...], tuple[UnprojectableCard, ...]]:
    seeds: list[PlatformSeed] = []
    unprojectable: list[UnprojectableCard] = []
    for card in CARD_TEMPLATES:
        outcome = _project_card(card)
        if isinstance(outcome, PlatformSeed):
            seeds.append(outcome)
        else:
            unprojectable.append(outcome)
    #  Ordered by the card path's own rank, highest first, then by id so two
    #  cards at the same rank never depend on a coin toss.
    seeds.sort(key=lambda seed: (-seed.fallback_rank, seed.seed_id))
    return tuple(seeds), tuple(unprojectable)


#: THE catalogue. Derived at import: a defect in it stops the module rather than
#: producing a projection that ships fewer templates than it declares.
PLATFORM_SEEDS, NOT_PROJECTABLE = _derive()

#: `{seed_id: PlatformSeed}` -- the lookup the projection and `--check` use to
#: answer "does the code declare this row?".
SEEDS_BY_ID: dict[str, PlatformSeed] = {seed.seed_id: seed for seed in PLATFORM_SEEDS}


def catalogue_payload() -> dict[str, Any]:
    """The whole catalogue as a reader receives it -- what a manifest would hold.

    Emitted from the declarations above, never typed. This is the shape the
    projection script prints and the shape a later surface serves; there is no
    committed copy of it, for the reason stated in the module docstring.
    """
    return {
        "contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
        "schema_version": CHART_TEMPLATE_SCHEMA_VERSION,
        "seed_origin": PLATFORM_SEED_ORIGIN,
        "seeds": [
            {
                "seed_id": seed.seed_id,
                "label": seed.label,
                "answers_question": seed.answers_question,
                "family": seed.family,
                "fallback_rank": seed.fallback_rank,
                "content_hash": seed.content_hash,
                "requires": seed.requires,
            }
            for seed in PLATFORM_SEEDS
        ],
        "not_projectable": [
            {"card_id": card.card_id, "title": card.title, "reasons": list(card.reasons)}
            for card in NOT_PROJECTABLE
        ],
    }
