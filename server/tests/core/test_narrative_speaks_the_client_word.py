"""Story 27.9 -- a SENTENCE and the heading above it call one dimension ONE thing.

WHY THIS FILE EXISTS. `test_card_titles_speak_the_client_word.py` closed the
heading; the envelope had already been carrying `meta.dimension_labels` since
2026-08-22. The prose underneath was still ours: a client who renamed
`device_category` to "Terminal" read "Terminal" on the numbers, "Terminal" on the
heading -- and "Segment dominant" in the comment between them. Measured on
2026-08-24: `grep -c dimension_label server/core/narrative.py` -> 0.
governance.md lists "a narrative sentence" beside a chart and an axis in the same
"Incomplete if"; one surface armed and its neighbour not is the same defect.

WHERE THE RESOLUTION LIVES, AND WHY NOT HERE. The narration reads nothing (AD-1,
`scripts/check_narrative_no_raw.py`). `cards.get_card` resolves the map ONCE and
hands the SAME object to the block titles and to the comment builder, so the two
cannot answer differently. The end-to-end test at the bottom is the one that
proves that sentence; the unit tests above it say what each half does.

THE FALLBACK IS THE PROSE, NOT THE IDENTIFIER. "Premier device_category" is
precisely what the label exists to prevent, so a map that merely falls back to
the identifier must leave the shipped sentence byte-identical.
"""

from __future__ import annotations

import inspect
import os
import re

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from core import (
    narrative,  # noqa: E402
    summarizer,  # noqa: E402
)
from core.dimension_conformance import (  # noqa: E402
    LABEL_SOURCE_CLIENT,
    LABEL_SOURCE_FALLBACK,
)
from core.dimension_lineage import known_canonical_dimensions  # noqa: E402


def _client(display_label: str) -> dict:
    return {
        "display_label": display_label,
        "label_source": LABEL_SOURCE_CLIENT,
        "scope_level": "project",
        "description": None,
    }


def _fallback(dimension: str) -> dict:
    """What `resolve_report_dimension_labels` returns when NO client row exists:
    the stable identifier, DECLARED as a fallback. A sentence that trusted
    `display_label` blindly would print the identifier from here."""
    return {
        "display_label": dimension,
        "label_source": LABEL_SOURCE_FALLBACK,
        "scope_level": None,
        "description": None,
    }


#: Only the identifiers a person must never read. Several canonical names ARE the
#: word -- `country`, `page`, `query` -- and there the two coincide with nothing
#: to repair. The underscore is the line, exactly as for the titles.
def _machine_shaped() -> set[str]:
    return {name for name in known_canonical_dimensions() if "_" in name}


def _offending_identifiers(text: str) -> list[str]:
    return sorted(
        name
        for name in _machine_shaped()
        if re.search(r"\b" + re.escape(name) + r"\b", text)
    )


# ---------------------------------------------------------------------------
# The one substitution rule, on its own.
# ---------------------------------------------------------------------------


def test_the_client_word_wins_when_a_client_named_the_dimension():
    assert narrative.dimension_word(
        {"device_category": _client("Terminal")}, "device_category", "Segment"
    ) == "Terminal"


@pytest.mark.parametrize(
    "labels",
    [
        None,
        {},
        {"device_category": _fallback("device_category")},
        {"country": _client("Marché")},  # a label for ANOTHER dimension
        {"device_category": {"display_label": "", "label_source": LABEL_SOURCE_CLIENT}},
    ],
    ids=["no-map", "empty-map", "fallback-entry", "other-dimension", "empty-label"],
)
def test_nobody_named_it_keeps_the_prose_the_sentence_ships(labels):
    """Four ways of having no client word, one answer -- and never the identifier."""
    assert narrative.dimension_word(labels, "device_category", "Segment") == "Segment"


# ---------------------------------------------------------------------------
# The builders. Each sentence that names a dimension is pinned twice: the client
# named it, and nobody did.
# ---------------------------------------------------------------------------

_KEYWORDS_BLOCKS = {
    "bar": {"bars": [{"label": "chaussures", "value": -2.0, "direction": "up"}]},
    "cannibalisation": {"rows": [{"_dim": "chaussures"}, {"_dim": "chaussures"}]},
}
_USERTYPES_BLOCKS = {
    "donut_user_type": {"slices": [{"label": "Fidèles", "pct": 61.1}]},
    "bar": {"bars": [{"label": "FR", "value": 72063.0}]},
}
_USERTYPES_DEVICE_ONLY = {
    "donut_device_category": {"slices": [{"label": "mobile", "pct": 55.0}]},
}
_JOURNEY_BLOCKS = {
    "funnel": {
        "steps": [
            {"label": "Sessions", "value": 1000.0, "rate": 1.0},
            {"label": "Conversions", "value": 120.0, "rate": 0.12},
        ],
        "overall_rate": 0.12,
    },
    "bar": {"bars": [{"label": "/accueil", "value": 1200.0}]},
}
_ATTRIBUTION_BLOCKS = {
    "bar": {"bars": [{"label": "cpc / google", "value": 1890.0}]},
}

#: (builder, block_data, canonical dimension, shipped prose, client word).
#: Every entry is a sentence a person reads; the dimension is the one the card's
#: own binding declares (cards.CARD_TEMPLATES), not a name chosen here.
_NAMED_DIMENSIONS = [
    (narrative.build_keywords_comment, _KEYWORDS_BLOCKS, "query", "Requête", "Mot-clé"),
    (narrative.build_usertypes_comment, _USERTYPES_BLOCKS, "user_type", "Type", "Profil"),
    (narrative.build_usertypes_comment, _USERTYPES_BLOCKS, "country", "pays", "Marché"),
    (
        narrative.build_usertypes_comment,
        _USERTYPES_DEVICE_ONLY,
        "device_category",
        "Segment",
        "Terminal",
    ),
    (
        narrative.build_journey_comment,
        _JOURNEY_BLOCKS,
        "landing_page",
        "Page d'entrée",
        "Atterrissage",
    ),
    (
        narrative.build_attribution_comment,
        _ATTRIBUTION_BLOCKS,
        "session_source_medium",
        "Canal",
        "Levier",
    ),
]

_IDS = [f"{builder.__name__}:{dim}" for builder, _b, dim, _d, _w in _NAMED_DIMENSIONS]


def _comment(builder, block_data, labels):
    return builder(
        block_data=block_data,
        rollup={"conversions": {"value": 120.0}, "impressions": {"value": 40000.0}},
        context_events=[],
        pull_ids=["pull_01TEST"],
        dimension_labels=labels,
    )


@pytest.mark.parametrize(
    ("builder", "block_data", "dimension", "shipped", "word"), _NAMED_DIMENSIONS, ids=_IDS
)
def test_the_client_word_reaches_the_sentence(builder, block_data, dimension, shipped, word):
    text = _comment(builder, block_data, {dimension: _client(word)})
    assert word in text, text
    assert shipped not in text, (
        f"the sentence kept shipping '{shipped}' beside the client's '{word}' -- "
        "one dimension, two names, one card"
    )


@pytest.mark.parametrize(
    ("builder", "block_data", "dimension", "shipped", "word"), _NAMED_DIMENSIONS, ids=_IDS
)
def test_nobody_named_it_and_the_sentence_is_byte_identical(
    builder, block_data, dimension, shipped, word
):
    """The fallback map and no map at all say the same thing: the shipped prose.

    Byte-identical on purpose -- a substitution that ALMOST restores the sentence
    is a copy change nobody asked for.
    """
    unlabelled = _comment(builder, block_data, None)
    assert shipped in unlabelled, unlabelled
    assert _comment(builder, block_data, {dimension: _fallback(dimension)}) == unlabelled


@pytest.mark.parametrize(
    ("builder", "block_data", "dimension", "shipped", "word"), _NAMED_DIMENSIONS, ids=_IDS
)
def test_a_sentence_never_prints_a_machine_shaped_identifier(
    builder, block_data, dimension, shipped, word
):
    """Not even from a fallback entry, which carries the identifier as its label."""
    for labels in (None, {dimension: _fallback(dimension)}, {dimension: _client(word)}):
        text = _comment(builder, block_data, labels)
        assert not _offending_identifiers(text), text


# ---------------------------------------------------------------------------
# The registry: uniform, or the next builder is a TypeError (review-9 F-10).
# ---------------------------------------------------------------------------


def test_every_registered_builder_accepts_the_label_map():
    """The generic dispatch passes `dimension_labels` to WHATEVER is registered.

    A builder that does not accept it raises TypeError on a real card; one that
    accepts and ignores it says why at its own signature.
    """
    for card_id, builder in narrative.CARD_COMMENT_BUILDERS.items():
        params = inspect.signature(builder).parameters
        assert "dimension_labels" in params, (
            f"'{card_id}' is dispatched with dimension_labels and does not accept it"
        )


def test_the_registry_is_not_empty():
    """A suite that derives everything from an empty dict proves nothing."""
    assert narrative.CARD_COMMENT_BUILDERS


# ---------------------------------------------------------------------------
# The two builders that name NO dimension. Their absence of a label map is a
# measurement, not an oversight -- so it is measured here rather than asserted in
# a docstring nobody re-runs.
# ---------------------------------------------------------------------------


def test_the_generic_narrative_names_no_dimension():
    text = narrative.build_narrative(
        project_id="proj_EXAMPLE",
        report_id="report_EXAMPLE",
        rollup={
            "clicks": {"value": 1200, "delta_pct": "+4%", "period": "période précédente"},
            "impressions": {"value": 90000},
            "screen_page_views": {"value": 4200},
        },
        context_events=[],
        alerts=[],
        as_of=None,
        narrative_prompt=None,
    )
    assert not _offending_identifiers(text), (
        "a generic narrative line now names a dimension -- it must receive the "
        "resolved label map, exactly like the per-card builders: " + text
    )


def test_the_daily_summary_names_no_dimension_even_on_broken_down_rows():
    """`summarizer` sums rows that CARRY a breakdown and never names it.

    So there is nothing for a client label to rename there, and threading a map
    into it would be a call site that looks armed and is not. The day a line
    names the breakdown, this goes red and the map has to arrive with it.
    """
    rows = [
        {
            "date": "2026-08-01",
            "connector": "my-connector",
            "metric": metric,
            "breakdown_dimension": "device_category",
            "breakdown_value": value,
            "value": 100.0,
            "pull_id": "pull_01TEST",
            "loaded_at": "2026-08-02T00:00:00",
        }
        for metric in ("sessions", "active_users", "conversions")
        for value in ("desktop", "mobile")
    ]
    text = summarizer.build_daily_report_summary(
        rows,
        {"start": "2026-08-01", "end": "2026-08-07"},
        ["my-connector"],
        "proj_EXAMPLE",
    )
    assert not _offending_identifiers(text), text
    # And the empty state, which is the branch the daily report actually reaches
    # (`reporting_mcp` calls the summarizer only when there are no rows at all).
    empty = summarizer.build_daily_report_summary(
        [], {"start": "2026-08-01", "end": "2026-08-07"}, [], "proj_EXAMPLE"
    )
    assert not _offending_identifiers(empty), empty


# ---------------------------------------------------------------------------
# ONE map, both surfaces. This is the claim the story makes; the rest is parts.
# ---------------------------------------------------------------------------


def _usertypes_rows() -> list[dict]:
    rows = []
    for day in ("2026-07-02", "2026-07-04"):
        for metric in ("active_users", "sessions"):
            for dimension, value in (
                ("device_category", "mobile"),
                ("device_category", "desktop"),
                ("country", "FR"),
            ):
                rows.append(
                    {
                        "date": day,
                        "connector": "ga4",
                        "metric": metric,
                        "breakdown_dimension": dimension,
                        "breakdown_value": value,
                        "value": 1000.0 if value == "mobile" else 400.0,
                        "pull_id": "pull_01TEST",
                        "loaded_at": f"{day}T00:00:00",
                    }
                )
    return rows


def test_the_heading_and_the_comment_under_it_read_the_same_map():
    """get_card resolves the labels ONCE; both surfaces speak the client's word.

    Two resolutions on one card would be two answers free to disagree, which is
    the defect `governance.md` names for a screen and which holds identically for
    a card.
    """
    labels = {
        "device_category": _client("Terminal"),
        "country": _client("Marché"),
    }
    with (
        patch("core.warehouse.query_daily_report", return_value=_usertypes_rows()),
        patch(
            "core.dimension_lineage.resolve_report_dimension_labels",
            return_value=labels,
        ),
    ):
        from core import cards as cards_module

        _summary, envelope, _uri = cards_module.get_card(
            ["ga4"],
            "proj_EXAMPLE",
            metrics=["active_users", "sessions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )

    data = envelope["data"]
    assert data["card_id"] == "usertypes", data["card_id"]
    titles = [b.get("title") for b in data["composition"] if b.get("title")]
    assert any("Terminal" in (t or "") for t in titles), titles
    comment = data["rendered_comment"]
    assert "Terminal" in comment or "Marché" in comment, comment
    assert "Segment dominant" not in comment, comment
    assert not _offending_identifiers(comment), comment
    # The envelope carries the same map it composed both surfaces from.
    assert envelope["meta"]["dimension_labels"] == labels
