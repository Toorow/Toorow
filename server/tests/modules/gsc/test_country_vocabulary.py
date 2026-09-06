"""GSC emits ISO 3166-1 alpha-3 country codes — they must resolve (Story 37.7).

``api_catalog.json`` declares the ``country`` dimension as "a 3-letter ISO 3166-1
alpha-3 country code (e.g. 'fra', 'gbr')" and ``connector.py`` passes the API key
through untouched (``r.get("country", "")``).  ``manifest.json`` maps it onto the
canonical ``country`` dimension.

Two readers must therefore accept those spellings:

* the Python vocabulary (``core.country_vocabulary.normalize_country_value``),
  which is case-insensitive;
* the dbt path, where ``normalize_dimension`` compares aliases with
  ``list_contains`` — an exact, case-SENSITIVE membership test — after the
  staging model has upper-cased the provider value.

Before Story 37.7's repair the seed carried no alpha-3 spelling at all, so every
GSC country landed in ``Unknown`` while the conformance suite stayed green.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[4]
_GSC_MODULE = _REPO_ROOT / "server" / "modules" / "gsc"
_SEED = _REPO_ROOT / "dbt" / "seeds" / "dim_country.csv"

#: Spellings Google Search Console really returns for these markets.
_GSC_EMITTED = {
    "fra": "FR",
    "gbr": "GB",
    "deu": "DE",
    "usa": "US",
    "esp": "ES",
    "ita": "IT",
    "bel": "BE",
    "che": "CH",
    "mco": "MC",
    "reu": "RE",
    "guf": "GF",
    "glp": "GP",
    "mtq": "MQ",
    "myt": "YT",
    "jpn": "JP",
    "bra": "BR",
    "zaf": "ZA",
    "aus": "AU",
    "civ": "CI",
    "kor": "KR",
}


def _seed_alias_set() -> set[str]:
    with _SEED.open(newline="", encoding="utf-8") as handle:
        return {
            alias.strip()
            for row in csv.DictReader(handle)
            for alias in (row.get("aliases") or "").split("|")
            if alias.strip()
        }


def test_gsc_catalog_still_declares_alpha3_country() -> None:
    """Guard the premise: if GSC stopped emitting alpha-3, this test must be revisited."""
    catalog = json.loads((_GSC_MODULE / "api_catalog.json").read_text(encoding="utf-8"))
    fields = {field["field_id"]: field for field in catalog["fields"]}
    assert "alpha-3" in fields["country"]["description"], (
        "[gsc] api_catalog no longer declares country as ISO 3166-1 alpha-3"
    )

    manifest = json.loads((_GSC_MODULE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("canonical_dimension_mapping", {}).get("country") == "country", (
        "[gsc] country is no longer mapped onto the canonical country dimension"
    )


def test_gsc_alpha3_country_resolves_in_python_vocabulary() -> None:
    """Every alpha-3 spelling GSC emits resolves to its canonical alpha-2 code."""
    from core.country_vocabulary import normalize_country_value

    failures = [
        f"{spelling!r} -> {normalize_country_value(spelling)!r} (expected {expected!r})"
        for spelling, expected in _GSC_EMITTED.items()
        if normalize_country_value(spelling) != expected
    ]
    assert not failures, (
        "[gsc] alpha-3 country spellings do not resolve — every one of them lands "
        "in Unknown:\n" + "\n".join(f"  - {f}" for f in failures)
    )


def test_gsc_alpha3_country_resolves_on_the_sql_path() -> None:
    """The dbt path compares aliases case-sensitively, on the upper-cased value."""
    aliases = _seed_alias_set()
    missing = sorted(
        spelling.upper() for spelling in _GSC_EMITTED if spelling.upper() not in aliases
    )
    assert not missing, (
        "[gsc] dim_country.csv carries no alpha-3 alias for: "
        f"{missing}\n  list_contains() is exact — a missing spelling is a NULL country."
    )


def test_gsc_staging_normalizes_country_through_the_shared_macro() -> None:
    """stg_gsc_daily must route country through normalize_dimension like every other model."""
    sql = (_GSC_MODULE / "dbt" / "staging" / "stg_gsc_daily.sql").read_text(encoding="utf-8")

    assert "normalize_dimension" in sql, (
        "[gsc] stg_gsc_daily selects country raw — the provider alpha-3 code reaches "
        "the marts unnormalized and never joins dim_country."
    )
    assert "dim_country" in sql, "[gsc] stg_gsc_daily does not reference the dim_country seed"
    assert "country_source" in sql, (
        "[gsc] stg_gsc_daily must preserve the raw provider value as country_source (AD-6)"
    )


@pytest.mark.parametrize("value", ["", "   ", "xxx", "zzz", "Atlantis", "(not set)"])
def test_gsc_country_normalization_still_refuses_to_guess(value: str) -> None:
    """Adding alpha-3 must not turn an unknown three-letter token into a country."""
    from core.country_vocabulary import normalize_country_value

    assert normalize_country_value(value) is None
