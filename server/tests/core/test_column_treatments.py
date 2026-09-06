"""Story 60.6 -- what one source column becomes, and what a declaration may not say.

WHAT THIS FILE PINS. The three verbs a person applies to a spreadsheet -- exclude
a column, join several into one concept, split one into several -- and the word
each column carries afterwards. Plus the four refusals that keep a declaration
from producing a column whose provenance nobody can tell.

WHY THE REFUSALS MATTER MORE THAN THE HAPPY PATH. `csv_excel_import` already
refuses two source columns landing on one canonical target
(`dispatch_mapping_collision`) -- but it refuses it while landing rows, which is
after the person went away. Refusing at the append is what makes it "before the
import", and that is the half this file proves.
"""

from __future__ import annotations

import pytest
from core.column_treatments import (
    DIRECT,
    EXCLUDED,
    JOINED,
    NO_SAMPLE_VALUE,
    NOT_DECIDED,
    RESOLVED_BY_LIST,
    UNKNOWN_SAMPLE,
    ColumnTreatmentError,
    describe_columns,
    describe_recognized_columns,
    is_excluded,
    is_reviewed,
    normalize_treatments,
    produced_columns,
    sample_value,
)


def _binding(canonical: str | None, status: str = "confirmed") -> dict:
    return {
        "canonical_target": canonical,
        "mdm_target": None,
        "status": status,
        "blocking_reason": None,
        "confirmed_by": "owner@example.com" if status == "confirmed" else None,
        "confirmed_reason": "reviewed in the Mapping tab" if status == "confirmed" else None,
    }


def _field(field_id: str, *, canonical=None, status="confirmed", samples=("v1",)) -> dict:
    return {
        "field_id": field_id,
        "physical_type": "string",
        "profile": {
            "nullable": False,
            "unique": False,
            "cardinality_signal": "medium",
            "sample_values": list(samples),
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
        "binding": _binding(canonical, status),
    }


def _payload(fields: list[dict], treatments: dict | None = None) -> dict:
    payload = {"fields": fields}
    if treatments is not None:
        payload["column_treatments"] = treatments
    return payload


# ---------------------------------------------------------------------------
# The word, per column
# ---------------------------------------------------------------------------


def test_every_column_carries_a_treatment_word_including_the_excluded_ones():
    """The inventory stays whole. An excluded column keeps its row.

    "35 colonnes sur 46 ne servent a rien et restent listees -- leur absence
    serait l'inventaire perdu de ce qui reste a decider" (epic-60:136-138).
    """
    payload = _payload(
        [
            _field("Date", canonical="day"),
            _field("Notes", canonical=None),
            _field("Internal ref", canonical=None, status="excluded"),
        ]
    )

    rows = describe_columns(payload)

    assert [row["field_id"] for row in rows] == ["Date", "Notes", "Internal ref"]
    assert [row["treatment"] for row in rows] == [DIRECT, NOT_DECIDED, EXCLUDED]


def test_a_join_keeps_its_source_columns_and_marks_them():
    """Arbitrage 5: joining does NOT consume the sources.

    Hiding the columns a join reads is the same defect as hiding an excluded
    column under another name, and the story's Refuse line forbids the second.
    """
    payload = _payload(
        [_field("Year"), _field("Month"), _field("Day"), _field("Spend", canonical="spend")],
        {
            "joins": [
                {"target": "event_date", "sources": ["Year", "Month", "Day"], "separator": "-"}
            ],
            "splits": [],
        },
    )

    rows = {row["field_id"]: row for row in describe_columns(payload)}

    assert len(rows) == 4
    assert [rows[name]["treatment"] for name in ("Year", "Month", "Day")] == [JOINED] * 3
    assert rows["Year"]["contributes_to"] == ["event_date"]
    assert rows["Year"]["joined_with"] == ["Month", "Day"]
    assert rows["Spend"]["treatment"] == DIRECT


def test_one_split_declaration_names_its_n_targets_and_the_word_counts_them():
    """Arbitrage 6: ONE entry with N targets, never N entries sharing a pattern."""
    payload = _payload(
        [_field("Placement code")],
        {
            "joins": [],
            "splits": [
                {
                    "source": "Placement code",
                    "pattern": r"^(?P<country>[A-Z]{2})_(?P<fmt>[A-Z]+)_(?P<theme>.+)$",
                    "targets": [
                        {"group": "country", "target": "country"},
                        {"group": "fmt", "target": "creative_format"},
                        {"group": "theme", "target": "campaign_theme"},
                    ],
                }
            ],
        },
    )

    rows = describe_columns(payload)

    assert rows[0]["treatment"] == "Split into 3"
    assert rows[0]["contributes_to"] == ["country", "creative_format", "campaign_theme"]


def test_a_value_mapping_assignment_is_what_makes_a_column_resolved_by_a_list():
    """The fifth word is reachable, and only from a real assignment row.

    `app.value_mapping_assignments (datastream_id, source_field)` -- story 60.1.
    An empty set is "no table assigned", never "the store could not be read".
    """
    payload = _payload([_field("Campaign name", canonical="campaign")])

    plain = describe_columns(payload)
    resolved = describe_columns(payload, resolved_by_list={"Campaign name"})

    assert plain[0]["treatment"] == DIRECT
    assert resolved[0]["treatment"] == RESOLVED_BY_LIST


def test_exclusion_wins_over_every_other_word():
    """A column somebody excluded is excluded, whatever else was declared on it."""
    payload = _payload(
        [_field("Year", status="excluded"), _field("Month"), _field("Day")],
        {"joins": [], "splits": []},
    )

    rows = {row["field_id"]: row for row in describe_columns(payload)}

    assert rows["Year"]["treatment"] == EXCLUDED


# ---------------------------------------------------------------------------
# The example value, and the two absences that are not the same absence
# ---------------------------------------------------------------------------


def test_the_example_value_is_read_from_the_sample_already_profiled():
    payload = _payload([_field("Date", canonical="day", samples=("2026-07-01", "2026-07-02"))])

    assert describe_columns(payload)[0]["sample_value"] == "2026-07-01"


def test_an_empty_sample_says_no_sample_value_and_never_an_empty_string():
    payload = _payload([_field("Notes", samples=())])

    assert describe_columns(payload)[0]["sample_value"] == NO_SAMPLE_VALUE


def test_a_column_that_was_never_profiled_says_unknown_which_is_a_different_fact():
    """`no sample value` = the sample was read and this column was empty.

    `unknown` = nobody ever profiled it. Rendering one for the other would let a
    reader believe a measure was taken.
    """
    field = _field("Notes")
    field["profile"] = {}

    assert describe_columns(_payload([field]))[0]["sample_value"] == UNKNOWN_SAMPLE


def test_a_zero_read_from_the_file_is_a_value_and_stays_one():
    """A `0` in the file is data. The rule forbids INVENTING a zero, not showing one."""
    assert sample_value({"sample_values": ["0"]}) == "0"
    assert sample_value({"sample_values": [""]}) == NO_SAMPLE_VALUE


# ---------------------------------------------------------------------------
# The refusals -- before the import, not while landing rows
# ---------------------------------------------------------------------------


def test_a_concept_claimed_by_a_binding_and_by_a_join_is_refused():
    payload = _payload(
        [_field("Date", canonical="event_date"), _field("Year"), _field("Month")],
        {
            "joins": [
                {"target": "event_date", "sources": ["Year", "Month"], "separator": "-"}
            ],
            "splits": [],
        },
    )

    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)

    assert raised.value.code == "treatment_target_claimed_twice"
    assert raised.value.detail["target"] == "event_date"


def test_a_concept_claimed_by_a_join_and_by_a_split_is_refused():
    payload = _payload(
        [_field("Year"), _field("Month"), _field("Placement code")],
        {
            "joins": [{"target": "country", "sources": ["Year", "Month"], "separator": ""}],
            "splits": [
                {
                    "source": "Placement code",
                    "pattern": r"^(?P<country>[A-Z]{2})_(?P<theme>.+)$",
                    "targets": [
                        {"group": "country", "target": "country"},
                        {"group": "theme", "target": "campaign_theme"},
                    ],
                }
            ],
        },
    )

    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)

    assert raised.value.code == "treatment_target_claimed_twice"


def test_a_join_of_one_column_is_refused():
    payload = _payload(
        [_field("Year")],
        {"joins": [{"target": "event_date", "sources": ["Year"], "separator": "-"}], "splits": []},
    )

    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)

    assert raised.value.code == "treatment_join_needs_two_sources"


def test_a_split_toward_a_single_concept_is_refused():
    payload = _payload(
        [_field("Placement code")],
        {
            "joins": [],
            "splits": [
                {
                    "source": "Placement code",
                    "pattern": r"^(?P<country>[A-Z]{2})",
                    "targets": [{"group": "country", "target": "country"}],
                }
            ],
        },
    )

    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)

    assert raised.value.code == "treatment_split_needs_two_targets"


def test_an_excluded_column_cannot_feed_a_join():
    """It never lands, so joining it would build a value out of nothing."""
    payload = _payload(
        [_field("Year", status="excluded"), _field("Month")],
        {
            "joins": [
                {"target": "event_date", "sources": ["Year", "Month"], "separator": "-"}
            ],
            "splits": [],
        },
    )

    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)

    assert raised.value.code == "treatment_source_excluded"


def test_a_treatment_naming_a_column_the_mapping_does_not_carry_is_refused():
    payload = _payload(
        [_field("Year"), _field("Month")],
        {
            "joins": [
                {"target": "event_date", "sources": ["Year", "Trimester"], "separator": "-"}
            ],
            "splits": [],
        },
    )

    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)

    assert raised.value.code == "treatment_source_unknown"


def test_a_split_group_the_pattern_does_not_capture_is_refused():
    payload = _payload(
        [_field("Placement code")],
        {
            "joins": [],
            "splits": [
                {
                    "source": "Placement code",
                    "pattern": r"^(?P<country>[A-Z]{2})_(?P<theme>.+)$",
                    "targets": [
                        {"group": "country", "target": "country"},
                        {"group": "format", "target": "creative_format"},
                    ],
                }
            ],
        },
    )

    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)

    assert raised.value.code == "treatment_split_group_missing"
    assert raised.value.detail["captured"] == ["country", "theme"]


def test_a_pattern_that_does_not_compile_is_refused_before_a_version_exists():
    payload = _payload(
        [_field("Placement code")],
        {
            "joins": [],
            "splits": [
                {
                    "source": "Placement code",
                    "pattern": r"^(?P<country>[A-Z]{2}",
                    "targets": [
                        {"group": "country", "target": "country"},
                        {"group": "theme", "target": "campaign_theme"},
                    ],
                }
            ],
        },
    )

    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)

    assert raised.value.code == "treatment_split_pattern_invalid"


def test_a_mapping_with_no_declaration_is_not_a_refusal():
    """Three families, all empty -- and `schema_splits` is the third since 70.1.

    The normalised shape names every family the vocabulary has, empty or not, so
    a reader never has to tell "this mapping declares no schema set" from "this
    mapping predates schema sets" by the presence of a key.
    """
    assert normalize_treatments(_payload([_field("Date", canonical="day")])) == {
        "joins": [],
        "splits": [],
        "schema_splits": [],
    }


# ---------------------------------------------------------------------------
# What the projection says a produced column came from
# ---------------------------------------------------------------------------


def test_a_produced_column_names_the_columns_it_came_from():
    payload = _payload(
        [_field("Year"), _field("Month"), _field("Day"), _field("Placement code")],
        {
            "joins": [
                {"target": "event_date", "sources": ["Year", "Month", "Day"], "separator": "-"}
            ],
            "splits": [
                {
                    "source": "Placement code",
                    "pattern": r"^(?P<country>[A-Z]{2})_(?P<theme>.+)$",
                    "targets": [
                        {"group": "country", "target": "country"},
                        {"group": "theme", "target": "campaign_theme"},
                    ],
                }
            ],
        },
    )

    produced = produced_columns(payload)

    assert produced == [
        {"target": "campaign_theme", "kind": "split", "sources": ["Placement code"]},
        {"target": "country", "kind": "split", "sources": ["Placement code"]},
        {"target": "event_date", "kind": "join", "sources": ["Year", "Month", "Day"]},
    ]


def test_no_declaration_produces_no_column_rather_than_an_empty_measure():
    assert produced_columns(_payload([_field("Date", canonical="day")])) == []


# ---------------------------------------------------------------------------
# The same two readings on the FILE path -- one vocabulary, not two
# ---------------------------------------------------------------------------


def test_the_file_path_reads_its_example_value_from_the_parsed_sample():
    """The recognizer already RECEIVES the sample rows and renders nothing from them."""
    recognized = [
        {"source_column": "Date", "canonical_target": "day", "status": "matched",
         "confidence": 0.98, "name_confidence": 0.98, "evidence": []},
        {"source_column": "Commentaire", "canonical_target": None, "status": "unmatched",
         "confidence": 0.1, "name_confidence": 0.1, "evidence": []},
    ]

    described = describe_recognized_columns(
        recognized,
        sample_rows=[{"Date": "2026-07-01", "Commentaire": ""}, {"Date": "2026-07-02"}],
    )

    assert described[0]["sample_value"] == "2026-07-01"
    assert described[0]["treatment"] == DIRECT
    # An extra column the recognizer refuses to force-map is NOT `Direct`, and
    # its empty cell is NOT an empty string.
    assert described[1]["sample_value"] == NO_SAMPLE_VALUE
    assert described[1]["treatment"] == NOT_DECIDED


def test_the_file_path_says_unknown_when_no_sample_was_read_at_all():
    described = describe_recognized_columns(
        [{"source_column": "Date", "canonical_target": "day", "status": "matched"}],
        sample_rows=None,
    )

    assert described[0]["sample_value"] == UNKNOWN_SAMPLE


def test_the_file_path_and_the_mapping_path_use_the_same_words():
    """The same file, the same concepts -- one vocabulary, computed once."""
    treatments = {
        "joins": [{"target": "event_date", "sources": ["Year", "Month"], "separator": "-"}],
        "splits": [],
    }
    payload = _payload([_field("Year"), _field("Month")], treatments)
    recognized = [
        {"source_column": "Year", "canonical_target": None, "status": "unmatched"},
        {"source_column": "Month", "canonical_target": None, "status": "unmatched"},
    ]

    from_mapping = [row["treatment"] for row in describe_columns(payload)]
    from_file = [
        row["treatment"]
        for row in describe_recognized_columns(
            recognized, sample_rows=[], mapping_payload=payload
        )
    ]

    assert from_mapping == from_file == [JOINED, JOINED]


# ---------------------------------------------------------------------------
# The two predicates the activation compiler now reads
# ---------------------------------------------------------------------------


def test_exclusion_is_read_from_the_binding_status_and_from_nothing_else():
    """`included` governs nothing. Five live readers already read this key."""
    assert is_excluded(_field("a", status="excluded")) is True
    assert is_excluded(_field("a", status="confirmed")) is False
    # The retired flag cannot resurrect an exclusion, in either direction.
    stale = _field("a", status="confirmed")
    stale["included"] = False
    assert is_excluded(stale) is False


def test_a_field_carrying_no_binding_at_all_is_not_reviewed():
    assert is_reviewed(_field("a")) is True
    assert is_reviewed({"field_id": "a"}) is False
    assert is_reviewed({"field_id": "a", "binding": {}}) is False


# ---------------------------------------------------------------------------
# The screen and the store must meet. One grammar, one shape.
#
# The Mapping tab composes a `column_treatments` declaration and posts it to
# `workbench/mapping/changes`, which appends a version through
# `save_field_mapping` -> `normalize_treatments`. If the two halves disagree on
# ONE character, the control looks like it worked and the append refuses. These
# replay the EXACT objects `WorkbenchMappingPage.tsx` builds.
# ---------------------------------------------------------------------------


def test_the_declaration_the_mapping_tab_composes_is_accepted_as_it_stands():
    """Byte-for-byte what the screen posts, including its default separator."""
    payload = _payload(
        [_field("col_0"), _field("col_1"), _field("col_2")],
        {
            # The join the screen writes: target typed by the person, sources in
            # pick order, separator defaulting to "-".
            "joins": [
                {"target": "event_date", "sources": ["col_0", "col_1", "col_2"], "separator": "-"}
            ],
            "splits": [],
        },
    )

    assert normalize_treatments(payload)["joins"][0]["sources"] == ["col_0", "col_1", "col_2"]
    assert describe_columns(payload)[0]["treatment"] == JOINED


def test_the_split_the_mapping_tab_composes_is_accepted_as_it_stands():
    payload = _payload(
        [_field("col_0")],
        {
            "joins": [],
            "splits": [
                {
                    "source": "col_0",
                    "pattern": "^(?P<country>..)_(?P<theme>.+)$",
                    "targets": [
                        {"group": "country", "target": "country"},
                        {"group": "theme", "target": "campaign_theme"},
                    ],
                }
            ],
        },
    )

    assert normalize_treatments(payload)["splits"][0]["source"] == "col_0"
    assert describe_columns(payload)[0]["treatment"] == "Split into 2"


def test_the_javascript_spelling_of_a_named_group_is_refused_here_too():
    """`(?<name>…)` is a hard `re.error`, so both halves refuse it.

    The screen refuses it before the click; this is the second lock, so a
    declaration composed anywhere else cannot get in either.
    """
    payload = _payload(
        [_field("col_0")],
        {
            "joins": [],
            "splits": [
                {
                    "source": "col_0",
                    "pattern": "^(?<country>..)_(?<theme>.+)$",
                    "targets": [
                        {"group": "country", "target": "country"},
                        {"group": "theme", "target": "campaign_theme"},
                    ],
                }
            ],
        },
    )

    with pytest.raises(ColumnTreatmentError) as raised:
        normalize_treatments(payload)

    assert raised.value.code == "treatment_split_pattern_invalid"


def test_the_shape_the_mapping_tab_posts_validates_against_the_stored_schema():
    """The JSON schema is the third lock, and it is the one the append runs.

    `normalize_mapping` is what `save_field_mapping` calls before anything else;
    a declaration it rejects never becomes a version, whatever this module says.
    """
    from core.datastream_field_mapping import normalize_mapping

    payload = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_EXAMPLE",
        "capability_fingerprint": "b" * 64,
        "grain": ["col_0"],
        "ambiguities": [],
        "fields": [_field("col_0"), _field("col_1")],
        "column_treatments": {
            "joins": [{"target": "event_date", "sources": ["col_0", "col_1"], "separator": "-"}],
            "splits": [],
        },
    }

    normalized, _hash = normalize_mapping(payload)
    assert normalized["column_treatments"]["joins"][0]["target"] == "event_date"
    normalize_treatments(normalized)
