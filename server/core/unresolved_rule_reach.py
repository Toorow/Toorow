"""How far a repair rule reaches, before anyone confirms it.

`unresolved-values.md` S3, point 3: the drawer shows the match mode and, under
it, live -- *"this rule also matches 42 of the 128 remaining values"*, list
foldable. The document says why in one line: **that sentence is what turns 200
pairs into one rule, and it must be visible BEFORE the confirmation, not
after.** A person who confirms a `contains` rule without it is guessing at the
blast radius of their own click.

WHY A MODULE AND NOT A BRANCH IN THE ROUTE. The reach is a pure question about a
list of strings -- no connection, no warehouse, no project. Kept pure, it is
testable against the pathological inputs that matter (a regex that will not
compile, a pattern that matches everything, a value that is not repairable by a
pair at all) without standing a database up. The route below it does one thing:
read the set the panel already read, and hand it here.

TWO REFUSALS ARE BUILT IN, and both would otherwise be silent over-promises.

  * **`absent_at_source` is never counted.** `action_for` types each unresolved
    value, and a value absent at the source carries `pair_editor: false` -- no
    pair can repair it, because there is nothing to pair. Counting it in the
    reach would advertise a rule that repairs rows it cannot touch.
  * **A regex is bounded or it is refused.** An unbounded pattern typed into a
    drawer runs on the server. The pattern is length-capped, the corpus is
    capped, and a pattern that will not compile comes back as a named refusal
    rather than a 500.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

#: The four modes `unresolved-values.md` S3 names, in the order it names them.
#: `exact_ci` is the wire spelling of the document's "exact (case-insensitive)".
MATCH_MODES: tuple[str, ...] = ("exact", "exact_ci", "contains", "regex")

#: Labels for the screen, so the drawer never invents a second wording for a
#: mode the server already named.
MATCH_MODE_LABELS: dict[str, str] = {
    "exact": "exact",
    "exact_ci": "exact (case-insensitive)",
    "contains": "contains",
    "regex": "regex",
}

#: A pattern longer than this is refused before it is compiled. The longest
#: legitimate source value seen in the unresolved reads is a campaign name; 512
#: is generous for one and far below what a pathological pattern needs.
MAX_PATTERN_LENGTH = 512

#: How many values one reach may examine. The panel's own read is already
#: truncated (`answer["truncated"]`), so this cap is the second, not the first --
#: it exists so a project-wide sweep cannot turn a keystroke into a long scan.
MAX_CORPUS = 5000

#: How many matched values travel back for the foldable list. The COUNT is exact
#: over the whole corpus; only the sample is cut, and it says so.
MAX_SAMPLE = 50


class InvalidRule(ValueError):
    """A rule that cannot be compiled, carrying the sentence the screen shows."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def compile_rule(pattern: str, mode: str):
    """Return a predicate for *pattern* under *mode*, or raise `InvalidRule`.

    Every refusal names the gesture that repairs it, because this text lands in
    a drawer under the person's cursor and "invalid pattern" names nothing.
    """
    if mode not in MATCH_MODES:
        raise InvalidRule(
            "unknown_match_mode",
            f"{mode!r} is not a match mode. Choose one of: "
            + ", ".join(MATCH_MODE_LABELS[m] for m in MATCH_MODES)
            + ".",
        )
    if not pattern:
        raise InvalidRule(
            "empty_pattern",
            "The rule has nothing to match on. Type the value, or the part of it "
            "the rule should recognise.",
        )
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise InvalidRule(
            "pattern_too_long",
            f"The rule is {len(pattern)} characters long and the limit is "
            f"{MAX_PATTERN_LENGTH}. Shorten it, or use `contains` on the part "
            "that identifies the value.",
        )

    if mode == "exact":
        return lambda value: value == pattern
    if mode == "exact_ci":
        folded = pattern.casefold()
        return lambda value: value.casefold() == folded
    if mode == "contains":
        return lambda value: pattern in value
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise InvalidRule(
            "pattern_does_not_compile",
            f"The regular expression does not compile ({exc}). Fix it, or switch "
            "the match mode to `contains`, which takes plain text.",
        ) from None
    return lambda value: compiled.search(value) is not None


def _repairable(row: Mapping[str, Any]) -> bool:
    """A value a PAIR can repair -- `absent_at_source` cannot, and says so."""
    action = row.get("action")
    if isinstance(action, Mapping):
        return bool(action.get("pair_editor"))
    # A row that carries no typing is not assumed repairable: the typing is what
    # `action_for` exists to state, and its absence is a missing answer.
    return False


def rule_reach(
    values: Iterable[Mapping[str, Any]],
    *,
    pattern: str,
    mode: str,
    exclude: str | None = None,
) -> dict[str, Any]:
    """What *pattern* under *mode* would also repair, besides *exclude*.

    *values* are the rows of an unresolved read (`_value` shape: `source_value`,
    `occurrences`, `action`). *exclude* is the value the drawer was opened on --
    the document's sentence is about the OTHERS ("this rule ALSO matches"), so
    counting the one already on screen would inflate every number by one.

    The answer never raises on a bad pattern: it comes back with `valid: False`
    and the sentence, so the drawer keeps its shape while the person types.
    """
    corpus = [
        row
        for row in values
        if _repairable(row) and isinstance(row.get("source_value"), str)
    ]
    truncated = len(corpus) > MAX_CORPUS
    if truncated:
        corpus = corpus[:MAX_CORPUS]

    candidates = [row for row in corpus if row.get("source_value") != exclude]

    try:
        predicate = compile_rule(pattern, mode)
    except InvalidRule as exc:
        return {
            "valid": False,
            "reason": exc.reason,
            "message": exc.message,
            "mode": mode,
            "mode_label": MATCH_MODE_LABELS.get(mode),
            "pattern": pattern,
            # NOT ZERO. The rule did not match nothing -- it was never run. A
            # zero here would read as "this rule is safe, it touches nothing".
            "also_matches": None,
            "remaining": len(candidates),
            "occurrences": None,
            "sample": [],
            "sample_truncated": False,
            "matched_values": [],
            "corpus_truncated": truncated,
        }

    matched = [row for row in candidates if predicate(str(row["source_value"]))]
    occurrences = sum(int(row.get("occurrences") or 0) for row in matched)
    return {
        "valid": True,
        "reason": None,
        "message": None,
        "mode": mode,
        "mode_label": MATCH_MODE_LABELS[mode],
        "pattern": pattern,
        "also_matches": len(matched),
        "remaining": len(candidates),
        "occurrences": occurrences,
        "sample": [
            {
                "source_value": row["source_value"],
                "occurrences": row.get("occurrences"),
            }
            for row in matched[:MAX_SAMPLE]
        ],
        "sample_truncated": len(matched) > MAX_SAMPLE,
        # THE WHOLE LIST, BECAUSE THE STORE HOLDS PAIRS AND NOT RULES.
        # The value-mapping entry store (owned by `value_mapping_tables`, which is
        # the only module allowed to name it) has one column for `source_value` and no
        # match mode (migration 235:117) -- so a `contains` rule is not stored as
        # a rule, it is UNFOLDED into one pair per value at the confirmation. The
        # screen needs every matched value to compose that write; `sample` is for
        # reading and is cut at 50, this is for writing and is not.
        "matched_values": [str(row["source_value"]) for row in matched],
        "corpus_truncated": truncated,
    }


def sentence(reach: Mapping[str, Any]) -> str:
    """The document's own sentence, composed once so two screens cannot differ.

    `unresolved-values.md` S3.3 writes it as *"this rule also matches 42 of the
    128 remaining values"*. The plural and the zero case are handled here rather
    than in JSX, because a count of one rendered as "1 values" is the kind of
    thing a screen ships and nobody files.
    """
    if not reach.get("valid"):
        return str(reach.get("message") or "")
    also = int(reach["also_matches"])
    remaining = int(reach["remaining"])
    if remaining == 0:
        return "This is the only value left to repair on this dimension."
    if also == 0:
        return f"This rule matches this value only, and none of the {remaining} others."
    # L'ACCORD PORTE SUR `remaining`, PAS SUR `also`. Le document ecrit "42 of
    # the 128 remaining values" : le nom qualifie le total restant, pas le
    # sous-ensemble. Accorde sur `also`, la phrase rend "1 of the 3 remaining
    # value" -- mesure le 2026-08-22, et c'est exactement le "1 values" que le
    # docstring plus haut disait vouloir eviter, dans l'autre sens.
    noun = "value" if remaining == 1 else "values"
    return f"This rule also matches {also} of the {remaining} remaining {noun}."
