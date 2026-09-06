"""Governed key matching with explicit verdicts (Story 68.3).

THE DRIVER THE ENGINE NEVER HAD. `tracked_entities.rank_candidates` ranks the
identities an observed value might denote, `master_data_aliases` stores the
typed claims, and `dbt/macros/resolve_governed_node.sql` ratified the
resolution taxonomy -- but measured on the day this module was written, NO
production caller drove any of them at landing time. Story 68.2 lets a mapping
column declare which entity type its values are keys OF
(``fields[].binding.designates_object_kind``); this module is what runs that
declaration: each DISTINCT value a pull lands in a designated column resolves
to an MDM node with a persisted verdict, so no approximate join ever happens
silently.

THE TAXONOMY IS RATIFIED AND CLOSED. A verdict is ``resolved``, ``unmatched``
or ``ambiguous`` -- the ``governed_node_resolution_state`` vocabulary, never a
third invention. ``ambiguous`` persists the candidate list and NEVER auto-picks
(the ``resolve_governed_node`` rule: refuse, never choose -- ``COUNT(*)=1``,
never ``LIMIT 1``). The database CHECK on ``entity_key_match_verdicts`` makes
the same point a fact: ``resolved`` names its node, and nothing else may.

TWO RESOLVERS, ONE AUTHORITY EACH:

* the direct ``master_data_aliases`` lookup (source_key mode) -- a live,
  non-conflicted ``exact`` alias whose stored ``normalized_value`` is the
  value's normalized form. One hit resolves; two hits on two nodes of the SAME
  registry are the homonym the unique index cannot see (different namespaces,
  overlapping windows), and they are ``ambiguous``, never arbitrated;
* ``rank_candidates`` over the registry's alias corpus (governed_label mode) --
  a ``ranked`` + ``exact`` proposal resolves; an ``ambiguous`` proposal names
  its candidates; everything else (no candidate, below threshold, negative
  alias, a similarity win that is only ``close``) is ``unmatched`` WITH its
  candidates persisted, because what was not chosen is the evidence a person
  repairs from.

NORMALIZATION HAS EXACTLY ONE AUTHORITY: ``normalize_alias_value`` in
:mod:`core.master_data`. This module defines no fold of its own, and the value
it persists is the same key the aliases were stored under -- the key
``governed_alias_normalized`` mirrors in the warehouse.

IDEMPOTENCE IS THE COVERAGE CONTRACT. The occurrence key is (datastream, field,
value hash, window): a replayed pull over the same window with the same outcome
writes nothing, so ``matching_coverage`` never inflates. The same occurrence
resolving DIFFERENTLY -- an alias was recorded, a node declared -- is a new
``current`` row superseding the old one (append-only, migration 296), never an
edit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

from core.audit import declare_action, insert_audit_row
from core.entity_bindings import hash_value
from core.master_data import (
    MasterDataConflict,
    _mint,
    _required,
    current_node_versions,
    fetch_registry,
    list_aliases,
    list_nodes,
    normalize_alias_value,
)
from core.object_kind_registry import entity_designations
from core.tracked_entities import (
    REASON_AMBIGUOUS,
    REASON_RANKED,
    RELATION_EXACT,
    RELATION_NONE,
    Candidate,
    Proposal,
    build_corpus,
    rank_candidates,
)

logger = logging.getLogger("core.entity_key_matching")

# ---------------------------------------------------------------------------
# What this module writes to the audit trail -- AD-42: an action is declared
# where it is WRITTEN, never in a central list nobody owns.
# ---------------------------------------------------------------------------

ACTION_ENTITY_KEY_MATCHED = declare_action("mdm.entity_key.matched")

#: The ratified verdict taxonomy (AC1). CLOSED: a fourth word is a third
#: invention, and the table CHECK refuses it.
VERDICT_RESOLVED = "resolved"
VERDICT_UNMATCHED = "unmatched"
VERDICT_AMBIGUOUS = "ambiguous"
VERDICTS = (VERDICT_RESOLVED, VERDICT_UNMATCHED, VERDICT_AMBIGUOUS)

#: Reasons this module adds to the ranker's own vocabulary: the direct alias
#: lookup resolving, and a designation whose type cannot be resolved against
#: (undeclared, or archived since the mapping was published).
REASON_EXACT_LOOKUP = "exact_lookup"
REASON_NO_CANDIDATE = "no_candidate"
REASON_TYPE_UNAVAILABLE = "entity_type_unavailable"

STATE_CURRENT = "current"
STATE_SUPERSEDED = "superseded"

_VERDICT_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "datastream_id",
    "execution_id",
    "mapping_version_id",
    "registry_id",
    "object_kind",
    "field_id",
    "raw_value_hash",
    "normalized_value",
    "occurrence_count",
    "observed_from",
    "observed_to",
    "verdict",
    "relation",
    "node_id",
    "candidates",
    "confidence",
    "reason_code",
    "evidence",
    "state",
    "supersedes_id",
    "created_by",
    "created_at",
)


# ---------------------------------------------------------------------------
# The verdict. Pure outcomes, so every case is provable without a database
# (the discipline `test_tracked_entity_matching_proposals.py` states).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verdict:
    """What one occurrence resolved to. A verdict, never a decision.

    ``candidates`` survives every outcome, including the refusals: an ambiguous
    occurrence names the identities a person must choose between, and an
    unmatched one keeps what was ranked and rejected.
    """

    verdict: str
    relation: str
    reason_code: str
    node_id: str | None = None
    candidates: tuple[dict[str, Any], ...] = ()
    confidence: float | None = None
    features: Mapping[str, Any] = field(default_factory=dict)


def verdict_from_exact_hits(matches: Sequence[tuple[str, str]]) -> Verdict:
    """The direct-lookup verdict. Pure.

    ``matches`` is ``(node_id, matched_surface)`` for every live, exact,
    non-conflicted alias whose stored key is the occurrence's normalized value.
    The ``COUNT(*)=1`` rule, verbatim: one NODE resolves, two nodes are a
    homonym the server refuses to arbitrate -- never ``LIMIT 1``.
    """

    by_node: dict[str, str] = {}
    for node_id, surface in matches:
        by_node.setdefault(str(node_id), str(surface))
    if not by_node:
        return Verdict(
            verdict=VERDICT_UNMATCHED,
            relation=RELATION_NONE,
            reason_code=REASON_NO_CANDIDATE,
            features={"resolver": REASON_EXACT_LOOKUP},
        )
    candidates = tuple(
        {
            "node_id": node_id,
            "matched_surface": by_node[node_id],
            "relation": RELATION_EXACT,
            "score": 1.0,
            "method": REASON_EXACT_LOOKUP,
        }
        for node_id in sorted(by_node)
    )
    if len(candidates) > 1:
        return Verdict(
            verdict=VERDICT_AMBIGUOUS,
            relation=RELATION_NONE,
            reason_code=REASON_AMBIGUOUS,
            candidates=candidates,
            confidence=1.0,
            features={"resolver": REASON_EXACT_LOOKUP},
        )
    return Verdict(
        verdict=VERDICT_RESOLVED,
        relation=RELATION_EXACT,
        reason_code=REASON_EXACT_LOOKUP,
        node_id=candidates[0]["node_id"],
        candidates=candidates,
        confidence=1.0,
        features={"resolver": REASON_EXACT_LOOKUP},
    )


def verdict_from_proposal(proposal: Proposal) -> Verdict:
    """Map a ranking outcome onto the ratified taxonomy. Pure.

    Only a ``ranked`` proposal whose relation is ``exact`` resolves -- a
    similarity win is ``close``, and SKOS is explicit that a close match is not
    transitive, so promoting it here would be the silent approximate join this
    story exists to refuse. An ambiguous proposal keeps its candidates; every
    other outcome is ``unmatched`` with its reason preserved.
    """

    candidates = tuple(candidate.as_dict() for candidate in proposal.candidates)
    if (
        proposal.reason_code == REASON_RANKED
        and proposal.relation == RELATION_EXACT
        and proposal.top is not None
    ):
        return Verdict(
            verdict=VERDICT_RESOLVED,
            relation=proposal.relation,
            reason_code=proposal.reason_code,
            node_id=proposal.top.node_id,
            candidates=candidates,
            confidence=proposal.confidence,
            features=dict(proposal.features),
        )
    if proposal.reason_code == REASON_AMBIGUOUS:
        return Verdict(
            verdict=VERDICT_AMBIGUOUS,
            relation=RELATION_NONE,
            reason_code=proposal.reason_code,
            candidates=candidates,
            confidence=proposal.confidence,
            features=dict(proposal.features),
        )
    return Verdict(
        verdict=VERDICT_UNMATCHED,
        relation=proposal.relation,
        reason_code=proposal.reason_code,
        candidates=candidates,
        confidence=proposal.confidence,
        features=dict(proposal.features),
    )


def _as_candidates(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):  # pragma: no cover - driver-dependent
        value = json.loads(value)
    return [dict(entry) for entry in (value or [])]


def same_outcome(existing: Mapping[str, Any], verdict: Verdict) -> bool:
    """True when the current row IS this verdict -- the replay path. Pure.

    Compared on what a reader acts on: the verdict, the node, the relation, the
    reason and the candidates. ``confidence`` is deliberately OUT: a corpus
    edit that moves a score by a tenth without moving the outcome is not a new
    fact about the occurrence, and superseding on it would churn the history
    every time an unrelated alias is recorded.
    """

    return (
        str(existing.get("verdict")) == verdict.verdict
        and (existing.get("node_id") or None) == verdict.node_id
        and str(existing.get("relation")) == verdict.relation
        and str(existing.get("reason_code")) == verdict.reason_code
        and _as_candidates(existing.get("candidates")) == [dict(c) for c in verdict.candidates]
    )


# ---------------------------------------------------------------------------
# The coverage read (AC2): N resolved of M occurrences, CoverageReport-shaped.
# ---------------------------------------------------------------------------


def compose_coverage(rows: Sequence[Sequence[Any]]) -> dict[str, Any]:
    """Aggregate verdict counts into the coverage report. Pure.

    ``rows`` is ``(datastream_id, object_kind, verdict, count)`` as the GROUP
    BY returns it. ``bound``/``eligible`` follow the CoverageReport contract:
    one population on both sides of the fraction (current verdict rows), and
    the unavailable case is a separate constructor, never a zero.
    """

    per_pair: dict[tuple[str, str], dict[str, int]] = {}
    totals: dict[str, int] = {verdict: 0 for verdict in VERDICTS}
    for datastream_id, object_kind, verdict, count in rows:
        key = (str(datastream_id), str(object_kind))
        entry = per_pair.setdefault(key, {v: 0 for v in VERDICTS})
        entry[str(verdict)] += int(count)
        totals[str(verdict)] = totals.get(str(verdict), 0) + int(count)

    bound = totals.get(VERDICT_RESOLVED, 0)
    eligible = sum(totals.values())
    pair_rows = [
        {
            "datastream_id": datastream_id,
            "object_kind": object_kind,
            "bound": entry.get(VERDICT_RESOLVED, 0),
            "eligible": sum(entry.values()),
            "by_state": {state: n for state, n in entry.items() if n},
        }
        for (datastream_id, object_kind), entry in sorted(per_pair.items())
    ]
    return {
        "state": "available" if eligible else "empty",
        "coverage": {
            "bound": bound,
            "eligible": eligible,
            "by_state": {state: n for state, n in totals.items() if n},
        },
        "rows": pair_rows,
        "unavailable_reason": None,
    }


def unavailable_coverage(detail: str) -> dict[str, Any]:
    """The shape when the verdict store cannot be read. Counts None, never 0.

    A number here would be read as a measurement -- the rule
    ``semantic_coverage.unavailable_report`` states for mapping coverage,
    applied to occurrence coverage.
    """

    return {
        "state": "unavailable",
        "coverage": {"bound": None, "eligible": None, "by_state": {}},
        "rows": [],
        "unavailable_reason": {
            "code": "entity_key_verdicts_unreadable",
            "message": (
                "The entity key verdict store could not be read, so matching "
                "coverage is unknown for this Project. This is not a coverage of zero."
            ),
            "detail": detail,
        },
    }


def matching_coverage(conn, *, project_id: str) -> dict[str, Any]:
    """N resolved of M occurrences per (datastream, entity type), queryable.

    The read Story 68.7 composes into the reconciliation context. Only
    ``current`` rows count: a superseded verdict is history, not coverage, and
    a replayed pull is one verdict row -- never two.
    """

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT datastream_id, object_kind, verdict, COUNT(*)
                  FROM app.entity_key_match_verdicts
                 WHERE project_id = %s AND state = 'current'
                 GROUP BY datastream_id, object_kind, verdict
                """,
                (_required(project_id, "project_id"),),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- the adapter, not the data, failed
        logger.warning(
            "entity_key_matching: coverage unreadable project=%s: %s", project_id, exc
        )
        return unavailable_coverage(str(exc))
    return compose_coverage(rows)


# ---------------------------------------------------------------------------
# The reads the resolution composes from.
# ---------------------------------------------------------------------------


def _pinned_designations(
    conn, *, project_id: str, datastream_id: str
) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """The ``(field_id, object_kind)`` pairs the PINNED mapping designates.

    The ONLY mapping that counts is the one the Datastream currently points at
    (the rule `semantic_coverage` states for active mappings): a designation
    read from a draft would resolve against a binding the Project has not
    decided on. ``None`` means "this Datastream pins no mapping" or "nothing is
    designated" -- both are "nothing to resolve", and the caller writes no row.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.mapping_payload
              FROM app.datastreams d
              JOIN app.datastream_mapping_versions v
                ON v.id = d.current_mapping_version_id
               AND v.datastream_id = d.id
               AND v.project_id = d.project_id
             WHERE d.id = %s AND d.project_id = %s
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    mapping_version_id, payload = row
    if isinstance(payload, str):  # pragma: no cover - driver-dependent
        payload = json.loads(payload)
    designations = entity_designations(payload or {})
    if not designations:
        return None
    return str(mapping_version_id), designations


def _exact_alias_hits(
    conn,
    *,
    org_id: str,
    project_id: str,
    registry_id: str,
    normalized_values: Sequence[str],
) -> dict[str, list[tuple[str, str]]]:
    """Live exact aliases of THIS registry's nodes, keyed by normalized value.

    Mirrors the dbt predicate of ``resolve_governed_node``: relation ``exact``,
    no conflict recorded, not retired, inside its effective window, on a node
    that is itself live. Aliases are org-anchored (migration 143 writes
    ``project_id NULL``), so the project scope rides the NODE, and an alias
    that names a project belongs to that project alone -- the seam the dbt
    macro comment states.

    NOT namespace-scoped, deliberately: the designation names the TYPE the
    value is a key of, not the namespace it was spelled in, so the registry is
    the authority the lookup scopes to.
    """

    values = sorted({value for value in normalized_values if value})
    if not values:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.normalized_value, a.node_id, a.raw_value
              FROM app.master_data_aliases a
              JOIN app.master_data_nodes n
                ON n.id = a.node_id AND n.org_id = a.org_id
             WHERE a.org_id = %s
               AND n.project_id = %s
               AND n.registry_id = %s
               AND n.archived_at IS NULL
               AND (a.project_id IS NULL OR a.project_id = %s)
               AND a.normalized_value = ANY(%s)
               AND a.relation = 'exact'
               AND a.retired_at IS NULL
               AND a.conflict_state = 'none'
               AND a.effective_from <= CURRENT_DATE
               AND (a.effective_to IS NULL OR a.effective_to > CURRENT_DATE)
             ORDER BY a.normalized_value, a.node_id, a.raw_value
            """,
            (org_id, project_id, registry_id, project_id, values),
        )
        rows = cur.fetchall()
    hits: dict[str, list[tuple[str, str]]] = {}
    for normalized_value, node_id, raw_value in rows:
        hits.setdefault(str(normalized_value), []).append((str(node_id), str(raw_value)))
    return hits


def _registry_corpus(conn, *, org_id: str, project_id: str, registry_id: str):
    """The registry's identities shaped for ``rank_candidates``.

    The SAME read ``tracked_entities.list_entities`` composes for the
    organization master, scoped to ONE client registry: live nodes, their
    current version (the preferred label) and their non-retired aliases --
    negative ones included, because a refusal must keep refusing.
    """

    nodes = list_nodes(conn, project_id=project_id, registry_id=registry_id)
    if not nodes:
        return []
    node_ids = [str(node["id"]) for node in nodes]
    versions = current_node_versions(
        conn, org_id=org_id, registry_id=registry_id, node_ids=node_ids
    )
    aliases: dict[str, list[dict[str, Any]]] = {}
    for alias in list_aliases(conn, org_id=org_id, node_ids=node_ids):
        aliases.setdefault(str(alias["node_id"]), []).append(alias)
    return build_corpus(
        [
            {
                "entity_id": node["id"],
                "label": node["label"],
                "current_version": versions.get(str(node["id"])),
                "aliases": aliases.get(str(node["id"]), []),
            }
            for node in nodes
        ]
    )


# ---------------------------------------------------------------------------
# The persistence. Append-only with supersession; idempotent on the occurrence.
# ---------------------------------------------------------------------------


def _fetch_current(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    field_id: str,
    raw_value_hash: str,
    observed_from: date | str,
    observed_to: date | str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_VERDICT_COLUMNS)}
              FROM app.entity_key_match_verdicts
             WHERE project_id = %s AND datastream_id = %s AND field_id = %s
               AND raw_value_hash = %s AND observed_from = %s AND observed_to = %s
               AND state = 'current'
            """,
            (
                project_id,
                datastream_id,
                field_id,
                raw_value_hash,
                observed_from,
                observed_to,
            ),
        )
        row = cur.fetchone()
    return None if row is None else dict(zip(_VERDICT_COLUMNS, row, strict=False))


def _insert_verdict(
    conn,
    *,
    verdict: Verdict,
    occurrence: Mapping[str, Any],
    actor: str,
    supersedes_id: str | None,
) -> dict[str, Any] | None:
    evidence = {"resolver": verdict.features.get("resolver", "rank_candidates")}
    evidence.update({k: v for k, v in verdict.features.items() if k != "resolver"})
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.entity_key_match_verdicts
                (id, org_id, project_id, datastream_id, execution_id, mapping_version_id,
                 registry_id, object_kind, field_id, raw_value_hash, normalized_value,
                 occurrence_count, observed_from, observed_to, verdict, relation, node_id,
                 candidates, confidence, reason_code, evidence, state, supersedes_id,
                 created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING {", ".join(_VERDICT_COLUMNS)}
            """,
            (
                _mint("ekmv"),
                occurrence["org_id"],
                occurrence["project_id"],
                occurrence["datastream_id"],
                occurrence["execution_id"],
                occurrence["mapping_version_id"],
                occurrence["registry_id"],
                occurrence["object_kind"],
                occurrence["field_id"],
                occurrence["raw_value_hash"],
                occurrence["normalized_value"],
                occurrence["occurrence_count"],
                occurrence["observed_from"],
                occurrence["observed_to"],
                verdict.verdict,
                verdict.relation,
                verdict.node_id,
                json.dumps([dict(c) for c in verdict.candidates]),
                verdict.confidence,
                verdict.reason_code,
                json.dumps(evidence),
                STATE_CURRENT,
                supersedes_id,
                _required(actor, "actor"),
            ),
        )
        row = cur.fetchone()
    return None if row is None else dict(zip(_VERDICT_COLUMNS, row, strict=False))


def _persist_verdict(
    conn,
    *,
    verdict: Verdict,
    occurrence: Mapping[str, Any],
    actor: str,
) -> str:
    """Write the verdict for one occurrence. Returns what happened.

    * ``written``    -- no current row existed, one now does;
    * ``replayed``   -- the current row IS this verdict: nothing is written,
      and the coverage fraction does not inflate (AC2);
    * ``superseded`` -- the occurrence resolves DIFFERENTLY now: the old row
      walks to ``superseded`` and the new one points back at it, in ONE
      statement pair on the caller's transaction -- the order
      ``tracked_entities._supersede_with`` states, because the live unique
      index covers ``current`` and the previous row must leave it first.
    """

    read_args = {
        "project_id": occurrence["project_id"],
        "datastream_id": occurrence["datastream_id"],
        "field_id": occurrence["field_id"],
        "raw_value_hash": occurrence["raw_value_hash"],
        "observed_from": occurrence["observed_from"],
        "observed_to": occurrence["observed_to"],
    }
    existing = _fetch_current(conn, **read_args)
    if existing is None:
        row = _insert_verdict(conn, verdict=verdict, occurrence=occurrence, actor=actor,
                              supersedes_id=None)
        if row is not None:
            return "written"
        # Lost a race: another transaction wrote this occurrence first. Re-read
        # and treat it as what it is -- a replay or a genuine divergence.
        existing = _fetch_current(conn, **read_args)
        if existing is None:  # pragma: no cover - only under a concurrent delete
            raise MasterDataConflict("verdict could not be written or read back")
    if same_outcome(existing, verdict):
        return "replayed"

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.entity_key_match_verdicts
               SET state = 'superseded'
             WHERE id = %s AND state = 'current'
            """,
            (existing["id"],),
        )
        if cur.rowcount != 1:
            raise MasterDataConflict(
                "the verdict changed while it was being superseded; re-read and resolve again"
            )
    row = _insert_verdict(
        conn,
        verdict=verdict,
        occurrence=occurrence,
        actor=actor,
        supersedes_id=str(existing["id"]),
    )
    if row is None:  # pragma: no cover - the live index was just freed above
        raise MasterDataConflict("the superseding verdict could not be written")
    return "superseded"


# ---------------------------------------------------------------------------
# The driver. Runs at landing, beside record_observations_for_pull (AC4).
# ---------------------------------------------------------------------------


def record_verdicts_for_pull(
    conn,
    *,
    datastream: Mapping[str, Any],
    project_id: str,
    pull_id: str,
    date_from: date | str,
    date_to: date | str,
    actor: str,
    connection_ref_id: str | None = None,
    org_id: str | None = None,
) -> int:
    """Resolve every designated value a landed pull observed, with a verdict.

    THE SEAM IS `_finish_job`'s success path for the reason its neighbours
    state: it is the only place that knows the datastream, the project AND that
    the run succeeded. The designated columns are READ from the pinned mapping
    (Story 68.2's declaration), the values from the rows this pull actually
    landed (``verification.distinct_raw_values``, AI-302 multi-relation
    handled) -- never guessed, and a Datastream that designates nothing writes
    nothing.

    Unmatched stays unmatched: this driver resolves occurrences to EXISTING
    nodes and never auto-creates one. Node creation is the registry's feeding
    path, and an invented node would be the approximate join wearing a
    governance hat.

    Returns the number of verdict rows appended (new + superseding). The caller
    owns the transaction; the audit row commits with the verdicts.
    """

    from core.verification import distinct_raw_values  # noqa: PLC0415

    project_id = _required(project_id, "project_id")
    datastream_id = _required(str(datastream.get("id") or ""), "datastream_id")
    actor = _required(actor, "actor")

    pinned = _pinned_designations(conn, project_id=project_id, datastream_id=datastream_id)
    if pinned is None:
        return 0
    mapping_version_id, designations = pinned
    kind_by_field = {field_id: kind for field_id, kind in designations}

    observed = distinct_raw_values(
        pull_id,
        sorted(kind_by_field),
        provider=datastream.get("module_name"),
        project_id=project_id,
        report_profile_id=datastream.get("report_profile_id"),
    )
    if not observed:
        return 0

    # Resolved HERE rather than threaded through the caller, for the reason
    # `entity_bindings.record_observations_for_pull` states: a verdict written
    # under the wrong org would be readable by the wrong tenant.
    if not org_id:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
        if not row or not row[0]:
            logger.warning(
                "entity_key_matching: no org for project=%s -- verdicts skipped", project_id
            )
            return 0
        org_id = str(row[0])

    registries = {
        kind: fetch_registry(conn, project_id=project_id, object_kind=kind)
        for kind in sorted(set(kind_by_field.values()))
    }

    corpora: dict[str, Any] = {}
    written = 0
    replayed = 0
    superseded = 0
    skipped_unstorable = 0
    by_verdict = {verdict: 0 for verdict in VERDICTS}

    for field_id, values in sorted(observed.items()):
        kind = kind_by_field.get(field_id)
        if kind is None:
            # The pull landed a field the pinned mapping no longer designates:
            # nothing to resolve it against, and "no verdict" is the truth.
            continue
        registry = registries.get(kind)
        registry_id = str(registry["id"]) if registry else None
        usable: dict[str, int] = {}
        for raw_value, occurrences in values.items():
            normalized = normalize_alias_value(raw_value)
            # The stored columns bound the value (1..400 after normalization):
            # a value outside them cannot carry a verdict row, and silently
            # truncating one would resolve a DIFFERENT string than the source
            # sent. Skipped and counted, never folded into a neighbour.
            if not normalized or len(normalized) > 400:
                skipped_unstorable += 1
                continue
            usable[str(raw_value)] = int(occurrences)
        if not usable:
            continue

        type_available = registry is not None and str(
            registry.get("lifecycle_state") or ""
        ) != "disabled"
        hits: dict[str, list[tuple[str, str]]] = {}
        if type_available:
            hits = _exact_alias_hits(
                conn,
                org_id=org_id,
                project_id=project_id,
                registry_id=registry_id or "",
                normalized_values=[normalize_alias_value(v) for v in usable],
            )

        for raw_value in sorted(usable):
            normalized = normalize_alias_value(raw_value)
            if not type_available:
                verdict = Verdict(
                    verdict=VERDICT_UNMATCHED,
                    relation=RELATION_NONE,
                    reason_code=REASON_TYPE_UNAVAILABLE,
                    features={"resolver": "none", "object_kind": kind},
                )
            elif hits.get(normalized):
                verdict = verdict_from_exact_hits(hits[normalized])
            else:
                if registry_id not in corpora:
                    corpora[registry_id or ""] = _registry_corpus(
                        conn,
                        org_id=org_id,
                        project_id=project_id,
                        registry_id=registry_id or "",
                    )
                verdict = verdict_from_proposal(
                    rank_candidates(raw_value, corpora[registry_id or ""])
                )
            by_verdict[verdict.verdict] += 1
            outcome = _persist_verdict(
                conn,
                verdict=verdict,
                occurrence={
                    "org_id": org_id,
                    "project_id": project_id,
                    "datastream_id": datastream_id,
                    "execution_id": pull_id,
                    "mapping_version_id": mapping_version_id,
                    "registry_id": registry_id,
                    "object_kind": kind,
                    "field_id": field_id,
                    "raw_value_hash": hash_value(raw_value),
                    "normalized_value": normalized,
                    "occurrence_count": usable[raw_value],
                    "observed_from": date_from,
                    "observed_to": date_to,
                },
                actor=actor,
            )
            if outcome == "written":
                written += 1
            elif outcome == "superseded":
                superseded += 1
            else:
                replayed += 1

    if written or superseded:
        insert_audit_row(
            conn,
            identity=actor,
            action=ACTION_ENTITY_KEY_MATCHED,
            provider_account="",
            connection_ref=connection_ref_id or "",
            metadata={
                "project_id": project_id,
                "datastream_id": datastream_id,
                "pull_id": pull_id,
                "mapping_version_id": mapping_version_id,
                "written": written,
                "superseded": superseded,
                "replayed": replayed,
                "skipped_unstorable": skipped_unstorable,
                "by_verdict": {state: n for state, n in by_verdict.items() if n},
            },
        )
    return written + superseded


__all__ = [
    "ACTION_ENTITY_KEY_MATCHED",
    "REASON_EXACT_LOOKUP",
    "REASON_NO_CANDIDATE",
    "REASON_TYPE_UNAVAILABLE",
    "STATE_CURRENT",
    "STATE_SUPERSEDED",
    "VERDICT_AMBIGUOUS",
    "VERDICT_RESOLVED",
    "VERDICT_UNMATCHED",
    "VERDICTS",
    "Candidate",
    "Verdict",
    "compose_coverage",
    "matching_coverage",
    "record_verdicts_for_pull",
    "same_outcome",
    "unavailable_coverage",
    "verdict_from_exact_hits",
    "verdict_from_proposal",
]
