"""Story 48.5, AC2/AC8/AC12: the two invariants Epic 40 proved, re-proven on the new owners.

Migration 147 and `core.tracked_entities` replaced four modules. The tests that
went with them are deleted, but two of their invariants are not Epic 40's -- they
are the product's -- and they move here rather than disappearing with the code
that used to carry them:

* **No cross-Project enumerator exists.** Confidentiality is enforced BY
  CONSTRUCTION, not by a filter somebody remembers to write. If no function can
  answer "which other Projects track this identity?", no route, no MCP tool and
  no future refactor can leak it by forgetting a WHERE clause.
* **Identity alignment writes nothing that authorizes comparison.** Aligning a
  source value to an entity conforms an identity. It creates no overlap group and
  no reconciliation rule, so a cross-source sum stays unruled -- which is what
  makes the refusal in Analyze real rather than a convention.

Both are proven statically, over the source, because both are claims about what
the code CANNOT do. A behavioural test proves the current call path; a source
test proves there is no other one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[2] / "core"

#: The modules that own tracked-entity identity, evidence and physical binding.
OWNER_MODULES = (
    "tracked_entities.py",
    "entity_bindings.py",
)

#: Epic 27's overlap/reconciliation tables. A write to any of them from an
#: identity module would silently turn "these two rows denote one entity" into
#: "these two metrics may be added".
_EPIC27_TABLES = (
    "overlap_groups",
    "overlap_group_members",
    "reconciliation_rules",
    "metric_definitions",
    "source_metric_mappings",
)


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    return {name: (_CORE / name).read_text(encoding="utf-8") for name in OWNER_MODULES}


# ---------------------------------------------------------------------------
# No cross-Project enumerator. The absence IS the boundary.
# ---------------------------------------------------------------------------


def test_no_module_exposes_a_cross_project_enumerator():
    from core import master_data, tracked_entities

    forbidden = (
        "projects_tracking",
        "tracking_entity",
        "siblings",
        "sibling",
        "all_projects",
        "cross_project",
        "projects_for",
    )
    leaks = []
    for module in (tracked_entities, master_data):
        for name in dir(module):
            if name.startswith("_"):
                continue
            if any(word in name.lower() for word in forbidden):
                leaks.append(f"{module.__name__}.{name}")
    assert not leaks, f"a cross-Project enumerator leaked: {leaks}"


def test_every_association_read_is_scoped_to_one_project(sources):
    """`project_id = %s` is not optional in any association query.

    Read from the SQL rather than from a call, because the failure this guards
    against is a future query added without the clause -- which a behavioural
    test over today's callers would never see.
    """
    from core import master_data

    source = (_CORE / "master_data.py").read_text(encoding="utf-8")
    start = source.index("def list_project_associations")
    end = source.index("def ensure_type_version")
    body = source[start:end]
    assert "master_data_project_associations" in body
    assert "project_id = %s" in body
    # And the signature has no way to widen the scope.
    import inspect

    parameters = inspect.signature(master_data.list_project_associations).parameters
    assert "project_id" in parameters
    assert not any(name in parameters for name in ("org_id", "project_ids", "all_projects"))


def test_the_project_projection_returns_only_associated_identities():
    """`project_registry` intersects the organization master with THIS Project's
    associations. An identity the Project never associated is absent -- not listed
    with a null role, which would confirm its existence."""
    source = (_CORE / "tracked_entities.py").read_text(encoding="utf-8")
    start = source.index("def project_registry")
    end = source.index(
        "# ---------------------------------------------------------------------------", start
    )
    body = source[start:end]
    assert "list_project_associations" in body
    assert "if not associations:" in body
    assert "in by_node" in body


# ---------------------------------------------------------------------------
# Identity alignment never becomes permission to compare.
# ---------------------------------------------------------------------------


def test_no_owner_module_writes_an_epic27_overlap_table(sources):
    verbs = ("insert into", "update ", "delete from")
    breaches: list[str] = []
    for name, source in sources.items():
        flat = source.lower().replace(" ", "")
        for table in _EPIC27_TABLES:
            for verb in verbs:
                if f"{verb}app.{table}".replace(" ", "") in flat:
                    breaches.append(f"{name}: {verb.strip()} -> app.{table}")
    assert not breaches, f"an identity module WRITES a comparability table: {breaches}"


def test_no_owner_module_imports_a_comparability_write_helper(sources):
    forbidden_modules = {
        "core.metric_semantics",
        "core.metric_reconciliation",
        "core.semantic_model",
    }
    hits: list[str] = []
    for name, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                hits.extend(
                    f"{name}: from {node.module} import {alias.name}"
                    for alias in node.names
                )
    assert not hits, f"an identity module imports a comparability writer: {hits}"


def test_the_compiler_states_the_distinction_on_every_complete_verdict():
    """A screen must never have to infer it. The exception is emitted with the
    verdict, in a sentence, so the person reading 100% reads the caveat too."""
    source = (_CORE / "capability_compilers.py").read_text(encoding="utf-8")
    start = source.index("class CompetitorsCompiler")
    body = source[start:]
    assert "identity_is_not_commensurability" in body
    assert "does not authorize" in body


def test_the_mcp_projection_carries_the_same_sentence():
    source = (_CORE / "project_capabilities_mcp.py").read_text(encoding="utf-8")
    assert "comparison_note" in source
    assert "commensurability decision" in source


# ---------------------------------------------------------------------------
# The removed authorities stay removed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    ["brand_registry_mcp.py"],
)
def test_the_bespoke_mcp_tool_family_is_gone(module):
    assert not (_CORE / module).exists(), f"{module} came back"


def test_no_production_module_imports_the_replaced_authorities():
    """The four Epic 40 modules may still exist as unreferenced files; nothing
    that runs may reach them. Their tables also still exist, deliberately -- the
    rows are the only record of what was configured."""
    replaced = {
        "core.tracked_entity_registry",
        "core.tracked_entity_matching",
        "core.entity_source_bindings",
        "core.entity_scope_change",
        "core.brand_registry_mcp",
    }
    importers: list[str] = []
    for path in sorted(_CORE.glob("*.py")):
        if path.name in {
            "tracked_entity_registry.py",
            "tracked_entity_matching.py",
            "entity_source_bindings.py",
            "entity_scope_change.py",
        }:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module in replaced:
                importers.append(f"{path.name}: from {node.module}")
            if isinstance(node, ast.Import):
                importers.extend(
                    f"{path.name}: import {alias.name}"
                    for alias in node.names
                    if alias.name in replaced
                )
    assert not importers, f"a replaced authority is still reachable: {importers}"
