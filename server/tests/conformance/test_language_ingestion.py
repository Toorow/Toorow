"""Story 27.8 -- a language value ENTERS the product canonical, or it enters as a gap.

WHY THIS FILE EXISTS. `core.language_dimensions.adapt_source_value` -- the per-source
adapter -- shipped on 2026-07-25 with no production caller. Meanwhile four manifests
bound seven source fields to the language family, and every connector renamed its
columns through a plain dict. Two consequences measured on 2026-08-22, both silent:

  * google-analytics binds BOTH `language` ('English') and `languageCode` ('en-us') to
    `audience_language`. `canonical_row[rename_map.get(key, key)] = value` makes the
    LAST field of manifest.json win, so a pull selecting both columns dropped one of
    them and nobody could see which.
  * no transform called the adapter, so 'English' could be stored as a canonical value
    of a dimension whose declared vocabulary is BCP 47.

The rule is written in docs/product-architecture/governance.md, section "'Language' is
three dimensions, and an encoding is not one of them".

EVERYTHING BELOW IS DERIVED FROM THE SHIPPED MANIFESTS. No connector is named in an
assertion: a module that binds a language tomorrow is judged by exactly these rules,
and a module that stops binding one drops out of the parametrisation on its own.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from core import language_dimensions as ld

_MODULES_DIR = Path(__file__).resolve().parents[2] / "modules"


def _language_binding_modules() -> list[tuple[str, dict[str, str]]]:
    """[(connector, {source_field -> language dimension})] over the shipped tree."""
    found: list[tuple[str, dict[str, str]]] = []
    for manifest_path in sorted(_MODULES_DIR.glob("*/manifest.json")):
        with manifest_path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        targets = ld.language_targets(manifest.get("canonical_dimension_mapping"))
        if targets:
            found.append((manifest_path.parent.name, targets))
    return found


_BINDINGS = _language_binding_modules()


def _load_connector(connector: str):
    path = _MODULES_DIR / connector / "connector.py"
    spec = importlib.util.spec_from_file_location(f"_lang_{connector.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shipped_tree_still_binds_at_least_one_language() -> None:
    """A parametrised suite over an empty list is a green that proves nothing."""
    assert _BINDINGS, (
        "no shipped manifest binds a language dimension any more -- either the family "
        "was removed (then this file goes with it) or a mapping was lost"
    )


@pytest.mark.parametrize(("connector", "targets"), _BINDINGS, ids=[c for c, _ in _BINDINGS])
def test_every_language_encoding_reaches_the_canonical_vocabulary(
    connector: str, targets: dict[str, str]
) -> None:
    """Each bound source field, fed its provider's own encoding, comes out BCP 47.

    The value fed is the DISPLAY NAME, because that is the encoding a plain rename map
    passes through untouched -- it is the shape that used to reach the warehouse.
    """
    module = _load_connector(connector)
    supported = ld.get_language_alias_map() if hasattr(ld, "get_language_alias_map") else None
    assert supported is None or supported  # the vocabulary must be readable at all

    for source_field, dimension in sorted(targets.items()):
        rows = module.transform([{source_field: "French"}])
        assert len(rows) == 1, f"{connector}.transform dropped a row"
        value = rows[0].get(dimension)
        assert value == "fr", (
            f"[{connector}] '{source_field}' -> {dimension} produced {value!r}, not the "
            f"canonical BCP 47 value 'fr'. The transform must call "
            f"core.language_dimensions.adapt_manifest_row_languages."
        )
        assert source_field not in rows[0] or source_field == dimension, (
            f"[{connector}] the raw field '{source_field}' survived beside its canonical "
            f"dimension -- two columns for one dimension is the collision, not the repair"
        )


@pytest.mark.parametrize(("connector", "targets"), _BINDINGS, ids=[c for c, _ in _BINDINGS])
def test_two_encodings_of_one_dimension_never_race_on_field_order(
    connector: str, targets: dict[str, str]
) -> None:
    """Where a connector binds several fields to ONE dimension, the finest grain wins.

    Field order in a JSON file is not a semantic decision. This is the exact defect
    measured on google-analytics; the test is written over whatever the tree declares
    so the next connector to do it is caught without an edit here.
    """
    by_dimension: dict[str, list[str]] = {}
    for source_field, dimension in targets.items():
        by_dimension.setdefault(dimension, []).append(source_field)
    multi = {dim: sorted(fields) for dim, fields in by_dimension.items() if len(fields) > 1}
    if not multi:
        pytest.skip(f"{connector} binds one source field per language dimension")

    module = _load_connector(connector)
    for dimension, fields in sorted(multi.items()):
        # The coarse encoding first, then the fine one -- and the reverse. The answer
        # must not depend on which one the dict happened to visit last.
        coarse, fine = fields[0], fields[-1]
        forward = module.transform([{coarse: "French", fine: "fr-FR"}])[0]
        backward = module.transform([{fine: "fr-FR", coarse: "French"}])[0]
        assert forward.get(dimension) == "fr-FR", (
            f"[{connector}] {dimension} kept {forward.get(dimension)!r}: the coarser "
            f"encoding of '{coarse}' won over the locale '{fine}' carried"
        )
        assert backward.get(dimension) == forward.get(dimension), (
            f"[{connector}] {dimension} depends on the ORDER of the source fields"
        )


@pytest.mark.parametrize(("connector", "targets"), _BINDINGS, ids=[c for c, _ in _BINDINGS])
def test_a_value_no_adapter_can_read_is_absent_not_invented(
    connector: str, targets: dict[str, str]
) -> None:
    """An unparseable language leaves NO canonical value. Never a guess, never the raw string."""
    module = _load_connector(connector)
    for source_field, dimension in sorted(targets.items()):
        row = module.transform([{source_field: "Klingon"}])[0]
        assert dimension not in row, (
            f"[{connector}] '{source_field}'='Klingon' published {row.get(dimension)!r} as a "
            f"canonical {dimension}"
        )


def test_no_manifest_binds_a_language_it_cannot_name() -> None:
    """Every language target declared by the tree is a MEMBER of the closed family."""
    for connector, targets in _BINDINGS:
        for source_field, dimension in sorted(targets.items()):
            assert ld.describe_dimension(dimension) is not None, (
                f"[{connector}] '{source_field}' targets '{dimension}', which the family "
                f"does not declare"
            )
