"""A connector that declares it READS a zone must RETURN what it read -- AI-161.

Story 39.7 declared the capture contract and connectors honoured half of it: they resolve
the zone and bury it in the landed row. The pull result carried ``{pull_id, row_count,
date_from, date_to}`` and nothing else, so ``queue._execute_job`` -- the only place that
knows the datastream, the project AND that the run succeeded -- could not record what the
pull observed. ``time_boundary.record_boundary_evidence`` therefore had ZERO callers in the
whole repository, while ``capability_compilers`` READ the table it fills and told the
operator "Run this Datastream so its publication records the source day boundary". They
could run it forever.

This is a CLASS guard, not a check on the two connectors repaired today. It reads the
manifests, keeps the ones whose ``time_context.locus`` is a metadata locus -- the loci that
mean "I read a live zone from provider metadata at pull" -- and requires their pull
functions to return the contract key. A connector added tomorrow with ``locus: network``
and a pull that stays silent fails here rather than in six months, on a screen, as another
capability that can never reach `complete`.

Loci deliberately NOT required to return anything:
  * ``fixed``  -- the zone comes from the declaration, resolvable with no pull at all;
  * ``none``   -- the provider exposes no report timezone; the gap IS the answer.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULES = ROOT / "server" / "modules"

#: Mirrors ``core.report_timezone._METADATA_LOCI``. Duplicated on purpose: a conformance
#: test that imports the thing it guards stops guarding it the day that thing is wrong.
METADATA_LOCI = {"network", "property", "account"}

CONTRACT_KEY = "report_timezone"


def _declared_metadata_locus() -> list[tuple[str, str]]:
    """(module_name, locus) for every connector declaring a metadata time_context locus."""
    found: list[tuple[str, str]] = []
    for manifest_path in sorted(MODULES.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        caps = manifest.get("source_capabilities")
        time_context = caps.get("time_context") if isinstance(caps, dict) else None
        locus = time_context.get("locus") if isinstance(time_context, dict) else None
        if locus in METADATA_LOCI:
            found.append((manifest_path.parent.name, locus))
    return found


def _pull_functions_returning_dicts(source: str) -> list[ast.Dict]:
    """Every dict literal returned by a top-level ``pull*`` function."""
    tree = ast.parse(source)
    returned: list[ast.Dict] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("pull"):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Dict):
                returned.append(inner.value)
    return returned


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def test_at_least_one_connector_declares_a_metadata_locus():
    """Guard the guard: if nothing matches, the test below passes by vacuity."""
    assert _declared_metadata_locus(), (
        "no connector declares a metadata time_context locus -- this conformance test "
        "would then assert nothing at all"
    )


@pytest.mark.parametrize("module_name,locus", _declared_metadata_locus())
def test_a_metadata_locus_connector_returns_the_zone_it_read(module_name, locus):
    connector = MODULES / module_name / "connector.py"
    assert connector.exists(), f"{module_name} declares locus={locus!r} but has no connector.py"

    returns = _pull_functions_returning_dicts(connector.read_text(encoding="utf-8"))
    assert returns, f"{module_name}: no pull* function returning a dict literal was found"

    missing = [node.lineno for node in returns if CONTRACT_KEY not in _dict_keys(node)]
    assert not missing, (
        f"{module_name} declares time_context.locus={locus!r} -- it reads a live zone from "
        f"provider metadata at pull -- but its pull result at line(s) {missing} does not "
        f"return {CONTRACT_KEY!r}. The worker cannot record what it cannot see, and the "
        "Reporting Timezone capability stays unavailable for this Datastream forever. "
        "Return the zone resolved through core.report_timezone.resolve_capture; None is a "
        "legitimate value and is recorded as the gap it is."
    )
