"""The layer contract of the Country capability is measured, not declared.

Amendment 2026-07-25 made `specs/spec-toorow/geographic-reporting.md` §Layer
contract the canonical source of Story 37.5's second half -- "every layer is
exercised by at least one test". The section did not exist until 2026-08-22, so
half of the acceptance had no source at all, and the epic's own list still named
`geographic_change.py`, a module deleted on 2026-08-17.

That is the defect class this file answers: **a contract stated in prose, in one
document, never confronted with the tree**. So the assertions below read the
ratified table and the repository, and refuse three ways for the two to drift:

* an owner or a covering test that does not exist on disk;
* a covering test that never names its owner -- "covered" claimed, not shown;
* a layer the epic binds that the canonical table forgot to carry.

A layer with no test is legal, and says so: `Covered by` is `uncovered` and the
obligation names an owner and a trigger. It is never left out and never assumed
green.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "_bmad-output/specs/spec-toorow/geographic-reporting.md"
EPIC_37 = REPO_ROOT / "_bmad-output/planning-artifacts/epic-37-geographic-reporting-posture.md"

#: The layer table is bounded by the section that follows it, and this file used
#: to name that section by its title -- which made it a second module matching on
#: the `Incomplete if` marker, the one thing
#: `test_criteria_parser.py::test_no_module_anywhere_re_implements_the_walk`
#: refuses. It never needed the criteria list; it needed a section boundary. So
#: the boundary comes from the module that owns markdown structure here, and this
#: file spells no marker at all.
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
AUDIT = importlib.import_module("finished_work_audit")

_LAYER_CONTRACT = "Layer contract"

_CODE = re.compile(r"`([^`]+)`")

# Where the capability lives. Bounded on purpose: the guard must stay cheap enough
# to run on every conformance pass.
_SEARCH_ROOTS = ("server", "scripts", "dbt/models", "dbt/macros", "dbt/seeds", "ui/admin/src")

# The layers Amendment 2026-07-25 binds. Kept here as the epic SPELLS them, so a
# layer quietly dropped from the canonical table fails this file by name rather
# than shrinking the contract in silence.
_BOUND_LAYERS = (
    "Vocabulary seed",
    "Posture aggregate",
    "Project Settings activation",
    "Plan compilation",
    "Change governance",
    "Semantic grouping",
    "Warehouse and dbt",
    "Report envelope",
    "Tools and MCP",
    "Narrative and cards",
    "Coverage read model",
    "Governance > Master Data editor",
    "UI",
    "Audit",
    "Permissions",
)


class _Layer:
    __slots__ = ("name", "obligation", "owners", "tests")

    def __init__(self, name: str, obligation: str, owners: list[str], tests: list[str]):
        self.name = name
        self.obligation = obligation
        self.owners = owners
        self.tests = tests

    def __repr__(self) -> str:  # pragma: no cover - assertion messages only
        return f"<layer {self.name}>"


def _layers() -> list[_Layer]:
    """Parse the canonical table. A missing section is itself the failure."""

    body = AUDIT.section_body(SPEC.read_text(encoding="utf-8"), _LAYER_CONTRACT)
    assert body is not None, (
        "`specs/spec-toorow/geographic-reporting.md` carries no `## Layer contract` "
        "section, and Amendment 2026-07-25 declares that section the canonical source "
        "of half of Story 37.5's acceptance. Half an acceptance with no source is not "
        "an acceptance."
    )

    parsed: list[_Layer] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4 or cells[0] == "Layer":
            continue
        name, obligation, owner_cell, test_cell = cells
        parsed.append(
            _Layer(name, obligation, _CODE.findall(owner_cell), _CODE.findall(test_cell))
        )
    assert parsed, "the `## Layer contract` section carries no table rows"
    return parsed


def test_every_bound_layer_is_carried_by_the_canonical_table() -> None:
    named = {layer.name for layer in _layers()}
    missing = [layer for layer in _BOUND_LAYERS if layer not in named]
    assert not missing, (
        f"the epic binds these layers and the canonical table does not carry them: "
        f"{missing}. A layer dropped from the table is a layer nobody has to cover."
    )


def test_every_named_owner_exists_in_the_tree() -> None:
    """The previous list named `geographic_change.py`, deleted on 2026-08-17."""

    offenders: list[str] = []
    for layer in _layers():
        assert layer.owners, f"layer `{layer.name}` names no owner"
        for owner in layer.owners:
            if not (REPO_ROOT / owner).exists():
                offenders.append(f"{layer.name} -> {owner}")
    assert not offenders, (
        f"these layers are owned by paths that do not exist: {offenders}. This is how "
        "half the contract came to point at a retired module: the list was prose, and "
        "prose does not notice a deletion."
    )


def test_every_layer_is_covered_or_names_its_owner_and_trigger() -> None:
    uncovered_without_escape: list[str] = []
    missing_tests: list[str] = []
    for layer in _layers():
        if layer.tests == ["uncovered"] or "uncovered" in layer.tests:
            has_escape = "Owner:" in layer.obligation and "Trigger:" in layer.obligation
            if not has_escape:
                uncovered_without_escape.append(layer.name)
            continue
        assert layer.tests, f"layer `{layer.name}` names no covering test"
        for test in layer.tests:
            if not (REPO_ROOT / test).exists():
                missing_tests.append(f"{layer.name} -> {test}")
    assert not missing_tests, f"covering tests that do not exist: {missing_tests}"
    assert not uncovered_without_escape, (
        "an uncovered layer must name an owner and a trigger in its obligation, so it "
        "is a debt somebody holds rather than a silence: "
        f"{uncovered_without_escape}"
    )


def test_each_covering_test_actually_names_its_owner() -> None:
    """"Covered by" is a measurement, not a claim.

    A test file listed against an owner it never mentions covers nothing. The link
    is checked on the owner's STEM (`country_registry`, `CountryWorkspace`,
    `normalize_dimension`), because a Python test imports `core.country_registry`,
    a Vitest file imports `../governance/CountryWorkspace`, and a dbt test calls
    `ref()` / the macro by name -- three spellings of one relation.
    """

    unproven: list[str] = []
    for layer in _layers():
        stems = {Path(owner).stem for owner in layer.owners}
        for test in layer.tests:
            if test == "uncovered":
                continue
            text = (REPO_ROOT / test).read_text(encoding="utf-8", errors="replace")
            if not any(stem in text for stem in stems):
                unproven.append(f"{layer.name}: {test} never names any of {sorted(stems)}")
    assert not unproven, (
        f"these coverage claims are prose -- the test never names the owner it covers: "
        f"{unproven}"
    )


def test_the_epic_points_at_the_canonical_section_and_no_retired_module() -> None:
    epic = EPIC_37.read_text(encoding="utf-8")
    assert "§Layer contract" in epic
    assert "geographic-reporting.md" in epic
    # The binding list may cite modules; none of them may be gone. Searched over the
    # roots the capability actually lives in -- an unbounded `rglob` walks `.venv`
    # and `node_modules` and takes minutes, which is a guard nobody would keep.
    #
    # Bounded to the ENUMERATION, not to everything under the heading: the amendment
    # note beneath it names `geographic_change.py` on purpose, to record what was
    # retired. A guard that counted that note would push the explanation out of the
    # document to stay green -- the same trap the posture ratchet fell into first.
    binding = epic.split("**Layer contract (binding).**", 1)[1].split(
        "which is the canonical source.", 1
    )[0]
    for module in _CODE.findall(binding):
        if not module.endswith((".py", ".sql", ".tsx", ".csv")):
            continue
        alive = [
            match
            for root in _SEARCH_ROOTS
            for match in (REPO_ROOT / root).rglob(module)
            if "__pycache__" not in match.parts
        ]
        assert alive, (
            f"the epic's binding layer list names `{module}`, which is not in the tree. "
            "A layer named by a deleted file is a layer nobody can cover -- exactly what "
            "`geographic_change.py` did to Story 37.5 between 2026-08-17 and 2026-08-22."
        )
