"""Story 37.9 AC5, repo-level: no client geography ships as a platform default.

The rule, verbatim from the spec: *no market definition, preset, or alias may be
shipped as a platform-wide default*. This is asserted over the SHIPPED artifacts,
not over intent, because the failure mode is silent -- a preset that ships as a
seeded row or a market composition that ships as a code constant would apply one
client's decision to every other client without anyone noticing.

Not asserted here (deliberately): the ~250-entry ISO seed and its per-country
aliases. Those ARE the legal set of canonical values, identical for every client
(Story 37.7) -- the thing the MDM layer resolves *to*.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SERVER_CORE = _REPO_ROOT / "server" / "core"
_MIGRATIONS = _REPO_ROOT / "infra" / "nango" / "migrations"

# The geography modules that could plausibly carry a shipped default.
_GEO_MODULES = (
    "geographic_reporting.py",
    "geographic_semantics.py",
    "geographic_conformance.py",
    "market_governance.py",
    "country_vocabulary.py",
    # Story 48.2: the module that ships the presets is IN the scan. It was
    # absent before, so the blanket ban below passed by omission rather than by
    # design -- a rule that cannot see the file it governs proves nothing.
    "country_registry.py",
)

# Unknown is a computed read-layer bucket, never budgetable, and is allowed to
# be named in code. `__other_markets__` is gone: Rest of World is a governed
# node with a per-Project id, so no market id is nameable in code any more.
_ALLOWED_MARKET_IDS = {"__unknown__"}


def _geo_sources() -> list[tuple[Path, str]]:
    out = []
    for name in _GEO_MODULES:
        path = _SERVER_CORE / name
        assert path.exists(), f"expected geography module missing: {path}"
        out.append((path, path.read_text(encoding="utf-8")))
    return out


def test_no_module_level_market_composition_ships_in_code() -> None:
    """No shipped constant may define what a market contains.

    A market composition is ``{'id', 'label', 'country_codes'}``. Finding one as a
    module-level literal would mean the platform chose a definition for the client.
    """

    offenders: list[str] = []
    for path, source in _geo_sources():
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if {"id", "label", "country_codes"} <= keys:
                # Allowed only when country_codes is not a literal list of codes
                # (i.e. it is derived from caller data at runtime).
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "country_codes"
                        and isinstance(value, (ast.List, ast.Tuple))
                        and value.elts
                        and all(isinstance(el, ast.Constant) for el in value.elts)
                    ):
                        offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "a market composition ships as a code literal (platform-chosen definition): "
        + ", ".join(offenders)
    )


def test_no_default_market_selection_ships_as_a_platform_constant() -> None:
    """A DEFAULT market selection is still banned. A PRESET no longer is.

    Story 37.9 banned the word `PRESET` outright, which was the only tool
    available when nothing could hold provenance. Story 48.2 (AC4) replaces the
    word-ban with the three properties that made a preset dangerous, proven
    below: it must not self-apply, must not stay a live dependency, and must
    not carry a client alias at platform scope. What stays banned is a
    *default* -- a composition the platform picks when the client picked none.
    """

    pattern = re.compile(r"^\s*(?P<name>[A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=", re.MULTILINE)
    banned = re.compile(
        r"(DEFAULT_MARKET|MARKET_DEFAULT|DEFAULT_COUNTRIES|"
        r"DEFAULT_COUNTRY_CODES|PRIORITY_COUNTRIES)",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path, source in _geo_sources():
        for match in pattern.finditer(source):
            name = match.group("name")
            if banned.search(name):
                offenders.append(f"{path.name}:{name}")
    assert not offenders, (
        "a market default ships as a platform constant: " + ", ".join(offenders)
    )


def test_no_shipped_preset_applies_itself() -> None:
    """A preset is read to build a draft, and is never consulted again."""

    from core import country_registry as cr

    # Storing every preset is a write to an immutable table and nothing else:
    # no registry, no node and no version is created as a side effect. The only
    # function that mints Project objects is materialize_preset, which an
    # operator calls with an explicit preset id.
    source = (_SERVER_CORE / "country_registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    seeding = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "seed_country_presets"
    )
    called = {
        node.func.id
        for node in ast.walk(seeding)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not ({"create_node", "create_draft_version", "create_registry"} & called), (
        "seeding a preset creates Project objects: a preset that applies itself "
        "is an authority nobody chose"
    )
    assert cr.COUNTRY_PRESETS, "the presets exist and are inert, not absent"


def test_no_preset_remains_a_live_dependency_of_what_it_produced() -> None:
    """Materializing records provenance, and nothing reads the preset again."""


    source = (_SERVER_CORE / "country_registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    projecting = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_projection"
    )
    text = ast.dump(projecting)
    assert "preset" not in text.lower(), (
        "the read-time projection consults a preset: a materialized draft must "
        "have no live dependency on what it was seeded from"
    )
    # Provenance is retained -- the distinction is 'recorded', not 'wired'.
    assert "origin_preset_version_id" in source


def test_no_preset_ships_a_client_alias_at_platform_scope() -> None:
    """A preset names canonical codes. Spelling repair stays a scoped decision."""

    from core import country_registry as cr
    from core.country_vocabulary import get_supported_country_codes

    legal = get_supported_country_codes()
    for preset in cr.COUNTRY_PRESETS:
        for member in preset.members:
            assert member.value in legal, (
                f"preset {preset.preset_key} names {member.value!r}, which is not a "
                "canonical code -- a provider spelling must never ship at platform scope"
            )


def _preset_line_range(source: str) -> tuple[int, int] | None:
    """The lines the shipped preset catalogue occupies, if this module has one."""

    tree = ast.parse(source)
    for node in tree.body:
        targets = getattr(node, "targets", None) or (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "COUNTRY_PRESETS":
                return node.lineno, (node.end_lineno or node.lineno)
    return None


def test_no_iso_code_tuple_masquerades_as_a_tracked_set() -> None:
    """A literal ISO list is a platform-chosen SELECTION -- except inside a preset.

    A bare ``('FR', 'DE', 'IT')`` in the geography layer is a tracked-market
    selection the platform made for the client, and stays banned.

    A preset is the one legitimate exception, and the reason is precisely why
    Story 37.9 banned presets and Story 48.2 (AC4) un-banned them under
    conditions: a preset's members MUST be explicit literals, because "exact
    members visible before use" is the property that makes it inspectable. What
    makes it safe is not hiding the list -- it is that the list is offered,
    versioned, provenance-bearing, and never applied without an explicit act.
    Those three properties are proven above, one test each.
    """

    code_re = re.compile(r"^[A-Z]{2}$")
    offenders: list[str] = []
    for path, source in _geo_sources():
        exempt = _preset_line_range(source)
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                continue
            elts = node.elts
            if len(elts) < 2:
                continue
            if exempt and exempt[0] <= node.lineno <= exempt[1]:
                continue
            if all(
                isinstance(el, ast.Constant)
                and isinstance(el.value, str)
                and code_re.fullmatch(el.value)
                for el in elts
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "a literal ISO code list ships in the geography layer (a platform-chosen "
        "tracked set): " + ", ".join(offenders)
    )


def test_no_migration_seeds_a_market_or_a_client_alias() -> None:
    """Migrations may create structure; they may never INSERT a market or an alias."""

    insert_re = re.compile(
        r"INSERT\s+INTO\s+app\.(market_bindings|project_preferences|dimension_value_mappings)",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if insert_re.search(text):
            offenders.append(path.name)
    assert not offenders, (
        "a migration seeds market master data or a client value mapping: "
        + ", ".join(offenders)
    )


def test_no_default_conformance_mapping_ships_at_platform_scope() -> None:
    """The country conformance layer must persist at PROJECT scope only.

    A PLATFORM-scoped country value mapping would be exactly the hardcode Story
    37.9 removes: one client's spelling imposed on all the others.
    """

    source = (_SERVER_CORE / "geographic_conformance.py").read_text(encoding="utf-8")
    assert "SCOPE_PROJECT" in source
    assert "SCOPE_PLATFORM" not in source
    assert "SCOPE_ORG" not in source


def test_the_shared_seed_carries_only_the_legal_iso_set() -> None:
    """The seed is the legal set of canonical values -- not a market registry."""

    seed = _REPO_ROOT / "dbt" / "seeds" / "dim_country.csv"
    if not seed.exists():  # pragma: no cover - repo layout guard
        pytest.skip("country seed not present in this checkout")
    header = seed.read_text(encoding="utf-8").splitlines()[0]
    columns = {col.strip().lower() for col in header.split(",")}
    forbidden = {"market", "market_id", "market_label", "markets", "preset", "group"}
    assert not (columns & forbidden), (
        "the shared vocabulary seed carries market/preset master data: "
        f"{sorted(columns & forbidden)}"
    )


def test_market_ids_named_in_code_are_only_the_synthetic_groupings() -> None:
    """Nothing in the read layer may name a real market id."""

    from core import geographic_semantics as gs

    assert gs.UNKNOWN_BUCKET_ID in _ALLOWED_MARKET_IDS
    # Rest of World is no longer nameable in code: its id is minted per Project.
    assert not hasattr(gs, "OTHER_MARKETS")


def _global_posture():
    from core.geographic_reporting import GeographicPosture

    return GeographicPosture()


def test_a_fresh_project_is_offered_no_market(tmp_path) -> None:
    """A project with no configuration resolves to Global with an empty selection."""

    from core.geographic_reporting import GLOBAL, GeographicPosture

    # This is exactly what fetch_project_geographic_posture returns when no
    # preference row exists yet.
    posture = GeographicPosture()
    assert posture.mode == GLOBAL
    assert posture.markets == ()
    assert posture.country_codes == ()


def test_json_artifacts_ship_no_market_definition() -> None:
    """No shipped JSON (manifests, catalogs, configs) may define a market."""

    offenders: list[str] = []
    roots = [_REPO_ROOT / "server" / "modules", _REPO_ROOT / "server" / "core"]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            # A JSON *Schema* describes the shape a market may take; it does not
            # define one. Only instance documents can ship a definition.
            if path.name.endswith(".schema.json"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 -- unreadable artifact is not this test's job
                continue
            if _contains_market_definition(payload):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        "a shipped JSON artifact defines a market: " + ", ".join(offenders)
    )


def _contains_market_definition(node: object) -> bool:
    if isinstance(node, dict):
        keys = set(node)
        codes = node.get("country_codes")
        if (
            {"id", "label", "country_codes"} <= keys
            and isinstance(codes, list)
            and codes
            and all(isinstance(code, str) for code in codes)
        ):
            return True
        if "local_markets" in keys and node.get("local_markets"):
            return True
        return any(_contains_market_definition(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_market_definition(item) for item in node)
    return False
