"""The warning before the cliff: how close is each card to the model-channel budget?

WHY THIS FILE EXISTS, and it is a finding rather than a precaution. Story 52.4
added a ~336-byte answer contract to the card envelope. Two MCP seam tests went
red -- not because the contract was wrong, but because the `dedup` envelope sits
a two-digit number of bytes under the 4096-byte model-channel budget, so the
guard did what it is for and moved `composition` to the app channel. The contract
had displaced the very thing it describes.

DO NOT QUOTE A CONSTANT FOR THAT MARGIN. Story 52.4 recorded it as "117 bytes" in
six places; measured on 2026-08-01 at HEAD `8ee8ca8a` it was **127**, and later the
same day -- with `core/cards.py` and `core/answer_contract.py` carrying a parallel
session's uncommitted repairs -- **65**, at HEAD `0f53bec1` and again at HEAD
`33be9245`. The number is not a property of the card, it is a reading of the tree.
Take it with:

    cd server && python -m pytest tests/integration/test_model_channel_margin.py -q

and, for the per-card figures, the loop over `_MEASURED_CARDS` below.

Story 50.6's budget is not the problem: it is doing its job, loudly and
correctly. What was missing is that NOTHING SAID a card was one field away from
the cliff. The failure surfaced two suites away from the change, as a card that
suddenly rendered nothing, and the person reading it had no way to know the cause
was arithmetic.

So this file measures the margin and fails while there is still room to act. It
does not lower the budget, does not widen it, and does not move anything: it
turns a silent proximity into a stated number.

WHAT A FAILURE HERE MEANS. Not "your change is wrong". It means: this card is now
close enough to the ceiling that the NEXT field added anywhere in the envelope
will push a payload out of the model channel. The fix is a decision -- trim the
envelope, or accept that this card's composition travels in the app channel and
say so -- never to raise the floor below without writing down why.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from core import cards  # noqa: E402
from core.model_channel import MODEL_CHANNEL_MAX_BYTES, serialized_bytes  # noqa: E402

from tests.integration.test_cards_integration_seams import (  # noqa: E402
    _attribution_seam_rows,
    _cannibalisation_position_rows,
    _cannibalisation_rows,
    _dedup_seam_rows,
    _keyword_rows,
    _sessions_rows,
)

#: A card closer than this to the ceiling is one ordinary field away from losing
#: a payload to the app channel. 336 bytes is not arbitrary: it is the size of the
#: Story 52.4 answer contract, i.e. the size of ONE modest addition -- measured,
#: not guessed. A card with less margin than one such field is already fragile.
MARGIN_FLOOR_BYTES = 336

#: THE CARDS THAT ARE ALREADY TIGHT. This table is the finding, not a tolerance
#: list: FOUR of the five cards this repository has offline fixtures for cannot
#: meet the floor today -- two on 2026-08-01, four on 2026-09-01. Only `kpi`
#: still passes the general assertion. The dated block at the end of the table
#: carries the per-commit reading that took it from two to four.
#:
#:   * `dedup` is the card that caught Story 52.4. Its margin, re-measured
#:     2026-08-01 at HEAD `0f53bec1` and again at `33be9245` (working tree in
#:     both: `core/cards.py` and `core/answer_contract.py` dirty, parallel repair
#:     session), is **65 bytes**.
#:     Story 52.4 recorded 117; that figure never reproduced -- the same command
#:     gave 127 at HEAD `8ee8ca8a` a few hours earlier. See the module docstring:
#:     the margin is a reading of the tree, not a constant, and the band below is
#:     what asserts, not the figure.
#:   * `keywords` WITH cannibalisation data is 404 bytes OVER the budget, so its
#:     `composition` already travels in the app channel and a model calling
#:     `get_card` never sees it. That is not something Story 52.4 caused: the seam
#:     test for this card reads the REST response, where the split does not apply,
#:     so nothing ever said it. `-404` DOES reproduce, at both tree states above.
#:
#: WHY THE BANDS ARE WIDE, deliberately. `dedup`'s band is the whole space below
#: the floor, so a movement of 62 bytes inside it does not fail -- which is how
#: the wrong figure survived six citations. Narrowing it would be the wrong
#: repair while two sessions write the envelope: it would fail on someone else's
#: correct work. What fails here is a card LEAVING its band; what documents the
#: number is the command, in the docstring above.
KNOWN_TIGHT = {
    # RE-MEASURED 2026-08-16, after the envelope gave back what it was repeating.
    # `meta.analytical_path` carried a 250-byte paragraph on EVERY card, and
    # `meta.card_selection` carried `answers_question` a second time -- once for
    # the chosen card, which `data` already states, and once per alternative,
    # which nothing read (`_build_summary` joins their ids only). Removing both
    # returned ~490 bytes per card, and it is what took `keywords` from 94 to 408
    # and `attribution` from 84 to 395: two cards that no longer sit on the edge.
    #
    # These two remain over the ceiling, and that is the FINDING this table
    # exists to state, not a tolerance:
    #
    #   `dedup` at -144. Its `composition` alone is 2 226 bytes -- the blocks it
    #   renders, which are its answer. Nothing in it repeats anything.
    #   `keywords-cannibalisation` at -571, for the same reason at a larger size:
    #   composition 2 271 plus 687 bytes of metrics.
    #
    # Their composition therefore travels in the APP channel, which is written
    # down here as the test asked. The bands are tight on purpose: a card that
    # drifts further is a change somebody made, and it fails.
    # Both re-measured 2026-09-01 with the block below: `dedup` -134 -> -219,
    # `keywords-cannibalisation` -736 -> -1051, from the same three commits.
    # Neither changes state -- both were already over the ceiling and their
    # composition already travelled in the app channel.
    "dedup": (-400, 0),
    "keywords-cannibalisation": (-1200, -300),
    #
    # RE-MEASURED 2026-08-18 (AI-59). `keywords` fell from 408 to 247, and the
    # 161 bytes are not a repetition this time -- they are the contract. The
    # cannibalisation table alone emitted bare label strings where every other
    # table emits {key,label,numeric} objects with rows keyed by `key`, which is
    # the only shape `DataTable` reads. Left as it was, the block would have
    # rendered four empty columns the day it stopped being empty.
    #
    # Decision, as this table asks: PAY IT. The card still fits the model
    # channel at 247 bytes to spare; what it no longer has is room for one more
    # ordinary field. The next field added anywhere in this envelope pushes it
    # into the app channel, and that is what this band is here to catch. If the
    # envelope is trimmed back above 336, remove this entry -- the general
    # assertion covers it again.
    #
    # ===================================================================
    # RE-MEASURED 2026-09-01. THE NEXT FIELD WAS ADDED. It was three, and
    # `keywords` went over.
    #
    # The command, from `server/`:
    #     python -m pytest tests/integration/test_model_channel_margin.py -q
    # Read per commit in a detached worktree, same fixtures, same command
    # (`kpi / keywords / keywords-cannibalisation / attribution / dedup`):
    #
    #   3617e323  AI-59, the row above     2297 /  247 /  -736 /  405 / -134
    #   6f0ec96a  view timeline dates      2297 /  247 /  -736 /  405 / -134
    #   d9ac8af1  27.8 + 27.9              2169 /  119 /  -864 /  277 / -134
    #   0472efaf  67-24                    2149 /   99 /  -936 /  243 / -214
    #   3e7ead13  the client's word        2149 /  -11 / -1046 /  175 / -214
    #   HEAD (3c0c3ebc)                    2144 /  -16 / -1051 /  170 / -219
    #
    # WHAT WAS SPENT, and none of it is a repetition -- which is why this is a
    # re-record and not a trim:
    #
    #   * d9ac8af1 added `meta.dimension_labels`, a FLAT 128 bytes on every card
    #     (108 of payload, 20 of key). For these fixtures it resolves one entry,
    #     `date`, carrying its own identifier and `label_source:
    #     fallback_identifier`.
    #   * 0472efaf put the client's word into the sentence that names a
    #     dimension, and 3e7ead13 into 25 block titles, the table headers and the
    #     donut unit. Both land in `data`, both are governed content:
    #     `docs/product-architecture/governance.md`, "A client label reaches
    #     every surface that shows the number".
    #
    # THE CONSEQUENCE, STATED RATHER THAN SMOOTHED. `keywords` crossed zero: at
    # -16 its `composition` now travels in the APP channel, so a model calling
    # `get_card` no longer reads the blocks of that card. The REST seam does not
    # show this (`test_cards_integration_seams.py` reads the response before the
    # split), which is exactly why this file exists. Two written rules meet here
    # and only one can win -- the governed label reaching every surface, and the
    # composition reaching the model -- and choosing between them is a product
    # decision, not a test repair. The cheapest 128 bytes on the table are the
    # `dimension_labels` fallback entry, which `cards.dimension_word` and
    # `narrative` BOTH already refuse to trust; removing it from the wire would
    # put `keywords` back at +112, still under the floor.
    #
    # `MARGIN_FLOOR_BYTES` and `MODEL_CHANNEL_MAX_BYTES` are untouched.
    # RE-MEASURED 2026-09-02 (AI-355, arbitrated: the wire drops the
    # `fallback_identifier` entries of `meta.dimension_labels` -- the 128 bytes
    # both readers already refused to trust). `keywords` comes back ABOVE zero:
    # its composition travels in the model channel again. Still under the
    # 336-byte floor, which is what this entry now states.
    "keywords": (0, 336),
    # `attribution` enters this table for the first time. It still FITS -- 170
    # bytes under the ceiling, nothing of it is displaced -- but it no longer has
    # room for one ordinary field, which is the whole statement this file makes.
    "attribution": (100, 336),
}


def _envelope_for(
    template: str,
    rows,
    *,
    extra_patches=(),
    warehouse: str = "core.warehouse.query_daily_report",
):
    with patch(warehouse, return_value=rows):
        stack = [patch(target, return_value=value) for target, value in extra_patches]
        for entered in stack:
            entered.start()
        try:
            _summary, envelope, _uri = cards.get_card(
                [],
                "default",
                template=template,
                metrics=None,
                date_from="2026-07-01",
                date_to="2026-07-10",
            )
        finally:
            for entered in stack:
                entered.stop()
    return envelope


#: (measured name, template, rows, extra warehouse patches, warehouse function).
#: Only cards this repository already has offline fixtures for: inventing rows to
#: measure a margin would measure the fixture, not the card.
_MEASURED_CARDS = [
    ("kpi", "kpi", _sessions_rows(), (), "core.warehouse.query_daily_report"),
    ("keywords", "keywords", _keyword_rows(), (), "core.warehouse.query_daily_report"),
    (
        "keywords-cannibalisation",
        "keywords",
        _cannibalisation_rows(),
        (("core.warehouse.query_composite_positions", _cannibalisation_position_rows()),),
        "core.warehouse.query_daily_report",
    ),
    (
        "attribution",
        "attribution",
        _attribution_seam_rows(),
        (),
        "core.warehouse.query_daily_report",
    ),
    ("dedup", "dedup", _dedup_seam_rows(), (), "core.warehouse.query_dedup_estimate"),
]


def _margin(envelope) -> int:
    return MODEL_CHANNEL_MAX_BYTES - serialized_bytes(envelope)


@pytest.mark.parametrize("name,template,rows,extra,warehouse", _MEASURED_CARDS)
def test_a_card_keeps_room_for_one_more_field(name, template, rows, extra, warehouse):
    envelope = _envelope_for(template, rows, extra_patches=extra, warehouse=warehouse)
    margin = _margin(envelope)

    if name in KNOWN_TIGHT:
        low, high = KNOWN_TIGHT[name]
        assert low <= margin < high, (
            f"card {name!r} was recorded at a margin in [{low}, {high}) bytes on "
            f"2026-08-01 and now measures {margin}. If it grew past "
            f"{MARGIN_FLOOR_BYTES}, remove its entry from KNOWN_TIGHT -- the general "
            "assertion below covers it. If it shrank, something was added to an "
            "envelope that had nothing left to give."
        )
        return

    assert margin >= MARGIN_FLOOR_BYTES, (
        f"card {name!r} is {margin} bytes under the {MODEL_CHANNEL_MAX_BYTES}-byte "
        f"model-channel budget, less than one ordinary field ({MARGIN_FLOOR_BYTES}). "
        "The next field added to this envelope -- anywhere -- will push a payload "
        "into the app channel, and the failure will surface as a card that renders "
        "nothing, two suites away from the change. Decide: trim the envelope, or "
        "accept that this card's composition travels in the app channel and write "
        "that down. Do not lower this floor without saying why."
    )


def test_the_over_budget_card_really_does_lose_its_composition_to_the_app_channel():
    """The consequence, stated rather than inferred from a byte count.

    `keywords` with cannibalisation data exceeds the budget, so the split moves
    its `composition` out of the model channel. A model calling `get_card` for
    that card receives a withheld marker where the blocks should be. Nothing is
    lost -- the app channel carries them -- but the model cannot read the layout
    it is sometimes asked about, and until this test nobody said so.
    """
    from core.model_channel import WITHHELD_MARKER, partition_envelope

    envelope = _envelope_for(
        "keywords",
        _cannibalisation_rows(),
        extra_patches=(
            ("core.warehouse.query_composite_positions", _cannibalisation_position_rows()),
        ),
    )
    model, app_payload = partition_envelope(envelope, tool_name="get_card")

    withheld = model["data"]["composition"]
    assert isinstance(withheld, dict) and withheld.get("withheld") == WITHHELD_MARKER
    assert "composition" in app_payload, "the blocks must still reach the app channel"
