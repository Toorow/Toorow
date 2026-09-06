"""Story 27.9 -- a heading and the numbers under it call one dimension ONE thing.

WHY THIS FILE EXISTS. On 2026-08-22 the render envelope started carrying
`meta.dimension_labels`, so a client who renamed a dimension read their own word
on the numbers. The card templates went on shipping their headings in prose:
`"title": "Users by device"`, hard-coded, on a block bound to `device_category`.
A client who called that dimension "Terminal" therefore read "Terminal" in the
data and "device" in the heading above it -- the same dimension, two names, one
session. That is a bullet of the "Incomplete if" list this story owns.

THE RULE, from docs/product-architecture/governance.md: a block that names a
dimension in its title DECLARES which one, and the composition substitutes the
client's word. Where nobody named it the title stays exactly what the template
shipped -- never a MACHINE-SHAPED identifier, because "Users by device_category"
is precisely the defect the label exists to prevent. The underscore is the line:
`country` and `device` are both identifiers and words, and there the two
coincide with nothing to repair.

NO DIMENSION IS NAMED IN AN ASSERTION BELOW. Everything is derived from what the
shipped templates declare, so a template that starts naming a dimension tomorrow
is judged by these rules without an edit here.
"""

from __future__ import annotations

import re

from core import cards
from core.dimension_conformance import LABEL_SOURCE_CLIENT, LABEL_SOURCE_FALLBACK

_PLACEHOLDER = "{dimension}"


def _declaring_blocks() -> list[tuple[str, dict]]:
    """[(template id, block)] for every shipped block that declares a title dimension."""
    found: list[tuple[str, dict]] = []
    for template in cards.CARD_TEMPLATES:
        for block in template.composition:
            if isinstance(block.get("title_dimension"), dict):
                found.append((template.id, block))
    return found


def _titled_blocks() -> list[tuple[str, dict]]:
    """[(template id, block)] for every shipped block carrying a title at all."""
    return [
        (template.id, block)
        for template in cards.CARD_TEMPLATES
        for block in template.composition
        if isinstance(block.get("title"), str) and block["title"].strip()
    ]


def _context(labels: dict) -> cards._BlockContext:
    return cards._BlockContext(
        current_rows=[],
        prior_rows=[],
        rollup={},
        metric_definitions=None,
        resolved_metrics=[],
        start="2026-08-01",
        end="2026-08-21",
        dimension_labels=labels,
    )


def _client(display_label: str) -> dict:
    return {
        "display_label": display_label,
        "label_source": LABEL_SOURCE_CLIENT,
        "scope_level": "project",
        "description": None,
    }


def test_at_least_one_shipped_block_declares_the_dimension_it_names():
    """A suite that derives everything from an empty list proves nothing."""
    assert _declaring_blocks(), (
        "no shipped block declares a `title_dimension` any more -- either the rule was "
        "removed (then this file goes with it) or a declaration was lost"
    )


def test_a_declaration_names_a_real_canonical_dimension_and_a_readable_default():
    from core.dimension_lineage import known_canonical_dimensions

    known = known_canonical_dimensions()
    for template_id, block in _declaring_blocks():
        declared = block["title_dimension"]
        name = declared.get("name")
        where = f"{template_id}/{block.get('type')}"
        assert name in known, (
            f"{where} declares '{name}', which no shipped manifest conforms -- a title "
            "cannot carry a client word for a dimension no client can name"
        )
        # The block must actually BIND what its title claims to name, or the heading
        # describes a breakdown the block does not perform.
        bound = (block.get("binding") or {}).get("dimensions") or []
        assert name in bound, f"{where} names '{name}' and does not bind it"
        default = declared.get("default")
        assert isinstance(default, str) and default.strip(), f"{where} declares no default"
        assert "_" not in default, (
            f"{where} falls back to '{default}', which is machine-shaped -- the underscore "
            "is the line: a compound the product built for joining is not what a person "
            "reads. Ship the word instead."
        )
        assert _PLACEHOLDER in block["title"], f"{where} declares a dimension it never uses"


def test_no_title_ever_prints_a_machine_shaped_identifier():
    """A compound the product built for joining is never read by a person."""
    from core.dimension_lineage import known_canonical_dimensions

    # ONLY the machine-shaped ones. Several canonical identifiers ARE the word a
    # person reads -- `country`, `device` -- and there the two coincide with nothing
    # to repair. `device_category` is a compound the product built for joining, and a
    # heading that printed it would be speaking the database at somebody.
    machine_shaped = {name for name in known_canonical_dimensions() if "_" in name}
    for template_id, block in _titled_blocks():
        composed = cards._resolve_block_title(block, _context({})) or ""
        offending = sorted(
            name
            for name in machine_shaped
            if re.search(r"\b" + re.escape(name) + r"\b", composed)
        )
        assert not offending, f"{template_id}/{block.get('type')} prints {offending}"


def test_the_client_word_replaces_the_shipped_one():
    for template_id, block in _declaring_blocks():
        name = block["title_dimension"]["name"]
        composed = cards._resolve_block_title(block, _context({name: _client("Terminal")}))
        assert "Terminal" in composed, f"{template_id}: {composed}"
        assert _PLACEHOLDER not in composed


def test_nobody_named_it_keeps_the_prose_the_template_shipped():
    for template_id, block in _declaring_blocks():
        name = block["title_dimension"]["name"]
        default = block["title_dimension"]["default"]
        # The fallback map is what `resolve_report_dimension_labels` returns when no
        # client row exists: the identifier, DECLARED as a fallback. A title that
        # trusted `display_label` blindly would print the identifier from here.
        composed = cards._resolve_block_title(
            block,
            _context(
                {
                    name: {
                        "display_label": name,
                        "label_source": LABEL_SOURCE_FALLBACK,
                        "scope_level": None,
                        "description": None,
                    }
                }
            ),
        )
        assert default in composed, f"{template_id}: {composed}"
        if "_" in name:
            assert name not in composed, (
                f"{template_id} printed the machine-shaped identifier '{name}' from a "
                "FALLBACK entry"
            )


def test_a_block_that_declares_nothing_is_returned_untouched():
    for template_id, block in _titled_blocks():
        if isinstance(block.get("title_dimension"), dict):
            continue
        assert cards._resolve_block_title(block, _context({})) == block["title"], template_id


def test_resolve_block_puts_the_composed_title_on_the_block_it_returns():
    """The composition is what the renderer reads; a title composed and not attached
    would be a repair nobody sees."""
    for _template_id, block in _declaring_blocks():
        name = block["title_dimension"]["name"]
        resolved = cards.resolve_block(block, _context({name: _client("Terminal")}), "")
        assert "Terminal" in resolved["title"]
        # The shipped template is never mutated: two cards in one process would
        # otherwise share one project's vocabulary.
        assert _PLACEHOLDER in block["title"]


# ===========================================================================
# 2026-08-31 -- THE GUARD JUDGED ONLY THE MEMBERS THAT HAD DECLARED THEMSELVES.
#
# Everything above walks `_declaring_blocks()`. A block that named a dimension
# and declared NOTHING was, to this file, a block that named no dimension: it
# was checked for a machine-shaped identifier and for nothing else. Measured
# 2026-08-31 on the shipped registry: 25 titled blocks, 3 declarations, and 5 of
# the remaining 22 named a canonical dimension they bind -- "Top queries" on a
# `query` binding, "Top viewed pages" on `page`. A client who renamed those read
# their word on the numbers and the product's word on the heading, which is the
# defect the declaration exists to prevent, and this file passed.
#
# The lesson has a name in this repository: a guard that judges only its
# self-declared members measures its own copy. So the walk below is over ALL of
# them, and a mention without a declaration is a failure.
#
# WHICH DIMENSIONS IT JUDGES, and why not the others. Only the ones a client can
# NAME -- the canonical identifiers `known_canonical_dimensions()` returns. A
# card also binds tokens no manifest conforms (`connector`, `channel_connector`,
# `video`); no label can ever be stored against those, so a declaration there
# would promise a substitution that could never happen. Their prose is the
# product's own word and it stays.
# ===========================================================================


def _known() -> set[str]:
    from core.dimension_lineage import known_canonical_dimensions

    return known_canonical_dimensions()


def _readable_words(dimension: str, known: set[str]) -> set[str]:
    """Every way a person might write *dimension* in a sentence (DERIVED, never listed).

    Three shapes, and each one is a shape the shipped templates actually used:
      * the identifier itself and the phrase it derives to (`landing_page` ->
        "landing page") -- the literal defect;
      * its HEAD noun, because an English compound is head-final: a
        `landing_page` IS a page, a `session_campaign` IS a campaign, and "Top
        viewed pages" is how a heading names the first;
      * any token that is ITSELF a canonical dimension (`channel` inside
        `channel_connector`), because that token is a word the product already
        treats as a dimension name.
    Each is also matched in its naive plural, which is how a heading names a
    breakdown: "Top queries", never "Top query".
    """
    tokens = dimension.split("_")
    words = {dimension, dimension.replace("_", " "), tokens[-1]}
    words |= {token for token in tokens if token in known}
    plurals = {f"{word}s" for word in words}
    plurals |= {f"{word[:-1]}ies" for word in words if word.endswith("y")}
    return {word for word in words | plurals if word}


def _mentions(text: str, dimension: str, known: set[str]) -> list[str]:
    return sorted(
        word
        for word in _readable_words(dimension, known)
        if re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", text, re.IGNORECASE)
    )


def _labellable(names, known: set[str]) -> list[str]:
    return [name for name in names if name in known]


def _undeclared_title_mentions(known: set[str]) -> list[str]:
    offences: list[str] = []
    for template_id, block in _titled_blocks():
        declared = {
            str(d.get("name") or "") for d in cards.block_dimension_declarations(block)
        }
        bound = (block.get("binding") or {}).get("dimensions") or []
        for name in _labellable(bound, known):
            if name in declared:
                continue
            hits = _mentions(block["title"], name, known)
            if hits:
                offences.append(
                    f"{template_id}/{block.get('type')} title {block['title']!r} names "
                    f"'{name}' as {hits} and declares nothing"
                )
    return offences


def _undeclared_question_mentions(known: set[str]) -> list[str]:
    offences: list[str] = []
    for template in cards.CARD_TEMPLATES:
        declared = {str(d.get("name") or "") for d in template.question_dimensions}
        named = tuple(template.required_dimensions) + tuple(template.optional_dimensions)
        for name in _labellable(named, known):
            if name in declared:
                continue
            hits = _mentions(template.answers_question, name, known)
            if hits:
                offences.append(
                    f"{template.id} question {template.answers_question!r} names "
                    f"'{name}' as {hits} and declares nothing"
                )
    return offences


def test_no_block_title_names_a_dimension_it_does_not_declare():
    """EVERY titled block, not only the ones that already declare."""
    offences = _undeclared_title_mentions(_known())
    assert not offences, "\n".join(offences)


def test_no_question_names_a_dimension_it_does_not_declare():
    """A question is a surface: it reaches the model channel and `data.answers_question`."""
    offences = _undeclared_question_mentions(_known())
    assert not offences, "\n".join(offences)


def test_the_guard_refuses_the_shape_it_was_written_for():
    """RED ON MUTATION, proven here rather than asserted.

    Without this, a later narrowing of `_readable_words` could make both walks
    above blind to the very class they were built for and still pass -- green by
    absence, the failure mode this repository names by that name.
    """
    known = _known()
    dimension = sorted(name for name in known if "_" in name)[0]
    head = dimension.split("_")[-1]
    # A heading naming a bound dimension by its head noun, in the plural.
    assert _mentions(f"Top {head}s", dimension, known), (
        f"the walk cannot see '{head}s' as a mention of '{dimension}' -- it would "
        "pass every heading that names a compound by its head noun"
    )
    # And the identifier itself, which is the literal defect.
    assert _mentions(f"Users by {dimension}", dimension, known)
    # The walks READ those mentions: a synthetic registry carrying one is refused.
    offender = cards.CardTemplate(
        id="synthetic",
        title="Synthetic",
        answers_question=f"How are my {head}s doing?",
        widget_uri="ui://synthetic",
        optional_dimensions=(dimension,),
        composition=(
            {
                "type": "table",
                "title": f"Top {head}s",
                "binding": {"metrics": ["clicks"], "dimensions": [dimension]},
            },
        ),
    )
    shipped = list(cards.CARD_TEMPLATES)
    cards.CARD_TEMPLATES.append(offender)
    try:
        assert _undeclared_title_mentions(known), "an undeclared heading passed"
        assert _undeclared_question_mentions(known), "an undeclared question passed"
    finally:
        cards.CARD_TEMPLATES[:] = shipped
    assert not _undeclared_title_mentions(known)
    assert not _undeclared_question_mentions(known)


def test_a_question_declaration_names_a_real_dimension_and_a_word_in_its_prose():
    """A declaration that substitutes nothing is a declaration nobody reads."""
    known = _known()
    for template in cards.CARD_TEMPLATES:
        for declaration in template.question_dimensions:
            name = declaration.get("name")
            default = declaration.get("default")
            where = f"{template.id} question"
            assert name in known, (
                f"{where} declares '{name}', which no shipped manifest conforms -- no "
                "client can name it, so no client word can ever reach this sentence"
            )
            assert isinstance(default, str) and default.strip(), (
                f"{where} declares no default word for '{name}'"
            )
            assert "_" not in default, (
                f"{where} falls back to '{default}', which is machine-shaped"
            )
            assert re.search(
                r"(?<![\w])" + re.escape(default) + r"(?![\w])",
                template.answers_question,
            ), (
                f"{where} declares '{default}' and the sentence never says it -- the "
                "client's word would replace nothing"
            )


def test_a_client_word_reaches_the_question():
    known = _known()
    declaring = [t for t in cards.CARD_TEMPLATES if t.question_dimensions]
    assert declaring, "no shipped question declares a dimension any more"
    machine_shaped = {name for name in known if "_" in name}
    for template in declaring:
        labels = {
            str(d["name"]): _client(f"Word{index}")
            for index, d in enumerate(template.question_dimensions)
        }
        composed = template.composed_question(labels)
        for index, declaration in enumerate(template.question_dimensions):
            assert f"Word{index}" in composed, f"{template.id}: {composed}"
            # And the prose it replaced is gone, so one sentence says one thing.
            assert not re.search(
                r"(?<![\w])" + re.escape(str(declaration["default"])) + r"(?![\w])",
                composed,
            ), f"{template.id} still ships '{declaration['default']}': {composed}"
        assert not [
            name
            for name in machine_shaped
            if re.search(r"\b" + re.escape(name) + r"\b", composed)
        ], composed


def test_nobody_named_it_keeps_the_question_the_template_shipped():
    for template in cards.CARD_TEMPLATES:
        fallback = {
            str(d["name"]): {
                "display_label": str(d["name"]),
                "label_source": LABEL_SOURCE_FALLBACK,
                "scope_level": None,
                "description": None,
            }
            for d in template.question_dimensions
        }
        assert template.composed_question(None) == template.answers_question
        assert template.composed_question(fallback) == template.answers_question


def test_the_catalog_round_trips_the_declaration():
    """A rebuilt template that lost its declaration would drop the client's word
    silently, and `template_from_entry` is what a Project's own catalog resolves
    through."""
    for template in cards.CARD_TEMPLATES:
        rebuilt = cards.template_from_entry(template.to_catalog_entry())
        assert [dict(d) for d in rebuilt.question_dimensions] == [
            dict(d) for d in template.question_dimensions
        ], template.id
        labels = {
            str(d["name"]): _client("Terminal") for d in template.question_dimensions
        }
        assert rebuilt.composed_question(labels) == template.composed_question(labels)


# ===========================================================================
# THE OTHER SURFACES OF A CARD -- a header and a donut's unit (2026-08-31).
#
# The rule was delivered on the heading, then on the sentence, and stopped
# there. Measured on the shipped resolvers: `_resolve_table` set its first
# column's label to `dimension` RAW, so a client who called `device_category`
# "Terminal" read "Terminal" on the heading and `device_category` on the column
# one line below it; `_resolve_donut` shipped the same identifier as the unit
# the shell prints at the centre of the donut. Same dimension, same card, two
# names -- which is the whole of what the label exists to prevent.
# ===========================================================================


def _rows(dimension: str, metric: str = "active_users") -> list[dict]:
    return [
        {
            "date": "2026-08-02",
            "connector": "google-analytics",
            "metric": metric,
            "breakdown_dimension": dimension,
            "breakdown_value": value,
            "value": float(amount),
            "pull_id": "pull_1",
            "loaded_at": "2026-08-02T00:00:00",
        }
        for value, amount in (("alpha", 30), ("beta", 10))
    ]


def _rows_context(rows, metrics, labels):
    ctx = _context(labels)
    return cards._BlockContext(
        current_rows=rows,
        prior_rows=[],
        rollup=ctx.rollup,
        metric_definitions=None,
        resolved_metrics=metrics,
        start=ctx.start,
        end=ctx.end,
        dimension_labels=labels,
    )


def _first_column_labels(labels: dict) -> dict[str, str]:
    """{bound dimension -> the header the table block prints for it}."""
    printed: dict[str, str] = {}
    for _template_id, block in _titled_blocks():
        if block.get("type") != "table":
            continue
        binding = block.get("binding") or {}
        if binding.get("cannibalisation") or binding.get("source"):
            continue
        for dimension in binding.get("dimensions") or []:
            metrics = binding.get("metrics") or []
            metrics = [metrics] if isinstance(metrics, str) else list(metrics)
            payload = cards._resolve_table(
                block, _rows_context(_rows(dimension, metrics[0]), metrics, labels)
            )
            columns = payload.get("columns") or []
            if columns:
                printed[dimension] = str(columns[0].get("label"))
            break
    return printed


def test_a_table_header_never_prints_a_machine_shaped_identifier():
    """The column beside the heading obeys the heading's rule."""
    known = _known()
    printed = _first_column_labels({})
    assert printed, "no shipped table block resolved a first column -- nothing measured"
    offenders = {
        dimension: label
        for dimension, label in printed.items()
        if "_" in label and label in known
    }
    assert not offenders, (
        f"table headers speaking the database: {offenders}. A compound the product "
        "built for joining is not what a person reads."
    )


def test_a_table_header_speaks_the_client_word():
    printed_before = _first_column_labels({})
    dimensions = sorted(printed_before)
    labels = {dimension: _client("Terminal") for dimension in dimensions}
    printed_after = _first_column_labels(labels)
    assert printed_after, dimensions
    for dimension, label in printed_after.items():
        assert label == "Terminal", (
            f"{dimension} header is {label!r} -- a client named it and the header "
            "kept the product's word"
        )


def _donut_blocks() -> list[tuple[str, dict]]:
    return [
        (template.id, block)
        for template in cards.CARD_TEMPLATES
        for block in template.composition
        if block.get("type") == "donut"
    ]


def test_a_donut_carries_the_word_its_centre_prints():
    """`dimension` is the join key and stays; `dimension_label` is what a person reads."""
    known = _known()
    blocks = _donut_blocks()
    assert blocks, "no shipped donut block -- nothing measured"
    for template_id, block in blocks:
        for dimension in (block.get("binding") or {}).get("dimensions") or []:
            metric = (block.get("binding") or {}).get("metrics")
            rows = _rows(dimension, str(metric))
            unnamed = cards._resolve_donut(block, _rows_context(rows, [str(metric)], {}))
            assert unnamed["dimension"] == dimension, template_id
            assert unnamed["dimension_label"], f"{template_id}: no word for the centre"
            assert not (
                "_" in unnamed["dimension_label"] and unnamed["dimension_label"] in known
            ), f"{template_id} prints {unnamed['dimension_label']!r} at the centre"
            named = cards._resolve_donut(
                block, _rows_context(rows, [str(metric)], {dimension: _client("Terminal")})
            )
            assert named["dimension_label"] == "Terminal", template_id


def test_an_empty_donut_still_declares_the_key_the_shell_reads():
    """A payload that omits the key on its empty path teaches the shell to fall back
    to the identifier, which is the defect wearing an empty state."""
    empty = cards._resolve_donut(
        {"type": "donut", "binding": {"metrics": "active_users", "dimensions": ["nope"]}},
        _rows_context([], ["active_users"], {}),
    )
    assert "dimension_label" in empty and empty["dimension_label"] is None
