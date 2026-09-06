"""A fake cursor that recognizes SQL by text, told how to do it without lying.

WHY THIS EXISTS. A test double that dispatches on a fragment of the product's
SQL holds a copy of that query. The copy is invisible, and it rots the moment
the product rewrites the statement -- an alias, a join, a reordered projection.
Two things then happen, and neither of them says "the fixture is stale":

1. the fake falls through to a default that answers no rows and, worse,
   ``description = None`` -- the one signal psycopg reserves for a statement
   that returned NO RESULT SET. The product reads it correctly and dies with
   ``TypeError: 'NoneType' object is not iterable``, naming the product.
2. or the fake answers something plausible, the suite stays green, and every
   assertion downstream is about a code path the test never exercised.

Both were measured on ``test_competitor_fan_out.py`` (c9d740cb): ten red tests
that pointed at ``entity_bindings.py`` while the product was untouched. The
census ``scripts/fake_cursor_census.py`` found the class -- 84 textual fakes
under ``server/tests``, most of them mute on a statement they do not know.

WHAT THIS MODULE OFFERS. Three pieces, usable one at a time. It is deliberately
NOT a base class: the fakes in this repository hold wildly different state, and
a mandatory shape would be rewritten around rather than adopted.

``StatementInventory``
    The statements one fake answers, NAMED, in one place. ``match()`` returns
    the name -- or raises with the statement and the inventory in the message.
    An unrecognized statement can no longer be answered by accident.

``describe(sql)``
    The ``description`` psycopg would report, DERIVED from the SELECT. A
    hand-written column tuple is a second copy of the query; it is the copy
    that goes stale first, because nothing reads it.

``UnknownStatement``
    What a fake raises when it is shown a query it was never taught. Raising is
    the point: the failure names the query that moved.
"""

from __future__ import annotations

import re

__all__ = [
    "StatementInventory",
    "UnknownStatement",
    "describe",
    "flatten",
    "projected_columns",
]


class UnknownStatement(AssertionError):
    """A fake cursor was handed a statement it does not recognize.

    An ``AssertionError`` on purpose: this is a failing test, not a product
    error. It must never be caught by a fixture's ``except Exception``.
    """


def flatten(sql: object) -> str:
    """One statement, one line, lower case -- so fragments compare the same way.

    Every fake normalizes before matching, and each one did it slightly
    differently: ``.lower()`` here, ``" ".join(sql.split())`` there,
    ``.strip().upper()`` in a third. A fragment written for one spelling
    silently never matches under another.
    """

    return " ".join(str(sql).split()).lower()


def projected_columns(sql: object) -> list[str]:
    """The column names psycopg would report for a flat, single-level SELECT.

    Only what a fake needs: the projection of a SELECT whose first ``FROM`` is
    its own, with no sub-select and no function call in the list. It exists so
    the fake never restates the product's column list by hand.

    Raises ``UnknownStatement`` rather than guessing when the projection is
    richer than that -- a fake that guesses a column list is back to holding a
    copy of the query.
    """

    flat = flatten(sql)
    match = re.match(r"select (?:distinct )?(.+?) from ", flat)
    if not match:
        # A write can carry a projection too, and psycopg reports it the same
        # way: `INSERT ... RETURNING id` has a description, and a fake that
        # answers `None` there tells the caller the write returned nothing.
        match = re.search(r" returning (.+?)$", flat)
        if not match:
            raise UnknownStatement(f"not a SELECT a fake can project: {flat}")
    projection = match.group(1)
    if "(" in projection:
        raise UnknownStatement(
            "projection too rich to derive -- name the columns in the route: "
            f"{flat}"
        )
    names = []
    for item in projection.split(","):
        expression = item.strip().split(" as ")[-1].strip()
        names.append(expression.rsplit(".", 1)[-1])
    return names


def describe(sql: object) -> list[tuple[str]]:
    """``cursor.description``, derived from the statement. Never spelled out."""

    return [(name,) for name in projected_columns(sql)]


class StatementInventory:
    """The statements one fake answers, named -- and nothing else.

    Declaration order is the order an ``if/elif`` chain would test in: the
    first entry whose every fragment appears in the statement wins. That is
    deliberate, because real statements overlap -- a page and its ``COUNT(*)``
    read the same relation -- and a "must match exactly one" rule would force
    each fragment to restate what the others exclude.

    Usage::

        RUNS = StatementInventory(
            "_RunsCursor",
            total=("from app.datastream_executions", "count(*)"),
            states=("from app.datastream_executions", "distinct state"),
            page="from app.datastream_executions",
        )

        def execute(self, sql, params=None):
            match RUNS.match(sql):
                case "total":
                    ...

    ``match`` raises ``UnknownStatement`` on anything else. That is the whole
    point: the fake stops being able to answer a question it was never asked.
    """

    def __init__(self, owner: str, **statements: str | tuple[str, ...]):
        if not statements:
            raise ValueError(f"{owner}: an inventory with no statement recognizes nothing")
        self.owner = owner
        self.statements: dict[str, tuple[str, ...]] = {}
        for name, fragments in statements.items():
            if isinstance(fragments, str):
                fragments = (fragments,)
            self.statements[name] = tuple(flatten(fragment) for fragment in fragments)

    def find(self, sql: object) -> str | None:
        """The name of the statement, or ``None``. For a fake with a real tail."""

        flat = flatten(sql)
        for name, fragments in self.statements.items():
            if all(fragment in flat for fragment in fragments):
                return name
        return None

    def match(self, sql: object) -> str:
        """The name of the statement. Raises if this fake was never taught it."""

        name = self.find(sql)
        if name is not None:
            return name
        raise self.unknown(sql)

    def unknown(self, sql: object) -> UnknownStatement:
        """The loud refusal, carrying the statement AND what this fake knows.

        The statement is in the message because the reader's next question is
        always "which query moved?", and the inventory is in it because the
        second question is "what did it use to look like?".
        """

        inventory = "\n".join(
            f"    {name}: {' + '.join(fragments)}"
            for name, fragments in self.statements.items()
        )
        return UnknownStatement(
            f"{self.owner} has no answer for this statement:\n"
            f"    {flatten(sql)}\n"
            f"  it recognizes:\n{inventory}\n"
            "  Either the product query moved (fix the fragment), or this is a "
            "statement the fake was never taught (add it -- do not widen an "
            "existing fragment until it swallows both)."
        )

    def __contains__(self, sql: object) -> bool:
        return self.find(sql) is not None

    def __iter__(self):
        return iter(self.statements)

    def __len__(self) -> int:
        return len(self.statements)
