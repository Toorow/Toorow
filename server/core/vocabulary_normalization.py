"""ONE normalization over the four international vocabularies.

WHY THIS EXISTS. Country, language, currency and reporting timezone all start
from the SAME place: a predefined international standard, identical for every
client. That is deliberate, and the reason is written in
:mod:`core.language_vocabulary` -- the governed alias list exists so a per-source
adapter can "recognise the many ENCODINGS a provider may use for the SAME
subtag". The vocabularies are fixed precisely so that matching the conventions of
external tools is easy, and easy the same way, for everybody.

They did not answer the same way. Measured 2026-08-08:

    country    normalize_country_value    alias map, casefolded, display name
    language   normalize_language_subtag  alias map, casefolded, display name
    currency   resolve_currency           alias index, upper-cased
    timezone   resolve_timezone           EXACT string, case-SENSITIVE, no aliases

So `europe/paris` did not resolve while `deutschland` did, and a caller wanting
all four had to know four verbs and four return shapes. Wiring two of them into
one screen -- which is what a file preview needs -- would have left the other two
unrecognised for the next screen, for the same reason. One seam, four registries.

WHAT THIS DOES NOT DO. It does not repair one client's spellings. That boundary
is `core.country_vocabulary`'s and it holds for all four: teaching the platform
that one client's provider writes a value by editing the shared seed would impose
that client's decision on everyone else. Per-client resolution is a conformed
dimension and lives in `core.dimension_conformance`. Grouping -- markets over
countries, zones -- sits ON TOP of the canonical code and never replaces it.

AND IT NEVER GUESSES. An unresolved value comes back with a REASON, so a caller
reports a gap instead of inventing a country.
"""

from __future__ import annotations

from dataclasses import dataclass

KIND_COUNTRY = "country"
KIND_CURRENCY = "currency"
KIND_LANGUAGE = "language"
KIND_TIMEZONE = "timezone"

#: The four axes that come out of an international standard. Ordered for stable
#: rendering; membership is what callers check.
VOCABULARY_KINDS: tuple[str, ...] = (
    KIND_COUNTRY,
    KIND_CURRENCY,
    KIND_LANGUAGE,
    KIND_TIMEZONE,
)

#: Why a value did not resolve. A closed set: a caller renders these, and a free
#: string would be a message nobody can branch on or translate.
REASON_NOT_A_STRING = "not_a_string"
REASON_EMPTY = "empty_value"
REASON_UNKNOWN_KIND = "unknown_vocabulary"
REASON_NOT_IN_VOCABULARY = "not_in_governed_vocabulary"
REASON_AMBIGUOUS_ABBREVIATION = "ambiguous_timezone_abbreviation"

#: How the value was recognised, when it was.
MATCH_CANONICAL = "canonical"
MATCH_ALIAS = "alias"
MATCH_LINK = "link"


@dataclass(frozen=True)
class Normalization:
    """What ONE source value resolved to, and how -- or why it did not.

    `canonical_value` is the international code, never the raw value: a caller
    that stores the raw value under a canonical name is how two spellings of one
    country become two countries.
    """

    kind: str
    raw_value: str
    canonical_value: str | None
    matched_on: str | None
    reason: str | None

    @property
    def resolved(self) -> bool:
        return self.canonical_value is not None


def _country(value: str) -> tuple[str | None, str | None, str | None]:
    from core.country_vocabulary import (  # noqa: PLC0415
        get_supported_country_codes,
        normalize_country_value,
    )

    code = normalize_country_value(value)
    if code is None:
        return None, None, REASON_NOT_IN_VOCABULARY
    exact = value.strip().upper() in get_supported_country_codes()
    return code, MATCH_CANONICAL if exact else MATCH_ALIAS, None


def _language(value: str) -> tuple[str | None, str | None, str | None]:
    from core.language_vocabulary import (  # noqa: PLC0415
        get_supported_language_subtags,
        normalize_language_subtag,
    )

    subtag = normalize_language_subtag(value)
    if subtag is None:
        return None, None, REASON_NOT_IN_VOCABULARY
    exact = value.strip().lower() in get_supported_language_subtags()
    return subtag, MATCH_CANONICAL if exact else MATCH_ALIAS, None


def _currency(value: str) -> tuple[str | None, str | None, str | None]:
    from core.currency_vocabulary import resolve_currency  # noqa: PLC0415

    currency = resolve_currency(value)
    if currency is None:
        return None, None, REASON_NOT_IN_VOCABULARY
    exact = value.strip().upper() == currency.code
    return currency.code, MATCH_CANONICAL if exact else MATCH_ALIAS, None


def _looks_like_a_timezone_abbreviation(value: str) -> bool:
    """A SHAPE test, never a mapping, and it only ever runs AFTER the standard.

    Measured 2026-08-08: the tz database itself ships `CET`, `EET`, `EST`, `HST`,
    `MET`, `MST` and `WET` as link zones, so those resolve above through the
    governed vocabulary -- the standard's own answer, not one invented here.

    What it does not ship is `CST` and `IST`, precisely because they are
    ambiguous: `CST` is US Central AND China Standard, `IST` is India, Ireland
    and Israel. Mapping one would pick a country on the operator's behalf and
    silently shift every day boundary in the file.

    So this recognises the SHAPE (two to five upper-case letters, no region
    separator) only to REFUSE with the right reason -- "ambiguous" and "unknown"
    call for different repairs. It maps nothing, and a list of abbreviations
    would be exactly the invention it exists to avoid.
    """
    candidate = value.strip()
    return 2 <= len(candidate) <= 5 and candidate.isalpha() and candidate.isupper()


def _timezone(value: str) -> tuple[str | None, str | None, str | None]:
    from core.timezone_vocabulary import (  # noqa: PLC0415
        load_timezone_vocabulary,
        resolve_timezone,
    )

    candidate = value.strip()
    zone = resolve_timezone(candidate)
    if zone is None:
        # Case-insensitive retry. `resolve_timezone` matches the governed string
        # exactly, so `europe/paris` -- what a CSV routinely carries -- missed a
        # zone the vocabulary holds. Casefolding recognises the SAME identifier
        # written differently; it does not admit a new one.
        folded = candidate.casefold()
        for item in load_timezone_vocabulary():
            if item.zone.casefold() == folded:
                zone = item
                break
    if zone is None:
        if _looks_like_a_timezone_abbreviation(candidate):
            return None, None, REASON_AMBIGUOUS_ABBREVIATION
        return None, None, REASON_NOT_IN_VOCABULARY
    # A backward link (`Europe/Kiev`) is a real recognition, and its canonical
    # name is what must be stored -- but the caller has to be able to say the
    # file used the old name, so the match is reported as a LINK.
    matched = MATCH_CANONICAL if zone.is_canonical else MATCH_LINK
    return zone.canonical_zone, matched, None


_RESOLVERS = {
    KIND_COUNTRY: _country,
    KIND_CURRENCY: _currency,
    KIND_LANGUAGE: _language,
    KIND_TIMEZONE: _timezone,
}


def normalize(kind: str, raw_value: object) -> Normalization:
    """Resolve ONE source value against ONE governed international vocabulary.

    Never raises on a bad value and never guesses on an unknown one: every
    outcome is a `Normalization`, and an unresolved one carries the reason a
    screen can render and a test can assert.
    """
    text = raw_value if isinstance(raw_value, str) else None
    if text is None:
        return Normalization(kind, "", None, None, REASON_NOT_A_STRING)
    if not text.strip():
        return Normalization(kind, text, None, None, REASON_EMPTY)
    resolver = _RESOLVERS.get(kind)
    if resolver is None:
        return Normalization(kind, text, None, None, REASON_UNKNOWN_KIND)
    canonical, matched, reason = resolver(text)
    return Normalization(kind, text, canonical, matched, reason)


@dataclass(frozen=True)
class ColumnProfile:
    """How ONE source column reads against ONE governed vocabulary.

    `resolved` over `total` is the whole point and it is reported RAW, with no
    threshold applied: 48/50 is a column with two dirty rows, 1/50 is a word that
    happened to be a country code, and 30/50 is a file mixing two conventions.
    Collapsing those into a boolean would decide for the operator, and picking
    the cut-off would be a policy invented here.
    """

    kind: str
    resolved: int
    total: int
    canonical_values: tuple[str, ...]
    unresolved_reasons: dict[str, int]


def profile_column(raw_values: list[object], *, sample_limit: int = 8) -> tuple[ColumnProfile, ...]:
    """Read ONE column's sample against all four vocabularies at once.

    Returns a profile for EVERY axis that recognised at least one value, never
    the single "best" one. A column of two-letter codes is genuinely readable as
    more than one axis -- `FR` is a country and `fr` is a language subtag -- and
    choosing between them from the values alone would be a guess. The screen
    shows what each axis saw; the mapping decides which one the column IS.

    WHAT IT CANNOT SEE, said here rather than discovered later: a column whose
    values resolve NOWHERE is indistinguishable from free text. A currency column
    written entirely as `€` produces no profile at all -- not because the file is
    fine, but because nothing in a governed vocabulary matched it. Detecting THAT
    needs the Template's declared semantic for the field, which this function is
    not given.
    """
    values = [value for value in raw_values if value is not None and str(value).strip()]
    profiles: list[ColumnProfile] = []
    for kind in VOCABULARY_KINDS:
        canonical: list[str] = []
        reasons: dict[str, int] = {}
        for value in values:
            outcome = normalize(kind, value)
            if outcome.canonical_value is not None:
                if outcome.canonical_value not in canonical:
                    canonical.append(outcome.canonical_value)
            elif outcome.reason:
                reasons[outcome.reason] = reasons.get(outcome.reason, 0) + 1
        resolved = len(values) - sum(reasons.values())
        if resolved <= 0:
            continue
        profiles.append(
            ColumnProfile(
                kind=kind,
                resolved=resolved,
                total=len(values),
                canonical_values=tuple(canonical[:sample_limit]),
                unresolved_reasons=dict(sorted(reasons.items())),
            )
        )
    # Strongest reading first, then by axis, so two runs of the same file render
    # in the same order.
    return tuple(sorted(profiles, key=lambda item: (-item.resolved, item.kind)))


def normalize_values(kind: str, raw_values: list[object]) -> list[Normalization]:
    """The same, over a column's sample. Order is preserved; nothing is deduped.

    Deduping here would hide HOW OFTEN a spelling appears, which is the fact that
    tells an operator whether one row is dirty or the whole file uses another
    convention.
    """
    return [normalize(kind, value) for value in raw_values]
