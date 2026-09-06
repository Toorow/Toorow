"""Static guard (Story 24.1/24.4, AC3/AC6): warehouse schema naming has ONE home.

Python side: no file under server/core/ or server/modules/ may compose a marts
schema name inline (quoted ``main_marts.`` literal or ``f"marts_{`` interpolation
or ``org_<...>_(raw|marts)``) -- ``core/warehouse_tenancy.py`` is the single naming
point (same discipline the 23.1 guard enforces for color constants).

dbt side (Story 24.4): the ``org_<wslug>_<custom>`` / ``org_<wslug>_raw``
composition lives ONLY in two macros -- ``dbt/macros/generate_schema_name.sql``
(schemas dbt WRITES) and ``dbt/macros/raw_source_schema.sql`` (raw_* source
schema). No model, no schema.yml composes an org schema name: every module's
raw_* source declares ``schema: "{{ raw_source_schema() }}"`` and the marts land
via generate_schema_name. This test scans dbt for inline org composition and
allows it ONLY in those two macros.

24.4 flip (AC6): REACHED. ``_LEGACY_DEBT == {}`` since T7 drained it (bdd58d27,
2026-07-22 -- literal out of the 24 connectors) and the module mart-read helpers
now resolve ``warehouse_tenancy.mart_prefix(project_id)`` (2026-08-04, T7
second half). The third test below locks that second half: a connector that
reverts to ``mart_prefix(None)`` would read ``main_marts.`` under the flag ON and
return ZERO rows in silence -- the exact failure mode condition 3 of the epic-24
flip list names. Invariant preserved: the allowlist may only shrink, and it is
empty.
"""

from __future__ import annotations

import re
from pathlib import Path

# server/tests/core/ -> server/
_SERVER_ROOT = Path(__file__).resolve().parents[2]
# server/ -> repo root -> dbt/
_DBT_ROOT = _SERVER_ROOT.parent / "dbt"

_SCAN_DIRS = ("core", "modules")
_EXCLUDED_PARTS = {"tests", "seeds", "__pycache__"}
# dbt generates compiled copies under target/ and vendors packages under
# dbt_packages/ -- both are build artifacts, never source, and must not be scanned
# (they also mirror the very files we guard, which would create false positives).
_DBT_EXCLUDED_PARTS = {"target", "dbt_packages", "__pycache__"}

#: The single Python naming point -- the only file allowed to own these literals.
_NAMING_POINT = "core/warehouse_tenancy.py"

#: The two dbt naming points -- the only dbt files allowed to compose org_* schemas.
_DBT_NAMING_POINTS = {
    "macros/generate_schema_name.sql",
    "macros/raw_source_schema.sql",
}

#: Module-read debt: VACATED by T7 (epic-24 close). Every module connector.py
#: that read a mart table inline (``_get_mart_table`` / ``_get_semantic_view``
#: DuckDB branch) now routes through ``warehouse_tenancy.mart_prefix(project_id)``
#: -- the single Python naming point, THREADED. Byte-identical output under the
#: flag OFF default (mart_prefix ignores project_id when the flag is off). The
#: epic invariant "single naming point" is reached and the allowlist is empty
#: (invariant: shrink, never grow -- it may never be re-populated).
_LEGACY_DEBT: dict[str, int] = {}

#: T7 second half: the mart prefix must be resolved FOR A PROJECT. A literal
#: ``None`` argument is the silent-empty-read defect (see the third test).
_MART_PREFIX_NONE = re.compile(r"mart_prefix\(\s*None\s*\)")

_PATTERNS = (
    re.compile(r"""["']main_marts\."""),
    re.compile(r"""f["']marts_\{"""),
    # F-4 (Story 24.2): catch inline org-schema composition f"org_{...}_raw/marts"
    # or literal strings like "org_foo_raw" / "org_foo_marts".
    re.compile(r"""f["']org_\{.*?_(?:raw|marts)"""),
    re.compile(r"""["']org_\w+_(?:raw|marts)["']"""),
)

#: dbt-side patterns: Jinja composition of an org schema (``'org_' ~ ...`` or
#: ``org_{{ ... }}_raw/marts/staging``). Allowed ONLY in the two naming macros.
_DBT_PATTERNS = (
    re.compile(r"""['"]org_['"]\s*~"""),          # 'org_' ~ org  (Jinja concat)
    re.compile(r"""~\s*['"]_(?:raw|marts|staging)['"]"""),  # ~ '_raw' etc.
    re.compile(r"""org_\{\{.*?\}\}_(?:raw|marts|staging)"""),
    re.compile(r"""['"]org_\w+_(?:raw|marts|staging)['"]"""),
)


def _occurrences(text: str, patterns) -> int:
    return sum(len(p.findall(text)) for p in patterns)


#: The three populations this file reads, measured 2026-08-31. FLOORS, not
#: equalities: growth is welcome, collapse is not. Criterion 13 of
#: `docs/product-architecture/module-boundaries.md` -- all three tests below say
#: `assert not violations`, which is exactly what an empty scan says.
_SCANNED_PY_AT_2026_08_31 = 612
_SCANNED_CONNECTORS_AT_2026_08_31 = 39
_SCANNED_DBT_FILES_AT_2026_08_31 = 307


def test_the_three_scans_still_reach_the_product():
    """Each verdict below is worth exactly what its scan found. Name it here."""
    scanned_py = [
        path
        for scan_dir in _SCAN_DIRS
        for path in (_SERVER_ROOT / scan_dir).rglob("*.py")
        if not _EXCLUDED_PARTS.intersection(path.parts)
    ]
    connectors = [
        path
        for path in (_SERVER_ROOT / "modules").rglob("connector.py")
        if not _EXCLUDED_PARTS.intersection(path.parts)
    ]
    dbt_files = [
        path
        for root in [_DBT_ROOT, *(_SERVER_ROOT / "modules").glob("*/dbt")]
        for path in [*root.rglob("*.sql"), *root.rglob("*.yml")]
        if path.is_file() and not _DBT_EXCLUDED_PARTS.intersection(path.parts)
    ]
    assert len(scanned_py) >= _SCANNED_PY_AT_2026_08_31, (
        f"{len(scanned_py)} python files scanned under {_SCAN_DIRS}, "
        f"{_SCANNED_PY_AT_2026_08_31} on 2026-08-31 -- the tree moved and the "
        "inline-naming verdict below is about nothing."
    )
    assert len(connectors) >= _SCANNED_CONNECTORS_AT_2026_08_31, (
        f"{len(connectors)} connector.py found, "
        f"{_SCANNED_CONNECTORS_AT_2026_08_31} on 2026-08-31 -- the mart_prefix "
        "verdict below covers a population that shrank."
    )
    assert len(dbt_files) >= _SCANNED_DBT_FILES_AT_2026_08_31, (
        f"{len(dbt_files)} dbt sql/yml files found, "
        f"{_SCANNED_DBT_FILES_AT_2026_08_31} on 2026-08-31 -- the org-schema "
        "composition verdict below is about nothing."
    )


def test_no_inline_marts_schema_naming_outside_the_naming_point():
    violations: list[str] = []
    for scan_dir in _SCAN_DIRS:
        for path in sorted((_SERVER_ROOT / scan_dir).rglob("*.py")):
            if _EXCLUDED_PARTS.intersection(path.parts):
                continue
            rel = path.relative_to(_SERVER_ROOT).as_posix()
            count = _occurrences(
                path.read_text(encoding="utf-8", errors="replace"), _PATTERNS
            )
            if count == 0:
                continue
            if rel == _NAMING_POINT:
                continue
            if count <= _LEGACY_DEBT.get(rel, 0):
                continue  # frozen debt, not growing
            violations.append(f"{rel}: {count} inline marts naming occurrence(s)")

    assert not violations, (
        "Warehouse schema names must come from core/warehouse_tenancy.py "
        "(story 24.1). New inline naming found:\n  " + "\n  ".join(violations)
        + "\nIf this is the 24.x module migration, shrink _LEGACY_DEBT instead."
    )


def test_module_connectors_resolve_the_mart_prefix_for_a_project():
    """T7 (epic-24 flip condition 3): no connector may resolve an UNSCOPED prefix.

    ``mart_prefix(None)`` returns the legacy ``main_marts.`` even under
    ``TOOROW_ORG_SCHEMAS=1`` (warehouse_tenancy degrades + warns once). Under the
    org topology ``main_marts`` does not exist, so a connector that kept the
    ``None`` argument would read an absent schema and return ZERO rows WITHOUT
    an error -- the silent-empty-read the epic-24 flip list names by hand.

    Scope: ``server/modules/`` only. Two ``mart_prefix(None)`` calls live in
    ``core/cache_warehouse.py`` and are DELIBERATE -- they sit on the explicit
    ``if not org_schemas_enabled()`` branch (the cache export fans out over
    ``list_active_orgs()`` when the flag is ON, story 24.4 AC5). A connector has
    no such branch: it reads one project's marts, so it must name that project.
    """
    violations: list[str] = []
    for path in sorted((_SERVER_ROOT / "modules").rglob("connector.py")):
        if _EXCLUDED_PARTS.intersection(path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        count = len(_MART_PREFIX_NONE.findall(text))
        if count:
            rel = path.relative_to(_SERVER_ROOT).as_posix()
            violations.append(f"{rel}: {count} unscoped mart_prefix(None) call(s)")

    assert not violations, (
        "A connector must resolve its mart prefix FOR A PROJECT "
        "(warehouse_tenancy.mart_prefix(project_id), story 24.1/T7). "
        "mart_prefix(None) reads main_marts. under the org topology -> zero rows, "
        "no error:\n  " + "\n  ".join(violations)
    )


def test_no_inline_org_schema_composition_in_dbt_outside_the_naming_macros():
    """Story 24.4 (AC1/AC2): dbt composes org_* ONLY in the two naming macros.

    Scans dbt/**/*.sql and dbt/**/*.yml for Jinja/literal org-schema composition;
    the only allowed origins are generate_schema_name.sql and raw_source_schema.sql
    (twin of warehouse_tenancy.OrgSchemas on the Python side). A model or schema.yml
    that inlines ``org_<...>_raw/marts/staging`` fails this guard.
    """
    if not _DBT_ROOT.exists():  # pragma: no cover -- dbt tree always present in repo
        return
    # Review 24.4 F-1: the module dbt trees (server/modules/**/dbt/) are where
    # T3 will wire raw_source_schema() -- they must be under the guard too, or
    # an inline org_* in a module schema.yml would escape the naming point.
    module_dbt_roots = sorted((_SERVER_ROOT / "modules").glob("*/dbt"))
    scan_roots = [_DBT_ROOT, *module_dbt_roots]
    violations: list[str] = []
    scan_files: list[tuple[Path, Path]] = []
    for root in scan_roots:
        for path in sorted([*root.rglob("*.sql"), *root.rglob("*.yml")]):
            scan_files.append((root, path))
    for root, path in scan_files:
        if _DBT_EXCLUDED_PARTS.intersection(path.parts):
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if root is _DBT_ROOT and rel in _DBT_NAMING_POINTS:
            continue
        rel = rel if root is _DBT_ROOT else path.relative_to(_SERVER_ROOT).as_posix()
        count = _occurrences(
            path.read_text(encoding="utf-8", errors="replace"), _DBT_PATTERNS
        )
        if count:
            violations.append(f"dbt/{rel}: {count} inline org-schema composition(s)")

    assert not violations, (
        "dbt org schema names must come from generate_schema_name.sql / "
        "raw_source_schema.sql (story 24.4). Inline composition found:\n  "
        + "\n  ".join(violations)
    )
