"""Story 66.4 -- one exact, immutable plan for a cross-Datastream analysis.

WHAT REPLACES WHAT. `query_execution.resolve_physical_plan` refuses a request
that spans several Datastreams by name -- "this request spans several
Datastreams, which needs a compiled join plan that does not exist yet",
`missing_link: dbt_relation_refs`. This module is that plan. It does not widen
the single-source resolver: it produces a SEPARATE, self-describing document so
every Query Spec written before it stays byte-identical (NFR10).

WHAT A PLAN FREEZES, AND WHY EACH PIN IS THERE.

    members            which sources, with the EXACT mapping version read
    edges              which common key version and which published relationship
                       makes each cross executable
    inclusion policy   matched_only | preserve_primary | preserve_all, chosen
                       once and never defaulted at execution
    grain + filters    what is asked, and at which stage each filter applies
    bounds             the caps execution is allowed to spend

A pin that is missing at compile time is a decision execution would have to make
later, alone, with no user in the room. That is the failure this module exists to
prevent: the plan is the moment where every ambiguity is either resolved by a
person or refused.

THE GRAPH IS A TREE, AND THAT IS A REFUSAL NOT A LIMITATION. N members need
exactly N-1 edges, all connected. Fewer and a source hangs unattached -- a
cartesian product wearing an analysis's clothes. More and two paths reach the
same pair, which is the "multiple equally valid paths" the epic requires the USER
to resolve; the compiler names them and refuses to pick.

NOTHING FROM THE CLIENT BECOMES AUTHORITY. No SQL, no expression, no relation
name, no column. A member is a Datastream id whose mapping version must be the
one Data currently publishes, and a measure is a canonical field id that mapping
actually binds. Anything else is refused by name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from ulid import ULID

from core import business_identity_catalogue as catalogue

logger = logging.getLogger(__name__)

#: The grammar of a canonical field id, pinned by
#: `schemas/datastream-field-mapping.schema.json` and minted by
#: `canonical_field_registry.declare_project_field`. It is here so this module can
#: RECOGNISE one anywhere in a request and resolve it to a word before a message
#: is built -- see `_requested_field_ids`.
_CANONICAL_FIELD_ID = re.compile(r"^mdm_[0-9A-HJKMNP-TV-Z]{26}$")

#: What a refusal calls a field when nothing -- not the governed vocabulary, not
#: the archived registry, not the column the person mapped -- can give it a word.
#: A canonical id is a term of the database and never reaches a reader
#: (`analyze-and-test.md`, amendment of 2026-08-21), so the sentence names the
#: ROLE the request gave it instead of an identifier nobody can act on.
UNNAMED_MEASURE = "the measure this request names"
UNNAMED_DIMENSION = "the dimension this request names"

#: It travels INSIDE the hashed document, so a stored hash is only ever comparable
#: to one produced by the same contract -- and a reader that does not know this
#: string must refuse the row rather than read it as a single-source spec.
#:
#: WHEN IT IS BUMPED, arbitrated 2026-08-22 and written in `analyze-and-test.md`
#: under *an additive key does not bump a contract version*. This comment used to
#: say "bumped when the frozen document changes shape", which is too wide: story
#: 66.4 added `left_physical_type` / `right_physical_type` to `key_paths` and a
#: reader that ignores them reads exactly what it read before.
#:
#:     A contract version is bumped when a reader that understood the old
#:     document would MISREAD the new one.
#:
#: An ADDED key does not do that. Removing one, renaming one, or changing what an
#: existing one MEANS does. What this costs is stated rather than discovered: a
#: `content_hash` identifies a DOCUMENT, not a request across two builds, so a
#: consumer that recompiles a stored request and compares hashes is reading it
#: wrong -- it compares the stored document instead.
MULTI_SOURCE_PLAN_CONTRACT_VERSION = "multi-source-plan.v1"

#: Two is the point of the epic; four is where a governed exploration stops being
#: one question. The bound is here AND in the refusal message, so a caller reads
#: what it may do rather than discovering it.
MIN_MEMBERS, MAX_MEMBERS = 2, 4

#: The three inclusion policies, and there is no fourth and no default. A plan
#: that did not state one would let execution decide what happens to unmatched
#: rows -- silently, differently on two runs.
INCLUSION_POLICIES = ("matched_only", "preserve_primary", "preserve_all")

#: Aggregation stage of a filter. `pre` narrows the rows a source aggregates;
#: `post` narrows the merged result. The same predicate at the two stages is two
#: different questions, so the stage is declared and never inferred.
FILTER_STAGES = ("pre_aggregation", "post_aggregation")

MAX_MEASURES_PER_MEMBER = 20
MAX_DIMENSIONS = 10
MAX_FILTERS = 50
MAX_ESTIMATED_RESULT_CELLS = 2_000_000
MAX_PIVOT_VALUES = 20
MAX_REQUESTED_SKILLS = 8
PIVOT_GRAND_TOTAL_POLICIES = frozenset({"none", "rows", "columns", "both"})
ANALYSIS_CONTEXT_CONTRACT_VERSION = "analysis-context.v1"
PERIOD_COMPARISON_CONTRACT_VERSION = "period-comparison.v1"
COMPARISON_PERIOD_FIELD = "k_comparison_period"
PERIOD_COMPARISONS = frozenset({"none", "previous_period", "previous_year"})


class PlanRefused(ValueError):
    """The plan is refused, by name, before anything is stored."""

    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def unsupported_relationship(relationship: Mapping[str, Any]) -> tuple[str, str] | None:
    """What the multi-source executor cannot run today, named — or `None`.

    ONE IMPLEMENTATION, TWO DOORS, and the second one is why this function exists.
    These three refusals used to live inline in `compile_plan` alone, so the
    DISCOVERY side (`datastream_matches._safety`) judged a match on its
    `cardinality` and nothing else. Measured 2026-08-16: a `many_to_one`
    relationship carrying `fan_out_policy = 'deduplicate'` was advertised
    **`ready`** in the match catalogue and refused by this compiler the moment
    somebody acted on it. A badge that promises what the next step refuses is
    worse than no badge — the person spends their trust before they spend their
    click.

    The rule is the EXECUTOR's capability, so it lives with the executor and is
    imported by whoever advertises it. A second copy in the discovery module is
    exactly how the two got to disagree in the first place.
    """
    cardinality = str(relationship.get("cardinality") or "")
    fan_out_policy = str(relationship.get("fan_out_policy") or "")
    if fan_out_policy == "bridge" or relationship.get("bridge_dataset"):
        return (
            "bridge_execution_not_supported",
            "This relationship requires its governed bridge, but the current multi-source "
            "executor does not execute bridge rows. Nothing is joined optimistically.",
        )
    if cardinality == "many_to_many":
        return (
            "many_to_many_not_supported",
            "A many-to-many relationship needs a governed bridge execution path before it "
            "can preserve both sources' totals.",
        )
    if fan_out_policy == "deduplicate":
        return (
            "deduplication_not_supported",
            "This relationship requires an explicit deduplication rule that the current "
            "executor cannot apply.",
        )
    return None


# ---------------------------------------------------------------------------
# The merge bound (AI-293, ratified 2026-08-17)
# ---------------------------------------------------------------------------
#
# WHAT WAS LIFTED, AND WHAT REPLACED IT. Until this amendment the compiler
# refused every cross whose measured profile was not `ready`, except one exact
# one-sided duplicate. A two-sided duplicate -- three spend rows and two
# conversion rows on `(2026-08-01, A)` -- was refused, and the measurement taken
# on exactly those rows shows both control totals surviving: 18 and 7, against
# the 36 and 21 a row-level join would have produced. The refusal was
# CONSERVATIVE RATHER THAN NECESSARY, which is the one kind of refusal a reader
# is entitled to contradict.
#
# The bound that replaces it is stated, not implied: the merge is admitted WHEN
# THE MERGE KEY EQUALS THE AGGREGATION GRAIN AND EVERY COMBINED METRIC IS
# ADDITIVE. Inside it, `multi_source_execution` folds each member to the merge
# key before joining, so each side is one row per key and the join is one-to-one
# -- however many times either side repeated that key. The duplication
# measurement then decides nothing about correctness, which is exactly why being
# two-sided is no longer a reason to refuse.
#
# OUTSIDE the bound the refusal remains, and it NAMES THE CONDITION THAT FAILED.
# A single code for three different causes sends the reader to change the wrong
# thing, so there are two, and each carries what broke it.
#
# THE BOUND IS A PROPERTY OF THE MERGE, NOT OF THE PROFILE THAT MEASURED IT.
# `compile_plan` used to evaluate the bound only where the pre-AI-293 refusal had
# just been lifted, so an edge measured `ready`, and the older
# `exact_one_sided_duplicates` concession, crossed no bound at all -- neither the
# key/grain condition nor additivity. The defect that let through is the one this
# module's docstring describes: `multi_source_execution` folds EVERY member to
# the full merge key (`:166`) and then projects only `output_dimension_field_ids`
# (`:343`), whatever the profile said. So the bound is evaluated on every plan,
# on all three paths, and what the profile measured decides only whether a
# refusal was LIFTED, never whether the bound applies.

MERGE_BOUND_CONTRACT_VERSION = "merge-bound.v1"

#: The merge key is matched on but not shown, so the grain asked for is coarser
#: than the key the two sources meet on.
BOUND_KEY_FINER_THAN_GRAIN = "merge_key_finer_than_grain"
#: A measure the merge would fold to the shared key is not governed as additive.
BOUND_METRIC_NOT_ADDITIVE = "metric_not_additive"
#: Nothing could say whether a combined measure is additive. `unavailable` never
#: reads as a pass (`analyze-and-test.md:647`), so an unread rule refuses instead
#: of admitting -- the half of the ratified bound that cannot be evaluated is not
#: the half that silently passes.
BOUND_ADDITIVITY_UNKNOWN = "metric_additivity_unknown"


def _requested_field_ids(payload: Any) -> list[str]:
    """Every canonical field id anywhere in a request or a frozen plan.

    A COLLECTOR AND NOT A LIST OF KNOWN PLACES, deliberately. The ids reach a
    message from at least five surfaces -- a member's measures, a pivot axis, a
    ratio, a filter, a merge key component -- and a hand-written list of those
    surfaces is a list that stops being complete the next time one is added. The
    id grammar is pinned by the mapping schema, so recognising it is exact: a
    string that matches is a canonical field id and nothing else is.

    What this buys is one read, before anything is compiled, that can give EVERY
    refusal in this module a word instead of an identifier.
    """
    found: set[str] = set()
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if _CANONICAL_FIELD_ID.match(node):
                found.add(node)
        elif isinstance(node, Mapping):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, (list, tuple, set)):
            stack.extend(node)
    return sorted(found)


def _reader_identities(
    conn, project_id: str, payload: Any
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Name and status for every canonical id a refusal of this module may print.

    READ WITH `status` UNFILTERED, which is the whole reason this is not the
    governed vocabulary. `_governed_vocabulary` reads
    `list_visible_canonical_fields`, and that read hides archived rows because it
    offers a catalog to bind AGAINST. A refusal has the opposite need: the field
    a person archived last week is still bound by a published mapping, and the
    one moment they most need its NAME is the moment the merge refuses it.
    `canonical_field_registry.load_canonical_names` was written and documented for
    exactly that, three minutes before the refusal that needed it -- and was never
    called from here.

    THE STATUS COMES FROM THE ROW, NOT FROM SUBTRACTING TWO READS. "In the
    registry but not in the visible catalog" is only "archived" while the two
    reads differ by nothing but status, and the answer decides whether the
    sentence says "declare it" or "restore it".

    IT DOES NOT TURN A REFUSAL INTO A STACK TRACE, AND IT DOES NOT PRETEND
    EITHER. The read used to fall back to `{}` and say nothing, which reads as
    "no field is archived" -- and `{}` is indistinguishable from a registry that
    answered nothing at all. The consequence was not cosmetic: a field a person
    archived last week came back as `metric_not_in_vocabulary`, and the refusal
    told them to "declare it in Governance with an aggregation", which is the
    IMPOSSIBLE GESTURE this whole read exists to avoid. An unreadable registry
    reopened it every time.

    So this returns `(identities, readable)`, exactly as `_governed_vocabulary`
    does. What cannot be read is not asserted: the caller that has to choose
    between "restore it" and "declare it" is told it may choose neither, and
    says instead that the catalogue could not be read. Naming is still never a
    permission -- nothing here admits a merge -- but a sentence that names the
    wrong gesture is worse than one that names none.
    """
    from core.canonical_field_registry import (  # noqa: PLC0415
        load_canonical_field_identities,
    )

    field_ids = _requested_field_ids(payload)
    if not field_ids:
        return {}, True
    try:
        return load_canonical_field_identities(
            conn, project_id=project_id, field_ids=field_ids
        ), True
    except Exception as exc:  # noqa: BLE001 -- a name is not a permission
        logger.warning("multi_source_plan: canonical identities unreadable: %s", exc)
        return {}, False


def _names_of(identities: Mapping[str, Mapping[str, Any]] | None) -> dict[str, str]:
    """The word half of `_reader_identities`."""
    return {
        field_id: str(identity.get("canonical_name") or "")
        for field_id, identity in (identities or {}).items()
        if identity.get("canonical_name")
    }


def _archived_of(identities: Mapping[str, Mapping[str, Any]] | None) -> set[str]:
    """The ids the registry still holds but no longer offers to bind against."""
    return {
        field_id
        for field_id, identity in (identities or {}).items()
        if str(identity.get("status") or "") not in ("", "active")
    }


def _field_word(
    field_id: str,
    *,
    vocabulary: Mapping[str, Mapping[str, Any]] | None = None,
    names: Mapping[str, str] | None = None,
    fallback: str | None = None,
    unnamed: str = UNNAMED_MEASURE,
) -> str:
    """The one word a reader can act on for a canonical field. Never its id.

    In order: the name the governed vocabulary gives it; the name the registry
    still holds for it once archived; the column of the source the person mapped;
    and, when the request named an id no store knows at all, the ROLE the request
    gave it. Every refusal of this module goes through here, so "a refusal prints
    a canonical field id" is one function to look at rather than fifteen
    f-strings to keep watching.
    """
    governed = ((vocabulary or {}).get(field_id) or {}).get("canonical_name")
    if governed:
        return str(governed)
    archived = (names or {}).get(field_id)
    if archived:
        return str(archived)
    if fallback:
        return str(fallback)
    return unnamed


def _metric_additivity(
    project_id: str, vocabulary: Mapping[str, Mapping[str, Any]]
) -> dict[str, bool]:
    """canonical field id -> is this metric additive, per the GOVERNED matrix.

    Two declaring stores, Concept first, in the order
    `metric_semantics.resolve_declared_additivity` already ratifies: the Semantic
    Model class a person chose on the Concept wins over the registry's boolean
    (migration 237 makes it NOT NULL for every published metric version).

    `semi_additive` IS NOT ADDITIVE HERE, and the distinction is the whole point.
    A measure that may be summed over some dimensions and not others cannot be
    summed over a merge key nobody constrained -- `declared_non_additive` puts it
    on the same side for the same reason.

    FAIL-SOFT ON THE DECLARATION, NEVER ON THE REGISTRY. The three-class read
    opens its own connection, so it cannot see work that is still in this
    compiler's transaction; it is therefore an OVERLAY and not the source. When
    it answers nothing, the registry boolean -- read on the compiler's own
    connection, from the one catalog every binding is validated against -- still
    answers, and that answer is governed. What is never done is guessing: a field
    absent from both is ABSENT FROM THE RESULT, and absence is read downstream as
    `unknown`. `_merge_bound` refuses on unknown; it does not sum on it.
    """
    from core.metric_semantics import (  # noqa: PLC0415
        ADDITIVITY_ADDITIVE,
        resolve_declared_additivity,
    )

    try:
        declared = resolve_declared_additivity(project_id)
    except Exception as exc:  # noqa: BLE001 -- the registry below still answers
        logger.warning("multi_source_plan: declared additivity unreadable: %s", exc)
        declared = {}

    additivity: dict[str, bool] = {}
    for field_id, governed in vocabulary.items():
        klass = declared.get(str(governed.get("canonical_name") or ""))
        if klass is not None:
            additivity[field_id] = klass == ADDITIVITY_ADDITIVE
        else:
            additivity[field_id] = not bool(governed.get("non_additive"))
    return additivity


def _merge_bound(
    members: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    dimensions: Sequence[Mapping[str, Any]],
    grain_field_id: str | None,
    additivity: Mapping[str, bool],
    vocabulary: Mapping[str, Mapping[str, Any]],
    *,
    additivity_readable: bool = True,
    names: Mapping[str, str] | None = None,
    archived: set[str] | None = None,
    identities_readable: bool = True,
) -> dict[str, Any]:
    """Is this plan inside the bound under which a merge is admitted?

    THE GRAIN IS READ THE WAY THE EXECUTOR COMPUTES IT, never from the `grain`
    string alone. `multi_source_execution.output_dimension_field_ids` projects the
    merged rows on the SELECTED dimensions and does not roll up afterwards -- the
    merged SELECT carries no GROUP BY. So a merge key finer than that projection
    emits one row per key while showing only part of it, which is a real defect
    and not a theoretical one. When no dimension is selected the projection IS the
    whole key, which is the ordinary case and always inside the bound.

    Returns the verdict rather than raising: whether a failure becomes a refusal
    depends on what the profile measured, and only `compile_plan` knows both.
    """
    merge_key = _edge_components(edges)
    selected = [
        str(entry["canonical_field_id"])
        for entry in dimensions
        if entry.get("canonical_field_id")
    ]
    if selected:
        output_ids = list(selected)
        if grain_field_id and grain_field_id not in output_ids:
            output_ids.insert(0, grain_field_id)
    else:
        output_ids = list(merge_key)

    failures: list[dict[str, Any]] = []
    shown = set(output_ids)
    dropped = [
        _field_word(
            field_id,
            vocabulary=vocabulary,
            names=names,
            fallback=str(component.get("canonical_name") or ""),
            unnamed=UNNAMED_DIMENSION,
        )
        for field_id, component in merge_key.items()
        if field_id not in shown
    ]
    if dropped:
        failures.append(
            {
                "condition": BOUND_KEY_FINER_THAN_GRAIN,
                "message": (
                    "The two sources are matched on "
                    + ", ".join(sorted(dropped))
                    + ", and this analysis does not show it. Folding to the coarser "
                    "grain would collapse the very key they meet on, so the totals "
                    "would stop being each source's own. Show it as a dimension, or "
                    "cross on a key that stops at the grain you asked for."
                ),
                "detail": {"components": sorted(dropped)},
            }
        )

    not_additive: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for member in members:
        for measure in member.get("measures") or []:
            field_id = str(measure.get("canonical_field_id") or "")
            # NAMED IN THE READER'S WORDS, NEVER IN THE DATABASE'S. The governed
            # name when the vocabulary can give one; the name the registry still
            # holds for it once ARCHIVED -- `list_visible_canonical_fields` hides
            # those, `load_canonical_names` does not, and a field archived while
            # a published mapping still binds it is precisely the case where the
            # word matters most; otherwise the column of the source the person
            # mapped it from. A canonical id is a term of the base and never
            # reaches a message; it stays in `detail` for the machine.
            #
            # THE LAST RESORT NAMES THE SOURCE, NOT THE ID. The chain above used
            # to end on `field_id`, and it was reachable: a binding whose column
            # is blank has no fallback, and an archived field is absent from the
            # vocabulary -- so the refusal printed `mdm_01...` to a reader, which
            # this same document forbids in the same commit that wrote it.
            source_name = str(member.get("name") or "")
            entry = {
                "source": member.get("name"),
                "metric": _field_word(
                    field_id,
                    vocabulary=vocabulary,
                    names=names,
                    fallback=str(measure.get("physical_field") or ""),
                    unnamed=(
                        f"a measure of {source_name}" if source_name else UNNAMED_MEASURE
                    ),
                ),
                "canonical_field_id": field_id,
            }
            # ABSENT FROM THE MATRIX IS `unknown`, AND `unknown` IS NOT `additive`.
            # It used to default to True, so a governed catalogue this compiler
            # could not read admitted every merge it should have refused -- the
            # ratified condition passed empty instead of refusing. `unavailable`
            # never reads as a pass (`analyze-and-test.md:647`), so the two ways
            # of not knowing -- the whole read failed, or this one metric is not
            # in the catalogue -- both land in `unknown` and both refuse.
            if not additivity_readable or field_id not in additivity:
                # THREE WAYS OF NOT KNOWING, THREE GESTURES. The catalogue could
                # not be read at all; the field was ARCHIVED while a published
                # mapping still binds it; or nobody ever declared it. All three
                # refuse -- `unavailable` never reads as a pass -- but telling a
                # person to "declare it in Governance" about a field they already
                # declared and then archived names a gesture that cannot be
                # performed, which is the one thing a refusal must never do.
                #
                # AND A FOURTH WAY OF NOT KNOWING, WHICH USED TO BE INVISIBLE.
                # `archived` comes from `_reader_identities`, and that read used
                # to fail soft to `{}`. An empty set reads as "nothing is
                # archived", so an unreadable registry sent an archived metric
                # down the `metric_not_in_vocabulary` branch and told the person
                # to declare a field they had declared and archived themselves --
                # reopening the impossible gesture the branch above exists to
                # close, on every read failure. What cannot be read is not
                # asserted: the sentence says the catalogue could not be read.
                if not additivity_readable or not identities_readable:
                    reason = "vocabulary_unreadable"
                elif field_id in (archived or set()):
                    reason = "metric_archived"
                else:
                    reason = "metric_not_in_vocabulary"
                unknown.append({**entry, "reason": reason})
                continue
            if additivity[field_id]:
                continue
            not_additive.append(entry)
    if not_additive:
        named = sorted({entry["metric"] for entry in not_additive})
        failures.append(
            {
                "condition": BOUND_METRIC_NOT_ADDITIVE,
                "message": (
                    "A merge folds each source to the shared key before it joins, and "
                    + ", ".join(named)
                    + (" is" if len(named) == 1 else " are")
                    + " not governed as additive -- folding it would state a number "
                    "nobody measured. Ask for the measures it is computed from and let "
                    "the Result recompute it after the merge."
                ),
                "detail": {"metrics": not_additive},
            }
        )

    if unknown:
        unreadable = any(entry["reason"] == "vocabulary_unreadable" for entry in unknown)
        if unreadable:
            # NOBODY CAN BE NAMED WHEN THE CATALOGUE IS THE THING THAT FAILED, so
            # the sentence names the SOURCES instead -- the words the person typed
            # when they created them, and the only honest handle left.
            sources = sorted({str(entry["source"] or "") for entry in unknown if entry["source"]})
            message = (
                "A merge folds each source to the shared key before it joins, and the "
                "governed vocabulary that says whether the measures of "
                + ", ".join(sources)
                + " may be summed could not be read at all. Nothing is folded on a rule "
                "nobody could state. Run this analysis again in a moment, and if it keeps "
                "refusing, open the Project's governed field catalogue and check it "
                "answers."
            )
        else:
            archived = sorted(
                {entry["metric"] for entry in unknown if entry["reason"] == "metric_archived"}
            )
            undeclared = sorted(
                {
                    entry["metric"]
                    for entry in unknown
                    if entry["reason"] == "metric_not_in_vocabulary"
                }
            )
            clauses: list[str] = []
            if archived:
                one = len(archived) == 1
                clauses.append(
                    ", ".join(archived)
                    + (" was" if one else " were")
                    + " archived in this Project's governed vocabulary, so nothing "
                    "declares whether "
                    + ("it" if one else "they")
                    + " may be summed any more. Restore "
                    + ("it" if one else "them")
                    + " in Governance, or ask for a measure that is still governed."
                )
            if undeclared:
                one = len(undeclared) == 1
                clauses.append(
                    ("Separately, " if archived else "")
                    + ", ".join(undeclared)
                    + (" is" if one else " are")
                    + " not in the governed vocabulary this Project validates its bindings "
                    "against, so nothing declares whether "
                    + ("it" if one else "they")
                    + " may be summed. Declare "
                    + ("it" if one else "them")
                    + " in Governance with an aggregation, then compile this cross again."
                )
            message = (
                "A merge folds each source to the shared key before it joins, and "
                + " ".join(clauses)
            )
        failures.append(
            {
                "condition": BOUND_ADDITIVITY_UNKNOWN,
                "message": message,
                "detail": {"metrics": unknown},
            }
        )

    return {
        "within": not failures,
        "failures": failures,
        "merge_key_field_ids": sorted(merge_key),
        "output_field_ids": sorted(shown),
    }


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Reading what the request claims, and checking it against what is published
# ---------------------------------------------------------------------------


def _published_members(conn, project_id: str, datastream_ids: Sequence[str]) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.name, d.current_mapping_version_id, m.mapping_payload,
                   m.version_number, d.current_published_execution_id,
                   ov.id, ov.output_id, ov.publication_log_id, ov.plan_version_id,
                   ov.relation_ref, ov.schema_hash
              FROM app.datastreams d
              LEFT JOIN app.datastream_mapping_versions m
                     ON m.id = d.current_mapping_version_id
              LEFT JOIN LATERAL (
                    SELECT dov.id, dov.output_id, dov.publication_log_id,
                           dov.plan_version_id, dov.relation_ref, dov.schema_hash
                      FROM app.datastream_output_versions dov
                      JOIN app.datastream_outputs output ON output.id = dov.output_id
                     WHERE dov.project_id = d.project_id
                       AND dov.datastream_id = d.id
                       AND dov.execution_id = d.current_published_execution_id
                       AND dov.mapping_version_id = d.current_mapping_version_id
                       AND output.output_kind = 'full_grain'
                     ORDER BY dov.created_at DESC, dov.id DESC
                     LIMIT 1
              ) ov ON TRUE
             WHERE d.project_id = %s AND d.id = ANY(%s) AND d.archived_at IS NULL
            """,
            (project_id, list(datastream_ids)),
        )
        rows = cur.fetchall()
    return {
        row[0]: {
            "datastream_id": row[0],
            "name": row[1],
            "mapping_version_id": row[2],
            "mapping_payload": row[3] if isinstance(row[3], dict) else {},
            "mapping_version_number": row[4],
            "published_execution_id": row[5],
            "output_version_id": row[6],
            "output_id": row[7],
            "publication_log_id": row[8],
            "plan_version_id": row[9],
            "relation_ref": row[10],
            "schema_hash": row[11],
        }
        for row in rows
    }


def _governed_vocabulary(
    conn, project_id: str
) -> tuple[dict[str, dict[str, Any]], bool]:
    """canonical field id -> what the governed vocabulary says its numbers mean.

    Read from the ONE catalog every binding is validated against
    (`canonical_field_registry.list_visible_canonical_fields`), so a plan can
    never freeze a unit the mapping door would have refused. An unreadable
    vocabulary is not an empty one: the read is allowed to fail and the plan then
    freezes no unit at all, which a reader renders as a bare number rather than
    as an amount in a currency nobody declared.

    AND THE SECOND MEMBER OF THE TUPLE IS WHY THAT SENTENCE IS NOT THE WHOLE
    STORY. Failing soft is right for a UNIT -- a missing unit prints a bare
    number, which is honest. It is wrong for ADDITIVITY, because an empty matrix
    read as "everything is additive" turns an unreachable catalogue into a
    permission to merge. So the failure is now RETURNED rather than swallowed:
    the units stay fail-soft, and `_merge_bound` refuses on the same event.
    """
    from core.canonical_field_registry import list_visible_canonical_fields  # noqa: PLC0415

    try:
        rows = list_visible_canonical_fields(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("multi_source_plan: canonical vocabulary unreadable: %s", exc)
        return {}, False
    return {
        str(row["id"]): {
            "value_type": row.get("value_type"),
            "unit": row.get("unit"),
            # READ ON THIS CONNECTION, AND NO LONGER THROWN AWAY. The merge bound
            # (AI-293) asks whether every combined metric is additive, and the
            # answer is already in the row this read returns -- `non_additive` is
            # a column of `app.mdm_canonical_fields`, and the registry refuses a
            # metric that declares neither an aggregation nor that flag
            # (`canonical_field_registry.py:175-183`). Projecting it away here
            # cost a SECOND connection to answer the same question, and a second
            # connection cannot see an open transaction.
            "canonical_name": row.get("canonical_name"),
            "non_additive": bool(row.get("non_additive")),
            "aggregation": row.get("aggregation"),
        }
        for row in rows
    }, True


def _bindings(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """canonical field id -> {physical, role, aggregation} for implemented bindings."""
    from core.mdm_common_keys import IMPLEMENTING_BINDING_STATUSES  # noqa: PLC0415

    bound: dict[str, dict[str, Any]] = {}
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
        suggestion = field.get("suggestion") if isinstance(field.get("suggestion"), dict) else {}
        target = binding.get("mdm_target")
        if not target or binding.get("status") not in IMPLEMENTING_BINDING_STATUSES:
            continue
        bound.setdefault(
            str(target),
            {
                "physical_field": str(field.get("field_id") or ""),
                "physical_type": str(field.get("physical_type") or ""),
                "semantic_role": str(suggestion.get("semantic_role") or ""),
                "aggregation": suggestion.get("aggregation"),
            },
        )
    return bound


def _edge_relationships(
    conn,
    project_id: str,
    key_version_id: str,
    left_datastream_id: str,
    right_datastream_id: str,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.name, r.cardinality_type, r.fan_out_policy, r.bridge_dataset,
                   v.id, v.view_id, v.version_number,
                   r.left_datastream_id, r.right_datastream_id
              FROM app.semantic_view_version_relationships r
              JOIN app.semantic_view_versions v ON v.id = r.view_version_id
             WHERE v.project_id = %s AND v.status = 'published'
               AND r.mdm_common_key_version_id = %s
               AND (
                    (r.left_datastream_id = %s AND r.right_datastream_id = %s)
                 OR (r.left_datastream_id = %s AND r.right_datastream_id = %s)
               )
             ORDER BY v.version_number DESC, r.ordinal
            """,
            (
                project_id,
                key_version_id,
                left_datastream_id,
                right_datastream_id,
                right_datastream_id,
                left_datastream_id,
            ),
        )
        rows = cur.fetchall()
    return [
        {
            "relationship_name": row[0],
            "cardinality": row[1],
            "fan_out_policy": row[2],
            "bridge_dataset": row[3],
            "view_version_id": row[4],
            "view_id": row[5],
            "view_version_number": row[6],
            "left_datastream_id": row[7],
            "right_datastream_id": row[8],
        }
        for row in rows
    ]


def _key_components(conn, project_id: str, key_version_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT components FROM app.mdm_common_key_versions WHERE id = %s AND project_id = %s",
            (key_version_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise PlanRefused(
            "undeclared_common_key",
            "That common key version is not declared in this Project, so nothing says "
            "these sources share an identity.",
        )
    return [dict(component) for component in (row[0] or [])]


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


def _check_tree(member_ids: Sequence[str], edges: Sequence[Mapping[str, Any]]) -> None:
    """N members, N-1 edges, all connected, no pair joined twice."""
    if len(edges) != len(member_ids) - 1:
        raise PlanRefused(
            "join_graph_not_a_tree",
            f"{len(member_ids)} sources need exactly {len(member_ids) - 1} declared crosses; "
            f"{len(edges)} were sent. Fewer leaves a source attached to nothing; more means "
            "two paths reach the same pair, and the product does not choose between them.",
        )
    seen_pairs: set[frozenset[str]] = set()
    parent = {member: member for member in member_ids}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for edge in edges:
        left, right = str(edge.get("left") or ""), str(edge.get("right") or "")
        if left not in parent or right not in parent:
            raise PlanRefused(
                "edge_names_unknown_member",
                "A declared cross names a source that is not part of this analysis.",
            )
        if left == right:
            raise PlanRefused(
                "edge_joins_a_source_to_itself",
                "A source cannot be crossed with itself.",
            )
        pair = frozenset({left, right})
        if pair in seen_pairs:
            raise PlanRefused(
                "duplicate_edge",
                "The same two sources are crossed twice. Two paths between one pair is "
                "the ambiguity a person resolves, not the product.",
            )
        seen_pairs.add(pair)
        a, b = find(left), find(right)
        if a == b:
            raise PlanRefused(
                "join_graph_has_a_cycle",
                "These crosses form a loop, so the same rows would be joined twice by "
                "two different routes.",
            )
        parent[a] = b


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def compile_plan(
    conn,
    *,
    project_id: str,
    request: Mapping[str, Any],
    profile_lookup=None,
) -> dict[str, Any]:
    """Freeze one exact multi-Datastream plan, or refuse it by name.

    `profile_lookup(left, right, key_version_id, relationship_name, view_version_id)` is
    the 66.3 measurement, passed
    in rather than called directly so a plan can be compiled without a warehouse
    round trip when the caller already has the evidence. When it is provided and
    says `unsafe`, the plan is refused: the compiler does not knowingly freeze a
    path that multiplies every measure of both sources.
    """
    members_raw = request.get("members")
    if not isinstance(members_raw, list) or not (
        MIN_MEMBERS <= len(members_raw) <= MAX_MEMBERS
    ):
        raise PlanRefused(
            "member_count_out_of_bounds",
            f"A cross-source analysis names between {MIN_MEMBERS} and {MAX_MEMBERS} sources.",
        )

    ids: list[str] = []
    for entry in members_raw:
        if not isinstance(entry, dict) or not entry.get("datastream_id"):
            raise PlanRefused("malformed_member", "Every member names a Datastream.")
        datastream_id = str(entry["datastream_id"])
        if datastream_id in ids:
            # THE ID IS NOT THE ANSWER HERE EITHER. This sentence printed `ds_<ULID>`
            # at a reader, and the reader can act without it: the duplicate is in
            # the request they just sent, and removing it is the whole gesture. The
            # Datastream's name is not readable at this point -- `_published_members`
            # has not run yet -- and inventing a read to print one would be a
            # heavier repair than the sentence needs.
            raise PlanRefused(
                "duplicate_member",
                "A Datastream contributes to an analysis once. Remove the member that "
                "repeats it, then compile this cross again.",
            )
        ids.append(datastream_id)

    published = _published_members(conn, project_id, ids)
    missing = [i for i in ids if i not in published]
    if missing:
        # Absent, archived and foreign are one answer: naming which of the three
        # would tell an unauthorized caller that an id exists elsewhere.
        raise PlanRefused(
            "member_not_available",
            "One of the sources is not an available published source of this Project.",
        )

    inclusion = str(request.get("inclusion_policy") or "")
    if inclusion not in INCLUSION_POLICIES:
        raise PlanRefused(
            "inclusion_policy_required",
            "State what happens to unmatched rows: "
            + ", ".join(INCLUSION_POLICIES)
            + ". There is no default, because a default would decide it silently.",
        )

    primary = str(request.get("primary_datastream_id") or ids[0])
    if primary not in ids:
        raise PlanRefused(
            "primary_not_a_member", "The primary source is not one of the named sources."
        )

    vocabulary, vocabulary_readable = _governed_vocabulary(conn, project_id)
    # READ ONCE, BEFORE ANY REFUSAL CAN BE BUILT. The governed vocabulary above
    # hides archived fields by design; this read does not, so every sentence this
    # compiler may have to write has a WORD for every id the request names --
    # including the field a person archived while a published mapping still binds
    # it, which is the one case where the id used to reach the screen.
    identities, identities_readable = _reader_identities(conn, project_id, request)
    names, archived = _names_of(identities), _archived_of(identities)
    compiled_members = []
    for entry in members_raw:
        compiled_members.append(
            _compile_member(
                entry, published[str(entry["datastream_id"])], vocabulary, names
            )
        )

    edges_raw = request.get("edges")
    if not isinstance(edges_raw, list):
        raise PlanRefused("edges_required", "A cross-source analysis declares its crosses.")
    _check_tree(ids, edges_raw)

    compiled_edges = []
    deferred_safety: list[dict[str, Any]] = []
    for edge in edges_raw:
        pending: list[dict[str, Any]] = []
        compiled_edge = _compile_edge(conn, project_id, edge, published, profile_lookup, pending)
        for entry in pending:
            entry["edge"] = compiled_edge
        deferred_safety.extend(pending)
        compiled_edges.append(compiled_edge)

    _assign_measure_output_names(compiled_members)
    dimensions = _compile_dimensions(request.get("dimensions") or [], compiled_edges)
    grain, grain_field_id = _compile_grain(request.get("grain"), compiled_edges)
    selected_dimension_ids = {
        str(entry["canonical_field_id"]) for entry in dimensions
    }
    if not selected_dimension_ids:
        selected_dimension_ids = set(_edge_components(compiled_edges))
    if grain_field_id:
        selected_dimension_ids.add(grain_field_id)
    filters = _compile_filters(
        request.get("filters") or [],
        compiled_members,
        compiled_edges,
        selected_dimension_ids=selected_dimension_ids,
    )
    comparison = _compile_comparison(
        request.get("comparison"),
        grain_field_id=grain_field_id,
        grain_value_type=(
            (_edge_components(compiled_edges).get(grain_field_id) or {}).get("value_type")
            if grain_field_id
            else None
        ),
        filters=filters,
        member_ids={str(member["datastream_id"]) for member in compiled_members},
    )
    derived = _compile_derived(
        request.get("derived_measures") or [], compiled_members, vocabulary, names
    )
    pivot = _compile_pivot(
        request.get("pivot"),
        compiled_members,
        dimensions,
        compiled_edges,
        comparison=comparison,
        vocabulary=vocabulary,
        names=names,
    )
    row_limit = _bounded_int(request.get("row_limit"), 10_000, 100_000, "row_limit")
    projected_columns = len(dimensions) + (1 if comparison else 0) + sum(
        len(member.get("measures") or []) for member in compiled_members
    ) + len(derived)
    estimated_max_cells = row_limit * projected_columns
    if estimated_max_cells > MAX_ESTIMATED_RESULT_CELLS:
        raise PlanRefused(
            "estimated_result_too_large",
            "This request could return more than 2,000,000 cells. Lower the row limit "
            "or select fewer measures before execution.",
            detail={
                "estimated_max_cells": estimated_max_cells,
                "max_estimated_result_cells": MAX_ESTIMATED_RESULT_CELLS,
            },
        )

    view_version_ids = sorted({edge["relationship"]["view_version_id"] for edge in compiled_edges})
    if len(view_version_ids) != 1:
        raise PlanRefused(
            "multiple_semantic_view_versions_not_supported",
            "One stored multi-source plan currently pins exactly one Semantic View version. "
            "A path crossing several View authorities is refused rather than relationally "
            "pinning only the first one.",
            detail=view_version_ids,
        )
    analysis_context = _compile_analysis_context(
        conn,
        project_id=project_id,
        semantic_view_version_id=view_version_ids[0],
        raw=request.get("analysis_context"),
    )
    for compiled_edge in compiled_edges:
        if compiled_edge.get("measured_safety") is None:
            # Same deferral, same reason: evidence bounds a fan-out, and inside
            # the bound there is no fan-out left for it to bound.
            deferred_safety.append(
                {"code": "profile_required", "detail": None, "edge": compiled_edge}
            )

    # EVALUATED ON EVERY PLAN, ON ALL THREE PATHS. This call used to sit behind
    # `if deferred_safety:`, so the bound was checked exactly where the old
    # blanket refusal had just been lifted and nowhere else: an edge measured
    # `ready`, and the older `exact_one_sided_duplicates` concession, compiled
    # without either condition being read. What the profile measured is evidence
    # about FAN-OUT; the bound is about what the executor DOES with the key --
    # fold every member to it (`multi_source_execution:166`) and project only the
    # selected dimensions (`:343`) -- and it does that on every plan.
    bound = _merge_bound(
        compiled_members,
        compiled_edges,
        dimensions,
        grain_field_id,
        _metric_additivity(project_id, vocabulary),
        vocabulary,
        additivity_readable=vocabulary_readable,
        names=names,
        archived=archived,
        identities_readable=identities_readable,
    )
    if not bound["within"]:
        # NAMED, NEVER GENERIC. The reader is told which of the two
        # conditions broke and what broke it; a plan that fails both is
        # refused on the first and carries the other in its detail, so the
        # second gesture is never a surprise after the first.
        failure = bound["failures"][0]
        raise PlanRefused(
            failure["condition"],
            failure["message"],
            detail={
                **(failure["detail"] or {}),
                "conditions_failed": [f["condition"] for f in bound["failures"]],
                "merge_key_field_ids": bound["merge_key_field_ids"],
                "output_field_ids": bound["output_field_ids"],
                "measured": [
                    entry["detail"] for entry in deferred_safety if entry.get("detail")
                ],
            },
        )

    merge_bound = None
    if deferred_safety:
        for entry in deferred_safety:
            entry["edge"]["safety_mitigation"] = "aggregate_each_member_before_merge"
        merge_bound = {
            "contract_version": MERGE_BOUND_CONTRACT_VERSION,
            "merge_key_field_ids": bound["merge_key_field_ids"],
            "output_field_ids": bound["output_field_ids"],
            "refusals_lifted": sorted({entry["code"] for entry in deferred_safety}),
        }

    plan = {
        "contract_version": MULTI_SOURCE_PLAN_CONTRACT_VERSION,
        "primary_datastream_id": primary,
        "inclusion_policy": inclusion,
        "grain": grain,
        "grain_canonical_field_id": grain_field_id,
        "members": compiled_members,
        "edges": compiled_edges,
        "dimensions": dimensions,
        "filters": filters,
        "bounds": {
            "row_limit": row_limit,
            "max_members": MAX_MEMBERS,
            "estimated_max_cells": estimated_max_cells,
            "max_estimated_result_cells": MAX_ESTIMATED_RESULT_CELLS,
        },
        "semantic_view_version_ids": view_version_ids,
    }
    if derived:
        # ADDITIVE, and deliberately absent when unused: a plan that declares no
        # ratio hashes exactly as it did before this key existed, so every plan
        # stored by story 66.4 keeps its identity.
        plan["derived_measures"] = derived
    if merge_bound is not None:
        # THE BOUND IS CHECKED ON EVERY PLAN; THIS KEY RECORDS A LIFTED REFUSAL.
        # It appears only where the bound admitted a cross that could not compile
        # at all before AI-293, so every plan that compiled yesterday hashes today
        # exactly as it did. An immutable plan states why it was allowed to exist,
        # and "the profile was ready" and "the bound lifted a refusal" are
        # different permissions -- but they are not different bounds.
        plan["merge_bound"] = merge_bound
    if pivot is not None:
        plan["pivot"] = pivot
    if comparison is not None:
        plan["comparison"] = comparison
    if analysis_context is not None:
        plan["analysis_context"] = analysis_context
    return {"plan": plan, "content_hash": content_hash(plan)}


def _compile_analysis_context(
    conn,
    *,
    project_id: str,
    semantic_view_version_id: str,
    raw: Any,
) -> dict[str, Any] | None:
    """Freeze the business question and requested Skills beside the analytical plan.

    These are provenance pins, not labels supplied by the browser.  Business Domain
    and Golden Question identities are checked against the exact Semantic View used
    by the plan. Requested Skills remain separate from Skills later *observed* on the
    Result-owned AI Path.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PlanRefused("malformed_analysis_context", "Analysis context is an object.")
    allowed = {
        "business_domain_id",
        "business_domain_version_number",
        "golden_question_version_id",
        "skill_version_ids",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise PlanRefused(
            "undeclared_analysis_context_key",
            "Analysis context contains only Business Domain, Golden Question and Skill pins.",
            detail=unknown,
        )

    domain_id = str(raw.get("business_domain_id") or "").strip() or None
    raw_domain_version = raw.get("business_domain_version_number")
    domain_version: int | None = None
    if raw_domain_version is not None:
        try:
            domain_version = int(raw_domain_version)
        except (TypeError, ValueError) as exc:
            raise PlanRefused(
                "malformed_business_domain_pin", "Business Domain version is a whole number."
            ) from exc
        if domain_version < 1:
            raise PlanRefused(
                "malformed_business_domain_pin", "Business Domain version starts at 1."
            )
    if (domain_id is None) != (domain_version is None):
        raise PlanRefused(
            "incomplete_business_domain_pin",
            "Business Domain id and version are pinned together.",
        )

    golden_question: dict[str, Any] | None = None
    gq_version_id = str(raw.get("golden_question_version_id") or "").strip() or None
    if gq_version_id:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.golden_question_id, v.id, v.version_number, q.title,
                       v.content_hash, v.business_domain_id,
                       v.business_domain_version_number, v.semantic_view_version_id
                  FROM app.golden_question_versions v
                  JOIN app.golden_questions q
                    ON q.id = v.golden_question_id
                   AND q.org_id = v.org_id AND q.project_id = v.project_id
                 WHERE v.id = %s AND v.project_id = %s
                """,
                (gq_version_id, project_id),
            )
            row = cur.fetchone()
        if row is None:
            raise PlanRefused(
                "golden_question_not_available",
                "That Golden Question version is not available in this Project.",
            )
        if str(row[7]) != semantic_view_version_id:
            raise PlanRefused(
                "golden_question_view_mismatch",
                "The Golden Question is pinned to a different Semantic View version.",
            )
        if domain_id is not None and (domain_id, domain_version) != (str(row[5]), int(row[6])):
            raise PlanRefused(
                "golden_question_domain_mismatch",
                "The Golden Question and selected Business Domain version disagree.",
            )
        domain_id, domain_version = str(row[5]), int(row[6])
        golden_question = {
            "id": str(row[0]),
            "version_id": str(row[1]),
            "version_number": int(row[2]),
            "title": str(row[3]),
            "content_hash": str(row[4]),
        }

    business_domain: dict[str, Any] | None = None
    if domain_id is not None and domain_version is not None:
        with conn.cursor() as cur:
            # Story 49.2: the pinned Business Domain version resolves through the
            # authority, and its NAME comes from the identity -- the authority's
            # version ledger carries no name column, and the name a person reads
            # on a plan is the identity's current one, which is the same value the
            # superseded ledger froze for a store that never renamed after a
            # version.
            cur.execute(
                f"""
                SELECT d.name
                  FROM {catalogue.DOMAIN_VERSION_SOURCE} v
                  JOIN {catalogue.DOMAIN_SOURCE} d
                    ON d.id = v.domain_id AND d.org_id = v.org_id
                  JOIN app.projects p ON p.org_id = v.org_id
                  JOIN app.semantic_view_versions svv
                    ON svv.id = %s AND svv.project_id = p.id
                 WHERE p.id = %s AND v.domain_id = %s AND v.version_number = %s
                   AND svv.business_domain_refs ? v.domain_id
                """,
                (semantic_view_version_id, project_id, domain_id, domain_version),
            )
            row = cur.fetchone()
        if row is None:
            raise PlanRefused(
                "business_domain_not_available",
                "That Business Domain version is not governed by the selected Semantic View.",
            )
        business_domain = {
            "id": domain_id,
            "version_number": domain_version,
            "version_id": f"{domain_id}:{domain_version}",
            "name": str(row[0]),
        }

    skill_refs = raw.get("skill_version_ids") or []
    if not isinstance(skill_refs, list) or len(skill_refs) > MAX_REQUESTED_SKILLS:
        raise PlanRefused(
            "skill_pin_count_out_of_bounds",
            f"Analysis context pins at most {MAX_REQUESTED_SKILLS} Skills.",
        )
    normalized_refs: list[tuple[str, int, str]] = []
    for value in skill_refs:
        reference = str(value or "").strip()
        procedure_id, separator, raw_version = reference.rpartition("@")
        if not separator or not procedure_id or not raw_version.isdigit() or int(raw_version) < 1:
            raise PlanRefused(
                "malformed_skill_version_pin",
                "Every Skill pin uses the immutable procedure_id@version_number form.",
            )
        normalized_refs.append((procedure_id, int(raw_version), reference))
    if len({entry[2] for entry in normalized_refs}) != len(normalized_refs):
        raise PlanRefused("duplicate_skill_version_pin", "A Skill version is pinned only once.")

    requested_skills: list[dict[str, Any]] = []
    for procedure_id, version_number, reference in sorted(normalized_refs, key=lambda x: x[2]):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name
                  FROM app.procedures_versions
                 WHERE procedure_id = %s AND version_number = %s
                   AND (project_id IS NULL OR project_id = %s)
                """,
                (procedure_id, version_number, project_id),
            )
            row = cur.fetchone()
        if row is None:
            raise PlanRefused(
                "skill_version_not_available",
                "One requested Skill version is not available in this Project.",
            )
        requested_skills.append(
            {
                "procedure_id": procedure_id,
                "version_number": version_number,
                "version_id": reference,
                "name": str(row[0]),
            }
        )

    if business_domain is None and golden_question is None and not requested_skills:
        return None
    return {
        "contract_version": ANALYSIS_CONTEXT_CONTRACT_VERSION,
        "business_domain": business_domain,
        "golden_question": golden_question,
        "requested_skills": requested_skills,
        "semantic_view_version_id": semantic_view_version_id,
    }


def _compile_pivot(
    raw: Any,
    members: Sequence[Mapping[str, Any]],
    dimensions: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    comparison: Mapping[str, Any] | None = None,
    vocabulary: Mapping[str, Mapping[str, Any]] | None = None,
    names: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Freeze the MCP/Console pivot wells as exact Result-field identities.

    The request names governed fields, not aliases invented by a client. Measure
    selectors include the Datastream id because two sources may contribute the
    same canonical measure while the Result must keep both columns distinct.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PlanRefused("malformed_pivot", "The pivot declaration is an object.")
    allowed = {
        "rows",
        "columns",
        "values",
        "filters",
        "subtotals",
        "grand_total",
        "row_sort",
        "column_sort",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise PlanRefused(
            "undeclared_pivot_key",
            "A pivot contains only Rows, Columns, Values, Filters, subtotals and totals.",
            detail=unknown,
        )

    dimension_ids = {
        str(entry["canonical_field_id"])
        for entry in dimensions
        if entry.get("canonical_field_id")
    }
    if not dimension_ids:
        dimension_ids = set(_edge_components(edges))

    def compile_axis(value: Any, name: str) -> list[str]:
        if not isinstance(value, list):
            raise PlanRefused("malformed_pivot_axis", f"Pivot {name} is a list of fields.")
        if len(value) > MAX_DIMENSIONS:
            raise PlanRefused("too_many_pivot_dimensions", f"At most {MAX_DIMENSIONS} {name}.")
        fields: list[str] = []
        for entry in value:
            field_id = (
                str(entry.get("canonical_field_id") or "")
                if isinstance(entry, Mapping)
                else str(entry or "")
            )
            if field_id not in dimension_ids:
                raise PlanRefused(
                    "pivot_dimension_not_selected",
                    "A pivot axis can only use a conformed dimension projected by the Result.",
                    detail=sorted(dimension_ids),
                )
            result_field = f"k_{field_id}"
            if result_field in fields:
                word = _field_word(
                    field_id,
                    vocabulary=vocabulary,
                    names=names,
                    unnamed=UNNAMED_DIMENSION,
                )
                raise PlanRefused(
                    "duplicate_pivot_dimension",
                    f"{word} is selected twice on the same axis. Place it once.",
                )
            fields.append(result_field)
        return fields

    rows = compile_axis(raw.get("rows") or [], "rows")
    columns = compile_axis(raw.get("columns") or [], "columns")
    if comparison is not None:
        columns.append(COMPARISON_PERIOD_FIELD)
    overlap = sorted(set(rows) & set(columns))
    if overlap:
        raise PlanRefused(
            "pivot_field_in_two_wells",
            "The same governed dimension cannot be both a Row and a Column.",
            detail=overlap,
        )

    measure_lookup = {
        (str(member["datastream_id"]), str(measure["canonical_field_id"])): str(
            measure["result_field"]
        )
        for member in members
        for measure in member.get("measures") or []
    }
    values_raw = raw.get("values") or []
    if not isinstance(values_raw, list) or not values_raw:
        raise PlanRefused("pivot_values_required", "A pivot selects at least one Value.")
    if len(values_raw) > MAX_PIVOT_VALUES:
        raise PlanRefused(
            "too_many_pivot_values", f"A pivot selects at most {MAX_PIVOT_VALUES} Values."
        )
    values: list[str] = []
    for entry in values_raw:
        if not isinstance(entry, Mapping):
            raise PlanRefused(
                "malformed_pivot_value",
                "Every pivot Value names its Datastream and canonical measure.",
            )
        selector = (
            str(entry.get("datastream_id") or ""),
            str(entry.get("canonical_field_id") or ""),
        )
        result_field = measure_lookup.get(selector)
        if result_field is None:
            raise PlanRefused(
                "pivot_measure_not_selected",
                "A pivot Value must be a measure selected from that exact Datastream.",
                detail={"datastream_id": selector[0], "canonical_field_id": selector[1]},
            )
        if result_field in values:
            raise PlanRefused("duplicate_pivot_value", "A pivot Value is selected only once.")
        values.append(result_field)

    filters_raw = raw.get("filters") or []
    if not isinstance(filters_raw, list) or len(filters_raw) > MAX_FILTERS:
        raise PlanRefused("too_many_pivot_filters", f"At most {MAX_FILTERS} pivot Filters.")
    filters: list[dict[str, Any]] = []
    for entry in filters_raw:
        if not isinstance(entry, Mapping):
            raise PlanRefused("malformed_pivot_filter", "Every pivot Filter is an object.")
        field_id = str(entry.get("canonical_field_id") or "")
        selected = entry.get("in")
        if field_id not in dimension_ids or not isinstance(selected, list) or not selected:
            raise PlanRefused(
                "malformed_pivot_filter",
                "A pivot Filter names a projected dimension and one or more exact values.",
            )
        filters.append({"field": f"k_{field_id}", "in": selected})

    grand_total = str(raw.get("grand_total") or "none")
    if grand_total not in PIVOT_GRAND_TOTAL_POLICIES:
        raise PlanRefused(
            "unknown_pivot_total_policy", "Pivot totals are none, rows, columns or both."
        )
    if comparison is not None and (bool(raw.get("subtotals")) or grand_total != "none"):
        raise PlanRefused(
            "comparison_totals_not_supported",
            "Period comparison keeps current and baseline totals separate. Turn off "
            "subtotals and grand totals rather than combining both windows into one number.",
        )
    row_sort = str(raw.get("row_sort") or "asc")
    column_sort = str(raw.get("column_sort") or "asc")
    if row_sort not in {"asc", "desc"} or column_sort not in {"asc", "desc"}:
        raise PlanRefused(
            "unknown_pivot_sort", "Pivot axis order is ascending or descending."
        )
    return {
        "contract_version": "pivot-request.v1",
        "rows": rows,
        "columns": columns,
        "values": values,
        "filters": filters,
        "subtotals": bool(raw.get("subtotals")),
        "grand_total": grand_total,
        "row_sort": row_sort,
        "column_sort": column_sort,
    }


def _compile_comparison(
    raw: Any,
    *,
    grain_field_id: str | None,
    grain_value_type: Any = None,
    filters: Sequence[Mapping[str, Any]],
    member_ids: set[str],
) -> dict[str, Any] | None:
    """Freeze two exact inclusive windows under one analytical authority."""
    kind = "none" if raw is None else str(raw).strip().lower()
    if kind not in PERIOD_COMPARISONS:
        raise PlanRefused(
            "unknown_comparison",
            "Comparison is none, previous_period or previous_year.",
        )
    if kind == "none":
        return None
    if not grain_field_id:
        raise PlanRefused(
            "comparison_grain_required",
            "A period comparison needs one governed temporal grain shared by every source.",
        )
    if str(grain_value_type or "") != "date":
        raise PlanRefused(
            "comparison_temporal_grain_required",
            "A period comparison needs a governed date component shared by every source.",
        )

    conflicting_grain_filters = [
        entry
        for entry in filters
        if entry.get("canonical_field_id") == grain_field_id
        and (
            entry.get("stage") != "pre_aggregation"
            or entry.get("operator") not in {"gte", "lte"}
        )
    ]
    if conflicting_grain_filters:
        raise PlanRefused(
            "comparison_window_conflict",
            "The comparison date component may only carry the exact From and To bounds; "
            "filter another dimension to narrow the answer.",
        )

    by_member: dict[str, dict[str, set[str]]] = {
        member_id: {"gte": set(), "lte": set()} for member_id in member_ids
    }
    for entry in filters:
        member_id = str(entry.get("datastream_id") or "")
        operator = str(entry.get("operator") or "")
        if (
            entry.get("stage") == "pre_aggregation"
            and entry.get("canonical_field_id") == grain_field_id
            and member_id in by_member
            and operator in {"gte", "lte"}
        ):
            by_member[member_id][operator].add(str(entry.get("value") or ""))
    if not by_member or any(
        len(bounds["gte"]) != 1 or len(bounds["lte"]) != 1
        for bounds in by_member.values()
    ):
        raise PlanRefused(
            "comparison_window_required",
            "Every source needs the same exact From and To dates before it can be compared.",
        )
    starts = {next(iter(bounds["gte"])) for bounds in by_member.values()}
    ends = {next(iter(bounds["lte"])) for bounds in by_member.values()}
    if len(starts) != 1 or len(ends) != 1:
        raise PlanRefused(
            "comparison_window_required",
            "Every source needs the same exact From and To dates before it can be compared.",
        )
    try:
        current_start = date.fromisoformat(next(iter(starts)))
        current_end = date.fromisoformat(next(iter(ends)))
    except ValueError as exc:
        raise PlanRefused(
            "comparison_window_required", "Comparison dates use the ISO YYYY-MM-DD format."
        ) from exc
    if current_start > current_end:
        raise PlanRefused(
            "comparison_window_required", "The comparison From date is not after its To date."
        )

    try:
        if kind == "previous_period":
            span = (current_end - current_start).days + 1
            baseline_end = current_start - timedelta(days=1)
            baseline_start = baseline_end - timedelta(days=span - 1)
        else:
            def previous_year(value: date) -> date:
                try:
                    return value.replace(year=value.year - 1)
                except ValueError:
                    return value.replace(year=value.year - 1, day=28)

            baseline_start = previous_year(current_start)
            baseline_end = previous_year(current_end)
    except (OverflowError, ValueError) as exc:
        raise PlanRefused(
            "comparison_window_required",
            "The selected window has no representable earlier comparison window.",
        ) from exc
    if baseline_end >= current_start:
        raise PlanRefused(
            "comparison_windows_overlap",
            "Current and baseline comparison windows must not overlap.",
        )

    return {
        "contract_version": PERIOD_COMPARISON_CONTRACT_VERSION,
        "kind": kind,
        "canonical_field_id": grain_field_id,
        "period_field": COMPARISON_PERIOD_FIELD,
        "current": {"start": current_start.isoformat(), "end": current_end.isoformat()},
        "baseline": {"start": baseline_start.isoformat(), "end": baseline_end.isoformat()},
    }


def _compile_member(
    entry: Mapping[str, Any],
    published: Mapping[str, Any],
    vocabulary: Mapping[str, Mapping[str, Any]] | None = None,
    names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One source, its EXACT mapping version, and the measures it contributes."""
    vocabulary = vocabulary or {}
    names = names or {}
    claimed = entry.get("mapping_version_id")
    actual = published.get("mapping_version_id")
    if not actual:
        raise PlanRefused(
            "member_publishes_no_mapping",
            f"{published['name']} publishes no mapping version, so nothing says which "
            "column carries which field.",
        )
    if claimed and str(claimed) != str(actual):
        # A stale pin is the whole reason the pin exists: the caller read the
        # catalog at version N and is asking to freeze N while Data publishes N+1.
        raise PlanRefused(
            "stale_mapping_pin",
            f"{published['name']} now publishes a different mapping version than the one "
            "this request names. Reopen the match so the analysis is built on what Data "
            "publishes today.",
            detail={"claimed": str(claimed), "published": str(actual)},
        )

    output_version_id = published.get("output_version_id")
    published_execution_id = published.get("published_execution_id")
    relation_ref = published.get("relation_ref")
    if not output_version_id or not published_execution_id or not relation_ref:
        raise PlanRefused(
            "member_publishes_no_output",
            f"{published['name']} has no current full-grain Output. Publish one before "
            "building a cross-source analysis.",
        )
    claimed_output = entry.get("output_version_id")
    if claimed_output and str(claimed_output) != str(output_version_id):
        raise PlanRefused(
            "stale_output_pin",
            f"{published['name']} now publishes a different Output version. Reopen the "
            "match before compiling the analysis.",
            detail={"claimed": str(claimed_output), "published": str(output_version_id)},
        )
    claimed_execution = entry.get("published_execution_id")
    if claimed_execution and str(claimed_execution) != str(published_execution_id):
        raise PlanRefused(
            "stale_publication_pin",
            f"{published['name']} now publishes a different execution. Reopen the match "
            "before compiling the analysis.",
            detail={
                "claimed": str(claimed_execution),
                "published": str(published_execution_id),
            },
        )

    bound = _bindings(published["mapping_payload"])
    measures = []
    seen_measures: set[str] = set()
    for measure in entry.get("measures") or []:
        if not isinstance(measure, dict) or not measure.get("canonical_field_id"):
            raise PlanRefused("malformed_measure", "Every measure names a canonical field.")
        if measure.get("expression") or measure.get("sql"):
            raise PlanRefused(
                "client_expression_refused",
                "A measure is a governed field, never an expression sent with the request.",
            )
        field_id = str(measure["canonical_field_id"])
        binding = bound.get(field_id)
        # THE SAME RULE AS THE BOUND'S REFUSALS, AND FOR THE SAME REASON. These
        # three sentences printed `mdm_01...` at a reader -- outside the bound,
        # but the same defect the amendment of 2026-08-21 forbids. A defect of
        # one refusal is a defect of every refusal in the module.
        word = _field_word(
            field_id,
            vocabulary=vocabulary,
            names=names,
            fallback=str((binding or {}).get("physical_field") or ""),
        )
        if binding is None:
            raise PlanRefused(
                "measure_not_bound",
                f"{published['name']} does not bind {word}, so it cannot contribute it. "
                f"Map a column of {published['name']} to it, or ask for a measure it "
                "already maps.",
            )
        if not str(binding.get("semantic_role") or "").startswith("measure"):
            raise PlanRefused(
                "field_is_not_a_measure",
                f"{word} is not governed as a measure on {published['name']}, so it "
                "cannot be summed. Ask for it as a dimension, or pick a measure of "
                f"{published['name']}.",
            )
        if field_id in seen_measures:
            raise PlanRefused(
                "duplicate_measure",
                f"{published['name']} selects {word} more than once. Ask for it once.",
            )
        if not str(binding.get("physical_field") or "").strip():
            # A BINDING WITHOUT A COLUMN CONTRIBUTES NOTHING, and it is refused
            # here rather than left to produce a nameless measure downstream:
            # the merge bound had no word left for it and printed its canonical
            # id, and the executor would have aliased an empty column name.
            raise PlanRefused(
                "measure_binding_has_no_column",
                f"{published['name']} binds {word} to no column, so it has no numbers "
                "to contribute. Map it to a column in this source's field mapping, then "
                "compile this cross again.",
            )
        seen_measures.add(field_id)
        governed = vocabulary.get(field_id) or {}
        measures.append(
            {
                "canonical_field_id": field_id,
                # THE WORD TRAVELS WITH THE MEASURE, exactly as the unit does and
                # for the same reason: an immutable plan states what its columns
                # ARE, and the readers of a Result must not each re-derive it.
                # The Explorer used to compose this label from the match
                # catalogue and fall back to `measure.canonical_field_id`, so a
                # measure absent from that catalogue was legended `mdm_01KZ...`.
                # `_field_word` is the one chain that produces this word: `word`
                # is the value the refusals of this same loop are built from, so
                # what a Result legends and what a refusal says cannot diverge.
                "canonical_name": word,
                "physical_field": binding["physical_field"],
                "aggregation": binding.get("aggregation") or "sum",
                # THE UNIT TRAVELS WITH THE MEASURE, FROZEN HERE. Canonical money
                # is micros (`core/money.py`, E39-AD1) and the division back to
                # display happens exactly once, at read. A Result that carried
                # only the number left every reader -- Console table, pivot, MCP
                # App -- to print `124000000` for 124 EUR, which is the same
                # amount stated wrong by six orders of magnitude. The plan is
                # where it belongs: an immutable plan states what its numbers
                # mean, and a later edit of the vocabulary cannot restate them.
                "value_type": governed.get("value_type"),
                "unit": governed.get("unit"),
            }
        )
    if len(measures) > MAX_MEASURES_PER_MEMBER:
        raise PlanRefused(
            "too_many_measures",
            f"A source contributes at most {MAX_MEASURES_PER_MEMBER} measures.",
        )
    return {
        "datastream_id": published["datastream_id"],
        "name": published["name"],
        "mapping_version_id": str(actual),
        "mapping_version_number": published.get("mapping_version_number"),
        "output_version_id": str(output_version_id),
        "output_id": str(published.get("output_id") or ""),
        "published_execution_id": str(published_execution_id),
        "publication_log_id": published.get("publication_log_id"),
        "plan_version_id": str(published.get("plan_version_id") or ""),
        "relation_ref": relation_ref,
        "schema_hash": published.get("schema_hash"),
        "measures": measures,
    }


def _assign_measure_output_names(members: Sequence[dict[str, Any]]) -> None:
    """Give every selected measure one stable Result-field identity.

    A canonical measure may legitimately be contributed by two Datastreams. In
    that case the canonical id alone is not a column identity: two SQL aliases
    would collide and the Result would lose which source produced which number.
    Unique measures retain the historical `m_<field>` name; duplicates gain a
    short source suffix while their governed and Datastream identities remain in
    the schema.
    """
    counts: dict[str, int] = {}
    for member in members:
        for measure in member.get("measures") or []:
            field_id = str(measure["canonical_field_id"])
            counts[field_id] = counts.get(field_id, 0) + 1
    for member in members:
        source_suffix = str(member["datastream_id"]).rsplit("_", 1)[-1][-8:]
        for measure in member.get("measures") or []:
            field_id = str(measure["canonical_field_id"])
            measure["result_field"] = (
                f"m_{field_id}" if counts[field_id] == 1 else f"m_{field_id}__{source_suffix}"
            )


#: STORY 66.4 -- the cross-source temporal compatibility gate.
#:
#: The key VERSION is immutable; the mappings that implement it are not. A key
#: declared while both sources stored a calendar DATE keeps its content hash after
#: one of them republishes the same column as a TIMESTAMP, and the plan pinning it
#: would then match a day against an instant. `mdm_common_keys` refuses a
#: DECLARATION whose implementing types disagree; this refuses an EXECUTION whose
#: implementing types have since come to.
#:
#: The two halves are not the same refusal, and they are named apart:
#:  - `incompatible_key_types`  -- the classes differ (a string against an integer);
#:  - `false_day_equivalence`   -- both are temporal, at two different granularities.
#: The second borrows the semantic compiler's name on purpose: it is the same
#: defect, one layer out. A day matched against an instant silently keeps the rows
#: stamped at midnight and drops the rest of the day, and the loss reads as a trend.
#:
#: What this does NOT do: realign anything. Governed grains are dates and the
#: timezone module SIGNALS a misalignment rather than re-projecting rows
#: (`analyze-and-test.md`, the four-outcome table). Refusing IS the signal.
def _refuse_incompatible_time(
    *,
    component: Mapping[str, Any],
    left_name: str,
    right_name: str,
    left_binding: Mapping[str, Any],
    right_binding: Mapping[str, Any],
) -> None:
    """Raise what `incompatible_key_time` returns. The compiler's half."""
    refusal = incompatible_key_time(
        component=component,
        left_name=left_name,
        right_name=right_name,
        left_binding=left_binding,
        right_binding=right_binding,
    )
    if refusal is not None:
        raise PlanRefused(*refusal)


def incompatible_key_time(
    *,
    component: Mapping[str, Any],
    left_name: str,
    right_name: str,
    left_binding: Mapping[str, Any],
    right_binding: Mapping[str, Any],
) -> tuple[str, str] | None:
    """The refusal, or `None` -- ASKABLE, the way `unsupported_relationship` is.

    SPLIT FROM ITS RAISER ON PURPOSE, and for the reason this module already
    learned once: the match catalogue and the profile both import
    `unsupported_relationship` rather than restating what the executor supports,
    because while each parsed it on its own the two came to disagree and the
    screen advertised `ready` for a cross that ended in a named refusal one click
    later. The temporal gate was written as a raiser only, so the catalogue could
    not ask it, and it sold a day-against-instant cross as a viable candidate --
    the same defect, one gate later.
    """
    from core.datastream_field_mapping import (  # noqa: PLC0415
        date_granularity,
        physical_type_class,
    )

    left_type = str(left_binding.get("physical_type") or "")
    right_type = str(right_binding.get("physical_type") or "")
    left_class = physical_type_class(left_type, "")
    right_class = physical_type_class(right_type, "")
    # A type the classifier does not recognize is an `unknown_field` ambiguity the
    # mapping already raises. Refusing here would answer a question nobody asked.
    if "unknown" in (left_class, right_class):
        return

    name = component.get("canonical_name") or component.get("canonical_field_id")
    if left_class != right_class:
        return (
            "incompatible_key_types",
            f"{left_name} stores {name} as a {left_class} ({left_type}) and "
            f"{right_name} as a {right_class} ({right_type}). Two sources cannot be "
            f"matched on a column each stores as a different kind of value. Republish "
            f"the mapping that is wrong, then compile this cross again.",
        )
    if left_class != "date":
        return None

    left_grain = date_granularity(left_type)
    right_grain = date_granularity(right_type)
    if "unspecified" in (left_grain, right_grain) or left_grain == right_grain:
        return None
    finer, coarser = (
        (left_name, right_name) if left_grain == "instant" else (right_name, left_name)
    )
    # THE GESTURE HAS TO EXIST, and this one names the only two that do. There is
    # no timestamp-to-day treatment in the mapping contract (`column_treatments`
    # implements joins and splits, nothing else), so telling a person to "project
    # the column to a day in its mapping" sent them looking for a control the
    # product does not have. What they can actually do is pick the source's own
    # day column if it publishes one, or cross on a component both sides stamp
    # the same way.
    return (
        "false_day_equivalence",
        f"{finer} stamps {name} at an instant and {coarser} at a calendar day. "
        f"Matching them keeps only the rows stamped at midnight and drops the rest "
        f"of every day, and the loss reads as a trend. Cross these two on a "
        f"component both sources stamp the same way, or map {finer} to a column "
        f"that already carries a calendar day.",
    )


def _compile_edge(
    conn,
    project_id: str,
    edge: Mapping[str, Any],
    published: Mapping[str, Mapping[str, Any]],
    profile_lookup,
    deferred: list[dict[str, Any]],
) -> dict[str, Any]:
    key_version_id = str(edge.get("common_key_version_id") or "")
    if not key_version_id:
        # This is the raw fact-to-fact join, and it is refused at the door: two
        # sources with no declared shared identity have nothing to join ON.
        raise PlanRefused(
            "undeclared_common_key",
            "A cross names the common key version that gives the two sources one "
            "identity. Two fact tables joined without one is not an analysis.",
        )
    components = _key_components(conn, project_id, key_version_id)
    if not components:
        raise PlanRefused(
            "common_key_has_no_component",
            "That common key version declares no component.",
        )

    left, right = str(edge["left"]), str(edge["right"])
    relationships = _edge_relationships(conn, project_id, key_version_id, left, right)
    if not relationships:
        raise PlanRefused(
            "no_approved_relationship",
            "No published Semantic View relationship pins that common key version, so it "
            "names an identity nobody approved crossing on.",
        )
    wanted = edge.get("relationship_name")
    wanted_version = str(edge.get("view_version_id") or "")
    if wanted or wanted_version:
        chosen = [
            r
            for r in relationships
            if (not wanted or r["relationship_name"] == str(wanted))
            and (not wanted_version or str(r["view_version_id"]) == wanted_version)
        ]
        if not chosen:
            raise PlanRefused(
                "relationship_not_found",
                "The exact Semantic View relationship version does not pin that common key "
                "for this Datastream pair.",
            )
        if len(chosen) > 1:
            raise PlanRefused(
                "ambiguous_relationship",
                "More than one relationship matches that name. Pin its exact Semantic View "
                "version before compiling.",
            )
        relationship = chosen[0]
    elif len(relationships) > 1:
        # Equally valid paths: named, and left to the person.
        raise PlanRefused(
            "ambiguous_relationship",
            f"{len(relationships)} approved relationships pin that key. Choose one — the "
            "product does not pick between two equally valid paths.",
            detail=[r["relationship_name"] for r in relationships],
        )
    else:
        relationship = relationships[0]

    if str(relationship["left_datastream_id"]) != left:
        reversed_cardinality = {
            "many_to_one": "one_to_many",
            "one_to_many": "many_to_one",
        }.get(str(relationship.get("cardinality")), relationship.get("cardinality"))
        relationship = {
            **relationship,
            "cardinality": reversed_cardinality,
            "left_datastream_id": left,
            "right_datastream_id": right,
            "authored_direction_reversed": True,
        }
    else:
        relationship = {**relationship, "authored_direction_reversed": False}

    blocked = unsupported_relationship(relationship)
    if blocked is not None:
        raise PlanRefused(*blocked)

    # Read AFTER the orientation block above, and never before it: the
    # one-sided-duplicate mitigation below compares `left`'s duplicated keys
    # against this value, so it must be the cardinality as seen from the edge's
    # left member, not the one the author happened to store.
    cardinality = str(relationship.get("cardinality") or "")

    key_paths = []
    for component in components:
        field_id = str(component.get("canonical_field_id") or "")
        left_binding = _bindings(published[left]["mapping_payload"]).get(field_id)
        right_binding = _bindings(published[right]["mapping_payload"]).get(field_id)
        if not left_binding or not right_binding:
            side = published[left if not left_binding else right]["name"]
            raise PlanRefused(
                "key_component_unmapped",
                f"{side} maps no column to {component.get('canonical_name')}, so the two "
                "sources cannot be matched on this key.",
            )
        _refuse_incompatible_time(
            component=component,
            left_name=str(published[left]["name"]),
            right_name=str(published[right]["name"]),
            left_binding=left_binding,
            right_binding=right_binding,
        )
        key_paths.append(
            {
                "canonical_field_id": field_id,
                "canonical_name": component.get("canonical_name"),
                "left_field": left_binding["physical_field"],
                "right_field": right_binding["physical_field"],
                "left_physical_type": left_binding.get("physical_type") or None,
                "right_physical_type": right_binding.get("physical_type") or None,
            }
        )

    safety = None
    safety_mitigation = None
    evidence: dict[str, Any] = {}
    if profile_lookup is not None:
        profile = profile_lookup(
            left,
            right,
            key_version_id,
            str(relationship["relationship_name"]),
            str(relationship["view_version_id"]),
        )
        safety = (profile or {}).get("execution_safety")
        left_profile = (profile or {}).get("left") or {}
        right_profile = (profile or {}).get("right") or {}
        multiplication = (profile or {}).get("multiplication") or {}
        authority = (profile or {}).get("authority") or {}
        def _expected_snapshot(member: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "datastream_id": member.get("datastream_id"),
                "mapping_version_id": member.get("mapping_version_id"),
                "execution_id": member.get("published_execution_id"),
                "output_version_id": member.get("output_version_id"),
                "output_id": member.get("output_id"),
                "publication_log_id": member.get("publication_log_id"),
                "plan_version_id": member.get("plan_version_id"),
                "schema_hash": member.get("schema_hash"),
            }

        expected_authority = {
            "left": _expected_snapshot(published[left]),
            "right": _expected_snapshot(published[right]),
        }
        if authority != expected_authority:
            raise PlanRefused(
                "profile_authority_changed",
                "A source published a different Output while matching was measured. "
                "Inspect the refreshed match before compiling again.",
            )
        evidence = (profile or {}).get("evidence") or {}
        valid_until = evidence.get("valid_until")
        if not isinstance(valid_until, int):
            raise PlanRefused(
                "profile_expiry_missing",
                "The matching evidence has no bounded validity period.",
            )
        exact_one_sided_duplicates = (
            safety == "review_required"
            and left_profile.get("state") == "exact"
            and right_profile.get("state") == "exact"
            and multiplication.get("state") == "exact"
            and int(multiplication.get("worst_case_rows_per_key") or 1) > 1
            and (
                (
                    cardinality == "many_to_one"
                    and int(left_profile.get("duplicated_keys") or 0) > 0
                    and int(right_profile.get("duplicated_keys") or 0) == 0
                )
                or (
                    cardinality == "one_to_many"
                    and int(left_profile.get("duplicated_keys") or 0) == 0
                    and int(right_profile.get("duplicated_keys") or 0) > 0
                )
            )
        )
        if exact_one_sided_duplicates:
            # The executor aggregates EVERY member to the common-key grain before
            # joining CTEs. The measured duplicate is therefore folded once on the
            # declared many side, while the approved cardinality proves the other
            # side is unique. This is not a caller override and unavailable evidence
            # cannot enter this branch.
            #
            # IT IS NOT A BYPASS OF THE BOUND EITHER. This concession predates
            # AI-293 and used to leave `deferred` empty, which is how it crossed
            # `compile_plan` without the key/grain or additivity conditions ever
            # being read. The bound is now evaluated on every plan, so this branch
            # decides only that no refusal has to be lifted -- not that none has
            # to be checked.
            safety_mitigation = "aggregate_each_member_before_merge"
        elif safety != "ready":
            # DEFERRED, NOT LIFTED (AI-293). Whether this cross is refused now
            # depends on the plan's GRAIN and on the ADDITIVITY of what it
            # combines, and neither is compiled yet -- the bound is a property of
            # the whole plan, never of one edge. `compile_plan` resolves this the
            # moment dimensions and grain exist: inside the bound the merge is
            # admitted and this entry is dropped, outside it the refusal is raised
            # and names the condition that failed instead of this sentence.
            deferred.append(
                {
                    "code": "unsafe_fan_out" if safety == "unsafe" else "profile_not_ready",
                    "detail": multiplication,
                }
            )

    return {
        "left": left,
        "right": right,
        "common_key_version_id": key_version_id,
        "components": components,
        "key_paths": key_paths,
        "relationship": relationship,
        "measured_safety": safety,
        "safety_mitigation": safety_mitigation,
        "profile_evidence": evidence,
    }


def _compile_derived(
    raw: Sequence[Any],
    members: Sequence[Mapping[str, Any]],
    vocabulary: Mapping[str, Mapping[str, Any]] | None = None,
    names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """A ratio, declared as its two governed components -- never as a number.

    `SUM(revenue) / SUM(spend)` after aggregation, which is the rule the story
    53.2 amendment already states for rollups. A source cannot contribute a ratio
    directly: the sum of row ratios and the ratio of sums are different numbers,
    and only one of them is the answer.
    """
    selected = {
        str(measure["canonical_field_id"])
        for member in members
        for measure in member.get("measures") or []
    }
    compiled = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise PlanRefused("malformed_ratio", "Every ratio is an object.")
        for key in ("canonical_field_id", "numerator_field_id", "denominator_field_id"):
            if not entry.get(key):
                raise PlanRefused(
                    "malformed_ratio",
                    "A ratio names itself, its numerator and its denominator.",
                )
        for side in ("numerator_field_id", "denominator_field_id"):
            if str(entry[side]) not in selected:
                raise PlanRefused(
                    "ratio_component_not_selected",
                    _field_word(
                        str(entry["canonical_field_id"]),
                        vocabulary=vocabulary,
                        names=names,
                    )
                    + " divides by a measure this analysis does not select. A ratio is "
                    "recomputed from components that are actually in the answer, so ask "
                    "for both of them as measures too.",
                )
        governed = (vocabulary or {}).get(str(entry["canonical_field_id"])) or {}
        compiled.append(
            {
                "canonical_field_id": str(entry["canonical_field_id"]),
                "numerator_field_id": str(entry["numerator_field_id"]),
                "denominator_field_id": str(entry["denominator_field_id"]),
                # THE WORD TRAVELS WITH THE RATIO, FROZEN HERE, for the same
                # reason the unit does. The executor re-checks this ratio against
                # the aggregated members and can refuse it -- and it runs with no
                # connection to the registry, so the only word it can print is
                # the one the plan carries. Without it,
                # `multi_source_execution._derived_selects` printed
                # `mdm_<ULID>` at a reader. `_field_word` is the one chain that
                # produces this word, here as everywhere else in the module.
                "canonical_name": _field_word(
                    str(entry["canonical_field_id"]), vocabulary=vocabulary, names=names
                ),
                # A ratio states its own governed type: `cost per conversion` is
                # money and reads in its currency, `conversion rate` is a ratio
                # and reads as a ratio. Neither is inferred from its components.
                "value_type": governed.get("value_type"),
                "unit": governed.get("unit"),
            }
        )
    return compiled


def _edge_components(edges: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    available: dict[str, dict[str, Any]] = {}
    for edge in edges:
        for component in edge.get("components") or []:
            field_id = str(component.get("canonical_field_id") or "")
            if field_id:
                available.setdefault(field_id, dict(component))
    return available


def _compile_dimensions(
    raw: Sequence[Any], edges: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Dimensions exposed by the merged Result.

    Version 1 deliberately accepts only conformed key components. A dimension
    present on one fact only would change the other fact's grain and repeat its
    measures after the merge; that needs a separately governed bridge contract,
    never an optimistic browser selection.
    """
    if len(raw) > MAX_DIMENSIONS:
        raise PlanRefused(
            "too_many_dimensions", f"At most {MAX_DIMENSIONS} dimensions in one analysis."
        )
    available = _edge_components(edges)
    compiled = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("canonical_field_id"):
            raise PlanRefused("malformed_dimension", "Every dimension names a canonical field.")
        field_id = str(entry["canonical_field_id"])
        if field_id not in available:
            raise PlanRefused(
                "dimension_not_conformed",
                "A multi-source dimension must be a component of the selected governed "
                "matching path. A source-only field would multiply another source's measures.",
                detail=field_id,
            )
        if field_id in seen:
            raise PlanRefused(
                "duplicate_dimension", "A dimension is selected only once in one Result."
            )
        seen.add(field_id)
        component = available[field_id]
        compiled.append(
            {
                "canonical_field_id": field_id,
                "canonical_name": component.get("canonical_name"),
            }
        )
    return compiled


def _compile_grain(
    raw: Any, edges: Sequence[Mapping[str, Any]]
) -> tuple[str | None, str | None]:
    if raw is None or str(raw).strip() == "":
        return None, None
    wanted = str(raw).strip()
    normalized = wanted.casefold().replace("_", " ").replace("-", " ")
    available = _edge_components(edges)
    matches = [
        field_id
        for field_id, component in available.items()
        if field_id == wanted
        or str(component.get("canonical_name") or "")
        .casefold()
        .replace("_", " ")
        .replace("-", " ")
        == normalized
    ]
    if len(matches) != 1:
        raise PlanRefused(
            "grain_not_conformed",
            "The requested grain must identify exactly one component of the selected "
            "governed matching path.",
            detail=wanted,
        )
    return wanted, matches[0]


def _compile_filters(
    raw: Sequence[Any],
    members: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    selected_dimension_ids: set[str],
) -> list[dict[str, Any]]:
    member_ids = {member["datastream_id"] for member in members}
    conformed_fields = set(_edge_components(edges))
    if len(raw) > MAX_FILTERS:
        raise PlanRefused("too_many_filters", f"At most {MAX_FILTERS} filters in one analysis.")
    compiled = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise PlanRefused("malformed_filter", "Every filter is an object.")
        stage = str(entry.get("stage") or "")
        if stage not in FILTER_STAGES:
            raise PlanRefused(
                "filter_stage_required",
                "A filter says whether it narrows the rows a source aggregates "
                "(pre_aggregation) or the merged result (post_aggregation). The same "
                "predicate at the two stages is two different questions.",
            )
        if entry.get("sql") or entry.get("expression"):
            raise PlanRefused(
                "client_expression_refused",
                "A filter is a field, an operator and a value. Never a fragment of SQL.",
            )
        target = entry.get("datastream_id")
        if stage == "pre_aggregation":
            if not target or str(target) not in member_ids:
                raise PlanRefused(
                    "filter_names_no_source",
                    "A pre-aggregation filter names which source it narrows.",
                )
        field_id = str(entry.get("canonical_field_id") or "")
        if not field_id:
            raise PlanRefused("filter_field_required", "A filter names one canonical field.")
        if field_id not in conformed_fields:
            raise PlanRefused(
                "filter_field_not_conformed",
                "A filter can only use a dimension carried by the selected governed path.",
                detail=sorted(conformed_fields),
            )
        if stage == "post_aggregation" and field_id not in selected_dimension_ids:
            raise PlanRefused(
                "post_filter_field_not_selected",
                "A post-aggregation filter must name a dimension projected by this Result.",
                detail=sorted(selected_dimension_ids),
            )
        operator = str(entry.get("operator") or "")
        if operator not in {"eq", "neq", "gt", "gte", "lt", "lte"}:
            raise PlanRefused(
                "filter_operator_required",
                "A filter operator is one of eq, neq, gt, gte, lt or lte.",
            )
        compiled.append(
            {
                "stage": stage,
                "datastream_id": str(target) if target else None,
                "canonical_field_id": field_id,
                "operator": operator,
                "value": entry.get("value"),
            }
        )
    return compiled


def _bounded_int(value: Any, default: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PlanRefused("malformed_bound", f"{name} is a whole number.") from exc
    if number < 1 or number > maximum:
        raise PlanRefused(
            "bound_out_of_range", f"{name} is between 1 and {maximum}; {number} was sent."
        )
    return number


# ---------------------------------------------------------------------------
# Persistence -- an immutable version beside the single-source ones
# ---------------------------------------------------------------------------


def store_plan_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    compiled: Mapping[str, Any],
    actor: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Append one immutable Query Spec version carrying the plan.

    IT SHARES THE TABLE AND NOT THE DOCUMENT. `spec.contract_version` is
    `multi-source-plan.v1`, so a reader that only knows `query-spec.v1` sees a
    contract it does not understand and must refuse the row rather than read a
    single-source shape into it. Existing rows are untouched and their bytes
    unchanged, which is the compatibility NFR10 asks for.
    """
    plan = compiled["plan"]
    view_version_id = (plan.get("semantic_view_version_ids") or [None])[0]
    if not view_version_id:
        raise PlanRefused(
            "plan_pins_no_view_version",
            "A stored plan pins the published Semantic View version its crosses come from.",
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT view_id FROM app.semantic_view_versions WHERE id = %s AND project_id = %s",
            (view_version_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise PlanRefused(
                "plan_pins_no_view_version",
                "The Semantic View version this plan pins is not in this Project.",
            )
        view_id = row[0]

        spec_id = f"qs_{ULID()}"
        version_id = f"qsv_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.query_specs
                (id, org_id, project_id, semantic_view_id, name, created_by)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (spec_id, org_id, project_id, view_id, name, actor),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES (%s,%s,%s,%s,1,%s,%s,%s::jsonb,%s,%s)
            """,
            (
                version_id,
                spec_id,
                org_id,
                project_id,
                view_id,
                view_version_id,
                canonical_json(plan),
                compiled["content_hash"],
                actor,
            ),
        )
        cur.execute(
            "UPDATE app.query_specs SET current_version_id = %s WHERE id = %s",
            (version_id, spec_id),
        )
    return {
        "query_spec_id": spec_id,
        "query_spec_version_id": version_id,
        "content_hash": compiled["content_hash"],
        "contract_version": MULTI_SOURCE_PLAN_CONTRACT_VERSION,
    }


def merge_bound_verdict(conn, *, project_id: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Is this FROZEN plan inside the merge bound, read now? A read, never a write.

    WHY THE BOUND IS RE-EVALUATED AT EXECUTION AND NOT TRUSTED FROM THE PLAN.
    Until this function existed, `execute_plan` read `plan["merge_bound"]` only to
    decide whether matching evidence was optional -- the bound itself was never
    checked again. A plan frozen before the bound was evaluated on every path
    (commit `a7d4fd9f`) is IMMUTABLE and therefore still out of bound, and it kept
    executing with the very defect that commit calls "a real defect and not a
    theoretical one": every member folded to the full merge key while only the
    selected dimensions are projected.

    A PLAN IS NEVER REWRITTEN TO REPAIR IT. An immutable document says what was
    decided; it is the READING that refuses. So this returns the same verdict
    shape `_merge_bound` returns and `execute_plan` raises on it, and nothing on
    the stored row is touched.

    IT IS ALSO RE-EVALUATED AGAINST TODAY'S VOCABULARY, not a snapshot. Whether a
    measure may be folded is a governed fact that can change after a plan is
    frozen -- a metric reclassified as non-additive, or archived -- and reading
    yesterday's permission would be the same fail-open this bound exists to close.
    """
    vocabulary, readable = _governed_vocabulary(conn, project_id)
    identities, identities_readable = _reader_identities(conn, project_id, plan)
    return _merge_bound(
        plan.get("members") or [],
        plan.get("edges") or [],
        plan.get("dimensions") or [],
        plan.get("grain_canonical_field_id"),
        _metric_additivity(project_id, vocabulary),
        vocabulary,
        additivity_readable=readable,
        names=_names_of(identities),
        archived=_archived_of(identities),
        identities_readable=identities_readable,
    )


def load_plan_version(conn, *, project_id: str, query_spec_version_id: str) -> dict[str, Any]:
    """Read a stored plan back, refusing a row of a contract this does not own."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT spec, content_hash, semantic_view_version_id, created_at
              FROM app.query_spec_versions
             WHERE id = %s AND project_id = %s
            """,
            (query_spec_version_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise PlanRefused("plan_not_found", "That plan is not in this Project.")
    spec = row[0] if isinstance(row[0], dict) else {}
    if spec.get("contract_version") != MULTI_SOURCE_PLAN_CONTRACT_VERSION:
        raise PlanRefused(
            "not_a_multi_source_plan",
            "That Query Spec version is a single-source request. Reading it as a plan "
            "would invent members it never had.",
        )
    return {
        "plan": spec,
        "content_hash": row[1],
        "semantic_view_version_id": row[2],
        "created_at": row[3].isoformat() if row[3] is not None else None,
    }
