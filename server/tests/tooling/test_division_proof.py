"""Criteria 5 and 11: a move reads as "this code moved", and the dump bites.

`docs/product-architecture/module-boundaries.md` carries the two criteria this
file pins:

    5.  A responsibility is moved and its call sites are updated by hand rather
        than through a seam, so the diff cannot be read as "this code moved".
    11. A division is claimed without a before/after behaviour dump compared
        entry for entry, or with an edit to a moved body in the same commit.

Six extractions were proven against those two sentences by hand, once each, with
a `python -c` nobody wrote down. `scripts/division_proof.py` is the command, and
this file is what stops it becoming a paragraph again.

THE FIRST TEST OF A GUARD IS TO SHOW IT A DEFECT IT SHOULD SEE. That lesson is
written twice in the document it serves -- once about the patch guard that let
nine targets through for two days, once about the quiet-guard classifier. So
every check here drives the instrument with a tree built for it: a clean move, a
move whose body was edited, a move that left a seam, a move whose name is
ambiguous, an artefact that would certify anything, and a router re-spliced one
position off. A census that only ever ran on today's tree would agree with
itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import division_proof  # noqa: E402

_GUARD = division_proof._load_guard()

_BODY = """
async def _create_project(request):
    scope = await _check_auth(request)
    if scope is None:
        return _refuse()
    return _ok(scope)
"""


def _tree(**files: str) -> dict[str, str]:
    return {path.replace("__", "/"): source for path, source in files.items()}


# --------------------------------------------------------------------------- #
# Criterion 5 -- the move census
# --------------------------------------------------------------------------- #


def test_a_body_that_moved_untouched_reads_as_this_code_moved():
    before = _tree(server__core__admin_api="import x\n" + _BODY)
    after = _tree(server__core__admin_api="import x\n", server__core__projects_api=_BODY)

    found, ambiguous = division_proof.moves(
        {f"{k}.py": v for k, v in before.items()}, {f"{k}.py": v for k, v in after.items()}
    )

    assert len(found) == 1, f"one move expected, got {[m.name for m in found]}"
    assert not ambiguous
    move = found[0]
    assert move.name == "_create_project"
    assert move.frm == "server/core/admin_api.py"
    assert move.to == "server/core/projects_api.py"
    assert move.body_identical is True
    assert move.verdict == "moved"


def test_a_body_edited_on_the_way_is_refused():
    """The defect the instrument exists to see: a move and a fix in one commit."""

    before = _tree(server__core__admin_api="import x\n" + _BODY)
    edited = _BODY.replace("return _ok(scope)", "return _ok(scope, cached=True)")
    after = _tree(server__core__admin_api="import x\n", server__core__projects_api=edited)

    found, _ = division_proof.moves(
        {f"{k}.py": v for k, v in before.items()}, {f"{k}.py": v for k, v in after.items()}
    )

    assert len(found) == 1
    assert found[0].body_identical is False, (
        "a body that changed on the way must not pass as a move -- mixing a "
        "refactoring with a fix makes both unverifiable"
    )
    assert found[0].verdict == "EDITED"


def test_a_decorator_that_changed_is_an_edit_too():
    """Decorators travel with the body: an arrival wearing another one is not a move."""

    before = _tree(server__core__admin_api="@requires_auth\n" + _BODY.strip() + "\n")
    after = _tree(
        server__core__admin_api="",
        server__core__projects_api="@requires_nothing\n" + _BODY.strip() + "\n",
    )

    found, _ = division_proof.moves(
        {f"{k}.py": v for k, v in before.items()}, {f"{k}.py": v for k, v in after.items()}
    )

    assert len(found) == 1
    assert found[0].body_identical is False


def test_a_name_that_left_two_modules_is_reported_rather_than_guessed():
    """`_list_notebooks` exists twice in `core`; a name-only match must not pick one.

    AD-43's fourth step measured what a by-name lookup costs -- it *"finds the
    right object by accident often enough to be dangerous"* and failed later with
    an unrelated `KeyError`.
    """

    body = "def _list_feedback():\n    return []\n"
    before = _tree(server__core__a=body, server__core__b=body)
    after = _tree(server__core__a="", server__core__b="", server__core__c=body)

    found, ambiguous = division_proof.moves(
        {f"{k}.py": v for k, v in before.items()}, {f"{k}.py": v for k, v in after.items()}
    )

    assert not found, "an ambiguous match must not be recorded as a move"
    assert len(ambiguous) == 1
    assert "_list_feedback" in ambiguous[0]


def test_a_facade_re_export_is_read_as_a_seam():
    """AD-41's shape: the module the readers name still resolves the name."""

    before = {"server/core/csv_excel_import.py": "def run_import():\n    return 1\n"}
    after = {
        "server/core/csv_excel_import.py": "from core.import_runner import run_import\n",
        "server/core/import_runner.py": "def run_import():\n    return 1\n",
    }

    found, _ = division_proof.moves(before, after)
    assert len(found) == 1
    division_proof.add_seam_and_patches(found, after, "HEAD", _GUARD)

    assert found[0].seam is True, (
        "the facade re-exports the name, so every reader still resolves it -- "
        "that is the seam AD-41 calls the compatibility contract"
    )
    assert found[0].dangling_patches == []


def test_a_move_with_no_seam_reports_the_patch_sites_that_now_dangle():
    """The failure this repository paid for 59 + 18 + 9 times, on a real module.

    `_check_auth` is patched on `core.admin_api` by dozens of suites and it has
    NOT moved. So the census is handed the fiction that it left, and it must name
    those live patch sites: that is exactly the reading a real extraction needs.
    """

    before = {"server/core/admin_api.py": "async def _check_auth(request):\n    return None\n"}
    after = {
        "server/core/admin_api.py": "# the seam left\n",
        "server/core/auth_seam.py": "async def _check_auth(request):\n    return None\n",
    }

    found, _ = division_proof.moves(before, after)
    assert len(found) == 1
    division_proof.add_seam_and_patches(found, after, "HEAD", _GUARD)

    assert found[0].seam is False
    assert len(found[0].dangling_patches) >= 1, (
        "core.admin_api no longer resolves _check_auth in this tree, so every "
        "patch that names it intercepts nothing -- and stays green"
    )
    assert found[0].verdict == "DANGLING PATCH"
    assert any("core.admin_api._check_auth" in site for site in found[0].dangling_patches)


# --------------------------------------------------------------------------- #
# Criterion 11 -- the before/after behaviour dump
# --------------------------------------------------------------------------- #


def test_the_live_dump_carries_a_surface_to_compare():
    """An empty artefact compares equal to another empty one, and certifies anything."""

    dump = division_proof.behaviour_dump()

    assert len(dump["routes"]) >= division_proof.ROUTES_AT_2026_09_02
    assert len(dump["tools"]) >= division_proof.TOOLS_AT_2026_09_02
    assert division_proof.dump_failures(dump) == []


def test_an_artefact_with_nothing_in_it_is_refused():
    failures = division_proof.dump_failures({"routes": [], "tools": []})

    assert len(failures) == 2
    assert any("routes" in line for line in failures)
    assert any("MCP tools" in line for line in failures)


def test_two_identical_dumps_diverge_nowhere():
    dump = division_proof.behaviour_dump()

    assert division_proof.compare(dump, dump) == []


def test_a_handler_that_changed_module_is_named_entry_for_entry():
    before = {
        "routes": [{"path": "/api/projects", "methods": ["GET"], "handler": "h", "module": "a"}],
        "tools": [{"tool": "t", "binder": "b"}],
    }
    after = {
        "routes": [{"path": "/api/projects", "methods": ["GET"], "handler": "h", "module": "b"}],
        "tools": [{"tool": "t", "binder": "b"}],
    }

    divergences = division_proof.compare(before, after)

    assert len(divergences) == 1
    assert "CHANGED" in divergences[0]


def test_a_splice_re_inserted_one_position_off_is_caught():
    """The set is identical and the server answers differently. Order is compared.

    Starlette resolves in declaration order, and `admin_api.py` states six such
    orders in comments. A comparison of two SETS prints "zero divergence" over
    precisely the regression this dump exists to catch.
    """

    static = {"path": "/api/projects", "methods": ["GET"], "handler": "s", "module": "m"}
    param = {"path": "/api/projects/{id}", "methods": ["GET"], "handler": "p", "module": "m"}
    before = {"routes": [static, param], "tools": []}
    after = {"routes": [param, static], "tools": []}

    divergences = division_proof.compare(before, after)

    assert len(divergences) == 2
    assert all("ORDER" in line for line in divergences)


def test_two_routes_sharing_a_key_would_merge_and_are_refused():
    """`_key` must name one entry. Two that share it read as proof of each other."""

    twin = {"path": "/api/x", "methods": ["GET"], "handler": "h", "module": "m"}
    failures = division_proof.dump_failures(
        {
            "routes": [twin, dict(twin, handler="other")] * division_proof.ROUTES_AT_2026_09_02,
            "tools": [{"tool": f"t{i}", "binder": "b"} for i in range(500)],
        }
    )

    assert len(failures) == 1
    assert "share a path AND a method set" in failures[0]
