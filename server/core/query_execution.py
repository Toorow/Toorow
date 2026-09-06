"""Story 50.1 -- governed execution: one attempt, one immutable Result.

THE SHAPE, and why it is two objects instead of one. Progress is mutable and
uninteresting; the answer is immutable and is evidence. Putting both on one row
forces either a mutable Result (which AC8 forbids) or a Result that cannot show
progress. So an ATTEMPT carries the running state and its append-only events, and
a RESULT is inserted exactly once, when terminal evidence is durable.

`result_id` is allocated at ACCEPTANCE, before any data exists. A caller therefore
holds the final address while the query is still running, and an attempt that dies
mid-flight already knows which Result must be written for it. That is what makes
AC4's promise -- "an accepted attempt cannot remain permanently without its
Result" -- something the code can actually keep, via `terminalize_interrupted`.

WHAT WE DO NOT DO. We do not compile SQL from the caller's strings. Every
identifier that reaches the warehouse is looked up in an allowlist built from the
mapping version's own field list; every value is a bound parameter. A physical
column the mapping does not declare cannot be named, so there is no path from a
request to arbitrary SQL.

We also do not invent a source. AC9 says execution reads the governed path, and
`warehouse._query_duckdb` / `_query_bigquery` stay the only runners -- reused
behind this service, never re-implemented.

HONEST ABSENCE. If the pinned view's bindings do not resolve to a materialized
relation, that is not an error and not an empty answer: it is `unavailable`, with
a manifest that names exactly which link is missing. That distinction is the whole
point of the five-outcome vocabulary -- `empty` means "asked, nothing matched",
`unavailable` means "could not ask".
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from ulid import ULID

from core.query_specs import QuerySpecNotFound, canonical_hash
from core.result_shapes import ResultShapeRefused, serialize_waterfall_v1

logger = logging.getLogger(__name__)

#: The exact literal AC7 demands when no AI took part. Not a default, not a
#: placeholder: the absence of an AI Path is itself a recorded fact.
NO_AI_PATH = "No AI path"

#: Terminal outcomes, AC4. Nothing outside this set may be written.
OUTCOMES = frozenset({"success", "empty", "degraded", "refused", "unavailable"})

#: A physical identifier must look like one before it is allowed near SQL. This
#: is the second line of defence -- the first is the mapping allowlist -- because
#: a mapping row is data, and data can be wrong.
#:
#: LA BORNE DE LONGUEUR EST PASSEE DE 62 A 126 le 2026-08-01, et seulement elle.
#: Ce qui protege de l'injection est la CLASSE DE CARACTERES : un identifiant sans
#: guillemet, sans espace, sans point-virgule ne peut pas s'echapper de sa
#: position. La longueur n'y contribue rien.
#:
#: 63 est la limite de POSTGRES, et la relation lue ici ne vit pas dans Postgres :
#: elle vit dans l'entrepot (DuckDB, BigQuery), qui l'accepte bien plus longue.
#: Mesure du 2026-08-01 : la relation isolee d'un candidat s'appelle
#: `<table_brute>__cand_<execution_id>`, et avec un execution_id de 30 caracteres
#: CINQ des 51 tables brutes du depot depassaient 63 -- dont
#: `raw_linkedin_company_pages_daily` a 69. Elles auraient ete refusees ici, donc
#: silencieusement illisibles, et seulement les noms les plus longs : le genre de
#: defaut qui n'apparait que chez le client qui a branche le mauvais connecteur.
#:
#: L'alternative etait de raccourcir le nom candidat, mais l'activation exige que
#: l'execution_id y figure (`datastream_activation.py:1071`, tracabilite de
#: l'isolement) : le raccourcir aurait casse l'autre bout.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,126}$")

#: Rows kept inline on the Result payload. Beyond this the Result is marked
#: truncated and says so; a bounded UI must never imply completeness (AC5).
MAX_INLINE_ROWS = 1_000


class ExecutionUnavailable(RuntimeError):
    """The governed path could not be reached. Terminalizes as `unavailable`.

    AI-308: it is caught and FILED, never propagated. Until 2026-08-21 this class
    was raised in one file and caught in none, so an accepted attempt ended with
    no Result at all -- a 500 where the product already had a sentence.
    """


#: THE ONE SENTENCE for a published run whose landing cannot be read, wherever
#: that is discovered -- the planner, the compiler, or a multi-source read. It
#: names the gesture and nothing else: no relation, no column, no exception text.
#: The machine-facing half travels beside it as `missing_link`.
UNREADABLE_LANDING_MESSAGE = (
    "This Datastream published a run that landed nowhere readable. "
    "Re-run it; if it lands again the same way, its Runs tab carries the failure."
)


def names_a_readable_relation(relation: Any) -> bool:
    """Can this published reference actually be read?

    THE SINGLE ANSWER, imported and never retyped -- it is exactly the rule
    :func:`build_sql` applies part by part, so a reference the planner accepts can
    no longer be one the compiler then refuses. That disagreement was the whole
    defect: a candidate that landed nowhere readable is published under the
    traceability path `execution/<id>/candidate/relation`, which the activation
    guard accepts (the execution appears in it) and which this refuses (a slash
    is not an identifier). Both guards are right; only one of them was asked.
    """
    if not isinstance(relation, str) or not relation:
        return False
    return all(_IDENTIFIER.match(part) for part in relation.split("."))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value or ""):
        raise ExecutionUnavailable(f"refusing an unsafe physical identifier: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Acceptance.
# ---------------------------------------------------------------------------


def accept_execution(
    conn, *, org_id: str, project_id: str, query_spec_version_id: str, actor: str
) -> dict[str, Any]:
    """Allocate the attempt and its Result identity. Runs in the caller's transaction."""
    attempt_id = f"qea_{ULID()}"
    result_id = f"qr_{ULID()}"
    with conn.cursor() as cur:
        # Scope is part of the lookup: a foreign version does not resolve, and
        # answers exactly like a missing one (AC10).
        cur.execute(
            """
            SELECT id FROM app.query_spec_versions
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (query_spec_version_id, org_id, project_id),
        )
        if cur.fetchone() is None:
            raise QuerySpecNotFound("query spec version not found in this Project")

        cur.execute(
            """
            INSERT INTO app.query_execution_attempts
                (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (attempt_id, org_id, project_id, query_spec_version_id, result_id, actor),
        )
    append_event(
        conn,
        attempt_id=attempt_id,
        org_id=org_id,
        project_id=project_id,
        ordinal=1,
        state="accepted",
        detail={},
    )
    return {
        "attempt_id": attempt_id,
        "result_id": result_id,
        "state": "accepted",
        "query_spec_version_id": query_spec_version_id,
    }


def append_event(
    conn,
    *,
    attempt_id: str,
    org_id: str,
    project_id: str,
    ordinal: int,
    state: str,
    detail: dict[str, Any] | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.query_execution_attempt_events
                (id, attempt_id, org_id, project_id, ordinal, state, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                f"qev_{ULID()}",
                attempt_id,
                org_id,
                project_id,
                ordinal,
                state,
                json.dumps(detail or {}),
            ),
        )


# ---------------------------------------------------------------------------
# Resolving the physical target.
#
# The chain is exact and every link is checked:
#   binding(concept) -> mapping version -> published output version -> relation
# A break anywhere is reported by NAME, so the manifest says which link is
# missing rather than "no data".
# ---------------------------------------------------------------------------


def resolve_physical_plan(
    conn, *, project_id: str, semantic_view_version_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
    """Return {relation, columns, grain} or {unavailable_reason, missing_link}."""
    wanted = [m["id"] for m in spec.get("measures", [])] + [
        d["id"] for d in spec.get("dimensions", [])
    ]
    if not wanted:
        return {
            "unavailable_reason": (
                "This request asks for nothing. Choose at least one measure or one "
                "dimension before running it."
            ),
            "missing_link": "request",
        }

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.concept_id, b.datastream_id, b.mapping_version_id, b.binding_state,
                   cv.name, d.archived_at
            FROM app.semantic_view_version_bindings b
            JOIN app.semantic_concept_versions cv ON cv.concept_id = b.concept_id
            LEFT JOIN app.datastreams d ON d.id = b.datastream_id
            WHERE b.view_version_id = %s AND b.project_id = %s AND b.concept_id = ANY(%s)
            """,
            (semantic_view_version_id, project_id, wanted),
        )
        bindings = cur.fetchall()

    #  AI-358 (governance.md, amendment 2026-09-02): a binding whose Datastream
    #  is ARCHIVED does not carry. It cannot collect, so offering it as "where
    #  these figures come from" is an answer the product cannot honour --
    #  measured on the reference project, where an archived draft still bound to
    #  the View kept a one-source question refused as an arbitration. Historical
    #  rows stay in the table; they just stop counting here. Rows narrower than
    #  six columns (older fixtures) read as living.
    bindings = [b for b in bindings if len(b) < 6 or b[5] is None]

    if not bindings:
        return {
            # CHANTIER A -- THE SENTENCE NAMES THE GESTURE; the code stays
            # machine-facing. This said "no active binding pins these members to
            # a Datastream": true, and it leaves a person with nothing to do.
            "unavailable_reason": (
                "This Semantic View does not say where these members are measured. "
                "Open it and name the Datastream that measures each of them."
            ),
            "missing_link": "semantic_view_version_bindings",
        }
    inactive = [b[0] for b in bindings if b[3] != "active"]
    if inactive:
        return {
            "unavailable_reason": (
                f"{len(inactive)} of the members this request names are pinned to a "
                "Datastream mapping that is no longer the published one. Republish "
                "the Semantic View to pin what Data publishes today."
            ),
            "missing_link": "binding_state",
        }
    bound_concepts = {b[0] for b in bindings}
    unbound = [c for c in wanted if c not in bound_concepts]
    if unbound:
        return {
            "unavailable_reason": (
                f"{len(unbound)} of the members this request names have no Datastream "
                "behind them in this Semantic View. Open it and name where each one "
                "is measured."
            ),
            "missing_link": "semantic_view_version_bindings",
        }

    #  UN CONCEPT PEUT ETRE SERVI PAR PLUSIEURS FLUX, et le jour en est l'exemple
    #  meme : sept flux de la Vue de derivation rapportent leur propre date. Lier
    #  `date` a un seul d'entre eux faisait refuser « l'audience par tranche
    #  d'age, jour apres jour » comme multi-sources, alors que le flux d'audience
    #  porte a lui seul le jour, la tranche d'age, le genre et sa mesure : la
    #  question traversait deux flux par la LIAISON, pas par les donnees.
    #
    #  On cherche donc les flux qui servent TOUS les membres demandes. Un seul :
    #  c'est lui, sans jointure ni invention. Plusieurs : on refuse en les
    #  NOMMANT, parce que choisir a la place de la personne deciderait en silence
    #  d'ou viennent ses chiffres. Aucun : la question traverse reellement les
    #  sources, et le refus ci-dessous garde exactement son sens.
    by_datastream: dict[str, set[str]] = {}
    mapping_of: dict[str, set[str]] = {}
    for concept_id, datastream_id_, mapping_version_id_, _state, _name, *_rest in bindings:
        by_datastream.setdefault(str(datastream_id_), set()).add(str(concept_id))
        mapping_of.setdefault(str(datastream_id_), set()).add(str(mapping_version_id_))
    wanted_set = set(wanted)
    complete = sorted(
        ds for ds, concepts in by_datastream.items() if wanted_set <= concepts
    )
    chosen_by: str | None = None
    if len(complete) > 1:
        # CHANTIER B -- A DECLARATION MAY RESOLVE THIS, NOTHING ELSE MAY. When
        # several carriers can answer, the Datastream the client declared
        # authoritative for the measure's total answers, and the plan records
        # that a declaration chose it. With no declaration the refusal stands:
        # picking one here would decide, in silence, where their figures come
        # from -- which is exactly the decision this chantier exists to surface.
        declared = _declared_total_among(
            conn, project_id=project_id, spec=spec, candidates=complete
        )
        chosen_reason = "declared_total_authority"
        if declared is None:
            #  AI-358 (governance.md, 2026-09-02): a declared BREAKDOWN
            #  arbitrates like a declared total. A `sums_to = 'equals'` row
            #  naming exactly one candidate is the person's own statement of
            #  where that cut is measured; refusing it re-asks a question that
            #  has been answered. Two rows naming two candidates keep the
            #  refusal: two statements are not one.
            declared = _declared_breakdown_among(
                conn, project_id=project_id, spec=spec, candidates=complete
            )
            chosen_reason = "declared_breakdown_authority"
        if declared is None:
            return {
                "unavailable_reason": (
                    f"{len(complete)} Datastreams of this Semantic View can answer every "
                    "member of this request, so which one answers is a decision. Declare "
                    "which Datastream is authoritative for the total of this measure."
                ),
                "missing_link": "metric_grain_declaration",
            }
        complete = [declared]
        chosen_by = chosen_reason
    if len(complete) == 1 and len(mapping_of[complete[0]]) == 1:
        bindings = [b for b in bindings if str(b[1]) == complete[0]]

    mapping_ids = {b[2] for b in bindings}
    datastream_ids = {b[1] for b in bindings}
    if len(mapping_ids) != 1 or len(datastream_ids) != 1:
        # STORY 66.11 -- THE DEAD END IS GONE, THE REFUSAL STAYS. This resolver
        # is the SINGLE-source one and must keep refusing: widening it would give
        # the product two answers to "how is a cross executed". What changes is
        # the sentence. It used to say the plan "does not exist yet", which sent
        # a reader looking for work that is now done -- the cross-source plan is
        # `core.multi_source_plan` (story 66.4) and its executor is
        # `core.multi_source_execution` (66.5), reachable at
        # `POST /api/projects/{id}/analyze/multi-source/plans`.
        return {
            # CHANTIER A -- the sentence used to describe how the product is
            # built ("is composed and executed as a cross-source plan rather than
            # as a single-relation query"). Accurate, and it left a person with
            # nothing to do. Story 66.11's rule still holds: the refusal must not
            # send a reader looking for work that is already done.
            "unavailable_reason": (
                "This request reads from several Datastreams at once. Open Explore "
                "and compose it as a cross-source analysis, where the governed "
                "paths between them are offered with their coverage."
            ),
            "missing_link": "multi_source_plan",
        }
    mapping_version_id = next(iter(mapping_ids))
    datastream_id = next(iter(datastream_ids))

    with conn.cursor() as cur:
        cur.execute(
            """
            -- `id` and `output_id` are the Output version's own identity, and
            -- they are carried so the consumption can be RECORDED at the write
            -- below (`_record_output_consumption`). They were the one thing
            -- missing: `app.datastream_output_used_by` needs both as NOT NULL
            -- FKs, and this SELECT is where they are already in hand.
            SELECT relation_ref, execution_id, publication_log_id, created_at,
                   id, output_id
            FROM app.datastream_output_versions
            WHERE mapping_version_id = %s AND datastream_id = %s AND project_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (mapping_version_id, datastream_id, project_id),
        )
        output = cur.fetchone()
        if output is None or not output[0]:
            # This is the live state on 2026-07-31: the Datastream has a mapping
            # version but has never executed, so nothing is materialized.
            return {
                "unavailable_reason": (
                    "This Datastream has collected nothing that can be queried yet. "
                    "Run it, or wait for its next run, then ask again."
                ),
                "missing_link": "datastream_output_versions",
            }
        relation_ref = output[0]

        cur.execute(
            """
            SELECT mapping_payload FROM app.datastream_mapping_versions
            WHERE id = %s AND project_id = %s
            """,
            (mapping_version_id, project_id),
        )
        mapping_row = cur.fetchone()

    payload = (mapping_row[0] if mapping_row else {}) or {}
    # canonical_target is the mapping's own name for the field; the semantic
    # concept carries the same name. That equality is the join, and it is checked
    # rather than assumed -- an unmatched concept is reported, not skipped.
    by_target = {
        str((f.get("binding") or {}).get("canonical_target") or ""): str(f.get("field_id") or "")
        for f in payload.get("fields") or []
    }
    columns: dict[str, str] = {}
    for concept_id, _ds, _mv, _state, name, *_rest in bindings:
        physical = by_target.get(str(name) or "")
        if not physical:
            return {
                "unavailable_reason": (
                    f"`{name}` is not mapped on the Datastream that answers this "
                    "request. Map it there, or ask for it from a Datastream that has it."
                ),
                "missing_link": "mapping_payload.fields",
            }
        columns[concept_id] = _safe_identifier(physical)

    relation = (
        relation_ref
        if isinstance(relation_ref, str)
        else (relation_ref.get("relation") or relation_ref.get("name") or "")
    )
    # A PUBLISHED CANDIDATE IS A PROMOTED ONE (2026-09-04). An Output version exists
    # only for a published execution, and publication appended the isolated
    # `<table>__cand_<execution_id>` into `<table>` and dropped it -- so a
    # `relation_ref` still naming the isolated table (every mapping-change
    # publication before today) reaches the shared table here, instead of a 404
    # the attempt could not explain. `raw_landing.promoted_relation` is the rule.
    from core.raw_landing import promoted_relation  # noqa: PLC0415

    relation = promoted_relation(relation) if "__cand_" in str(relation) else relation
    # AI-308: A NAME AND A READABLE NAME ARE TWO CHECKS. The first was made, the
    # second was deferred to `build_sql` -- which does not answer, it RAISES, and
    # nothing caught it. A published run whose landing is the traceability path
    # `execution/<id>/candidate/relation` therefore reached here, was planned, and
    # blew up two hundred lines later with the attempt already accepted. The
    # answer is the same in both cases -- the run landed nowhere a question can
    # reach -- so it is the same sentence, decided here, where a plan can still
    # become an outcome.
    if not names_a_readable_relation(relation):
        return {
            "unavailable_reason": UNREADABLE_LANDING_MESSAGE,
            "missing_link": "relation_ref",
        }
    dataset = _dataset_for(project_id, relation)
    from core import relation_shape  # noqa: PLC0415

    shape = relation_shape.read(project_id, relation, dataset=dataset)
    present = set(shape.columns)

    # AI-342 -- A DIMENSION THE RELATION DOES NOT CARRY IS REFUSED, NOT ANSWERED
    # WITH NOTHING. A breakdown landing holds every dimension in one pair of
    # columns, so `breakdown_pivot` below turns ANY requested dimension into
    # `WHERE breakdown_dimension = <its name>` -- including one nothing ever
    # landed. The read then succeeded, returned zero rows, and the person read
    # "no views from anywhere" where the truth was "this source publishes no
    # country split". Measured 2026-09-01 on the reference Project: eleven
    # dimensions bound, two materialised, and the country question answered
    # empty.
    #
    # ASKED ONLY WHERE IT DECIDES SOMETHING. The keys are read once per plan and
    # only for a member whose physical field is NOT a column of the relation --
    # that is exactly the member about to pivot. A dimension that is a real
    # column costs nothing new.
    #
    # AND IT IS THE READER'S HALF OF A PAIR. `semantic_model.prepare_change_set`
    # refuses to PUBLISH such a binding, which is where the repair belongs; this
    # answers for every View published before that guard existed, and for a
    # relation whose keys changed after publication.
    for member in spec.get("dimensions", []):
        concept_id = str(member.get("id") or "")
        physical = columns.get(concept_id)
        if not physical or physical in present:
            continue
        name = next(
            (str(b[4]) for b in bindings if str(b[0]) == concept_id and b[4]), ""
        )
        if relation_shape.answers(shape, physical_field=physical, concept_name=name) is False:
            carried = sorted(shape.breakdown_keys or ())
            carries = (
                "It publishes " + ", ".join(f"`{key}`" for key in carried) + "."
                if carried
                else "It publishes no breakdown at all yet."
            )
            return {
                "unavailable_reason": (
                    f"The Datastream that answers this request does not publish a "
                    f"`{name}` breakdown. {carries} Ask for one of those, or collect "
                    f"`{name}` on a Datastream that reports it and bind this member "
                    "there."
                ),
                "missing_link": "breakdown_dimension",
            }

    # AS-OF IS EXECUTED, OR IT IS REFUSED HERE. The read keeps the rows loaded at
    # or before the instant, which needs the landing to carry when each row
    # landed. A relation without `loaded_at` cannot answer "as it was known then"
    # -- and answering with today's rows under an as-of label is the half-truth
    # this whole repair removes.
    if (spec.get("time") or {}).get("as_of") and "loaded_at" not in present:
        return {
            "unavailable_reason": (
                "This Datastream's published rows do not record when each of them "
                "landed, so what was known on an earlier day cannot be read from them. "
                "Ask without a Reported-as-of date."
            ),
            "missing_link": "loaded_at",
        }
    return {
        "relation": relation,
        # WHERE that relation lives, resolved by the writer's own naming point.
        "dataset": dataset,
        # AND WHAT SHAPE IT IS IN, read from the relation rather than declared.
        "long_form": long_form_pivot(present),
        "present_columns": sorted(present),
        "columns": columns,
        "grain": payload.get("grain") or [],
        # AI-303: the finer grains that share this landing and are NOT this
        # profile's. Reading without restricting to its own grain sums them all.
        "grain_restrictions": _grain_restrictions(conn, project_id, datastream_id, present),
        # The key a WIDE landing supersedes on (AI-369): the published mapping's
        # grain, as landed column names. Empty when the mapping declares none.
        "grain_columns": _managed_feed_grain(conn, project_id, datastream_id),
        "mapping_version_id": mapping_version_id,
        "datastream_id": datastream_id,
        # `declared_total_authority` when a declaration arbitrated between several
        # capable carriers, None when there was only one and nothing was decided.
        # A Result must be able to say which of the two happened.
        "chosen_by": chosen_by,
        # Carried so evidence can be captured at WRITE time. See capture_evidence.
        "pull_id": output[1],
        "publication_log_id": output[2],
        "output_created_at": output[3],
        # The Output version this execution reads, by identity rather than by
        # name. `relation` above is a place; these two are the governed object,
        # and they are what `_record_output_consumption` writes so the Datastream
        # Workbench's `Used by` panel can finally answer.
        "output_version_id": output[4],
        "output_id": output[5],
    }


# ---------------------------------------------------------------------------
# CHANTIER B -- the same measure at several grains.
#
# Three helpers, and the order they run in is the contract: a declaration may
# ARBITRATE between capable carriers (`_declared_total_among`), the authority's
# own relation is then materialized like any other (`_total_plan_for`), and the
# two figures are COMPARED without either being touched (`reconcile_grains`).
#
# Nothing here adjusts a number. A product that quietly scales a breakdown onto
# its total is lying about one of the two, and which one is not even knowable.
# ---------------------------------------------------------------------------


def _measure_concept_ids(spec: dict[str, Any]) -> list[str]:
    return [str(m["id"]) for m in spec.get("measures", []) if m.get("id")]


def _declared_total_among(
    conn, *, project_id: str, spec: dict[str, Any], candidates: list[str]
) -> str | None:
    """The declared total authority, when exactly one of the request's measures
    names one and it is among the capable carriers.

    Returns None -- i.e. keeps the refusal -- when nothing is declared, when the
    declared authority is not one of the candidates, or when two measures of the
    same request declare two different authorities. That last case is not a
    tie to break: two totals living in two Datastreams make one row of output
    that answers from two places, and the person asking must say which.
    """
    measure_ids = _measure_concept_ids(spec)
    if not measure_ids:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT total_datastream_id
            FROM app.metric_grain_declarations
            WHERE project_id = %s AND concept_id = ANY(%s)
            """,
            (project_id, measure_ids),
        )
        declared = {str(row[0]) for row in cur.fetchall()}
    if len(declared) != 1:
        # Nothing declared, or two measures whose totals live in two Datastreams.
        return None
    only = next(iter(declared))
    # AND IT MUST BE ONE OF THE CANDIDATES. A declaration naming a Datastream
    # that cannot answer this request does not arbitrate it -- falling back to
    # "some other capable one" would be the silent choice all over again.
    return only if only in set(candidates) else None


def _declared_breakdown_among(
    conn, *, project_id: str, spec: dict[str, Any], candidates: list[str]
) -> str | None:
    """The declared `equals` breakdown authority, when it names exactly one
    of the capable carriers (AI-358, governance.md 2026-09-02).

    Same contract as `_declared_total_among`: None keeps the refusal -- nothing
    declared, the declared Datastream is not among the candidates, or several
    breakdown rows of this request's measures name several candidates. Only
    `sums_to = 'equals'` arbitrates: a `partial_by_design` breakdown is a
    documented gap, not a statement that this is where the figures live.
    """
    measure_ids = _measure_concept_ids(spec)
    if not measure_ids:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT b.datastream_id
            FROM app.metric_grain_breakdowns b
            JOIN app.metric_grain_declarations g ON g.id = b.declaration_id
            WHERE b.project_id = %s AND g.concept_id = ANY(%s)
              AND b.sums_to = 'equals'
            """,
            (project_id, measure_ids),
        )
        declared = {str(row[0]) for row in cur.fetchall()} & set(candidates)
    return next(iter(declared)) if len(declared) == 1 else None


def _total_plan_for(
    conn, *, project_id: str, datastream_id: str, member_names: dict[str, str]
) -> dict[str, Any] | None:
    """Materialize the authority's relation for the given members.

    ``member_names`` maps concept id -> concept name, exactly the equality
    ``resolve_physical_plan`` uses. Returns None -- never a partial plan -- when
    the authority has no published output, or does not carry one of the members.
    A total computed while silently dropping a member would not be the same
    question as the breakdown it is compared against.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_mapping_version_id FROM app.datastreams "
            "WHERE id = %s AND project_id = %s",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
        if row is None or not row[0]:
            return None
        mapping_version_id = str(row[0])

        cur.execute(
            """
            SELECT relation_ref FROM app.datastream_output_versions
            WHERE mapping_version_id = %s AND datastream_id = %s AND project_id = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (mapping_version_id, datastream_id, project_id),
        )
        output = cur.fetchone()
        if output is None or not output[0]:
            return None
        relation_ref = output[0]

        cur.execute(
            "SELECT mapping_payload FROM app.datastream_mapping_versions "
            "WHERE id = %s AND project_id = %s",
            (mapping_version_id, project_id),
        )
        mapping_row = cur.fetchone()

    payload = (mapping_row[0] if mapping_row else {}) or {}
    by_target = {
        str((f.get("binding") or {}).get("canonical_target") or ""): str(f.get("field_id") or "")
        for f in payload.get("fields") or []
    }
    columns: dict[str, str] = {}
    for concept_id, name in member_names.items():
        physical = by_target.get(str(name))
        if not physical:
            return None
        columns[concept_id] = _safe_identifier(physical)

    relation = (
        relation_ref
        if isinstance(relation_ref, str)
        else (relation_ref.get("relation") or relation_ref.get("name") or "")
    )
    # AI-308: the authority's own landing has to be readable too. A total read
    # from an unreadable relation raised out of `build_sql` and took the whole
    # breakdown with it, which is why this returns None rather than a partial
    # plan: the comparison is declared unavailable, the breakdown still answers.
    if not names_a_readable_relation(relation):
        return None
    dataset = _dataset_for(project_id, relation)
    present = _relation_columns(dataset, relation, project_id)
    return {
        "relation": relation,
        "dataset": dataset,
        "long_form": long_form_pivot(present),
        "present_columns": sorted(present),
        "columns": columns,
        "grain": payload.get("grain") or [],
        # AI-303: the finer grains that share this landing and are NOT this
        # profile's. Reading without restricting to its own grain sums them all.
        "grain_restrictions": _grain_restrictions(conn, project_id, datastream_id, present),
        # The key a WIDE landing supersedes on (AI-369): the published mapping's
        # grain, as landed column names. Empty when the mapping declares none.
        "grain_columns": _managed_feed_grain(conn, project_id, datastream_id),
        "mapping_version_id": mapping_version_id,
        "datastream_id": datastream_id,
    }


def reconcile_grains(
    conn,
    *,
    project_id: str,
    semantic_view_version_id: str,
    plan: dict[str, Any],
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    truncated: bool,
) -> list[dict[str, Any]]:
    """For every measure of this request that declares a total elsewhere, compare.

    Returns one entry per measure with a declaration whose authority is NOT the
    Datastream that just answered. An empty list means there was nothing to
    reconcile -- which is a different thing from a reconciliation that failed, and
    the two must not be printed the same way.
    """
    from core import metric_grain  # noqa: PLC0415 - avoids an import cycle at module load

    executed_on = str(plan.get("datastream_id") or "")
    measures = [m for m in spec.get("measures", []) if m.get("id")]
    if not measures:
        return []

    # The concept NAMES of every member of the request, read once. The authority
    # must carry all of them or the comparison is refused, not narrowed.
    member_ids = [str(m["id"]) for m in measures] + [
        str(d["id"]) for d in spec.get("dimensions", []) if d.get("id")
    ]
    filter_ids = [str(f["member_id"]) for f in spec.get("filters", []) if f.get("member_id")]
    time_member = str((spec.get("time") or {}).get("member_id") or "")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT concept_id, name FROM app.semantic_concept_versions "
            "WHERE concept_id = ANY(%s)",
            (list({*member_ids, *filter_ids, *({time_member} if time_member else set())}),),
        )
        names = {str(row[0]): str(row[1]) for row in cur.fetchall()}

    out: list[dict[str, Any]] = []
    for measure in measures:
        concept_id = str(measure["id"])
        declaration = metric_grain.get_declaration(
            conn, project_id=project_id, concept_id=concept_id
        )
        if not declaration:
            continue
        authority = str(declaration["total_datastream_id"])
        if authority == executed_on:
            # This Result IS the total. Nothing to compare it against.
            continue

        # The breakdown's own sum, from the rows this Result actually carries. A
        # truncated Result has no sum -- summing the page that survived and
        # calling it the breakdown would invent a gap out of the row limit.
        breakdown_sum: Any = None
        if not truncated:
            # PHYSICAL rows, keyed by physical column. `rows` here is what the
            # warehouse returned, before `shape_result_payload` -- and that is
            # deliberate: `waterfall_v1` re-keys its rows by MEMBER id, so summing
            # the shaped rows would read `None` on every one of them and hand back
            # a silent zero on exactly the shapes that are not tabular.
            column = str((plan.get("columns") or {}).get(concept_id) or concept_id)
            # A PERIOD COMPARISON DOUBLES THE ROWS ON PURPOSE, and only the current
            # window is the question the authority is asked. Summing both windows
            # against a one-window total would invent a gap the size of the
            # baseline.
            period_field = str(
                (spec.get("comparison_windows") or {}).get("period_field") or ""
            )
            total = 0.0
            seen = False
            for row in rows:
                if period_field and row.get(period_field) != "current":
                    continue
                value = row.get(column)
                if value is None:
                    continue
                try:
                    total += float(value)
                    seen = True
                except (TypeError, ValueError):
                    seen = False
                    break
            breakdown_sum = total if seen else None

        # The authority is asked the same question minus the breakdown's own
        # dimensions: same window, same filters, no grouping. A filter the
        # authority cannot honour makes the total unavailable rather than wrong.
        total_value: Any = None
        unavailable_reason: str | None = None
        needed = {concept_id: names.get(concept_id, "")}
        for member in [*filter_ids, *([time_member] if time_member else [])]:
            needed[member] = names.get(member, "")
        if any(not name for name in needed.values()):
            unavailable_reason = "a member of this request has no governed name"
        else:
            total_plan = _total_plan_for(
                conn,
                project_id=project_id,
                datastream_id=authority,
                member_names=needed,
            )
            if total_plan is None:
                unavailable_reason = (
                    "the Datastream declared authoritative for this total does not "
                    "publish every member of this request"
                )
            elif (spec.get("time") or {}).get("as_of") and "loaded_at" not in set(
                total_plan.get("present_columns") or []
            ):
                # The breakdown honours the as-of instant; a total read without it
                # would be today's figure compared against an earlier day's.
                unavailable_reason = (
                    "the Datastream declared authoritative for this total does not record "
                    "when its rows landed, so it cannot be read as of that date"
                )
            else:
                total_spec = {
                    "measures": [{"id": concept_id}],
                    "dimensions": [],
                    "filters": spec.get("filters", []),
                    "time": spec.get("time"),
                    "sort": [],
                    "row_limit": 1,
                }
                try:
                    from core import warehouse  # noqa: PLC0415

                    sql, params = build_sql(total_plan, total_spec)
                    bigquery_mode = warehouse._db_mode() == "bigquery"
                    if bigquery_mode:
                        sql = _bind_positional(sql)
                    runner = (
                        warehouse._query_bigquery if bigquery_mode else warehouse._query_duckdb
                    )
                    total_rows = runner(sql, params)
                except Exception as exc:  # noqa: BLE001
                    # THE TRACE NAMES THE CAUSE; the answer stays a verdict. Three
                    # `except` blocks swallowed theirs this week and each cost a pass.
                    logger.warning(
                        "metric_grain: total unreadable authority=%s measure=%s: %s: %s",
                        authority,
                        concept_id,
                        type(exc).__name__,
                        exc,
                        exc_info=True,
                    )
                    unavailable_reason = "the declared total could not be read"
                    total_rows = []
                if total_rows:
                    first = total_rows[0]
                    total_column = str(total_plan["columns"][concept_id])
                    if isinstance(first, dict):
                        total_value = first.get(total_column)
                        if total_value is None and len(first) == 1:
                            total_value = next(iter(first.values()))

        verdict = metric_grain.reconcile_breakdown(
            declaration=declaration,
            breakdown_datastream_id=executed_on,
            breakdown_sum=breakdown_sum,
            total=total_value,
        )
        if unavailable_reason and verdict["verdict"] == metric_grain.VERDICT_UNAVAILABLE:
            verdict["statement"] = (
                f"{verdict['statement']} Reason: {unavailable_reason}."
            )
        if truncated and breakdown_sum is None:
            verdict["statement"] = (
                "This Result was truncated at its row limit, so its own total is not "
                "known and cannot be compared. Narrow the window or raise the limit."
            )
        verdict["measure_id"] = concept_id
        verdict["measure_name"] = names.get(concept_id)
        verdict["semantic_view_version_id"] = semantic_view_version_id
        out.append(verdict)
    return out


# ---------------------------------------------------------------------------
# Evidence capture -- at execution time, on purpose.
#
# Story 50.2's Result lenses named this module as the owner of three links they
# could not fill (`analyze_workbench.py`): per-value provenance tuples, DQ
# evaluations, and freshness. They were right not to fill them: deriving them
# when a Result is READ means describing a past execution from TODAY's binding
# and quality state, which is the "reconstruct historical meaning from current
# state" failure AC6 and AC8 both forbid.
#
# So they are snapshotted here, once, while the facts are still true. A Result
# written today keeps saying what was true today even after the binding moves,
# the case closes, or the Datastream is re-mapped.
# ---------------------------------------------------------------------------


def capture_evidence(conn, *, project_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Snapshot provenance, freshness and DQ as they stand for THIS execution."""
    datastream_id = plan["datastream_id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT module_name FROM app.datastreams WHERE id = %s AND project_id = %s",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
        source_system = str(row[0]) if row and row[0] else None

        # DQ evaluations in force at execution. An unreadable DQ owner is recorded
        # as unavailable rather than as zero open cases -- "no evidence" and
        # "evidence says healthy" are different answers (AC6).
        dq_ids: list[str] = []
        dq_unavailable: str | None = None
        try:
            cur.execute(
                """
                SELECT id FROM app.control_cases
                WHERE project_id = %s AND subject_kind = 'datastream' AND subject_id = %s
                  AND status NOT IN ('resolved', 'dismissed')
                ORDER BY last_observed_at DESC
                LIMIT 50
                """,
                (project_id, datastream_id),
            )
            dq_ids = [str(r[0]) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            dq_unavailable = str(exc)

    # One tuple per requested member, because AC6 asks for per-VALUE provenance
    # and a single Datastream-level pointer would not survive the day this
    # service supports more than one bound Datastream.
    values = [
        {
            "member_id": concept_id,
            "source_system": source_system,
            "source_field": physical,
            "pull_id": plan.get("pull_id"),
        }
        for concept_id, physical in sorted(plan.get("columns", {}).items())
    ]

    output_at = plan.get("output_created_at")
    return {
        # THE EXACT PHYSICAL VERSION THIS EXECUTION READ, at the ROOT of the
        # manifest, because that is where the two consumers already spell it:
        # `render_app_payload._SAFE_RESULT_MANIFEST_KEYS` allowlists this key and
        # `analyze_render_mcp.project_provenance` reads it. Until this line
        # nothing wrote it, so every Result in existence answered `unavailable`
        # about the Output it had just read -- and the Data ruling of 2026-08-12
        # named this exact seam as the blocker for all three consuming kinds.
        #
        # THREADED, NOT RE-QUERIED. `resolve_physical_plan` already SELECTs `id`
        # off `app.datastream_output_versions` and hands it over on the plan. A
        # second read here could answer with a version published since, which
        # would be a Result naming a source it never read.
        "datastream_output_version_id": plan.get("output_version_id"),
        "provenance": {
            "source_system": source_system,
            "datastream_id": datastream_id,
            "mapping_version_id": plan.get("mapping_version_id"),
            "relation": plan.get("relation"),
            "pull_id": plan.get("pull_id"),
            "publication_log_id": plan.get("publication_log_id"),
            "values": values,
        },
        "freshness": {
            "output_created_at": output_at.isoformat()
            if hasattr(output_at, "isoformat")
            else output_at,
        },
        "dq_evaluation_ids": dq_ids,
        "dq_unavailable_reason": dq_unavailable,
    }


#: The Analyze consumers this module can honestly declare. `delivery` is an
#: Output KIND, never a consumer this product serves (pinned by
#: `test_publication_writes_no_consumer.py`), and `semantic_view` is not written
#: here: a Semantic View does not read an Output, an execution against it does.
_CONSUMER_KIND_RESULT = "result"


def record_output_consumption(
    conn,
    *,
    project_id: str,
    plan: dict[str, Any],
    result_id: str,
    query_spec_version_id: str | None,
) -> None:
    """Journal that THIS execution read THAT Output version. Story 47.5's table.

    WHY THIS IS THE WRITER, AND WHY IT IS HERE. `app.datastream_output_used_by`
    has had three readers since migration 138 and no writer at all -- the
    Datastream Workbench's `Used by` panel and `_fan_out`'s "downstream
    consumers" have been answering from a structurally empty table, and
    `_fan_out` reported `state: "observed"` with a count of zero, which reads as
    "nothing depends on this" rather than "nobody ever recorded it".

    The ruling of 2026-08-12 settled who may write it: **only the consumer**, and
    never the publication path. `test_publication_writes_no_consumer.py` enforces
    that, and its own docstring names this function's address as the intended
    one. So the writer is here, in Analyze, at the moment an execution resolves a
    `relation_ref` into a physical relation -- which IS the act of consuming an
    Output.

    A READ DOES NOT WRITE IN THE MIDDLE OF ITS OWN READ. This is called from
    `run_execution` at the same seam as `capture_evidence`, on the caller's
    connection, and commits with the caller's Result rather than on its own. The
    consumption and the Result it justifies are one fact: a Result that rolls
    back must not leave a dependency claiming to exist.

    RECORDED AT RESOLUTION, not at the warehouse's answer -- deliberately, and
    the same reasoning `capture_evidence` is placed on. The panel answers "what
    depends on this Output", and a Result whose runner then failed still names
    this Output version as the source it was built against. A dependency is not
    undone by one bad night.

    IDEMPOTENT BY THE PRIMARY KEY, and it must be: an execution is retried, and
    the table's trigger refuses UPDATE and DELETE outright (evidence is
    immutable), so `ON CONFLICT DO NOTHING` is the only conflict clause the
    schema permits -- `DO UPDATE` would fire the trigger and abort the Result.

    NEVER FATAL, and never poisoning. A bookkeeping row must not cost a Result
    that ran. The INSERT is wrapped in its own SAVEPOINT, because an unguarded
    failed statement aborts the whole transaction in Postgres -- swallowing the
    exception without one would take the Result down a few lines later, which is
    the opposite of not being fatal.
    """
    output_version_id = plan.get("output_version_id")
    output_id = plan.get("output_id")
    if not output_version_id or not output_id or not result_id:
        # An execution that resolved no Output version consumed nothing. Silence
        # here is the honest answer, not a swallowed failure.
        return
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.datastream_output_used_by
                        (output_id, output_version_id, project_id, consumer_kind,
                         consumer_ref, consumer_version_ref, owner_href)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (output_version_id, consumer_kind, consumer_ref)
                    DO NOTHING
                    """,
                    (
                        output_id,
                        output_version_id,
                        project_id,
                        _CONSUMER_KIND_RESULT,
                        result_id,
                        query_spec_version_id,
                        # The address the console already serves a Result on
                        # (`trace_observation.py`), so the panel's link resolves
                        # against the real router rather than a shape invented
                        # here -- `WorkbenchOutputsLinks` refuses to render an
                        # `owner_href` the router does not know.
                        f"/api/projects/{project_id}/analyze/results/{result_id}",
                    ),
                )
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning(
            "query_execution: output_consumption_not_recorded result=%s output=%s: %s",
            result_id,
            output_version_id,
            exc,
        )


def _relation_columns(dataset: str, relation: str, project_id: str = "") -> set[str]:
    """The columns a relation actually has, asked of the warehouse.

    READ, NOT DECLARED. The shape of a landing is derivable from the landing, and
    what is derivable is not stored: a fifth Connector that lands long would
    otherwise need a manifest edit before it could ever be read. Metadata only --
    `INFORMATION_SCHEMA` scans no table bytes.

    An unreadable schema yields an empty set, and the caller then builds the plain
    SELECT it always built: a shape we could not confirm is not a shape we invent.

    AI-342: the reading itself moved to `core.relation_shape`, which asks BOTH
    engines. This wrapper is kept because three call sites want only the column
    set; the ones that need to know what the relation can ANSWER take the shape.
    """
    from core import relation_shape  # noqa: PLC0415

    return set(relation_shape.read(project_id, relation, dataset=dataset).columns)


def long_form_pivot(present: set[str]) -> tuple[str, str] | None:
    """`(key_column, value_column)` when a relation lands one measurement per row.

    Four of the forty Connectors land a metric NAME beside its value instead of
    one column per measurement. The semantic layer names columns, so a read of
    such a relation asked for `views` and BigQuery answered `Unrecognized name:
    views; Did you mean video?`. The pivot that turns those rows into columns is
    the staging model's job, and the staging model is built by a nightly this
    deployment does not run.

    THE PAIR IS THE WHOLE ANSWER HERE, and which members it applies to is decided
    one member at a time by the caller: a dimension of such a relation is a real
    column (`date`, `channel_id`) while a measure is a row. Deciding for the whole
    request -- what this returned first -- meant one real column disabled the
    pivot for every measure beside it.
    """
    for key_column, value_column in (("metric", "value"), ("breakdown_dimension", "value")):
        if key_column in present and value_column in present:
            return key_column, value_column
    return None


def breakdown_pivot(present: set[str]) -> tuple[str, str] | None:
    """`(dimension_column, value_column)` when a relation lands one BREAKDOWN per row.

    The sibling of `long_form_pivot`, for the other half of the same shape. A
    breakdown landing carries the dimension's NAME beside its value -- one row per
    (breakdown_dimension, breakdown_value, metric) -- so `country` is not a column
    of it any more than `views` is.

    Measured 2026-08-14 on the reference Project: with `views` finally bound to
    the country Datastream, the plan resolved, and BigQuery answered
    `Unrecognized name: country`. The measure half of the pivot had been built;
    the dimension half had never been reached, because no View had ever bound a
    measure to a breakdown Datastream.

    The equality is the one the whole module already runs on -- the concept's
    name. `breakdown_dimension` holds `country` for the country landing, so the
    filter is the concept name itself and nothing is invented for it.
    """
    if "breakdown_dimension" in present and "breakdown_value" in present:
        return "breakdown_dimension", "breakdown_value"
    return None


def _bind_positional(sql: str) -> str:
    """Turn the portable `?` placeholders into the `@pN` BigQuery binds."""
    out: list[str] = []
    index = 0
    for char in sql:
        if char == "?":
            out.append(f"@p{index}")
            index += 1
        else:
            out.append(char)
    return "".join(out)


def _dataset_for(project_id: str, relation: str) -> str:
    """The dataset a relation lives in, from the writer's own naming point.

    Raw and marts are two datasets and the resolver names both. A relation that
    already carries its dataset is left alone by the caller; this only answers
    for a bare name.
    """
    if "." in relation:
        return ""
    try:
        from core import warehouse_tenancy  # noqa: PLC0415

        # `managed_feed_*` IS a raw landing (2026-09-04): `managed_feed_ledger`
        # refuses any landing relation that is not `managed_feed_*`, the plan
        # store or the reference routes -- "never a mart" -- and the promoted
        # shared table of a managed feed lives in the raw dataset next to the
        # connectors' `raw_*`. Routing it to the marts answered a 404 on the
        # first managed feed anyone queried through Analyze.
        if (
            relation.startswith("raw_")
            or relation.startswith("managed_feed_")
            or "__cand_" in relation
        ):
            return warehouse_tenancy.bigquery_raw_dataset(project_id)
        return warehouse_tenancy.bigquery_marts_dataset(project_id)
    except Exception as exc:  # noqa: BLE001 -- an unqualified read says so itself
        logger.warning("query_execution: dataset_unresolved relation=%s: %s", relation, exc)
        return ""


_PROVENANCE_COLUMNS = ("pull_id", "loaded_at", "project_id")


def _managed_feed_grain(conn, project_id: str, datastream_id: str) -> list[str]:
    """The landed column names of the published mapping's grain, or [].

    Read from `app.managed_feed_grain_v` (migration 302), which is exactly what
    the marts model `managed_feed_superseding` joins on -- one source for "which
    key does this feed supersede on", read by both paths. A connector pull is not
    listed there and gets []; its long-form landing has its own partition rule.
    A read must not fail on this: an unreadable view means "no declared grain",
    said in the log, and the wide landing is then read as it is.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT grain_columns
                FROM app.managed_feed_grain_v
                WHERE project_id = %s AND datastream_id = %s AND has_grain
                """,
                (project_id, datastream_id),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- a read must not fail on this
        logger.warning(
            "query_execution: managed_feed_grain_unreadable ds=%s: %s", datastream_id, exc
        )
        return []
    if row is None or not row[0]:
        return []
    raw = row[0] if isinstance(row[0], list) else json.loads(row[0])
    return [str(c) for c in raw if str(c).strip()]


def _grain_restrictions(
    conn, project_id: str, datastream_id: str, present: set[str]
) -> list[tuple[str, str]]:
    """The axes that separate this profile from the others sharing its landing.

    Returns `(column, operator)` pairs, the operator being `=` or `<>` against the
    empty marker. AI-303 returned only the first kind; AI-310 added the second,
    and the reason is below.

    WHAT THIS REPAIRS, measured on production 2026-08-17. `raw_youtube_daily`
    holds TWO report grains: the `channel_daily` profile lands one row per day for
    the whole channel (`video` empty), and `video_daily` lands one row per day PER
    VIDEO. Read together they are the same views counted twice -- 2026-07-18
    answered 1556 where the source says 778, and every one of the 33 days was
    exactly double. The two grains are not interchangeable either: `video_daily`'s
    own manifest note says subscriber flows attributed to a video "summed over the
    `video` axis do not give the channel total".

    IT WAS HIDDEN BY A SECOND DEFECT. `uq_pull_jobs_active` keyed on
    (connection, window) without the Datastream, so only ONE of the two profiles
    ever pulled: a single grain landed and the sum was right BY ACCIDENT. Migration
    275 gave each profile its pull, both grains landed, and the double became
    visible. The double-count is older than the fix that revealed it.

    WHY IT IS DERIVED AND NOT DECLARED ANEW. `stage_relation_resolver` argues at
    length that a relation cannot be guessed and must be declared -- and the
    declaration needed here ALREADY EXISTS: each report profile lists its own
    `dimensions`. `channel_daily` carries `[date, channel_id]`, `video_daily`
    carries `[date, video, channel_id]`. So a column that a SIBLING profile of the
    same `raw_relation` reports on, and this one does not, is a finer grain living
    beside these rows -- and reading this profile means restricting to the rows
    where it is absent. Nothing new is invented; what was already written is read.

    This is the same rule the breakdown filter below applies one relation over: a
    landing that holds several report shapes must be restricted to the one being
    read, or it sums the others with it.

    AI-310 -- IT ONLY PROTECTED THE COARSER HALF, and the finer one is where the
    same double lives. The rule above ("a column a SIBLING reports and I do not")
    restricts `channel_daily` to `video = ''` and is correct. Asked for
    `video_daily`, the same rule yields NOTHING: every column its sibling reports,
    it reports too. So a per-video question summed the channel roll-up beside the
    videos -- the identical double, in the direction nobody measured.

    The partition is symmetric, so the derivation is too. A column separates two
    profiles of one landing whichever side of it you stand on:

      * reported by a sibling and NOT by me -> I am coarser on that axis, my rows
        carry it empty, and the sibling's finer rows must be excluded: `= ''`.
      * reported by me and NOT by some sibling -> I am the finer one, the
        sibling's coarser rows carry it empty, and they must be excluded: `<> ''`.

    Together they make the two reads DISJOINT and EXHAUSTIVE, which the one-sided
    rule never was: read as it stood, `channel_daily` and `video_daily` returned
    the channel row twice between them and nothing was left out.

    Returns the physical column names to restrict on, with the operator that
    restricts them. Empty for the connectors whose profiles do not share a
    landing, so their SQL is unchanged.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT module_name, report_profile_id FROM app.datastreams "
                "WHERE id = %s AND project_id = %s",
                (datastream_id, project_id),
            )
            row = cur.fetchone()
        if not row or not row[0] or not row[1]:
            return []
        module_name, profile_id = str(row[0]), str(row[1])

        from core.queue import _get_manifest_for_module  # noqa: PLC0415

        profiles = (_get_manifest_for_module(module_name) or {}).get("report_profiles") or []
        mine = next((p for p in profiles if p.get("id") == profile_id), None)
        if mine is None:
            return []
        landing = mine.get("raw_relation")
        if not landing:
            return []
        my_dimensions = {str(d) for d in (mine.get("dimensions") or [])}
        sharing = [
            p
            for p in profiles
            if p.get("id") != profile_id and p.get("raw_relation") == landing
        ]
        siblings = {
            str(d) for p in sharing for d in (p.get("dimensions") or [])
        }
        # Present in the relation, reported by a sibling, and NOT by this profile:
        # the finer rows lying beside mine.
        finer = (siblings - my_dimensions) & set(present)
        # Present in the relation, reported by ME and not by at least one sibling
        # of the same landing: the coarser rows lying beside mine. `any` and not
        # `all` -- one sibling that does not report the axis is one set of rows
        # carrying it empty, and that is enough to have to exclude them.
        coarser = {
            column
            for column in my_dimensions & set(present)
            if any(
                column not in {str(d) for d in (p.get("dimensions") or [])}
                for p in sharing
            )
        }
        return [(column, "=") for column in sorted(finer)] + [
            (column, "<>") for column in sorted(coarser)
        ]
    except Exception as exc:  # noqa: BLE001 -- a read must not fail on this
        logger.warning(
            "query_execution: grain_restrictions_unreadable ds=%s: %s", datastream_id, exc
        )
        return []


def as_of_instant(as_of: str | None) -> str | None:
    """The instant an `as_of` names, a bare date meaning the END of that day.

    "Reported as of the 5th" means everything that had landed by the close of the
    5th. Binding the bare date would compare a timestamp against midnight and
    silently drop the whole day the person asked about.
    """
    if not as_of:
        return None
    text = str(as_of)
    return f"{text}T23:59:59.999999" if len(text) == 10 else text


def _superseded_source(
    relation: str, plan: dict[str, Any], as_of: str | None = None
) -> tuple[str, list[Any]]:
    """The relation as it was known at `as_of`, with earlier pulls of a row dropped.

    AS-OF IS EXECUTED HERE, and it is the same rule the mart path already applies
    (`warehouse._build_asof_query`): keep only the rows loaded at or before the
    instant, THEN let the latest surviving pull of each row win. Filtering after
    the supersede would answer with today's revision under yesterday's date.

    `resolve_physical_plan` refuses an as-of request on a relation that carries no
    `loaded_at`, so reaching here with an instant means the column exists.
    """
    present = set(plan.get("present_columns") or [])
    long_form = plan.get("long_form")
    instant = as_of_instant(as_of)
    params: list[Any] = []
    as_of_where = ""
    if instant is not None:
        as_of_where = " WHERE loaded_at <= ?"
        params.append(instant)
    if not present:
        return (
            f"(SELECT * FROM {relation}{as_of_where})" if instant else relation  # noqa: S608
        ), params
    if not long_form:
        # A WIDE landing has no single value column, so its measures must stay
        # out of the partition: the key is the GRAIN the published mapping
        # declares (`app.managed_feed_grain_v`, the same source the marts model
        # `managed_feed_superseding` reads), and the order is the load identity
        # the rows carry -- `pull_id` for a connector, `execution_id` for a
        # managed feed (a `dse_<ULID>`, monotone). AI-369, measured 2026-09-04:
        # three publications of the same two rows read as 240 clicks for 120.
        grain = [str(c) for c in (plan.get("grain_columns") or []) if str(c) in present]
        order_column = next((c for c in ("pull_id", "execution_id") if c in present), None)
        if grain and len(grain) == len(plan.get("grain_columns") or []) and order_column:
            partition = ", ".join(_safe_identifier(k) for k in grain)
            return (
                f"(SELECT * FROM {relation}{as_of_where} "  # noqa: S608 - identifiers allowlisted
                f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {partition} "
                f"ORDER BY {_safe_identifier(order_column)} DESC) = 1)"
            ), params
        logger.warning(
            "query_execution: supersede_not_applied relation=%s reason=wide_raw_landing",
            relation,
        )
        return (
            f"(SELECT * FROM {relation}{as_of_where})" if instant else relation  # noqa: S608
        ), params
    if "pull_id" not in present or "loaded_at" not in present:
        return (
            f"(SELECT * FROM {relation}{as_of_where})" if instant else relation  # noqa: S608
        ), params
    _, value_column = long_form
    keys = sorted(present - {value_column, *_PROVENANCE_COLUMNS})
    if not keys:
        return (
            f"(SELECT * FROM {relation}{as_of_where})" if instant else relation  # noqa: S608
        ), params
    partition = ", ".join(_safe_identifier(k) for k in keys)
    return (
        f"(SELECT * FROM {relation}{as_of_where} "  # noqa: S608 - identifiers allowlisted
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {partition} "
        f"ORDER BY loaded_at DESC) = 1)"
    ), params


def build_sql(
    plan: dict[str, Any], spec: dict[str, Any], *, max_rows: int | None = None
) -> tuple[str, list[Any]]:
    """Build parameterized SQL. Identifiers come from the plan, values are bound.

    *max_rows* is for a caller that STORES NO RESULT. `MAX_INLINE_ROWS` bounds
    what an immutable Result keeps inline (AC5) -- it is a storage ceiling, and
    applying it to a read that stores nothing would silently cap that read at a
    thousand rows. The precedent is spelled out in `core.model_channel`, which
    refuses to reuse the same constant as a transport budget for the same reason.
    Story 62.1's MMM extract is the first such caller: three years of daily rows
    is the ordinary size of the file it produces, and it refuses above its own
    bound rather than truncating.

    The `+ 1` is unchanged in both branches, so truncation stays detectable by
    counting rather than by trusting the limit.
    """
    # A RELATION NEEDS ITS DATASET. This dropped everything before the last dot,
    # which is right for a name that arrives qualified and fatal for one that does
    # not: BigQuery answers `Table "raw_youtube_daily" must be qualified with a
    # dataset`, and the surface turned that into "the source could not produce
    # this result". The qualifier is the one naming point the writer already uses
    # -- `warehouse_tenancy` -- so read and write cannot disagree about where the
    # rows are. An already-qualified name is left exactly as it is.
    # AI-308: `str(plan["relation"])` coerced a missing relation to the literal
    # `"None"` -- a perfectly valid identifier, so the compiler ACCEPTED what the
    # planner refuses and would have queried a table named `None`. Asked once,
    # through the predicate the planner uses.
    raw_relation = plan.get("relation")
    if not names_a_readable_relation(raw_relation):
        raise ExecutionUnavailable(f"refusing an unreadable relation: {raw_relation!r}")
    raw_relation = str(raw_relation)
    if "." in raw_relation:
        relation = ".".join(_safe_identifier(part) for part in raw_relation.split("."))
    else:
        dataset = str(plan.get("dataset") or "")
        relation = (
            f"{_safe_identifier(dataset)}.{_safe_identifier(raw_relation)}"
            if dataset
            else _safe_identifier(raw_relation)
        )
    columns = plan["columns"]

    selects: list[str] = []
    group_by: list[str] = []
    # Filled by the dimension loop and consumed by the WHERE below: a breakdown
    # landing holds every dimension of the Datastream in the same two columns, so
    # reading one of them means restricting the rows to it. Without the
    # restriction, `views by country` would sum the country rows AND the rows of
    # every other breakdown that landed beside them.
    breakdown_filters: list[tuple[str, str]] = []
    breakdown = breakdown_pivot(set(plan.get("present_columns") or []))
    for dimension in spec.get("dimensions", []):
        physical_dimension = columns[dimension["id"]]
        col = _safe_identifier(physical_dimension)
        if breakdown and physical_dimension not in set(plan.get("present_columns") or []):
            dimension_column, value_column = breakdown
            selects.append(f"{_safe_identifier(value_column)} AS {col}")
            group_by.append(_safe_identifier(value_column))
            breakdown_filters.append((dimension_column, physical_dimension))
            continue
        selects.append(col)
        group_by.append(col)
    # A measurement that arrived as a ROW is summed under a condition on its own
    # name; one that arrived as a COLUMN is summed directly. Same aggregate, and
    # the alias is the concept's name either way, so nothing downstream has to
    # know which shape answered.
    long_form = plan.get("long_form")
    present = set(plan.get("present_columns") or [])
    params: list[Any] = []
    for measure in spec.get("measures", []):
        col = _safe_identifier(columns[measure["id"]])
        # SUM is the only aggregate emitted here. The pinned concept carries its
        # own `aggregation`, and honouring anything beyond additive measures needs
        # the compiler's expression tree -- which this story does not own.
        physical = columns[measure["id"]]
        # A measure that IS a column of this relation is summed directly; one that
        # is not, on a relation that lands measurements as rows, is summed under a
        # condition on its own name. Decided per measure: a dimension of the same
        # relation is a real column, and judging the request as a whole let that
        # one column disable the pivot for every measure beside it.
        if long_form and physical not in present:
            key_column, value_column = long_form
            selects.append(
                f"SUM(IF({_safe_identifier(key_column)} = ?, "
                f"{_safe_identifier(value_column)}, NULL)) AS {col}"
            )
            params.append(physical)
        else:
            selects.append(f"SUM({col}) AS {col}")

    time = spec.get("time") or {}
    time_member = time.get("member_id")
    time_col = (
        _safe_identifier(columns[time_member])
        if time_member and columns.get(time_member)
        else None
    )

    # A PERIOD COMPARISON IS EXECUTED, and it is executed the way the cross-source
    # path executes it: one read over two frozen, non-overlapping windows, each
    # row labelled with the window it belongs to. The two windows are NOT summed
    # together and no ratio is computed here -- the Result carries both periods
    # and what to make of them belongs to the reading, not to the SQL.
    #
    # The windows come from the immutable Spec (`query_specs._comparison_windows`),
    # never re-derived here: a baseline recomputed at run time would let one
    # pinned Spec answer a different question tomorrow.
    frozen_windows = spec.get("comparison_windows")
    windows = frozen_windows if isinstance(frozen_windows, dict) else None
    if windows and time_col:
        current = windows.get("current") or {}
        baseline = windows.get("baseline") or {}
        period_field = _safe_identifier(str(windows.get("period_field") or "comparison_period"))
        case_expression = (
            "CASE "
            f"WHEN {time_col} >= ? AND {time_col} <= ? THEN 'current' "
            f"WHEN {time_col} >= ? AND {time_col} <= ? THEN 'baseline' "
            "END"
        )
        selects.append(f"{case_expression} AS {period_field}")
        params.extend(
            [current.get("start"), current.get("end"), baseline.get("start"), baseline.get("end")]
        )
        # Grouped by the ALIAS, not by a second copy of the expression: repeating
        # it would repeat its four bound values in a clause that comes after the
        # WHERE, and positional binding would then read them in the wrong order.
        group_by.append(period_field)
    else:
        windows = None

    # AD-7: A LATER PULL SUPERSEDES AN EARLIER ONE. A raw landing is append-only
    # and pull windows overlap, so a relation re-pulled three times holds three
    # copies of the same day -- measured 2026-08-12: three pulls, 196/203/203
    # rows, and the read answered 987 views for a day the source reports as 329,
    # exactly three times over. The staging model does this with a QUALIFY, and
    # this read stands in for the staging model, so it does the same thing.
    #
    # The partition is every column that IDENTIFIES a row, which on a long-form
    # landing is unambiguous: everything but the value and the three provenance
    # columns. A wide raw landing would need its grain instead -- its measures
    # must not enter the partition -- so it is left alone and said out loud
    # rather than deduplicated on a rule that would be wrong for it.
    #
    # The same subquery carries the as-of instant, so the parameters of the FROM
    # clause are appended after the SELECT list's and before the WHERE's --
    # positional binding means the order of appends IS the order in the string.
    relation, from_params = _superseded_source(relation, plan, time.get("as_of"))
    params.extend(from_params)

    where: list[str] = []
    # AI-303: restrict to THIS profile's grain before anything is summed. A
    # landing shared by two report profiles holds both, and the finer one summed
    # beside the coarser one is the same measure counted twice -- measured
    # 2026-07-18: 1556 where the source says 778, on all 33 days. The marker is
    # the absent value of the sibling's dimension, which is what the connector
    # lands when it does not report on that axis.
    #
    # AI-310: the operator travels WITH the column, because the exclusion runs
    # both ways. `= ''` drops the finer rows when this profile is the coarse one;
    # `<> ''` drops the coarse roll-up when this profile is the fine one. Only the
    # first existed, so a per-video question still summed the channel row.
    for column, operator in plan.get("grain_restrictions") or []:
        where.append(f"{_safe_identifier(column)} {operator} ?")
        params.append("")

    for dimension_column, wanted_dimension in breakdown_filters:
        where.append(f"{_safe_identifier(dimension_column)} = ?")
        params.append(wanted_dimension)
    for filt in spec.get("filters", []):
        col = columns.get(filt["member_id"])
        if not col:
            continue
        col = _safe_identifier(col)
        operator = filt["operator"]
        if operator == "eq":
            where.append(f"{col} = ?")
            params.append(filt["value"])
        elif operator == "in" and isinstance(filt.get("value"), list) and filt["value"]:
            where.append(f"{col} IN ({', '.join('?' for _ in filt['value'])})")
            params.extend(filt["value"])
        elif operator == "is_null":
            where.append(f"{col} IS NULL")
        elif operator == "is_not_null":
            where.append(f"{col} IS NOT NULL")

    if time_col and windows:
        # Both frozen windows, and nothing between them. Widening to a single span
        # from the baseline start to the current end would drag in the months the
        # person did not ask for and label them `NULL`.
        current = windows.get("current") or {}
        baseline = windows.get("baseline") or {}
        where.append(
            f"(({time_col} >= ? AND {time_col} <= ?) OR ({time_col} >= ? AND {time_col} <= ?))"
        )
        params.extend(
            [current.get("start"), current.get("end"), baseline.get("start"), baseline.get("end")]
        )
    elif time_col:
        if time.get("start"):
            where.append(f"{time_col} >= ?")
            params.append(time["start"])
        if time.get("end"):
            where.append(f"{time_col} <= ?")
            params.append(time["end"])

    sql = f"SELECT {', '.join(selects)} FROM {relation}"  # noqa: S608 - identifiers allowlisted
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group_by:
        sql += " GROUP BY " + ", ".join(group_by)
    order = [
        f"{_safe_identifier(columns[s['member_id']])} {s['direction'].upper()}"
        for s in spec.get("sort", [])
        if columns.get(s["member_id"])
    ]
    if order:
        sql += " ORDER BY " + ", ".join(order)
    # One row over the REAL frozen-storage limit, so truncation is detected
    # without asking the warehouse for rows this Result can never preserve. A
    # caller that stores no Result declares its own bound instead -- see the
    # docstring; `int()` is applied to both so neither door takes a string.
    if max_rows is not None:
        storage_limit = max(int(max_rows), 0)
    else:
        requested_limit = int(spec.get("row_limit") or MAX_INLINE_ROWS)
        storage_limit = min(requested_limit, MAX_INLINE_ROWS)
    sql += f" LIMIT {storage_limit + 1}"
    return sql, params


def shape_result_payload(
    *,
    result_id: str,
    physical_rows: list[dict[str, Any]],
    plan: dict[str, Any],
    spec: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Apply the pinned server-authored Result shape after warehouse execution."""
    result_shape = str(spec.get("result_shape") or "tabular_v1")
    if result_shape == "tabular_v1":
        #  THE FIELD CARRIES ITS MEMBER ID, not only its column name. A
        #  Visualization Spec binds to CONCEPT IDS -- that is what the server
        #  validates it against -- while this projection named its columns
        #  `date`, `views`. The rendering runtime accepts either key
        #  (`viz/validate.ts`: `field?.name` OR `field?.id`), so the missing half
        #  was here: a spec the server had just declared valid was REFUSED by the
        #  runtime for naming a field "the Result projection does not carry".
        #  Measured 2026-08-13 by G14-T01, on a spec G13-T08 had passed.
        #  The mapping is not invented: `plan["columns"]` already holds
        #  member id -> physical column.
        columns = plan.get("columns") or {}
        member_of = {str(physical): str(member) for member, physical in columns.items()}
        keys = list(physical_rows[0].keys()) if physical_rows else []
        fields: list[dict[str, Any]] = []
        for key in keys:
            field: dict[str, Any] = {"name": key}
            member = member_of.get(str(key))
            if member:
                field["id"] = member
            fields.append(field)
        return {
            "rows": physical_rows,
            "schema": {"fields": fields},
            "manifest": {"result_shape": result_shape},
        }
    if result_shape != "waterfall_v1":
        raise ResultShapeRefused(
            "unknown_result_shape", f"Result shape `{result_shape}` is not implemented"
        )

    descriptor = spec.get("result_shape_descriptor")
    if not isinstance(descriptor, dict):
        raise ResultShapeRefused(
            "missing_result_shape_descriptor",
            "the pinned Query Spec carries no server-authored waterfall_v1 descriptor",
            outcome="unavailable",
        )
    columns = plan.get("columns") or {}
    member_rows = [
        {member_id: row.get(physical) for member_id, physical in columns.items()}
        for row in physical_rows
    ]
    provenance_values = (evidence.get("provenance") or {}).get("values") or []
    provenance_by_member = {
        str(item.get("member_id")): dict(item)
        for item in provenance_values
        if isinstance(item, dict) and item.get("member_id")
    }
    component_members = {
        str(component.get("member_id"))
        for component in descriptor.get("components") or []
        if isinstance(component, dict) and component.get("member_id")
    }
    evidence_keys = {
        member_id: f"{result_id}:evidence:{member_id}"
        for member_id in component_members
        if member_id in provenance_by_member
    }
    shaped = serialize_waterfall_v1(
        result_id=result_id,
        source_rows=member_rows,
        descriptor=descriptor,
        grain=spec.get("grain"),
        evidence_by_member=evidence_keys,
    )
    shaped["manifest"]["datum_evidence"] = {
        evidence_key: provenance_by_member[member_id]
        for member_id, evidence_key in evidence_keys.items()
    }
    return shaped


def required_capability_outcome(
    conn,
    *,
    project_id: str,
    spec: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Resolve a server-pinned capability prerequisite without caller authority."""
    requirement = spec.get("required_capability")
    if not isinstance(requirement, dict) or not requirement.get("key"):
        return None
    capability_key = str(requirement["key"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT state, active_version_id
                FROM app.project_capabilities
                WHERE project_id = %s AND capability_key = %s
                """,
                (project_id, capability_key),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        return (
            "unavailable",
            {
                "reason_code": "CAPABILITY_AUTHORITY_UNAVAILABLE",
                "unavailable_reason": str(exc),
                "missing_link": "project_capabilities",
            },
        )
    if row is not None and str(row[0]) != "disabled":
        return None

    # LE LIEN POINTAIT VERS UNE SECTION QUE LA CONSOLE NE DECLARE PAS -- story
    # 67.11. Il composait `governance/capabilities`, un couple espace/section qui
    # n'existe dans aucun registre de navigation : le refus nommait donc une
    # porte, et cette porte ne s'ouvrait pas. Les capacites d'un Projet vivent
    # sur la surface GLOBALE des reglages (`project-settings` / `capabilities`,
    # `ui/admin/src/shell/ownerResolution.ts:104`), qui est ce que
    # `_global_owner_reference` compose -- comme les deux autres liens de
    # `project_overview` le font deja.
    #
    # Un refus qui nomme un geste que personne ne peut faire est pire qu'un refus
    # muet : le muet envoie chercher, celui-la envoie au mauvais endroit.
    from core.project_overview import _global_owner_reference  # noqa: PLC0415

    return (
        "refused",
        {
            "reason_code": "CAPABILITY_DISABLED",
            "reason": "The required project capability is disabled.",
            # `capability_key` reste dans la charge : la surface globale n'a pas
            # d'objet a epingler, et perdre QUELLE capacite est refusee ferait
            # arriver la personne sur la bonne page sans savoir quoi y activer.
            "capability_key": capability_key,
            "owner_link": _global_owner_reference("project-settings", "capabilities"),
        },
    )


# ---------------------------------------------------------------------------
# Terminalization: exactly one immutable Result, whatever happened.
# ---------------------------------------------------------------------------


def terminalize(
    conn,
    *,
    attempt: dict[str, Any],
    org_id: str,
    project_id: str,
    outcome: str,
    started_at: datetime,
    rows: list[dict[str, Any]] | None = None,
    result_schema: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    truncated: bool = False,
    ai_path_id: str | None = None,
    predecessor_result_id: str | None = None,
) -> dict[str, Any]:
    """Write the attempt's single Result plus its mandatory payload."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown terminal outcome: {outcome}")

    rows = list(rows or [])
    if len(rows) > MAX_INLINE_ROWS:
        rows = rows[:MAX_INLINE_ROWS]
        truncated = True
    schema = result_schema or {"fields": []}
    from core.feedback_review import freeze_result_classification  # noqa: PLC0415

    classification = freeze_result_classification(
        conn,
        org_id=org_id,
        project_id=project_id,
        query_spec_version_id=str(attempt["query_spec_version_id"]),
        outcome=outcome,
        ai_path_id=ai_path_id,
    )
    body = {**(manifest or {}), "evaluation_classification": classification}
    payload_document = {"schema": schema, "rows": rows, "manifest": body}
    content_hash = canonical_hash(payload_document)
    cell_count = sum(len(r) for r in rows)
    byte_count = len(json.dumps(payload_document, ensure_ascii=False).encode("utf-8"))
    ended_at = _now()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.query_results
                (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                 ai_path_id, ai_path_absent_literal, content_hash, row_count, cell_count,
                 byte_count, truncated, predecessor_result_id, started_at, ended_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                attempt["result_id"],
                org_id,
                project_id,
                attempt["attempt_id"],
                attempt["query_spec_version_id"],
                outcome,
                ai_path_id,
                None if ai_path_id else NO_AI_PATH,
                content_hash,
                len(rows),
                cell_count,
                byte_count,
                truncated,
                predecessor_result_id,
                started_at,
                ended_at,
            ),
        )
        # Mandatory for EVERY outcome. An empty or refused Result keeps a full
        # manifest explaining why no rows exist (AC5) -- there is no
        # "Result without payload" state to fall into.
        cur.execute(
            """
            INSERT INTO app.query_result_payloads
                (result_id, org_id, project_id, content_hash, result_schema, manifest, rows_chunk)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
            """,
            (
                attempt["result_id"],
                org_id,
                project_id,
                content_hash,
                json.dumps(schema),
                json.dumps(body),
                json.dumps(rows),
            ),
        )
        cur.execute(
            """
            UPDATE app.query_execution_attempts
            SET state = 'terminal', terminal_at = %s
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (ended_at, attempt["attempt_id"], org_id, project_id),
        )
    return {
        "result_id": attempt["result_id"],
        "outcome": outcome,
        "row_count": len(rows),
        "truncated": truncated,
        "content_hash": content_hash,
        "ai_path": ai_path_id or NO_AI_PATH,
    }


_DEFERRED_TERMINALIZATION = "_deferred_terminalization"


def _terminalize_or_defer(
    conn, *, defer_terminalization: bool, **terminal_kwargs: Any
) -> dict[str, Any]:
    if not defer_terminalization:
        return terminalize(conn, **terminal_kwargs)
    attempt = terminal_kwargs["attempt"]
    return {
        "result_id": attempt["result_id"],
        "outcome": terminal_kwargs["outcome"],
        "ai_path": terminal_kwargs.get("ai_path_id") or NO_AI_PATH,
        _DEFERRED_TERMINALIZATION: terminal_kwargs,
    }


def complete_deferred_result(conn, pending: dict[str, Any]) -> dict[str, Any]:
    """Write a Result after its transaction-owned AI Path has reached finalized."""
    terminal_kwargs = pending.get(_DEFERRED_TERMINALIZATION)
    if not isinstance(terminal_kwargs, dict):
        raise ValueError("the Result is not awaiting terminalization")
    return terminalize(conn, **terminal_kwargs)


def run_execution(
    conn,
    *,
    attempt: dict[str, Any],
    org_id: str,
    project_id: str,
    spec: dict[str, Any],
    semantic_view_version_id: str,
    ai_path_id: str | None = None,
    defer_terminalization: bool = False,
) -> dict[str, Any]:
    """Resolve and execute, writing one Result unless the owning path defers it."""
    started_at = _now()
    base_manifest = {
        "semantic_view_version_id": semantic_view_version_id,
        "query_spec_version_id": attempt["query_spec_version_id"],
        "grain": spec.get("grain"),
        "time": spec.get("time"),
        "filters": spec.get("filters"),
        "comparison": spec.get("comparison"),
        "row_limit": spec.get("row_limit"),
        "result_shape": spec.get("result_shape") or "tabular_v1",
    }

    append_event(
        conn,
        attempt_id=attempt["attempt_id"],
        org_id=org_id,
        project_id=project_id,
        ordinal=2,
        state="running",
        detail={},
    )

    capability_terminal = required_capability_outcome(
        conn,
        project_id=project_id,
        spec=spec,
    )
    if capability_terminal is not None:
        outcome, detail = capability_terminal
        return _terminalize_or_defer(
            conn,
            defer_terminalization=defer_terminalization,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            outcome=outcome,
            started_at=started_at,
            manifest={**base_manifest, **detail},
            ai_path_id=ai_path_id,
        )

    # AI-308: THE ATTEMPT IS ALREADY ACCEPTED HERE, so from this line on every
    # exit is a Result. `resolve_physical_plan` allowlists the mapped physical
    # columns (`_safe_identifier`), and a mapping that names something that is not
    # an identifier raised straight through this call -- past the terminalization,
    # out of `run_execution`, into a 500 with no Result to read or replay.
    try:
        plan = resolve_physical_plan(
            conn,
            project_id=project_id,
            semantic_view_version_id=semantic_view_version_id,
            spec=spec,
        )
    except ExecutionUnavailable as exc:
        # THE ANSWER NAMES THE GESTURE; THE RECORD NAMES THE CAUSE. The refused
        # identifier is evidence for whoever repairs the mapping and must never
        # reach a screen, so it goes to the log and to `missing_link`.
        logger.warning("query_execution: unreadable_identifier in plan: %s", exc, exc_info=True)
        _record_semantic_query_crossing(
            semantic_view_version_id=semantic_view_version_id,
            outcome="unavailable",
        )
        return _terminalize_or_defer(
            conn,
            defer_terminalization=defer_terminalization,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            outcome="unavailable",
            started_at=started_at,
            manifest={
                **base_manifest,
                "unavailable_reason": UNREADABLE_LANDING_MESSAGE,
                "missing_link": "physical_identifier",
            },
            ai_path_id=ai_path_id,
        )
    if "unavailable_reason" in plan:
        return _terminalize_or_defer(
            conn,
            defer_terminalization=defer_terminalization,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            outcome="unavailable",
            started_at=started_at,
            manifest={
                **base_manifest,
                "unavailable_reason": plan["unavailable_reason"],
                "missing_link": plan["missing_link"],
            },
            ai_path_id=ai_path_id,
        )

    # Captured BEFORE the query runs, so a Result carries its evidence even when
    # the runner then fails: an `unavailable` answer still says which source,
    # mapping version and pull it was about to read.
    evidence = capture_evidence(conn, project_id=project_id, plan=plan)
    base_manifest = {**base_manifest, **evidence}

    # And the same fact from the OTHER side. `capture_evidence` tells the Result
    # which Output it read; this tells the Output which Result read it. The
    # Datastream Workbench's `Used by` panel has had three readers and no writer
    # since migration 138, so it has been answering from an empty table.
    record_output_consumption(
        conn,
        project_id=project_id,
        plan=plan,
        result_id=str(attempt.get("result_id") or ""),
        query_spec_version_id=attempt.get("query_spec_version_id"),
    )

    try:
        sql, params = build_sql(plan, spec)
    except ExecutionUnavailable as exc:
        # The last identifier seam: filters and sorts carry columns the planner
        # did not allowlist. Same class, same answer, same sentence.
        logger.warning("query_execution: unreadable_identifier in sql: %s", exc, exc_info=True)
        _record_semantic_query_crossing(
            semantic_view_version_id=semantic_view_version_id,
            outcome="unavailable",
        )
        return _terminalize_or_defer(
            conn,
            defer_terminalization=defer_terminalization,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            outcome="unavailable",
            started_at=started_at,
            manifest={
                **base_manifest,
                "unavailable_reason": UNREADABLE_LANDING_MESSAGE,
                "missing_link": "physical_identifier",
            },
            ai_path_id=ai_path_id,
        )

    try:
        from core import warehouse  # noqa: PLC0415

        bigquery_mode = warehouse._db_mode() == "bigquery"
        runner = warehouse._query_bigquery if bigquery_mode else warehouse._query_duckdb
        if bigquery_mode:
            # `_query_bigquery` binds `@p0..@pN` and runs the string as it is, so
            # a `?` reaches BigQuery as a syntax error. Nothing had noticed:
            # every execution failed earlier, on the relation, before a parameter
            # was ever bound. Positional and in order, exactly as the parameters
            # were appended.
            sql = _bind_positional(sql)
        rows = runner(sql, params)
    except Exception as exc:  # noqa: BLE001
        # THE ANSWER STAYS OPAQUE; THE RECORD MUST NOT. The reason travels in the
        # manifest and the surface serves "The source could not produce this
        # result." -- correct for a caller, useless for anyone repairing it. The
        # same silence cost a full diagnosis pass on the Semantic Model 503.
        logger.warning(
            "query_execution: warehouse_unreadable relation=%s: %s: %s",
            plan.get("relation"),
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        _record_semantic_query_crossing(
            semantic_view_version_id=semantic_view_version_id,
            outcome="unavailable",
        )
        # A runner failure is `unavailable`, never `empty`: we could not ask, so
        # claiming nothing matched would be a lie about the data.
        return _terminalize_or_defer(
            conn,
            defer_terminalization=defer_terminalization,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            outcome="unavailable",
            started_at=started_at,
            manifest={**base_manifest, "unavailable_reason": str(exc), "missing_link": "warehouse"},
            ai_path_id=ai_path_id,
        )

    _record_semantic_query_crossing(
        semantic_view_version_id=semantic_view_version_id,
        outcome="succeeded",
    )

    limit = min(int(spec.get("row_limit") or MAX_INLINE_ROWS), MAX_INLINE_ROWS)
    truncated = len(rows) > limit
    rows = rows[:limit]
    try:
        shaped = shape_result_payload(
            result_id=attempt["result_id"],
            physical_rows=rows,
            plan=plan,
            spec=spec,
            evidence=evidence,
        )
    except ResultShapeRefused as exc:
        return _terminalize_or_defer(
            conn,
            defer_terminalization=defer_terminalization,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            outcome=exc.outcome,
            started_at=started_at,
            manifest={**base_manifest, "reason_code": exc.code, "reason": exc.message},
            ai_path_id=ai_path_id,
        )
    shaped_rows = shaped["rows"]
    outcome = "success" if shaped_rows else "empty"

    # CHANTIER B -- a breakdown states its distance from the declared total, and
    # the reason for it when one was declared. Snapshotted into the manifest with
    # the rest of the evidence, so a Result keeps saying what was true the day it
    # ran even after the declaration moves.
    try:
        grain_reconciliation = reconcile_grains(
            conn,
            project_id=project_id,
            semantic_view_version_id=semantic_view_version_id,
            plan=plan,
            spec=spec,
            # The warehouse rows, not the shaped ones. See `reconcile_grains`.
            rows=rows,
            truncated=truncated,
        )
    except Exception as exc:  # noqa: BLE001
        # A reconciliation that could not run must not take the answer down, and
        # must not look like "nothing to reconcile" either. The trace names the
        # cause; the manifest carries the absence.
        logger.warning(
            "query_execution: grain reconciliation failed: %s: %s",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        grain_reconciliation = None

    return _terminalize_or_defer(
        conn,
        defer_terminalization=defer_terminalization,
        attempt=attempt,
        org_id=org_id,
        project_id=project_id,
        outcome=outcome,
        started_at=started_at,
        rows=shaped_rows,
        result_schema=shaped["schema"],
        manifest={
            **base_manifest,
            **shaped["manifest"],
            "relation": plan["relation"],
            "truncated": truncated,
            "chosen_by": plan.get("chosen_by"),
            "grain_reconciliation": grain_reconciliation,
        },
        truncated=truncated,
        ai_path_id=ai_path_id,
    )


def _record_semantic_query_crossing(
    *, semantic_view_version_id: str, outcome: str
) -> bool:
    """Observe the governed query without exposing its SQL, relation or arguments."""
    from core.ai_path_recorder import emit_step_sync  # noqa: PLC0415

    return emit_step_sync(
        step_kind="semantic_query",
        outcome=outcome,
        tool_name="execute_analyze_query_spec",
        owner_workspace="analyze",
        owner_object_type="semantic-view-version",
        owner_object_id=semantic_view_version_id,
    )


def terminalize_interrupted(conn, *, project_id: str, older_than_seconds: int = 900) -> list[str]:
    """Close attempts that were accepted but never finished. AC4's recovery half.

    Without this an accepted attempt can sit forever with a Result address and no
    Result -- which is exactly the permanently-pending state the attempt/Result
    split was built to prevent.
    """
    closed: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, org_id, project_id, query_spec_version_id, result_id, accepted_at
            FROM app.query_execution_attempts
            WHERE project_id = %s AND terminal_at IS NULL
              AND accepted_at < NOW() - (%s * INTERVAL '1 second')
            """,
            (project_id, older_than_seconds),
        )
        stale = cur.fetchall()
    for attempt_id, org_id, proj, spec_version_id, result_id, accepted_at in stale:
        terminalize(
            conn,
            attempt={
                "attempt_id": attempt_id,
                "result_id": result_id,
                "query_spec_version_id": spec_version_id,
            },
            org_id=org_id,
            project_id=proj,
            outcome="unavailable",
            started_at=accepted_at,
            manifest={
                "unavailable_reason": (
                    "This run was interrupted and never reported a result. Nothing "
                    "was produced; run the question again."
                ),
                "missing_link": "execution",
                "recovered": True,
            },
        )
        closed.append(result_id)
    return closed
