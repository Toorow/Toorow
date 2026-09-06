"""Story 72.7 -- the bounded block of Chart Templates an Analyze answer carries.

WHAT THIS FILE IS FOR, IN ONE PARAGRAPH.
`docs/product-architecture/visualization-and-rendering.md` ("A model chooses a
template by its typed metadata"): for a given Result the model reads the
templates whose declared question and required roles are COMPATIBLE, chooses
one, and the server materialises a project-owned Visualization Spec version.
This module composes the reading half of that sentence -- the choosing half is
`render_analyze_result`'s `visualization_template_version_id` parameter, and the
materialising half is `core.template_materialization`. It adds NO tool: D3 of
epic 72 settled that the assembled catalog stays at its budget, so the catalogue
of templates is a bounded FIELD of an answer the model already asked for
(`mcp-tool-surface.md`: "une description d'outil est du budget, pas de la
documentation" -- and neither is a second tool).

THREE BOUNDS, AND EACH ONE IS THE REASON A LINE HERE EXISTS.

1. **Bounded.** At most `MAX_OFFERED_TEMPLATES` entries, and the count of what is
   not shown travels with them -- the rule `model_channel.bounded_head` states
   for every bounded list in this channel. `attach_offer` then trims further if
   the answer it joins is close to the 4 KiB model-channel ceiling: an answer
   that fit before this block existed must still fit, and a template block is
   never worth refusing an answer over.
2. **Scoped to the Project.** Every row comes from `list_chart_templates`, which
   is scoped `org_id + project_id` and drops archived heads. Nothing here widens
   that scope, and a template of another Project reads exactly like one that does
   not exist.
3. **No Result data.** Four fields per entry -- the version id to pass back, the
   question the template declares it answers, its visual family, and its rank.
   Not one value, column name or row of the Result enters. The Result is the
   thing being JUDGED here; what it carries is the answer's own business.

WHAT `rank` IS, AND WHAT IT IS NOT. It is a POSITION in a deterministic order --
the order the Project's own Chart Template list already carries -- and never a
score, a fit or a preference. Every entry in the block is compatible; the
deterministic verdict decided that, and `visualization-and-rendering.md` is
explicit that a model "may rank but never override" it. So the model is free to
re-order these entries by what the person asked; it cannot add one, and asking to
render a template that is not here is refused by name rather than approximated.
"""

from __future__ import annotations

from typing import Any

from core.chart_template_store import list_chart_templates
from core.model_channel import MODEL_CHANNEL_MAX_BYTES, serialized_bytes
from core.template_compatibility import (
    COMPATIBLE,
    ResultFacts,
    check_template_compatibility,
    read_result_facts,
    read_template_predicates,
)

__all__ = [
    "MAX_OFFERED_TEMPLATES",
    "OFFER_KEY",
    "attach_offer",
    "compatible_templates",
]

#: How many compatible templates the model is shown at once. A catalogue is a
#: cost paid before a question is asked; five named, ranked choices is a menu,
#: and thirty is a second tool surface smuggled into a field.
MAX_OFFERED_TEMPLATES = 5

#: The one key this block ever occupies in the concise summary. Spelled once, so
#: the composer and the guard cannot disagree about which key is bounded.
OFFER_KEY = "chart_templates"


def compatible_templates(
    conn, *, org_id: str, project_id: str, result_id: str
) -> dict[str, Any]:
    """The bounded, ranked block of Chart Templates this Result can be drawn with.

    ONE Result is read, N templates are judged -- the shape
    `core.template_compatibility` was built for. `read_result_facts` is called
    exactly once and every template is judged against that same value, so N
    templates cost N primary-key reads rather than N re-readings of the Result.

    A Result nobody can read yields an EMPTY block rather than an absent one: the
    model is told there is nothing to apply, which is a fact, instead of being
    left to wonder whether the block was omitted.
    """
    facts = read_result_facts(conn, org_id=org_id, project_id=project_id, result_id=result_id)
    if not isinstance(facts, ResultFacts):
        return _block([], 0)

    offered: list[dict[str, Any]] = []
    for row in list_chart_templates(conn, org_id=org_id, project_id=project_id):
        version_id = row.get("current_version_id")
        if not version_id:
            #  A head whose version was never written is not a choice. It is
            #  named on the Chart Templates lens, where a person can repair it;
            #  it has no business in a model's menu.
            continue
        verdict = check_template_compatibility(
            read_template_predicates(
                conn,
                org_id=org_id,
                project_id=project_id,
                template_version_id=version_id,
            ),
            facts,
        )
        if verdict.state != COMPATIBLE:
            #  `incompatible` and `unavailable` are two different answers and the
            #  console shows both, each with its named predicate. Neither is a
            #  choice, so neither is offered -- the model cannot be given a door
            #  that the deterministic verdict has already closed.
            continue
        offered.append(
            {
                "visualization_template_version_id": verdict.template_version_id,
                "answers_question": verdict.answers_question,
                "family": verdict.family,
            }
        )

    head = offered[:MAX_OFFERED_TEMPLATES]
    return _block(head, len(offered) - len(head))


def _block(entries: list[dict[str, Any]], withheld: int) -> dict[str, Any]:
    return {
        "templates": [
            dict(entry, rank=position) for position, entry in enumerate(entries, start=1)
        ],
        "withheld": max(0, withheld),
    }


def attach_offer(summary: dict[str, Any], offer: dict[str, Any]) -> dict[str, Any]:
    """Put the block in the summary, and keep the summary inside its budget.

    The 4 KiB model-channel ceiling belongs to the ANSWER, not to this block. So
    entries are dropped one at a time -- each one raising `withheld`, so what is
    not shown stays counted -- and if even an empty block cannot fit, the key is
    removed entirely and the answer is exactly what it was before this story.
    `enforce_model_channel` still owns the refusal; nothing here hides an answer
    that was already over budget on its own.
    """
    block = {"templates": list(offer["templates"]), "withheld": int(offer["withheld"])}
    summary[OFFER_KEY] = block
    while serialized_bytes(summary) > MODEL_CHANNEL_MAX_BYTES and block["templates"]:
        block["templates"].pop()
        block["withheld"] += 1
    if serialized_bytes(summary) > MODEL_CHANNEL_MAX_BYTES:
        del summary[OFFER_KEY]
    return summary
