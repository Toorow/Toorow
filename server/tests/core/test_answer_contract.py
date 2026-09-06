"""Story 52.4 -- one answer contract, and the two honesty gaps it closes.

A contract nothing verifies is a comment, so the tests here refuse rather than
describe: a missing field raises, an empty visual slot is never `rendered`, and
`not_applicable` never collapses into `unavailable`.
"""

from __future__ import annotations

import pytest
from core import answer_contract as contract


def _envelope(**data):
    base = {
        "card_id": "kpi",
        "answers_question": "How are my KPIs evolving?",
        "rendered_comment": "Sessions are up 12% over the period.",
        "composition": [],
        "metrics": {},
        "knowledge_citations": [],
        "knowledge_status": None,
    }
    base.update(data)
    return {"schema_version": "1", "meta": {}, "data": base}


def _block(kind, payload=None):
    return {"type": kind, "binding": {"metrics": "*"}, "data": payload or {}}


# ---------------------------------------------------------------------------
# AC3 -- an empty visual slot is never a broken card.
# ---------------------------------------------------------------------------


def test_a_prose_only_answer_is_not_applicable_and_says_why():
    """A composition of comment blocks is a question answered in prose."""
    answer = contract.build_answer(_envelope(composition=[_block("comment")]))

    assert answer["visual"]["state"] == contract.VISUAL_NOT_APPLICABLE
    assert answer["visual"]["reason"]
    contract.validate_answer(answer)


def test_declared_visual_blocks_with_nothing_to_draw_are_unavailable_not_absent():
    """A chart that could not be produced is a failure, and it is named as one."""
    answer = contract.build_answer(
        _envelope(composition=[_block("bar"), _block("comment")], metrics={})
    )

    assert answer["visual"]["state"] == contract.VISUAL_UNAVAILABLE
    assert answer["visual"]["reason"]


def test_the_two_absent_visual_states_are_never_the_same():
    """A renderer that cannot tell them apart draws the same empty frame for both."""
    prose = contract.build_answer(_envelope(composition=[_block("comment")]))
    broken = contract.build_answer(_envelope(composition=[_block("bar")], metrics={}))

    assert prose["visual"]["state"] != broken["visual"]["state"]
    assert contract.VISUAL_NOT_APPLICABLE != contract.VISUAL_UNAVAILABLE


def test_a_chart_with_an_empty_payload_is_never_rendered_because_the_card_has_numbers():
    """review-epic-52 D-6, restated as an assertion.

    `build_answer` used to decide the state from `data.metrics`, which answers
    "does this card have numbers?" -- not "did this block draw?". A `bar` resolved
    to `{}` on a card with metrics therefore reported `rendered`, with no reason:
    the empty frame AC3 exists to abolish, announced as a success.
    """
    answer = contract.build_answer(
        _envelope(
            composition=[_block("comment", {"text": "x"}), _block("bar", {})],
            metrics={"sessions": {"value": 10}},
        )
    )

    assert answer["visual"]["state"] == contract.VISUAL_UNAVAILABLE
    assert answer["visual"]["reason"]
    contract.validate_answer(answer)


def test_an_empty_table_drew_nothing_even_though_it_still_carries_its_columns():
    """The truthiness of `block["data"]` is not the emptiness of what it drew.

    Every degraded block in `cards.py` keeps its descriptive payload: an empty
    table keeps its `columns`, an empty movers bar keeps its `dimension` and its
    `partial_reason`, an empty cannibalisation table keeps its `empty_label`.
    Reading the dict's truthiness counted all three as drawn.
    """
    answer = contract.build_answer(
        _envelope(
            composition=[
                _block("table", {"columns": [{"key": "a"}, {"key": "b"}], "rows": []}),
                _block("bar", {"dimension": "query", "bars": [], "partial": True}),
            ],
            metrics={"clicks": {"value": 1}},
        )
    )

    assert answer["visual"]["state"] == contract.VISUAL_UNAVAILABLE


def test_a_gauge_with_a_null_value_drew_nothing_despite_its_unit_and_label():
    """A gauge's whole answer is one scalar; `unit`/`direction`/`label` are chrome."""
    answer = contract.build_answer(
        _envelope(
            composition=[
                _block(
                    "gauge",
                    {
                        "value": None,
                        "target": None,
                        "target_source": "unset",
                        "unit": "EUR",
                        "direction": "down_good",
                        "label": "CPA vs target",
                    },
                )
            ],
            metrics={"conversions": {"value": 5}},
        )
    )

    assert answer["visual"]["state"] == contract.VISUAL_UNAVAILABLE


def test_numbers_and_tables_without_a_chart_are_not_applicable_not_a_failure():
    """review-epic-52 C-6: `not_applicable` had no reachable producer.

    Defined as "the composition holds nothing but comments", the state could only
    be produced by a hand-built envelope: all nine registered templates carry
    between one and five non-comment blocks, and a project-authored topic inherits
    its base template's composition. Naming the CHART types gives the state real
    producers -- `kpi`, `connectors`, `mediaplan_pacing` -- and gives a reader a
    fact they can act on: nothing is missing from this card.
    """
    answer = contract.build_answer(
        _envelope(
            composition=[
                _block("kpi_row", {"metrics": [{"name": "sessions", "value": 220}]}),
                _block("table", {"columns": [{"key": "a"}], "rows": [{"a": 1}]}),
                _block("comment", {"text": "Sessions are up."}),
            ],
            metrics={"sessions": {"value": 220}},
        )
    )

    assert answer["visual"]["state"] == contract.VISUAL_NOT_APPLICABLE
    assert "chart" in answer["visual"]["reason"]
    contract.validate_answer(answer)


def test_the_same_card_with_a_chart_that_drew_is_rendered():
    """The counterpart of the test above: adding a chart changes the state."""
    answer = contract.build_answer(
        _envelope(
            composition=[
                _block("kpi_row", {"metrics": [{"name": "sessions", "value": 220}]}),
                _block("bar", {"dimension": "query", "bars": [{"label": "a", "value": 1}]}),
            ],
            metrics={"sessions": {"value": 220}},
        )
    )

    assert answer["visual"]["state"] == contract.VISUAL_RENDERED
    assert answer["visual"]["reason"] is None


def test_a_card_whose_chart_is_empty_but_whose_table_drew_is_still_rendered():
    """The bound on the repair, stated so it is not tightened by accident.

    `keywords` ships a movers bar that is empty whenever there is no prior period,
    beside tables that resolved rows. Calling that whole visual half a FAILURE
    would overstate as badly as reading `data.metrics` understated: each empty
    block already carries its own designed-empty payload (`partial_reason`,
    `empty_label`), which is where per-block emptiness is reported.
    """
    answer = contract.build_answer(
        _envelope(
            composition=[
                _block("bar", {"dimension": "query", "bars": [], "partial": True}),
                _block("table", {"columns": [{"key": "q"}], "rows": [{"q": "x"}]}),
            ],
            metrics={"clicks": {"value": 100}},
        )
    )

    assert answer["visual"]["state"] == contract.VISUAL_RENDERED


def test_a_resolved_visual_is_rendered():
    answer = contract.build_answer(
        _envelope(
            composition=[_block("bar", {"rows": [{"label": "a", "value": 1}]})],
            metrics={"sessions": {"value": 10}},
        ),
        widget_uri="ui://core/card-kpi",
    )

    assert answer["visual"]["state"] == contract.VISUAL_RENDERED
    assert answer["visual"]["reason"] is None
    assert answer["visual"]["widget_uri"] == "ui://core/card-kpi"
    # The visual half is NAMED, never copied: one composition, one place.
    assert answer["visual"]["blocks_ref"] == "data.composition"
    assert answer["visual"]["block_count"] == 1
    contract.validate_answer(answer)


def test_the_contract_names_the_composition_instead_of_copying_it():
    """Inlining it doubled the envelope and cost two cards their model-channel budget."""
    composition = [_block("bar", {"rows": [1]}), _block("comment")]
    answer = contract.build_answer(
        _envelope(composition=composition, metrics={"sessions": {"value": 1}})
    )
    assert "blocks" not in answer["visual"]
    assert answer["visual"]["block_count"] == len(composition)


def test_the_contract_stays_small_enough_not_to_displace_what_it_describes():
    """The regression this bound exists for: a 2246-byte composition plus a
    duplicating contract crossed the 4096-byte model-channel budget, and the
    composition -- not the contract -- was the field that got moved out."""
    import json

    answer = contract.build_answer(
        _envelope(
            composition=[_block("bar", {"rows": [{"label": "x" * 200, "value": 1}]})],
            metrics={"sessions": {"value": 1}},
            rendered_comment="y" * 2000,
            knowledge_citations=[{"kind": "topic", "id": "c", "version": 1, "title": "t"}] * 20,
        )
    )
    assert len(json.dumps(answer)) < 512


# ---------------------------------------------------------------------------
# AC4 -- the textual half, including a refusal as a legitimate value.
# ---------------------------------------------------------------------------


def test_the_textual_half_names_what_it_carries_and_counts_the_citations():
    """The contract NAMES both halves; copying them doubled the envelope and cost
    two cards their model-channel budget."""
    citations = [{"kind": "topic", "id": "ctx_1", "version": 3, "title": "Attribution"}]
    answer = contract.build_answer(_envelope(knowledge_citations=citations))

    assert answer["text"]["citations_ref"] == "data.knowledge_citations"
    assert answer["text"]["citation_count"] == 1
    assert answer["text"]["question_ref"] == "data.answers_question"
    assert answer["text"]["comment_ref"] == "data.rendered_comment"
    # And nothing is duplicated: no value of the envelope is repeated here.
    assert "comment" not in answer["text"]
    assert "citations" not in answer["text"]


def test_a_refusal_is_a_value_of_the_contract_not_an_error_around_it():
    answer = contract.build_answer(
        _envelope(
            knowledge_status="context_missing",
            knowledge_refusal={"refused": True, "reason_code": "context_missing"},
        )
    )

    assert answer["text"]["refused"] is True
    assert answer["text"]["knowledge_status"] == "context_missing"
    contract.validate_answer(answer)


def test_an_answer_with_no_knowledge_is_not_a_refusal():
    answer = contract.build_answer(_envelope(knowledge_status="no_knowledge_declared"))
    assert answer["text"]["refused"] is False


# ---------------------------------------------------------------------------
# AC2 -- the contract refuses, it does not describe.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", contract.ANSWER_KEYS)
def test_a_missing_top_level_key_is_refused(missing):
    answer = contract.build_answer(_envelope(composition=[_block("comment")]))
    answer.pop(missing)
    with pytest.raises(contract.AnswerContractViolation):
        contract.validate_answer(answer)


@pytest.mark.parametrize("missing", contract.TEXT_ANSWER_KEYS)
def test_a_missing_textual_key_is_refused(missing):
    answer = contract.build_answer(_envelope(composition=[_block("comment")]))
    answer["text"].pop(missing)
    with pytest.raises(contract.AnswerContractViolation):
        contract.validate_answer(answer)


@pytest.mark.parametrize("missing", contract.VISUAL_ANSWER_KEYS)
def test_a_missing_visual_key_is_refused(missing):
    answer = contract.build_answer(_envelope(composition=[_block("comment")]))
    answer["visual"].pop(missing)
    with pytest.raises(contract.AnswerContractViolation):
        contract.validate_answer(answer)


def test_an_unrendered_visual_without_a_reason_is_refused():
    """The empty slot this story exists to abolish: the renderer would have to guess."""
    answer = contract.build_answer(_envelope(composition=[_block("comment")]))
    answer["visual"]["reason"] = None
    with pytest.raises(contract.AnswerContractViolation):
        contract.validate_answer(answer)


def test_an_unknown_visual_state_is_refused():
    answer = contract.build_answer(_envelope(composition=[_block("comment")]))
    answer["visual"]["state"] = "maybe"
    with pytest.raises(contract.AnswerContractViolation):
        contract.validate_answer(answer)


# ---------------------------------------------------------------------------
# AC7 -- every card path carries it, not three out of four.
# ---------------------------------------------------------------------------


def test_every_host_of_a_card_answer_builds_the_contract():
    """AC7, restated by a measurement.

    The contract was first built inside the envelope, on all four of `cards.py`'s
    return paths. It was moved OUT: the `dedup` envelope has only tens of bytes of
    room under the 4096-byte model-channel budget, so a 336-byte contract inside it
    displaced `composition` into the app channel -- the contract breaking the
    answer it describes. It is a HOST binding now, so the class to treat is the
    hosts: every surface that returns a card answer must build it, and there are
    exactly two.

    THE MARGIN IS A MOVING NUMBER, so it is not quoted here. "117 bytes" was
    written in six places and none of them reproduced: the command below returned
    **127** on 2026-08-01 (review-epic-52 D-11), and **65** after Story 52.3's
    knowledge fields were extended to the `dedup` path, which is the repair for
    D-2. Read it, do not trust a copy of it:

        cd server && python -c "
        import os,sys; sys.path.insert(0,'.')
        os.environ.setdefault('HEALTH_POLLER_ENABLED','false')
        os.environ.setdefault('QUEUE_WORKER_ENABLED','false')
        os.environ.setdefault('SCHEDULER_ENABLED','false')
        from tests.integration import test_model_channel_margin as m
        for n,t,r,e,w in m._MEASURED_CARDS:
            print(n, m._margin(m._envelope_for(t,r,extra_patches=e,warehouse=w)))"
    """
    import pathlib
    import re

    core = pathlib.Path(contract.__file__).parent
    # THE HOST IS THE MODULE THAT SERVES THE CARD, NOT THE ENTRYPOINT. `get_card`
    # moved out of `main.py` into `core.cards_mcp` when the entrypoint was split
    # by surface; the REST half never moved. Naming `main.py` here would assert
    # a contract on a file that no longer returns a card answer.
    hosts = {"cards_api.py": "REST", "cards_mcp.py": "MCP"}
    for filename in hosts:
        src = (core / filename).read_text(encoding="utf-8")
        assert re.search(r"build_answer\(envelope", src), (
            f"{filename} returns a card answer without building the contract"
        )

    # And the envelope no longer carries it: one place, not two.
    cards_src = (core / "cards.py").read_text(encoding="utf-8")
    assert 'envelope["data"]["answer"]' not in cards_src, (
        "the contract is back inside the envelope, where it displaces the payload"
    )
