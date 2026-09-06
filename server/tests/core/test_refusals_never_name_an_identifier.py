"""A refusal names the gesture that repairs. Never an identifier -- SWEPT, not listed.

WHAT THIS REPLACES, AND WHY IT HAD TO BE REPLACED.

`tests/integration/test_multi_source_plan_pg.py` held five scenarios in a dict,
one per refusal somebody had met, and promised in its docstring that "the next
one added is measured against the same rule". A dict of five cannot keep that
promise: nothing measures a refusal nobody wrote a scenario for. And that is not
a hypothetical -- while those five stood green,
`multi_source_execution._derived_selects` rendered

    A ratio is computed from two measures this analysis actually selected.
    mdm_01J0000000000000000000RR names one that is absent.

into `manifest.refusal.message` of the Result, for eight months. The enumeration
did not catch it because the enumeration was the finder.

SO THE FINDER IS A WALK. Every `raise <Something>Refused(...)` under
`server/core/` is located by the AST, its message is read, and every value the
message interpolates is examined. A value whose OUTERMOST expression reads an
identifier -- a name ending `_id`, a `["..._id"]`, a `.get("..._id")` -- puts a
`mdm_<ULID>`, a `ds_<ULID>` or a `qsv_<ULID>` in front of a person, and comes
back here with its file, its line and its refusal code.

Nothing has to be registered for a new refusal to be measured. That is the only
property that separates an instrument from a list, and it is the property the
five-scenario dict never had.

WHY THE OUTERMOST EXPRESSION, AND NOT THE TEXT. `_field_word(field_id, ...)`
mentions `field_id` and renders a WORD; `derived.get("canonical_field_id")`
mentions the same thing and renders the id. The difference is not in the text,
it is in what the expression evaluates to -- so the walk reads the shape of the
expression rather than grepping its source.

A REFUSAL IS NOT ALWAYS RAISED (2026-09-01). The walk above reads
`raise <X>Refused(...)` and nothing else, so for eight months it never once read
the OTHER half of the product's refusal vocabulary: the `Refusal(code, message,
subject)` records that `prepare_change_set`, `validate_query_spec`,
`golden_questions` and the visualization validator append to a list and hand
back inside a 200. Those reach a person through exactly the same panel. Measured
the day the second walk was written: **328 `Refusal(...)` constructions in
`server/core/`, 29 of them rendering an identifier at a reader** -- one of which,
`semantic_model.py:775`, was the exact twin of the `raise` this file caught on
2026-08-31 and 4b034bc0 repaired, three hundred lines above it in the same file.
The instrument had read one and not the other.

The four sites of `semantic_model.py` are repaired in the same commit as this
walk. The remaining 25 stand in `REFUSAL_VALUES_AT_2026_09_01`, a DECREASING
ratchet on the shape `scripts/ledger_verdict_census.py` uses for prose verdicts:
striking an identity is free (it is the record of a repair), adding one is an
admission, and both directions fail loudly.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

#: `server/`, from `server/tests/core/`.
SERVER = Path(__file__).resolve().parents[2]
CORE = SERVER / "core"

#: A name, key or attribute that holds an identity rather than a word. The
#: product's identifier families are minted as `<prefix>_<ULID>` and carried in
#: exactly these: `field_id`, `datastream_id`, `member_id`, `version_id`,
#: `canonical_field_id`, and their plurals.
_IDENTITY_NAME = re.compile(r"(?:^|_)(?:id|ids)$")


@dataclass(frozen=True)
class Leak:
    module: str
    lineno: int
    code: str
    expression: str

    def __str__(self) -> str:
        return f"core/{self.module}:{self.lineno} ({self.code}) renders {self.expression}"


def _reads_an_identifier(node: ast.expr) -> bool:
    """Does this expression evaluate to an identity rather than to a word?"""
    if isinstance(node, ast.Name):
        return _IDENTITY_NAME.search(node.id) is not None
    if isinstance(node, ast.Attribute):
        return _IDENTITY_NAME.search(node.attr) is not None
    if isinstance(node, ast.Subscript):
        key = node.slice
        return (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and _IDENTITY_NAME.search(key.value) is not None
        )
    if isinstance(node, ast.Call):
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr == "get" and node.args:
            key = node.args[0]
            return (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and _IDENTITY_NAME.search(key.value) is not None
            )
        # `str(field_id)` and `repr(version_id)` render exactly what they wrap.
        if isinstance(function, ast.Name) and function.id in {"str", "repr"} and node.args:
            return _reads_an_identifier(node.args[0])
        return False
    if isinstance(node, ast.JoinedStr):
        return any(
            _reads_an_identifier(part.value)
            for part in node.values
            if isinstance(part, ast.FormattedValue)
        )
    if isinstance(node, ast.BoolOp):
        return any(_reads_an_identifier(value) for value in node.values)
    return False


def _refusal_message(call: ast.Call) -> ast.expr | None:
    """The sentence a person reads: second positional, or the `message` keyword."""
    message = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "message":
            message = keyword.value
    return message


def _refusal_code(call: ast.Call) -> str:
    first = call.args[0] if call.args else None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return "<computed>"


def _called_name(call: ast.Call) -> str:
    function = call.func
    return function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")


def _core_trees() -> list[tuple[str, ast.Module]]:
    trees: list[tuple[str, ast.Module]] = []
    for path in sorted(CORE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="ignore"))
        except SyntaxError:  # pragma: no cover -- a module that does not parse refuses nothing
            continue
        trees.append((path.relative_to(CORE).as_posix(), tree))
    return trees


def _leaks_of(module: str, lineno: int, call: ast.Call) -> list[Leak]:
    message = _refusal_message(call)
    if message is None:
        return []
    return [
        Leak(module, lineno, _refusal_code(call), ast.unparse(part.value))
        for part in ast.walk(message)
        if isinstance(part, ast.FormattedValue) and _reads_an_identifier(part.value)
    ]


def refusals_that_render_an_identifier() -> tuple[Leak, ...]:
    """Every refusal under `server/core/` whose sentence can print an identity."""
    leaks: list[Leak] = []
    for module, tree in _core_trees():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            if not _called_name(node.exc).endswith("Refused"):
                continue
            leaks.extend(_leaks_of(module, node.lineno, node.exc))
    return tuple(leaks)


def refusal_values_that_render_an_identifier() -> tuple[Leak, ...]:
    """The same question asked of the refusals that are RETURNED, not raised.

    `Refusal(...)` and `VisualizationRefusal(...)` are frozen records appended to
    a list and handed back inside a 200. Every one of them is `(code, message,
    ...)`, so the message is found exactly as it is in a raise -- and a person
    reads it in exactly the same panel.
    """
    leaks: list[Leak] = []
    for module, tree in _core_trees():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _called_name(node).endswith("Refusal")):
                continue
            leaks.extend(_leaks_of(module, node.lineno, node))
    return tuple(leaks)


#: THE MODULES THE 2026-08-21 AMENDMENT BINDS: the cross-source plan and the
#: executor that runs it. `visualization-and-rendering.md` states the rule for
#: the analysis surface, and these two are the whole of it. They are held at
#: ZERO -- no baseline, no accepted site, nothing to grow back into.
BOUND_MODULES = ("multi_source_plan.py", "multi_source_execution.py")

#: WHAT THE SWEEP FINDS ELSEWHERE, and why it is not silently accepted.
#:
#: This is a RATCHET, not a list of what to look for: the walk above finds every
#: refusal in `server/core/` on its own, and anything it finds that is not
#: written here fails the test below. These four are outside the amendment's
#: surface and each needs a read that does not exist yet, so closing them is a
#: separate repair rather than a deletion that would leave a blank where a word
#: belongs. Every one is keyed by its REFUSAL CODE, never by its line: a line
#: moves with the next edit, a code is the identity of the refusal.
ACCEPTED_ELSEWHERE = {
    ("analyze_artifacts.py", "placeholder_pin"): (
        "not a leak, and measured as one: the branch is `version_id.lower() in "
        "_FORBIDDEN_PIN_VALUES`, so the only values that reach the sentence are "
        "`legacy`, `current`, `deferred`, `latest`, `unknown`, `none`. A minted "
        "identifier can never be printed here."
    ),
    ("answerable_topics.py", "unknown_query_spec_version"): (
        "2026-08-21 -- prints the `qsv_<ULID>` the caller itself sent. Closing it "
        "means the refusal naming the Query Spec instead, and the read that would "
        "give it a word is the one that just returned nothing."
    ),
    ("mdm_common_keys.py", "component_duplicated"): (
        "2026-08-21 -- prints a `mdm_<ULID>` the caller sent. The vocabulary read "
        "that would name it (`list_visible_canonical_fields`) happens AFTER this "
        "check, deliberately: a caller of another project must not learn that an "
        "id exists elsewhere."
    ),
    ("mdm_common_keys.py", "component_not_found"): (
        "2026-08-21 -- same refusal, the other branch. The field is absent from "
        "the visible catalog by definition, so there is no word to serve for it; "
        "the repair is a sentence that names the gesture without the id at all."
    ),
    ("result_shapes.py", "invalid_micros"): (
        "2026-08-21 -- prints a Result column key (`m_mdm_<ULID>`). This is a "
        "contract violation between two of our own layers, not a person's mistake, "
        "and no ratified document says what a reader should see instead."
    ),
    ("result_shapes.py", "inconsistent_total"): (
        "2026-08-21 -- same module, same column key, same open question."
    ),
}


#: THE DECREASING RATCHET over the refusals that are RETURNED. Measured
#: 2026-09-01 by `refusal_values_that_render_an_identifier()` itself: 25 sites,
#: 16 identities, in four modules. `semantic_model.py` is NOT here -- its four
#: sites were repaired the day this walk was written, which is what a ratchet
#: with a numerator is for.
#:
#: Keyed by `(module, refusal code)`, never by line: a line moves with the next
#: edit, a code is the identity of the refusal. Striking an entry is FREE and is
#: how a repair is recorded; adding one is an admission that a new sentence
#: prints an id at a reader, and the test below refuses it.
REFUSAL_VALUES_AT_2026_09_01: frozenset[tuple[str, str]] = frozenset(
    {
        # Two renderer pins printed back into the artifact's own refusal. The
        # `renderer_build_id` is a HOST-supplied string, not a minted id, and
        # what a reader should see instead is an open question for the host
        # contract rather than for this file.
        ("analyze_artifacts.py", "missing_pin"),
        ("analyze_artifacts.py", "renderer_pin_disagrees"),
        # The Golden Question author's document pins domains, classifications,
        # Semantic View versions and Query Spec versions by id. Naming them
        # needs a read of each pinned head -- four different catalogues, none of
        # them read in this validator today.
        ("golden_questions.py", "duplicate_reference_path"),
        ("golden_questions.py", "moving_version_pin"),
        ("golden_questions.py", "unknown_business_classification"),
        ("golden_questions.py", "unknown_business_domain_version"),
        ("golden_questions.py", "unknown_reference_path"),
        ("golden_questions.py", "unknown_semantic_view_version"),
        # The Query Spec validator refuses members by `member_id`. The word for
        # a member lives in the pinned Semantic View, and the branch that
        # refuses is precisely the one where that lookup returned nothing.
        ("query_specs.py", "duplicate_member"),
        ("query_specs.py", "incompatible_pair"),
        ("query_specs.py", "sort_not_selected"),
        ("query_specs.py", "unknown_member"),
        ("query_specs.py", "version_mismatch"),
        # Same shape one layer up: the visualization validator names wells by
        # the member id the Builder sent, and the Query Spec mismatch prints
        # three ids at once.
        ("visualization_specs.py", "cardinality_over_limit"),
        ("visualization_specs.py", "query_spec_mismatch"),
        ("visualization_specs.py", "unknown_member"),
    }
)


def test_no_returned_refusal_starts_rendering_an_identifier():
    """The ratchet over `Refusal(...)`, and it turns ONE WAY.

    `semantic_model.py:775` was the twin of a `raise` this file had already
    caught, and it stood green because the walk only read raises. Now both are
    read. A NEW returned refusal that pipes an id into its sentence fails here
    on the run that writes it; a baselined one that gets repaired must be struck
    from the frozenset above, because the assertion is an equality in both
    directions and a stale entry is an exemption outliving its reason.
    """
    found = {(leak.module, leak.code) for leak in refusal_values_that_render_an_identifier()}

    appeared = sorted(found - REFUSAL_VALUES_AT_2026_09_01)
    assert not appeared, (
        "these returned refusals render an identifier at a reader and are not baselined:\n  "
        + "\n  ".join(
            str(leak)
            for leak in refusal_values_that_render_an_identifier()
            if (leak.module, leak.code) in set(appeared)
        )
    )

    repaired = sorted(REFUSAL_VALUES_AT_2026_09_01 - found)
    assert not repaired, (
        "these no longer render an identifier -- strike them from "
        f"REFUSAL_VALUES_AT_2026_09_01 so the ratchet cannot slip back: {repaired}"
    )


def test_no_returned_refusal_of_the_semantic_model_names_an_identifier():
    """`semantic_model.py` is held at ZERO -- no baseline, nothing to grow into.

    The four sites repaired on 2026-09-01 were the write door of the Semantic
    View: the version a View pins, and the two Datastream bindings. Each now
    names the Concept or the Datastream by the word it is known by, and the
    gesture that repairs.
    """
    leaked = [
        leak
        for leak in refusal_values_that_render_an_identifier()
        if leak.module == "semantic_model.py"
    ]
    assert not leaked, "these refusals render an identifier at a reader:\n  " + "\n  ".join(
        str(leak) for leak in leaked
    )


def test_the_walk_over_returned_refusals_finds_them_at_all():
    """A second walk that matches nothing would baseline nothing and prove nothing."""
    total = sum(
        1
        for _module, tree in _core_trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node).endswith("Refusal")
    )
    assert total > 300, f"the walk found {total} returned refusals: the call sites moved"


def test_no_refusal_of_the_cross_source_surface_names_a_field_by_its_identifier():
    """THE AMENDMENT, swept over both modules -- the plan AND the executor.

    The plan's refusals were repaired on 2026-08-21 and the executor's were not
    looked at, because the instrument that measured them only ever ran on the
    plan. One of eleven leaked, and it leaked into `manifest.refusal.message` of
    a persisted Result. There is no scenario to write here and none to forget:
    the walk finds every refusal of both modules by itself.
    """
    leaked = [leak for leak in refusals_that_render_an_identifier() if leak.module in BOUND_MODULES]
    assert not leaked, "these refusals render an identifier at a reader:\n  " + "\n  ".join(
        str(leak) for leak in leaked
    )


def test_no_refusal_anywhere_in_core_starts_rendering_an_identifier():
    """The ratchet over every other refusal of `server/core/`.

    A new refusal that pipes an id into its sentence fails here on the run that
    introduces it, in whatever module it was written, with no scenario and no
    registration. What is already accepted is written above WITH ITS REASON, and
    an accepted site that gets repaired must be struck from that table -- the
    assertion is an equality, so a stale entry is as loud as a new leak.
    """
    found = {(leak.module, leak.code) for leak in refusals_that_render_an_identifier()}
    accepted = set(ACCEPTED_ELSEWHERE)

    appeared = sorted(found - accepted)
    assert not appeared, (
        "these refusals render an identifier at a reader and are not accepted anywhere: "
        f"{appeared}"
    )

    repaired = sorted(accepted - found)
    assert not repaired, (
        "these no longer render an identifier -- strike them from ACCEPTED_ELSEWHERE so "
        f"the ratchet cannot slip back: {repaired}"
    )


def test_the_walk_finds_refusals_at_all():
    """A walk that quietly matches nothing proves nothing about the modules it read."""
    total = 0
    for path in sorted(CORE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="ignore"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                function = node.exc.func
                name = (
                    function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
                )
                if name.endswith("Refused"):
                    total += 1
    assert total > 400, f"the walk found {total} refusals: the raise sites moved"
