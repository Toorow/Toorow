"""Story 66.2 -- which published Datastreams can usefully be crossed, and why.

WHAT THIS ANSWERS. A person holding two Datastreams had no way to learn that they
speak about the same business identity, and no way to act on the intuition that
they do. This module produces that answer for a Project: the governed crosses it
can actually execute, the candidates a governor could turn into one, and for each
of them the sentence that says what the cross unlocks.

DERIVED FROM THE KEY, NOT FROM THE RELATIONSHIP'S DATASETS. A relationship names
`from_dataset` / `to_dataset` -- Semantic View dataset names -- and the dataset a
concept sits on is NOT stored on `app.semantic_view_version_concepts` (measured on
the live schema, 2026-08-13: it carries `view_version_id, ordinal, concept_id,
concept_version_id, role`). Deriving Datastreams from those names would mean
re-deriving them from a compiled artifact, which is a second authority that drifts.

So a governed match is composed the way the epic decided it:

    one MDM common key version                     -> the shared identity
    pinned by a relationship of a PUBLISHED view   -> the executability
    every component implemented on BOTH sides      -> the physical reality

Miss the middle line and it is a CANDIDATE: identity without permission. Miss the
first and it is a candidate of the other kind: two columns that look alike.

THREE STATES, NEVER ONE BADGE. Authority (`governed` / `needs_governance`),
observed coverage (`exact` / `estimated` / `unavailable`) and execution safety
(`ready` / `review_required` / `unsafe`) fail separately and are reported
separately. Story 66.2 measures no rows, so `observed_coverage` is `unavailable`
everywhere here -- said out loud, because an absent field would be read as `exact`
by the first surface that forgot.

NO CONFIDENCE SCORE. The order is a deterministic tuple. An opaque 0.87 would be
unarguable and unreproducible; a tuple can be read back off the response.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping, Sequence

#: The binding statuses that count as a physical implementation -- the same
#: reading `mdm_common_keys` applies, imported rather than retyped so the two
#: answers cannot diverge.
from core.canonical_field_registry import list_visible_canonical_fields
from core.mdm_common_keys import IMPLEMENTING_BINDING_STATUSES

#: What the multi-source EXECUTOR cannot run. Imported so discovery never
#: advertises `ready` for a relationship compilation would refuse.
from core.multi_source_plan import unsupported_relationship

logger = logging.getLogger(__name__)

#: A read of this catalog never walks more than this many Datastreams, and never
#: returns more than this many matches. Both bounds travel in the response.
MAX_DATASTREAMS_SCANNED = 200
MAX_MATCHES = 50
MAX_MEASURES_PER_DATASTREAM = 50
MAX_RESPONSE_BYTES = 262_144

#: Cardinalities that need no human before a first execution. `many_to_many` is
#: absent on purpose: the schema only stores it WITH a bridge, and a bridge is a
#: decision somebody should look at before it multiplies a measure.
#:
#: NOT THE WHOLE ANSWER, and 2026-08-16 measured what that cost. A cardinality
#: is one of THREE things the executor looks at; `fan_out_policy` and
#: `bridge_dataset` are the other two, and this set knows nothing about them. A
#: `many_to_one` relationship that deduplicates is safe by cardinality and
#: refused by `compile_plan`. `_safety` therefore asks the executor FIRST — see
#: `unsupported_relationship` — and consults this set only once the executor has
#: no objection.
_SAFE_CARDINALITIES = frozenset({"one_to_one", "many_to_one", "one_to_many"})


class MatchesUnavailable(RuntimeError):
    """The catalog could not be read. Not an empty catalog -- a failed one."""


class DatastreamNotFound(LookupError):
    """Foreign, denied and nonexistent Datastreams are one answer."""


# ---------------------------------------------------------------------------
# Reading the world
# ---------------------------------------------------------------------------


def _published_datastreams(conn, project_id: str) -> tuple[list[dict[str, Any]], bool]:
    """Datastreams that publish a mapping version, with their bound canonical fields.

    A Datastream with no `current_mapping_version_id` publishes nothing, and this
    read drops it entirely: it must not appear in a match, in a count or in a
    refusal message. What it needs is a mapping, and that sentence belongs to the
    Datastream's own screen, not to a list of crosses.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.name, d.current_mapping_version_id, d.current_published_execution_id,
                   m.mapping_payload, m.version_number, ov.id, ov.output_id,
                   ov.publication_log_id, ov.plan_version_id, ov.relation_ref, ov.schema_hash
              FROM app.datastreams d
              JOIN app.datastream_mapping_versions m
                ON m.id = d.current_mapping_version_id
              JOIN LATERAL (
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
             WHERE d.project_id = %(project_id)s
               AND d.archived_at IS NULL
             ORDER BY lower(d.name), d.id
             LIMIT %(limit)s
            """,
            {"project_id": project_id, "limit": MAX_DATASTREAMS_SCANNED + 1},
        )
        rows = cur.fetchall()

    scan_truncated = len(rows) > MAX_DATASTREAMS_SCANNED
    rows = rows[:MAX_DATASTREAMS_SCANNED]

    datastreams: list[dict[str, Any]] = []
    for row in rows:
        payload = row[4] if isinstance(row[4], dict) else {}
        datastreams.append(
            {
                "id": row[0],
                "name": row[1],
                "mapping_version_id": row[2],
                "published_execution_id": row[3],
                "mapping_version_number": row[5],
                "output_version_id": row[6],
                "output_id": row[7],
                "publication_log_id": row[8],
                "plan_version_id": row[9],
                "relation_ref": row[10],
                "schema_hash": row[11],
                "bindings": _bindings_of(payload),
                # The TYPE beside the column, because the temporal gate compares
                # types and this catalogue has to ask it the same question the
                # compiler will.
                "binding_types": _binding_types_of(payload),
                "physical_fields": _physical_fields(payload),
                "measures": _measures_of(payload),
                "measure_count": _measure_count(payload),
            }
        )
    return datastreams, scan_truncated


def _bindings_of(payload: Mapping[str, Any]) -> dict[str, str]:
    """canonical field id -> physical field id, for implemented bindings only."""
    bound: dict[str, str] = {}
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        binding = field.get("binding")
        if not isinstance(binding, dict):
            continue
        target = binding.get("mdm_target")
        if not target or binding.get("status") not in IMPLEMENTING_BINDING_STATUSES:
            continue
        bound.setdefault(str(target), str(field.get("field_id") or ""))
    return bound


def _binding_types_of(payload: Mapping[str, Any]) -> dict[str, str]:
    """canonical field id -> declared physical type, for implemented bindings."""
    typed: dict[str, str] = {}
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        binding = field.get("binding")
        if not isinstance(binding, dict):
            continue
        target = binding.get("mdm_target")
        if not target or binding.get("status") not in IMPLEMENTING_BINDING_STATUSES:
            continue
        typed.setdefault(str(target), str(field.get("physical_type") or ""))
    return typed


def _physical_fields(payload: Mapping[str, Any]) -> dict[str, bool]:
    """physical field id -> whether it is bound to a canonical field."""
    fields: dict[str, bool] = {}
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
        if binding.get("status") == "excluded":
            continue
        field_id = str(field.get("field_id") or "")
        if not field_id:
            continue
        fields[field_id] = bool(binding.get("mdm_target"))
    return fields


def _measure_count(payload: Mapping[str, Any]) -> int:
    """How many measures this side publishes -- what a cross makes queryable."""
    count = 0
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        suggestion = field.get("suggestion") if isinstance(field.get("suggestion"), dict) else {}
        binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
        if binding.get("status") not in IMPLEMENTING_BINDING_STATUSES:
            continue
        if str(suggestion.get("semantic_role") or "").startswith("measure"):
            count += 1
    return count


def _measures_of(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Governed measures a caller may select, never inferred from raw numbers."""
    measures: list[dict[str, str]] = []
    seen: set[str] = set()
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        suggestion = field.get("suggestion") if isinstance(field.get("suggestion"), dict) else {}
        binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
        field_id = str(binding.get("mdm_target") or "")
        if (
            not field_id
            or field_id in seen
            or binding.get("status") not in IMPLEMENTING_BINDING_STATUSES
            or not str(suggestion.get("semantic_role") or "").startswith("measure")
        ):
            continue
        seen.add(field_id)
        measures.append(
            {
                "canonical_field_id": field_id,
                "name": str(suggestion.get("canonical_name") or field.get("field_id") or field_id),
                "aggregation": str(suggestion.get("aggregation") or "sum"),
            }
        )
    return measures[:MAX_MEASURES_PER_DATASTREAM]


def _executable_key_versions(conn, project_id: str) -> dict[str, list[dict[str, Any]]]:
    """key version id -> the PUBLISHED relationships that pin it.

    A relationship inside a draft, candidate, superseded or archived view version
    is not permission to do anything. Only `published` is read here, and the join
    is the whole authority half of a governed match.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.mdm_common_key_version_id, r.name, r.from_dataset, r.to_dataset,
                   r.cardinality_type, r.fan_out_policy, r.bridge_dataset,
                   v.id, v.view_id, v.version_number, s.name,
                   r.left_datastream_id, r.right_datastream_id,
                   v.business_domain_refs
              FROM app.semantic_view_version_relationships r
              JOIN app.semantic_view_versions v ON v.id = r.view_version_id
              JOIN app.semantic_views s ON s.id = v.view_id
             WHERE v.project_id = %(project_id)s
               AND v.status = 'published'
               AND r.mdm_common_key_version_id IS NOT NULL
               AND r.left_datastream_id IS NOT NULL
               AND r.right_datastream_id IS NOT NULL
             ORDER BY v.version_number DESC, r.ordinal
            """,
            {"project_id": project_id},
        )
        rows = cur.fetchall()

    pinned: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        pinned.setdefault(str(row[0]), []).append(
            {
                "relationship_name": row[1],
                "from_dataset": row[2],
                "to_dataset": row[3],
                "cardinality": row[4],
                "fan_out_policy": row[5],
                "bridge_dataset": row[6],
                "view_version_id": row[7],
                "view_id": row[8],
                "view_version_number": row[9],
                "view_name": row[10],
                "left_datastream_id": row[11],
                "right_datastream_id": row[12],
                "business_domain_refs": list(row[13] or []),
            }
        )
    return pinned


#: The SAME predicate, under a public name -- story 70.3.
#:
#: Analytics Alignment has to answer "is crossing these two Datastreams
#: approved?", which is this exact read: published views only, a pinned key
#: version, two named Datastream sides. It imports this instead of restating the
#: SQL, because this module already learned what a second copy of a predicate
#: costs -- `unsupported_relationship` was parsed twice, the two copies came to
#: disagree, and the screen advertised `ready` for a cross that ended in a named
#: refusal one click later. A third copy of the authority half would reopen the
#: same defect one surface further out.
executable_key_versions = _executable_key_versions


def _key_versions(conn, project_id: str) -> list[dict[str, Any]]:
    """Every ACTIVE key's current version, with its ordered components."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT k.id, k.name, v.id, v.version_number, v.components, v.content_hash
              FROM app.mdm_common_keys k
              JOIN app.mdm_common_key_versions v ON v.id = k.current_version_id
             WHERE k.project_id = %(project_id)s AND k.status = 'active'
             ORDER BY lower(k.name), k.id
            """,
            {"project_id": project_id},
        )
        rows = cur.fetchall()
    return [
        {
            "key_id": row[0],
            "key_name": row[1],
            "key_version_id": row[2],
            "key_version_number": row[3],
            "components": row[4] or [],
            "content_hash": row[5],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Composing a match
# ---------------------------------------------------------------------------


def _component_names(components: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(component.get("canonical_name") or "") for component in components]


def _implements_all(datastream: Mapping[str, Any], components: Sequence[Mapping[str, Any]]) -> bool:
    bindings = datastream["bindings"]
    return all(
        str(component.get("canonical_field_id") or "") in bindings for component in components
    )


def _key_paths(
    left: Mapping[str, Any], right: Mapping[str, Any], components: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Both physical paths, component by component. This is the evidence a user reads."""
    paths = []
    for component in components:
        field_id = str(component.get("canonical_field_id") or "")
        paths.append(
            {
                "canonical_name": component.get("canonical_name"),
                "canonical_field_id": field_id,
                "left_field": left["bindings"].get(field_id),
                "right_field": right["bindings"].get(field_id),
                # Frozen beside the column for the same reason the plan freezes
                # them: a pin nobody can read is a pin nobody can check.
                "left_physical_type": (left.get("binding_types") or {}).get(field_id) or None,
                "right_physical_type": (right.get("binding_types") or {}).get(field_id) or None,
            }
        )
    return paths


def _ambiguous_safety(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    relationships: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """`review_required` when a choice helps, `unsafe` when no choice does.

    Picking between two paths is a human decision, so ambiguity is normally the
    one thing review really does unblock. But if the executor refuses EVERY
    candidate -- or the columns themselves cannot be matched -- then no choice
    leads anywhere, and telling a person to choose is telling them to spend a
    decision on nothing.
    """
    temporal = _temporal_block(left, right, components)
    if temporal is not None:
        return {"execution_safety": "unsafe", "execution_blocked": temporal}
    blocks = [_execution_blocked(relationship) for relationship in relationships]
    if blocks and all(block is not None for block in blocks):
        return {"execution_safety": "unsafe", "execution_blocked": blocks[0]}
    return {"execution_safety": "review_required", "execution_blocked": None}


def _temporal_block(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
) -> dict[str, str] | None:
    """The compiler's temporal refusal for this pair, or `None`.

    IMPORTED, NEVER RESTATED -- the same posture this module already holds for
    `unsupported_relationship`, and for the same measured reason. The temporal
    gate lived only in `_compile_edge`, so this catalogue advertised a
    day-against-instant cross as a viable candidate, let a person open it,
    measure it, press Run, and meet `false_day_equivalence` one click later.
    That is the exact sequence the 2026-08-16 repair exists to prevent.
    """
    from core.multi_source_plan import incompatible_key_time  # noqa: PLC0415

    left_types = left.get("binding_types") or {}
    right_types = right.get("binding_types") or {}
    for component in components:
        field_id = str(component.get("canonical_field_id") or "")
        refusal = incompatible_key_time(
            component=component,
            left_name=str(left.get("name") or ""),
            right_name=str(right.get("name") or ""),
            left_binding={"physical_type": left_types.get(field_id) or ""},
            right_binding={"physical_type": right_types.get(field_id) or ""},
        )
        if refusal is not None:
            return {"code": refusal[0], "message": refusal[1]}
    return None


def _execution_blocked(relationship: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The executor's own refusal for this relationship, or `None`.

    Imported, never restated: `multi_source_plan` owns what its executor can run,
    and a second copy here is precisely how the two came to disagree.
    """
    if relationship is None:
        return None
    blocked = unsupported_relationship(relationship)
    if blocked is None:
        return None
    return {"code": blocked[0], "message": blocked[1]}


def _safety(relationship: Mapping[str, Any] | None) -> str:
    if relationship is None:
        # A candidate needs a governor, not a warning label. `unsafe` would say
        # "this would break"; what is true is "nobody has approved this yet".
        return "review_required"
    if _execution_blocked(relationship) is not None:
        # NOT `review_required`, and the difference is the whole repair. Review
        # means "somebody should look before this runs" — it promises that a
        # human decision unblocks it. No human decision unblocks a bridge or a
        # deduplication the executor has no path for; approving it would end in
        # the same named refusal, one click later. `unsafe` is the honest word,
        # and `execution_blocked` carries WHICH refusal beside it so the screen
        # can say it instead of a badge nobody can act on.
        return "unsafe"
    if str(relationship.get("cardinality")) in _SAFE_CARDINALITIES:
        return "ready"
    return "review_required"


def _analysis_sentence(
    left_name: str, right_name: str, component_names: Sequence[str]
) -> str:
    by = " and ".join(component_names) if component_names else "their shared identity"
    return f"{left_name} and {right_name}, by {by}."


def _governed_match(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    key: Mapping[str, Any],
    relationship: Mapping[str, Any],
) -> dict[str, Any]:
    components = key["components"]
    names = _component_names(components)
    oriented = dict(relationship)
    #  ORIENTER, C EST RETOURNER TOUT CE QUI A UN SENS -- pas seulement ce dont
    #  on se sert. Jusqu au 2026-08-16 cette branche retournait les deux
    #  Datastreams et la cardinalite, et laissait `from_dataset` / `to_dataset`
    #  dans le sens AUTEUR. La charge se contredisait alors elle-meme : un lecteur
    #  qui prenait `from_dataset` a cote de `left_datastream_id` lisait deux
    #  directions opposees dans le meme objet. Le preambule de ce module explique
    #  qu on ne DERIVE rien de ces deux noms -- il n explique pas qu on ait le
    #  droit de les publier faux.
    oriented["authored_direction_reversed"] = False
    if str(oriented.get("left_datastream_id")) != str(left["id"]):
        oriented["left_datastream_id"], oriented["right_datastream_id"] = (
            oriented.get("right_datastream_id"),
            oriented.get("left_datastream_id"),
        )
        oriented["from_dataset"], oriented["to_dataset"] = (
            oriented.get("to_dataset"),
            oriented.get("from_dataset"),
        )
        oriented["cardinality"] = {
            "many_to_one": "one_to_many",
            "one_to_many": "many_to_one",
        }.get(str(oriented.get("cardinality")), oriented.get("cardinality"))
        #  Le mot est celui du compilateur (`multi_source_plan`), pas un second :
        #  savoir qu on lit une relation retournee est ce qui permet de la
        #  retrouver dans la Semantic View, ou elle est ecrite dans l autre sens.
        oriented["authored_direction_reversed"] = True
    # TWO GATES, ONE ANSWER. `_execution_blocked` asks the executor about the
    # RELATIONSHIP; `_temporal_block` asks it about the COLUMNS the key is
    # implemented by. Both are the compiler's own predicates, imported. A cross
    # this catalogue advertised as viable while the second gate would refuse it
    # is the 2026-08-16 defect exactly, one gate later.
    blocked = _execution_blocked(oriented) or _temporal_block(left, right, components)
    return {
        "kind": "governed",
        "authority": "governed",
        "observed_coverage": "unavailable",
        "execution_safety": "unsafe" if blocked is not None else _safety(oriented),
        # The executor's own refusal, whole, when there is one. `null` means the
        # executor has no objection — never "we did not look".
        "execution_blocked": blocked,
        "left": _side(left),
        "right": _side(right),
        "common_key": {
            "id": key["key_id"],
            "name": key["key_name"],
            "version_id": key["key_version_id"],
            "version_number": key["key_version_number"],
            "components": components,
        },
        "key_paths": _key_paths(left, right, components),
        "relationship": oriented,
        "unlocked_measures": int(left["measure_count"]) + int(right["measure_count"]),
        "analysis": _analysis_sentence(left["name"], right["name"], names),
        # The handoff is the exact state that was read to produce this line. A
        # consumer that re-resolved a head would open a different question from
        # the one the person was looking at.
        "explore_together": {
            "datastreams": [
                {
                    "datastream_id": left["id"],
                    "mapping_version_id": left["mapping_version_id"],
                    "published_execution_id": left["published_execution_id"],
                    "output_version_id": left["output_version_id"],
                },
                {
                    "datastream_id": right["id"],
                    "mapping_version_id": right["mapping_version_id"],
                    "published_execution_id": right["published_execution_id"],
                    "output_version_id": right["output_version_id"],
                },
            ],
            "common_key_version_id": key["key_version_id"],
            "view_version_id": relationship["view_version_id"],
            "relationship_name": relationship["relationship_name"],
        },
    }


def _candidate_key_missing(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    shared: Sequence[Mapping[str, Any]],
    *,
    key: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    names = [str(entry["canonical_name"]) for entry in shared]
    # LA MEME QUESTION, UNE PORTE PLUS LOIN. `_governed_match` et
    # `_ambiguous_safety` interrogeaient le refus temporel, pas celle-ci -- et
    # c'est la branche qui dit « approuvez une relation ». Mesure : meme paire
    # jour-contre-instant, cle implementee des deux cotes, relation pas encore
    # epinglee -> `review_required`, `execution_blocked: None`, et le geste
    # propose menait tout droit a `false_day_equivalence`. « Une decision humaine
    # debloque ceci » quand aucune ne le fait, exactement la phrase que
    # l'amendement du 2026-08-16 interdit.
    temporal = _temporal_block(left, right, shared)
    # EMITTED, NOT OMITTED. `explorerClient.ts` declares `ExecutionBlock | null`
    # and the governed branch's own comment promises `null` means "the executor
    # has no objection, never we did not look". Leaving the key out of the other
    # branches made a reader see `undefined` where the contract says `null` — the
    # third answer the contract exists to forbid.
    return {
        "kind": "candidate_key_missing",
        "authority": "needs_governance",
        "observed_coverage": "unavailable",
        "execution_safety": "unsafe" if temporal is not None else "review_required",
        "execution_blocked": temporal,
        "left": _side(left),
        "right": _side(right),
        "common_key": (
            {
                "id": key["key_id"],
                "name": key["key_name"],
                "version_id": key["key_version_id"],
                "version_number": key["key_version_number"],
                "components": shared,
            }
            if key is not None
            else None
        ),
        "key_paths": [
            {
                "canonical_name": entry["canonical_name"],
                "canonical_field_id": entry["canonical_field_id"],
                "left_field": left["bindings"].get(entry["canonical_field_id"]),
                "right_field": right["bindings"].get(entry["canonical_field_id"]),
            }
            for entry in shared
        ],
        "relationship": None,
        "unlocked_measures": int(left["measure_count"]) + int(right["measure_count"]),
        "analysis": _analysis_sentence(left["name"], right["name"], names),
        "next_action": (
            # LE GESTE SUIT LE VERDICT. Proposer d'approuver une relation quand
            # l'executeur refusera les colonnes envoie depenser une decision pour
            # rien : le refus dit deja ce qu'il faut faire, et il le dit mieux.
            temporal["message"]
            if temporal is not None
            else (
                "Both sources implement the declared common key "
                f"{key['key_name']}. Approve one exact relationship for this Datastream pair."
            )
            if key is not None
            else (
                "Both sources describe "
                + " and ".join(names)
                + ". Declare a common key over those fields, then approve the relationship "
                "that crosses them."
            )
        ),
        "explore_together": None,
    }


def _candidate_binding_missing(
    left: Mapping[str, Any], right: Mapping[str, Any], columns: Sequence[str]
) -> dict[str, Any]:
    named = ", ".join(sorted(columns)[:5])
    return {
        "kind": "candidate_binding_missing",
        "authority": "needs_governance",
        "observed_coverage": "unavailable",
        "execution_safety": "review_required",
        "execution_blocked": None,
        "left": _side(left),
        "right": _side(right),
        "common_key": None,
        "key_paths": [
            {
                "canonical_name": None,
                "canonical_field_id": None,
                "left_field": column,
                "right_field": column,
            }
            for column in sorted(columns)[:5]
        ],
        "relationship": None,
        "unlocked_measures": int(left["measure_count"]) + int(right["measure_count"]),
        "analysis": (
            f"{left['name']} and {right['name']} both carry {named}, but nothing says "
            "the two mean the same thing."
        ),
        "next_action": (
            f"Map {named} to a canonical field on both sources, then declare a common key "
            "over it. Two columns with the same name are not an identity."
        ),
        "explore_together": None,
    }


def _side(datastream: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "datastream_id": datastream["id"],
        "name": datastream["name"],
        "mapping_version_id": datastream["mapping_version_id"],
        "mapping_version_number": datastream["mapping_version_number"],
        "published_execution_id": datastream["published_execution_id"],
        "output_version_id": datastream["output_version_id"],
        "measures": list(datastream.get("measures") or ()),
    }


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def rank_key(match: Mapping[str, Any]) -> tuple:
    """The deterministic order, readable straight off the response.

    1. governed before candidate -- a cross a user can run outranks one they must
       first get approved;
    2. more unlocked measures -- the cross that answers more questions;
    3. fewer key components -- the shorter identity is the safer one to start from;
    4. stable identity -- key id then both Datastream ids, so two runs on unchanged
       data return the same order and a test can shuffle the input to prove it.
    """
    governed = 0 if match["authority"] == "governed" else 1
    components = match.get("common_key") or {}
    component_count = len(components.get("components") or []) if components else 99
    return (
        governed,
        -int(match.get("unlocked_measures") or 0),
        component_count,
        str((match.get("common_key") or {}).get("version_id") or ""),
        str(match["left"]["datastream_id"]),
        str(match["right"]["datastream_id"]),
    )


def rank(matches: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(match) for match in sorted(matches, key=rank_key)]


# ---------------------------------------------------------------------------
# The composition itself
# ---------------------------------------------------------------------------


def discover_matches(conn, *, project_id: str) -> dict[str, Any]:
    """Every governed cross and candidate of the Project, ranked and bounded."""
    try:
        datastreams, datastream_scan_truncated = _published_datastreams(conn, project_id)
        keys = _key_versions(conn, project_id)
        pinned = _executable_key_versions(conn, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("datastream_matches: read failed project=%s: %s", project_id, exc)
        raise MatchesUnavailable(str(exc)) from exc

    try:
        names = {
            row["id"]: row["canonical_name"]
            for row in list_visible_canonical_fields(conn, project_id=project_id)
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("datastream_matches: vocabulary read failed project=%s: %s", project_id, exc)
        raise MatchesUnavailable(str(exc)) from exc

    matches: list[dict[str, Any]] = []
    for index, left in enumerate(datastreams):
        for right in datastreams[index + 1 :]:
            matches.extend(_pair_matches(left, right, keys, pinned, names))

    ordered = rank(matches)
    selected = ordered[:MAX_MATCHES]
    truncated = datastream_scan_truncated or len(ordered) > MAX_MATCHES
    response = {
        "matches": selected,
        "counts": {
            "datastreams_published": len(datastreams),
            "governed": sum(1 for m in ordered if m["authority"] == "governed"),
            "candidates": sum(1 for m in ordered if m["authority"] != "governed"),
            "returned": len(selected),
        },
        "bounds": {
            "max_datastreams_scanned": MAX_DATASTREAMS_SCANNED,
            "max_matches": MAX_MATCHES,
            "max_measures_per_datastream": MAX_MEASURES_PER_DATASTREAM,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "truncated": truncated,
            "datastream_scan_truncated": datastream_scan_truncated,
        },
        "observed_coverage_state": "unavailable",
        "empty_reason": _empty_reason(datastreams) if not ordered else None,
    }
    #  UNE SEULE BOUCLE DE ROGNAGE (2026-08-16). Il y en avait TROIS imbriquees,
    #  et la premiere mesurait une charge a laquelle il manquait encore
    #  `response_bytes` -- donc elle rognait d apres une taille que la reponse
    #  n aurait jamais. La troisieme refaisait le meme travail par-dessus, avec le
    #  point fixe recopie a l interieur. Meme invariant, un seul endroit ou le
    #  lire : la reponse tient dans son budget ET dit sa propre taille.
    _settle_response_bytes(response)
    while selected and len(_canonical_bytes(response)) > MAX_RESPONSE_BYTES:
        selected.pop()
        response["counts"]["returned"] = len(selected)
        response["bounds"]["truncated"] = True
        _settle_response_bytes(response)
    return response


def _settle_response_bytes(response: dict[str, Any]) -> None:
    """Write the response's own size INTO it, until writing it stops changing it.

    Un point fixe, et il en faut un : le nombre fait partie de la charge, donc
    l ecrire la fait grossir, donc le nombre devient faux. Deux ou trois tours
    suffisent et la boucle s arrete d elle-meme -- la taille croit, jamais ne
    revient, et chaque tour ajoute au plus un chiffre.
    """
    response["bounds"]["response_bytes"] = 0
    while True:
        measured = len(_canonical_bytes(response))
        if response["bounds"]["response_bytes"] == measured:
            return
        response["bounds"]["response_bytes"] = measured


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _pair_matches(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    keys: Sequence[Mapping[str, Any]],
    pinned: Mapping[str, list[dict[str, Any]]],
    names: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Every match these two Datastreams produce: governed first, else one candidate."""
    produced: list[dict[str, Any]] = []

    for key in keys:
        components = key["components"]
        if not components:
            continue
        if not (_implements_all(left, components) and _implements_all(right, components)):
            continue
        relationships = [
            relationship
            for relationship in (pinned.get(str(key["key_version_id"])) or [])
            if {str(relationship["left_datastream_id"]), str(relationship["right_datastream_id"])}
            == {str(left["id"]), str(right["id"])}
        ]
        if len(relationships) > 1:
            produced.append(
                {
                    "kind": "ambiguous_relationship",
                    "authority": "governed",
                    "observed_coverage": "unavailable",
                    # THE AMBIGUITY IS A HUMAN QUESTION; THE REFUSAL IS NOT. This
                    # branch hardcoded `review_required` and asked the executor
                    # nothing, so two pinned `many_to_many` relationships read
                    # "a human decision unblocks this" when no human decision
                    # does — the very sentence the 2026-08-16 amendment forbids.
                    # When EVERY candidate relationship is refused, choosing
                    # between them changes nothing: the honest word is `unsafe`.
                    **_ambiguous_safety(left, right, components, relationships),
                    "left": _side(left),
                    "right": _side(right),
                    "common_key": {
                        "id": key["key_id"],
                        "name": key["key_name"],
                        "version_id": key["key_version_id"],
                        "version_number": key["key_version_number"],
                        "components": components,
                    },
                    "relationships": [
                        str(relationship.get("relationship_name") or "unnamed")
                        for relationship in relationships
                    ],
                    # LE BADGE ET LE GESTE DISAIENT LE CONTRAIRE L'UN DE L'AUTRE.
                    # `_ambiguous_safety` rend `unsafe` quand l'executeur refuse
                    # TOUS les candidats -- et cette phrase restait
                    # inconditionnelle : « aucun choix ne mene nulle part »
                    # au-dessus de « choisissez ».
                    "next_action": (
                        _ambiguous_safety(left, right, components, relationships)[
                            "execution_blocked"
                        ]
                        or {}
                    ).get("message")
                    or "Choose one approved relationship before analysis.",
                }
            )
            continue
        if relationships:
            produced.append(_governed_match(left, right, key, relationships[0]))

    if produced:
        return produced

    # No executable path. Is there at least a shared identity to declare one over?
    shared = [
        {"canonical_field_id": field_id, "canonical_name": names.get(field_id, field_id)}
        for field_id in sorted(set(left["bindings"]) & set(right["bindings"]))
    ]
    if shared:
        # A key may exist and simply not be approved yet -- name its components
        # rather than the raw ids when one covers this pair.
        for key in keys:
            components = key["components"]
            if components and _implements_all(left, components) and _implements_all(
                right, components
            ):
                return [_candidate_key_missing(left, right, components, key=key)]
        return [_candidate_key_missing(left, right, shared)]

    same_name = {
        column
        for column, bound in left["physical_fields"].items()
        if column in right["physical_fields"] and not (bound and right["physical_fields"][column])
    }
    if same_name:
        return [_candidate_binding_missing(left, right, same_name)]
    return []


def _empty_reason(datastreams: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Why the list is empty, and the gesture that fills it. Never a table name."""
    if not datastreams:
        return {
            "code": "no_published_datastream",
            "message": (
                "No source publishes a mapping yet, so nothing can be crossed. Map a "
                "Datastream's columns and publish it, then come back."
            ),
        }
    if len(datastreams) == 1:
        return {
            "code": "one_published_datastream",
            "message": (
                "Only one source is published. A cross needs two, so publish a second "
                "Datastream to see what it can be combined with."
            ),
        }
    return {
        "code": "no_shared_identity",
        "message": (
            "The published sources share no canonical field, so nothing says they "
            "describe the same thing. Map the columns they have in common to the same "
            "canonical field, then declare a common key over it."
        ),
    }


def matches_for_datastream(conn, *, project_id: str, datastream_id: str) -> dict[str, Any]:
    """The same answer, filtered to the crosses that involve one Datastream."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.datastreams WHERE id = %s AND project_id = %s",
            (datastream_id, project_id),
        )
        if cur.fetchone() is None:
            raise DatastreamNotFound(datastream_id)

    catalog = discover_matches(conn, project_id=project_id)
    involved = [
        match
        for match in catalog["matches"]
        if datastream_id in (match["left"]["datastream_id"], match["right"]["datastream_id"])
    ]
    catalog["matches"] = involved
    catalog["counts"]["returned"] = len(involved)
    catalog["counts"]["governed"] = sum(1 for m in involved if m["authority"] == "governed")
    catalog["counts"]["candidates"] = sum(1 for m in involved if m["authority"] != "governed")
    if not involved:
        catalog["empty_reason"] = {
            "code": "no_match_for_datastream",
            "message": (
                "This source shares no canonical field with another published source. "
                "Map a column both sources carry to the same canonical field to make "
                "them comparable."
            ),
        }
    return catalog
