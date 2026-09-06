"""toorow -- Judged candidates become ONE step and ONE cited line (Story 54.2).

Why a shared module rather than two call sites
----------------------------------------------
Story 54.2 has two halves that judge candidates: the knowledge-tree walk
(`core.context_search`) and the business-event pairing (`core.briefing`,
`core.anomaly_alerts`). Both must say the same four things -- what was reached,
what became of it, on what basis, and what was *not* examined -- and both must say
them in the same words. Two sites that compose that sentence separately drift, and
the drift is invisible until someone reads both; that is exactly the failure
`narrative.GUIDANCE_FRAME` was extracted for in Story 53.5.

So this module owns:

* :func:`candidate_detail` -- the flattening of judged candidates into the closed,
  scalar-only shape `ai_path_recorder.sanitize_detail` accepts;
* :func:`emit_candidates` -- the ONE crossing on which those candidates travel,
  posted through the Story 54.1 seam (`emit_step_sync`). No second seam, no second
  middleware, no second notification channel;
* :func:`cited_fate_line` -- the same facts on the text channel, built ONLY from
  enumerated constants and counts;
* :func:`compose_text_channel` -- how a cited line and model-facing prose share one
  channel without becoming indistinguishable, using the AD-9 frame **imported**
  from `core.narrative`.

The retrieval descriptor rides the SAME crossing as the candidates
-----------------------------------------------------------------
Never beside them. `reached_count` re-labelled "candidates evaluated", with no
statement of what the retriever *is*, is precisely the sentence this story exists
to remove: over a lexical one-hop walk it tells the reader a semantic sweep
happened. :func:`candidate_detail` therefore refuses to build a payload without a
descriptor, and :func:`cited_fate_line` names the mode and the depth before it
names a single count.

Three fates, and the third one is the point
-------------------------------------------
`core.candidate_fate` owns the vocabulary; nothing here re-types a value from it.
A candidate reached and kept, a candidate reached and dropped with an enumerated
reason, and a node **never reached** are three different facts. The third is not
enumerated by anyone -- enumerating it would mean walking what the walk did not
walk -- so it is *declared*, once, as ``not_reached_enumerated`` in the descriptor.
An absence that is not declared reads as exhaustiveness.

No causation, ever
------------------
`analyze-and-test.md:267`. "This context covers the same date and the same
connector" is a fact; "this drop is due to this event" is not. Every string this
module composes is assembled from constants, integers and values that already
passed `candidate_fate.is_rejection_reason` -- a free-text reason cannot reach the
channel because it is dropped before the line is built, not filtered after.
:data:`CAUSAL_MARKERS` pins the vocabulary a test scans for.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from typing import Any, Mapping, Sequence

from core import candidate_fate
from core.ai_path_recorder import (
    DETAIL_MAX_KEYS,
    DETAIL_MAX_LIST_ITEMS,
    DETAIL_VALUE_MAX_CHARS,
    emit_step_sync,
    observation_is_recorded,
)
from core.narrative import GUIDANCE_FRAME

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# What a crossing is, in the vocabulary already persisted
# ---------------------------------------------------------------------------

#: The recorded kind of a step that read governed knowledge. One of
#: ``core.ai_paths.STEP_KINDS`` (pinned by a test) -- nothing new is persisted and
#: no migration is added: `ai_path_recorder.level_of` derives the display rung
#: from this kind plus the object type it reached.
STEP_KIND_KNOWLEDGE_READ = "knowledge_read"

#: Where the knowledge tree and the business events live, in the vocabulary of
#: ``core.ai_paths.OWNER_WORKSPACES``.
OWNER_WORKSPACE_CONTEXT_HUB = "context-hub"

#: Object types a crossing can report having reached. ``procedure`` is what makes
#: `level_of` classify a crossing as PROCEDURE rather than CONTEXT -- the two
#: share a step kind, which is why the object type must travel with it.
OBJECT_TYPE_PROCEDURE = "procedure"
OBJECT_TYPE_TOPIC = "topic"
OBJECT_TYPE_SCHEMA_DOC = "schema_doc"
OBJECT_TYPE_CONTEXT_EVENT = "context_event"

#: THE COLUMN'S VOCABULARY (AI-376). A candidate's `kind` is the detail vocabulary
#: (`topic`, `procedure`, `schema_doc`, `context_event`; stored in JSON, pinned by
#: content hashes). The OWNER object type of a step is the column's:
#: `ai_path_steps.owner_object_type ~ '^[a-z][a-z0-9-]{2,60}$'` -- hyphens, never
#: underscores. Two kinds carried an underscore straight into the column and the
#: INSERT was refused: every briefing Event crossing and every search that kept a
#: schema document were swallowed (« observation never breaks the observed ») and
#: the Event branch of the overlay could never be reached from the store.
OWNER_OBJECT_TYPE_FOR_KIND = {
    OBJECT_TYPE_SCHEMA_DOC: "schema-doc",
    OBJECT_TYPE_CONTEXT_EVENT: "context-event",
}


def owner_object_type_for(kind: str | None) -> str | None:
    """The owner object type the column accepts for a candidate kind (identity for the others)."""
    if kind is None:
        return None
    return OWNER_OBJECT_TYPE_FOR_KIND.get(kind, kind)

BRANCH_SCHEMA_VERSION = "retrieval-branch-detail.v1"
BRANCH_STATE_UNAVAILABLE = "unavailable"
PRODUCER_CONTEXT_SEARCH = "context_search"
PRODUCER_BRIEFING_CONTEXT_EVENT = "briefing_context_event"
PRODUCERS = (PRODUCER_CONTEXT_SEARCH, PRODUCER_BRIEFING_CONTEXT_EVENT)

TOOL_SEARCH_CONTEXT = "search_context"
TOOL_BRIEFING_CONTEXT_EVENT = "briefing_context_event"

_PRODUCER_KINDS = {
    PRODUCER_CONTEXT_SEARCH: frozenset(
        {OBJECT_TYPE_TOPIC, OBJECT_TYPE_PROCEDURE, OBJECT_TYPE_SCHEMA_DOC}
    ),
    PRODUCER_BRIEFING_CONTEXT_EVENT: frozenset({OBJECT_TYPE_CONTEXT_EVENT}),
}
_PRODUCER_REASONS = {
    PRODUCER_CONTEXT_SEARCH: frozenset(candidate_fate.TREE_WALK_REASONS),
    PRODUCER_BRIEFING_CONTEXT_EVENT: frozenset(
        candidate_fate.BRIEFING_CONTEXT_EVENT_REASONS
    ),
}
_CANDIDATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")


# ---------------------------------------------------------------------------
# The detail shape
# ---------------------------------------------------------------------------

#: Per-candidate fields carried on a crossing. ``score`` / ``tier`` / ``matched``
#: are ``None`` when the walk that produced the candidate has no such notion (the
#: business-event pairing does not score), and ``None`` means *not applicable*,
#: never *zero*.
CANDIDATE_FIELDS = (
    "id",
    "kind",
    "title",
    "score",
    "tier",
    "matched",
    "rank",
    "fate",
    "reason",
)

#: One parallel list per field: `sanitize_detail` accepts scalars and lists of
#: scalars, and refuses a list of dicts. Parallel lists keep every candidate's
#: fields aligned by index without inventing a nested shape the seam would drop.
_LIST_KEY = {field: f"candidate_{field}s" for field in CANDIDATE_FIELDS}
_LIST_KEY["matched"] = "candidate_matched"

#: `sanitize_detail` DROPS a list longer than this **whole**, silently. So the
#: candidate lists are cut here, deliberately and visibly: ``candidates_listed``
#: states how many travelled and ``reached_count`` stays exact, so a reader can
#: always tell a truncated listing from a short walk.
CANDIDATE_LIST_LIMIT = DETAIL_MAX_LIST_ITEMS

#: A node's title is a recorded name, not copied content. The public contract is
#: stricter than the generic seam: an overlong value makes the producer detail
#: unavailable rather than clipping evidence or dropping one parallel list.
TITLE_MAX_CHARS = min(200, DETAIL_VALUE_MAX_CHARS)

#: Vocabulary that turns an observation into a claim about cause. Pinned here so
#: the test that forbids it and the code that must avoid it read the same list.
CAUSAL_MARKERS = (
    "caused by",
    "cause of",
    "due to",
    "because",
    "as a result of",
    "results from",
    "led to",
    "leads to",
    "explains",
    "explained by",
    "thanks to",
    "en raison de",
    "a cause de",
    "cause par",
)


def _normalized_string(value: Any, *, title: bool = False) -> str:
    """Return one safe NFC string, refusing controls before normalization."""
    if not isinstance(value, str):
        raise ValueError("branch strings must be strings")
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ValueError("branch strings cannot contain Unicode controls")
    text = unicodedata.normalize("NFC", value)
    if title:
        text = " ".join(text.split())
        if not text or len(text) > TITLE_MAX_CHARS:
            raise ValueError("candidate title is empty or too long")
    return text


def _non_negative_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _finite_number(value: Any, *, nullable: bool = False) -> int | float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("candidate score must be a number or null")
    if not math.isfinite(value):
        raise ValueError("candidate score must be finite")
    return value


def flatten_descriptor(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a retrieval/pairing descriptor to scalars the seam accepts.

    ``mode`` is renamed ``retrieval_mode`` so the emitted key says what it
    describes, and a nested map (``tiers``) becomes one key per entry rather than
    being dropped by `sanitize_detail` -- losing the tier scale would leave the
    scores on the wire with nothing to read them against.
    """
    flat: dict[str, Any] = {}
    for key, value in (descriptor or {}).items():
        name = "retrieval_mode" if key == "mode" else str(key)
        if isinstance(value, Mapping):
            for sub_key, sub_value in value.items():
                flat[f"{name}_{sub_key}"] = sub_value
        else:
            flat[name] = value
    return flat


def _refuse_if_over_budget(descriptor_keys: Mapping[str, Any], candidate_keys: int) -> None:
    """Raise when the crossing cannot fit the seam, naming both halves.

    The seam drops the TAIL of an over-budget map, and the tail here is the
    candidate lists -- the last of which is `candidate_reasons`. Failing at the
    build tells whoever grew the descriptor what it cost; the alternative was a
    payload that looked whole.
    """
    total = len(descriptor_keys) + candidate_keys
    if total > DETAIL_MAX_KEYS:
        raise ValueError(
            f"a crossing of {total} keys does not fit the seam budget of "
            f"{DETAIL_MAX_KEYS}: {len(descriptor_keys)} describe the retrieval "
            f"and {candidate_keys} carry the candidates. The seam would drop the "
            f"tail -- the candidate lists -- and say nothing."
        )


def _unavailable_detail(producer: str) -> dict[str, Any]:
    return {
        "branch_schema_version": BRANCH_SCHEMA_VERSION,
        "branch_producer": producer,
        "branch_state": BRANCH_STATE_UNAVAILABLE,
    }


def _normalized_descriptor(
    descriptor: Mapping[str, Any], *, producer: str
) -> tuple[dict[str, Any], dict[str, int | float]]:
    if not isinstance(descriptor, Mapping):
        raise ValueError("candidate descriptor must be a mapping")
    mode = " ".join(_normalized_string(descriptor.get("mode")).split())
    if not mode or len(mode) > DETAIL_VALUE_MAX_CHARS:
        raise ValueError("candidate descriptor needs a bounded retrieval mode")
    graph_hop_depth = _non_negative_int(
        descriptor.get("graph_hop_depth"), name="graph_hop_depth"
    )
    limit = _non_negative_int(descriptor.get("limit"), name="limit")
    semantic_recall = descriptor.get("semantic_recall")
    if not isinstance(semantic_recall, bool):
        raise ValueError("semantic_recall must be boolean")
    if descriptor.get("not_reached_enumerated") is not False:
        raise ValueError("v1 producers must not enumerate unreached candidates")

    counts = {
        name: _non_negative_int(descriptor.get(name), name=name)
        for name in ("reached_count", "selected_count", "rejected_count")
    }
    detail: dict[str, Any] = {
        "branch_schema_version": BRANCH_SCHEMA_VERSION,
        "branch_producer": producer,
        "retrieval_mode": mode,
        "graph_hop_depth": graph_hop_depth,
        "semantic_recall": semantic_recall,
        "limit": limit,
        "not_reached_enumerated": False,
        **counts,
    }

    tiers: dict[str, int | float] = {}
    if producer == PRODUCER_CONTEXT_SEARCH:
        raw_tiers = descriptor.get("tiers")
        if not isinstance(raw_tiers, Mapping):
            raise ValueError("context search must declare its tier scale")
        for tier_name in ("title", "description", "neighbor"):
            value = _finite_number(raw_tiers.get(tier_name))
            assert value is not None
            tiers[tier_name] = value
            detail[f"tiers_{tier_name}"] = value
    return detail, tiers


def _normalized_candidate(
    candidate: Mapping[str, Any],
    *,
    producer: str,
    tiers: Mapping[str, int | float],
    previous_rank: int,
) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise ValueError("candidate must be a mapping")
    candidate_id = candidate.get("id")
    if not isinstance(candidate_id, str) or _CANDIDATE_ID.fullmatch(candidate_id) is None:
        raise ValueError("candidate id does not match the closed identifier rule")
    kind = candidate.get("kind")
    if kind not in _PRODUCER_KINDS[producer]:
        raise ValueError("candidate kind is not allowed for this producer")
    title = _normalized_string(candidate.get("title"), title=True)
    rank = _non_negative_int(candidate.get("rank"), name="rank")
    if rank == 0 or rank <= previous_rank:
        raise ValueError("candidate ranks must be positive and strictly increasing")

    fate = candidate.get("fate")
    reason = candidate.get("reason")
    if fate == candidate_fate.FATE_NOT_REACHED:
        raise ValueError("v1 producers cannot list an unreached candidate")
    candidate_fate.validate_fate(fate, reason)
    if fate == candidate_fate.FATE_REJECTED and reason not in _PRODUCER_REASONS[producer]:
        raise ValueError("candidate reason is not allowed for this producer")

    if producer == PRODUCER_CONTEXT_SEARCH:
        score = _finite_number(candidate.get("score"))
        tier = candidate.get("tier")
        if tier not in tiers:
            raise ValueError("context-search tier must belong to its declared scale")
        matched = candidate.get("matched")
        if not isinstance(matched, bool):
            raise ValueError("context-search matched must be boolean")
    else:
        score = candidate.get("score")
        tier = candidate.get("tier")
        matched = candidate.get("matched")
        if score is not None or tier is not None or matched is not None:
            raise ValueError("briefing pairing does not score, tier, or match")

    return {
        "id": candidate_id,
        "kind": kind,
        "title": title,
        "score": score,
        "tier": tier,
        "matched": matched,
        "rank": rank,
        "fate": fate,
        "reason": reason,
    }


def candidate_detail(
    candidates: Sequence[Mapping[str, Any]],
    descriptor: Mapping[str, Any],
    *,
    producer: str,
) -> dict[str, Any]:
    """Build one validated producer detail or its exact unavailable sentinel."""
    if producer not in PRODUCERS:
        raise ValueError(f"unknown branch producer {producer!r}")
    try:
        detail, tiers = _normalized_descriptor(descriptor, producer=producer)
        normalized: list[dict[str, Any]] = []
        previous_rank = 0
        for candidate in list(candidates):
            item = _normalized_candidate(
                candidate,
                producer=producer,
                tiers=tiers,
                previous_rank=previous_rank,
            )
            normalized.append(item)
            previous_rank = item["rank"]

        selected_count = sum(
            item["fate"] == candidate_fate.FATE_SELECTED for item in normalized
        )
        rejected_count = sum(
            item["fate"] == candidate_fate.FATE_REJECTED for item in normalized
        )
        if (
            detail["reached_count"] != len(normalized)
            or detail["selected_count"] != selected_count
            or detail["rejected_count"] != rejected_count
            or len(normalized) != selected_count + rejected_count
        ):
            raise ValueError("candidate counts do not match the judged candidates")

        listed = normalized[:CANDIDATE_LIST_LIMIT]
        detail["candidates_listed"] = len(listed)
        detail["listing_truncated"] = len(normalized) > len(listed)
        for field in CANDIDATE_FIELDS:
            detail[_LIST_KEY[field]] = [candidate[field] for candidate in listed]
        _refuse_if_over_budget(detail, 0)
        return detail
    except (AttributeError, TypeError, ValueError):
        return _unavailable_detail(producer)


# ---------------------------------------------------------------------------
# The crossing itself -- Story 54.1's seam, and only it
# ---------------------------------------------------------------------------


def emit_candidates(
    *,
    candidates: Sequence[Mapping[str, Any]],
    descriptor: Mapping[str, Any],
    producer: str,
    tool_name: str,
    owner_object_type: str | None,
    owner_object_id: str | None = None,
    owner_version_id: str | None = None,
    owner_workspace: str = OWNER_WORKSPACE_CONTEXT_HUB,
    outcome: str | None = None,
) -> bool:
    """Post ONE crossing carrying its candidates and its descriptor. Never raises.

    Returns whether live delivery accepted the crossing. For a Result-bound call
    that means accepted by the non-blocking FIFO, not transport acknowledgement;
    for an unwatched call ``False`` is normal while the crossing is still kept
    for persistence. The `ai_path_recorder.observation_is_recorded` gate avoids
    assembling detail when no call can record it at all.

    The level (``CONTEXT`` / ``PROCEDURE``) is **derived** by
    `ai_path_recorder.level_of` from the step kind and *owner_object_type*, never
    passed in: the reading grid stays in one module.
    """
    if not observation_is_recorded():
        return False
    try:
        from core.ai_paths import OUTCOME_SUCCEEDED  # noqa: PLC0415

        return emit_step_sync(
            step_kind=STEP_KIND_KNOWLEDGE_READ,
            outcome=outcome or OUTCOME_SUCCEEDED,
            tool_name=tool_name,
            owner_workspace=owner_workspace,
            # The column's vocabulary, mapped where the candidate kinds are written (AI-376);
            # the writer itself (`ai_paths.append_step`) refuses any word the column cannot hold.
            owner_object_type=owner_object_type_for(owner_object_type),
            owner_object_id=owner_object_id,
            owner_version_id=owner_version_id,
            detail=candidate_detail(candidates, descriptor, producer=producer),
        )
    except Exception as exc:  # noqa: BLE001 -- observation never breaks the observed
        logger.warning(
            "candidate_emission: crossing not emitted (%s)", type(exc).__name__
        )
        return False


# ---------------------------------------------------------------------------
# The text channel -- cited data on one side of the AD-9 line, prose on the other
# ---------------------------------------------------------------------------


def reason_tally(candidates: Sequence[Mapping[str, Any]]) -> list[tuple[str, int]]:
    """Count rejections per reason, keeping ONLY enumerated reasons.

    This is the guard, not a formatting convenience: a value that is not in
    `candidate_fate.REJECTION_REASONS` -- a sentence a model wrote, say -- is
    dropped here, so it cannot reach the text channel at all. Filtering after
    composition would still have put it in the string once.
    """
    counts: dict[str, int] = {}
    for candidate in candidates or []:
        if candidate.get("fate") != candidate_fate.FATE_REJECTED:
            continue
        reason = candidate.get("reason")
        if not candidate_fate.is_rejection_reason(reason):
            continue
        counts[reason] = counts.get(reason, 0) + 1
    return sorted(counts.items())


def cited_fate_line(
    *,
    subject: str,
    descriptor: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]] = (),
) -> str:
    """One line of CITED DATA: what the walk was, what it judged, what it did not.

    Composed exclusively from *subject*, integers, and constants that already
    belong to a closed vocabulary. It states the mode and the depth **first**,
    because a count of candidates read without them is the misleading half of the
    sentence.
    """
    flat = flatten_descriptor(descriptor)

    shape: list[str] = []
    mode = flat.get("retrieval_mode")
    if mode:
        shape.append(str(mode))
    depth = flat.get("graph_hop_depth")
    if depth is not None:
        shape.append(f"{int(depth)} graph hop" + ("s" if int(depth) != 1 else ""))
    if flat.get("semantic_recall") is False:
        shape.append("no semantic recall")
    cap = flat.get("limit")
    if cap is not None:
        shape.append(f"cap {int(cap)}")

    head = subject if not shape else f"{subject} ({', '.join(shape)})"

    counts: list[str] = []
    # The labels ARE the fate names, taken from the vocabulary rather than
    # re-typed beside it: a line that says one word and a payload that says
    # another describe the same walk twice.
    for key, label in (
        ("reached_count", "reached"),
        ("selected_count", candidate_fate.FATE_SELECTED),
        ("rejected_count", candidate_fate.FATE_REJECTED),
    ):
        if flat.get(key) is not None:
            counts.append(f"{int(flat[key])} {label}")

    tally = reason_tally(candidates)
    tally_part = ""
    if tally:
        tally_part = " [" + ", ".join(f"{reason} x{n}" for reason, n in tally) + "]"

    parts = [f"{head}: {', '.join(counts)}{tally_part}." if counts else f"{head}.{tally_part}"]

    basis = flat.get("basis") or flat.get("pairing_basis")
    if basis:
        parts.append("Basis: " + ", ".join(str(b) for b in basis) + ".")
    unscoped = flat.get("unscoped_dimensions")
    if unscoped:
        parts.append("Not scoped: " + ", ".join(str(u) for u in unscoped) + ".")

    if flat.get("not_reached_enumerated") is False:
        parts.append("Candidates outside the walk were not judged and are not listed.")

    return " ".join(part for part in parts if part).strip()


def compose_text_channel(cited: str, model_guidance: str | None = None) -> str:
    """Put cited data and model-facing prose on ONE channel, still distinguishable.

    AD-9, and the mechanism is the one Story 53.5 already installed: the prose
    side carries `narrative.GUIDANCE_FRAME`, **imported**, and the cited side
    carries nothing -- it is evidence, and dressing evidence as an instruction
    would be the same collapse in the other direction. Re-typing the frame's
    wording here instead of importing it fails
    ``test_candidate_emission.test_the_frame_is_imported_never_retyped``.
    """
    cited = (cited or "").strip()
    guidance = (model_guidance or "").strip()
    if not guidance:
        return cited
    separator = "\n\n" if cited else ""
    return f"{cited}{separator}{GUIDANCE_FRAME}{guidance}"


__all__ = [
    "BRANCH_SCHEMA_VERSION",
    "BRANCH_STATE_UNAVAILABLE",
    "CANDIDATE_FIELDS",
    "CANDIDATE_LIST_LIMIT",
    "CAUSAL_MARKERS",
    "OBJECT_TYPE_CONTEXT_EVENT",
    "OWNER_OBJECT_TYPE_FOR_KIND",
    "owner_object_type_for",
    "OBJECT_TYPE_PROCEDURE",
    "OBJECT_TYPE_SCHEMA_DOC",
    "OBJECT_TYPE_TOPIC",
    "OWNER_WORKSPACE_CONTEXT_HUB",
    "PRODUCER_BRIEFING_CONTEXT_EVENT",
    "PRODUCER_CONTEXT_SEARCH",
    "PRODUCERS",
    "STEP_KIND_KNOWLEDGE_READ",
    "TITLE_MAX_CHARS",
    "TOOL_BRIEFING_CONTEXT_EVENT",
    "TOOL_SEARCH_CONTEXT",
    "candidate_detail",
    "cited_fate_line",
    "compose_text_channel",
    "emit_candidates",
    "flatten_descriptor",
    "reason_tally",
]
