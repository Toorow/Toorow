from __future__ import annotations

from pathlib import Path

import pytest


def test_country_vocabulary_loads_sorted_canonical_records(tmp_path: Path) -> None:
    from core.country_vocabulary import load_country_vocabulary

    seed = tmp_path / "dim_country.csv"
    seed.write_text(
        "iso_code,display_name,aliases\nfr,France,France|FR\nDE,Germany,Germany|DE\n",
        encoding="utf-8",
    )

    vocabulary = load_country_vocabulary(seed)

    assert [item.code for item in vocabulary] == ["DE", "FR"]
    assert vocabulary[1].display_name == "France"


def test_country_vocabulary_rejects_duplicate_codes(tmp_path: Path) -> None:
    from core.country_vocabulary import CountryVocabularyError, load_country_vocabulary

    seed = tmp_path / "dim_country.csv"
    seed.write_text(
        "iso_code,display_name,aliases\nFR,France,France|FR\nfr,France duplicate,FR\n",
        encoding="utf-8",
    )

    with pytest.raises(CountryVocabularyError, match="duplicate"):
        load_country_vocabulary(seed)


@pytest.mark.parametrize(
    ("mode", "codes", "expected_mode", "expected_codes"),
    [
        ("global", ["FR"], "global", ()),
        ("local_markets", ["de", "FR"], "local_markets", ("DE", "FR")),
    ],
)
def test_normalize_geographic_posture(
    mode: str,
    codes: list[str],
    expected_mode: str,
    expected_codes: tuple[str, ...],
) -> None:
    from core.geographic_reporting import normalize_geographic_posture

    posture = normalize_geographic_posture(mode, codes, {"DE", "FR"})

    assert posture.mode == expected_mode
    assert posture.country_codes == expected_codes


@pytest.mark.parametrize(
    ("mode", "codes", "message"),
    [
        ("regional", ["FR"], "geographic_mode"),
        ("local_markets", [], "at least one"),
        ("local_markets", ["FRA"], "alpha-2"),
        ("local_markets", ["fr", "FR"], "duplicate"),
        ("local_markets", ["US"], "unsupported"),
    ],
)
def test_normalize_geographic_posture_rejects_invalid_state(
    mode: str, codes: list[str], message: str
) -> None:
    from core.geographic_reporting import InvalidGeographicPosture, normalize_geographic_posture

    with pytest.raises(InvalidGeographicPosture, match=message):
        normalize_geographic_posture(mode, codes, {"DE", "FR"})


def test_merge_geographic_patch_validates_final_state_and_global_clears() -> None:
    from core.geographic_reporting import GeographicPosture, merge_geographic_patch

    current = GeographicPosture(mode="local_markets", country_codes=("DE", "FR"))

    cleared = merge_geographic_patch(
        current,
        {"geographic_mode": "global"},
        {"DE", "FR"},
    )
    assert cleared == GeographicPosture(mode="global", country_codes=())

    updated = merge_geographic_patch(
        current,
        {"local_market_country_codes": ["fr"]},
        {"DE", "FR"},
    )
    assert updated == GeographicPosture(mode="local_markets", country_codes=("FR",))
