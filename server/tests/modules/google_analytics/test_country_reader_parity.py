"""One reader for dim_country.csv, not two (Story 37.7 repair).

The GA4 connector used to carry its own CSV parser for dbt/seeds/dim_country.csv.
Same file, two behaviours: it matched aliases case-SENSITIVELY, returned an empty
map instead of raising when the seed was missing, and ignored TOOROW_DBT_SEEDS_DIR.
Each divergence turned a country into a pass-through provider spelling that no
downstream join resolves.

These tests pin the parity: transform() must answer exactly what
core.country_vocabulary answers.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[4]
_CONNECTOR_PATH = _REPO_ROOT / "server" / "modules" / "google-analytics" / "connector.py"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_ga4_country", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


def _country_of(connector_module, raw: str) -> str:
    rows = connector_module.transform([{"country": raw, "sessions": 1}])
    return rows[0]["country"]


def test_ga4_connector_keeps_no_private_seed_parser(connector) -> None:
    """The module-local CSV parser and its eager alias map must be gone."""
    leftovers = [
        name
        for name in ("_load_country_alias_map", "_COUNTRY_ALIAS_MAP")
        if hasattr(connector, name)
    ]
    assert not leftovers, (
        f"[google-analytics] a second reader of dim_country.csv is back: {leftovers}"
    )


@pytest.mark.parametrize(
    "raw",
    ["France", "FRANCE", "france", "FR", "fr", "fra", "FRA"],
)
def test_ga4_country_normalization_is_case_insensitive(connector, raw: str) -> None:
    """core.country_vocabulary case-folds; the connector must too."""
    from core.country_vocabulary import normalize_country_value

    assert normalize_country_value(raw) == "FR"
    assert _country_of(connector, raw) == "FR", (
        f"[google-analytics] {raw!r} normalized by the shared vocabulary but not by transform()"
    )


def test_ga4_country_normalization_matches_the_shared_vocabulary(connector) -> None:
    """Every seed spelling resolves identically on both sides."""
    from core.country_vocabulary import load_country_vocabulary, normalize_country_value

    vocabulary = load_country_vocabulary(_REPO_ROOT / "dbt" / "seeds" / "dim_country.csv")
    failures: list[str] = []
    for country in vocabulary:
        for candidate in (
            country.code,
            country.code.lower(),
            country.display_name,
            country.display_name.upper(),
            *country.aliases,
        ):
            expected = normalize_country_value(candidate)
            actual = _country_of(connector, candidate)
            if expected != actual:
                failures.append(f"{candidate!r}: vocabulary {expected!r} vs transform {actual!r}")
    assert not failures, "[google-analytics] reader divergence:\n" + "\n".join(
        f"  - {f}" for f in failures[:20]
    )


def test_ga4_country_unknown_value_passes_through_unchanged(connector) -> None:
    """The vocabulary refuses to guess; transform() keeps the provider spelling.

    dbt's not_null test on the staging model stays the hard gate — the connector
    never invents a country.
    """
    from core.country_vocabulary import normalize_country_value

    for value in ["(not set)", "Atlantis", "Europe", "ZZ"]:
        assert normalize_country_value(value) is None
        assert _country_of(connector, value) == value


def test_ga4_country_honours_the_seed_directory_override(connector, tmp_path, monkeypatch) -> None:
    """TOOROW_DBT_SEEDS_DIR relocates the seed for BOTH readers, or neither."""
    import core.country_vocabulary as vocab

    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    (seed_dir / "dim_country.csv").write_text(
        "iso_code,display_name,aliases\nFR,France,FR|FRA|FRANCE|France\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TOOROW_DBT_SEEDS_DIR", str(seed_dir))
    vocab.get_country_vocabulary.cache_clear()
    vocab.get_country_alias_map.cache_clear()
    try:
        assert vocab.normalize_country_value("fra") == "FR"
        assert _country_of(connector, "fra") == "FR"
        # Germany is absent from THIS seed: both readers must refuse it.
        assert vocab.normalize_country_value("Germany") is None
        assert _country_of(connector, "Germany") == "Germany"
    finally:
        vocab.get_country_vocabulary.cache_clear()
        vocab.get_country_alias_map.cache_clear()


def test_ga4_country_fails_closed_when_the_seed_is_missing(
    connector, tmp_path, monkeypatch
) -> None:
    """A missing vocabulary is an outage, not a silent pass-through of every country."""
    import core.country_vocabulary as vocab

    monkeypatch.setenv("TOOROW_DBT_SEEDS_DIR", str(tmp_path / "does-not-exist"))
    vocab.get_country_vocabulary.cache_clear()
    vocab.get_country_alias_map.cache_clear()
    try:
        with pytest.raises(vocab.CountryVocabularyError):
            _country_of(connector, "France")
    finally:
        vocab.get_country_vocabulary.cache_clear()
        vocab.get_country_alias_map.cache_clear()
