"""An MCP surface acquires an ARMED connection, or its exception is dated here.

WHY THIS FILE EXISTS. `core/db.py` states the rule in the docstring of
`request_connection`: "THE ACQUISITION IS THE PLACE [...] arming a *different*
connection buys nothing either: the authorization check usually opens, uses and
closes its own, and the handler then opens a fresh unarmed one." Every MCP tool
that resolved a caller and then read tenant state on a bare `get_connection()`
was that exact sentence, executed. NOTHING COUNTED THEM. Measured on
2026-08-21, before this file existed:

    grep -rln request_connection server/tests/conformance/   ->  0 files

and the sweep below returned **72** bare acquisitions inside MCP surface
modules. The neighbouring half of the same class (`test_mcp_tools_resolve_
project_scope.py`) records what happens when nothing counts: four unguarded
tools on 2026-07-31, nine on 2026-08-05, without anyone being wrong.

IT SWEEPS, IT DOES NOT ENUMERATE, AND IT DOES NOT SWEEP BY FILENAME. The
inventory is recomputed by AST on every run over the whole of `server/` (tests
excluded), and a module counts as an MCP surface because it REGISTERS tools --
`register_profiled(...)`, `mcp.tool(...)`, `@mcp.tool` -- never because it is
called `*_mcp.py`. That distinction is not decoration: three of the surfaces
found this way are `core/main.py`, `core/datastream_diagnosis.py` and
`core/mcp_profiles.py`, and a filename-anchored sweep declared **65** sites
where there were 72. Seven acquisitions -- five of them in
`datastream_diagnosis`, on the support-disclosure WRITE path -- lived in the
blind spot of the count that named this work.

AND IT RESOLVES IMPORT ALIASES, since 2026-08-24 (67-1). The visitor matched the
CALLED NAME, so `from core.db import get_connection as _pg` followed by `_pg()`
was invisible to it -- and that exact line was live in `report_mcp.get_report`,
one bare acquisition hiding behind two characters inside a function this file
already declared as debt. A sweep that can be defeated by an alias measures
spelling, not acquisition. The neighbouring guard
(`test_mcp_tools_resolve_project_scope.py`) learnt the same lesson on
`resolve_strict_resource_access as _access`; the repair is the same shape.

WHAT COUNTS AS ARMED. `core.db.request_connection(identity)` installs the
Epic-36 floor and translates the identity for it. `core.db.background_connection
(reason)` is the honest unisolated path and already forces its caller to write
down why, so it is not what this file hunts. `get_connection()` is the one
acquisition that says nothing at all, and it is the only one measured here.

THE LIST BELOW IS A DEBT, NOT A VOCABULARY. It is compared for STRICT EQUALITY
against the sweep: a new bare acquisition fails with its own name, and an entry
that no longer matches a site fails just as loudly. A stale exemption is a lie
about the shape of the codebase, and this file refuses to keep it quietly. Every
entry carries the date it was written and the reason it is not a repair; adding
one is a decision a reader can see in the diff, which is the whole point of
freezing the count instead of trusting a threshold.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[2]

#: What the sweep does NOT read. `tests` is excluded because a test double named
#: `get_connection` is not an acquisition of the product; everything else under
#: `server/` is in scope, including packages that do not exist yet.
_SKIPPED_PARTS = frozenset({"tests", "__pycache__", ".venv", "node_modules", "build", "dist"})

#: The acquisition that names nothing. `request_connection` arms the floor and
#: `background_connection` demands a written reason; neither is measured here.
_BARE_ACQUISITION = "get_connection"

#: THE DEBT, DATED. Key: (path relative to `server/`, enclosing function) ->
#: number of bare acquisitions in that function. Compared for strict equality.
#:
#: SENSE OF THE ONE-WAY RATCHET: removing an entry is a repair that holds;
#: adding one asks its author to write, right here, why an MCP surface reads or
#: writes on a connection that carries no access context.
_UNARMED_BY_DESIGN_OR_BY_DEBT: dict[tuple[str, str], int] = {
    # --- Platform scope: there is no tenant column to isolate on -------------
    # `app.platform_clocks` has NO `org_id`, so the Epic-36 membership gate
    # cannot decide it. Its policy opens on `toorow.platform_operator`, which
    # `request_connection` does not set and `platform_clocks.arm_platform_clock_
    # access` does -- transaction-locally, inside the read model these four sites
    # call. Converting them would arm the wrong flag and hide zero rows from the
    # one operator the surface exists for. 2026-08-21.
    ("core/platform_clocks_mcp.py", "_collect"): 1,
    ("core/platform_clocks_mcp.py", "set_platform_clock_cadence"): 1,
    ("core/platform_clocks_mcp.py", "apply_platform_clock"): 1,
    ("core/platform_clocks_mcp.py", "run_platform_clock_now"): 1,
    # The connector installation/domain/verification read models are platform
    # catalogs behind `identity_is_super_admin`; their tables carry no `org_id`
    # and no policy. The access decision is the allow-list, and there is no
    # second barrier to arm. 2026-08-21.
    ("core/operations_mcp.py", "get_connector_installation_status"): 1,
    ("core/operations_mcp.py", "get_connector_domain_config"): 1,
    ("core/operations_mcp.py", "get_connector_verification_status"): 1,
    # The inbound template catalogue ships with the product, one immutable row
    # per version, no tenant column and no caller identity in scope. 2026-08-21.
    ("core/operations_mcp.py", "list_inbound_templates"): 1,
    # The `datastream_id`-less branch of inbound health reads the connector's
    # installation and domain state -- platform facts, no tenant row, and no
    # identity is resolved on that branch at all. The tenant-scoped branch below
    # it acquires through `_readable_datastream`, which IS armed. 2026-08-21.
    ("core/inbound_mcp.py", "get_inbound_health"): 1,
    # --- Before there is an identity to arm with -----------------------------
    # THE `mcp_profiles._attest` ENTRY IS GONE, 2026-08-31, and it was FALSE the
    # day it was written. It said `_attest` ran "before any caller identity has
    # been resolved"; its only caller, `_capability_context`, resolves the
    # identity through `mcp_scope.caller_identity` and passes it in on the very
    # next statement. An exemption that describes a codebase that does not exist
    # is what the header of this file refuses to keep quietly, so the site was
    # armed instead: `request_connection(identity)`, and the Epic-36 policy on
    # `app.mcp_capability_contexts` (`epic36_is_org_member(org_id)`, migration
    # 273) now refuses a pointer naming another organization's context.
    # --- The parallel-session block is GONE, 2026-08-24 (67-1) ---------------
    # It exempted `connectors_mcp.get_source_capabilities`,
    # `notebook_mcp._assert_project_access`,
    # `report_mcp._load_project_geographic_posture` and `report_mcp.get_report`
    # because another session held those files. `git status --porcelain --
    # server/core/connectors_mcp.py server/core/notebook_mcp.py
    # server/core/report_mcp.py` returned NOTHING on 2026-08-24, so the reason
    # had expired, and every one of those sites was armed by exactly the
    # one-line change the block promised -- eight acquisitions, not seven: the
    # aliased `_pg()` in `get_report` was never in the count at all.
    #
    # `_load_project_geographic_posture` was the only one that cost more than a
    # line: it took no identity. It now takes one as a REQUIRED parameter,
    # threaded from its single production caller. That is a different question
    # from `main.py:_resolve_project` below, and the difference is the whole
    # arbitration: this helper has ONE caller and does not decide existence.
    #
    # --- The design question was ARBITRATED, so the entry is GONE -----------
    # This block used to declare `("core/main.py", "_resolve_project"): 1` and
    # said the arming was an arbitration rather than a repair, because arming it
    # would turn "you may not see this project" into "this project does not
    # exist". Jean ratified that exact answer on 2026-08-25
    # (`docs/product-architecture/mcp-tool-surface.md`, "naming a project you may
    # not see answers `project_not_found`"): ONE envelope for absent and
    # forbidden, no enumeration oracle. `_resolve_project` now takes an identity,
    # acquires by `core.db.request_connection`, and resolves access through
    # `mcp_scope.refuse_unless_project_scope`. The declared debt drops by one
    # because the site is armed, not because the rule moved.
    #
    # --- `notebook_mcp` : deux sites ARMES, un troisieme sans identite --------
    #
    # 2026-08-22 (story 67.23) : `save_notebook` et `run_notebook` ont bascule sur
    # le magasin gouverne, et leur connexion est passee a `request_connection(
    # identity)` par le meme changement d'une ligne que les 49 armees -- la
    # phrase du bloc ci-dessus disait qu'ils l'etaient ; ils le sont. Leurs deux
    # entrees sont donc SUPPRIMEES plutot que recomptees.
    #
    # `run_notebook_direct` reste nu, et pour la seule raison qui vaille : c'est
    # le point d'entree du pas NOCTURNE. Il n'a pas d'appelant humain, donc pas
    # d'identite a armer -- son acteur est le mot `scheduler`. L'armer
    # demanderait d'inventer un sujet, ce qui donnerait un contexte d'acces que
    # personne n'a accorde. Une acquisition, pas deux : le second moteur
    # d'execution qu'il portait est parti avec le magasin herite.
    ("core/notebook_mcp.py", "run_notebook_direct"): 1,
}


def _python_files() -> list[Path]:
    return [
        p
        for p in sorted(_SERVER.rglob("*.py"))
        if not _SKIPPED_PARTS.intersection(p.relative_to(_SERVER).parts)
    ]


def _registers_mcp_tools(tree: ast.AST) -> bool:
    """True when this module REGISTERS MCP tools, whatever it is called.

    Three shapes, and all three are in the tree today: `register_profiled(mcp,
    fn)` (the profiled registrar every recent surface uses), `mcp.tool(fn)` (the
    direct call), and `@mcp.tool()` (the decorator). Reading the registration
    rather than the filename is what puts `main.py`, `datastream_diagnosis.py`
    and `mcp_profiles.py` inside the perimeter.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "register_profiled":
                return True
            if (
                isinstance(func, ast.Attribute)
                and func.attr in ("tool", "register_profiled")
                and isinstance(func.value, ast.Name)
                and func.value.id == "mcp"
            ):
                return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for raw in node.decorator_list:
                dec = raw.func if isinstance(raw, ast.Call) else raw
                if (
                    isinstance(dec, ast.Attribute)
                    and dec.attr == "tool"
                    and isinstance(dec.value, ast.Name)
                    and dec.value.id == "mcp"
                ):
                    return True
    return False


class _BareAcquisitions(ast.NodeVisitor):
    """Collect (function name, line) for every `get_connection()` call.

    Import aliases are resolved first: `from core.db import get_connection as
    _pg` makes `_pg()` an acquisition of `get_connection`, and matching the
    written name alone let that spelling out of the count.
    """

    def __init__(self) -> None:
        self._enclosing: list[ast.AST] = []
        self._aliases: set[str] = set()
        self.found: list[tuple[str, int]] = []

    def _visit_function(self, node: ast.AST) -> None:
        self._enclosing.append(node)
        self.generic_visit(node)
        self._enclosing.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def seed_aliases(self, tree: ast.AST) -> None:
        """One pass over the WHOLE module first, so order never decides.

        Recording aliases as the walk meets them would make the answer depend
        on where the import sits relative to the call -- and these modules
        import inside function bodies (the documented anti-cycle seam).
        """
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if (
                        alias.asname
                        and alias.name.rsplit(".", 1)[-1] == _BARE_ACQUISITION
                    ):
                        self._aliases.add(alias.asname)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else (func.attr if isinstance(func, ast.Attribute) else None)
        )
        if name == _BARE_ACQUISITION or name in self._aliases:
            owner = self._enclosing[-1] if self._enclosing else None
            self.found.append(
                (getattr(owner, "name", "<module>"), node.lineno)
            )
        self.generic_visit(node)


def sweep_mcp_surfaces() -> dict[tuple[str, str], list[int]]:
    """Recompute the inventory from the source tree. Never from a stored list."""
    inventory: dict[tuple[str, str], list[int]] = {}
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if not _registers_mcp_tools(tree):
            continue
        visitor = _BareAcquisitions()
        visitor.seed_aliases(tree)
        visitor.visit(tree)
        relative = path.relative_to(_SERVER).as_posix()
        for function_name, lineno in visitor.found:
            inventory.setdefault((relative, function_name), []).append(lineno)
    return inventory


def test_the_sweep_actually_finds_mcp_surfaces() -> None:
    """A sweep that matched nothing would make every other test here vacuous.

    This is the instrument checking its own reach, and it is deliberately not a
    check against the debt list below: a guard validated against the list it
    produces measures the list.
    """
    surfaces = {module for module, _ in sweep_mcp_surfaces()} | {
        path.relative_to(_SERVER).as_posix()
        for path in _python_files()
        if _registers_mcp_tools(_safe_parse(path))
    }
    assert len(surfaces) >= 20, (
        f"the MCP surface sweep found only {len(surfaces)} registering module(s); "
        "the registration shapes it looks for no longer match the code, so every "
        "other assertion in this file is empty"
    )


def _safe_parse(path: Path) -> ast.AST:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return ast.Module(body=[], type_ignores=[])


def test_every_bare_acquisition_in_an_mcp_surface_is_dated_here() -> None:
    """A new unarmed acquisition fails HERE, with its own name and line."""
    measured = {key: len(lines) for key, lines in sweep_mcp_surfaces().items()}
    lines_of = sweep_mcp_surfaces()

    undeclared = {
        key: lines_of[key]
        for key in measured
        if key not in _UNARMED_BY_DESIGN_OR_BY_DEBT
    }
    assert not undeclared, (
        "an MCP surface acquires its connection through `get_connection()`, which "
        "carries no access context -- so the row-level floor is not armed behind "
        "it:\n"
        + "\n".join(
            f"  server/{module}:{','.join(str(n) for n in lines)}  in {function}()"
            for (module, function), lines in sorted(undeclared.items())
        )
        + "\n\nRepair it: acquire with `core.db.request_connection(identity)` at "
        "the site, using the identity the surface already resolved. If it cannot "
        "be armed, say so in `_UNARMED_BY_DESIGN_OR_BY_DEBT` in this file, with "
        "the date and the reason -- never with a default identity of `None`, "
        "which is a hole that reads like a repair."
    )

    stale = {
        key: count
        for key, count in _UNARMED_BY_DESIGN_OR_BY_DEBT.items()
        if measured.get(key) != count
    }
    assert not stale, (
        "an entry of `_UNARMED_BY_DESIGN_OR_BY_DEBT` no longer matches the code. "
        "A stale exemption describes a codebase that no longer exists, and it is "
        "exactly as misleading as an unmeasured leak:\n"
        + "\n".join(
            f"  server/{module}  {function}(): declared {count}, measured "
            f"{measured.get(key, 0)}"
            for key, (count) in sorted(stale.items())
            for module, function in [key]
        )
        + "\n\nRepair it: delete the entry if the site is armed, or correct its "
        "count if the surface grew a second acquisition."
    )


def test_the_debt_never_grows_in_silence() -> None:
    """The total is frozen, so raising it is a line a reviewer reads.

    Strict equality above already names every drift. This second assertion
    exists because a count is the thing a person carries away from a run, and
    because the neighbouring half of this class went from 38 to 49 without
    anyone seeing a number move.
    """
    # 23 -> 19 le 2026-08-22 (story 67.23) : `save_notebook` et `run_notebook`
    # ont ete ARMES (leurs deux entrees sont parties, 3 acquisitions), et
    # `run_notebook_direct` est passe de 2 a 1 en perdant le second moteur
    # d'execution qu'il portait sur le magasin herite. Quatre de moins, aucune
    # exemption elargie.
    #
    # 19 -> 12 le 2026-08-24 (67-1) : les quatre entrees << tenues par une
    # session voisine >> sont parties, sept acquisitions declarees et une
    # huitieme que l'alias `_pg` cachait. Ce qui reste n'est PLUS de la dette :
    # dix sites de portee plateforme ou d'avant-identite, plus les deux qui
    # demandent une decision -- `_resolve_project` (arbitrage ecrit ci-dessus)
    # et `run_notebook_direct` (le pas nocturne n'a pas de sujet).
    #
    # 12 -> 11 le 2026-08-25 : l'arbitrage `_resolve_project` est RENDU, et le
    # site est arme. Ce qui reste est dix sites de portee plateforme ou
    # d'avant-identite, plus `run_notebook_direct` -- le pas nocturne n'a pas de
    # sujet, et lui en inventer un donnerait un contexte d'acces que personne
    # n'a accorde.
    # 11 -> 10 le 2026-08-31 : `mcp_profiles._attest` est ARME. Son exemption
    # n'etait pas une dette, c'etait une phrase fausse -- l'identite est resolue
    # une ligne avant l'appel.
    assert sum(_UNARMED_BY_DESIGN_OR_BY_DEBT.values()) == 10, (
        "the number of unarmed acquisitions declared in this file changed. Going "
        "DOWN is a repair: lower the constant in the same commit. Going UP means "
        "an MCP surface was written against the rule of `core/db.py` -- arm it "
        "instead."
    )
