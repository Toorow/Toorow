"""A reader that takes a Datastream id from a request resolves its scope -- AI-219.

WHY A DERIVED GUARD AND NOT A LIST. The 63.2 re-read named three unguarded
readers on 2026-08-06. Derived on the same day, the family of functions that
accept a caller-supplied Datastream id is far larger than three, and it grows
every time a surface gains a detail route. A hand-written list would age exactly
like the epic-53 list of "four" MCP tools that had become nine.

WHAT THIS FILE DERIVES, on every run:

    a function in `server/core/` that binds a Datastream id FROM THE REQUEST
      -> does its body, or a `core/` function it calls, resolve access?

A reader that takes a stream id from the caller and resolves nothing fails HERE,
by name, instead of shipping. The derivation follows DELEGATES because most
guards live one call away: `_upsert_mapping` holds no guard keyword itself, it
calls `_project_access_allowed`.

THE SECOND TEST IS THE STRUCTURAL ONE. Resolving *the identity* was never the
hole -- every offender authenticated. The hole was that `_require_datastream_role`
receives the `datastream_id` and used it for audit metadata ONLY: a member of
project A could name a stream of project B and pass. So this file also asserts
the guard proves the pair, and that its refusal reuses the one non-disclosing
envelope -- a second, distinguishable refusal would be an enumeration oracle.

WHY THE SWEEP GREW A SECOND FAMILY -- AI-278, 2026-08-10. Everything above binds
its Datastream id from `request.path_params | query_params`. An MCP tool does
not: it receives `datastream_id` as a FUNCTION ARGUMENT, so the whole MCP
surface sat outside this file by construction. Measured that day: removing the
AD-5 scope check from `inbound_mcp._readable_datastream` left **0 lines red**
across 1263 tests, and six tools that read another tenant's deliveries, inbox,
timeline, mapping context and versions stayed green.

The neighbouring guard did not cover them either: `test_mcp_tools_resolve_project_scope`
keys on a `project_id` PARAMETER, and these tools take a Datastream instead --
25 registered MCP tools take a stream id, 16 of them take no project id at all.
Two guards, and the intersection of what they missed was an entire surface.

The MCP half deliberately uses the VOCABULARY rule (a guard keyword reachable
through delegation) and not the pair rule used above. `_readable_datastream`
resolves `project_id` FROM `app.datastreams` and then authorizes THAT project --
a sound proof that carries no `project_id = %s` literal, so the stricter rule
would have accused it falsely. The behaviour itself is proved on refusals in
`tests/isolation/test_inbound_mcp_scope_refusal.py`; this file only makes a NEW
unguarded tool loud.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path
from typing import Any

_CORE = Path(__file__).resolve().parents[2] / "core"

#: What counts as "resolving access". `require_datastream_in_project` is the
#: AI-219 seam; the others predate it and stay valid -- the sweep checks that a
#: decision is taken, not which one.
_GUARDS = (
    "require_datastream_in_project",
    "_require_datastream_role",
    "_enforce_datastream_project_scope",
    "resolve_strict_resource_access",
    "identity_has_project_role",
    "identity_can_read_project",
    "_project_access_allowed",
    "_strict_project_capability_allowed",
    "_assert_project_access",
    "_refuse_unless_project_scope",
    "_require_org_read",
    "_require_org_member",
)

#: A Datastream id that came from the CALLER, not from a project-scoped read.
#: `ds_id = request.path_params.get("id", "")` is the admin_api shape -- the
#: parameter is named "id", so keying on the parameter name would miss 36 sites.
_FROM_REQUEST = re.compile(
    r"^\s*(?:\w+\s*,\s*)?(datastream_id|ds_id|stream_id)\s*=[^=].*"
    r"(request\.(?:path_params|query_params)|"
    r"(?:body|payload|data)\.get\(\s*[\"']datastream_id)",
)

#: Same binding, spread over the continuation lines of a wrapped call.
_FROM_REQUEST_KWARG = re.compile(
    r"datastream_id\s*=\s*(?:str\()?\(?\s*request\.(?:path_params|query_params)"
)

def _functions(path: Path) -> list[tuple[str, int, str]]:
    """Return (name, 1-based line, source) for every def in *path*, nested included.

    PARSED, NOT SLICED BY REGEX. The first version cut each function at the next
    `def` line and read `_datastream_schedule` as ending at the one-line closure
    it declares halfway through -- the delegate call that carries the pair fell
    into the closure and the handler was accused of proving nothing. A parent's
    source segment must contain its children.
    """
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover -- a broken module fails elsewhere
        return []
    out: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        segment = ast.get_source_segment(source, node) or ""
        out.append((node.name, node.lineno, segment))
    return out


def _guard_calls_seeing_the_stream(body: str) -> bool:
    """True when a guard is called AND the Datastream id is one of its arguments.

    THE DISTINCTION IS THE WHOLE BUG. `_list_mappings` calls
    `_project_access_allowed(project_id=...)` on one of its two branches and was
    still reachable across projects on the other: a guard that never sees the
    stream id cannot prove the stream is in the project it just authorized. So
    the sweep does not ask "is there a guard", it asks "did a guard see the id".
    """
    for guard in _GUARDS:
        for call in re.finditer(
            re.escape(guard) + r"\((?:[^()]|\([^()]*\))*\)", body, re.S
        ):
            if re.search(r"\b(datastream_id|ds_id|stream_id)\b", call.group(0)):
                return True
    return False


#: The OTHER legitimate shape, and the one 63.2 shipped: no guard sees the id,
#: but the statement that uses it constrains both columns at once --
#: `WHERE d.id = %s AND d.project_id = %s`. `schedule_mcp.read_schedule` is
#: exactly this, and calling it an offender would have been a false accusation.
_SQL_PROJECT = re.compile(r"project_id\s*=\s*%s", re.I)
_SQL_STREAM = re.compile(r"\bdatastream_id\s*=\s*%s", re.I)
#: `WHERE id = %s AND project_id = %s` on `app.datastreams` names the stream with
#: the table's own primary key -- `event_configurations.create` writes it that
#: way, and a rule that only knew the `datastream_id` spelling missed it.
_SQL_STREAM_BY_PK = re.compile(r"(?is)FROM\s+app\.datastreams\b.{0,400}?\bid\s*=\s*%s")


def _sql_carries_the_pair(body: str) -> bool:
    if not (_SQL_STREAM.search(body) or _SQL_STREAM_BY_PK.search(body)):
        return False
    return bool(_SQL_PROJECT.search(body))


def _calls_carrying_the_stream(body: str) -> set[str]:
    """Callee names invoked with a Datastream id anywhere in their argument text."""
    return {
        call.group(1)
        for call in re.finditer(r"\b([a-z_][a-zA-Z0-9_]{3,})\(", body)
        if re.search(r"\b(datastream_id|ds_id)\b", body[call.start() : call.start() + 400])
    }


def _pair_proving_functions() -> set[str]:
    """Every `core/` function that proves the pair, directly or through a delegate.

    A FIXPOINT, NOT ONE HOP. The pair usually travels two calls before it reaches
    a statement: the handler hands `(project_id, datastream_id)` to a domain
    function, which hands them to the read model, which is where the `WHERE`
    finally names both. Stopping at one hop accused four honest handlers.
    """
    bodies: dict[str, str] = {}
    for path in sorted(_CORE.glob("*.py")):
        for name, _line, body in _functions(path):
            bodies[name] = bodies.get(name, "") + "\n" + body
    proving = {
        name
        for name, body in bodies.items()
        if _guard_calls_seeing_the_stream(body) or _sql_carries_the_pair(body)
    }
    while True:
        grown = {
            name
            for name, body in bodies.items()
            if name not in proving and (_calls_carrying_the_stream(body) & (proving - {name}))
        }
        if not grown:
            return proving
        proving |= grown


def _binds_a_request_datastream_id(body: str) -> bool:
    return any(
        _FROM_REQUEST.match(line) or _FROM_REQUEST_KWARG.search(line)
        for line in body.splitlines()
    )


#: The one accounting rule the guard accepts: "my own read carries the pair, do
#: not spend a round trip proving it twice". It is a CLAIM, and this file is what
#: makes it a claim rather than a way out.
_CLAIM = "pair_proven_by_read=True"


def readers_claiming_their_read_proves_the_pair() -> list[tuple[str, str, bool]]:
    """Every call site making the claim, with whether its reader really does."""
    pair_proving = _pair_proving_functions()
    claims: list[tuple[str, str, bool]] = []
    for path in sorted(_CORE.glob("*.py")):
        functions = _functions(path)
        for name, line, body in functions:
            if _CLAIM.replace(" ", "") not in body.replace(" ", ""):
                continue
            # The claim belongs to the SMALLEST function containing it: a module
            # that has one claimed call site must not be able to launder it
            # through an enclosing function that reads something else entirely.
            if any(
                other != name
                and _CLAIM.replace(" ", "") in other_body.replace(" ", "")
                and len(other_body) < len(body)
                for other, _other_line, other_body in functions
            ):
                continue
            proven = _sql_carries_the_pair(body) or bool(
                _calls_carrying_the_stream(body) & (pair_proving - {name})
            )
            claims.append((f"server/core/{path.name}:{line} {name}", name, proven))
    return claims


def test_every_reader_claiming_the_pair_actually_carries_it(capsys):
    """A claim that stops being true must fail HERE, not silently open the hole."""
    claims = readers_claiming_their_read_proves_the_pair()
    with capsys.disabled():
        print()
        print(f"  readers claiming `{_CLAIM}` : {len(claims)}")
        for where, _name, proven in claims:
            print(f"    {'ok  ' if proven else 'FALSE'} {where}")

    unproven = [where for where, _name, proven in claims if not proven]
    assert not unproven, (
        "reader(s) telling the guard not to prove the pair, whose own SQL does "
        "not carry it:\n  " + "\n  ".join(unproven) + "\n\n"
        "Either restore both columns to the statement, or drop the argument and "
        "let the guard pay for the proof. The argument exists to avoid a SECOND "
        "proof, never to avoid the first."
    )


def test_the_claim_stays_rare_enough_to_be_read_one_by_one():
    """A ratchet on the count, because an escape hatch spreads by copy-paste.

    Raising this number is legitimate exactly once per route that is genuinely
    polled, and it must be raised in the same change as the measurement that
    justifies it -- the way `datastream_progress_api` was, at the ASGI seal.
    """
    claims = readers_claiming_their_read_proves_the_pair()
    assert len(claims) <= 1, [where for where, _name, _proven in claims]


def request_scoped_datastream_readers() -> tuple[list[str], list[str]]:
    """Return (every reader, the ones proving nothing) as ``path:line name``.

    THE LIMIT OF THE DERIVATION, written down so nobody mistakes it for proof of
    correctness: it resolves callees BY NAME, so two same-named helpers in two
    modules share a verdict, and it cannot tell that the `project_id` a delegate
    constrains is the one the caller was authorized for. Its job is to make a NEW
    unguarded reader loud, not to verify the call graph -- the behaviour itself
    is proved on real rows in `tests/isolation/`.
    """
    pair_proving = _pair_proving_functions()
    readers: list[str] = []
    offenders: list[str] = []
    for path in sorted(_CORE.glob("*.py")):
        for name, line, body in _functions(path):
            if not _binds_a_request_datastream_id(body):
                continue
            where = f"server/core/{path.name}:{line} {name}"
            readers.append(where)
            if _guard_calls_seeing_the_stream(body) or _sql_carries_the_pair(body):
                continue
            if _calls_carrying_the_stream(body) & (pair_proving - {name}):
                continue
            offenders.append(where)
    return readers, offenders


def test_every_request_scoped_datastream_reader_resolves_access(capsys):
    readers, offenders = request_scoped_datastream_readers()
    with capsys.disabled():
        print()
        print(f"  readers binding a request-supplied datastream id : {len(readers)}")
        print(f"  resolving no access at all                       : {len(offenders)}")
        for offender in offenders:
            print(f"    {offender}")

    assert not offenders, (
        "reader(s) accepting a Datastream id from the caller without ever "
        "resolving access:\n  " + "\n  ".join(offenders) + "\n\n"
        "Call `require_datastream_in_project(conn, datastream_id=..., "
        "project_id=...)` -- or one of the existing seams -- BEFORE the first "
        "read. Authenticating the identity is not authorizing the project."
    )


def test_the_re_collection_route_is_derived_and_claims_no_shortcut():
    """Story 58.4: the one handler of this family that SPENDS provider quota.

    It moved to `/api/projects/{project_id}/datastreams/{datastream_id}/refetch`,
    so it now binds BOTH ids from the path -- and a write is exactly the shape of
    reader whose guard nobody notices missing. Two things are asserted by name:
    the derivation sees it and finds it guarded, and it makes no
    `pair_proven_by_read` claim. The claim exists for a POLLED read paying for a
    membership proof twice; a re-collection is opened by a click and pays.
    """
    readers, offenders = request_scoped_datastream_readers()
    handler = "_refetch_datastream"
    assert any(where.endswith(handler) for where in readers), (
        "the re-collection handler is no longer derived as a request-scoped "
        "reader -- either it stopped binding the id from the request, or the "
        "derivation stopped seeing it, and both make this guard blind to it"
    )
    assert not any(where.endswith(handler) for where in offenders)
    assert not any(
        name == handler for _where, name, _proven in
        readers_claiming_their_read_proves_the_pair()
    )


# ---------------------------------------------------------------------------
# THE SAME QUESTION, ASKED OF THE MCP SURFACE -- AI-278.
#
# A tool receives its Datastream id as an ARGUMENT, never from a request, so
# every derivation above is blind to it. This half asks the one question that
# still applies: a registered MCP tool that names a Datastream -- does anything
# on its path decide whether this caller may have it?
# ---------------------------------------------------------------------------

#: The vocabulary, widened to the org-scoped deciders the MCP modules use.
#: `core/project_access.py` is the authority on this list, and
#: `test_the_mcp_guard_vocabulary_knows_every_access_decision_helper` fails when
#: it grows without this tuple growing -- a decision helper missing from here
#: makes a guarded tool read as unguarded, which pushes toward exempting rather
#: than repairing.
_MCP_GUARDS = _GUARDS + (
    "refuse_unless_project_scope",
    "identity_can_manage_org",
    "identity_has_org_access",
    "identity_can_access_project_in_org",
    "resolve_org_role",
    "resolve_provider_account_access",
    "resolve_scheduled_account_access",
    "effective_project_role",
)

#: Answers EXISTENCE, not access. Named so the vocabulary test does not demand
#: it, and kept out of `_MCP_GUARDS` so a tool calling only it stays an offender.
_NOT_AN_ACCESS_DECISION = frozenset({"project_exists"})

_STREAM_PARAMS = frozenset({"datastream_id", "ds_id", "stream_id"})
_CALLED = re.compile(r"\b([A-Za-z_]\w*)\s*\(")

#: `from core.project_access import resolve_strict_resource_access as _access`.
#: The aliased call is why the neighbouring guard matches guard names as plain
#: TEXT -- an AST call check keyed on the real name sees only `_access`.
_ALIASED_IMPORT = re.compile(r"import\s+(\w+)\s+as\s+(\w+)")


def _alias_map(tree: ast.Module) -> dict[str, str]:
    """`from core.inbound_health import get_inbound_health as _read` -> {_read: get_inbound_health}.

    THE THIRD FALSE ABSOLUTION, and the last one the AD-5 mutant survived. With
    method calls dropped, `inbound_mcp.get_inbound_health` still passed on the
    mutant: its remaining bare call was `_read`, a LOCAL ALIAS, and the
    name-keyed delegate map matched it against an unrelated `_read` defined
    elsewhere in `core/` that happens to be guard-bearing. The tool was cleared
    by a function it has never called.

    Resolving the alias to what it actually imports makes the delegate lookup
    ask about `get_inbound_health` -- the function really behind `_read`.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for name in node.names:
                if name.asname:
                    aliases[name.asname] = name.name.split(".")[-1]
    return aliases


def _called_names(segment: str, aliases: dict[str, str] | None = None) -> set[str]:
    """Names invoked as PLAIN FUNCTIONS in *segment* -- never method calls.

    THE REGEX VERSION ABSOLVED THE THING IT WAS WATCHING. `\\b(\\w+)\\s*\\(` cannot
    tell `identity_can_read_project(...)` from `cur.execute(...)`, so every
    `conn.cursor()`, `cur.execute()` and `row.get()` was resolved against
    `core/` function names by bare word. With the delegation fixpoint that put
    **670** names in the guard-bearing set, and any tool calling `execute` or
    `get` -- which is all of them -- was absolved.

    Measured on the AD-5 mutant: 5 of the 6 inbound tools failed, and
    `get_inbound_health` passed, cleared by the `execute`/`get` collision alone.
    Resolving through the AST drops the method calls and the sixth fails too.
    """
    try:
        tree = ast.parse(textwrap.dedent(segment))
    except SyntaxError:
        return set()
    local = _alias_map(tree)
    resolved = {**(aliases or {}), **local}
    return {
        resolved.get(node.func.id, node.func.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _calls_a_guard(segment: str) -> bool:
    """True when the source CALLS an access decision, alias included.

    A CALL, NEVER A MENTION, and that distinction is the whole test. The first
    version of this sweep asked `guard in segment`, copied from the neighbouring
    file -- and the AD-5 mutation of 2026-08-10 survived it: deleting the
    `if not identity_can_read_project(...)` body left the module's own
    `from core.project_access import identity_can_read_project` line in place,
    so the guard was still spelled inside the function that no longer ran it.
    Measured: 3 passed, 0 red, with the check gone.

    An import is a declaration of intent. Only a call is evidence.
    """
    names = set(_MCP_GUARDS)
    for original, alias in _ALIASED_IMPORT.findall(segment):
        if original in names:
            names.add(alias)
    return any(
        re.search(r"\b" + re.escape(name) + r"\s*\(", segment) for name in names
    )

#: How many delegation hops the sweep follows. The measured chains need two
#: (`get_inbound_delivery -> _readable_datastream -> identity_can_read_project`);
#: an unbounded closure would make every tool that calls anything look guarded.
_MCP_DELEGATION_DEPTH = 3


def _module_trees() -> dict[str, tuple[str, ast.Module]]:
    out: dict[str, tuple[str, ast.Module]] = {}
    for path in sorted(_CORE.glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            out[path.name] = (source, ast.parse(source))
        except SyntaxError:  # pragma: no cover -- a broken module fails elsewhere
            continue
    return out


def _registered_mcp_tools(source: str, tree: ast.Module) -> set[str]:
    """Every name this module publishes as an MCP tool.

    FOUR SHAPES, and the fourth is why this exists. Beyond `@mcp.tool`,
    `mcp.tool(f)` and `register_profiled(mcp, f)`, a module may also DECLARE its
    surface in a `*_MCP_TOOLS` tuple -- `inbound_mcp.INBOUND_MCP_TOOLS` is
    exactly that, and it is the list a reviewer reads. Deriving from the tuple
    too means the declared surface and the swept surface cannot disagree.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.add(node.name)
        if isinstance(node, ast.Call):
            func, arg = node.func, None
            if isinstance(func, ast.Attribute) and func.attr == "tool" and node.args:
                arg = node.args[0]
            elif (
                isinstance(func, ast.Name)
                and func.id == "register_profiled"
                and len(node.args) >= 2
            ):
                arg = node.args[1]
            if isinstance(arg, ast.Name):
                names.add(arg.id)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.endswith("_MCP_TOOLS"):
                    if isinstance(node.value, (ast.Tuple, ast.List)):
                        names.update(
                            element.value
                            for element in node.value.elts
                            if isinstance(element, ast.Constant)
                            and isinstance(element.value, str)
                        )
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if (
                isinstance(target, ast.Name)
                and target.id.endswith("_MCP_TOOLS")
                and isinstance(node.value, (ast.Tuple, ast.List))
            ):
                names.update(
                    element.value
                    for element in node.value.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                )
    # FIFTH SHAPE -- `for handler in (a, b): register_profiled(mcp, handler, ...)`.
    #
    # The loop registers a tuple of tools under one declaration, and the pass above
    # reads its VARIABLE: it recorded `handler`, a name no module defines, and the
    # two real tools vanished from the sweep. Measured when AD-43 gave every module
    # its declaration: `datastream_diagnosis.py` dropped out of
    # `test_the_mcp_sweep_does_not_go_blind` -- eleven contributing modules became
    # ten, and `datastream_pull_history` / `datastream_diagnose`, which both take a
    # `datastream_id` from the model, stopped being swept. That is exactly the
    # silence that test is named after, and the same unrolling already lives in
    # `test_mcp_tools_resolve_project_scope._registered_tools`.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id in names
            and isinstance(node.iter, (ast.Tuple, ast.List))
        ):
            names.discard(node.target.id)
            names.update(e.id for e in node.iter.elts if isinstance(e, ast.Name))
    del source
    return names


def _mcp_guard_bearing() -> set[str]:
    """Every `core/` function taking an access decision, delegates included.

    NAME-KEYED, and the limit is written down because it bites here: two
    same-named helpers in two modules share one verdict --
    `inbound_mcp.get_inbound_health` and `inbound_health.get_inbound_health` are
    a real pair in this repo. It is why a TOOL is judged on its own
    module-scoped source below, and only its DELEGATES are resolved by name.
    """
    bodies: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for source, tree in _module_trees().values():
        aliases = _alias_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bodies.setdefault(node.name, []).append(
                    (ast.get_source_segment(source, node) or "", aliases)
                )
    bearing = {
        name
        for name, segments in bodies.items()
        if any(_calls_a_guard(segment) for segment, _ in segments)
    }
    for _ in range(_MCP_DELEGATION_DEPTH):
        grown = {
            name
            for name, segments in bodies.items()
            if set().union(*(_called_names(s, a) for s, a in segments)) & (bearing - {name})
        }
        if grown <= bearing:
            break
        bearing |= grown
    return bearing


def datastream_scoped_mcp_tools() -> tuple[list[str], list[str]]:
    """Return (every stream-scoped tool, the ones resolving nothing)."""
    bearing = _mcp_guard_bearing()
    scoped: list[str] = []
    offenders: list[str] = []
    for module, (source, tree) in _module_trees().items():
        aliases = _alias_map(tree)
        defined: dict[str, Any] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.setdefault(node.name, node)
        for name in sorted(_registered_mcp_tools(source, tree)):
            node = defined.get(name)
            if node is None:
                continue
            params = [a.arg for a in node.args.args + node.args.kwonlyargs]
            if not (_STREAM_PARAMS & set(params)):
                continue
            where = f"server/core/{module}:{node.lineno} {name}"
            scoped.append(where)
            segment = ast.get_source_segment(source, node) or ""
            if _calls_a_guard(segment):
                continue
            if _called_names(segment, aliases) & (bearing - {name}):
                continue
            offenders.append(where)
    return scoped, offenders


def test_every_datastream_scoped_mcp_tool_resolves_access(capsys):
    """A tool naming a Datastream it was never authorized for fails HERE."""
    scoped, offenders = datastream_scoped_mcp_tools()
    with capsys.disabled():
        print()
        print(f"  MCP tools taking a Datastream id : {len(scoped)}")
        print(f"  resolving no access at all       : {len(offenders)}")
        for offender in offenders:
            print(f"    {offender}")

    assert not offenders, (
        "MCP tool(s) accepting a Datastream id from the model without ever "
        "resolving access:\n  " + "\n  ".join(offenders) + "\n\n"
        "Resolve the caller identity and refuse a Datastream outside its "
        "readable projects BEFORE the first read -- `_readable_datastream` in "
        "`inbound_mcp` and `_guard_datastream` in `operations_mcp` are the two "
        "shapes already in use. A model-invokable read is the one surface where "
        "a missing guard is reachable without a session."
    )


def test_the_mcp_sweep_does_not_go_blind(capsys):
    """A sweep that stops SEEING its subjects is worse than no sweep.

    The count going to zero -- a renamed registrar, a new registration shape --
    would turn this guard green forever without a word. So the families that
    exist today are asserted by name, and a shrinking count fails loudly.
    """
    scoped, _ = datastream_scoped_mcp_tools()
    modules = {where.split(":")[0] for where in scoped}
    for expected in (
        "server/core/inbound_mcp.py",
        "server/core/operations_mcp.py",
        "server/core/mapping_proposal_mcp.py",
    ):
        assert any(where.startswith(expected) for where in scoped), (
            f"{expected} no longer contributes a Datastream-scoped MCP tool to "
            "the sweep. Either the tools moved, or the derivation stopped "
            "reading their registration -- and the second is silent."
        )
    with capsys.disabled():
        print(f"\n  modules contributing stream-scoped MCP tools : {len(modules)}")
    assert len(scoped) >= 20, (
        f"the sweep sees only {len(scoped)} Datastream-scoped MCP tools; 25 were "
        "derived on 2026-08-10. A falling count means a registration shape went "
        "unread, not that the surface shrank."
    )


def test_the_inbound_tuple_and_the_swept_surface_agree():
    """`INBOUND_MCP_TOOLS` is what a reviewer reads; it must be what is swept.

    The tuple exists so an added tool fails a test rather than shipping quietly.
    That only holds while the tuple and the registration agree -- a tool
    registered and left out of the tuple would be reviewed by nobody.
    """
    from core import mcp_profiles
    from core.inbound_mcp import INBOUND_MCP_TOOLS, register_inbound_tools

    registered: list[str] = []

    class _Recorder:
        # `register_profiled` forwards `name`/`tags`/`meta` (and `app`) to
        # `mcp.tool`, so a recorder that took the handler alone stopped recording
        # the moment these six tools received the declaration AD-43 requires.
        def tool(self, handler, **_declaration):
            registered.append(handler.__name__)
            return handler

    # The registrar records into a PROCESS-GLOBAL registry; this sweep is not a
    # registration and must not leave one behind for the suites that follow.
    before = dict(mcp_profiles._REGISTRY.declarations)
    try:
        register_inbound_tools(_Recorder())
    finally:
        mcp_profiles._REGISTRY.declarations.clear()
        mcp_profiles._REGISTRY.declarations.update(before)
    assert sorted(registered) == sorted(INBOUND_MCP_TOOLS), (
        "the inbound MCP surface and its declared tuple disagree:\n"
        f"  registered but undeclared : {sorted(set(registered) - set(INBOUND_MCP_TOOLS))}\n"
        f"  declared but unregistered : {sorted(set(INBOUND_MCP_TOOLS) - set(registered))}"
    )


def test_the_mcp_guard_vocabulary_knows_every_access_decision_helper():
    """`core/project_access.py` grows; `_MCP_GUARDS` must grow with it."""
    tree = ast.parse((_CORE / "project_access.py").read_text(encoding="utf-8"))
    public = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    missing = sorted(public - set(_MCP_GUARDS) - _NOT_AN_ACCESS_DECISION)
    assert not missing, (
        "entry point(s) of `core/project_access.py` the MCP sweep does not "
        "recognise as an access decision:\n  " + "\n  ".join(missing) + "\n\n"
        "Add them to `_MCP_GUARDS`, or to `_NOT_AN_ACCESS_DECISION` when the "
        "function answers something other than 'may this caller'."
    )


def _guard_source() -> str:
    tree = ast.parse((_CORE / "admin_api.py").read_text(encoding="utf-8"))
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_require_datastream_role"
    )
    # THE CODE, NOT THE PROSE: a docstring that explains the rule must not be
    # able to satisfy the test that the rule is implemented.
    node.body = [
        n
        for n in node.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    ]
    return ast.unparse(node)


def test_the_role_guard_proves_the_stream_belongs_to_the_project():
    """The id it already receives must decide, not just decorate the audit row."""
    code = _guard_source()
    assert "require_datastream_in_project" in code, (
        "`_require_datastream_role` receives a `datastream_id` and never proves "
        "it belongs to `project_id`. 43 of its 44 id-bearing call sites take that "
        "id straight from the URL path -- without the pair, a member of project A "
        "reaches a stream of project B on every one of them."
    )


def test_the_pair_proof_is_one_statement_carrying_both_columns():
    """`WHERE id = %s AND project_id = %s` -- the shape 63.2 proved on real rows."""
    source = (_CORE / "admin_api.py").read_text(encoding="utf-8")
    node = next(
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef) and n.name == "require_datastream_in_project"
    )
    sql = " ".join(ast.unparse(node).split())
    assert "FROM app.datastreams" in sql, sql
    assert "id = %s" in sql and "project_id = %s" in sql, (
        "the membership proof must constrain BOTH columns in ONE statement: "
        "reading the row then comparing in Python re-opens the race and the "
        "second refusal envelope.\n" + sql
    )


def test_the_refusal_is_indistinguishable_from_an_absent_stream():
    """Two refusals that differ teach the caller that a stream exists."""
    code = _guard_source()
    assert code.count('"code": \'not_found\'') + code.count("'code': 'not_found'") >= 1, code
    for word in ("forbidden", "unauthorized", "access_denied", "cross_project"):
        assert word not in code, (
            f"the guard introduces a distinct refusal code ({word!r}): comparing "
            "two refusals would then reveal that the stream exists elsewhere"
        )
