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

24.4 flip (AC6, DEFERRED): the goal is ``_LEGACY_DEBT == {}`` once the module
mart-read helpers migrate to ``warehouse_tenancy.mart_prefix(project_id)``. That
module edit is blocked by the epic-25 parallel-session lock on server/modules/**
(story 25.9 is actively editing several of these files). The debt below is
therefore FROZEN, not vacated -- the dbt-side naming point IS enforced (the
second test), and the Python-side vacate is a T7 follow-up documented in the
24.4 story Completion. Invariant preserved: the allowlist may only shrink.
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
#: DuckDB branch) now routes through ``warehouse_tenancy.mart_prefix(None)`` --
#: the single Python naming point. Byte-identical output under the flag OFF
#: default; the ``project_id`` threading is a flip-time refinement (24.4). The
#: epic invariant "single naming point" is reached and the allowlist is empty
#: (invariant: shrink, never grow -- it may never be re-populated).
_LEGACY_DEBT: dict[str, int] = {}

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
