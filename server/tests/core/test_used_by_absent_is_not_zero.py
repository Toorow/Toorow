"""An unreadable used-by count is not "nothing depends on this" (AI-239).

Every used-by facet in the Governance read model wrote the same shape:

    _facet("available" if int(row.get(...) or 0) else "empty", count=...)

and `int(x or 0)` gives one answer to two opposite facts. Zero means the store
answered and nothing depends on the object. Absent means nobody asked -- a column
the query did not return, a sub-read that failed. Both arrived on screen as
"nothing depends on this", which is the sentence a person reads just before
moving or deleting the object.

`NodeImpact` (`master_data.py:739-748`) already carries the rule for the same
question one workspace over. `_counted_facet` is that rule as the ONE helper the
sites derive from, and these tests are what stop the next site from writing its
own `or 0` instead.
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from core.governance_read_model import _counted_facet  # noqa: E402

MODULE = pathlib.Path(__file__).resolve().parents[2] / "core" / "governance_read_model.py"


def test_a_count_that_could_not_be_read_is_unavailable() -> None:
    facet = _counted_facet(None)
    assert facet["state"] == "unavailable"


def test_zero_is_a_fact_and_reads_empty() -> None:
    facet = _counted_facet(0)
    assert facet["state"] == "empty"
    assert facet["count"] == 0


def test_a_real_count_reads_available_and_carries_its_number() -> None:
    facet = _counted_facet(4, [{"id": f"c{i}"} for i in range(4)])
    assert facet["state"] == "available"
    assert facet["count"] == 4
    assert [ref["id"] for ref in facet["refs"]] == ["c0", "c1", "c2", "c3"]


def test_a_count_with_nothing_to_list_is_not_available(caplog) -> None:
    """The 49-2 verdict, held as a property of the helper.

    `{"state": "available", "count": 3, "refs": []}` is the state the console
    read as "nothing depends on this object", in the tab a person opens before
    archiving. It cannot be composed any more: a positive count with no list is
    unavailable WITH its count, and it says so in a sentence.
    """
    facet = _counted_facet(3)
    assert facet["state"] == "unavailable"
    assert facet["count"] == 3
    assert facet["refs"] == []
    assert "3 recorded" in facet["reason"]["message"]


def test_absent_and_zero_never_produce_the_same_facet() -> None:
    """The whole point, stated as the comparison the defect failed."""
    assert _counted_facet(None) != _counted_facet(0)


def test_no_used_by_site_coerces_an_absent_count_to_zero() -> None:
    """The class, not the instance: no site may reintroduce `int(... or 0)`.

    Read as source rather than exercised through eleven read paths, because the
    defect is a SHAPE -- and the shape is what the next author copies.
    """
    source = MODULE.read_text(encoding="utf-8")
    offenders: list[str] = []
    for number, line in enumerate(source.splitlines(), 1):
        if "used_by" not in line and "dependent_" not in line:
            continue
        if line.strip().startswith("#"):
            continue
        if re.search(r"int\(\s*row\.get\([^)]*\)\s*or\s*0\s*\)", line):
            offenders.append(f"governance_read_model.py:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_every_used_by_facet_derives_from_the_one_helper_or_is_a_constant() -> None:
    """Calibration + scope.

    Two shapes stay legitimate and are not the defect:

      - a CONSTANT state -- `_facet("unavailable")` for an object whose consumers
        nobody tracks yet, `_facet("empty")` for one that can have none by
        construction. Nothing is being coerced; the fact is declared.
      - a state derived from the REFS the facet carries. A list that came back
        empty is an answer, and the refs beside it are the proof it was asked.
        `_counted_facet` takes no refs, so those sites keep their own shape.

    What may not exist is a site computing the state from a bare COUNT, because
    that is precisely where absent-versus-zero is decided.
    """
    source = MODULE.read_text(encoding="utf-8")
    computed = re.findall(r'used_by=_facet\(\s*\n?\s*"available" if (\w+)([^\n]*)', source)
    offenders = [
        f'{name}{tail}'
        for name, tail in computed
        # A `count=` keyword and no refs positional means the state came from a
        # number alone -- the shape `_counted_facet` exists to replace.
        if "count=" in tail or not tail.strip().endswith(",")
    ]
    assert not offenders, offenders
    # And the helper is genuinely used, so this file is not guarding an empty set.
    assert source.count("_counted_facet(") >= 10
