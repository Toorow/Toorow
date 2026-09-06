"""The vocabulary a client may declare, and the two things it may never do.

AI-248, arbitration of 2026-08-08. Proven without a database: the refusals are the
value, and they are pure.

Why this module exists at all, measured: `app.mdm_canonical_fields` held 0 rows at
both scopes while six production modules read it and one route listed it. The only
INSERTs in the repository were in five test files, and
`datastream_field_mapping.py:740` said "registry is minted elsewhere". Elsewhere
did not exist.
"""

from __future__ import annotations

import pytest
from core.canonical_field_registry import (
    AGGREGATIONS,
    CONCEPT_KINDS,
    CanonicalFieldError,
    validate_declaration,
)


def _metric(**kw):
    base = {
        "canonical_name": "duration_s",
        "concept_kind": "metric",
        "value_type": "integer",
        "aggregation": "sum",
    }
    return validate_declaration(**{**base, **kw})


# ---------------------------------------------------------------------------
# The invariant the database carries, said as a sentence
# ---------------------------------------------------------------------------


def test_a_metric_declaring_neither_aggregation_nor_non_additive_is_refused():
    """`ck_mdm_canonical_fields_metric_aggregation`, in words.

    Migration 032 states the reason: "a measure must never be silently
    non-summable". A metric that declared neither would be summed by whichever
    reader got to it first -- the same defect the semantic compiler refuses one
    layer up, at the other end of the same pipeline.
    """
    with pytest.raises(CanonicalFieldError) as excinfo:
        validate_declaration(canonical_name="views", concept_kind="metric", value_type="integer")
    assert "silently non-summable" in str(excinfo.value)


def test_a_metric_declaring_BOTH_is_refused_rather_than_ranked():
    """Accepting both would make a reader believe whichever it checked first."""
    with pytest.raises(CanonicalFieldError) as excinfo:
        _metric(aggregation="sum", non_additive=True)
    assert "contradict" in str(excinfo.value)


def test_a_metric_may_be_non_additive_with_no_aggregation():
    """`average_position` is the shipped example: non-additive, no aggregation."""
    declared = validate_declaration(
        canonical_name="average_position", concept_kind="metric",
        value_type="decimal", non_additive=True,
    )
    assert declared["non_additive"] is True and declared["aggregation"] is None


def test_the_aggregation_vocabulary_is_the_columns_own_and_not_the_semantic_layers():
    """The CHECK admits five; the semantic layer admits seven.

    Reusing `AGGREGATION_FUNCTIONS` here would accept `median` and
    `count_distinct` and produce rows the database refuses -- a validator that
    passes and an INSERT that fails is worse than no validator.
    """
    from core.semantic_expressions import AGGREGATION_FUNCTIONS

    assert set(AGGREGATIONS) < set(AGGREGATION_FUNCTIONS)
    for extra in set(AGGREGATION_FUNCTIONS) - set(AGGREGATIONS):
        with pytest.raises(CanonicalFieldError):
            _metric(aggregation=extra)


# ---------------------------------------------------------------------------
# A dimension is not a measure wearing a different word
# ---------------------------------------------------------------------------


def test_a_dimension_carrying_an_aggregation_or_a_unit_is_refused():
    """A column nobody reads is a column somebody believes is used."""
    for kw in ({"aggregation": "sum"}, {"unit": "second"}, {"non_additive": True}):
        with pytest.raises(CanonicalFieldError):
            validate_declaration(
                canonical_name="category", concept_kind="dimension",
                value_type="string", **kw,
            )


def test_a_dimension_normalizes_to_explicit_nulls():
    """The caller gets the row the database will hold, not a partial dict."""
    declared = validate_declaration(
        canonical_name="category", concept_kind="dimension", value_type="string"
    )
    assert declared == {
        "canonical_name": "category",
        "concept_kind": "dimension",
        "value_type": "string",
        "aggregation": None,
        "non_additive": False,
        "unit": None,
        "object_kind": None,
    }


def test_an_unknown_concept_kind_is_refused_and_both_are_named():
    with pytest.raises(CanonicalFieldError) as excinfo:
        validate_declaration(canonical_name="x", concept_kind="attribute", value_type="string")
    for kind in CONCEPT_KINDS:
        assert kind in str(excinfo.value)


def test_a_nameless_field_is_refused():
    for bad in (None, "", "   "):
        with pytest.raises(CanonicalFieldError):
            validate_declaration(canonical_name=bad, concept_kind="dimension", value_type="string")


# ---------------------------------------------------------------------------
# The scope boundary -- the arbitration, in the signature
# ---------------------------------------------------------------------------


def test_the_door_refuses_to_mint_a_platform_field():
    """The arbitration of 2026-08-08 is that the two branches are the two SCOPES.

    A platform field changes what every project of the instance aligns on.
    Minting one from a door a client reaches would let them edit the shared
    vocabulary -- so the refusal is in the function, not in whoever remembers.
    """
    from core.canonical_field_registry import declare_project_field

    for empty in (None, "", "   "):
        with pytest.raises(CanonicalFieldError) as excinfo:
            declare_project_field(
                None, project_id=empty, canonical_name="views",
                concept_kind="metric", value_type="integer", aggregation="sum",
                actor="tester",
            )
        assert "never the platform vocabulary" in str(excinfo.value)


def test_the_eleven_fields_of_one_video_all_declare_cleanly():
    """The use case that decided the arbitration, as executable evidence.

    Measured on the dossier: a single video needs eleven fields, of which the
    thirteen governed ones cover ZERO. Under a governed-edit-only extension
    point, describing one's own workbook would take eleven publication
    ceremonies.
    """
    video = [
        ("video_id", "dimension", "string", None), ("title", "dimension", "string", None),
        ("published_at", "dimension", "date", None), ("category", "dimension", "string", None),
        ("restaurant", "dimension", "string", None), ("product", "dimension", "string", None),
        ("recipe", "dimension", "string", None), ("film", "dimension", "string", None),
        ("duration_s", "metric", "integer", "sum"), ("views", "metric", "integer", "sum"),
        ("confidence", "metric", "ratio", "average"),
    ]
    declared = [
        validate_declaration(
            canonical_name=n, concept_kind=k, value_type=v, aggregation=a,
            object_kind="video",
        )
        for n, k, v, a in video
    ]
    assert len(declared) == 11
    assert sum(1 for d in declared if d["concept_kind"] == "metric") == 3


# ---------------------------------------------------------------------------
# Story 64.14 -- la definition est UNE, et elle dit quel objet elle qualifie.
#
# Arbitrage de Jean, 2026-08-08 : « la duree peut etre definie dans le MDM comme
# DUREE DE LA VIDEO ». Avant, le meme fait vivait dans deux magasins qui en
# savaient chacun la moitie -- le champ canonique sans `value_type`, le contrat
# d attribut sans genre ni agregation -- et AUCUN des deux n etait publiable en
# Concept, dont `value_type` est NOT NULL.
# ---------------------------------------------------------------------------


def test_a_field_without_a_value_type_is_refused():
    """La moitie qui manquait, et sans laquelle rien ne devient un Concept."""
    with pytest.raises(CanonicalFieldError) as excinfo:
        validate_declaration(
            canonical_name="duration_s", concept_kind="metric",
            value_type=None, aggregation="sum",
        )
    assert "value_type" in str(excinfo.value)


def test_the_value_type_vocabulary_is_the_semantic_layers_and_is_imported():
    """Une seconde liste aurait derive -- le defaut retire quatre fois dans l epic 64."""
    from core.semantic_expressions import VALUE_TYPES

    for value_type in VALUE_TYPES:
        assert validate_declaration(
            canonical_name="x", concept_kind="dimension", value_type=value_type
        )["value_type"] == value_type


def test_a_field_may_name_the_object_it_qualifies():
    """« la duree DE LA VIDEO », pas une duree flottante."""
    declared = validate_declaration(
        canonical_name="duration_s", concept_kind="metric", value_type="integer",
        aggregation="sum", unit="second", object_kind="video",
    )
    assert declared["object_kind"] == "video"


def test_a_field_may_qualify_NO_object_and_that_is_the_platform_case():
    """`date`, `country`, `clicks` ne qualifient aucun objet.

    Les forcer a en nommer un inventerait un proprietaire.
    """
    assert validate_declaration(
        canonical_name="date", concept_kind="dimension", value_type="date"
    )["object_kind"] is None


def test_a_malformed_object_kind_is_refused_before_the_insert():
    """Meme motif que `master_data_registries.object_kind` : un champ qui nomme un
    objet doit nommer un objet que le registre peut porter."""
    for bad in ("Video", "1video", "video-kind", "   "):
        with pytest.raises(CanonicalFieldError):
            validate_declaration(
                canonical_name="x", concept_kind="dimension",
                value_type="string", object_kind=bad,
            )
