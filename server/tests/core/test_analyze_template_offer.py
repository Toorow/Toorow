"""Story 72.7 -- the MCP door of a Chart Template, at constant tool budget.

WHAT THIS FILE PROVES, AND WHAT IT DELIBERATELY CANNOT.
Everything here is about the SHAPE of the door: which parameters are exclusive,
what the bounded block contains, how it is trimmed, and the fact that the block
disappears with the render tool it points at. Not one line touches a database,
and the two claims that are about rows -- an incompatible template writes
nothing, a compatible one materialises an ordinary Spec version -- live in
`test_analyze_template_offer_pg.py`, where a real table can answer them.

The bounding rules are tested against VALUES rather than through the tool,
because a block that is bounded only when the answer around it happens to be
small is not bounded.
"""

from __future__ import annotations

import json

import pytest
from core import analyze_render_mcp as adapter
from core import analyze_template_offer as offer
from core.model_channel import MODEL_CHANNEL_MAX_BYTES, serialized_bytes
from core.template_compatibility import (
    COMPATIBLE,
    INCOMPATIBLE,
    UNAVAILABLE,
    CompatibilityVerdict,
    ResultFacts,
    Unreadable,
)
from core.visualization_specs import VisualizationRefusal

RESULT = "qr_EXAMPLE"
ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"


# ---------------------------------------------------------------------------
# A stand-in for the two readers of story 72.3. The block's job is to CALL them
# and bound what comes back; judging is theirs and is tested in their own suite.
# ---------------------------------------------------------------------------


def _facts() -> ResultFacts:
    return ResultFacts(
        result_id=RESULT,
        pinned=None,
        members=(),
        row_count=6,
        truncated=False,
        outcome="success",
        distinct_values={},
    )


def _verdict(version_id: str, state: str = COMPATIBLE, question: str = "How much?"):
    return CompatibilityVerdict(
        state=state,
        template_version_id=version_id,
        result_id=RESULT,
        family="bar",
        answers_question=question,
    )


def _wire(monkeypatch, verdicts: dict[str, str], *, facts=None):
    """Wire the three reads `compatible_templates` performs. No database."""
    order = list(verdicts)
    monkeypatch.setattr(
        offer, "read_result_facts", lambda *a, **k: _facts() if facts is None else facts
    )
    monkeypatch.setattr(
        offer,
        "list_chart_templates",
        lambda *a, **k: [{"id": f"vtpl_{v}", "current_version_id": v} for v in order],
    )
    monkeypatch.setattr(
        offer, "read_template_predicates", lambda conn, **k: k["template_version_id"]
    )
    monkeypatch.setattr(
        offer,
        "check_template_compatibility",
        lambda predicates, _facts: _verdict(predicates, verdicts[predicates]),
    )


def _templates(monkeypatch, verdicts, **kwargs):
    _wire(monkeypatch, verdicts, **kwargs)
    return offer.compatible_templates(
        object(), org_id=ORG, project_id=PROJECT, result_id=RESULT
    )


# ---------------------------------------------------------------------------
# AC30 -- the catalogue is BOUNDED, and it carries no Result data.
# ---------------------------------------------------------------------------


def test_only_compatible_templates_are_offered_and_the_other_two_states_are_not():
    """`incompatible` and `unavailable` are answers, and neither is a choice.

    A model handed a template the deterministic verdict already closed would be
    a model invited to override it, which is exactly the sentence the ratified
    page forbids.
    """
    with pytest.MonkeyPatch.context() as patch:
        block = _templates(
            patch,
            {"vtv_A": COMPATIBLE, "vtv_B": INCOMPATIBLE, "vtv_C": UNAVAILABLE},
        )
    assert [t["visualization_template_version_id"] for t in block["templates"]] == ["vtv_A"]
    assert block["withheld"] == 0


def test_the_block_is_capped_and_states_how_many_it_is_not_showing():
    names = {f"vtv_{index:02d}": COMPATIBLE for index in range(9)}
    with pytest.MonkeyPatch.context() as patch:
        block = _templates(patch, names)
    assert len(block["templates"]) == offer.MAX_OFFERED_TEMPLATES
    assert block["withheld"] == 9 - offer.MAX_OFFERED_TEMPLATES


def test_an_entry_carries_four_fields_and_no_row_of_the_result():
    with pytest.MonkeyPatch.context() as patch:
        block = _templates(patch, {"vtv_A": COMPATIBLE})
    entry = block["templates"][0]
    assert set(entry) == {
        "visualization_template_version_id",
        "answers_question",
        "family",
        "rank",
    }
    serialized = json.dumps(block)
    for leak in ("row", "column", "value", "content_hash", "result_id"):
        assert leak not in serialized, f"`{leak}` reached the model in a template block"


def test_rank_is_a_position_in_the_order_and_never_a_score():
    """Contiguous from one, in the order the Project's own list carries.

    The order is the store's; the rank only names the place. A model may re-order
    what it was given -- it may not be told one of these fits better than another
    by a server that never measured such a thing.
    """
    with pytest.MonkeyPatch.context() as patch:
        block = _templates(
            patch, {"vtv_A": COMPATIBLE, "vtv_B": INCOMPATIBLE, "vtv_C": COMPATIBLE}
        )
    assert [t["rank"] for t in block["templates"]] == [1, 2]
    assert [t["visualization_template_version_id"] for t in block["templates"]] == [
        "vtv_A",
        "vtv_C",
    ]


def test_a_head_with_no_written_version_is_not_a_choice(monkeypatch):
    monkeypatch.setattr(offer, "read_result_facts", lambda *a, **k: _facts())
    monkeypatch.setattr(
        offer,
        "list_chart_templates",
        lambda *a, **k: [{"id": "vtpl_A", "current_version_id": None}],
    )
    monkeypatch.setattr(
        offer,
        "read_template_predicates",
        lambda *a, **k: pytest.fail("a head with no version was judged"),
    )
    block = offer.compatible_templates(
        object(), org_id=ORG, project_id=PROJECT, result_id=RESULT
    )
    assert block == {"templates": [], "withheld": 0}


def test_a_result_nobody_can_read_yields_an_empty_block_not_a_missing_one(monkeypatch):
    """Empty says "nothing to apply"; absent would say nothing at all."""
    unreadable = Unreadable(
        VisualizationRefusal("result_unreadable", "no schema", None, "Run it again.")
    )
    monkeypatch.setattr(offer, "read_result_facts", lambda *a, **k: unreadable)
    monkeypatch.setattr(
        offer, "list_chart_templates", lambda *a, **k: pytest.fail("templates were listed")
    )
    assert offer.compatible_templates(
        object(), org_id=ORG, project_id=PROJECT, result_id=RESULT
    ) == {"templates": [], "withheld": 0}


# ---------------------------------------------------------------------------
# The answer's budget belongs to the answer.
# ---------------------------------------------------------------------------


def test_the_block_never_pushes_an_answer_that_fitted_over_its_budget():
    """Entries drop one at a time, each one counted, until the summary fits."""
    summary = {"schema_version": 1, "filler": "x" * (MODEL_CHANNEL_MAX_BYTES - 200)}
    block = {
        "templates": [
            {
                "visualization_template_version_id": f"vtv_{index}",
                "answers_question": "q" * 110,
                "family": "bar",
                "rank": index + 1,
            }
            for index in range(5)
        ],
        "withheld": 2,
    }
    offer.attach_offer(summary, block)
    assert serialized_bytes(summary) <= MODEL_CHANNEL_MAX_BYTES
    attached = summary[offer.OFFER_KEY]
    assert attached["withheld"] == 2 + (5 - len(attached["templates"]))


def test_a_summary_already_over_budget_loses_the_key_rather_than_the_refusal():
    """`enforce_model_channel` still owns the refusal; this block never hides one."""
    summary = {"schema_version": 1, "filler": "x" * (MODEL_CHANNEL_MAX_BYTES + 100)}
    offer.attach_offer(summary, {"templates": [], "withheld": 0})
    assert offer.OFFER_KEY not in summary
    assert serialized_bytes(summary) > MODEL_CHANNEL_MAX_BYTES


# ---------------------------------------------------------------------------
# AC27 / the conditional registration -- the block inherits the render tool.
# ---------------------------------------------------------------------------


def test_without_the_runtime_the_template_block_is_not_offered_either(monkeypatch):
    """No runtime, no render tool, no menu that points at it.

    A block naming templates a deployment cannot draw would be an invitation to a
    fallback drawing path, and there is none -- by design, since Story 50.5.
    """
    monkeypatch.setitem(adapter.REGISTRATION_STATE, "render_tool_registered", False)
    monkeypatch.setattr(
        offer, "list_chart_templates", lambda *a, **k: pytest.fail("templates were read")
    )
    summary: dict = {"schema_version": 1}
    adapter._attach_template_offer(
        object(), org_id=ORG, project_id=PROJECT, result_id=RESULT, summary=summary
    )
    assert offer.OFFER_KEY not in summary


def test_with_the_runtime_the_block_joins_the_same_summary_both_tools_carry(monkeypatch):
    monkeypatch.setitem(adapter.REGISTRATION_STATE, "render_tool_registered", True)
    _wire(monkeypatch, {"vtv_A": COMPATIBLE})
    summary: dict = {"schema_version": 1}
    adapter._attach_template_offer(
        object(), org_id=ORG, project_id=PROJECT, result_id=RESULT, summary=summary
    )
    assert summary[offer.OFFER_KEY]["templates"][0]["rank"] == 1


# ---------------------------------------------------------------------------
# AC28 -- the two parameters are exclusive.
# ---------------------------------------------------------------------------


def _refusal(monkeypatch, **kwargs) -> dict:
    def answer(*_args, **_kwargs):  # pragma: no cover - reached only on a defect
        pytest.fail("the refusal must land before any Result is read")

    monkeypatch.setattr(adapter, "_answer", answer)
    with pytest.raises(Exception) as excinfo:
        adapter._render_answer(PROJECT, RESULT, **kwargs)
    return json.loads(str(excinfo.value))


def test_both_pins_together_are_a_named_refusal(monkeypatch):
    payload = _refusal(
        monkeypatch,
        visualization_spec_version_id="vsv_EXAMPLE",
        visualization_template_version_id="vtv_EXAMPLE",
    )
    assert payload["code"] == "visualization_pin_ambiguous"
    #  The sentence names the gesture, not the cause: which one to send.
    assert "visualization_spec_version_id" in payload["message"]
    assert "visualization_template_version_id" in payload["message"]


def test_neither_pin_is_the_refusal_this_tool_already_made(monkeypatch):
    payload = _refusal(monkeypatch, visualization_spec_version_id=" ")
    assert payload["code"] == "missing_param"
    assert "visualization_template_version_id" in payload["message"]


def test_the_template_pin_alone_is_accepted_where_the_spec_pin_used_to_be_required(
    monkeypatch,
):
    """The parameter is an ALTERNATIVE, not a second requirement."""
    seen: dict = {}

    def answer(project_id, result_id, tool_name, *, meta_finalizer=None):
        seen["tool"] = tool_name
        return "text", {}, {}

    monkeypatch.setattr(adapter, "_answer", answer)
    adapter._render_answer(PROJECT, RESULT, None, "vtv_EXAMPLE")
    assert seen["tool"] == adapter.RENDER_TOOL_NAME


def test_the_tool_signature_offers_the_two_pins_optionally_and_nothing_else():
    """The door is a PARAMETER of an existing tool -- D3, zero new tools."""
    import inspect

    parameters = inspect.signature(adapter.render_analyze_result).parameters
    assert list(parameters) == [
        "project_id",
        "result_id",
        "visualization_spec_version_id",
        "visualization_template_version_id",
    ]
    assert parameters["visualization_spec_version_id"].default is None
    assert parameters["visualization_template_version_id"].default is None
