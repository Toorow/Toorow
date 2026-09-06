"""toorow -- What became of a candidate, as a closed vocabulary (Story 54.2, AC2).

Why this module exists
----------------------
Epic 54 streams the walk through the knowledge tree *while it happens*. A walk
that only shows what it kept tells the reader "it found this", which is
indistinguishable from "this is all there was". So each candidate travels with
its **fate**, and a rejected candidate travels with a **reason**.

A reason written by a model is not a reason: it is prose that reads like one.
The set below is therefore **enumerated, declared once, and imported** by every
emitter -- the same discipline `narrative.GUIDANCE_FRAME` established in Story
53.5, for the same cause (two sites that re-type a wording drift, and the drift
is invisible until someone reads both).

`server/tests/core/test_candidate_fate.py` enforces it structurally: the string
literals below may exist in this module and in that test only. A second site
that re-types `"below_cutoff"` fails the suite instead of quietly forking the
vocabulary.

The three fates are three DIFFERENT facts
-----------------------------------------
* ``selected``    -- the retriever retained the node. This does not claim that a
                     model used or cited it.
* ``rejected``    -- the node was reached, scored, and lost. The reason is one
                     of ``REJECTION_REASONS``, never free text.
* ``not_reached`` -- the node was **not judged**. It was never touched by the
                     walk (no lexical match, and further than one graph hop).

The third one is the whole point of the story. Listing a never-reached node as
rejected invents an examination that never took place; omitting the distinction
implies the walk was exhaustive. Neither is true, so both are forbidden.

Who produces which reason -- measured, not assumed
--------------------------------------------------
The vocabulary is shared, but the emitters are not interchangeable. Each reason
below names the emitter that can genuinely produce it, so a surface never draws a
judgement nobody made:

* ``below_cutoff``  -- produced TODAY by the tree walk (`core.context_search`):
  the candidate was ranked and fell past ``limit``. It is the ONLY reason that
  walk can produce; ``TREE_WALK_REASONS`` says so in code.
* ``out_of_scope``  -- deliberately NOT producible by the tree walk. AD-5 scoping
  runs inside the SQL, so another Project's row is never a candidate: it is never
  fetched, never scored, never dropped. Reporting it as "rejected" would both
  invent a judgement and leak the existence of another Project's row. Reserved
  for an emitter that genuinely sees a candidate and then discards it for scope.
* ``date_mismatch`` / ``connector_mismatch`` / ``metric_mismatch`` -- produced
  by the finalized business-event pairing. ``metric_mismatch`` became producible
  on 2026-08-30: migration 322 gave ``app.context_events`` an optional ``metric``
  and ``briefing.context_event_walk`` disqualifies an event naming another one.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Fates
# ---------------------------------------------------------------------------

FATE_SELECTED = "selected"
FATE_REJECTED = "rejected"
FATE_NOT_REACHED = "not_reached"

#: Machine-readable statement of the only meaning ``selected`` carries.  It is
#: deliberately narrower than "used by the model" or "cited in the answer".
SELECTED_MEANING = "retained_by_retriever"

#: Every fate a candidate may carry. Closed set: an emitter that needs a fourth
#: adds it HERE, which forces the discussion at the right moment.
FATES: tuple[str, ...] = (FATE_SELECTED, FATE_REJECTED, FATE_NOT_REACHED)


# ---------------------------------------------------------------------------
# Rejection reasons
# ---------------------------------------------------------------------------

REASON_BELOW_CUTOFF = "below_cutoff"
REASON_OUT_OF_SCOPE = "out_of_scope"
REASON_DATE_MISMATCH = "date_mismatch"
REASON_METRIC_MISMATCH = "metric_mismatch"
REASON_CONNECTOR_MISMATCH = "connector_mismatch"
#: Story 45.5 -- the candidate itself DECLARED that this is not its question.
#: Distinct from ``out_of_scope`` on purpose: that one is what the walk OBSERVES,
#: this one is what the author WROTE in advance. Fusing them would destroy the
#: only comparison that makes either useful -- and would make a wanted drop count
#: as a missing link in the recurring-rejection aggregate.
REASON_ANTI_TRIGGER = "anti_trigger"

#: The closed set. A reason outside it is a bug, not a nuance.
REJECTION_REASONS: tuple[str, ...] = (
    REASON_BELOW_CUTOFF,
    REASON_OUT_OF_SCOPE,
    REASON_DATE_MISMATCH,
    REASON_METRIC_MISMATCH,
    REASON_CONNECTOR_MISMATCH,
    REASON_ANTI_TRIGGER,
)

#: Reasons the knowledge-tree walk (`core.context_search`) genuinely produces.
#: Two today: the cap, and a drop the candidate asked for itself.
TREE_WALK_REASONS: tuple[str, ...] = (REASON_BELOW_CUTOFF, REASON_ANTI_TRIGGER)

#: Reasons that must NEVER reach `app.context_candidate_fates`. The aggregate
#: exists to surface MISSING LINKS; a drop the author declared is the opposite of
#: a missing link, and the table's CHECK constraint refuses the value anyway --
#: which is the honest place for that refusal to live.
UNRECORDED_REASONS: tuple[str, ...] = (REASON_ANTI_TRIGGER,)

#: Reasons reserved for the business-event pairing (Half B). Nothing emits them
#: until Story 53.8 repairs the pairing they would explain.
CONTEXT_EVENT_REASONS: tuple[str, ...] = (
    REASON_DATE_MISMATCH,
    REASON_METRIC_MISMATCH,
    REASON_CONNECTOR_MISMATCH,
)

#: Reasons the finalized briefing context-event producer can actually emit.
#: ``metric_mismatch`` JOINED this list on 2026-08-30: migration 322 gave
#: `app.context_events` a nullable `metric`, so an event that names a different
#: metric than the claim can now be rejected for a field it actually carries.
#: An event that names NO metric is still not rejected -- it is about every
#: metric -- and the pairing descriptor reports the dimension as unscoped
#: instead. ``below_cutoff`` covers an otherwise-qualified event that lost to
#: the one-event retention limit.
BRIEFING_CONTEXT_EVENT_REASONS: tuple[str, ...] = (
    REASON_DATE_MISMATCH,
    REASON_METRIC_MISMATCH,
    REASON_CONNECTOR_MISMATCH,
    REASON_BELOW_CUTOFF,
)


def is_rejection_reason(value: object) -> bool:
    """True when *value* is one of the enumerated reasons (and nothing else)."""
    return isinstance(value, str) and value in REJECTION_REASONS


def validate_fate(fate: str, reason: str | None) -> tuple[str, str | None]:
    """Check a (fate, reason) pair and return it, or raise ``ValueError``.

    The three rules an emitter cannot bend:

    * the fate is one of ``FATES``;
    * ``rejected`` carries a reason, and that reason is enumerated -- this is the
      guard that stops model prose from reaching a surface as a reason;
    * ``selected`` and ``not_reached`` carry NO reason. A selected node needs no
      excuse, and a never-reached node was not judged, so nothing can be said
      about why it lost.
    """
    if fate not in FATES:
        raise ValueError(f"unknown fate {fate!r}; expected one of {FATES}")
    if fate == FATE_REJECTED:
        if not is_rejection_reason(reason):
            raise ValueError(
                f"rejected candidate needs an enumerated reason, got {reason!r}; "
                f"expected one of {REJECTION_REASONS}"
            )
    elif reason is not None:
        raise ValueError(f"fate {fate!r} must carry no reason, got {reason!r}")
    return fate, reason
