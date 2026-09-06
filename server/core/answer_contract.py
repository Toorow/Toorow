"""Story 52.4 -- ONE answer contract, carrying both halves of an answer.

WHY THIS MODULE EXISTS, since both halves were already built. `cards.get_card`
has returned a cited textual comment and an ordered composition since Epic 9, and
both the REST route and the MCP tool call that same function. What was missing is
that the two halves were **two conventions that happened to agree**, described
nowhere and asserted by nothing. Two descriptions is how they drift, and a host
that adds a field is how an answer starts changing shape with its host.

So this module states the contract once, in code, and gives it a validator. It
recomputes nothing: `build_answer` is a projection of what the envelope already
carries.

THE TWO HONESTY GAPS IT CLOSES, both named by the epic:

  * **An empty visual slot is never a broken card.** A composition made only of a
    comment block is not "a chart that failed to draw" -- it is a question that
    does not call for a visual, and the answer must SAY that. Hence `state`, with
    `not_applicable` and `unavailable` kept apart: one is a property of the
    question, the other is a failure, and a renderer that cannot tell them apart
    draws the same empty frame for both.
  * **The same contract serves the console and the MCP App.** Both hosts project
    THIS object. A host may add a binding of its own -- the MCP `_meta.ui.resourceUri`
    that names the widget -- and that is a host binding, not a different answer.

WHAT IT DOES NOT DO. It does not rewrite `get_card`, introduce a renderer, or
touch the composition vocabulary: the hybrid commentary shape and the composition
contract were decided on 2026-07-13 and are explicitly not reopened by Epic 52,
and the rendering stack belongs to `visualization-and-rendering.md` and Epic 50.

ASCII-only stdout (AI-03).
"""

from __future__ import annotations

from typing import Any

#: The visual half is really there: at least one block that is not a comment.
VISUAL_RENDERED = "rendered"
#: The question does not call for a visual. A stated answer, not a gap.
VISUAL_NOT_APPLICABLE = "not_applicable"
#: A visual was expected and could not be produced. NEVER merged with the above.
VISUAL_UNAVAILABLE = "unavailable"

VISUAL_STATES = (VISUAL_RENDERED, VISUAL_NOT_APPLICABLE, VISUAL_UNAVAILABLE)

#: Blocks that carry no visual answer on their own. `comment` is the textual half
#: rendered inside the composition; a composition made only of these is a question
#: answered in prose.
_NON_VISUAL_BLOCK_TYPES = frozenset({"comment"})

#: The block types that draw a CHART. Deliberately not "everything that is not a
#: comment": `kpi_row` and `table` restate numbers, they do not plot them.
#:
#: WHY THIS DISTINCTION EXISTS, and it is a measurement rather than a taste. The
#: first version of this module defined `not_applicable` as "the composition holds
#: nothing but comment blocks". Measured on 2026-08-01, all nine registered
#: templates carry between one and five non-comment blocks, and a project-authored
#: topic inherits its base template's composition
#: (`answerable_topics._entry_from_stored`), so NO card reachable in production
#: could ever report `not_applicable` -- only a hand-built envelope in a test could.
#: An acceptance criterion whose behaviour cannot be produced is a promise the
#: product does not keep (review-epic-52 C-6).
#:
#: With chart types named, the state is reachable by real cards and means something
#: a reader can act on: `kpi` (kpi_row + comment), `connectors` (table + comment)
#: and `mediaplan_pacing` (two tables + comment) answer WITHOUT a chart, so no chart
#: is missing from them. What was NOT done, and why: reclassifying `table` as
#: "prose" outright. `mediaplan_pacing` and `connectors` answer their whole question
#: in a table, and calling that "answered in prose" would replace one wrong label
#: with another.
_CHART_BLOCK_TYPES = frozenset(
    {"line", "bar", "donut", "gauge", "funnel", "waterfall"}
)

#: Where a resolved block puts what it drew. A block that declares one of these and
#: leaves it EMPTY drew nothing, whatever else its payload carries -- an empty table
#: still carries its `columns`, an empty movers bar still carries its `dimension`
#: and its `partial_reason`, and a gauge with a null `value` still carries its
#: `unit`, its `direction` and its `label`. Reading the payload's truthiness (what
#: `build_answer` used to do) counts all three as drawn.
_BLOCK_CONTENT_KEYS = ("rows", "bars", "slices", "steps", "series", "metrics", "points")

#: Blocks whose whole answer is one scalar (the gauge). `None` there is a real gap.
_BLOCK_SCALAR_KEYS = ("value", "total")

#: Required keys of each half. Stated as data so the validator and the builder
#: cannot disagree about what "complete" means.
#:
#: THE CONTRACT NAMES, IT NEVER COPIES. Every field that already exists elsewhere
#: in the envelope is carried as a `*_ref` -- the path where it lives -- and only
#: what exists nowhere else (the states, the counts, the refusal flag) is carried
#: by value. This is not a style choice; it was MEASURED. The first version
#: inlined the comment and the resolved composition, and two cards
#: (`attribution`, `dedup`) lost their `composition` to the app channel because
#: the duplicated envelope crossed the model-channel budget --
#: `test_get_card_{attribution,dedup}_through_mcp_tool_layer_seam`. A contract
#: that doubles the payload it describes is also two things that can drift.
TEXT_ANSWER_KEYS = (
    "question_ref",
    "comment_ref",
    "citations_ref",
    "citation_count",
    "knowledge_status",
    "refused",
)
#: `blocks_ref` NAMES where the visual half lives; it does not copy it. The first
#: version of this contract inlined the resolved composition, which doubled the
#: envelope and pushed one card's `composition` past the model-channel budget --
#: measured, in `test_get_card_dedup_through_mcp_tool_layer_seam`. Two copies of
#: one thing are also two things that can drift.
VISUAL_ANSWER_KEYS = ("state", "block_count", "blocks_ref", "widget_uri", "reason")
ANSWER_KEYS = ("topic_id", "text", "visual")


class AnswerContractViolation(Exception):
    """The answer does not satisfy the contract. Raised by `validate_answer`."""


def _block_drew_something(block: dict) -> bool:
    """True when this block's RESOLVED payload carries at least one datum.

    Reads the block, never the card's `data.metrics`. That proxy is what made the
    state dishonest: `metrics` says "this card has numbers", not "this block drew",
    so a `bar` resolved to `{}` on a card with metrics reported `rendered` -- the
    empty frame Story 52.4 exists to abolish (review-epic-52 D-6).
    """
    payload = block.get("data")
    if not isinstance(payload, dict) or not payload:
        return False

    containers = [payload[key] for key in _BLOCK_CONTENT_KEYS if key in payload]
    if containers:
        return any(
            len(value) > 0 for value in containers if isinstance(value, (list, tuple, dict))
        )

    scalars = [payload[key] for key in _BLOCK_SCALAR_KEYS if key in payload]
    if scalars:
        return any(value is not None for value in scalars)

    # A payload shape this contract has not met. Reporting a failure we cannot
    # actually see would be its own dishonesty, so a non-empty payload counts.
    return any(value not in (None, "", [], {}) for value in payload.values())


def _visual_state(composition: list[dict] | None) -> tuple[str, str | None]:
    """Decide the visual state from what the composition ACTUALLY resolved to.

    Read the resolved composition, never the template's declared one nor the card's
    metric totals: a template can declare a chart that resolved to nothing, and
    reporting `rendered` for it is precisely the empty frame the epic refuses.

    The three states, and the real card each is reachable by (measured 2026-08-01,
    `test_answer_contract_seams.py`):

      * `not_applicable` -- nothing is missing. Either the composition is prose, or
        it answers with numbers and tables and declares no chart (`kpi`,
        `connectors`, `mediaplan_pacing`).
      * `unavailable`    -- blocks were declared and not one of them drew. Reached
        by `connectors` when the database is unreachable: the inventory table
        resolves to zero rows and the card is a frame with nothing in it.
      * `rendered`       -- a chart was declared and at least one block drew.

    A card whose chart is empty but whose table drew is `rendered`, NOT
    `unavailable`: each block already carries its own designed-empty payload
    (`empty_label`, `partial_reason`), and calling the whole visual half a failure
    because one of five blocks is empty would overstate as badly as the proxy
    understated.
    """
    blocks = [b for b in (composition or []) if b.get("type") not in _NON_VISUAL_BLOCK_TYPES]
    if not blocks:
        # No visual block at all. The composition is prose, by design -- the
        # answer says so rather than leaving a slot a renderer must interpret.
        return VISUAL_NOT_APPLICABLE, "this question is answered in prose"
    if not any(_block_drew_something(b) for b in blocks):
        # Blocks were declared and NOT ONE of them resolved to anything. That is a
        # failure to produce, and it is named as one.
        return VISUAL_UNAVAILABLE, "no data resolved for the declared visual blocks"
    if not any(b.get("type") in _CHART_BLOCK_TYPES for b in blocks):
        # Numbers and tables, no chart declared. Nothing is missing here, and
        # saying so is the whole point of keeping this apart from `unavailable`.
        return (
            VISUAL_NOT_APPLICABLE,
            "this question is answered without a chart (numbers and tables)",
        )
    return VISUAL_RENDERED, None


def build_answer(envelope: dict, *, widget_uri: str | None = None) -> dict[str, Any]:
    """Project one card envelope into the answer contract. Computes nothing new.

    `envelope` is the AD-1 envelope `cards.get_card` builds. Every value below is
    read from it, so the contract can never disagree with the answer it describes.
    """
    data = (envelope or {}).get("data") or {}
    composition = data.get("composition") or []
    # `data.metrics` is NOT read here any more. It answered "does this card have
    # numbers?", which is not the question -- see `_block_drew_something`.
    state, reason = _visual_state(composition)

    refusal = data.get("knowledge_refusal") or {}
    return {
        "topic_id": data.get("card_id"),
        "text": {
            # Named, not copied -- see TEXT_ANSWER_KEYS.
            "question_ref": "data.answers_question",
            "comment_ref": "data.rendered_comment",
            # What was READ, never what was declared (Story 52.3).
            "citations_ref": "data.knowledge_citations",
            "citation_count": len(data.get("knowledge_citations") or []),
            "knowledge_status": data.get("knowledge_status"),
            # A refusal is a legitimate value of the contract, not an error path
            # around it: a topic may answer only from governed knowledge.
            "refused": bool(refusal.get("refused")),
        },
        "visual": {
            "state": state,
            "block_count": len(composition),
            # Named, not copied. See VISUAL_ANSWER_KEYS.
            "blocks_ref": "data.composition",
            "widget_uri": widget_uri,
            "reason": reason,
        },
    }


def validate_answer(answer: dict) -> None:
    """Raise `AnswerContractViolation` unless *answer* satisfies the contract.

    A contract nothing verifies is a comment, so this is called by the tests that
    pin both hosts -- and it refuses a missing key rather than tolerating it,
    because a tolerated omission is how one host quietly stops carrying a half.
    """
    if not isinstance(answer, dict):
        raise AnswerContractViolation("an answer is an object")
    for key in ANSWER_KEYS:
        if key not in answer:
            raise AnswerContractViolation(f"answer is missing {key!r}")

    text = answer.get("text")
    if not isinstance(text, dict):
        raise AnswerContractViolation("answer.text is an object")
    for key in TEXT_ANSWER_KEYS:
        if key not in text:
            raise AnswerContractViolation(f"answer.text is missing {key!r}")

    visual = answer.get("visual")
    if not isinstance(visual, dict):
        raise AnswerContractViolation("answer.visual is an object")
    for key in VISUAL_ANSWER_KEYS:
        if key not in visual:
            raise AnswerContractViolation(f"answer.visual is missing {key!r}")
    if visual["state"] not in VISUAL_STATES:
        raise AnswerContractViolation(
            f"answer.visual.state must be one of {VISUAL_STATES}"
        )
    if visual["state"] != VISUAL_RENDERED and not visual.get("reason"):
        # An absent visual without a reason is exactly the empty slot this story
        # exists to abolish: the renderer would have to guess whether it broke.
        raise AnswerContractViolation(
            "a visual that is not rendered must say why"
        )
