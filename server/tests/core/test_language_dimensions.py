"""Story 27.8 -- the language dimension FAMILY: three dimensions, never one.

Every test here is PURE (no Postgres, no network): the invariants of the story live in the
pure layer on purpose, so they can be proven offline.
"""

from __future__ import annotations

import pytest
from core import language_dimensions as ld
from core.language_vocabulary import (
    LanguageVocabularyError,
    load_language_vocabulary,
    normalize_language_subtag,
)

# ---------------------------------------------------------------------------
# A. The family is three dimensions, declared, closed and typed.
# ---------------------------------------------------------------------------


def test_family_has_exactly_three_distinct_members_with_distinct_natures():
    assert set(ld.LANGUAGE_DIMENSION_FAMILY) == {
        ld.AUDIENCE_LANGUAGE,
        ld.CONTENT_LANGUAGE,
        ld.TARGETING_LANGUAGE,
    }
    natures = {d.nature for d in ld.LANGUAGE_DIMENSION_FAMILY.values()}
    # Three members, three DIFFERENT natures -- that is exactly why they cannot merge.
    assert len(natures) == 3
    assert ld.describe_dimension(ld.AUDIENCE_LANGUAGE).nature == ld.NATURE_OBSERVED
    assert ld.describe_dimension(ld.CONTENT_LANGUAGE).nature == ld.NATURE_ASSET_PROPERTY
    assert ld.describe_dimension(ld.TARGETING_LANGUAGE).nature == ld.NATURE_DECLARED_INTENT


def test_a_generic_language_dimension_does_not_exist():
    assert not ld.is_language_dimension("language")
    assert ld.describe_dimension("language") is None


# ---------------------------------------------------------------------------
# B. The guard: never summed, never compared with each other.
# ---------------------------------------------------------------------------


def test_two_family_members_together_raise():
    with pytest.raises(ld.IncommensurableDimensions):
        ld.assert_language_dimensions_comparable(
            [ld.TARGETING_LANGUAGE, ld.AUDIENCE_LANGUAGE]
        )


def test_all_three_pairs_are_refused():
    members = sorted(ld.LANGUAGE_DIMENSION_FAMILY)
    for i, first in enumerate(members):
        for second in members[i + 1:]:
            with pytest.raises(ld.IncommensurableDimensions):
                ld.assert_language_dimensions_comparable([first, second])


def test_one_member_alone_is_fine_and_non_family_dimensions_are_ignored():
    ld.assert_language_dimensions_comparable([ld.AUDIENCE_LANGUAGE])
    ld.assert_language_dimensions_comparable([ld.AUDIENCE_LANGUAGE, "country", "device"])
    ld.assert_language_dimensions_comparable(["country", "device"])
    ld.assert_language_dimensions_comparable([])


def test_keep_separate_decision_reuses_the_platform_guard_vocabulary_and_has_no_total():
    from core.metric_reconciliation import KEEP_SEPARATE, METHOD_KEEP_SEPARATE

    decision = ld.keep_separate_decision(
        [ld.AUDIENCE_LANGUAGE, ld.TARGETING_LANGUAGE], connector="src_a"
    )
    # The SAME mechanism as a KEEP_SEPARATE metric group, not a parallel vocabulary.
    assert decision.status == KEEP_SEPARATE
    assert decision.method == METHOD_KEEP_SEPARATE
    assert decision.reason.code.startswith("KEEP_SEPARATE")
    # One series per dimension, and no field could ever carry a total.
    assert [s.dimension for s in decision.series] == [
        ld.AUDIENCE_LANGUAGE,
        ld.TARGETING_LANGUAGE,
    ]
    assert not hasattr(decision, "total")


def test_divergence_is_information_not_an_anomaly():
    divergence = ld.describe_reach_divergence(
        ["fr-FR", "fr"], ["nl-BE", "FR-fr", "garbage"]
    )
    assert divergence.is_anomaly is False
    assert divergence.interpretation == ld.DIVERGENCE_INFORMATIONAL
    # 'FR-fr' and 'fr-FR' are the same tag: the juxtaposition is on canonical values.
    assert "fr-FR" in divergence.both
    assert divergence.reached_not_targeted == ("nl-BE",)
    assert divergence.targeted_not_reached == ("fr",)
    # No arithmetic anywhere: sets only.
    assert not hasattr(divergence, "delta")


# ---------------------------------------------------------------------------
# C. Values are BCP 47; encoding is not a dimension.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected,encoding",
    [
        ("fr", "fr", ld.ENCODING_SUBTAG),
        ("FR", "fr", ld.ENCODING_SUBTAG),
        ("fr-FR", "fr-FR", ld.ENCODING_TAG),
        ("FR-fr", "fr-FR", ld.ENCODING_TAG),
        ("fr_FR", "fr-FR", ld.ENCODING_LOCALE_UNDERSCORE),
        ("French", "fr", ld.ENCODING_DISPLAY_NAME),
        ("ENGLISH", "en", ld.ENCODING_DISPLAY_NAME),
        ("en-us", "en-US", ld.ENCODING_TAG),
        ("zh-Hant-TW", "zh-Hant-TW", ld.ENCODING_TAG),
    ],
)
def test_bcp47_casing_and_encodings_collapse_to_one_canonical_value(raw, expected, encoding):
    tag = ld.parse_language_tag(raw)
    assert tag is not None
    assert tag.canonical == expected          # language lowercase, region UPPERCASE
    assert tag.source_encoding == encoding


@pytest.mark.parametrize("raw", ["", "   ", None, 42, "xx", "fr-ZZ", "en-US-POSIX", "-"])
def test_unparseable_values_are_a_gap_never_a_guess(raw):
    assert ld.parse_language_tag(raw) is None
    adapted = ld.adapt_source_value(raw)
    assert adapted.canonical_value is None
    assert adapted.gap_reason == ld.GAP_UNPARSEABLE


def test_label_and_code_columns_are_two_encodings_of_one_dimension():
    """The GA4-shaped case: a label column AND a code column, same dimension."""
    label = ld.adapt_source_value("English")
    code = ld.adapt_source_value("en")
    assert label.canonical_value == code.canonical_value == "en"

    proposals = [
        ld.BindingProposal(
            connector="src_a",
            report_id="R1",
            source_field=field,
            canonical_dimension=ld.AUDIENCE_LANGUAGE,
            status=ld.STATUS_PROPOSED,
            confidence=0.9,
            evidence=ld.BindingEvidence(quote="q", source=ld.EVIDENCE_DESCRIPTION),
            rationale="",
        )
        for field in ("languageLabel", "languageCode")
    ]
    grouped = ld.group_encoding_variants(proposals)
    # ONE key (one dimension), TWO fields -- not two dimensions.
    assert grouped == {("src_a", "R1", ld.AUDIENCE_LANGUAGE): ("languageCode", "languageLabel")}


def test_iso639_alone_is_not_the_stored_value_the_locale_survives():
    tag = ld.parse_language_tag("fr-BE")
    assert tag.canonical == "fr-BE"   # the region is KEPT at ingestion...
    assert tag.language == "fr"       # ...and the rollup remains derivable at read.


# ---------------------------------------------------------------------------
# D. Grain: the standard rollup is a legitimate default; fr-BE -> fr-FR is not.
# ---------------------------------------------------------------------------


def test_recommended_rollup_is_the_primary_subtag():
    assert ld.recommended_rollup("fr-FR") == "fr"
    assert ld.recommended_rollup("fr-BE") == "fr"
    assert ld.recommended_rollup("fr") == "fr"
    assert ld.recommended_rollup("zh-Hant-TW") == "zh"
    assert ld.recommended_rollup("nonsense") is None


def test_grain_choice_is_explicit_and_unknown_grain_raises():
    assert ld.apply_grain("fr-FR", ld.GRAIN_LOCALE) == "fr-FR"
    assert ld.apply_grain("fr-FR", ld.GRAIN_LANGUAGE) == "fr"
    with pytest.raises(ValueError):
        ld.apply_grain("fr-FR", "whatever")


def test_conforming_fr_be_to_fr_fr_is_refused():
    with pytest.raises(ld.IllegitimateLanguageConformance) as excinfo:
        ld.validate_conformance_target("fr-BE", "fr-FR")
    assert "region" in str(excinfo.value)


@pytest.mark.parametrize(
    "source,target",
    [
        ("fr", "fr-FR"),      # inventing a region the source never gave
        ("fr-FR", "nl-BE"),   # cross-language
        ("fr-FR", "en"),      # cross-language rollup
        ("fr-FR", "garbage"),
    ],
)
def test_other_illegitimate_conformances_are_refused(source, target):
    with pytest.raises(ld.IllegitimateLanguageConformance):
        ld.validate_conformance_target(source, target)


@pytest.mark.parametrize(
    "source,target",
    [("fr-FR", "fr"), ("fr-FR", "fr-FR"), ("French", "fr"), ("fr_FR", "fr-FR")],
)
def test_legitimate_conformances_are_the_encoding_change_and_the_standard_rollup(source, target):
    ld.validate_conformance_target(source, target)  # does not raise


def test_premapping_is_prefilled_carries_its_provenance_and_reports_gaps():
    premapping = ld.build_rollup_premapping(
        {"src_a": ["fr-FR", "fr-BE", "English", "fr", "not-a-language"]},
        grain=ld.GRAIN_LANGUAGE,
    )
    pairs = {(s.source_value, s.canonical_value) for s in premapping.suggestions}
    assert pairs == {("fr-FR", "fr"), ("fr-BE", "fr"), ("English", "en")}
    # 'fr' is already at the target grain -> no suggestion at all (nothing to change).
    assert all(s.source_value != "fr" for s in premapping.suggestions)
    # Provenance is visible and distinct from a textual heuristic.
    assert {s.method for s in premapping.suggestions} == {ld.METHOD_STANDARD_ROLLUP}
    assert [g.source_value for g in premapping.gaps] == ["not-a-language"]


def test_premapping_at_locale_grain_only_normalizes_the_encoding():
    premapping = ld.build_rollup_premapping(
        {"src_a": ["fr-FR", "fr_FR", "French"]}, grain=ld.GRAIN_LOCALE
    )
    pairs = {(s.source_value, s.canonical_value) for s in premapping.suggestions}
    # The locale is NEVER rolled up here: only the encoding is canonicalised.
    assert pairs == {("fr_FR", "fr-FR"), ("French", "fr")}


def test_premapping_suggestions_are_the_27_4_suggestion_type():
    from core.dimension_conformance import Suggestion

    premapping = ld.build_rollup_premapping({"src_a": ["fr-FR"]}, grain=ld.GRAIN_LANGUAGE)
    assert all(isinstance(s, Suggestion) for s in premapping.suggestions)


# --- the override precedence, proven without Postgres via the injected conformer ------


def _conformer_returning(value):
    def _conform(org_id, dimension, connector, source_value, *, project_id=None):
        return value

    return _conform


def test_a_confirmed_client_mapping_always_beats_the_standard_rollup():
    resolved = ld.resolve_reporting_language(
        "org_1",
        ld.AUDIENCE_LANGUAGE,
        "src_a",
        "fr-FR",
        grain=ld.GRAIN_LANGUAGE,
        conformer=_conformer_returning("fr-FR"),  # the client kept the locale
    )
    assert resolved.value == "fr-FR"
    assert resolved.provenance == ld.PROVENANCE_CLIENT_OVERRIDE


def test_without_an_override_the_standard_rollup_applies_and_says_so():
    resolved = ld.resolve_reporting_language(
        "org_1",
        ld.AUDIENCE_LANGUAGE,
        "src_a",
        "fr-FR",
        grain=ld.GRAIN_LANGUAGE,
        conformer=_conformer_returning(None),
    )
    assert resolved.value == "fr"
    assert resolved.provenance == ld.PROVENANCE_STANDARD_ROLLUP


def test_at_locale_grain_the_source_value_is_kept_as_the_finest_grain():
    resolved = ld.resolve_reporting_language(
        "org_1",
        ld.CONTENT_LANGUAGE,
        "src_a",
        "fr_FR",
        grain=ld.GRAIN_LOCALE,
        conformer=_conformer_returning(None),
    )
    assert resolved.value == "fr-FR"
    assert resolved.provenance == ld.PROVENANCE_SOURCE_GRAIN


def test_an_unparseable_value_resolves_to_nothing_not_to_a_guess():
    resolved = ld.resolve_reporting_language(
        "org_1",
        ld.AUDIENCE_LANGUAGE,
        "src_a",
        "???",
        conformer=_conformer_returning(None),
    )
    assert resolved.value is None
    assert resolved.provenance == ld.PROVENANCE_UNMAPPED


def test_resolution_refuses_a_dimension_outside_the_family():
    with pytest.raises(ValueError):
        ld.resolve_reporting_language(
            "org_1", "country", "src_a", "fr", conformer=_conformer_returning(None)
        )


def test_override_lookup_failure_is_fail_soft():
    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    resolved = ld.resolve_reporting_language(
        "org_1", ld.AUDIENCE_LANGUAGE, "src_a", "fr-FR", conformer=_boom
    )
    assert resolved.value == "fr-FR"
    assert resolved.provenance == ld.PROVENANCE_SOURCE_GRAIN


# ---------------------------------------------------------------------------
# E. Binding with cited evidence; ambiguity stays pending.
# ---------------------------------------------------------------------------


def _field(**kwargs) -> dict:
    base = {"kind": "dimension", "description": "", "source_field": "", "field_id": ""}
    base.update(kwargs)
    return base


def test_an_observation_description_proposes_the_audience_dimension_with_the_quote():
    quote = "The language setting of the user's browser or device."
    proposal = ld.propose_binding(
        _field(source_field="language", field_id="language", description=quote),
        connector="src_a",
        report_id="R1",
    )
    assert proposal.canonical_dimension == ld.AUDIENCE_LANGUAGE
    assert proposal.status == ld.STATUS_PROPOSED
    assert proposal.confidence == ld.CONFIDENCE_DESCRIPTION_MARKER
    # The PROOF is the provider's own sentence, verbatim -- never a paraphrase of ours.
    assert proposal.evidence.quote == quote
    assert proposal.evidence.source == ld.EVIDENCE_DESCRIPTION
    assert proposal.evidence.markers  # the markers actually matched are auditable


def test_nothing_is_ever_auto_confirmed_even_at_the_highest_confidence():
    proposal = ld.propose_binding(
        _field(
            source_field="language",
            description="The language setting of the user's browser or device.",
        ),
        connector="src_a",
    )
    assert proposal.status != ld.STATUS_CONFIRMED
    assert proposal.confidence < 1.0


def test_a_field_name_alone_is_a_weaker_confidence():
    proposal = ld.propose_binding(
        _field(
            source_field="creativeLanguage",
            field_id="creativeLanguage",
            description="Reporting column 'creativeLanguage' (String, columns dictionary).",
        ),
        connector="src_b",
    )
    assert proposal.canonical_dimension == ld.CONTENT_LANGUAGE
    assert proposal.confidence == ld.CONFIDENCE_NAME_MARKER
    assert proposal.evidence.source == ld.EVIDENCE_FIELD_NAME


def test_a_targeting_field_binds_to_the_declared_intent():
    for name in ("lineItemLanguageTargeting", "target_languages"):
        proposal = ld.propose_binding(
            _field(source_field=name, field_id=name), connector="src_b"
        )
        assert proposal.canonical_dimension == ld.TARGETING_LANGUAGE
        assert proposal.status == ld.STATUS_PROPOSED


def test_the_word_audience_alone_stays_pending_with_a_proposition_only():
    proposal = ld.propose_binding(
        _field(
            source_field="language",
            field_id="language",
            description="Language of the audience.",
        ),
        connector="src_c",
    )
    assert proposal.status == ld.STATUS_PENDING
    assert proposal.canonical_dimension == ld.AUDIENCE_LANGUAGE   # a reading...
    assert proposal.pending_reason == ld.PENDING_AMBIGUOUS_AUDIENCE_TERM  # ...not a decision
    assert proposal.confidence == ld.CONFIDENCE_PENDING_PROPOSITION


def test_a_grouping_attribute_stays_pending_as_an_intent_proposition():
    proposal = ld.propose_binding(
        _field(
            source_field="Language",
            field_id="language",
            description=(
                "Report column 'Language' (attribute -- guide 'Columns that group the data'). "
                "Available in: six report types."
            ),
        ),
        connector="src_d",
    )
    assert proposal.status == ld.STATUS_PENDING
    assert proposal.canonical_dimension == ld.TARGETING_LANGUAGE
    assert proposal.pending_reason == ld.PENDING_CONFIGURED_ATTRIBUTE


def test_no_evidence_at_all_means_pending_with_no_proposition():
    proposal = ld.propose_binding(
        _field(
            source_field="Language",
            field_id="language",
            description="Report column 'Language' (string). Section: INVENTORY.",
        ),
        connector="src_e",
    )
    assert proposal.status == ld.STATUS_PENDING
    assert proposal.canonical_dimension is None
    assert proposal.pending_reason == ld.PENDING_NO_EVIDENCE
    assert proposal.confidence == ld.CONFIDENCE_NONE


def test_a_metric_homonym_is_excluded_from_the_family():
    for name, desc in (
        ("offensive_language_ads", "Ads that failed the Offensive Language risk category."),
        ("target_language_mismatch_ads", "Ads served against content in a non-targeted language."),
    ):
        proposal = ld.propose_binding(
            _field(kind="metric", source_field=name, field_id=name, description=desc),
            connector="src_f",
        )
        assert proposal.status == ld.STATUS_EXCLUDED
        assert proposal.excluded_reason == ld.EXCLUDED_METRIC_HOMONYM
        assert proposal.canonical_dimension is None


def test_a_catalogue_item_attribute_is_out_of_the_family():
    proposal = ld.propose_binding(
        _field(source_field="product_language", field_id="product_language"),
        connector="src_g",
    )
    assert proposal.status == ld.STATUS_EXCLUDED
    assert proposal.excluded_reason == ld.EXCLUDED_ITEM_ATTRIBUTE


def test_conflicting_evidence_stays_pending_rather_than_picking_a_side():
    proposal = ld.propose_binding(
        _field(
            source_field="lang",
            description="The creative language of the ad, as targeted by the line item.",
        ),
        connector="src_h",
    )
    assert proposal.status == ld.STATUS_PENDING
    assert proposal.pending_reason == ld.PENDING_CONFLICTING_EVIDENCE
    assert proposal.canonical_dimension is None


def test_the_classifier_only_looks_at_language_candidates():
    assert ld.is_language_candidate(_field(source_field="languageCode"))
    assert ld.is_language_candidate(_field(source_field="target_languages"))
    assert not ld.is_language_candidate(_field(source_field="campaign_name"))


def test_propose_bindings_from_catalog_is_deterministic_and_data_driven():
    catalog = {
        "connector": "src_z",
        "fields": [
            _field(source_field="campaign_name", field_id="campaign_name", section="X"),
            _field(
                source_field="language",
                field_id="language",
                section="DEVICE",
                description="The language setting of the user's browser or device.",
            ),
            _field(
                source_field="languageCode",
                field_id="languageCode",
                section="DEVICE",
                description="The language setting of the user's browser or device (ISO 639).",
            ),
        ],
    }
    proposals = ld.propose_bindings_from_catalog(catalog)
    assert [p.source_field for p in proposals] == ["language", "languageCode"]
    assert {p.connector for p in proposals} == {"src_z"}
    assert {p.report_id for p in proposals} == {"DEVICE"}
    # Two columns at ONE provider -> ONE dimension, two encodings.
    assert ld.group_encoding_variants(proposals) == {
        ("src_z", "DEVICE", ld.AUDIENCE_LANGUAGE): ("language", "languageCode")
    }
    assert ld.propose_bindings_from_catalog(catalog) == proposals  # deterministic


# ---------------------------------------------------------------------------
# F. Persistence invariants provable without a database.
# ---------------------------------------------------------------------------


def test_the_automatic_path_can_never_write_a_confirmed_binding():
    forged = ld.BindingProposal(
        connector="src_a",
        report_id="R1",
        source_field="language",
        canonical_dimension=ld.AUDIENCE_LANGUAGE,
        status=ld.STATUS_CONFIRMED,
        confidence=1.0,
        evidence=ld.BindingEvidence(quote="", source=ld.EVIDENCE_NONE),
        rationale="",
    )
    with pytest.raises(ValueError, match="human act"):
        ld.persist_binding_proposals([forged])


def test_only_confirmed_bindings_resolve():
    rows = [
        {
            "connector": "src_a",
            "report_id": "R1",
            "source_field": "language",
            "canonical_dimension": ld.AUDIENCE_LANGUAGE,
            "status": ld.STATUS_PROPOSED,
            "scope_level": ld.SCOPE_PLATFORM,
        },
        {
            "connector": "src_a",
            "report_id": "R1",
            "source_field": "other",
            "canonical_dimension": ld.CONTENT_LANGUAGE,
            "status": ld.STATUS_PENDING,
            "scope_level": ld.SCOPE_PLATFORM,
        },
    ]
    assert ld.reduce_bindings_by_specificity(rows) == {}


def test_binding_cascade_project_beats_org_beats_platform():
    def _row(scope, dimension):
        return {
            "connector": "src_a",
            "report_id": "R1",
            "source_field": "language",
            "canonical_dimension": dimension,
            "status": ld.STATUS_CONFIRMED,
            "scope_level": scope,
        }

    winners = ld.reduce_bindings_by_specificity(
        [
            _row(ld.SCOPE_PLATFORM, ld.AUDIENCE_LANGUAGE),
            _row(ld.SCOPE_ORG, ld.CONTENT_LANGUAGE),
            _row(ld.SCOPE_PROJECT, ld.TARGETING_LANGUAGE),
        ]
    )
    winner = winners[("src_a", "R1", "language")]
    assert winner["canonical_dimension"] == ld.TARGETING_LANGUAGE


# ---------------------------------------------------------------------------
# G. The governed language vocabulary (seed).
# ---------------------------------------------------------------------------


def test_the_seed_loads_and_is_governed():
    vocabulary = load_language_vocabulary()
    assert len(vocabulary) >= 30
    subtags = [item.subtag for item in vocabulary]
    assert subtags == sorted(subtags)          # deterministic order
    assert len(subtags) == len(set(subtags))   # no duplicate
    assert all(item.subtag == item.subtag.lower() for item in vocabulary)


def test_display_names_and_codes_all_resolve_to_the_same_subtag():
    for raw in ("fr", "FR", "fra", "fre", "French", "FRENCH"):
        assert normalize_language_subtag(raw) == "fr"
    assert normalize_language_subtag("Deutsch") == "de"
    assert normalize_language_subtag("totally-unknown") is None


def test_an_ambiguous_alias_is_refused_by_the_loader(tmp_path):
    seed = tmp_path / "dim_language.csv"
    seed.write_text(
        "canonical_value,display_name,aliases\n"
        "fr,French,fr|Frisian\n"
        "fy,Frisian,fy|Frisian\n",
        encoding="utf-8",
    )
    with pytest.raises(LanguageVocabularyError):
        load_language_vocabulary(seed)
