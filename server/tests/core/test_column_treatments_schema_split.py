"""Story 70.1 -- the seventh word, and what a schema set may not say.

WHAT THIS FILE PINS. `Split by declared schema` is one entry carrying a SET of
positional layouts, a detector that picks one per ROW, a separator and a declared
remainder. This file pins the word itself, the split of one value from the left,
the provenance every row carries, and the refusals that keep a declaration from
becoming an immutable mapping version nobody could read back.

WHY THE REFUSALS ARE THE HALF THAT MATTERS. A mapping version is immutable and a
derivation applies at read: a declaration that is wrong is wrong for every row it
ever touches, and the repair is a NEW version rather than an edit. The cheapest
moment to say no is before the append -- which is the same argument story 60.6
made for joins and splits, at the scale a schema set has.

The placement codes here are generic on purpose: no client identifier belongs in
this repository, fixtures included.
"""

from __future__ import annotations

import copy

import pytest
from core.column_treatments import (
    NOT_DECIDED,
    SPLIT_BY_DECLARED_SCHEMA,
    ColumnTreatmentError,
    describe_columns,
    normalize_treatments,
    produced_columns,
    provenance_columns,
    read_schema_split,
    schema_split_columns,
    schema_split_targets,
    split_by_declared_schema,
    split_row_by_declared_schema,
)


def _schema_set(**overrides) -> dict:
    entry = {
        "name": "placement_code",
        "source": "Placement code",
        "separator": "_",
        "remainder": "placement_remainder",
        "detection": {
            "method": "anchor_min_offset",
            "anchor_tokens": ["PUBALPHA", "PUBBETA"],
        },
        "layouts": [
            {
                "name": "full",
                "anchor_offset": 4,
                "targets": [
                    "contract",
                    "fiscal_year",
                    "market",
                    "objective",
                    "publisher",
                    "channel",
                    "creative_format",
                ],
            },
            {
                "name": "short",
                "anchor_offset": 2,
                "targets": ["contract", "market", "publisher", "channel"],
            },
        ],
    }
    entry.update(copy.deepcopy(overrides))
    return entry


def _field(field_id: str, *, canonical=None, status="confirmed") -> dict:
    return {
        "field_id": field_id,
        "physical_type": "string",
        "profile": {
            "nullable": False,
            "unique": False,
            "cardinality_signal": "high",
            "sample_values": ["CON_FY26_EU_AWARENESS_PUBALPHA_DISPLAY_BANNER"],
            "confidence": 0.9,
        },
        "suggestion": {
            "semantic_role": "dimension",
            "aggregation": "none",
            "non_additive": False,
            "currency": "unknown",
            "sensitivity": "none",
            "status": "suggested",
            "evidence": [],
        },
        "binding": {
            "canonical_target": canonical,
            "mdm_target": None,
            "status": status,
            "blocking_reason": None,
            "confirmed_by": "owner@example.com",
            "confirmed_reason": "reviewed in the Mapping tab",
        },
    }


def _payload(fields: list[dict], schema_splits: list[dict] | None = None) -> dict:
    payload: dict = {"fields": fields}
    if schema_splits is not None:
        payload["column_treatments"] = {
            "joins": [],
            "splits": [],
            "schema_splits": schema_splits,
        }
    return payload


# ---------------------------------------------------------------------------
# The word
# ---------------------------------------------------------------------------


def test_the_seventh_word_has_one_writer_and_carries_no_n():
    """A schema set has layouts of different widths, so a number in the word
    would have to pick one of them and be wrong for the rows that took another."""
    assert split_by_declared_schema() == SPLIT_BY_DECLARED_SCHEMA == "Split by declared schema"


def test_the_source_column_of_a_schema_set_carries_the_seventh_word():
    payload = _payload([_field("Placement code")], [_schema_set()])

    rows = describe_columns(payload)

    assert rows[0]["treatment"] == SPLIT_BY_DECLARED_SCHEMA
    # It contributes to its provenance AND to every concept a layout can feed --
    # the inventory of the column stays whole, as it does for a join.
    assert rows[0]["contributes_to"][:4] == [
        "placement_code_layout",
        "placement_code_token_count",
        "placement_code_token_count_matches_schema",
        "placement_remainder",
    ]
    assert "creative_format" in rows[0]["contributes_to"]


def test_a_column_no_schema_set_names_keeps_its_honest_absence():
    payload = _payload([_field("Placement code"), _field("Notes")], [_schema_set()])

    rows = {row["field_id"]: row for row in describe_columns(payload)}

    assert rows["Notes"]["treatment"] == NOT_DECIDED


# ---------------------------------------------------------------------------
# The split of ONE value, from the left
# ---------------------------------------------------------------------------


def test_the_detector_picks_a_different_layout_for_two_different_rows():
    parsed = read_schema_split(_schema_set())

    wide = split_row_by_declared_schema(
        parsed, "CON_FY26_EU_AWARENESS_PUBALPHA_DISPLAY_BANNER"
    )
    narrow = split_row_by_declared_schema(parsed, "CON_EU_PUBBETA_VIDEO")

    assert wide["layout"] == "full"
    assert wide["token_count"] == 7
    assert wide["token_count_matches_schema"] is True
    assert wide["values"]["creative_format"] == "BANNER"

    assert narrow["layout"] == "short"
    assert narrow["token_count"] == 4
    assert narrow["token_count_matches_schema"] is True
    assert narrow["values"] == {
        "contract": "CON",
        "market": "EU",
        "publisher": "PUBBETA",
        "channel": "VIDEO",
    }
    # The row took ONE layout: nothing is borrowed from the other one.
    assert "creative_format" not in narrow["values"]


def test_what_exceeds_the_last_position_goes_to_the_declared_remainder():
    parsed = read_schema_split(_schema_set())

    row = split_row_by_declared_schema(
        parsed, "CON_FY26_EU_AWARENESS_PUBALPHA_DISPLAY_BANNER_WAVE2_RETARGET"
    )

    assert row["layout"] == "full"
    assert row["token_count"] == 9
    assert row["token_count_matches_schema"] is False
    assert row["remainder"] == "WAVE2_RETARGET"
    assert row["values"]["creative_format"] == "BANNER"


def test_a_row_that_fits_exactly_has_no_remainder_rather_than_an_empty_one():
    """An empty string reads as "there was something there and it was blank"."""
    parsed = read_schema_split(_schema_set())

    assert split_row_by_declared_schema(parsed, "CON_EU_PUBBETA_VIDEO")["remainder"] is None


def test_a_row_shorter_than_its_layout_is_readable_and_says_so():
    """Nothing is repaired: the missing positions are empty and the count is the
    count the row really had."""
    parsed = read_schema_split(_schema_set())

    row = split_row_by_declared_schema(parsed, "CON_FY26_EU_AWARENESS_PUBALPHA")

    assert row["layout"] == "full"
    assert row["token_count"] == 5
    assert row["token_count_matches_schema"] is False
    assert row["values"]["publisher"] == "PUBALPHA"
    assert row["values"]["channel"] is None
    assert row["values"]["creative_format"] is None


def test_a_row_no_layout_detects_stays_whole_and_still_carries_its_count():
    parsed = read_schema_split(_schema_set())

    row = split_row_by_declared_schema(parsed, "LEGACY_CODE_WITHOUT_A_PUBLISHER")

    assert row["layout"] is None
    assert row["values"] == {}
    assert row["remainder"] is None
    assert row["token_count_matches_schema"] is None
    # Countable, and returned as it was collected.
    assert row["token_count"] == 5
    assert row["undetected_value"] == "LEGACY_CODE_WITHOUT_A_PUBLISHER"


def test_an_anchor_closer_to_the_left_than_any_layout_declares_is_not_a_layout():
    """MIN(offset) is taken over EVERY offset, not over the declared ones only.

    Scanning past it would find the second-best layout, which is the "faux qui a
    l'air juste" the story refuses by name.
    """
    parsed = read_schema_split(_schema_set())

    row = split_row_by_declared_schema(parsed, "PUBALPHA_CON_EU_DISPLAY")

    assert row["layout"] is None
    assert row["token_count"] == 4


def test_a_value_that_is_not_a_string_is_not_a_zero_token_row():
    """No value read is a different fact from a value read as empty."""
    parsed = read_schema_split(_schema_set())

    assert split_row_by_declared_schema(parsed, None)["token_count"] is None
    assert split_row_by_declared_schema(parsed, "")["token_count"] == 1


# ---------------------------------------------------------------------------
# What a schema set produces
# ---------------------------------------------------------------------------


def test_the_three_provenance_columns_are_derived_from_the_set_name():
    assert provenance_columns(read_schema_split(_schema_set())) == {
        "layout": "placement_code_layout",
        "token_count": "placement_code_token_count",
        "token_count_matches_schema": "placement_code_token_count_matches_schema",
    }


def test_two_layouts_naming_one_concept_produce_ONE_column():
    """They are alternatives for the same row, never rivals: `contract` sits at
    position 0 in both layouts and `market` at 2 and 1, and both are one column
    whose value depends on the layout the row took."""
    parsed = read_schema_split(_schema_set())

    targets = schema_split_targets(parsed)

    assert targets.count("contract") == 1
    assert targets.count("market") == 1
    assert targets == [
        "contract",
        "fiscal_year",
        "market",
        "objective",
        "publisher",
        "channel",
        "creative_format",
    ]


def test_a_produced_column_names_the_column_it_came_from_provenance_included():
    payload = _payload([_field("Placement code")], [_schema_set()])

    produced = {entry["target"]: entry for entry in produced_columns(payload)}

    assert produced["creative_format"] == {
        "target": "creative_format",
        "kind": "schema_split",
        "sources": ["Placement code"],
    }
    assert produced["placement_remainder"]["kind"] == "schema_split"
    assert produced["placement_code_layout"] == {
        "target": "placement_code_layout",
        "kind": "schema_split_provenance",
        "sources": ["Placement code"],
    }
    assert len(produced) == len(schema_split_columns(read_schema_split(_schema_set())))


# ---------------------------------------------------------------------------
# The refusals -- before the version exists
# ---------------------------------------------------------------------------


def _refusal(entry: dict, fields: list[dict] | None = None) -> ColumnTreatmentError:
    payload = _payload(fields or [_field("Placement code")], [entry])
    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)
    return raised.value


def test_a_layout_with_no_position_is_refused():
    """An empty layout describes nothing and would still be detectable."""
    entry = _schema_set()
    entry["layouts"][1]["targets"] = []

    assert _refusal(entry).code == "treatment_schema_layout_empty"


def test_a_set_with_no_layout_at_all_is_refused():
    assert _refusal(_schema_set(layouts=[])).code == "treatment_schema_needs_a_layout"


def test_one_layout_feeding_one_concept_from_two_positions_is_refused():
    """Neither position can win in silence."""
    entry = _schema_set()
    entry["layouts"][1]["targets"] = ["contract", "market", "publisher", "market"]

    error = _refusal(entry)

    assert error.code == "treatment_target_claimed_twice"
    assert error.detail["target"] == "market"


def test_two_layouts_claiming_one_anchor_offset_are_refused():
    """The minimum offset would designate both, and the row has no layout it
    could name."""
    entry = _schema_set()
    entry["layouts"][0]["anchor_offset"] = 2

    error = _refusal(entry)

    assert error.code == "treatment_schema_anchor_ambiguous"
    assert error.detail["anchor_offset"] == 2
    assert error.detail["layouts"] == ["full", "short"]


def test_an_anchor_offset_outside_its_own_layout_is_refused():
    """It would key the detection on a position the layout does not describe."""
    entry = _schema_set()
    entry["layouts"][1]["anchor_offset"] = 9

    assert _refusal(entry).code == "treatment_schema_anchor_offset_invalid"


def test_two_layouts_carrying_one_name_are_refused():
    entry = _schema_set()
    entry["layouts"][1]["name"] = "full"

    assert _refusal(entry).code == "treatment_schema_layout_name_claimed_twice"


def test_an_anchor_token_containing_the_separator_is_refused():
    """A value carrying the separator is never ONE token, so it could never be
    found at an offset."""
    entry = _schema_set()
    entry["detection"]["anchor_tokens"] = ["PUB_ALPHA"]

    assert _refusal(entry).code == "treatment_schema_anchor_token_invalid"


def test_a_detector_with_no_anchor_token_is_refused():
    entry = _schema_set()
    entry["detection"]["anchor_tokens"] = []

    assert _refusal(entry).code == "treatment_schema_anchor_token_invalid"


def test_an_unknown_detection_method_is_refused():
    entry = _schema_set()
    entry["detection"]["method"] = "longest_layout_wins"

    assert _refusal(entry).code == "treatment_schema_detection_unknown"


def test_an_empty_separator_is_refused():
    """It would make every value a single token, so no layout could apply."""
    assert _refusal(_schema_set(separator="")).code == "treatment_schema_separator_invalid"


def test_a_set_with_no_declared_remainder_is_refused():
    """Overflow truncated in silence is a value that disappears without anybody
    being told."""
    assert _refusal(_schema_set(remainder="")).code == "treatment_schema_remainder_missing"


def test_a_set_name_that_could_not_be_a_column_prefix_is_refused():
    assert _refusal(_schema_set(name="Placement Code")).code == "treatment_schema_name_invalid"


def test_a_target_that_would_break_its_own_quoting_is_refused():
    """A target becomes a quoted warehouse column. Quoting is not escaping: a name
    carrying a double quote would close its alias and let the rest execute at
    read. Refused at the declaration, because the stored version is immutable."""
    entry = _schema_set()
    entry["layouts"][0]["targets"][2] = 'leak", (SELECT secret FROM vault LIMIT 1) AS "pwn'
    error = _refusal(entry)
    assert error.code == "treatment_schema_column_name_invalid"
    assert error.detail["target"].startswith("leak")


def test_a_remainder_that_would_break_its_own_quoting_is_refused():
    """The same, for the remainder: it is a column name too."""
    error = _refusal(_schema_set(remainder='r", 1 AS "x'))
    assert error.code == "treatment_schema_column_name_invalid"
    assert error.detail["remainder"].startswith("r")


def test_a_target_carrying_a_space_never_reaches_a_stored_version():
    """The immutable-append guard must refuse it, not just the row-level reader: a
    version that breaks its quoting can neither run nor be re-edited once stored.
    `normalize_treatments` is the gate `save_field_mapping` runs before a version
    exists."""
    entry = _schema_set()
    entry["layouts"][1]["targets"][1] = "has a space"
    with pytest.raises(ColumnTreatmentError) as caught:
        normalize_treatments(_payload([_field("Placement code")], schema_splits=[entry]))
    assert caught.value.code == "treatment_schema_column_name_invalid"


def test_a_set_naming_a_column_the_mapping_does_not_carry_is_refused():
    assert _refusal(_schema_set(source="Nomenclature")).code == "treatment_source_unknown"


def test_an_excluded_column_cannot_feed_a_schema_set():
    fields = [_field("Placement code", status="excluded")]

    assert _refusal(_schema_set(), fields).code == "treatment_source_excluded"


def test_a_binding_and_a_schema_set_cannot_claim_one_concept():
    fields = [_field("Placement code"), _field("Format", canonical="creative_format")]

    error = _refusal(_schema_set(), fields)

    assert error.code == "treatment_target_claimed_twice"
    assert error.detail["target"] == "creative_format"


def test_a_binding_cannot_claim_a_provenance_column_either():
    """Two writers on one column, and the row could no longer say which one
    answered."""
    fields = [
        _field("Placement code"),
        _field("Layout", canonical="placement_code_layout"),
    ]

    error = _refusal(_schema_set(), fields)

    assert error.code == "treatment_target_claimed_twice"
    assert error.detail["target"] == "placement_code_layout"


def test_two_schema_sets_cannot_read_one_source_column():
    payload = _payload(
        [_field("Placement code")],
        [_schema_set(), _schema_set(name="second_set", remainder="second_remainder")],
    )

    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)

    assert raised.value.code == "treatment_source_claimed_twice"


def test_a_valid_set_normalises_to_itself_and_keeps_the_other_two_families():
    payload = _payload([_field("Placement code")], [_schema_set()])

    normalized = normalize_treatments(payload)

    assert normalized["schema_splits"][0]["name"] == "placement_code"
    assert normalized["joins"] == []
    assert normalized["splits"] == []


# ---------------------------------------------------------------------------
# The JSON schema is the lock the append actually runs
# ---------------------------------------------------------------------------


def test_the_shape_validates_against_the_stored_mapping_schema():
    """`normalize_mapping` is what `save_field_mapping` calls before anything
    else; a declaration it rejects never becomes a version, whatever this module
    says."""
    from core.datastream_field_mapping import normalize_mapping

    payload = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_EXAMPLE",
        "capability_fingerprint": "b" * 64,
        "grain": ["Placement code"],
        "ambiguities": [],
        "fields": [_field("Placement code")],
        "column_treatments": {
            "joins": [],
            "splits": [],
            "schema_splits": [_schema_set()],
        },
    }

    normalized, _hash = normalize_mapping(payload)

    assert normalized["column_treatments"]["schema_splits"][0]["layouts"][0]["name"] == "full"
    normalize_treatments(normalized)


def test_a_mapping_written_before_the_seventh_word_still_validates():
    """An absent key is not an empty declaration, and no version is rewritten."""
    from core.datastream_field_mapping import normalize_mapping

    payload = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_EXAMPLE",
        "capability_fingerprint": "b" * 64,
        "grain": ["Placement code"],
        "ambiguities": [],
        "fields": [_field("Placement code")],
        "column_treatments": {"joins": [], "splits": []},
    }

    normalized, _hash = normalize_mapping(payload)

    assert "schema_splits" not in normalized["column_treatments"]
    assert normalize_treatments(normalized)["schema_splits"] == []
