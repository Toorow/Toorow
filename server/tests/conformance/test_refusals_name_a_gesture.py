"""Chantier A -- a refusal names the GESTURE that repairs it.

WHAT WAS MEASURED, and why the gate is narrow rather than repository-wide.
Scanning every literal returned in a refusal position across `server/core`:
**2 806 sentences, and 7 contain the name of a table** -- of which five name a
REQUEST PARAMETER the caller itself sent (`connection_ref_id`) and two are machine
codes, not sentences. So the product does not have a widespread habit of handing
people schema names, and a repository-wide ban would be ceremony over a number
that is already ~0.

The real gap is different, and it is the one Jean named: a refusal that states a
STATE instead of a GESTURE. "no active binding pins these members to a Datastream"
is true, and leaves a reader with nothing to do. That cannot be regexed in
general -- but it can be held exactly where a caller most often meets it, which is
the executor: every `unavailable_reason` a query can come back with.

So this gate covers `core.query_execution` completely, and -- since 2026-08-25 --
`core.report_chain`, whose route was the audit's M3/M5: a 500 that handed back
`Database error: <exception>` and four sentences half in French. It is a ratchet,
not a survey: the day another module's refusals are rewritten the same way, its
name is added here.

`core.connections_api` joined on 2026-08-31, and it is STEP 1 of
`docs/product-architecture/first-figure-path.md` -- the first door of the whole
path, where a person meets a refusal before they have any of the vocabulary the
later steps teach. Measured before the rewrite: 55 literal refusals, **51 of them
naming no gesture at all** ("Bearer token required", "project_id is required",
"Account discovery failed."), plus one -- the 429 of `refresh-health` -- that
carried no sentence whatsoever. Two of the four that passed passed by ACCIDENT,
the allow-list matching a verb inside a statement ("does not *declare* an account
topology", "does not *open* that tool): a mechanical check buys a floor, never a
reading, and both were rewritten to say what to do instead of merely containing a
verb.

TWO REFUSAL SHAPES, one reader. The executor hands a caller an
`unavailable_reason`; a route hands one a JSON body `{"code": ..., "message":
...}`. Both are the same act, so `message` is read as a refusal sentence -- but
only where a `code` sits beside it in the same dict, because that pairing IS the
envelope. A bare `message` key elsewhere is somebody else's word.

Two rules, both mechanical:

* the sentence contains an IMPERATIVE the reader can act on -- an allow-list of
  verbs, not a heuristic;
* the sentence names no table of the schema. The machine-readable `missing_link`
  code may and does -- it is for a log, not for a person.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[2]
_REPO = _SERVER.parent

#: The modules whose refusal sentences are held. Grows one module at a time, as
#: each is rewritten -- a list that grew by wishing would go red on arrival.
_COVERED = ("query_execution.py", "report_chain.py", "connections_api.py")

#: A sentence names a gesture when it tells the reader to do something. An
#: allow-list, because "does this read as actionable" is not a judgement a test
#: can make -- but "does it contain an instruction" is.
_IMPERATIVES = (
    "open ", "run ", "re-run ", "republish", "publish ", "map it", "choose ",
    "declare ", "name ", "bind ", "add ", "narrow ", "state ", "write ",
    "arm ", "pick ", "finish ", "wait for", "ask ", "enter ", "call ",
)


def _schema_tables() -> set[str]:
    """Table names distinctive enough to be looked for inside a sentence."""
    text = (_REPO / "SCHEMA.md").read_text(encoding="utf-8")
    return {
        name
        for name in re.findall(r"^\| `([a-z_]+)`", text, re.M)
        if len(name) >= 8 and "_" in name
    }


def _refusal_sentences(path: Path) -> list[tuple[int, str]]:
    """Every literal refusal sentence this module can hand back.

    Two shapes, because a module refuses in two places: `unavailable_reason` (the
    executor's answer to a question it could not run) and the `message` of a
    `{"code": ..., "message": ...}` route body. `message` counts only when a
    `code` shares the dict -- that pairing is the refusal envelope, and a lone
    `message` key is not.

    Read from the AST rather than by grep: an f-string built over several lines
    is one node here and three unrelated lines to a line-based search.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        literal_keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant):
                continue
            is_refusal = key.value == "unavailable_reason" or (
                key.value == "message" and "code" in literal_keys
            )
            if not is_refusal:
                continue
            text = _flatten(value)
            if text:
                found.append((getattr(value, "lineno", node.lineno), text))
    return found


def _flatten(node: ast.AST) -> str:
    """The literal text of a constant, an f-string or a concatenation; else ''.

    A non-literal (`str(exc)`, `plan["unavailable_reason"]`) returns '' and is
    skipped -- it carries a sentence this module did not author, and holding it
    here would be holding another owner's words.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten(node.left) + _flatten(node.right)
    return ""


def _covered_files() -> list[Path]:
    return [_SERVER / "core" / name for name in _COVERED]


def test_the_covered_modules_exist_and_carry_refusals():
    """A gate whose subject disappeared passes by vacuity. This is the guard."""
    total = 0
    for path in _covered_files():
        assert path.is_file(), f"{path.name} is covered by this gate and does not exist"
        count = len(_refusal_sentences(path))
        # A PER-MODULE FLOOR, not only an aggregate: one module could empty out
        # while another's refusals kept the total above the bar, and the gate
        # would still read green over a module it no longer holds.
        assert count, f"{path.name} is covered by this gate and carries no refusal the reader sees"
        total += count
    assert total >= 8, f"only {total} literal refusals found; the reader is wrong"


@pytest.mark.parametrize("path", _covered_files(), ids=lambda p: p.name)
def test_every_refusal_of_a_covered_module_names_a_gesture(path):
    silent = [
        f"{path.name}:{lineno} -- {text}"
        for lineno, text in _refusal_sentences(path)
        if not any(verb in text.lower() for verb in _IMPERATIVES)
    ]
    assert not silent, (
        "these refusals state a situation and never say what to do about it.\n"
        "A person who reads one of them has to guess, and guessing is how a "
        "working Semantic View gets rebuilt from scratch:\n  "
        + "\n  ".join(silent)
    )


@pytest.mark.parametrize("path", _covered_files(), ids=lambda p: p.name)
def test_no_refusal_of_a_covered_module_hands_back_a_table_name(path):
    tables = _schema_tables()
    offenders = [
        f"{path.name}:{lineno} names `{name}`"
        for lineno, text in _refusal_sentences(path)
        for name in sorted(tables)
        if name in text
    ]
    assert not offenders, (
        "a table name is our vocabulary, not the reader's. The machine-readable "
        "`missing_link` may carry it; the sentence may not:\n  "
        + "\n  ".join(offenders)
    )
