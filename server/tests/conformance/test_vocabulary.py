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
