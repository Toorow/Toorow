"""WHICH Datastream names the values of a dimension -- the matching context.

An unresolved set cannot be computed without it. `unresolved_values` asks "is this
value known?" and that question has no meaning until something says WHERE the
answer lives: the daily rows carry the id, another stream carries what the id
means, and the join between the two IS the logic. Jean, 2026-08-12.

NOTHING NEW IS DECLARED HERE, AND THAT IS THE POINT. Both halves of the join are
already in the database, written by a person during setup:

  * every mapping version binds a source field to a CANONICAL TARGET
    (`fields[].binding.canonical_target`), so two Datastreams that bind a field to
    the same target are talking about the same thing -- one calls it `video`, the
    other `video_id`, and the canonical target is what makes them one column;
  * `app.datastreams.data_role` says what a stream is FOR, out of the seven values
    of `datastreams.DATA_ROLES`. `Reference & targets` is the role of the stream
    that NAMES things, as opposed to the ones that measure them.

So the reference is DERIVED, never stored twice: a value derivable is not a value
to keep (CLAUDE.md). The alternative -- a table binding dimension to reference
stream -- would be a second answer to a question the mapping already answers, and
the day the two disagreed nobody could say which one the reading used.

WHAT IT REFUSES TO GUESS. If no stream of the project binds that canonical target
with the reference role, the answer is `absent` with the gap NAMED -- never the
nearest stream that happens to carry a similar column. Measured on the deployment
on 2026-08-12: a project whose performance flux carries 519 video ids has exactly
one stream with the reference role, `Sebastien Kardinal video catalogue`, and it
is ARCHIVED, IN DRAFT, with NO mapping version. The honest answer there is "the
catalogue that would name these values was never brought in", which is a sentence
a person can act on; "0 unresolved" would not have been.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

#: The `data_role` of a stream whose job is to NAME things rather than measure
#: them. One of the seven values of `core.datastreams.DATA_ROLES`; quoted here
#: rather than re-declared, and asserted against that tuple by the tests.
REFERENCE_ROLE = "Reference & targets"

#: Why no reference could be resolved. Each is a different sentence and a
#: different gesture, so they never collapse into one "not found".
GAP_NO_CANDIDATE = "no_stream_binds_this_dimension"
GAP_ROLE_ABSENT = "no_stream_carries_the_reference_role"
GAP_NOT_MAPPED = "the_reference_stream_has_no_mapping_version"
GAP_ARCHIVED = "the_reference_stream_is_archived"

MESSAGES = {
    GAP_NO_CANDIDATE: (
        "No Datastream of this Project binds a field to this dimension, so nothing "
        "can say what its values mean. Map a field to it on the stream that carries "
        "the catalogue."
    ),
    GAP_ROLE_ABSENT: (
        "Datastreams bind this dimension, but none of them is declared as a "
        "reference. Set the role of the stream that names these values to "
        f"'{REFERENCE_ROLE}'."
    ),
    GAP_NOT_MAPPED: (
        "The reference Datastream of this dimension carries no mapping version, so "
        "none of its columns is bound yet. Finish its mapping."
    ),
    GAP_ARCHIVED: (
        "The only Datastream that would name these values is archived. Restore it, "
        "or declare another stream as the reference."
    ),
}


@dataclass(frozen=True, slots=True)
class ReferenceCandidate:
    """One stream that could name a dimension, and how it is addressed."""

    datastream_id: str
    name: str
    data_role: str | None
    #: The stream's OWN name for the column -- `video_id` where the facts say
    #: `video`. Both bind the same canonical target; neither is renamed.
    source_field: str
    relation: str | None
    archived: bool
    mapped: bool

    @property
    def is_reference(self) -> bool:
        return self.data_role == REFERENCE_ROLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "datastream_id": self.datastream_id,
            "name": self.name,
            "data_role": self.data_role,
            "source_field": self.source_field,
            "relation": self.relation,
            "archived": self.archived,
            "mapped": self.mapped,
        }


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes)):
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def fields_binding(payload: Any, canonical_dimension: str) -> list[str]:
    """PURE: the source fields this mapping payload binds to *canonical_dimension*.

    Only CONFIRMED bindings count. A suggestion is what the product proposed, not
    what a person agreed to, and a reference resolved through an unconfirmed
    binding would name values on the strength of a guess.
    """
    wanted = str(canonical_dimension or "").strip()
    if not wanted:
        return []
    found: list[str] = []
    for field in _payload(payload).get("fields") or []:
        if not isinstance(field, Mapping):
            continue
        binding = field.get("binding")
        binding = binding if isinstance(binding, Mapping) else {}
        if str(binding.get("status") or "") != "confirmed":
            continue
        if str(binding.get("canonical_target") or "") != wanted:
            continue
        source_field = str(field.get("field_id") or "").strip()
        if source_field:
            found.append(source_field)
    return found


def select_reference(
    rows: Sequence[Mapping[str, Any]], *, canonical_dimension: str
) -> dict[str, Any]:
    """PURE: choose the stream that names *canonical_dimension*, or name the gap.

    ``rows`` are the project's Datastreams, each carrying ``id``, ``name``,
    ``data_role``, ``archived``, ``relation`` and ``mapping_payload``.

    The order of the refusals is the order a person repairs them: nothing binds
    the dimension at all, then nothing carries the reference role, then the
    reference exists but is archived or unmapped. Each answer names ONE gesture.
    """
    candidates: list[ReferenceCandidate] = []
    for row in rows:
        payload = row.get("mapping_payload")
        for source_field in fields_binding(payload, canonical_dimension):
            candidates.append(
                ReferenceCandidate(
                    datastream_id=str(row.get("id") or ""),
                    name=str(row.get("name") or ""),
                    data_role=(row.get("data_role") or None),
                    source_field=source_field,
                    relation=(row.get("relation") or None),
                    archived=bool(row.get("archived")),
                    mapped=bool(payload),
                )
            )
    answer: dict[str, Any] = {
        "dimension": canonical_dimension,
        "candidates": [item.as_dict() for item in candidates],
    }
    if not candidates:
        return {**answer, "state": "absent", "gap": GAP_NO_CANDIDATE,
                "message": MESSAGES[GAP_NO_CANDIDATE], "reference": None}

    references = [item for item in candidates if item.is_reference]
    if not references:
        return {**answer, "state": "absent", "gap": GAP_ROLE_ABSENT,
                "message": MESSAGES[GAP_ROLE_ABSENT], "reference": None}

    live = [item for item in references if not item.archived and item.mapped]
    if not live:
        gap = GAP_ARCHIVED if all(item.archived for item in references) else GAP_NOT_MAPPED
        return {**answer, "state": "absent", "gap": gap, "message": MESSAGES[gap],
                "reference": references[0].as_dict()}

    return {**answer, "state": "declared", "gap": None, "message": None,
            "reference": live[0].as_dict()}


_SQL = """
SELECT d.id, d.name, d.data_role, (d.archived_at IS NOT NULL) AS archived,
       mv.mapping_payload
  FROM app.datastreams d
  LEFT JOIN app.datastream_mapping_versions mv
         ON mv.id = d.current_mapping_version_id
 WHERE d.project_id = %s
"""


def read_reference(conn, *, project_id: str, canonical_dimension: str) -> dict[str, Any]:
    """The reference stream of one dimension of one project, or the named gap.

    Never raises: an unreadable catalogue answers `absent` with the gap of the
    thing that could not be read, because a caller that received an exception here
    would either crash a nightly sweep or swallow it into "nothing is unresolved".
    """
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL, (project_id,))
            columns = [description[0] for description in cur.description]
            rows = [dict(zip(columns, record)) for record in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dimension_reference: catalogue unreadable project=%s: %s", project_id, exc
        )
        return {
            "dimension": canonical_dimension,
            "state": "absent",
            "gap": GAP_NO_CANDIDATE,
            "message": MESSAGES[GAP_NO_CANDIDATE],
            "candidates": [],
            "reference": None,
        }
    return select_reference(rows, canonical_dimension=canonical_dimension)
