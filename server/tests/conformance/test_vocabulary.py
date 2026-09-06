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
import json
import os
import re
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
    assert not missing, f"[vocabulary] dim_country.csv is missing regression countries: {missing}"

    wrong_name = {
        code: rows[code]["display_name"]
        for code, expected in regression_cases.items()
        if rows[code]["display_name"] != expected
    }
    assert not wrong_name, f"[vocabulary] unexpected display names in dim_country.csv: {wrong_name}"


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
        # NOTE (Story 37.7 repair): this is a FLOOR, not a guard — a non-empty seed
        # proves nothing about the spellings the module actually emits. GSC declared
        # ISO alpha-3 ('fra') while the seed carried none, and this assertion stayed
        # green while every GSC country landed in Unknown. The real guard is
        # test_vocabulary_declared_spellings_resolve below.
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
                f"[{module_name}] Vocabulary seed missing for dimension '{voc_dim}': {seed_path}"
            )
            continue

        canonical_set = _load_seed_set(csv_file, canonical_col)
        if not canonical_set:
            errors.append(
                f"[{module_name}] Vocabulary seed '{csv_file}' is empty — "
                f"no canonical values for dimension '{voc_dim}'"
            )

    assert not errors, "[vocabulary] Vocabulary coverage failures:\n" + "\n".join(
        f"  - {e}" for e in errors
    )


# ---------------------------------------------------------------------------
# Story 37.7 (repair) — the guard that could not fail
#
# Everything above checks that the SEED is well formed and non-empty. Nothing
# checked that the spellings a module DECLARES it emits actually resolve against
# that seed. GSC's api_catalog declares ISO 3166-1 alpha-3 ("e.g. 'fra', 'gbr'"),
# dim_country.csv carried no alpha-3 alias, and the whole vocabulary layer stayed
# green while every GSC country silently landed in Unknown.
#
# The two tests below read each module's OWN api_catalog.json (HG-1: no
# module-specific string here) and confront its declared spellings with the seed,
# on both reader paths:
#   * Python  — core.country_vocabulary, case-INsensitive;
#   * dbt/SQL — normalize_dimension + list_contains, exact and case-SENSITIVE.
# ---------------------------------------------------------------------------

#: Values quoted inside a parenthetical that carries an "e.g." — the catalogs'
#: convention for declaring the graphies a provider emits.
_EG_PARENTHETICAL_RE = re.compile(r"\(([^)]*\be\.?g\.?\b[^)]*)\)", re.IGNORECASE)
_QUOTED_RE = re.compile(r"['\"‘’“”]([^'\"‘’“”]+)")

#: An explicitly declared encoding family for a country field, and the alias
#: length it obliges the seed to carry for EVERY country.
_COUNTRY_ENCODING_RE = re.compile(r"alpha-?([23])", re.IGNORECASE)

#: The OTHER declared encoding family, and the one that went unproven. A provider
#: emitting English country names obliges the seed to carry a name alias for every
#: country, exactly as alpha-3 obliges a three-letter one. Until 2026-08-17 this
#: family matched no pattern, so the whole-seed test skipped for every connector
#: that declares names -- CM360, DV360 and GA4 -- and their proof stopped at the two
#: exemplars in the parenthetical.
_COUNTRY_NAME_FAMILY_RE = re.compile(r"country\s+name|country\s+from\s+which", re.IGNORECASE)


def _declared_spellings(description: str) -> list[str]:
    """Extract the example values a catalog description declares, if any.

    Only quoted tokens inside an "(e.g. …)" parenthetical count: a quoted field
    name elsewhere in the prose ("when 'country' is in the dimensions list") is
    documentation, not a value.
    """
    spellings: list[str] = []
    for group in _EG_PARENTHETICAL_RE.findall(description or ""):
        for raw in _QUOTED_RE.findall(group):
            token = raw.strip().strip(",").strip()
            if token and token not in spellings:
                spellings.append(token)
    return spellings


def _catalog_fields_by_source(module_path: Path) -> dict[str, dict]:
    catalog_path = module_path / "api_catalog.json"
    if not catalog_path.exists():
        return {}
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    fields = catalog.get("fields", []) if isinstance(catalog, dict) else catalog
    by_source: dict[str, dict] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        for key in (field.get("source_field"), field.get("field_id")):
            if key:
                by_source.setdefault(key, field)
    return by_source


def test_vocabulary_declared_spellings_resolve(module_path: Path, manifest: dict) -> None:
    """Every graphy a module's own catalog declares must resolve against the seed.

    This is the guard whose absence let Story 37.7 ship with GSC countries in
    Unknown. It fails, per module, when a declared spelling resolves on neither
    reader path — or when the two paths disagree.
    """
    module_name = manifest.get("name", "unknown")
    dim_mapping = manifest.get("canonical_dimension_mapping", {}) or {}
    by_source = _catalog_fields_by_source(module_path)

    errors: list[str] = []
    undeclared: list[str] = []
    checked = 0

    for source_field, canonical in dim_mapping.items():
        if canonical not in _VOCABULARY_DIMS:
            continue
        csv_file, alias_col, canonical_col = _VOCABULARY_DIMS[canonical]
        alias_map = _load_alias_map(csv_file, alias_col, canonical_col)
        exact_aliases = set(alias_map)
        folded_aliases = {alias.casefold(): value for alias, value in alias_map.items()}

        field = by_source.get(source_field)
        if field is None:
            # The catalog is the contract; a mapped source field must exist in it.
            errors.append(
                f"[{module_name}] '{source_field}' is mapped onto the "
                f"vocabulary-governed dimension '{canonical}' but is absent from "
                f"api_catalog.json — its emitted graphies cannot be verified"
            )
            continue

        spellings = _declared_spellings(field.get("description", ""))
        if not spellings:
            undeclared.append(f"{source_field} -> {canonical}")
            continue

        for spelling in spellings:
            checked += 1
            resolved = folded_aliases.get(spelling.casefold())
            if resolved is None:
                errors.append(
                    f"[{module_name}] declared '{canonical}' value {spelling!r} does not "
                    f"resolve against {csv_file} — every row carrying it lands in Unknown"
                )
                continue
            if spelling not in exact_aliases and spelling.upper() not in exact_aliases:
                errors.append(
                    f"[{module_name}] declared '{canonical}' value {spelling!r} resolves in "
                    f"Python but not on the dbt path: neither {spelling!r} nor "
                    f"{spelling.upper()!r} is an alias in {csv_file} (list_contains is exact)"
                )
            if canonical == "country":
                from core.country_vocabulary import normalize_country_value  # noqa: PLC0415

                python_reader = normalize_country_value(spelling)
                if python_reader != resolved:
                    errors.append(
                        f"[{module_name}] readers disagree on {spelling!r}: seed says "
                        f"{resolved!r}, core.country_vocabulary says {python_reader!r}"
                    )

    assert not errors, "[vocabulary] declared graphies unresolved:\n" + "\n".join(
        f"  - {e}" for e in errors
    )

    if not checked:
        if undeclared:
            pytest.skip(
                f"[{module_name}] api_catalog declares no example graphy for "
                f"{undeclared} — the emitted values cannot be confronted with the seed"
            )
        pytest.skip(f"[{module_name}] maps no vocabulary-governed dimension")


def test_vocabulary_declared_country_encoding_is_carried_by_the_seed(
    module_path: Path, manifest: dict
) -> None:
    """A declared encoding binds the WHOLE seed, not just the two example values.

    GSC declares alpha-3 for every country it returns, so making 'fra' and 'gbr'
    resolve while leaving the other 247 unmapped would repair the exemplar and
    leave the class broken.
    """
    module_name = manifest.get("name", "unknown")
    dim_mapping = manifest.get("canonical_dimension_mapping", {}) or {}
    by_source = _catalog_fields_by_source(module_path)

    declared_lengths: dict[int, str] = {}
    declared_names: dict[str, str] = {}
    for source_field, canonical in dim_mapping.items():
        if canonical != "country":
            continue
        field = by_source.get(source_field)
        if field is None:
            continue
        description = field.get("description", "") or ""
        match = _COUNTRY_ENCODING_RE.search(description)
        if match:
            declared_lengths[int(match.group(1))] = source_field
        elif _COUNTRY_NAME_FAMILY_RE.search(description):
            declared_names[source_field] = description

    if not declared_lengths and not declared_names:
        pytest.skip(f"[{module_name}] declares no country encoding for its country field")

    gaps: list[str] = []
    for length, source_field in sorted(declared_lengths.items()):
        for row in _read_country_rows():
            code = row["iso_code"]
            if code in _EXCEPTIONAL_RESERVATIONS:
                # XK is a reservation, not an officially assigned code: ISO gives it
                # no alpha-3. Whatever a provider emits for Kosovo is not ISO and
                # belongs to per-client conformance, not to this seed.
                continue
            if not any(
                len(alias) == length and alias.isalpha() for alias in _split_aliases(row["aliases"])
            ):
                gaps.append(code)
        assert not gaps, (
            f"[{module_name}] '{source_field}' is declared as ISO alpha-{length}, but "
            f"dim_country.csv carries no {length}-letter alias for {len(gaps)} countries: "
            f"{sorted(gaps)[:12]}{' …' if len(gaps) > 12 else ''}\n"
            "  Each one is a real country that lands in Unknown."
        )

    # Story 37.7, closed 2026-08-17. A NAME is an encoding family too, and it was the
    # unproven one: CM360, DV360 and GA4 declare "Country name (e.g. 'France', 'United
    # States')", which matches no `alpha-N`, so this test SKIPPED for all three and the
    # proof stopped at two exemplars. Two names resolving proved nothing about the other
    # 248 -- exactly the shape of the defect 37.7 was opened for, where 'fra' and 'gbr'
    # would have resolved while GSC's remaining countries landed in Unknown.
    #
    # A provider emitting English country names obliges the seed to carry a name alias
    # for EVERY country, and `display_name` is not enough on its own: the dbt path is
    # `list_contains` over `aliases`, which is exact, so a name present only in
    # `display_name` resolves in Python and not in the warehouse.
    for source_field in sorted(declared_names):
        name_gaps: list[str] = []
        for row in _read_country_rows():
            aliases = _split_aliases(row["aliases"])
            display = str(row["display_name"]).strip()
            # A "name" alias is any alias longer than an alpha-3 code that is not a
            # bare code: the seed writes them upper-cased AND in display form, and
            # either resolves, so requiring one specific casing would fail a seed that
            # is correct.
            if not any(len(alias) > 3 for alias in aliases):
                name_gaps.append(row["iso_code"])
            elif display and display.casefold() not in {
                alias.casefold() for alias in aliases
            }:
                # The display name itself must be reachable on the exact dbt path.
                name_gaps.append(row["iso_code"])
        assert not name_gaps, (
            f"[{module_name}] '{source_field}' is declared as a country NAME, but "
            f"dim_country.csv carries no matching name alias for {len(name_gaps)} "
            f"countries: {sorted(name_gaps)[:12]}{' …' if len(name_gaps) > 12 else ''}\n"
            "  A provider that emits names and a seed that only carries codes puts "
            "every one of them in Unknown."
        )


def test_vocabulary_country_mappings_normalize_in_module_staging(
    module_path: Path, manifest: dict
) -> None:
    """Country mappings must cross the shared dbt vocabulary boundary.

    Catalog examples prove that declared spellings are representable, but they
    cannot prove that the warehouse actually performs the lookup. Requiring the
    shared macro and the retained source spelling closes the gap where DV360
    passed its raw country column straight through staging.
    """
    module_name = manifest.get("name", "unknown")
    dim_mapping = manifest.get("canonical_dimension_mapping", {}) or {}
    if "country" not in dim_mapping.values():
        pytest.skip(f"[{module_name}] maps no country dimension")

    staging_dir = module_path / "dbt" / "staging"
    sql_files = sorted(staging_dir.glob("*.sql")) if staging_dir.exists() else []
    sql_by_path = {path: path.read_text(encoding="utf-8") for path in sql_files}
    normalized = [
        path
        for path, sql in sql_by_path.items()
        if "normalize_dimension" in sql and "dim_country" in sql
    ]
    assert normalized, (
        f"[{module_name}] maps a source field to canonical country but no staging "
        "model routes it through normalize_dimension(..., dim_country, ...)"
    )

    source_preserved = [path for path in normalized if "country_source" in sql_by_path[path]]
    assert source_preserved, (
        f"[{module_name}] normalizes country without retaining country_source; "
        "unresolved values cannot produce actionable DQ evidence"
    )
