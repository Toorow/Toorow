"""A teardown does not write its own list of DELETEs (AI-291).

WHY. Twenty-six test files ended their fixture with `DELETE FROM app.projects`,
each preceded by a hand-written list of child deletes. A hand-written list loses
the race: measured on ONE of them, two tables added the same day held the Project
by `ON DELETE RESTRICT` -- `project_capabilities` (migration 243, seeded for
every Project) and then `mdm_business_domains`. Each was invisible until the
previous one was removed, and the whole failure mode is a SKIP: without a
Postgres DSN the fixture skips, so nobody sees the teardown break.

`tests.conftest.purge_fixture_project` removes the Project in ONE statement in
the common case, and falls back to the foreign-key graph production itself walks
the moment a blocking edge refuses. The cost is paid the day it buys something.

WHAT THIS GUARD ASKS. No file under `server/tests` may issue a bare
`DELETE FROM app.projects` unless it is listed below WITH ITS REASON. The list is
inventory, not exemption: every entry says why the helper does not fit, so an
entry that stops being true is visible instead of silent.

THE GUARD IS STATIC on purpose. The defect hides behind a skip, so a check that
also needs a DSN would hide in the same place.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
TESTS = ROOT / "server" / "tests"

#: `[ 	]+` and not `\s+`: the statement is written on ONE line everywhere it
#: is executed, while the same words wrapped across two lines are prose --
#: `test_fee_tax_rules.py` explains in its docstring why a raw delete fails,
#: and a guard that cannot tell an explanation from a statement forces the
#: explanation out of the file.
_DELETE = re.compile(r"DELETE[ 	]+FROM[ 	]+app\.projects", re.IGNORECASE)

#: Files that delete Projects by hand, each with the reason the helper does not
#: fit. `purge_fixture_project` takes ONE project id; none of these has one.
ALLOWED: dict[str, str] = {
    "conftest.py": (
        "It IS the helper. Its two statements are the fast path and the tail of "
        "the graph fallback."
    ),
    "integration/test_org_purge_end_to_end_pg.py": (
        "The bare DELETE IS the subject (migration 338): the project-rooted plan "
        "is replayed WITHOUT its context_graph statement and the final DELETE "
        "FROM app.projects must be refused BY NAME by context_graph_project_id_fkey "
        "-- the pre-338 world reproduced exactly. purge_fixture_project would "
        "erase the evidence the test exists to read."
    ),
    "core/test_context_seed.py": (
        "A multi-line literal built from a list of ids, not a single-project "
        "teardown."
    ),
    "core/test_google_oauth.py": (
        "The statement is a (sql, params) PAIR in a table of cleanup steps, not "
        "a call site."
    ),
    "core/test_google_token_store.py": "Same table-of-steps shape as test_google_oauth.",
    "core/test_token_service.py": "Same table-of-steps shape as test_google_oauth.",
    "core/test_org_enforcement.py": (
        "The match is PROSE in a docstring explaining why the delete could only "
        "ever succeed on an empty Project -- not a statement."
    ),
    "core/test_projects_api.py": (
        "Prose again, in the docstring that records the IN-doubt failure this "
        "helper exists to remove."
    ),
    "integration/test_admin_api.py": (
        "The statement is split across lines by the formatter; converting it "
        "would be a rewrite this guard cannot verify, so it stays and is named."
    ),
    "integration/test_projects_constraints.py": (
        "Deletes by SLUG, not by id: the fixture does not know the id it is "
        "removing."
    ),
    "isolation/conftest.py": (
        "`WHERE id = ANY(%s)` over the whole isolation fixture set, in one "
        "statement."
    ),
}


def _offending_files() -> list[str]:
    found: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        # This file quotes the statement it forbids; excluding it by name is
        # narrower than teaching the regex to ignore its own source.
        if path.name == pathlib.Path(__file__).name:
            continue
        if not _DELETE.search(path.read_text(encoding="utf-8", errors="replace")):
            continue
        found.append(path.relative_to(TESTS).as_posix())
    return found


def test_the_guard_still_sees_something() -> None:
    """Calibration: the allowlist must describe files that exist.

    An entry naming a file nobody has any more is an exemption outliving its
    reason -- the shape this repository keeps finding in guards with a fixed
    numerator.
    """
    present = set(_offending_files())
    stale = sorted(set(ALLOWED) - present)
    assert not stale, f"ALLOWED names files that no longer delete a Project: {stale}"


def test_no_new_fixture_deletes_a_project_by_hand() -> None:
    offenders = [
        f"server/tests/{name}: writes `DELETE FROM app.projects` by hand. Use "
        f"`tests.conftest.purge_fixture_project`, or add an entry to ALLOWED "
        f"saying why it does not fit."
        for name in _offending_files()
        if name not in ALLOWED
    ]
    assert not offenders, "\n".join(offenders)


def test_every_allowance_carries_a_reason() -> None:
    """An exemption with no reason is how a real gap gets filed as normal."""
    empty = sorted(name for name, reason in ALLOWED.items() if len(reason.strip()) < 20)
    assert not empty, empty
