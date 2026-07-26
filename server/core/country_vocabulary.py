"""Canonical country vocabulary backed by the governed dbt seed (AD-4).

MDM boundary (Story 37.9).  This module owns exactly ONE thing: the **legal set
of canonical values** — the ISO 3166-1 alpha-2 codes, identical for every
client, plus the spellings the platform ships for everybody.

It deliberately does NOT own per-client spelling repair.  Teaching the platform
that one client's provider writes ``Deutschland`` by editing the shared seed
would impose that client's decision on every other client.  That resolution is a
conformed-dimension value mapping and lives in the MDM layer
(``core.dimension_conformance``), bridged by ``core.geographic_conformance``.

``normalize_country_value`` therefore stays PURE and seed-only: it answers "is
this a value the platform knows for everyone?" and returns ``None`` otherwise.
``None`` is not a dead end any more — ``geographic_conformance`` turns it into a
governed suggestion.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_CODE_RE = re.compile(r"^[A-Z]{2}$")
_REPO_ROOT = Path(__file__).parents[2]

# The conformed-dimension identifier under which country value mappings are
# governed in app.dimension_value_mappings.  It is the geography module naming
# its OWN dimension (the same string the fact carries as
# ``breakdown_dimension``); AD-2 forbids a provider vocabulary or a foreign
# dimension name inside the generic conformance core, and none is added there.
# Overridable so a deployment can rename its country partition without a code
# change.
CANONICAL_COUNTRY_DIMENSION = (
    os.environ.get("TOOROW_COUNTRY_DIMENSION", "country").strip() or "country"
)


class CountryVocabularyError(RuntimeError):
    """Raised when the canonical country registry cannot be trusted."""


@dataclass(frozen=True, slots=True)
class Country:
    code: str
    display_name: str
    aliases: tuple[str, ...] = ()


def default_country_seed_path() -> Path:
    seed_dir = Path(os.environ.get("TOOROW_DBT_SEEDS_DIR", str(_REPO_ROOT / "dbt" / "seeds")))
    return seed_dir / "dim_country.csv"


def load_country_vocabulary(path: Path | None = None) -> tuple[Country, ...]:
    seed_path = path or default_country_seed_path()
    try:
        with seed_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not {"iso_code", "display_name", "aliases"} <= set(
                reader.fieldnames
            ):
                raise CountryVocabularyError(
                    "country vocabulary requires iso_code, display_name and aliases headers"
                )
            records: list[Country] = []
            seen: set[str] = set()
            seen_aliases: dict[str, str] = {}
            for row in reader:
                code = (row.get("iso_code") or "").strip().upper()
                name = (row.get("display_name") or "").strip()
                if not _CODE_RE.fullmatch(code):
                    raise CountryVocabularyError(
                        f"invalid ISO alpha-2 code in country vocabulary: {code!r}"
                    )
                if not name:
                    raise CountryVocabularyError(
                        f"missing display name for country vocabulary code {code}"
                    )
                if code in seen:
                    raise CountryVocabularyError(f"duplicate country vocabulary code: {code}")
                aliases = tuple(
                    alias.strip()
                    for alias in (row.get("aliases") or "").split("|")
                    if alias.strip()
                )
                if not aliases:
                    raise CountryVocabularyError(
                        f"missing aliases for country vocabulary code {code}"
                    )
                for alias in aliases:
                    key = alias.casefold()
                    owner = seen_aliases.get(key)
                    if owner is not None and owner != code:
                        raise CountryVocabularyError(
                            f"country alias {alias!r} maps to both {owner} and {code}"
                        )
                    seen_aliases[key] = code
                seen.add(code)
                records.append(Country(code=code, display_name=name, aliases=aliases))
    except CountryVocabularyError:
        raise
    except (OSError, csv.Error) as exc:
        raise CountryVocabularyError("country vocabulary is unavailable") from exc

    if not records:
        raise CountryVocabularyError("country vocabulary is empty")
    return tuple(sorted(records, key=lambda item: item.code))


@lru_cache(maxsize=1)
def get_country_vocabulary() -> tuple[Country, ...]:
    return load_country_vocabulary()


def get_supported_country_codes() -> frozenset[str]:
    return frozenset(item.code for item in get_country_vocabulary())


@lru_cache(maxsize=1)
def get_country_alias_map() -> dict[str, str]:
    """Return a case-insensitive governed alias-to-ISO lookup."""

    result: dict[str, str] = {}
    for country in get_country_vocabulary():
        result[country.code.casefold()] = country.code
        result[country.display_name.casefold()] = country.code
        for alias in country.aliases:
            result[alias.casefold()] = country.code
    return result


def normalize_country_value(raw_value: object) -> str | None:
    """Resolve a source value to canonical ISO alpha-2, or None when unmapped."""

    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip()
    if not value:
        return None
    return get_country_alias_map().get(value.casefold())
