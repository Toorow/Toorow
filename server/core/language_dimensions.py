"""toorow -- The LANGUAGE dimension FAMILY: three dimensions, never one (Story 27.8).

Country is ONE concept with several spellings: 'FR', 'France', 'FRANCE' designate the same
thing and conforming them is LOSSLESS. Language is the opposite trap: SEVERAL CONCEPTS
UNDER ONE WORD. Our own connector catalogs prove it -- a textual match on 'language' brings
back four concepts and two homonymous METRICS. Merging them into a single 'language'
dimension would be exactly the error epic-27 guards against: aligning identity does not
license comparing the incommensurable.

THE THREE DIMENSIONS (never merged, never summed, never compared with each other)
--------------------------------------------------------------------------------
  * ``audience_language``  -- OBSERVED on the person (a browser/device/user setting).
  * ``content_language``   -- a PROPERTY OF THE ASSET that was served.
  * ``targeting_language`` -- a DECLARED INTENT (what was asked for).

Each is homogeneous on its own. Between them there is NO total and NO comparison of
measures: the guard is the SAME mechanism metric reconciliation already uses for a
sensitive metric group -- ``KEEP_SEPARATE`` (imported from ``core.metric_reconciliation``,
not re-spelled here), i.e. N independent series and a structured reason, never a figure.

THE DIVERGENCE IS INFORMATION, NOT AN ANOMALY
---------------------------------------------
targeting_language vs audience_language ("we aimed at FR and reached NL-BE speakers") is a
READING, offered by ``describe_reach_divergence`` as two juxtaposed sets -- no delta
metric, no reconciliation, nothing to "fix". A third party already SELLS the measure of
that gap as a metric of its own, which is itself the proof the two dimensions are distinct.

VALUES ARE BCP 47
-----------------
'fr', 'fr-FR' -- language lowercase, region uppercase. NOT bare ISO 639: the locale would
be lost at ingestion, definitively, and no downstream choice could ever bring it back.

ENCODING IS NOT A DIMENSION
---------------------------
A provider exposing BOTH a label ('English') and a code ('en') exposes TWO ENCODINGS OF ONE
dimension. A per-source adapter normalizes them to the canonical value -- exactly the
doctrine of native money encodings normalized to canonical micros. Two columns at one
provider never create two dimensions (see ``group_encoding_variants``).

GRAIN
-----
Conform at the FINEST grain the source gives; the PROJECT chooses its reporting grain. The
rollup 'fr-FR' -> 'fr' ships as a RECOMMENDED, PRE-FILLED, OVERRIDABLE pre-mapping. A
default is legitimate HERE because it is DERIVED FROM THE BCP 47 STANDARD, not from an
opinion -- unlike geographic markets (epic-37), where composition is a business definition
and no default is legitimate. FORBIDDEN, and refused in code: conforming 'fr-BE' to
'fr-FR' -- a business decision disguised as normalization (see
``validate_conformance_target``).

BINDING WITH CITED EVIDENCE
---------------------------
A field -> dimension proposal carries a CONFIDENCE and the EVIDENCE (the provider's own
description, verbatim, as it stands in the catalog). Without disambiguating evidence the
proposal stays ``pending``. This does not relax the 27.4 invariant: NOTHING is ever
auto-confirmed -- an exact match is only a higher confidence, never a blank cheque.

AD-2: ZERO provider vocabulary in this module. The classifier reads the catalogs as DATA
and reasons on GENERIC English markers ('browser', 'creative', 'targeting', ...); no
connector, report or provider name appears anywhere below.

Design mirrors dimension_conformance.py: ``from __future__ import annotations``, module
logger, lazy ``core.db`` imports inside the DB functions, pure functions kept separate from
I/O so every invariant is testable without Postgres, ASCII-only log strings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from core.metric_reconciliation import (  # the SAME guard mechanism, imported not copied
    CODE_KEEP_SEPARATE,
    KEEP_SEPARATE,
    METHOD_KEEP_SEPARATE,
    ReconciliationReason,
)
from core.metric_semantics import (  # noqa: F401 -- re-exported socle symbols (27.1)
    SCOPE_ORG,
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
    InvalidScope,
    _mint_id,
    _write_semantics_audit,
    validate_scope,
)

logger = logging.getLogger(__name__)

__all__ = [
    # family
    "AUDIENCE_LANGUAGE",
    "CONTENT_LANGUAGE",
    "TARGETING_LANGUAGE",
    "LANGUAGE_DIMENSION_FAMILY",
    "LanguageDimension",
    "is_language_dimension",
    "describe_dimension",
    # guard
    "IncommensurableDimensions",
    "assert_language_dimensions_comparable",
    "keep_separate_decision",
    "describe_reach_divergence",
    # values
    "LanguageTag",
    "parse_language_tag",
    "adapt_source_value",
    "group_encoding_variants",
    # grain / rollup
    "GRAIN_LOCALE",
    "GRAIN_LANGUAGE",
    "METHOD_STANDARD_ROLLUP",
    "recommended_rollup",
    "apply_grain",
    "IllegitimateLanguageConformance",
    "validate_conformance_target",
    "build_rollup_premapping",
    "resolve_reporting_language",
    # binding
    "BindingEvidence",
    "BindingProposal",
    "propose_binding",
    "propose_bindings_from_catalog",
    # persistence
    "persist_binding_proposals",
    "confirm_binding",
    "reject_binding",
    "list_bindings",
    "resolve_field_bindings",
]

# ===========================================================================
# A. THE FAMILY -- three conformed dimensions, declared once.
#
# These identifiers are CANONICAL platform vocabulary (not provider vocabulary): they are
# the stable internal ids written into mapping rows and lineage. What the user READS is the
# client-owned label resolved by core.dimension_conformance.resolve_dimension_label
# (27.9) -- 'audience_language' is never shown as-is when a label exists.
# ===========================================================================

AUDIENCE_LANGUAGE = "audience_language"
CONTENT_LANGUAGE = "content_language"
TARGETING_LANGUAGE = "targeting_language"

# What the value is ABOUT. This is the reason the three cannot collapse into one.
NATURE_OBSERVED = "observed_on_person"        # measured on the human being reached
NATURE_ASSET_PROPERTY = "property_of_asset"   # a characteristic of what was served
NATURE_DECLARED_INTENT = "declared_intent"    # a setting, asked for, not measured


@dataclass(frozen=True)
class LanguageDimension:
    """One member of the family (PURE data; the family is closed and declared below)."""

    identifier: str
    nature: str
    definition: str


LANGUAGE_DIMENSION_FAMILY: dict[str, LanguageDimension] = {
    AUDIENCE_LANGUAGE: LanguageDimension(
        identifier=AUDIENCE_LANGUAGE,
        nature=NATURE_OBSERVED,
        definition=(
            "Language OBSERVED on the person reached (browser / device / user setting). "
            "A measurement, not a choice of ours."
        ),
    ),
    CONTENT_LANGUAGE: LanguageDimension(
        identifier=CONTENT_LANGUAGE,
        nature=NATURE_ASSET_PROPERTY,
        definition=(
            "Language of the ASSET actually served (the creative / the content). "
            "A property of the thing delivered, whoever received it."
        ),
    ),
    TARGETING_LANGUAGE: LanguageDimension(
        identifier=TARGETING_LANGUAGE,
        nature=NATURE_DECLARED_INTENT,
        definition=(
            "Language DECLARED as targeted (delivery setting). "
            "An intention, true even when nobody matching it was reached."
        ),
    ),
}


def is_language_dimension(dimension: str) -> bool:
    """True when *dimension* is a member of the language family."""
    return dimension in LANGUAGE_DIMENSION_FAMILY


def describe_dimension(dimension: str) -> LanguageDimension | None:
    """Return the family declaration of *dimension* (None when not a member)."""
    return LANGUAGE_DIMENSION_FAMILY.get(dimension)


# ===========================================================================
# B. THE GUARD -- never summed, never compared with each other.
#
# Same NATURE as a KEEP_SEPARATE metric group, and literally the same constants: the
# statuses/codes are IMPORTED from core.metric_reconciliation so there is ONE vocabulary of
# non-combination on the platform, not two that could drift apart.
# ===========================================================================

# Machine-readable reason code carried by every refusal (a specialisation of the metric
# KEEP_SEPARATE code, so a consumer that already handles KEEP_SEPARATE handles this too).
CODE_LANGUAGE_FAMILY_KEEP_SEPARATE = f"{CODE_KEEP_SEPARATE}_LANGUAGE_FAMILY"

_GUARD_MESSAGE = (
    "language family: '{a}' ({na}) and '{b}' ({nb}) are DIFFERENT concepts under one word "
    "-- no total, no comparison of measures between them (rule KEEP_SEPARATE). The gap "
    "between an intent and an observation is an INFORMATION, read it with "
    "describe_reach_divergence."
)


class IncommensurableDimensions(Exception):
    """Raised when a caller tries to combine two members of the language family."""


@dataclass(frozen=True)
class DimensionSeries:
    """One independent series, one per family member. NEVER added to another."""

    dimension: str
    nature: str
    connector: str | None = None


@dataclass(frozen=True)
class LanguageComparisonDecision:
    """The typed refusal: N series, no total, one structured reason (AD-9 shape).

    Mirrors ``metric_reconciliation.RouteDecision``: ``status`` is the imported
    ``KEEP_SEPARATE``, ``total`` does not exist as a field at all (there is nothing to put
    in it), and ``reason`` is the invitation to read the divergence instead."""

    status: str
    method: str
    dimensions: tuple[str, ...]
    series: tuple[DimensionSeries, ...]
    reason: ReconciliationReason


def assert_language_dimensions_comparable(dimensions) -> None:
    """Raise IncommensurableDimensions as soon as TWO DISTINCT family members are mixed.

    PURE. The contract of every aggregation/comparison surface that accepts a dimension
    list: call this first. Non-family dimensions are ignored (this guard owns the language
    family only); one single family member is always fine (a series of audience languages
    is perfectly summable WITHIN itself)."""
    members = []
    for dimension in dimensions or ():
        if is_language_dimension(dimension) and dimension not in members:
            members.append(dimension)
    if len(members) < 2:
        return
    first, second = members[0], members[1]
    raise IncommensurableDimensions(
        _GUARD_MESSAGE.format(
            a=first,
            na=LANGUAGE_DIMENSION_FAMILY[first].nature,
            b=second,
            nb=LANGUAGE_DIMENSION_FAMILY[second].nature,
        )
    )


def keep_separate_decision(
    dimensions, *, connector: str | None = None
) -> LanguageComparisonDecision:
    """Build the honest KEEP_SEPARATE decision for a mix of family members (PURE).

    The non-raising face of the guard, for surfaces that must RENDER the refusal rather than
    fail: one series per dimension, deterministically ordered, no total anywhere."""
    members = sorted({d for d in (dimensions or ()) if is_language_dimension(d)})
    if not members:
        raise ValueError("keep_separate_decision requires at least one language dimension")
    series = tuple(
        DimensionSeries(
            dimension=d, nature=LANGUAGE_DIMENSION_FAMILY[d].nature, connector=connector
        )
        for d in members
    )
    if len(members) >= 2:
        message = _GUARD_MESSAGE.format(
            a=members[0],
            na=LANGUAGE_DIMENSION_FAMILY[members[0]].nature,
            b=members[1],
            nb=LANGUAGE_DIMENSION_FAMILY[members[1]].nature,
        )
    else:
        message = (
            f"language family: '{members[0]}' is homogeneous on its own; it must never be "
            "totalled with another member of the family"
        )
    return LanguageComparisonDecision(
        status=KEEP_SEPARATE,
        method=METHOD_KEEP_SEPARATE,
        dimensions=tuple(members),
        series=series,
        reason=ReconciliationReason(
            code=CODE_LANGUAGE_FAMILY_KEEP_SEPARATE,
            message=message,
            emitters=(connector,) if connector else (),
        ),
    )


# The divergence read-out: an INFORMATION, explicitly not an anomaly and not a total.
DIVERGENCE_INFORMATIONAL = "informational"


@dataclass(frozen=True)
class ReachDivergence:
    """Juxtaposition (NOT comparison of measures) of an intent and an observation.

    ``targeted_not_reached`` / ``reached_not_targeted`` are SETS of canonical tags. There is
    deliberately no delta, no ratio and no score: the platform does not own the judgement of
    whether the gap is good or bad. ``is_anomaly`` is a constant False and
    ``interpretation`` says so, so no consumer can dress this up as an error to reconcile.
    """

    targeted: tuple[str, ...]
    reached: tuple[str, ...]
    targeted_not_reached: tuple[str, ...]
    reached_not_targeted: tuple[str, ...]
    both: tuple[str, ...]
    interpretation: str = DIVERGENCE_INFORMATIONAL
    is_anomaly: bool = False


def describe_reach_divergence(targeting_values, audience_values) -> ReachDivergence:
    """Read the targeting_language <-> audience_language gap as INFORMATION (PURE).

    The ONE sanctioned juxtaposition of two family members -- and it is a juxtaposition of
    SETS OF VALUES, never an arithmetic of their measures. Values are parsed to canonical
    BCP 47 tags first (so 'FR' and 'fr' are the same tag); unparseable values are dropped
    rather than guessed."""
    targeted = {t.canonical for t in (parse_language_tag(v) for v in targeting_values or ()) if t}
    reached = {t.canonical for t in (parse_language_tag(v) for v in audience_values or ()) if t}
    return ReachDivergence(
        targeted=tuple(sorted(targeted)),
        reached=tuple(sorted(reached)),
        targeted_not_reached=tuple(sorted(targeted - reached)),
        reached_not_targeted=tuple(sorted(reached - targeted)),
        both=tuple(sorted(targeted & reached)),
    )


# ===========================================================================
# C. VALUES -- BCP 47, and the per-source ENCODING adapter.
# ===========================================================================

ENCODING_SUBTAG = "subtag"                      # 'fr'
ENCODING_TAG = "bcp47_tag"                      # 'fr-FR'
ENCODING_LOCALE_UNDERSCORE = "locale_underscore"  # 'fr_FR'
ENCODING_DISPLAY_NAME = "display_name"          # 'French' / 'English'

_ALPHA2_RE = re.compile(r"^[A-Za-z]{2}$")
_ALPHA4_RE = re.compile(r"^[A-Za-z]{4}$")
_DIGIT3_RE = re.compile(r"^[0-9]{3}$")
_CODE_LIKE_RE = re.compile(r"^[A-Za-z]{2,3}$")


@dataclass(frozen=True)
class LanguageTag:
    """A parsed, canonical BCP 47 value. ``canonical`` is what we store, always."""

    canonical: str            # 'fr' | 'fr-FR' | 'zh-Hant-TW'
    language: str             # primary subtag, lowercase
    region: str | None = None  # region subtag, UPPERCASE (or UN M.49 digits)
    script: str | None = None  # script subtag, Titlecase
    source_encoding: str = ENCODING_TAG


def _known_region(region: str) -> bool:
    """True when *region* is a governed country code. Fail-soft on an unreadable registry.

    The region half of a BCP 47 tag is governed by the country registry that ALREADY exists
    -- this module never forks it. If that registry cannot be read we accept the SHAPE
    rather than crash (fail-soft), and say nothing we cannot prove."""
    if _DIGIT3_RE.fullmatch(region):
        return True  # UN M.49 area code: shape-valid, no registry of ours governs it.
    try:
        from core.country_vocabulary import get_supported_country_codes  # noqa: PLC0415

        return region.upper() in get_supported_country_codes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("language_dimensions: country registry unavailable: %s", exc)
        return True


def parse_language_tag(raw_value: object) -> LanguageTag | None:
    """Parse ONE source value into a canonical BCP 47 tag, or None when unmapped.

    Accepts the encodings a source may legitimately use for the SAME dimension -- a bare
    subtag ('fr'), a full tag ('fr-FR', 'FR-fr'), an underscore locale ('fr_FR'), or a
    display name ('French', 'ENGLISH'). All of them come out as ONE canonical value, which
    is the whole point of 'encoding is not a dimension'.

    HONEST, NEVER A GUESS: an unknown language, an unknown region, or any shape this
    version does not model (variants, extensions, private-use) returns None so the caller
    reports the gap instead of storing an invented value."""
    from core.language_vocabulary import normalize_language_subtag  # noqa: PLC0415

    if not isinstance(raw_value, str):
        return None
    text = raw_value.strip()
    if not text:
        return None

    encoding = ENCODING_TAG
    if "_" in text:
        encoding = ENCODING_LOCALE_UNDERSCORE
        text = text.replace("_", "-")

    parts = [p for p in text.split("-") if p != ""]
    if not parts or len(parts) > 3:
        # >3 subtags means variants/extensions/private-use: not modelled in v1 -> honest None.
        return None

    language = normalize_language_subtag(parts[0])
    if language is None:
        return None

    if len(parts) == 1:
        if encoding != ENCODING_LOCALE_UNDERSCORE:
            encoding = (
                ENCODING_SUBTAG if _CODE_LIKE_RE.fullmatch(parts[0]) else ENCODING_DISPLAY_NAME
            )
        return LanguageTag(
            canonical=language, language=language, source_encoding=encoding
        )

    script: str | None = None
    region: str | None = None
    for part in parts[1:]:
        if _ALPHA4_RE.fullmatch(part) and script is None:
            script = part.capitalize()
        elif (_ALPHA2_RE.fullmatch(part) or _DIGIT3_RE.fullmatch(part)) and region is None:
            candidate = part.upper()
            if not _known_region(candidate):
                return None  # an unknown region is a gap, never a silently dropped one.
            region = candidate
        else:
            return None

    canonical = language
    if script:
        canonical = f"{canonical}-{script}"
    if region:
        canonical = f"{canonical}-{region}"
    return LanguageTag(
        canonical=canonical,
        language=language,
        region=region,
        script=script,
        source_encoding=encoding,
    )


@dataclass(frozen=True)
class AdaptedValue:
    """What a per-source adapter produces for ONE raw value (PURE)."""

    source_value: str
    tag: LanguageTag | None
    canonical_value: str | None
    gap_reason: str | None = None


GAP_UNPARSEABLE = "unparseable_language_value"


def adapt_source_value(raw_value: object) -> AdaptedValue:
    """THE per-source adapter: native encoding in, canonical BCP 47 out.

    Same doctrine as the money adapters (native unit in, canonical micros out): the
    provider's encoding is an INPUT CONTRACT, the output is always the canonical value. A
    provider exposing both a label and a code feeds this adapter TWICE and gets the SAME
    canonical value -- which is why it is one dimension, not two."""
    text = raw_value if isinstance(raw_value, str) else ""
    tag = parse_language_tag(raw_value)
    if tag is None:
        return AdaptedValue(
            source_value=text, tag=None, canonical_value=None, gap_reason=GAP_UNPARSEABLE
        )
    return AdaptedValue(source_value=text, tag=tag, canonical_value=tag.canonical)


def group_encoding_variants(proposals) -> dict[tuple[str, str, str], tuple[str, ...]]:
    """Group binding proposals that are TWO ENCODINGS OF ONE dimension (PURE).

    Key: (connector, report_id, canonical_dimension) -> the source fields that all feed it.
    A key with several fields is the encoding case (a label column AND a code column): the
    curation surface shows ONE dimension fed by several encodings, and never proposes to
    create a second dimension."""
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for proposal in proposals or ():
        if not proposal.canonical_dimension:
            continue
        key = (proposal.connector, proposal.report_id, proposal.canonical_dimension)
        grouped.setdefault(key, []).append(proposal.source_field)
    return {key: tuple(sorted(fields)) for key, fields in sorted(grouped.items())}


# ===========================================================================
# D. GRAIN -- the standard-derived rollup, recommended / pre-filled / overridable.
# ===========================================================================

GRAIN_LOCALE = "locale"     # 'fr-FR' -- the finest grain the source gave
GRAIN_LANGUAGE = "language"  # 'fr'   -- the standard-derived rollup

# A suggestion method of its own (mirrors the CHECK extended by migration 105). It is NOT
# 'normalized': the provenance of this proposal is the BCP 47 STANDARD, and a curator must
# be able to see that at a glance -- and to filter it out wholesale if they disagree.
METHOD_STANDARD_ROLLUP = "standard_rollup"

# Where a resolved reporting value came from (contract of resolve_reporting_language).
PROVENANCE_CLIENT_OVERRIDE = "client_override"   # a CONFIRMED client mapping won
PROVENANCE_STANDARD_ROLLUP = "standard_rollup"   # the BCP 47 rollup applied
PROVENANCE_SOURCE_GRAIN = "source_grain"         # the source value, canonicalised only
PROVENANCE_UNMAPPED = "unmapped"                 # nothing honest to return


class IllegitimateLanguageConformance(Exception):
    """A proposed language conformance is a BUSINESS decision disguised as normalization."""


def recommended_rollup(value: object) -> str | None:
    """Return the standard-derived rollup of a value ('fr-FR' -> 'fr'), or None.

    PURE, and derived from BCP 47 alone: the rollup of a tag is its PRIMARY LANGUAGE
    SUBTAG. This is the ONE default this story ships, and it is legitimate precisely
    because it is read off the standard -- no business opinion is embedded in it."""
    tag = value if isinstance(value, LanguageTag) else parse_language_tag(value)
    return tag.language if tag else None


def apply_grain(value: object, grain: str = GRAIN_LOCALE) -> str | None:
    """Project a value onto the PROJECT's reporting grain (PURE).

    GRAIN_LOCALE keeps everything the source gave (we conform at the finest grain, always);
    GRAIN_LANGUAGE applies the standard rollup. Unknown grain -> ValueError (a silent
    fallback would be a grain decided by accident)."""
    if grain not in (GRAIN_LOCALE, GRAIN_LANGUAGE):
        raise ValueError(f"unknown language reporting grain: {grain!r}")
    tag = value if isinstance(value, LanguageTag) else parse_language_tag(value)
    if tag is None:
        return None
    return tag.language if grain == GRAIN_LANGUAGE else tag.canonical


def validate_conformance_target(source_value: object, canonical_value: object) -> None:
    """Refuse a language conformance that is NOT a normalization (PURE). Raises or returns.

    The only two legitimate targets for a source value are:
      * ITSELF, canonicalised (an encoding change: 'French' / 'FR-fr' -> 'fr-FR'), and
      * its STANDARD ROLLUP (the primary subtag: 'fr-FR' -> 'fr').

    Everything else is a business decision:
      * 'fr-BE' -> 'fr-FR' (same language, ANOTHER region) -- EXPLICITLY FORBIDDEN: it
        erases a market under the pretext of normalizing a language;
      * 'fr' -> 'fr-FR' -- inventing a region the source never gave;
      * any cross-language mapping.

    Call this BEFORE persisting any mapping on a language dimension."""
    src = (
        source_value
        if isinstance(source_value, LanguageTag)
        else parse_language_tag(source_value)
    )
    dst = (
        canonical_value
        if isinstance(canonical_value, LanguageTag)
        else parse_language_tag(canonical_value)
    )
    if src is None or dst is None:
        raise IllegitimateLanguageConformance(
            "language conformance refused: both sides must be valid BCP 47 values "
            f"(source={source_value!r}, canonical={canonical_value!r})"
        )
    if dst.canonical == src.canonical:
        return
    if dst.canonical == src.language:
        return  # the standard rollup, the one shipped default.
    if dst.language == src.language:
        raise IllegitimateLanguageConformance(
            f"language conformance refused: '{src.canonical}' -> '{dst.canonical}' keeps the "
            "language and CHANGES the region. That is a business decision disguised as "
            f"normalization -- the only standard rollup of '{src.canonical}' is "
            f"'{src.language}'"
        )
    raise IllegitimateLanguageConformance(
        f"language conformance refused: '{src.canonical}' -> '{dst.canonical}' maps one "
        "language onto another; normalization never changes the language"
    )


@dataclass(frozen=True)
class RollupPremapping:
    """The recommended pre-mapping: the suggestions to pre-fill, plus the honest gaps."""

    suggestions: tuple  # tuple[dimension_conformance.Suggestion, ...]
    gaps: tuple[AdaptedValue, ...]
    grain: str


def build_rollup_premapping(
    values_by_connector: dict[str, list[str]], *, grain: str = GRAIN_LANGUAGE
) -> RollupPremapping:
    """Build the RECOMMENDED, PRE-FILLED, OVERRIDABLE pre-mapping (PURE, no DB).

    Emits one ``dimension_conformance.Suggestion`` per raw value whose canonical target
    differs from the raw string -- i.e. both the ENCODING normalization ('English' -> 'en')
    and, at GRAIN_LANGUAGE, the standard ROLLUP ('fr-FR' -> 'fr').

    HOW IT IS OVERRIDDEN -- no new mechanism: the caller persists these through
    ``dimension_conformance.persist_suggestions`` at PLATFORM scope, so they land as
    ``status='proposed'`` (pre-filled and visible, never auto-applied to a stored value),
    and a client who disagrees writes a CONFIRMED row at ORG or PROJECT scope through
    ``set_mapping_manual``. The existing cascade PROJECT > ORG > PLATFORM then makes the
    client's row win, pair by pair. At READ, ``resolve_reporting_language`` gives the same
    precedence: a confirmed client mapping always beats the standard rollup.

    Values that do not parse produce NO suggestion; they are returned as gaps."""
    from core.dimension_conformance import Suggestion  # noqa: PLC0415

    if grain not in (GRAIN_LOCALE, GRAIN_LANGUAGE):
        raise ValueError(f"unknown language reporting grain: {grain!r}")

    suggestions: list = []
    gaps: list[AdaptedValue] = []
    seen: set[tuple[str, str]] = set()
    for connector in sorted(values_by_connector or {}):
        for raw in values_by_connector[connector]:
            key = (connector, raw if isinstance(raw, str) else "")
            if key in seen:
                continue
            seen.add(key)
            adapted = adapt_source_value(raw)
            if adapted.tag is None:
                gaps.append(adapted)
                continue
            target = apply_grain(adapted.tag, grain)
            if target is None or target == adapted.source_value:
                continue
            # Defensive: the builder can only ever emit identity or standard rollup.
            validate_conformance_target(adapted.tag, target)
            suggestions.append(
                Suggestion(
                    connector=connector,
                    source_value=adapted.source_value,
                    canonical_value=target,
                    method=METHOD_STANDARD_ROLLUP,
                    score=1.0,
                    normalized_form=adapted.tag.canonical,
                )
            )
    suggestions.sort(key=lambda s: (s.connector, s.source_value))
    return RollupPremapping(
        suggestions=tuple(suggestions), gaps=tuple(gaps), grain=grain
    )


@dataclass(frozen=True)
class ResolvedLanguage:
    """One resolved reporting value + WHERE it came from (never a bare string)."""

    value: str | None
    provenance: str
    tag: LanguageTag | None = None
    gap_reason: str | None = None


def resolve_reporting_language(
    org_id: str,
    dimension: str,
    connector: str,
    source_value: str,
    *,
    project_id: str | None = None,
    grain: str = GRAIN_LOCALE,
    conformer=None,
) -> ResolvedLanguage:
    """Resolve ONE raw value to the value the project reports on, WITH its provenance.

    Order, and it is the whole override story in three lines:
      1. the CLIENT wins -- a CONFIRMED mapping (cascade PROJECT > ORG > PLATFORM, read
         through ``dimension_conformance.conform_value``) is returned as-is;
      2. otherwise the source value is canonicalised (encoding adapter) and projected onto
         the project's grain -- the standard rollup, our only default;
      3. otherwise nothing honest exists -> value None, provenance 'unmapped'.

    Raises ValueError when *dimension* is not a member of the language family (this
    resolution is family-specific; a caller pointing it at another dimension has a bug).
    ``conformer`` is injectable so the whole precedence is testable without Postgres."""
    if not is_language_dimension(dimension):
        raise ValueError(
            f"resolve_reporting_language expects a language family dimension, got {dimension!r}"
        )
    if conformer is None:
        from core.dimension_conformance import conform_value as conformer  # noqa: PLC0415

    override = None
    try:
        override = conformer(
            org_id, dimension, connector, source_value, project_id=project_id
        )
    except Exception as exc:  # noqa: BLE001 -- fail-soft, exactly like conform_value itself
        logger.warning(
            "language_dimensions: override lookup failed org=%s dim=%s: %s",
            org_id,
            dimension,
            exc,
        )
    if override:
        return ResolvedLanguage(
            value=override,
            provenance=PROVENANCE_CLIENT_OVERRIDE,
            tag=parse_language_tag(override),
        )

    adapted = adapt_source_value(source_value)
    if adapted.tag is None:
        return ResolvedLanguage(
            value=None, provenance=PROVENANCE_UNMAPPED, gap_reason=adapted.gap_reason
        )
    value = apply_grain(adapted.tag, grain)
    provenance = (
        PROVENANCE_STANDARD_ROLLUP
        if value != adapted.tag.canonical
        else PROVENANCE_SOURCE_GRAIN
    )
    return ResolvedLanguage(value=value, provenance=provenance, tag=adapted.tag)


# ===========================================================================
# E. BINDING WITH CITED EVIDENCE -- field -> dimension, proposed, never auto-confirmed.
#
# The classifier reads the connector catalogs as DATA and reasons on a GENERIC lexicon of
# English markers. There is no provider name, no field name and no per-provider verdict
# hard-coded here (AD-2): the SAME rules applied to a catalog we have never seen produce a
# proposal or an honest 'pending'.
# ===========================================================================

STATUS_PROPOSED = "proposed"    # a defensible proposal; a human still confirms it
STATUS_PENDING = "pending"      # evidence does not settle it -- NOT confirmable as-is
STATUS_CONFIRMED = "confirmed"  # a human decided
STATUS_REJECTED = "rejected"    # a human refused
STATUS_EXCLUDED = "excluded"    # not a member of the dimension family at all

EVIDENCE_DESCRIPTION = "catalog_field_description"
EVIDENCE_FIELD_NAME = "catalog_field_name"
EVIDENCE_NONE = "no_evidence"
EVIDENCE_HUMAN = "human"

# Why a proposal cannot be settled by the evidence.
PENDING_AMBIGUOUS_AUDIENCE_TERM = "ambiguous_audience_term"
PENDING_CONFIGURED_ATTRIBUTE = "configured_attribute_not_observation"
PENDING_CONFLICTING_EVIDENCE = "conflicting_evidence"
PENDING_NO_EVIDENCE = "no_disambiguating_evidence"

# Why a field is out of the dimension family entirely.
EXCLUDED_METRIC_HOMONYM = "metric_homonym"
EXCLUDED_ITEM_ATTRIBUTE = "catalog_item_attribute"

# Confidence ladder. NOTHING reaches 1.0 and nothing is auto-confirmed: these are
# confidences, not permissions.
CONFIDENCE_DESCRIPTION_MARKER = 0.9
CONFIDENCE_NAME_MARKER = 0.75
CONFIDENCE_PENDING_PROPOSITION = 0.4
CONFIDENCE_NONE = 0.0

# --- the generic lexicon (English markers, ZERO provider vocabulary) -----------------
# Markers found in a provider's own DESCRIPTION that settle the nature of the field.
_DESCRIPTION_MARKERS: dict[str, tuple[str, ...]] = {
    AUDIENCE_LANGUAGE: (
        "browser", "device", "user's", "of the user", "visitor", "reader",
    ),
    CONTENT_LANGUAGE: (
        "creative", "ad copy", "asset", "content served", "of the content",
    ),
    TARGETING_LANGUAGE: (
        "targeting", "targeted", "target language", "you target",
    ),
}
# Markers found in the FIELD NAME. Weaker evidence (a name is a hint, not a definition).
_NAME_MARKERS: dict[str, tuple[str, ...]] = {
    AUDIENCE_LANGUAGE: ("browser", "user", "visitor"),
    CONTENT_LANGUAGE: ("creative", "content", "asset", "adcopy", "ad_copy"),
    TARGETING_LANGUAGE: ("target",),
}
# A word that CANNOT settle anything on its own. In an advertising context "audience" very
# often designates the targeted SEGMENT rather than an observed person -- which is exactly
# the ambiguity that must stay pending instead of being decided for the client.
_AMBIGUOUS_AUDIENCE_MARKERS = ("audience", "segment")
# A field described as a configuration ATTRIBUTE used to group rows is a SETTING, not an
# observation -- a proposition of intent, still pending.
_CONFIGURED_ATTRIBUTE_MARKERS = ("attribute", "group the data", "grouping", "ad group setting")
# An item/catalog property is a characteristic of a product, not of a delivery. Matched on
# NAME TOKENS, never on substrings: a substring match would swallow unrelated words (a field
# naming a delivery entity often contains a generic noun as a fragment).
_ITEM_MARKERS = ("product", "catalog", "catalogue", "sku", "offer")

_LANGUAGE_TOKEN = "lang"
# camelCase / snake_case / kebab-case boundaries -> the tokens of a field name.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _name_tokens(*names) -> tuple[str, ...]:
    """Split field names into lowercase tokens (PURE): 'lineItemLanguageTargeting' ->
    ('line','item','language','targeting'). Token matching keeps the lexicon honest -- a
    marker must be a WORD of the name, not an accidental fragment of another word."""
    tokens: list[str] = []
    for name in names:
        if not name:
            continue
        spaced = _CAMEL_BOUNDARY_RE.sub(" ", str(name))
        tokens.extend(t for t in _NON_ALNUM_RE.split(spaced.lower()) if t)
    return tuple(tokens)


def _matched_tokens(tokens, markers) -> tuple[str, ...]:
    """Markers matched as whole words (or as the stem of a word: 'target' ~ 'targeting')."""
    return tuple(m for m in markers if any(t.startswith(m) for t in tokens))


@dataclass(frozen=True)
class BindingEvidence:
    """The PROOF carried by a proposal: the provider's own words, verbatim.

    ``quote`` is copied unchanged from the catalog (never paraphrased -- a paraphrase is our
    opinion, and the point of the evidence is that it is NOT ours). ``markers`` are the
    generic tokens the classifier actually matched, so a reviewer can audit the reasoning
    instead of trusting the score."""

    quote: str
    source: str
    markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class BindingProposal:
    """One field -> dimension proposal (PURE data; persistence is a separate step)."""

    connector: str
    report_id: str
    source_field: str
    canonical_dimension: str | None
    status: str
    confidence: float
    evidence: BindingEvidence
    rationale: str
    pending_reason: str | None = None
    excluded_reason: str | None = None


def is_language_candidate(field: dict) -> bool:
    """True when a catalog field is a candidate for the family (PURE, name-based)."""
    name = f"{field.get('source_field') or ''} {field.get('field_id') or ''}".lower()
    return _LANGUAGE_TOKEN in name


def _matched(text: str, markers) -> tuple[str, ...]:
    return tuple(m for m in markers if m in text)


def _strip_field_name(description: str, *names) -> str:
    """Remove echoes of the field name from the description (PURE).

    A description that merely repeats the column name is not independent evidence -- a
    catalog line quoting 'creativeLanguage' must not be scored as if the provider had
    explained the field. Stripping the echo first keeps the confidence ladder honest."""
    text = description or ""
    for name in names:
        if not name:
            continue
        text = re.sub(re.escape(str(name)), " ", text, flags=re.IGNORECASE)
    return text


def propose_binding(
    field: dict, *, connector: str, report_id: str = "unknown"
) -> BindingProposal:
    """Classify ONE catalog field into the family, with cited evidence (PURE).

    Decision order, safest first -- and EVERY outcome is a proposal or an honest pending,
    never a decision applied on the client's behalf:
      1. the field is a METRIC -> excluded (homonym, e.g. a brand-safety count);
      2. the field names an ITEM/CATALOG property -> excluded (not a delivery dimension);
      3. the DESCRIPTION carries exactly one nature marker -> proposed (0.9);
         several conflicting natures -> pending;
      4. otherwise the FIELD NAME carries exactly one nature marker -> proposed (0.75);
      5. otherwise an ambiguous/configuration marker -> pending WITH a proposition (0.4);
      6. otherwise -> pending, no proposition at all (0.0).
    """
    source_field = str(field.get("source_field") or field.get("field_id") or "")
    field_id = str(field.get("field_id") or "")
    description = str(field.get("description") or "")
    kind = str(field.get("kind") or "").lower()
    tokens = _name_tokens(source_field, field_id)
    name_hits = {
        dimension: _matched_tokens(tokens, markers)
        for dimension, markers in _NAME_MARKERS.items()
    }
    name_hits = {d: m for d, m in name_hits.items() if m}

    def _proposal(**kwargs) -> BindingProposal:
        base = dict(
            connector=connector,
            report_id=report_id or "unknown",
            source_field=source_field,
            canonical_dimension=None,
            status=STATUS_PENDING,
            confidence=CONFIDENCE_NONE,
            evidence=BindingEvidence(quote=description, source=EVIDENCE_DESCRIPTION),
            rationale="",
        )
        base.update(kwargs)
        return BindingProposal(**base)

    # 1. A metric that merely SHARES THE WORD is not a dimension of the family.
    if kind == "metric":
        return _proposal(
            status=STATUS_EXCLUDED,
            excluded_reason=EXCLUDED_METRIC_HOMONYM,
            evidence=BindingEvidence(
                quote=description, source=EVIDENCE_DESCRIPTION, markers=("kind=metric",)
            ),
            rationale=(
                "declared as a metric in the catalog: a homonym (it MEASURES something), "
                "not a member of the language dimension family"
            ),
        )

    # 2. A property of a catalogue item is not a property of a delivery. Checked only when
    # the name does NOT already carry a delivery-side nature, so a field naming a delivery
    # entity is never mistaken for a catalogue attribute.
    item_markers = _matched_tokens(tokens, _ITEM_MARKERS)
    if item_markers and not name_hits:
        return _proposal(
            status=STATUS_EXCLUDED,
            excluded_reason=EXCLUDED_ITEM_ATTRIBUTE,
            evidence=BindingEvidence(
                quote=description, source=EVIDENCE_FIELD_NAME, markers=item_markers
            ),
            rationale=(
                "names an item/catalogue property: an attribute of a product, outside the "
                "delivery-side language family"
            ),
        )

    # 3. The provider's own description, with the echo of the field name removed.
    independent = _strip_field_name(description, source_field, field_id).lower()
    desc_hits = {
        dimension: _matched(independent, markers)
        for dimension, markers in _DESCRIPTION_MARKERS.items()
    }
    desc_hits = {d: m for d, m in desc_hits.items() if m}
    if len(desc_hits) == 1:
        dimension, markers = next(iter(desc_hits.items()))
        return _proposal(
            canonical_dimension=dimension,
            status=STATUS_PROPOSED,
            confidence=CONFIDENCE_DESCRIPTION_MARKER,
            evidence=BindingEvidence(
                quote=description, source=EVIDENCE_DESCRIPTION, markers=markers
            ),
            rationale=(
                f"the provider's description states the nature of the field ({', '.join(markers)})"
                f" -> {dimension}"
            ),
        )
    if len(desc_hits) > 1:
        return _proposal(
            status=STATUS_PENDING,
            pending_reason=PENDING_CONFLICTING_EVIDENCE,
            confidence=CONFIDENCE_NONE,
            evidence=BindingEvidence(
                quote=description,
                source=EVIDENCE_DESCRIPTION,
                markers=tuple(sorted(m for ms in desc_hits.values() for m in ms)),
            ),
            rationale=(
                "the description points at several natures at once "
                f"({', '.join(sorted(desc_hits))}): a human must say which one"
            ),
        )

    # 4. The field name, weaker evidence (computed above so step 2 can consult it).
    if len(name_hits) == 1:
        dimension, markers = next(iter(name_hits.items()))
        return _proposal(
            canonical_dimension=dimension,
            status=STATUS_PROPOSED,
            confidence=CONFIDENCE_NAME_MARKER,
            evidence=BindingEvidence(
                quote=description or source_field,
                source=EVIDENCE_FIELD_NAME,
                markers=markers,
            ),
            rationale=(
                f"the field name carries the nature ({', '.join(markers)}) -> {dimension}; "
                "the description adds nothing, so the confidence stays below a described field"
            ),
        )
    if len(name_hits) > 1:
        return _proposal(
            status=STATUS_PENDING,
            pending_reason=PENDING_CONFLICTING_EVIDENCE,
            evidence=BindingEvidence(
                quote=description or source_field,
                source=EVIDENCE_FIELD_NAME,
                markers=tuple(sorted(m for ms in name_hits.values() for m in ms)),
            ),
            rationale=(
                "the field name carries several natures at once: a human must say which one"
            ),
        )

    # 5. Ambiguous or configuration wording: a PROPOSITION, and it stays pending.
    configured = _matched(independent, _CONFIGURED_ATTRIBUTE_MARKERS)
    if configured:
        return _proposal(
            canonical_dimension=TARGETING_LANGUAGE,
            status=STATUS_PENDING,
            pending_reason=PENDING_CONFIGURED_ATTRIBUTE,
            confidence=CONFIDENCE_PENDING_PROPOSITION,
            evidence=BindingEvidence(
                quote=description, source=EVIDENCE_DESCRIPTION, markers=configured
            ),
            rationale=(
                "described as a configuration attribute used to group rows: that is a "
                "SETTING, not an observation, so the proposition is an intent -- but the "
                "description never says it, so it stays pending"
            ),
        )
    ambiguous = _matched(independent, _AMBIGUOUS_AUDIENCE_MARKERS)
    if ambiguous:
        return _proposal(
            canonical_dimension=AUDIENCE_LANGUAGE,
            status=STATUS_PENDING,
            pending_reason=PENDING_AMBIGUOUS_AUDIENCE_TERM,
            confidence=CONFIDENCE_PENDING_PROPOSITION,
            evidence=BindingEvidence(
                quote=description, source=EVIDENCE_DESCRIPTION, markers=ambiguous
            ),
            rationale=(
                "the description leans on a word that designates, depending on the context, "
                "the person reached OR the segment targeted; the surface reading is proposed "
                "but the evidence does not settle it -- pending"
            ),
        )

    # 6. Nothing to go on. Say so; do not guess.
    return _proposal(
        status=STATUS_PENDING,
        pending_reason=PENDING_NO_EVIDENCE,
        confidence=CONFIDENCE_NONE,
        evidence=BindingEvidence(
            quote=description, source=EVIDENCE_NONE if not description else EVIDENCE_DESCRIPTION
        ),
        rationale=(
            "no disambiguating evidence in the catalog: nothing says whether it is an "
            "observation, a property of the asset or a declared intent"
        ),
    )


def propose_bindings_from_catalog(
    catalog: dict, *, connector: str | None = None, report_id_key: str = "section"
) -> list[BindingProposal]:
    """Classify every language candidate of ONE catalog read as DATA (PURE).

    The catalog is a dict already loaded from disk (this module never hard-codes a path nor
    a provider). ``connector`` defaults to the catalog's own declaration. ``report_id_key``
    says which catalog key carries the report grain 27.9 keys its lineage on."""
    conn = connector or str(catalog.get("connector") or "unknown")
    proposals: list[BindingProposal] = []
    for field in catalog.get("fields") or ():
        if not isinstance(field, dict) or not is_language_candidate(field):
            continue
        proposals.append(
            propose_binding(
                field,
                connector=conn,
                report_id=str(field.get(report_id_key) or "unknown"),
            )
        )
    proposals.sort(key=lambda p: (p.connector, p.report_id, p.source_field))
    return proposals


# ===========================================================================
# F. PERSISTENCE -- app.dimension_field_bindings (migration 105), audited like 27.1/27.4.
#
# The SCHEMA-level counterpart of app.dimension_value_mappings: the value level knows
# (dimension, connector, source_value); this level knows (connector, report, source FIELD)
# and carries the confidence and the evidence. 27.9's inverse lineage keys its rows on the
# same grain (connector, report_id, source_field).
# ===========================================================================

ENTITY_TYPE_BINDING = "dimension_field_binding"
_BINDING_ID_PREFIX = "dfb_"

ACTION_CREATED = "created"
ACTION_UPSERTED = "upserted"
ACTION_CONFIRMED = "confirmed"
ACTION_REJECTED = "rejected"

_BINDING_COLUMNS = (
    "id, connector, report_id, source_field, canonical_dimension, status, confidence, "
    "evidence_quote, evidence_source, evidence_markers, rationale, pending_reason, "
    "excluded_reason, scope_level, org_id, project_id, created_by, reviewed_by, "
    "reviewed_at, created_at, updated_at"
)
_TS_COLS = {"created_at", "updated_at", "reviewed_at"}
_VOLATILE = frozenset({"id", "created_at", "updated_at"})

# Only these statuses are ever WRITTEN by the automatic path. 'confirmed'/'rejected' are
# reachable only through the human functions below (the invariant, in one line).
_AUTOMATIC_STATUSES = frozenset({STATUS_PROPOSED, STATUS_PENDING, STATUS_EXCLUDED})


class BindingNotFound(Exception):
    """A curation call targeted a binding id that does not exist."""


class BindingNotConfirmable(Exception):
    """A confirm was attempted on something the evidence never settled."""


def _row_to_binding(cols, row) -> dict:
    record: dict = {}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _TS_COLS and val is not None else val
    return record


def _binding_changed(before: dict | None, after: dict) -> bool:
    if before is None:
        return True
    for key, value in after.items():
        if key in _VOLATILE:
            continue
        if before.get(key) != value:
            return True
    return False


def _select_binding_by_id(conn, binding_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_BINDING_COLUMNS} FROM app.dimension_field_bindings WHERE id = %s",
            (binding_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_binding(cols, row)


def _select_binding(
    conn, *, scope_level, org_id, project_id, connector, report_id, source_field
) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_BINDING_COLUMNS}
            FROM app.dimension_field_bindings
            WHERE scope_level = %s
              AND COALESCE(org_id, '') = COALESCE(%s, '')
              AND COALESCE(project_id, '') = COALESCE(%s, '')
              AND connector = %s
              AND report_id = %s
              AND source_field = %s
            """,
            (scope_level, org_id, project_id, connector, report_id, source_field),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_binding(cols, row)


def persist_binding_proposals(
    proposals,
    *,
    scope_level: str = SCOPE_PLATFORM,
    org_id: str | None = None,
    project_id: str | None = None,
    identity: str = "system",
) -> dict:
    """UPSERT proposals, carrying the confidence AND the evidence. Returns counts.

    NEVER overwrites a row a human already decided ('confirmed'/'rejected') -> 'skipped'.
    Idempotent: a replay with identical content writes no audit ('unchanged'). The
    automatic path can only ever write proposed/pending/excluded (assertion below); a
    'confirmed' binding exists only because someone confirmed it."""
    from core.db import get_connection  # noqa: PLC0415

    validate_scope(scope_level, org_id, project_id)
    # PURE guard, BEFORE any connection: the automatic path may not smuggle a decision in.
    for proposal in proposals or ():
        if proposal.status not in _AUTOMATIC_STATUSES:
            raise ValueError(
                "the automatic path may only write proposed/pending/excluded bindings; "
                f"got {proposal.status!r} -- confirmation is a human act"
            )
    counts = {"written": 0, "skipped": 0, "unchanged": 0}

    with get_connection() as conn:
        for proposal in proposals or ():
            before = _select_binding(
                conn,
                scope_level=scope_level,
                org_id=org_id,
                project_id=project_id,
                connector=proposal.connector,
                report_id=proposal.report_id,
                source_field=proposal.source_field,
            )
            if before is not None and before.get("status") in (
                STATUS_CONFIRMED,
                STATUS_REJECTED,
            ):
                counts["skipped"] += 1
                continue

            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO app.dimension_field_bindings
                        (id, connector, report_id, source_field, canonical_dimension, status,
                         confidence, evidence_quote, evidence_source, evidence_markers,
                         rationale, pending_reason, excluded_reason, scope_level, org_id,
                         project_id, created_by, reviewed_by, reviewed_at, created_at,
                         updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, NULL, NULL, now(), now())
                    ON CONFLICT (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''),
                                 connector, report_id, source_field)
                    DO UPDATE SET
                        canonical_dimension = EXCLUDED.canonical_dimension,
                        status              = EXCLUDED.status,
                        confidence          = EXCLUDED.confidence,
                        evidence_quote      = EXCLUDED.evidence_quote,
                        evidence_source     = EXCLUDED.evidence_source,
                        evidence_markers    = EXCLUDED.evidence_markers,
                        rationale           = EXCLUDED.rationale,
                        pending_reason      = EXCLUDED.pending_reason,
                        excluded_reason     = EXCLUDED.excluded_reason,
                        updated_at          = now()
                    RETURNING {_BINDING_COLUMNS}
                    """,
                    (
                        _mint_id(_BINDING_ID_PREFIX),
                        proposal.connector,
                        proposal.report_id,
                        proposal.source_field,
                        proposal.canonical_dimension,
                        proposal.status,
                        proposal.confidence,
                        proposal.evidence.quote,
                        proposal.evidence.source,
                        "|".join(proposal.evidence.markers),
                        proposal.rationale,
                        proposal.pending_reason,
                        proposal.excluded_reason,
                        scope_level,
                        org_id,
                        project_id,
                        identity,
                    ),
                )
                row = cur.fetchone()
                cols = [desc[0] for desc in cur.description]
            after = _row_to_binding(cols, row)

            if not _binding_changed(before, after):
                counts["unchanged"] += 1
                continue
            _write_semantics_audit(
                conn,
                identity=identity,
                action=ACTION_CREATED if before is None else ACTION_UPSERTED,
                entity_type=ENTITY_TYPE_BINDING,
                entity_id=after["id"],
                scope_level=scope_level,
                org_id=org_id,
                project_id=project_id,
                before=before,
                after=after,
            )
            counts["written"] += 1
        conn.commit()

    logger.info(
        "language_dimensions: persisted bindings written=%d skipped=%d unchanged=%d",
        counts["written"],
        counts["skipped"],
        counts["unchanged"],
    )
    return counts


def confirm_binding(
    binding_id: str, *, identity: str, canonical_dimension: str | None = None
) -> dict:
    """Confirm a field -> dimension binding (the ONLY way a binding becomes resolvable).

    A PENDING row can be confirmed only when the human SUPPLIES the dimension explicitly:
    the stored proposition of a pending row is a reading, not a decision, and confirming it
    blindly would be exactly the 'decided on the client's behalf' this story refuses. An
    EXCLUDED row is not confirmable at all."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        before = _select_binding_by_id(conn, binding_id)
        if before is None:
            raise BindingNotFound(f"dimension_field_binding not found: {binding_id!r}")
        if before.get("status") == STATUS_EXCLUDED:
            raise BindingNotConfirmable(
                f"binding {binding_id!r} is excluded from the dimension family "
                f"({before.get('excluded_reason')}) -- it cannot be confirmed"
            )
        if before.get("status") == STATUS_PENDING and canonical_dimension is None:
            raise BindingNotConfirmable(
                f"binding {binding_id!r} is pending ({before.get('pending_reason')}): the "
                "evidence does not settle it, so a confirmation MUST carry the dimension "
                "the human decides on"
            )
        target = canonical_dimension or before.get("canonical_dimension")
        if not target:
            raise BindingNotConfirmable(
                f"binding {binding_id!r} carries no dimension to confirm"
            )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE app.dimension_field_bindings
                SET status = %s,
                    canonical_dimension = %s,
                    pending_reason = NULL,
                    reviewed_by = %s,
                    reviewed_at = now(),
                    updated_at = now()
                WHERE id = %s
                RETURNING {_BINDING_COLUMNS}
                """,
                (STATUS_CONFIRMED, target, identity, binding_id),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_binding(cols, row)
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_CONFIRMED,
            entity_type=ENTITY_TYPE_BINDING,
            entity_id=after["id"],
            scope_level=after["scope_level"],
            org_id=after["org_id"],
            project_id=after["project_id"],
            before=before,
            after=after,
        )
        conn.commit()
    return after


def reject_binding(binding_id: str, *, identity: str) -> dict:
    """Record a human refusal of a proposed/pending binding (a kept false friend)."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        before = _select_binding_by_id(conn, binding_id)
        if before is None:
            raise BindingNotFound(f"dimension_field_binding not found: {binding_id!r}")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE app.dimension_field_bindings
                SET status = %s, reviewed_by = %s, reviewed_at = now(), updated_at = now()
                WHERE id = %s
                RETURNING {_BINDING_COLUMNS}
                """,
                (STATUS_REJECTED, identity, binding_id),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_binding(cols, row)
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_REJECTED,
            entity_type=ENTITY_TYPE_BINDING,
            entity_id=after["id"],
            scope_level=after["scope_level"],
            org_id=after["org_id"],
            project_id=after["project_id"],
            before=before,
            after=after,
        )
        conn.commit()
    return after


def list_bindings(
    *,
    scope_level: str = SCOPE_PLATFORM,
    org_id: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List binding rows stored AT one exact scope (curation surface)."""
    from core.db import get_connection  # noqa: PLC0415

    sql = f"""
        SELECT {_BINDING_COLUMNS}
        FROM app.dimension_field_bindings
        WHERE scope_level = %s
          AND COALESCE(org_id, '') = COALESCE(%s, '')
          AND COALESCE(project_id, '') = COALESCE(%s, '')
    """
    params: list = [scope_level, org_id, project_id]
    if status is not None:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY connector, report_id, source_field"

    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(_row_to_binding(cols, row))
    return rows


def reduce_bindings_by_specificity(rows) -> dict:
    """Reduce binding rows to {(connector, report_id, source_field) -> row} (PURE).

    CONFIRMED rows only -- the exact mirror of ``conform_value``'s contract at the value
    level: a proposed or pending binding NEVER resolves. Cascade PROJECT > ORG > PLATFORM."""
    rank = {SCOPE_PLATFORM: 0, SCOPE_ORG: 1, SCOPE_PROJECT: 2}
    winner: dict[tuple[str, str, str], dict] = {}
    for row in rows or ():
        if row.get("status") != STATUS_CONFIRMED:
            continue
        key = (row.get("connector"), row.get("report_id"), row.get("source_field"))
        if None in key:
            continue
        current = winner.get(key)
        if current is None or rank.get(row.get("scope_level"), -1) > rank.get(
            current.get("scope_level"), -1
        ):
            winner[key] = row
    return winner


def resolve_field_bindings(
    *, org_id: str | None = None, project_id: str | None = None
) -> dict:
    """Return {(connector, report_id, source_field) -> canonical_dimension}, CONFIRMED only.

    S-4 (same trust note as ``dimension_conformance.conform_value``): org_id/project_id are
    assumed ALREADY AUTHORIZED; the org guard belongs to the calling surface."""
    from core.db import get_connection  # noqa: PLC0415

    clauses = ["scope_level = 'PLATFORM'"]
    params: list = []
    if org_id is not None:
        clauses.append("(scope_level = 'ORG' AND org_id = %s)")
        params.append(org_id)
    if project_id is not None:
        clauses.append("(scope_level = 'PROJECT' AND project_id = %s)")
        params.append(project_id)
    sql = f"""
        SELECT {_BINDING_COLUMNS}
        FROM app.dimension_field_bindings
        WHERE status = 'confirmed' AND ({" OR ".join(clauses)})
    """
    rows: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                for row in cur.fetchall():
                    rows.append(_row_to_binding(cols, row))
    except Exception as exc:  # noqa: BLE001 -- fail-soft, like the 27.4 resolvers
        logger.warning("language_dimensions: binding resolution failed: %s", exc)
        return {}
    return {
        key: row.get("canonical_dimension")
        for key, row in reduce_bindings_by_specificity(rows).items()
    }


# Re-exported so a surface never has to import two modules to validate a scope.
__all__ += ["InvalidScope", "SCOPE_PLATFORM", "SCOPE_ORG", "SCOPE_PROJECT"]
