"""Two guards on the pg-gated harness itself, both posted on a measurement.

WHY THIS FILE EXISTS. Review de contrôle 2026-07-31 (C-5) said of Story 49.4
that "toute son evidence passe par une suite pg-gated" that did not execute --
the cited command answered SKIPPED. Run for the first time on 2026-08-16
against a disposable PostgreSQL with every migration applied, connected as the
DEPLOYED APPLICATION ROLE, that family did not skip: **32 tests died in their
fixtures**, all on one cause, having asserted nothing.

A suite that skips hides its own defects, and both defects below are of the kind
that only a real run can show. So they get guards, textual and cheap, that hold
without a database.

GUARD 1 -- a DDL fixture declares the role it needs.
`conftest._enforce_declared_role` already offers `pg_owner` for a test whose
fixture does DDL: as a plain role it dies on "must be owner of table ...", and
the marker turns that death into an honest skip naming the DSN to point at. The
32 did DDL and declared nothing. This pins the markers they were given, on the
files where the deaths were measured -- see `_REPAIRED_FILES` for why the guard
holds a measured list rather than a rule read off the source.

GUARD 2 -- nobody re-copies `CREATE OR REPLACE FUNCTION app.set_updated_at()`.
Five files carried that statement byte for byte. `CREATE OR REPLACE` is not a
conditional create: on a migrated database it demands ownership of the function,
so it was the first wall all 30 hit. `tests.support.updated_at_trigger` asks
before creating, and it is now the only place that statement may live.
"""

from __future__ import annotations

import re
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_SUPPORT_OWNER = _TESTS_ROOT / "support" / "updated_at_trigger.py"

#: The call that replays a migration, inside a test body or its bootstrap.
_REPLAY_CALL_RE = re.compile(r"_(?:bootstrap|apply_migration)\w*\(", re.M)

#: The files whose DDL fixtures were MEASURED to die as `connector` on
#: 2026-08-16, and repaired by declaring the role. Guard 1 holds exactly these.
#:
#: Why a measured list and not "every file that replays a migration": that wider
#: rule is not decidable by reading. `test_org_entitlements.py` replays migration
#: 056, which carries seven ownership-requiring statements, and it passes as
#: `connector` -- demanding the marker there would skip nine green tests for a
#: wall they do not hit. Whether the replay touches an object the role does not
#: own is answered by running, so the list is what running answered.
_REPAIRED_FILES = (
    "core/test_async_reports.py",
    "core/test_dimension_conformance.py",
    "core/test_dimension_lineage.py",
    "core/test_metric_reconciliation.py",
    "core/test_metric_semantics.py",
    "core/test_metric_semantics_monetary.py",
    #  Mesure du 2026-08-31, meme mur, autre famille : les quatre tests vivants de
    #  ce fichier rejouent 030/032/042 et mouraient sur << doit etre le
    #  proprietaire de la table datastreams >> sous le role applicatif.
    #  `conftest._module_needs_owner` ne pouvait pas le deriver : il lit la SOURCE
    #  du fichier, et les instructions qui exigent la propriete sont dans les
    #  migrations que ce fichier se contente de nommer.
    "integration/test_datastream_publication_constraints.py",
)


def _test_blocks(source: str) -> list[tuple[str, str, str]]:
    """Every top-level test as `(name, its decorators, its body)`.

    Line-based on purpose. A regex split on `^@` gives every decorator a block
    of its own, so a test carrying `@pg_available` above `@pytest.mark.pg_owner`
    reads as undeclared -- which is how the first version of this guard reported
    thirty tests that DO declare the marker.
    """
    lines = source.split("\n")
    blocks: list[tuple[str, str, str]] = []
    for i, line in enumerate(lines):
        match = re.match(r"def (test_\w+)\(", line)
        if not match:
            continue
        start = i
        while start > 0 and lines[start - 1].startswith("@"):
            start -= 1
        end = i + 1
        while end < len(lines) and not re.match(r"^(@\w|def |class )", lines[end]):
            end += 1
        blocks.append((match.group(1), "\n".join(lines[start:i]), "\n".join(lines[i:end])))
    return blocks


def _test_files() -> list[Path]:
    return [
        p
        for p in _TESTS_ROOT.rglob("test_*.py")
        if "__pycache__" not in p.parts and p != Path(__file__).resolve()
    ]


def test_only_the_support_module_creates_the_updated_at_trigger():
    """Guard 2: one owner for the statement, so one place asks before creating."""
    offenders = []
    for path in _test_files():
        source = path.read_text(encoding="utf-8")
        if "CREATE OR REPLACE FUNCTION app.set_updated_at" in source:
            offenders.append(path.relative_to(_TESTS_ROOT).as_posix())
    assert not offenders, (
        "CREATE OR REPLACE demands ownership of the function on a migrated "
        "database, so these fixtures cannot run as the deployed application "
        "role. Call tests.support.updated_at_trigger.ensure_set_updated_at "
        f"instead: {offenders}"
    )
    assert _SUPPORT_OWNER.exists()
    assert "CREATE FUNCTION app.set_updated_at" in _SUPPORT_OWNER.read_text(encoding="utf-8")


def test_the_repaired_ddl_fixtures_keep_declaring_pg_owner():
    """Guard 1: the 32 tests measured red as `connector` keep saying so.

    Read on the test FUNCTION, not on the file: these files mix tests that
    replay DDL with tests that must stay runnable as the application role, and a
    file-wide marker would skip the second family for a wall that is not theirs.
    """
    undeclared: list[str] = []
    for relative in _REPAIRED_FILES:
        path = _TESTS_ROOT / relative
        assert path.exists(), f"{relative} was moved or removed; re-measure before dropping it here"
        source = path.read_text(encoding="utf-8")
        for name, decorators, body in _test_blocks(source):
            if not _REPLAY_CALL_RE.search(body):
                continue
            if "@pytest.mark.pg_owner" not in decorators:
                undeclared.append(f"{relative}::{name}")
    assert not undeclared, (
        "these tests replay a migration in their fixture and will die on "
        "'must be owner of table ...' as the deployed application role. Add "
        f"@pytest.mark.pg_owner so they skip with their reason instead: {undeclared}"
    )
