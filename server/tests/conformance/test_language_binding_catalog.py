"""Story 27.8 -- the REAL catalogs are the proof: one word, four concepts, two metrics.

The point of this suite is not to test the classifier on invented data (that is
tests/core/test_language_dimensions.py) but to run it over the catalogs SHIPPED IN THIS
REPO and check the verdict of every single field that mentions a language. The catalogs are
DATA: nothing about a provider is encoded in ``core.language_dimensions``; the expectation
table below lives in the TEST, where the contract of the story belongs.

If a catalog changes and a language field moves, or a new connector ships one, this suite
fails loudly -- which is the intent: a field of this family must never be bound silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core import language_dimensions as ld

_MODULES_DIR = Path(__file__).resolve().parents[2] / "modules"

# (connector, source_field) -> (canonical_dimension, status, reason)
# Transcribed from the epic-27 amendment table (2026-07-25). The classifier must reproduce
# it from the providers' own descriptions alone.
_EXPECTED: dict[tuple[str, str], tuple[str | None, str, str | None]] = {
    # OBSERVED on the person -- the description says 'browser'/'device'/'user'.
    ("google-analytics", "language"): (ld.AUDIENCE_LANGUAGE, ld.STATUS_PROPOSED, None),
    ("google-analytics", "languageCode"): (ld.AUDIENCE_LANGUAGE, ld.STATUS_PROPOSED, None),
    ("piano", "browser_language"): (ld.AUDIENCE_LANGUAGE, ld.STATUS_PROPOSED, None),
    # PROPERTY OF THE ASSET served.
    ("amazon-ads", "creativeLanguage"): (ld.CONTENT_LANGUAGE, ld.STATUS_PROPOSED, None),
    ("amazon-dsp", "creativeLanguage"): (ld.CONTENT_LANGUAGE, ld.STATUS_PROPOSED, None),
    # DECLARED INTENT.
    ("amazon-ads", "lineItemLanguageTargeting"): (
        ld.TARGETING_LANGUAGE, ld.STATUS_PROPOSED, None
    ),
    ("amazon-dsp", "lineItemLanguageTargeting"): (
        ld.TARGETING_LANGUAGE, ld.STATUS_PROPOSED, None
    ),
    # AMBIGUOUS -- a proposition, never a decision.
    ("tiktok-ads", "language"): (
        ld.AUDIENCE_LANGUAGE, ld.STATUS_PENDING, ld.PENDING_AMBIGUOUS_AUDIENCE_TERM
    ),
    ("microsoft-ads", "Language"): (
        ld.TARGETING_LANGUAGE, ld.STATUS_PENDING, ld.PENDING_CONFIGURED_ATTRIBUTE
    ),
    ("thetradedesk", "Language"): (None, ld.STATUS_PENDING, ld.PENDING_NO_EVIDENCE),
    # OUT OF THE FAMILY.
    ("google-ads", "segments.product_language"): (
        None, ld.STATUS_EXCLUDED, ld.EXCLUDED_ITEM_ATTRIBUTE
    ),
    ("ias", "offensiveLanguageImps"): (None, ld.STATUS_EXCLUDED, ld.EXCLUDED_METRIC_HOMONYM),
    ("ias", "failedByLanguageImps"): (None, ld.STATUS_EXCLUDED, ld.EXCLUDED_METRIC_HOMONYM),
}


def _all_proposals() -> list[ld.BindingProposal]:
    proposals: list[ld.BindingProposal] = []
    for catalog_path in sorted(_MODULES_DIR.glob("*/api_catalog.json")):
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        proposals.extend(ld.propose_bindings_from_catalog(catalog))
    return proposals


@pytest.fixture(scope="module")
def proposals() -> list[ld.BindingProposal]:
    found = _all_proposals()
    assert found, "no catalog field mentioning a language was found -- suite is vacuous"
    return found


def test_every_language_field_of_every_catalog_has_the_expected_verdict(proposals):
    actual = {
        (p.connector, p.source_field): (
            p.canonical_dimension,
            p.status,
            p.pending_reason or p.excluded_reason,
        )
        for p in proposals
    }
    assert actual == _EXPECTED


def test_one_word_really_does_cover_several_concepts(proposals):
    """The premise of the story, verified on the shipped catalogs, not asserted."""
    dimensions = {p.canonical_dimension for p in proposals if p.canonical_dimension}
    assert dimensions == set(ld.LANGUAGE_DIMENSION_FAMILY)  # the three, all present
    assert any(p.status == ld.STATUS_EXCLUDED for p in proposals)  # and homonym metrics


def test_no_field_is_ever_bound_to_a_single_generic_language_dimension(proposals):
    assert all(
        p.canonical_dimension in (None, *ld.LANGUAGE_DIMENSION_FAMILY) for p in proposals
    )
    assert all(p.canonical_dimension != "language" for p in proposals)


def test_nothing_is_auto_confirmed_and_everything_carries_its_evidence(proposals):
    for proposal in proposals:
        assert proposal.status in (
            ld.STATUS_PROPOSED, ld.STATUS_PENDING, ld.STATUS_EXCLUDED
        )
        assert proposal.confidence < 1.0
        assert proposal.rationale                      # the reasoning is readable
        assert proposal.evidence.source in (
            ld.EVIDENCE_DESCRIPTION, ld.EVIDENCE_FIELD_NAME, ld.EVIDENCE_NONE
        )


def test_the_cited_evidence_is_the_provider_text_verbatim():
    """The proof must be quotable: the stored quote is a SUBSTRING of the catalog itself."""
    for catalog_path in sorted(_MODULES_DIR.glob("*/api_catalog.json")):
        raw = catalog_path.read_text(encoding="utf-8")
        catalog = json.loads(raw)
        for proposal in ld.propose_bindings_from_catalog(catalog):
            if not proposal.evidence.quote:
                continue
            descriptions = {
                str(f.get("description") or "")
                for f in catalog.get("fields") or ()
                if isinstance(f, dict)
            }
            # Verbatim: the quote is one of the catalog's own description strings.
            assert proposal.evidence.quote in descriptions


def test_a_provider_exposing_two_encodings_still_feeds_one_dimension(proposals):
    groups = ld.group_encoding_variants(proposals)
    multi = {key: fields for key, fields in groups.items() if len(fields) > 1}
    # At least one provider ships a label column AND a code column for the same dimension.
    assert multi, "expected at least one encoding pair in the shipped catalogs"
    for (_connector, _report, dimension), fields in multi.items():
        assert dimension in ld.LANGUAGE_DIMENSION_FAMILY
        assert len(set(fields)) == len(fields)


def test_the_family_members_are_never_offered_together_for_aggregation(proposals):
    """Two connectors can feed two different members -- and they still never combine."""
    by_dimension: dict[str, set[str]] = {}
    for proposal in proposals:
        if proposal.canonical_dimension:
            by_dimension.setdefault(proposal.canonical_dimension, set()).add(
                proposal.connector
            )
    assert len(by_dimension) > 1
    with pytest.raises(ld.IncommensurableDimensions):
        ld.assert_language_dimensions_comparable(sorted(by_dimension))
