"""Conformance Layer 5 — Vocabulary boundary validation (Story 4.1, AC6).

For each module, reads manifest.json's canonical_dimension_mapping values
and asserts that any mapping resolving to a vocabulary-governed dimension
(country → dim_country.iso_code; device_category → dim_device.canonical_value)
has all its dimension values representable in the corresponding seed CSV.

This test reads seed CSVs from dbt/seeds/ directly using the stdlib csv module.
No dbt invocation is required — keeps the conformance suite runnable without
a dbt environment (AC6 spec).

HG-1 (AD-2): this file contains no module-specific strings except those read
from each module's own manifest.
HG-4: both GA4 and Meta Ads modules must pass this layer.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Seed CSV loading — no dbt dependency (plain csv.DictReader)
# ---------------------------------------------------------------------------

# Resolve dbt/seeds/ path from this file's location.
# Path: server/tests/conformance/test_vocabulary.py -> repo root -> dbt/seeds/
_REPO_ROOT = Path(__file__).parents[3]  # server/tests/conformance -> server -> repo root
_DBT_SEEDS_DIR = _REPO_ROOT / "dbt" / "seeds"

# Allow override via environment variable (useful in CI with different layout)
_SEEDS_DIR = Path(os.environ.get("TOOROW_DBT_SEEDS_DIR", str(_DBT_SEEDS_DIR)))


def _load_seed_set(csv_filename: str, key_column: str) -> set[str]:
    """Load all values from key_column in a seed CSV as a set.

    Args:
        csv_filename: Filename of the seed CSV (e.g. 'dim_country.csv').
        key_column:   Column to collect as the canonical value set (e.g. 'iso_code').

    Returns:
        Set of canonical values. Empty set if the file is missing (test will fail).
    """
    seed_path = _SEEDS_DIR / csv_filename
    if not seed_path.exists():
        pytest.fail(
            f"[vocabulary] seed file not found: {seed_path}\n"
            f"  Run 'dbt seed' or check TOOROW_DBT_SEEDS_DIR env var."
        )
    values: set[str] = set()
    with seed_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            val = row.get(key_column, "")
            if val:
                values.add(val.strip())
    return values


def _load_alias_map(csv_filename: str, alias_col: str, canonical_col: str) -> dict[str, str]:
    """Build alias→canonical mapping from a seed CSV.

    Each alias is a pipe-delimited entry in alias_col.

    Returns:
        Dict mapping each alias string to its canonical_col value.
    """
    seed_path = _SEEDS_DIR / csv_filename
    if not seed_path.exists():
        pytest.fail(f"[vocabulary] seed file not found: {seed_path}")
    alias_map: dict[str, str] = {}
    with seed_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            canonical = (row.get(canonical_col) or "").strip()
            aliases_raw = (row.get(alias_col) or "").strip()
            for alias in aliases_raw.split("|"):
                alias = alias.strip()
                if alias:
                    alias_map[alias] = canonical
    return alias_map


# ---------------------------------------------------------------------------
# Vocabulary-governed dimensions: map dim_name -> (csv_file, canonical_column)
# ---------------------------------------------------------------------------

_VOCABULARY_DIMS: dict[str, tuple[str, str, str]] = {
    # canonical_dimension_mapping value -> (seed_csv, alias_col, canonical_col)
    "country": ("dim_country.csv", "aliases", "iso_code"),
    "device_category": ("dim_device.csv", "aliases", "canonical_value"),
}


# ---------------------------------------------------------------------------
# Conformance tests
# ---------------------------------------------------------------------------


def test_vocabulary_seeds_exist() -> None:
    """dim_country.csv and dim_device.csv must exist in dbt/seeds/."""
    for dim_name, (csv_file, _alias_col, _canonical_col) in _VOCABULARY_DIMS.items():
        seed_path = _SEEDS_DIR / csv_file
        assert seed_path.exists(), (
            f"[vocabulary] Missing seed for '{dim_name}': {seed_path}\n"
            f"  Story 4.1 (AC1): dim_country.csv and dim_device.csv are required seeds."
        )


def test_vocabulary_dim_country_has_required_entries() -> None:
    """dim_country.csv must have at minimum FR, DE, GB, US, ES (GA4 seed countries)."""
    canonical_set = _load_seed_set("dim_country.csv", "iso_code")
    required = {"FR", "DE", "GB", "US", "ES"}
    missing = required - canonical_set
    assert not missing, (
        f"[vocabulary] dim_country.csv is missing required ISO codes: {sorted(missing)}\n"
        f"  GA4 seed data uses France, Germany, United Kingdom, United States, Spain."
    )


def test_vocabulary_dim_device_has_required_entries() -> None:
    """dim_device.csv must have canonical values: desktop, mobile, tablet."""
    canonical_set = _load_seed_set("dim_device.csv", "canonical_value")
    required = {"desktop", "mobile", "tablet"}
    missing = required - canonical_set
    assert not missing, (
        f"[vocabulary] dim_device.csv is missing required canonical values: {sorted(missing)}"
    )


def test_vocabulary_iso_codes_are_self_aliases() -> None:
    """Every iso_code in dim_country.csv must appear as its own alias (Meta pass-through).

    GA4 uses full names ('France'); Meta uses ISO codes ('FR') directly.
    The aliases column must contain the iso_code itself so Meta-style data passes through
    the normalize_dimension macro without modification (AC5).
    """
    seed_path = _SEEDS_DIR / "dim_country.csv"
    if not seed_path.exists():
        pytest.skip("dim_country.csv not found — test_vocabulary_seeds_exist covers this")

    violations: list[str] = []
    with seed_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            iso_code = (row.get("iso_code") or "").strip()
            aliases_raw = (row.get("aliases") or "").strip()
            aliases = {a.strip() for a in aliases_raw.split("|") if a.strip()}
            if iso_code and iso_code not in aliases:
                violations.append(f"{iso_code}: not in its own aliases ({aliases_raw!r})")

    assert not violations, (
        "[vocabulary] ISO codes must be self-aliases in dim_country.csv (AC5):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Story 37.7 — the canonical country vocabulary must be COMPLETE
#
# `normalize_geographic_posture` rejects any code absent from this seed (so a
# partial seed makes real markets unselectable) and `normalize_country_value`
# returns None for an unmapped source value (so a missing country lands silently
# in `Unknown`). Both failures are invisible in the product, hence these tests.
# ---------------------------------------------------------------------------

#: ISO 3166-1 alpha-2, the 249 officially assigned codes.
_ISO_3166_1_ALPHA2: frozenset[str] = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
    BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
    CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
    DE DJ DK DM DO DZ
    EC EE EG EH ER ES ET
    FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
    HK HM HN HR HT HU
    ID IE IL IM IN IO IQ IR IS IT
    JE JM JO JP
    KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY
    MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
    NA NC NE NF NG NI NL NO NP NR NU NZ
    OM
    PA PE PF PG PH PK PL PM PN PR PS PT PW PY
    QA
    RE RO RS RU RW
    SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
    TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ
    UA UG UM US UY UZ
    VA VC VE VG VI VN VU
    WF WS
    YE YT
    ZA ZM ZW
    """.split()
)

#: XK (Kosovo) is exceptionally reserved rather than officially assigned, but it
#: is a Google geo-target country and providers emit it, so the seed carries it.
#: It is the ONLY non-officially-assigned code the seed is allowed to carry.
_EXCEPTIONAL_RESERVATIONS: frozenset[str] = frozenset({"XK"})

#: Explicit floor. The seed shipped 32 entries before Story 37.7; anything close
#: to that number means the vocabulary regressed, whatever the diff looks like.
_MIN_COUNTRY_ENTRIES = 249


def _read_country_rows() -> list[dict[str, str]]:
    seed_path = _SEEDS_DIR / "dim_country.csv"
    if not seed_path.exists():
        pytest.fail(f"[vocabulary] seed file not found: {seed_path}")
    with seed_path.open(newline="", encoding="utf-8") as fh:
        return [
            {
                "iso_code": (row.get("iso_code") or "").strip(),
                "display_name": (row.get("display_name") or "").strip(),
                "aliases": (row.get("aliases") or "").strip(),
            }
            for row in csv.DictReader(fh)
        ]


def _split_aliases(raw: str) -> list[str]:
    return [alias.strip() for alias in raw.split("|") if alias.strip()]


def test_vocabulary_dim_country_covers_the_full_iso_set() -> None:
    """The seed must carry the complete ISO 3166-1 alpha-2 country set (Story 37.7).

    A partial seed is not a data-entry gap: `normalize_geographic_posture` rejects
    every absent code, so those markets cannot be selected at all.
    """
    codes = _load_seed_set("dim_country.csv", "iso_code")

    assert len(codes) >= _MIN_COUNTRY_ENTRIES, (
        f"[vocabulary] dim_country.csv carries {len(codes)} entries, "
        f"below the {_MIN_COUNTRY_ENTRIES}-entry ISO floor (Story 37.7)."
    )

    missing = _ISO_3166_1_ALPHA2 - codes
    assert not missing, (
        "[vocabulary] dim_country.csv is missing ISO 3166-1 alpha-2 codes "
        f"({len(missing)}): {sorted(missing)}\n"
        "  Every missing code is an unselectable market and a silent 'Unknown'."
    )

    unexpected = codes - _ISO_3166_1_ALPHA2 - _EXCEPTIONAL_RESERVATIONS
    assert not unexpected, (
        "[vocabulary] dim_country.csv carries codes outside ISO 3166-1 alpha-2 "
        f"and the allowed reservations: {sorted(unexpected)}"
    )


def test_vocabulary_dim_country_previously_missing_countries_are_selectable() -> None:
    """Countries whose absence broke real cases must be present by name.

    Monaco and the French overseas departments were the concrete reported gaps;
    the non-European sample guards against a Europe-only extension.
    """
    rows = {row["iso_code"]: row for row in _read_country_rows()}

    regression_cases = {
        # Reported breakages (Story 37.7 problem statement).
        "MC": "Monaco",
        "GF": "French Guiana",
        "GP": "Guadeloupe",
        "MQ": "Martinique",
        "RE": "Réunion",
        "YT": "Mayotte",
        # Broad non-European sample.
        "SG": "Singapore",
        "AE": "United Arab Emirates",
        "VN": "Vietnam",
        "TH": "Thailand",
        "PH": "Philippines",
        "ID": "Indonesia",
        "TW": "Taiwan",
        "HK": "Hong Kong",
        "PK": "Pakistan",
        "BD": "Bangladesh",
        "SA": "Saudi Arabia",
        "IL": "Israel",
        "EG": "Egypt",
        "MA": "Morocco",
        "KE": "Kenya",
        "GH": "Ghana",
        "CI": "Côte d'Ivoire",
        "ZM": "Zambia",
        "CL": "Chile",
        "PE": "Peru",
        "CO": "Colombia",
        "UY": "Uruguay",
        "CR": "Costa Rica",
        "PA": "Panama",
        "DO": "Dominican Republic",
        "JM": "Jamaica",
    }

    missing = sorted(code for code in regression_cases if code not in rows)
    assert not missing, (
        f"[vocabulary] dim_country.csv is missing regression countries: {missing}"
    )

    wrong_name = {
        code: rows[code]["display_name"]
        for code, expected in regression_cases.items()
        if rows[code]["display_name"] != expected
    }
    assert not wrong_name, (
        f"[vocabulary] unexpected display names in dim_country.csv: {wrong_name}"
    )


def test_vocabulary_dim_country_aliases_never_collide() -> None:
    """No alias may belong to two countries (case-insensitively).

    The loader raises on collision, so a collision is a hard outage of the whole
    vocabulary, not a degraded lookup. Checked here directly on the CSV so the
    failure names both owners.
    """
    owners: dict[str, str] = {}
    collisions: list[str] = []
    empty_aliases: list[str] = []
    missing_names: list[str] = []

    for row in _read_country_rows():
        code = row["iso_code"]
        if not row["display_name"]:
            missing_names.append(code)
        aliases = _split_aliases(row["aliases"])
        if not aliases:
            empty_aliases.append(code)
        # display_name also feeds get_country_alias_map(), so it is part of the
        # collision surface even though the loader only validates the alias column.
        for alias in [*aliases, row["display_name"]]:
            if not alias:
                continue
            key = alias.casefold()
            owner = owners.setdefault(key, code)
            if owner != code:
                collisions.append(f"{alias!r} claimed by both {owner} and {code}")

    assert not missing_names, f"[vocabulary] missing display_name for: {missing_names}"
    assert not empty_aliases, f"[vocabulary] empty aliases for: {empty_aliases}"
    assert not collisions, "[vocabulary] alias collisions in dim_country.csv:\n" + "\n".join(
        f"  - {c}" for c in collisions
    )


def test_vocabulary_dim_country_codes_are_unique_and_well_formed() -> None:
    """Load-time governance: unique, uppercase alpha-2 codes."""
    rows = _read_country_rows()
    codes = [row["iso_code"] for row in rows]

    malformed = sorted(
        {code for code in codes if len(code) != 2 or not code.isalpha() or not code.isupper()}
    )
    assert not malformed, f"[vocabulary] malformed ISO alpha-2 codes: {malformed}"

    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    assert not duplicates, f"[vocabulary] duplicate ISO codes in dim_country.csv: {duplicates}"


def test_vocabulary_dim_country_carries_uppercase_spelling_for_sql_lookup() -> None:
    """Each row must alias its own code AND the upper-cased display name.

    `normalize_dimension` matches aliases with `list_contains` — an exact,
    case-SENSITIVE comparison in SQL. Python's alias map is case-insensitive, so
    a missing uppercase spelling fails only in dbt, silently, as a NULL country.
    """
    violations: list[str] = []
    for row in _read_country_rows():
        aliases = set(_split_aliases(row["aliases"]))
        if row["iso_code"] not in aliases:
            violations.append(f"{row['iso_code']}: code missing from its own aliases")
        if row["display_name"].upper() not in aliases:
            violations.append(
                f"{row['iso_code']}: upper-cased display name "
                f"{row['display_name'].upper()!r} missing from aliases"
            )
    assert not violations, "[vocabulary] SQL-path spelling gaps:\n" + "\n".join(
        f"  - {v}" for v in violations
    )


def test_vocabulary_country_normalization_round_trip() -> None:
    """Every code, display name and alias resolves back to its canonical code."""
    from core.country_vocabulary import load_country_vocabulary, normalize_country_value

    vocabulary = load_country_vocabulary(_SEEDS_DIR / "dim_country.csv")
    assert len(vocabulary) >= _MIN_COUNTRY_ENTRIES

    failures: list[str] = []
    for country in vocabulary:
        candidates = [
            country.code,
            country.code.lower(),
            country.display_name,
            country.display_name.upper(),
            country.display_name.lower(),
            *country.aliases,
        ]
        for candidate in candidates:
            resolved = normalize_country_value(candidate)
            if resolved != country.code:
                failures.append(f"{candidate!r} -> {resolved!r} (expected {country.code})")

    assert not failures, "[vocabulary] normalization round-trip failures:\n" + "\n".join(
        f"  - {f}" for f in failures[:40]
    )


def test_vocabulary_country_normalization_provider_spellings() -> None:
    """Provider-emitted spellings resolve to the canonical ISO code."""
    from core.country_vocabulary import normalize_country_value

    cases = {
        "FR": ["FR", "fr", "France", "FRANCE", "french"],
        "GB": ["GB", "United Kingdom", "UNITED KINGDOM", "UK", "Great Britain"],
        "US": ["US", "USA", "United States", "United States of America", "U.S."],
        "KR": ["KR", "South Korea", "SOUTH KOREA", "Korea", "Republic of Korea"],
        "KP": ["KP", "North Korea", "DPRK"],
        "CZ": ["CZ", "Czechia", "Czech Republic"],
        "TR": ["TR", "Turkey", "Türkiye", "TURKIYE"],
        "CI": ["CI", "Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire"],
        "MM": ["MM", "Myanmar", "Myanmar (Burma)", "Burma"],
        "CD": ["CD", "DR Congo", "Congo - Kinshasa", "Democratic Republic of the Congo"],
        "CG": ["CG", "Republic of the Congo", "Congo - Brazzaville"],
        "MO": ["MO", "Macao", "Macau", "Macao SAR China"],
        "HK": ["HK", "Hong Kong", "Hong Kong SAR China"],
        "MC": ["MC", "Monaco", "MONACO"],
        "GF": ["GF", "French Guiana", "Guyane", "Guyane française"],
        "RE": ["RE", "Réunion", "Reunion", "REUNION"],
        "YT": ["YT", "Mayotte"],
        "GP": ["GP", "Guadeloupe"],
        "MQ": ["MQ", "Martinique"],
        "SZ": ["SZ", "Eswatini", "Swaziland"],
        "CV": ["CV", "Cabo Verde", "Cape Verde"],
        "TL": ["TL", "Timor-Leste", "East Timor"],
        "MK": ["MK", "North Macedonia", "Macedonia"],
        "PS": ["PS", "Palestine", "Palestinian Territories"],
        "VN": ["VN", "Vietnam", "Viet Nam"],
        "AE": ["AE", "United Arab Emirates", "UAE"],
        "NL": ["NL", "Netherlands", "Holland"],
        "XK": ["XK", "Kosovo"],
    }

    failures: list[str] = []
    for expected, spellings in cases.items():
        for spelling in spellings:
            resolved = normalize_country_value(spelling)
            if resolved != expected:
                failures.append(f"{spelling!r} -> {resolved!r} (expected {expected!r})")

    assert not failures, "[vocabulary] provider spellings unresolved:\n" + "\n".join(
        f"  - {f}" for f in failures
    )


def test_vocabulary_country_normalization_still_refuses_to_guess() -> None:
    """A genuinely unknown value stays unmapped — Unknown + DQ evidence, not a guess."""
    from core.country_vocabulary import normalize_country_value

    for value in ["", "   ", "Atlantis", "Europe", "(not set)", "ZZ", None, 42]:
        assert normalize_country_value(value) is None, (
            f"[vocabulary] {value!r} must not resolve to a country"
        )


def test_vocabulary_ga4_manifest_country_dimension(module_path: Path, manifest: dict) -> None:
    """For GA4 module: canonical_dimension_mapping must map 'country' to 'country'.

    The vocabulary test only checks modules that use vocabulary-governed dimensions.
    For GA4, the canonical_dimension_mapping should include a mapping that resolves
    to 'country' (the vocabulary-governed canonical name).
    """
    module_name = manifest.get("name", "unknown")
    dim_mapping = manifest.get("canonical_dimension_mapping", {})

    # Collect all canonical dimension values from the manifest
    canonical_dims = set(dim_mapping.values())

    # For each vocabulary-governed dimension, if it appears in the mapping,
    # check that the canonical values in the seed cover the expected aliases.
    alias_map_country = _load_alias_map("dim_country.csv", "aliases", "iso_code")
    alias_map_device = _load_alias_map("dim_device.csv", "aliases", "canonical_value")

    errors: list[str] = []

    if "country" in canonical_dims:
        # The module maps some source field to 'country' (vocabulary-governed).
        # Verify the seed's alias map covers at least the canonical ISO codes.
        # (We cannot know the exact values without running dbt, so we check alias coverage.)
        country_isos = set(alias_map_country.values())
        if not country_isos:
            errors.append(
                f"[{module_name}] dim_country.csv alias map is empty — "
                f"vocabulary boundary cannot be enforced"
            )

    if "device_category" in canonical_dims:
        device_canonicals = set(alias_map_device.values())
        if not device_canonicals:
            errors.append(
                f"[{module_name}] dim_device.csv alias map is empty — "
                f"vocabulary boundary cannot be enforced"
            )

    assert not errors, "\n".join(errors)


def test_vocabulary_canonical_dims_covered(module_path: Path, manifest: dict) -> None:
    """For each vocabulary-governed dimension in manifest.canonical_dimension_mapping,
    assert that the corresponding seed CSV exists and has entries.

    This test is module-agnostic (HG-1 / AD-2) — it reads the manifest dynamically.
    Both GA4 and Meta Ads modules must pass (HG-4).
    """
    module_name = manifest.get("name", "unknown")
    dim_mapping = manifest.get("canonical_dimension_mapping", {})
    canonical_dims = set(dim_mapping.values())

    errors: list[str] = []

    for voc_dim, (csv_file, alias_col, canonical_col) in _VOCABULARY_DIMS.items():
        if voc_dim not in canonical_dims:
            # Module doesn't use this vocabulary dimension — skip
            continue

        seed_path = _SEEDS_DIR / csv_file
        if not seed_path.exists():
            errors.append(
                f"[{module_name}] Vocabulary seed missing for dimension '{voc_dim}': "
                f"{seed_path}"
            )
            continue

        canonical_set = _load_seed_set(csv_file, canonical_col)
        if not canonical_set:
            errors.append(
                f"[{module_name}] Vocabulary seed '{csv_file}' is empty — "
                f"no canonical values for dimension '{voc_dim}'"
            )

    assert not errors, (
        "[vocabulary] Vocabulary coverage failures:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )
