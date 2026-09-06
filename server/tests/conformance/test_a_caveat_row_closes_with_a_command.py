"""A row of the caveats register may not close on a hash, and its command must resolve.

WHY THIS FILE EXISTS. `docs/product-architecture/caveats-register.md` ends with a
bullet it wrote about itself: *"a row in the register above is closed without the
command that proves it"*. That bullet had no instrument. It was enforced twice by
hand -- the amendment of 2026-08-31 re-derived nine rows (CAV-01, 03, 05, 06, 12,
13, 14, 15, 18) that cited a commit and nothing runnable -- and a rule enforced by
a sweep is a rule that lapses between sweeps: six further rows (CAV-04, 07, 08,
10, 16, 20) were still closed on prose the day after, and nothing said so.

So the bullet becomes a derivation. The register is parsed, every row that claims
a repair is required to carry a command someone can run, and the guard prints its
own census -- the shape of `test_rollup_call_sites_consult_the_gate.py`, one
directory over.

THE SECOND ASSERTION IS THE ONE THAT COST SOMETHING TO LEARN. A command is not
proof merely by existing: it has to still mean what the sentence beside it says.
Measured on 2026-09-02, CAV-08 closed on `grep -rn "?? FIXTURE_ENVELOPE"
ui/cards/*/src/main.tsx` -> 9, published as nine live fixture fallbacks. AI-271
had removed all nine; the nine hits that remain are the COMMENTS recording the
removal. The number was identical and its meaning had inverted -- which is this
register's own subject, committed inside the repair that denounced it. A parser
cannot judge a sentence, but it can hold the cheapest half of the question: every
path a command names must be a path this repository has. A suite deleted under a
row that still cites it is the next form of the same rot.

WHAT THIS GUARD DOES NOT DO, said rather than implied: it does not run the
commands. Running 23 rows' suites here would make the conformance sweep minutes
long and would duplicate the suites' own job. It asks that the row be
RE-CHECKABLE by a person in one paste, which is exactly what a commit hash never
was.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REGISTER = _REPO_ROOT / "docs" / "product-architecture" / "caveats-register.md"

#: Cells are separated by unescaped pipes: `\|` appears inside CAV-13 (`\|z\|`).
_CELL = re.compile(r"(?<!\\)\|")
#: A command is always written as inline code in this document.
_CODE = re.compile(r"`([^`]+)`")
#: The runners this repository actually offers a reader. `grep` is included on
#: purpose: CAV-11 closes on the absence of a mounted route, and no test can
#: assert an absence more directly than the grep that returns one line.
_RUNNERS = ("pytest", "vitest", "python -c", "python scripts/", "dbt build", "grep ")
#: A row that has NOT been repaired owes no command -- it owes an owner. None
#: exist today; the register is 23 rows and every one claims a repair.
_NOT_CLOSED = ("OPEN", "STILL LIVE", "not repaired,")
#: Where a command's relative paths resolve from.
_CHDIR = (
    ("cd server", "server"),
    ("cd ui/admin", "ui/admin"),
    ("cd ui/cards", "ui/cards"),
)
_PATHLIKE = re.compile(r"[\w./\\-]+\.(?:py|tsx|ts|sql|mdx)\b")


class _Row:
    __slots__ = ("caveat_id", "state", "line", "commands")

    def __init__(self, caveat_id: str, state: str, line: str) -> None:
        self.caveat_id = caveat_id
        self.state = state
        self.line = line
        # The whole ROW, not the State cell: the register puts a measurement
        # beside the sentence it corrects, and CAV-21 carries both of its
        # commands in the cell that states what makes the belief false.
        self.commands = [
            code for code in _CODE.findall(line) if any(runner in code for runner in _RUNNERS)
        ]

    @property
    def claims_a_repair(self) -> bool:
        head = self.state[:40]
        return not any(marker in head for marker in _NOT_CLOSED)

    def __repr__(self) -> str:  # pragma: no cover - failure messages only
        return f"{self.caveat_id} ({len(self.commands)} command(s))"


def _rows(text: str) -> list[_Row]:
    rows: list[_Row] = []
    for line in text.splitlines():
        if not line.startswith("| CAV-"):
            continue
        cells = [cell.strip() for cell in _CELL.split(line)]
        # ['', id, surface, belief, what makes it false, doc, state, owner, '']
        rows.append(_Row(cells[1], cells[6], line))
    return rows


def _base_for(command: str) -> Path:
    for prefix, relative in _CHDIR:
        if command.startswith(prefix):
            return _REPO_ROOT / relative
    return _REPO_ROOT


def _named_paths(command: str) -> list[str]:
    """The file paths a command names, ignoring globs the shell expands itself.

    A glob is skipped whole: `ui/cards/*/src/main.tsx` names nine files and none
    of them is `/src/main.tsx`, which is what a naive tail match produces.
    """
    named: list[str] = []
    for match in _PATHLIKE.finditer(command):
        token = match.group(0)
        before = command[: match.start()]
        if "*" in token or before.endswith("*"):
            continue
        if token.startswith("-"):
            continue
        named.append(token)
    return named


@pytest.fixture(scope="module")
def register_rows() -> list[_Row]:
    assert _REGISTER.exists(), f"the register moved: {_REGISTER}"
    return _rows(_REGISTER.read_text(encoding="utf-8"))


def test_every_row_that_claims_a_repair_carries_a_runnable_command(register_rows):
    """The last `Incomplete if` bullet of the register, derived."""
    naked = [row.caveat_id for row in register_rows if row.claims_a_repair and not row.commands]
    assert not naked, (
        "these rows of docs/product-architecture/caveats-register.md claim a "
        "repair and carry no command anyone can run:\n  " + "\n  ".join(naked) + "\n\n"
        "A commit hash says a change landed once; it cannot say whether the "
        "property still holds today, and this register carries the receipt for "
        "what that costs (CAV-21). Re-derive the row to the command that would "
        "go red if the caveat came back, RUN it, and write it in the row with "
        "its count."
    )


def test_every_command_in_the_register_names_a_path_this_repository_has(register_rows):
    """A command that points at a deleted suite is a hash with extra steps."""
    unreachable: list[str] = []
    for row in register_rows:
        for command in row.commands:
            base = _base_for(command)
            for token in _named_paths(command):
                if not (base / token).exists():
                    unreachable.append(f"{row.caveat_id}: {token}  (from: {command})")
    assert not unreachable, (
        "a caveat row cites a command whose target this repository no longer "
        "has:\n  " + "\n  ".join(unreachable) + "\n\n"
        "Repair the row against the suite that carries the property today — "
        "never delete the command, which would return the row to the hash it "
        "was rescued from."
    )


def test_a_row_closed_on_a_hash_alone_is_named():
    """The mutation proof, on a synthetic register rather than the real one.

    Against a throwaway text: a guard whose only demonstration is the tree it
    guards is a guard that has never been shown to fail. Two rows, identical but
    for the command, and only one may be reported.
    """
    header = "| ID | Surface | Belief | Why false | Doc | State | Owner |\n"
    proven = (
        "| CAV-90 | S | b | w | d | **repaired** (`deadbee`): "
        "`cd server && python -m pytest tests/conformance/"
        "test_a_caveat_row_closes_with_a_command.py -q` → 6 passed | — |\n"
    )
    hashed = "| CAV-91 | S | b | w | d | **repaired** (`deadbee`) | — |\n"
    rows = _rows(header + proven + hashed)

    assert [row.caveat_id for row in rows] == ["CAV-90", "CAV-91"]
    naked = [row.caveat_id for row in rows if row.claims_a_repair and not row.commands]
    assert naked == ["CAV-91"], naked


def test_a_row_that_is_still_open_owes_an_owner_and_not_a_command():
    """The other direction, and it is not symmetry for its own sake.

    A guard that demanded a command of every row would push an honest OPEN row
    into inventing one — the failure this register exists to catch, one floor up.
    """
    header = "| ID | Surface | Belief | Why false | Doc | State | Owner |\n"
    still_open = "| CAV-92 | S | b | w | d | **OPEN** — nothing protects it | 53.9 |\n"
    (row,) = _rows(header + still_open)
    assert not row.claims_a_repair
    assert not row.commands


def test_the_derivation_really_read_the_register(register_rows):
    """A floor, because a parser that matched nothing reports a clean sheet.

    The figures on 2026-09-02 were 23 rows, 23 claiming a repair, 23 carrying at
    least one command. Held as floors, not equalities: the register grows.
    """
    assert len(register_rows) >= 23, register_rows
    assert all(row.claims_a_repair for row in register_rows), [
        row.caveat_id for row in register_rows if not row.claims_a_repair
    ]
    commands = [command for row in register_rows for command in row.commands]
    assert len(commands) >= 23, commands
    # And the commands are not one runner counted many times: a register whose
    # only proof was `grep` would satisfy the bullet and prove no behaviour.
    assert any("pytest" in command for command in commands)
    assert any("vitest" in command for command in commands)


# ---------------------------------------------------------------------------
# The two rows whose property no suite carried at all (2026-09-02).
#
# CAV-16 and CAV-10 were repaired in the SCHEMA and in a DOCUMENT — there was no
# behaviour to name a test after, which is exactly how they stayed closed on
# prose for a month. Both are derived from the repository here rather than
# asserted, so the row's command is this file.
# ---------------------------------------------------------------------------


_MIGRATIONS = _REPO_ROOT / "infra" / "nango" / "migrations"


def _migration_text(prefix: str) -> str:
    (path,) = sorted(_MIGRATIONS.glob(f"{prefix}_*.sql"))
    return path.read_text(encoding="utf-8")


def test_the_exposure_grant_is_organization_scoped_in_the_schema_and_in_the_glossary():
    """CAV-16: exposing an account does not scope it to a Project.

    The belief the surface induced was Project granularity. The repair was a
    sentence in the glossary, and a sentence is only true while the schema
    underneath it stays that way — so both halves are read, and the schema half
    is read over EVERY migration that touches the table rather than over the one
    the glossary happens to cite.
    """
    touching = sorted(
        path
        for path in _MIGRATIONS.glob("*.sql")
        if "credential_account_grants" in path.read_text(encoding="utf-8")
    )
    assert [path.name.split("_")[0] for path in touching] == ["037", "059"], touching

    for path in touching:
        body = path.read_text(encoding="utf-8")
        statements = [
            line
            for line in body.splitlines()
            if "project" in line.lower() and "credential_account_grants" in line.lower()
        ]
        assert not statements, (
            f"{path.name} gives app.credential_account_grants a project: the grant "
            "became Project-scoped and the glossary entry CAV-16 repaired now "
            "understates it.\n  " + "\n  ".join(statements)
        )
    grants = _migration_text("037")
    assert "grantee_org_id" in grants
    assert "UNIQUE (credential_id, external_account_id, grantee_org_id)" in grants

    glossary = (_REPO_ROOT / "docs" / "product-architecture" / "glossary.md").read_text(
        encoding="utf-8"
    )
    assert "**The exposure grant is organization-scoped, not project-scoped.**" in glossary, (
        "the Source Account entry of glossary.md no longer states the granularity "
        "the grant actually has. CAV-16 closed on that sentence; without it the "
        "row is closed on nothing."
    )


def test_a_render_share_is_declared_with_a_mandatory_expiry():
    """CAV-10: a share link expires, and the column that makes that true exists.

    `054_render_snapshot_shares.sql` had no expiry column at all while
    `README.md:78` ratified "expiring". Story 50.7 replaced the table; what is
    pinned here is the property, on the migration that carries it and on every
    migration after it — a later `DROP COLUMN expires_at` would return the row to
    the day it was written.
    """
    body = _migration_text("162")
    assert "expires_at                TIMESTAMPTZ NOT NULL" in body, (
        "app.render_shares no longer declares expires_at NOT NULL"
    )
    assert "CHECK (expires_at > created_at)" in body, (
        "the expiry is nullable in practice again: nothing refuses an expiry at or before creation"
    )

    dropped = []
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        if path.name.startswith("162_"):
            continue
        body = path.read_text(encoding="utf-8")
        if re.search(r"DROP\s+COLUMN\s+(IF\s+EXISTS\s+)?expires_at", body, re.I):
            dropped.append(path.name)
        if re.search(r"ALTER\s+COLUMN\s+expires_at\s+DROP\s+NOT\s+NULL", body, re.I):
            dropped.append(path.name)
    assert not dropped, "a later migration takes the expiry off a share: " + ", ".join(dropped)

    # And the bearer never lands beside it in plaintext, which is the half story
    # 50.7's own analysis added to this row.
    assert "bearer_hash" in _migration_text("162")
