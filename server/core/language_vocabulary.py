"""toorow -- Canonical language vocabulary backed by the governed dbt seed (Story 27.8).

The strict counterpart of ``core.country_vocabulary``: the seed ``dbt/seeds/dim_language.csv``
is the ONE governed registry of BCP 47 PRIMARY LANGUAGE SUBTAGS ('fr', 'en', 'nl', ...),
each with a display name and the pipe-delimited alias list that lets a per-source adapter
recognise the many ENCODINGS a provider may use for the SAME subtag ('French', 'FRENCH',
'fra', 'fre', 'FR').

WHY A SUBTAG REGISTRY AND NOT A TAG REGISTRY
--------------------------------------------
A BCP 47 tag is COMPOSED ('fr' + '-' + 'FR'), it is not enumerated. The region part is
governed by the country registry that already exists (``core.country_vocabulary``, seed
``dim_country.csv``) -- so this seed governs the LANGUAGE half only and the composition is
validated at runtime by ``core.language_dimensions``. Enumerating every language x region
pair here would fork the country registry and rot.

AD-2: no provider vocabulary in this module NOR in the seed -- language subtags and their
human encodings only. Nothing here knows what a connector is.

Windows/CI note: all messages use ASCII-safe characters only.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# BCP 47 primary language subtag: 2 or 3 ASCII letters, canonically LOWERCASE.
_SUBTAG_RE = re.compile(r"^[a-z]{2,3}$")
_REPO_ROOT = Path(__file__).parents[2]

_REQUIRED_HEADERS = {"canonical_value", "display_name", "aliases"}


class LanguageVocabularyError(RuntimeError):
    """Raised when the canonical language registry cannot be trusted."""


@dataclass(frozen=True, slots=True)
class Language:
    """One governed BCP 47 primary language subtag."""

    subtag: str
    display_name: str
    aliases: tuple[str, ...] = ()


def default_language_seed_path() -> Path:
    seed_dir = Path(os.environ.get("TOOROW_DBT_SEEDS_DIR", str(_REPO_ROOT / "dbt" / "seeds")))
    return seed_dir / "dim_language.csv"


def load_language_vocabulary(path: Path | None = None) -> tuple[Language, ...]:
    """Load + validate the seed. Raises LanguageVocabularyError on anything untrustworthy.

    Validation is deliberately strict (same discipline as the country registry): a
    malformed subtag, a missing display name, a duplicate subtag or an alias claimed by two
    subtags all raise -- an ambiguous alias would silently fuse two languages, which is
    exactly the kind of silent fusion AD-9 forbids.
    """
    seed_path = path or default_language_seed_path()
    try:
        with seed_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not _REQUIRED_HEADERS <= set(reader.fieldnames):
                raise LanguageVocabularyError(
                    "language vocabulary requires canonical_value, display_name and "
                    "aliases headers"
                )
            records: list[Language] = []
            seen: set[str] = set()
            seen_aliases: dict[str, str] = {}
            for row in reader:
                subtag = (row.get("canonical_value") or "").strip().lower()
                name = (row.get("display_name") or "").strip()
                if not _SUBTAG_RE.fullmatch(subtag):
                    raise LanguageVocabularyError(
                        f"invalid BCP 47 primary language subtag in vocabulary: {subtag!r}"
                    )
                if not name:
                    raise LanguageVocabularyError(
                        f"missing display name for language vocabulary subtag {subtag}"
                    )
                if subtag in seen:
                    raise LanguageVocabularyError(
                        f"duplicate language vocabulary subtag: {subtag}"
                    )
                aliases = tuple(
                    alias.strip()
                    for alias in (row.get("aliases") or "").split("|")
                    if alias.strip()
                )
                if not aliases:
                    raise LanguageVocabularyError(
                        f"missing aliases for language vocabulary subtag {subtag}"
                    )
                for alias in (*aliases, name):
                    key = alias.casefold()
                    owner = seen_aliases.get(key)
                    if owner is not None and owner != subtag:
                        raise LanguageVocabularyError(
                            f"language alias {alias!r} maps to both {owner} and {subtag}"
                        )
                    seen_aliases[key] = subtag
                seen.add(subtag)
                records.append(Language(subtag=subtag, display_name=name, aliases=aliases))
    except LanguageVocabularyError:
        raise
    except (OSError, csv.Error) as exc:
        raise LanguageVocabularyError("language vocabulary is unavailable") from exc

    if not records:
        raise LanguageVocabularyError("language vocabulary is empty")
    return tuple(sorted(records, key=lambda item: item.subtag))


@lru_cache(maxsize=1)
def get_language_vocabulary() -> tuple[Language, ...]:
    return load_language_vocabulary()


def get_supported_language_subtags() -> frozenset[str]:
    return frozenset(item.subtag for item in get_language_vocabulary())


@lru_cache(maxsize=1)
def get_language_alias_map() -> dict[str, str]:
    """Return a case-insensitive governed alias-to-subtag lookup."""
    result: dict[str, str] = {}
    for language in get_language_vocabulary():
        result[language.subtag.casefold()] = language.subtag
        result[language.display_name.casefold()] = language.subtag
        for alias in language.aliases:
            result[alias.casefold()] = language.subtag
    return result


def normalize_language_subtag(raw_value: object) -> str | None:
    """Resolve ONE source token to a canonical primary subtag, or None when unmapped.

    Honest by construction: an unknown token returns None (never a guess), so the caller
    can report the gap instead of inventing a language.
    """
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip()
    if not value:
        return None
    return get_language_alias_map().get(value.casefold())


def get_language_display_name(subtag: str) -> str | None:
    """Return the governed display name of a subtag (None when unknown)."""
    target = (subtag or "").strip().lower()
    for language in get_language_vocabulary():
        if language.subtag == target:
            return language.display_name
    return None
