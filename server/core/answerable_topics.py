"""Story 52.1 -- the Answerable Topic catalog, resolved per Project.

ONE READER. `resolve_catalog` is the single path from which every surface reads
the catalog of business questions a Project answers: the MCP tool
`list_card_templates`, the REST route `GET /api/cards/templates`, and the agent
catalogue in `daily_insights_card_contract`. Before this module each of the three
iterated `cards.CARD_TEMPLATES` directly, which is why the catalog could not be
anything but platform-wide.

DEFAULTS YES, HARDCODES NO (glossary.md:223-224). `cards.CARD_TEMPLATES` stays,
and stays authoritative -- as the DEFAULT SET. A Project that has configured
nothing resolves to exactly those entries, field for field and in order; that
equivalence is a test, not a claim (`test_answerable_topics.py`). A Project that
has configured something gets its own catalog LAYERED over the defaults:

    default set  ->  stored entry with the same topic_key REPLACES it
                 ->  stored entry with a new topic_key is APPENDED
                 ->  stored entry marked `retired` is REMOVED from the catalog
                     and its row is kept (rule 9: the trace of what a Project
                     stopped answering is inventory, not litter)

Layering rather than materializing is what lets a Project reword one topic
without inheriting a frozen copy of the other eight -- and it is why no migration
seeds nine rows per Project.

RENDERING IS NOT REDEFINED HERE. A stored topic names an existing registered
composition through `base_template_id`, and `cards.get_card` maps its key back to
that base id from the catalog it has already resolved -- one read, not two. (An
earlier `resolve_alias` helper here did the same thing with a second read and
ended up called by nothing but its own tests; it was removed rather than left as
decor. `get_topic` was removed in the same spirit at epic review: it re-derived
one head by scanning `_fetch_stored`, and had no caller in production or in a
test -- an exported function nobody calls is decor that reads like a contract.)
Authoring a
composition without a deploy is Story 52.4's contract, and this module must not
pretend to offer it. Query bindings are Story 52.2; knowledge bindings are Story
52.3, whose design question is deliberately open; Semantic View bindings -- which
Views a topic reads and which governed join paths it may cross -- are Story 75-5,
and they mirror the query family in shape, refusals and audit rather than
inventing a fourth way to pin a version.

FAIL-SOFT ON READ, FAIL-CLOSED ON WRITE -- AND THE DEGRADATION IS NEVER MUTE. A
catalog read with no database, or against a schema where migration 170 has not
been applied, still returns the default set: the catalog is how an agent
discovers what it can ask, and answering "nothing" because Postgres blinked would
be worse than answering "the defaults". But serving the defaults SILENTLY is a
different defect, and it was this module's own: a Project that had retired six of
the nine questions was told it answers all nine, in the same shape as a Project
that had configured nothing. `resolve_catalog_with_reason` therefore returns the
value AND the reason, exactly as `verified_query_pairs` does for the query store,
and no caller may present a degraded catalog as "this Project's catalog".
A WRITE never degrades: it goes through `core.operations.execute_operation`, so
the audit row and the outbox entry commit in the same transaction as the change.

A NOTE ON `request_payload` KEYS. The governed operation payload names the topic
`topic`, not `topic_key`: `operations._is_secret_key` refuses any key ending in
`key`, and it is right to -- that heuristic is what keeps raw secrets out of the
audit ledger. Named `topic_key`, every write in this module was refused by
`prepare_operation` before it touched a row. The key is still recorded, in
`resource_path`, where it belongs.

ASCII-only stdout (AI-03).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from ulid import ULID

logger = logging.getLogger(__name__)

#: Entry provenance. `default` = derived from the platform default set (a reword
#: or a retirement of one of the nine); `project` = a question this Project
#: invented. Same doctrine as migration 152: a default is born a `default`.
ORIGIN_DEFAULT = "default"
ORIGIN_PROJECT = "project"

LIFECYCLE_ACTIVE = "active"
LIFECYCLE_RETIRED = "retired"

#: The keys a stored version pins, in the order `CardTemplate.to_catalog_entry`
#: emits them. Used to rebuild a catalog entry without a second serializer.
_VERSION_FIELDS = (
    "title",
    "answers_question",
    "kind",
    "fallback_rank",
    "required_metrics",
    "required_dimensions",
    "optional_metrics",
    "optional_dimensions",
)


class AnswerableTopicNotFound(Exception):
    """The topic does not exist in this Project -- foreign, denied and absent alike."""


class AnswerableTopicRefused(Exception):
    """The write was refused for a stated reason, before anything was stored."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(payload: dict[str, Any]) -> str:
    """Stable hash of a version's business content (never its id or timestamps)."""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The default set -- read from the registry, never copied.
# ---------------------------------------------------------------------------


def default_catalog() -> list[dict]:
    """The platform default set, serialized exactly as the registry serializes it."""
    from core import cards as _cards  # noqa: PLC0415

    return [tpl.to_catalog_entry() for tpl in _cards.CARD_TEMPLATES]


def _default_entry(template_id: str) -> dict | None:
    for entry in default_catalog():
        if entry["id"] == template_id:
            return entry
    return None


# ---------------------------------------------------------------------------
# Reads.
# ---------------------------------------------------------------------------


def _org_for_project(cur, project_id: str) -> str | None:
    cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    return str(row[0]) if row else None


def _fetch_stored(conn, project_id: str, org_id: str | None) -> list[dict]:
    """Return the Project's stored heads with their current version, oldest first."""
    with conn.cursor() as cur:
        if org_id is None:
            org_id = _org_for_project(cur, project_id)
            if org_id is None:
                return []
        cur.execute(
            """
            SELECT t.topic_key, t.lifecycle_state, t.base_template_id, t.origin,
                   v.id, v.version_number, v.title, v.answers_question, v.kind,
                   v.fallback_rank, v.required_metrics, v.required_dimensions,
                   v.optional_metrics, v.optional_dimensions, t.id,
                   t.requires_knowledge
            FROM app.answerable_topics t
            LEFT JOIN app.answerable_topic_versions v ON v.id = t.current_version_id
            WHERE t.org_id = %s AND t.project_id = %s
            ORDER BY t.created_at, t.id
            """,
            (org_id, project_id),
        )
        rows = cur.fetchall() or []
    stored: list[dict] = []
    for row in rows:
        stored.append(
            {
                # Story 52.2 appends `topic_id` (row[14]) rather than reshaping the
                # tuple: a binding needs the head's id, and renumbering the existing
                # positions would break every reader silently.
                "topic_id": row[14] if len(row) > 14 else None,
                # Read back so the editor can SHOW what the topic declared. A form
                # that cannot display the current value teaches the operator that
                # saving resets it.
                "requires_knowledge": bool(row[15]) if len(row) > 15 else False,
                "topic_key": row[0],
                "lifecycle_state": row[1],
                "base_template_id": row[2],
                "origin": row[3],
                "version_id": row[4],
                "version_number": row[5],
                "title": row[6],
                "answers_question": row[7],
                "kind": row[8],
                "fallback_rank": row[9],
                "required_metrics": list(row[10] or []),
                "required_dimensions": list(row[11] or []),
                "optional_metrics": list(row[12] or []),
                "optional_dimensions": list(row[13] or []),
            }
        )
    return stored


def _entry_from_stored(stored: dict) -> dict | None:
    """Build a catalog entry from a stored head+version, inheriting its base card.

    The base template supplies what only a built bundle can supply -- the widget
    URI and the ordered composition. Everything a Project can decide (wording,
    kind, rank, declared inputs) comes from the stored version.
    """
    base = _default_entry(stored["base_template_id"])
    if base is None:
        # The base card was removed from the registry after the topic was stored.
        # Dropping the entry is the honest outcome: offering a question whose
        # renderer no longer exists would be a card that cannot render.
        logger.warning(
            "answerable_topics: unknown base template %r for topic %r -- entry withheld",
            stored["base_template_id"],
            stored["topic_key"],
        )
        return None
    if stored["version_id"] is None:
        return None
    entry = dict(base)
    entry["id"] = stored["topic_key"]
    for field in _VERSION_FIELDS:
        entry[field] = stored[field]
    # Provenance, carried only on stored entries so an inherited default stays
    # byte-identical to what the registry emits.
    entry["origin"] = stored["origin"]
    entry["base_template_id"] = stored["base_template_id"]
    entry["version_id"] = stored["version_id"]
    entry["version_number"] = stored["version_number"]
    entry["requires_knowledge"] = stored["requires_knowledge"]
    return entry


# ---------------------------------------------------------------------------
# THE CATALOG'S OWN ABSENCES. Story 52.2 built three reason codes so that "this
# project bound no query" could never be confused with "the store could not be
# read". The catalog -- the ROOT object of the epic -- had none, and served the
# nine platform defaults for all three of: nothing is configured, the store is
# unreadable, the caller may not read this Project. The value served is still the
# defaults (a discovery surface that answers "nothing" is worse), but it now
# arrives WITH the fact that it is a fallback.
# ---------------------------------------------------------------------------

#: No Project was named, so the platform default set IS the answer -- not a
#: degradation, and not a claim about any Project.
CATALOG_NO_PROJECT_REASON = "no_project_named"
#: The stored catalog could not be read. The defaults are a fallback VALUE and
#: must never be presented as what this Project answers: a question it retired is
#: in there, and a rewording it authored is not.
CATALOG_UNAVAILABLE_REASON = "answerable_topic_catalog_unavailable"
#: The caller may not read this Project. Same fallback value, different fact --
#: and the only one of the three that is about the CALLER.
CATALOG_ACCESS_DENIED_REASON = "project_not_readable"


def _layer(defaults: list[dict], stored: list[dict]) -> list[dict]:
    """Pure layering of stored heads over the default set. No I/O, so the rule is
    testable and the two readers below cannot drift apart."""
    by_key = {s["topic_key"]: s for s in stored}
    catalog: list[dict] = []
    for entry in defaults:
        override = by_key.pop(entry["id"], None)
        if override is None:
            catalog.append(entry)
            continue
        if override["lifecycle_state"] == LIFECYCLE_RETIRED:
            continue
        replacement = _entry_from_stored(override)
        catalog.append(replacement if replacement is not None else entry)
    for remaining in by_key.values():
        if remaining["lifecycle_state"] == LIFECYCLE_RETIRED:
            continue
        added = _entry_from_stored(remaining)
        if added is not None:
            catalog.append(added)
    return catalog


def resolve_catalog_with_reason(project_id: str, conn=None) -> tuple[list[dict], str | None]:
    """Return ``(catalog, reason_code)`` -- the catalog AND whether it is this Project's.

    `reason_code` is None when the returned list IS the Project's resolved catalog
    (which includes the honest case of a Project that configured nothing: zero
    stored rows resolve to exactly `default_catalog()`, in registry order).
    Otherwise the list is the platform default set served as a fallback, and the
    code says which fact produced it -- `CATALOG_NO_PROJECT_REASON` or
    `CATALOG_UNAVAILABLE_REASON`. `CATALOG_ACCESS_DENIED_REASON` is produced by the
    caller that owns the scope check, not here: this module never decides access.
    """
    defaults = default_catalog()
    if not project_id or conn is None:
        return defaults, CATALOG_NO_PROJECT_REASON
    try:
        stored = _fetch_stored(conn, project_id, None)
    except Exception as exc:  # noqa: BLE001 -- the value degrades, the fact does not
        logger.debug("answerable_topics: stored_read_skipped: %s", exc)
        return defaults, CATALOG_UNAVAILABLE_REASON
    if not stored:
        return defaults, None
    return _layer(defaults, stored), None


def resolve_catalog(project_id: str, conn=None) -> list[dict]:
    """The catalog VALUE alone, for a caller that has already stated the reason.

    Kept as the narrow shape because the entry list is what most call sites need,
    and because `cards.py` / `cards_api.py` already read it. A surface that PRESENTS
    the result to a person or to a model must call `resolve_catalog_with_reason`
    instead: presenting a fallback as the Project's catalog is the defect this pair
    of functions exists to make impossible to write by accident.
    """
    return resolve_catalog_with_reason(project_id, conn)[0]


# ---------------------------------------------------------------------------
# Story 52.2 -- the governed queries that answer a topic.
#
# WHAT THE THREE REASON CODES SEPARATE, and why it is the point of the story.
# `metric_verified_queries` used to answer one empty list for every situation.
# Three different facts hid behind it:
#
#   * nobody built the store            -> a fact about US   (closed by 52.2)
#   * this project bound no query       -> a fact about the PROJECT
#   * the store could not be read       -> a fact about the RUN
#
# A reader that cannot tell them apart cannot act on any of them, so the read
# path below returns the reason with the emptiness, never the emptiness alone.
# ---------------------------------------------------------------------------

#: This project has authored no binding yet. Not an error, and not our absence.
NO_BINDING_REASON = "project_has_no_bound_query"
#: The bindings could not be read. NEVER reported as "there are none".
UNAVAILABLE_REASON = "governed_verified_query_store_unavailable"

#: Version pins a caller might try to store as an intent. Refused here AND by the
#: CHECK in migration 172 -- a reference that follows `latest` silently changes
#: what it means, which is the whole reason a pin exists.
_FORBIDDEN_PINS = ("latest", "current")


def _fetch_bindings(conn, project_id: str, org_id: str | None) -> list[dict]:
    """Return this Project's query bindings, ordered by topic then position."""
    with conn.cursor() as cur:
        if org_id is None:
            org_id = _org_for_project(cur, project_id)
            if org_id is None:
                return []
        cur.execute(
            """
            SELECT b.id, t.topic_key, b.role, b.position, b.query_spec_id,
                   b.query_spec_version_id, v.version_number, v.semantic_view_id,
                   v.semantic_view_version_id, t.lifecycle_state
            FROM app.answerable_topic_query_bindings b
            JOIN app.answerable_topics t
              ON t.id = b.answerable_topic_id
             AND t.org_id = b.org_id AND t.project_id = b.project_id
            LEFT JOIN app.query_spec_versions v
              ON v.id = b.query_spec_version_id
             AND v.org_id = b.org_id AND v.project_id = b.project_id
            WHERE b.org_id = %s AND b.project_id = %s
            ORDER BY t.topic_key, b.position
            """,
            (org_id, project_id),
        )
        rows = cur.fetchall() or []
    return [
        {
            "binding_id": row[0],
            "topic_key": row[1],
            "role": row[2],
            "position": row[3],
            "query_spec_id": row[4],
            "query_spec_version_id": row[5],
            "query_spec_version_number": row[6],
            "semantic_view_id": row[7],
            "semantic_view_version_id": row[8],
            "topic_lifecycle_state": row[9],
        }
        for row in rows
    ]


def resolve_bindings(project_id: str, conn=None, topic_key: str | None = None) -> list[dict]:
    """The governed queries this Project bound, optionally for one topic.

    Raises nothing: an unreadable store is the caller's business to report, so it
    is surfaced through `verified_query_pairs` rather than swallowed here.
    """
    if not project_id or conn is None:
        return []
    bindings = _fetch_bindings(conn, project_id, None)
    # A retired topic keeps its rows -- deleting them would destroy the record of
    # what used to answer the question -- but it answers nothing, so its bindings
    # are not part of what the project can be asked.
    bindings = [b for b in bindings if b["topic_lifecycle_state"] != LIFECYCLE_RETIRED]
    if topic_key is not None:
        bindings = [b for b in bindings if b["topic_key"] == topic_key]
    return bindings


def verified_query_pairs(project_id: str, conn=None) -> tuple[list[dict], str | None]:
    """Return ``(pairs, reason_code)`` -- the question <-> governed query pairing.

    The pair shape is the one `metric_semantics_mcp._filter_verified_queries`
    already filters on, so the pure filter is untouched by this story:

      * ``surface`` is the TOPIC KEY -- the surface a question belongs to;
      * ``tags`` is ``[role]`` -- what the query does for that question.

    That mapping is stated here rather than guessed at the call site, because the
    previous source of these fields was an evaluation corpus and nothing recorded
    what they had meant.

    `reason_code` is None when there are pairs, `NO_BINDING_REASON` when the
    Project bound none, and `UNAVAILABLE_REASON` when the store could not be read.

    `NO_BINDING_REASON` IS RESERVED FOR ZERO ROWS, and that is a repair. It used to
    be emitted whenever the loop produced no pair -- including when the Project HAD
    bindings that were all dropped for want of a question. "This project has bound
    no governed query" is then a false statement about the PROJECT caused by a fact
    about US, which is the exact confusion the three codes were built to end.
    """
    if not project_id or conn is None:
        return [], UNAVAILABLE_REASON
    try:
        bindings = resolve_bindings(project_id, conn)
        # Read the stored heads ONCE and layer here, rather than calling
        # `resolve_catalog`: that function degrades an unreadable store to the
        # defaults, and swallowing the failure here would pair project bindings
        # against platform questions and then report "no binding".
        stored = _fetch_stored(conn, project_id, None)
    except Exception as exc:  # noqa: BLE001 -- a read failure is never "there are none"
        logger.debug("answerable_topics: bindings_read_failed: %s", exc)
        return [], UNAVAILABLE_REASON

    catalog = _layer(default_catalog(), stored)
    questions = {entry["id"]: entry["answers_question"] for entry in catalog}
    # A stored topic whose base card left the registry has NO catalog entry -- it
    # cannot render -- and it still has a question, in its own stored version. The
    # pair (question, governed query) is true and useful even when no card can draw
    # it, so the binding is emitted rather than silently dropped.
    for s in stored:
        if s["version_id"] is None or not s["answers_question"]:
            # No current version: the LEFT JOIN produced NULLs and there is no
            # wording to pair a query with. That is half a pair, and stays dropped.
            continue
        questions.setdefault(s["topic_key"], s["answers_question"])

    # Story 75-5: which Semantic Views the topic reads, and by which governed
    # join paths. Read ONCE for the whole project, beside the catalog, so a topic
    # with four Views does not become four round trips on the agent's path.
    views_by_topic = _views_by_topic(project_id, conn)

    pairs: list[dict] = []
    for binding in bindings:
        question = questions.get(binding["topic_key"])
        if not question:
            # No wording at all: the head exists with no version yet. Emitting the
            # pair would hand a model a query with no question attached.
            continue
        pair = {
            "id": binding["binding_id"],
            "question": question,
            "surface": binding["topic_key"],
            "tags": [binding["role"]],
            "topic_key": binding["topic_key"],
            "role": binding["role"],
            "position": binding["position"],
            "query_spec_id": binding["query_spec_id"],
            "query_spec_version_id": binding["query_spec_version_id"],
            "query_spec_version_number": binding["query_spec_version_number"],
            "semantic_view_id": binding["semantic_view_id"],
            "semantic_view_version_id": binding["semantic_view_version_id"],
        }
        # ADDITIVE, AND THAT IS A RULE. A topic that bound no Semantic View gets
        # NO `views` key -- not an empty list -- so its payload is exactly what it
        # was before story 75-5, byte for byte. A consumer that never asked about
        # Views cannot be made to read one.
        topic_views = views_by_topic.get(binding["topic_key"])
        if topic_views:
            pair["views"] = topic_views
        pairs.append(pair)
    if pairs:
        return pairs, None
    if bindings:
        # Rows exist and none of them could be turned into a pair. That is a fact
        # about the store's contents, not about the Project's intent, so it takes
        # the "could not be read" code rather than the "bound none" one -- whose
        # message reads "This project has bound no governed query", and would be a
        # lie here.
        logger.debug(
            "answerable_topics: %d binding(s) carry no resolvable question", len(bindings)
        )
        return [], UNAVAILABLE_REASON
    return [], NO_BINDING_REASON


def _public(result: dict[str, Any]) -> dict[str, Any]:
    """Rename the ledger's `topic` back to `topic_key` for the caller.

    `operations._is_secret_key` refuses any key ending in `key`, in the request
    payload AND in the mutation result -- both are persisted in the audit ledger,
    and that heuristic is what keeps raw secrets out of it. So the ledger says
    `topic` and the HTTP response says `topic_key`, which is the name its callers
    have always read. Two names, one hop, stated here rather than discovered by
    the next person through a 500.
    """
    if "topic" in result:
        result = dict(result)
        result["topic_key"] = result.pop("topic")
    return result


def bind_query(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    topic_key: str,
    query_spec_version_id: str,
    role: str,
    position: int | None = None,
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Bind one exact Query Spec version to a topic, with a role and a position."""
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        _canonical_hash,
        execute_operation,
    )

    key = _validate_key(topic_key)
    version_id = str(query_spec_version_id or "").strip()
    role_value = str(role or "").strip()
    if not version_id or version_id.lower() in _FORBIDDEN_PINS:
        raise AnswerableTopicRefused(
            "pin_is_not_exact",
            "query_spec_version_id must name an exact version; 'latest' is unstorable",
        )
    if not role_value or len(role_value) > 60:
        raise AnswerableTopicRefused("invalid_role", "role must be 1-60 characters")

    spec = _operation_spec(
        command_type="answerable_topic.query_bound",
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        topic_key=key,
        request_payload={
            "topic": key,
            "query_spec_version_id": version_id,
            "role": role_value,
            "position": position,
        },
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM app.answerable_topics
                WHERE org_id = %s AND project_id = %s AND topic_key = %s
                """,
                (org_id, project_id, key),
            )
            head = cur.fetchone()
            if head is None:
                raise AnswerableTopicNotFound("answerable topic not found in this Project")
            topic_id = str(head[0])

            # Scope is part of the lookup: a version belonging to another Project
            # simply does not resolve, and is refused exactly like an unknown one.
            cur.execute(
                """
                SELECT query_spec_id FROM app.query_spec_versions
                WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (version_id, org_id, project_id),
            )
            version = cur.fetchone()
            if version is None:
                raise AnswerableTopicRefused(
                    "unknown_query_spec_version",
                    f"{version_id!r} names no Query Spec version in this Project",
                )
            query_spec_id = str(version[0])

            slot = position
            if slot is None:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(position), 0) + 1
                    FROM app.answerable_topic_query_bindings
                    WHERE answerable_topic_id = %s AND project_id = %s
                    """,
                    (topic_id, project_id),
                )
                slot = int(cur.fetchone()[0])

            binding_id = f"atq_{ULID()}"
            cur.execute(
                """
                INSERT INTO app.answerable_topic_query_bindings
                    (id, answerable_topic_id, org_id, project_id, query_spec_id,
                     query_spec_version_id, role, position, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    binding_id,
                    topic_id,
                    org_id,
                    project_id,
                    query_spec_id,
                    version_id,
                    role_value,
                    slot,
                    actor,
                ),
            )
            if cur.rowcount != 1:
                # Either the slot is taken or this exact (version, role) is already
                # bound. Both are stated rather than silently absorbed: a caller
                # that believes it bound something must be told it did not.
                raise AnswerableTopicRefused(
                    "binding_conflict",
                    "that position, or that exact version and role, is already bound",
                )
        result = {
            "binding_id": binding_id,
            "topic": key,
            "query_spec_id": query_spec_id,
            "query_spec_version_id": version_id,
            "role": role_value,
            "position": slot,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"topic": key, "project_id": project_id,
                            "transition": "query_bound"},
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _public(dict(op_result.result or {}))


def unbind_query(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    binding_id: str,
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Remove one binding. The Query Spec version itself is never touched."""
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        _canonical_hash,
        execute_operation,
    )

    spec = _operation_spec(
        command_type="answerable_topic.query_unbound",
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        topic_key=str(binding_id),
        request_payload={"binding_id": binding_id},
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM app.answerable_topic_query_bindings
                WHERE id = %s AND org_id = %s AND project_id = %s
                RETURNING query_spec_version_id, role, position
                """,
                (binding_id, org_id, project_id),
            )
            row = cur.fetchone()
            if row is None:
                raise AnswerableTopicNotFound("binding not found in this Project")
        result = {
            "binding_id": binding_id,
            "query_spec_version_id": row[0],
            "role": row[1],
            "position": row[2],
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"binding_id": binding_id, "project_id": project_id,
                            "transition": "query_unbound"},
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _public(dict(op_result.result or {}))


# ---------------------------------------------------------------------------
# Story 52.3 -- the governed knowledge a topic may elaborate from.
#
# WHAT IT BINDS TO IS A SPIKE RESULT, NOT A PREFERENCE. Epic 52 declared this its
# only open design question. Measured against the Context Hub as built
# (2026-08-01): the `skills-registry` section renders `Procedures` and no
# `app.skills` table exists, so the three candidates the epic listed are TWO
# stores -- `app.context_topics` and `app.procedures`. And a knowledge version has
# no id of its own (primary keys are `(topic_id, version_number)` and
# `(procedure_id, version_number)`), so a pin is COMPOSITE.
#
# THREE STATES, NEVER TWO. `context-hub.md:59-60` makes it a completeness
# criterion that "an unavailable context store is indistinguishable from an empty
# one, so a model reads 'nothing is defined here' and answers from its own
# priors". So a topic that declared nothing and a topic whose declared knowledge
# cannot be read are different answers, and they are different values here.
# ---------------------------------------------------------------------------

#: The topic declared no knowledge. A fact about the topic.
NO_KNOWLEDGE_REASON = "no_knowledge_declared"
#: The topic declared knowledge and none of it could be read. A fact about the
#: store -- and the one that must never be rendered as "there is nothing to say".
CONTEXT_MISSING_REASON = "context_missing"

KNOWLEDGE_KIND_TOPIC = "topic"
KNOWLEDGE_KIND_PROCEDURE = "procedure"
_KNOWLEDGE_KINDS = (KNOWLEDGE_KIND_TOPIC, KNOWLEDGE_KIND_PROCEDURE)


def _fetch_knowledge_bindings(conn, project_id: str, org_id: str | None) -> list[dict]:
    """Return the knowledge pins of a Project, resolved AT THEIR EXACT VERSION.

    The join is on `(id, version_number)` -- never on the head, and never on the
    current version. Falling back to current would make a cited answer cite
    something the topic never declared, which is the whole reason a pin exists.
    A pin whose version row cannot be read comes back with `readable = False`
    rather than being dropped: a declaration that cannot be honoured is
    information, and silently omitting it is how "context missing" becomes
    indistinguishable from "nothing was declared".
    """
    with conn.cursor() as cur:
        if org_id is None:
            org_id = _org_for_project(cur, project_id)
            if org_id is None:
                return []
        cur.execute(
            """
            SELECT b.id, t.topic_key, b.knowledge_kind,
                   COALESCE(b.context_topic_id, b.procedure_id) AS knowledge_id,
                   COALESCE(b.context_topic_version, b.procedure_version) AS knowledge_version,
                   ctv.title, ct.id, pv.name, p.id, t.lifecycle_state,
                   t.requires_knowledge
            FROM app.answerable_topic_knowledge_bindings b
            JOIN app.answerable_topics t
              ON t.id = b.answerable_topic_id
             AND t.org_id = b.org_id AND t.project_id = b.project_id
            -- The project predicate is part of the JOIN, not an afterthought:
            -- migration 173's foreign keys prove a version EXISTS, and cannot prove
            -- WHOSE it is (`context_topics_versions` carries no `org_id`). Without
            -- this, another project's knowledge title reached `knowledge_citations`.
            --
            -- `IS NULL OR =`, not `=`: `project_id IS NULL` means PLATFORM scope,
            -- readable by every project (`context_store.py:425-431`). A first pass
            -- of this repair used strict equality and made platform knowledge
            -- unpinnable while reporting `context_missing` for it -- a fix that
            -- invented a second defect while closing the first.
            LEFT JOIN app.context_topics_versions ctv
              ON ctv.topic_id = b.context_topic_id
             AND ctv.version_number = b.context_topic_version
             AND (ctv.project_id IS NULL OR ctv.project_id = b.project_id)
            -- The HEAD too, and not for the title: a version row survives its head
            -- (versions are append-only, heads are deletable -- production holds 41
            -- of the first and 0 of the second). Without this join a pin to purged
            -- knowledge still resolved a title and was CITED, which is the one thing
            -- `context_missing` exists to prevent.
            LEFT JOIN app.context_topics ct
              ON ct.id = b.context_topic_id
             AND (ct.project_id IS NULL OR ct.project_id = b.project_id)
            LEFT JOIN app.procedures_versions pv
              ON pv.procedure_id = b.procedure_id
             AND pv.version_number = b.procedure_version
             AND (pv.project_id IS NULL OR pv.project_id = b.project_id)
            LEFT JOIN app.procedures p
              ON p.id = b.procedure_id
             AND (p.project_id IS NULL OR p.project_id = b.project_id)
            WHERE b.org_id = %s AND b.project_id = %s
            ORDER BY t.topic_key, b.knowledge_kind, b.created_at
            """,
            (org_id, project_id),
        )
        rows = cur.fetchall() or []
    pins: list[dict] = []
    for row in rows:
        kind = row[2]
        title = row[5] if kind == KNOWLEDGE_KIND_TOPIC else row[7]
        # The HEAD id, not a body: `readable` means "this knowledge still exists",
        # and a version whose head was purged is exactly what does not.
        head_id = row[6] if kind == KNOWLEDGE_KIND_TOPIC else row[8]
        pins.append(
            {
                "binding_id": row[0],
                "topic_key": row[1],
                "knowledge_kind": kind,
                "knowledge_id": row[3],
                "knowledge_version": row[4],
                "title": title,
                # D-7: `body_md` was selected, carried, and read by nobody. A
                # needless read of business content is also a surface it can leak
                # from, so it is not read at all.
                "readable": title is not None and head_id is not None,
                "topic_lifecycle_state": row[9],
                "requires_knowledge": bool(row[10]),
            }
        )
    return pins


def resolve_knowledge(project_id: str, conn=None, topic_key: str | None = None) -> list[dict]:
    """The knowledge pins of a Project, optionally for one topic."""
    if not project_id or conn is None:
        return []
    pins = _fetch_knowledge_bindings(conn, project_id, None)
    pins = [p for p in pins if p["topic_lifecycle_state"] != LIFECYCLE_RETIRED]
    if topic_key is not None:
        pins = [p for p in pins if p["topic_key"] == topic_key]
    return pins


def _topic_requires_knowledge(conn, project_id: str, topic_key: str) -> bool | None:
    """Read `requires_knowledge` from the HEAD, scoped to the Project.

    A SECOND READ, deliberately. The declaration lives on `app.answerable_topics`
    and reaches `knowledge_citations` only as a column carried by the pin rows --
    so the branch where the pin read FAILED has no way to know whether the topic
    demanded knowledge, which is precisely the branch AC5 exists for. Returns None
    when the head cannot be resolved at all; the caller decides what that means,
    and it decides "refuse".
    """
    with conn.cursor() as cur:
        org_id = _org_for_project(cur, project_id)
        if org_id is None:
            return None
        cur.execute(
            """
            SELECT requires_knowledge FROM app.answerable_topics
            WHERE org_id = %s AND project_id = %s AND topic_key = %s
            """,
            (org_id, project_id, topic_key),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else None


def knowledge_citations(
    project_id: str, topic_key: str, conn=None
) -> tuple[list[dict], str | None, bool, bool]:
    """Return ``(citations, status, refused)`` for one topic.

    `citations` names what was ACTUALLY read, at the version that was read -- it is
    never the list of what was declared, because citing something unread is the
    defect this whole story exists to prevent.

    `status` is None when at least one pin was read, `NO_KNOWLEDGE_REASON` when the
    topic declared none, and `CONTEXT_MISSING_REASON` when it declared some and
    none could be read.

    `refused` is True only when the topic declares `requires_knowledge` AND its
    context is missing: a refusal is a legitimate, declared answer to a topic --
    not a card rendered without the half that explains it.

    THE REFUSAL FAILS CLOSED. When the pin read raises, this used to answer
    `refused=False` -- so the one situation AC5 names ("when its pins cannot be
    read, the answer is a stated refusal") was the one situation in which no
    refusal happened. The flag is re-read from the head; if that read fails too,
    the answer is a refusal, because a declaration that cannot be verified is not a
    declaration that can be ignored.
    """
    if not project_id or conn is None:
        # We did not look. `NO_KNOWLEDGE_REASON` would assert an absence on the
        # strength of a missing argument -- the confusion `context-hub.md:59-60`
        # makes a completeness criterion. Nothing was read, so nothing is claimed
        # about the topic: the refusal is UNEVALUATED, not decided either way.
        return [], CONTEXT_MISSING_REASON, False, False
    try:
        pins = resolve_knowledge(project_id, conn, topic_key=topic_key)
    except Exception as exc:  # noqa: BLE001 -- unreadable store is context_missing
        logger.debug("answerable_topics: knowledge_read_failed: %s", exc)
        try:
            requires = _topic_requires_knowledge(conn, project_id, topic_key)
        except Exception as head_exc:  # noqa: BLE001 -- unverifiable declaration
            logger.debug("answerable_topics: requires_knowledge_read_failed: %s", head_exc)
            # The head could not be read either, so whether this topic refuses is
            # UNKNOWN. Asserting a refusal here would refuse cards that never asked
            # for one; asserting none would claim a check nobody ran.
            return [], CONTEXT_MISSING_REASON, False, False
        return [], CONTEXT_MISSING_REASON, bool(requires), True
    if not pins:
        return [], NO_KNOWLEDGE_REASON, False, True

    requires = any(p["requires_knowledge"] for p in pins)
    citations = [
        {
            "kind": p["knowledge_kind"],
            "id": p["knowledge_id"],
            "version": p["knowledge_version"],
            "title": p["title"],
        }
        for p in pins
        if p["readable"]
    ]
    if not citations:
        return [], CONTEXT_MISSING_REASON, requires, True
    return citations, None, False, True


def bind_knowledge(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    topic_key: str,
    knowledge_kind: str,
    knowledge_id: str,
    knowledge_version: int,
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Declare one governed knowledge version a topic may read."""
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        _canonical_hash,
        execute_operation,
    )

    key = _validate_key(topic_key)
    kind = str(knowledge_kind or "").strip()
    kid = str(knowledge_id or "").strip()
    if kind not in _KNOWLEDGE_KINDS:
        raise AnswerableTopicRefused(
            "invalid_knowledge_kind",
            f"knowledge_kind must be one of {_KNOWLEDGE_KINDS}",
        )
    try:
        version = int(knowledge_version)
    except (TypeError, ValueError) as exc:
        raise AnswerableTopicRefused(
            "pin_is_not_exact",
            "knowledge_version must be an exact version number",
        ) from exc
    if not kid or version < 1:
        raise AnswerableTopicRefused(
            "pin_is_not_exact",
            "a knowledge pin names an id AND an exact version number",
        )

    spec = _operation_spec(
        command_type="answerable_topic.knowledge_bound",
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        topic_key=key,
        request_payload={
            "topic": key,
            "knowledge_kind": kind,
            "knowledge_id": kid,
            "knowledge_version": version,
        },
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM app.answerable_topics
                WHERE org_id = %s AND project_id = %s AND topic_key = %s
                """,
                (org_id, project_id, key),
            )
            head = cur.fetchone()
            if head is None:
                raise AnswerableTopicNotFound("answerable topic not found in this Project")
            topic_id = str(head[0])

            # Scope is part of the LOOKUP, exactly as `bind_query` does for a Query
            # Spec version: a knowledge item belonging to another project simply does
            # not resolve, and is refused like an unknown one. The foreign keys of
            # migration 173 cannot do this -- they prove the version exists, not whose
            # it is -- so a missing check here let one project pin, and then cite,
            # another project's knowledge.
            is_topic = kind == KNOWLEDGE_KIND_TOPIC
            if is_topic:
                cur.execute(
                    """
                    SELECT 1 FROM app.context_topics_versions
                    WHERE topic_id = %s AND version_number = %s
                      AND (project_id IS NULL OR project_id = %s)
                    """,
                    (kid, version, project_id),
                )
            else:
                cur.execute(
                    """
                    SELECT 1 FROM app.procedures_versions
                    WHERE procedure_id = %s AND version_number = %s
                      AND (project_id IS NULL OR project_id = %s)
                    """,
                    (kid, version, project_id),
                )
            if cur.fetchone() is None:
                raise AnswerableTopicRefused(
                    "unknown_knowledge_version",
                    f"{kind} {kid!r} version {version} names no governed knowledge "
                    "in this Project",
                )

            binding_id = f"atk_{ULID()}"
            cur.execute(
                """
                INSERT INTO app.answerable_topic_knowledge_bindings
                    (id, answerable_topic_id, org_id, project_id, knowledge_kind,
                     context_topic_id, context_topic_version, procedure_id,
                     procedure_version, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    binding_id,
                    topic_id,
                    org_id,
                    project_id,
                    kind,
                    kid if is_topic else None,
                    version if is_topic else None,
                    None if is_topic else kid,
                    None if is_topic else version,
                    actor,
                ),
            )
            if cur.rowcount != 1:
                raise AnswerableTopicRefused(
                    "binding_conflict",
                    "that exact knowledge version is already declared for this topic",
                )
        result = {
            "binding_id": binding_id,
            "topic": key,
            "knowledge_kind": kind,
            "knowledge_id": kid,
            "knowledge_version": version,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"topic": key, "project_id": project_id,
                            "transition": "knowledge_bound"},
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _public(dict(op_result.result or {}))


def unbind_knowledge(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    binding_id: str,
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Withdraw one knowledge declaration. The knowledge itself is never touched."""
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        _canonical_hash,
        execute_operation,
    )

    spec = _operation_spec(
        command_type="answerable_topic.knowledge_unbound",
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        topic_key=str(binding_id),
        request_payload={"binding_id": binding_id},
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM app.answerable_topic_knowledge_bindings
                WHERE id = %s AND org_id = %s AND project_id = %s
                RETURNING knowledge_kind
                """,
                (binding_id, org_id, project_id),
            )
            row = cur.fetchone()
            if row is None:
                raise AnswerableTopicNotFound("knowledge binding not found in this Project")
        result = {"binding_id": binding_id, "knowledge_kind": row[0]}
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"binding_id": binding_id, "project_id": project_id,
                            "transition": "knowledge_unbound"},
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _public(dict(op_result.result or {}))



# ---------------------------------------------------------------------------
# Story 75-5 -- the Semantic Views a topic reads, and the join paths it allows.
#
# THE THIRD BINDING FAMILY, AND IT IS THE FIRST ONE'S SHAPE. Everything below is
# `bind_query` / `unbind_query` / `resolve_bindings` taken again: the same key
# validation, the same governed operation path, the same "scope is part of the
# lookup" rule, the same refusal-by-name. What differs is what is pinned and what
# is validated against the model.
#
# WHAT A GOVERNED PATH IS. An ordered list of relation ids the model has ALREADY
# RATIFIED -- never a join invented here, never SQL. A relation id is the `name`
# of a relationship declared by the pinned Semantic View version
# (`app.semantic_view_version_relationships.name`), which is the identity
# `semantic_model.load_view_relationships` and the change-set export already key
# a relationship by.
#
# NOTHING DERIVABLE IS STORED. The endpoints, the cardinality and the fan-out
# policy of a relation are read from the model at resolution time. A copy here
# would be a second truth able to disagree with the compiler's, and the compiler
# is the one that refuses a fan-out.
#
# FAIL-SOFT ON READ. A binding whose pinned version has since become `superseded`
# or `archived` comes back with `stale = True`, never dropped -- the same rule
# `_fetch_knowledge_bindings` states for an unreadable pin, and for the same
# reason: "this View moved on" and "this topic declared nothing" are two
# different answers.
# ---------------------------------------------------------------------------

#: The Semantic View version statuses a topic may pin. `draft` would pin a world
#: nobody can execute against; `superseded` and `archived` would pin one that has
#: moved on.
#:
#: DECLARED HERE, NOT IMPORTED, AND THAT IS NOT A DUPLICATE BY ACCIDENT. The same
#: two words are the pinnable set of a Golden Question. Importing that module by
#: name from this file trips the anchoring guard of
#: `test_metric_semantics_mcp.test_the_anchoring_path_does_not_read_the_corpus_either`
#: -- a blunt ratchet, deliberately blunt, that forbids the answer path from
#: naming the evaluation path at all. The guard is right and this constant is not
#: worth relaxing it for, so the two are pinned EQUAL by
#: `test_topic_view_bindings_pg.test_the_pinnable_set_is_the_models_and_does_not_drift`
#: instead: one declaration per module, and a test that makes drift a red run.
PINNABLE_VIEW_STATUSES = ("published", "candidate")

#: Strictness order of `semantic_view_version_relationships.fan_out_policy`, used
#: ONLY to summarize a multi-relation path: a chain is as safe as its least safe
#: link. `forbid` refuses the cross outright, `bridge` requires a bridge dataset,
#: `deduplicate` accepts it with a deduplication. Reading a chain's policy as
#: anything but its strictest link would advertise a safety the model never gave.
_FAN_OUT_STRICTNESS = {"forbid": 0, "bridge": 1, "deduplicate": 2}


def _fetch_view_relations(conn, project_id: str, version_ids: list[str]) -> dict:
    """`(view_version_id, name)` -> the ratified relation, for the pinned versions.

    ONE READ for every pinned version, rather than one per binding: a resolution
    that fires a query per row is how a topic with four Views becomes five round
    trips on the agent's critical path.
    """
    if not version_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT view_version_id, name, from_dataset, to_dataset,
                   cardinality_type, fan_out_policy, bridge_dataset
            FROM app.semantic_view_version_relationships
            WHERE project_id = %s AND view_version_id = ANY(%s)
            ORDER BY view_version_id, ordinal
            """,
            (project_id, list(version_ids)),
        )
        rows = cur.fetchall() or []
    relations: dict = {}
    for row in rows:
        # First ordinal wins a duplicated name. `bind_view` refuses to STORE an
        # ambiguous name, so this branch can only be reached by a version
        # published before that refusal existed -- and picking silently here is
        # still better than dropping the path, because the path is reported with
        # every relation it names.
        relations.setdefault((str(row[0]), str(row[1])), {
            "relation_id": str(row[1]),
            "from": row[2],
            "to": row[3],
            "cardinality": row[4],
            "fan_out_policy": row[5],
            "bridge_dataset": row[6],
        })
    return relations


def _resolve_path(path: dict, view_version_id: str, relations: dict) -> dict:
    """Turn one stored `{"relation_ids": [...]}` into what the model is told.

    A relation the pinned version no longer declares is reported as unresolved
    rather than omitted: the path is what the Project ALLOWED, and hiding the
    broken link would present a path that crosses less than it says it does.
    """
    ids = [str(rid) for rid in (path.get("relation_ids") or [])]
    legs = [relations.get((view_version_id, rid)) for rid in ids]
    resolved = [leg for leg in legs if leg is not None]
    out: dict[str, Any] = {
        "relation_ids": ids,
        "relations": [dict(leg) for leg in resolved],
        "resolved": len(resolved) == len(ids) and bool(ids),
    }
    if not resolved:
        out["from"] = None
        out["to"] = None
        out["cardinality"] = None
        out["fan_out_policy"] = None
        return out
    out["from"] = resolved[0]["from"]
    out["to"] = resolved[-1]["to"]
    # A single-relation path IS its relation. A chain reports the cardinality of
    # its last leg -- what the path arrives at -- and the STRICTEST fan-out policy
    # of its legs, because a chain is as safe as its least safe link.
    out["cardinality"] = resolved[-1]["cardinality"]
    out["fan_out_policy"] = min(
        (leg["fan_out_policy"] for leg in resolved),
        key=lambda p: _FAN_OUT_STRICTNESS.get(str(p), 0),
    )
    return out


def _fetch_view_bindings(conn, project_id: str, org_id: str | None) -> list[dict]:
    """Return this Project's Semantic View bindings, resolved AT THEIR EXACT PIN."""
    with conn.cursor() as cur:
        if org_id is None:
            org_id = _org_for_project(cur, project_id)
            if org_id is None:
                return []
        cur.execute(
            """
            SELECT b.id, t.topic_key, b.semantic_view_id, b.semantic_view_version_id,
                   v.version_number, v.status, v.name, s.name, b.allowed_paths,
                   b.note, t.lifecycle_state
            FROM app.answerable_topic_view_bindings b
            JOIN app.answerable_topics t
              ON t.id = b.answerable_topic_id
             AND t.org_id = b.org_id AND t.project_id = b.project_id
            -- The Project predicate rides the JOIN, not an afterthought: it is
            -- the same hole `_fetch_knowledge_bindings` had to close, and
            -- `app.semantic_views` carries no `org_id` to fall back on.
            LEFT JOIN app.semantic_view_versions v
              ON v.id = b.semantic_view_version_id AND v.project_id = b.project_id
            LEFT JOIN app.semantic_views s
              ON s.id = b.semantic_view_id AND s.project_id = b.project_id
            WHERE b.org_id = %s AND b.project_id = %s
            ORDER BY t.topic_key, b.created_at, b.id
            """,
            (org_id, project_id),
        )
        rows = cur.fetchall() or []

    version_ids = [str(row[3]) for row in rows]
    relations = _fetch_view_relations(conn, project_id, version_ids)

    bindings: list[dict] = []
    for row in rows:
        version_id = str(row[3])
        status = row[5]
        stored_paths = row[8]
        if isinstance(stored_paths, str):
            stored_paths = json.loads(stored_paths)
        paths = [
            _resolve_path(p, version_id, relations)
            for p in (stored_paths or [])
            if isinstance(p, dict)
        ]
        bindings.append(
            {
                "binding_id": row[0],
                "topic_key": row[1],
                "view_id": row[2],
                "view_version_id": version_id,
                "view_version_number": row[4],
                "status": status,
                "view_name": row[6] or row[7],
                # A pin whose version left the pinnable set is STALE, not gone.
                # `status is None` means the version row itself could not be read
                # in this Project, which is staler still.
                "stale": status is None or str(status) not in PINNABLE_VIEW_STATUSES,
                "paths": paths,
                "note": row[9],
                "topic_lifecycle_state": row[10],
            }
        )
    return bindings


def resolve_views(project_id: str, conn=None, topic_key: str | None = None) -> list[dict]:
    """The Semantic Views this Project bound, optionally for one topic.

    Raises nothing and drops nothing: a retired topic answers no question, so its
    bindings leave the resolution and its rows stay -- the rule `resolve_bindings`
    already applies to the queries.
    """
    if not project_id or conn is None:
        return []
    bindings = _fetch_view_bindings(conn, project_id, None)
    bindings = [b for b in bindings if b["topic_lifecycle_state"] != LIFECYCLE_RETIRED]
    if topic_key is not None:
        bindings = [b for b in bindings if b["topic_key"] == topic_key]
    return bindings


#: How many pinnable Semantic View versions the choices door serves at once. A
#: bound, and it SAYS SO on the wire when it bites: a picker that silently ends
#: at 200 reads as "that View is gone", which is the absence lie this whole
#: surface refuses.
PINNABLE_VIEW_CHOICE_LIMIT = 200


def resolve_pinnable_views(project_id: str, conn=None) -> tuple[list[dict], bool]:
    """The View versions a topic MAY pin, each with the relations it ratified.

    `(choices, truncated)`. This is what a console binder needs to ask its two
    questions -- WHICH version, then WHICH relations of THAT version -- without
    an operator retyping an `svv_...` or a relation name. It is a read of the
    model as it stands, and it stores nothing.

    Only `published` and `candidate` versions are offered, because those are
    exactly the two `bind_view` accepts: offering a `draft` would be offering a
    choice the write refuses, and every other picker on this screen is a list of
    what the server will actually take.
    """
    if not project_id or conn is None:
        return [], False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.view_id, v.version_number, v.status,
                   COALESCE(v.label, v.name, s.name, v.view_id)
            FROM app.semantic_view_versions v
            LEFT JOIN app.semantic_views s
              ON s.id = v.view_id AND s.project_id = v.project_id
            WHERE v.project_id = %s AND v.status = ANY(%s)
            ORDER BY COALESCE(v.label, v.name, s.name, v.view_id),
                     v.version_number DESC, v.id
            LIMIT %s
            """,
            (project_id, list(PINNABLE_VIEW_STATUSES), PINNABLE_VIEW_CHOICE_LIMIT + 1),
        )
        rows = cur.fetchall() or []
    truncated = len(rows) > PINNABLE_VIEW_CHOICE_LIMIT
    rows = rows[:PINNABLE_VIEW_CHOICE_LIMIT]

    # ONE read for every offered version, the rule `_fetch_view_relations` was
    # written for: a picker that fires a query per option is how a page with
    # thirty Views becomes thirty-one round trips before a first question.
    relations = _fetch_view_relations(conn, project_id, [str(row[0]) for row in rows])
    by_version: dict[str, list[dict]] = {}
    for (version_id, _name), relation in relations.items():
        by_version.setdefault(version_id, []).append(dict(relation))

    return [
        {
            "view_id": str(row[1]),
            "view_version_id": str(row[0]),
            "view_version_number": row[2],
            "status": row[3],
            "view_name": row[4],
            "relations": by_version.get(str(row[0]), []),
        }
        for row in rows
    ], truncated


def _validate_allowed_paths(payload: Any) -> list[dict]:
    """Shape only, refused before anything is stored. PURE -- no database."""
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise AnswerableTopicRefused(
            "invalid_path", "allowed_paths must be an array of {relation_ids: [...]}"
        )
    normalized: list[dict] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise AnswerableTopicRefused(
                "invalid_path", f"allowed_paths[{index}] must be an object"
            )
        raw = item.get("relation_ids")
        if not isinstance(raw, list) or not raw:
            raise AnswerableTopicRefused(
                "invalid_path",
                f"allowed_paths[{index}].relation_ids must be a non-empty array of "
                "relation ids the model already ratified",
            )
        ids = []
        for rid in raw:
            if not isinstance(rid, str) or not rid.strip():
                raise AnswerableTopicRefused(
                    "invalid_path",
                    f"allowed_paths[{index}].relation_ids must contain relation names, "
                    "never blanks",
                )
            ids.append(rid.strip())
        # A path is a WALK, and a walk crosses each relation once. The same
        # relation twice in one chain is either a typo or a cycle, and a cycle
        # re-enters a dataset it already left -- which is how a join multiplies
        # the rows the fan-out policy exists to protect. Refused by its own name,
        # here and not in the database, because it is a shape fact and needs no
        # relation graph to be seen.
        seen: set[str] = set()
        for rid in ids:
            if rid in seen:
                raise AnswerableTopicRefused(
                    "duplicate_relation",
                    f"allowed_paths[{index}] crosses relation {rid!r} twice; a join "
                    "path crosses each relation once",
                )
            seen.add(rid)
        normalized.append({"relation_ids": ids})
    return normalized


def bind_view(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    topic_key: str,
    semantic_view_version_id: str,
    allowed_paths: list[dict] | None = None,
    note: str | None = None,
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Declare one exact Semantic View version a topic reads, and its join paths."""
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        _canonical_hash,
        execute_operation,
    )

    key = _validate_key(topic_key)
    version_id = str(semantic_view_version_id or "").strip()
    if not version_id or version_id.lower() in _FORBIDDEN_PINS:
        raise AnswerableTopicRefused(
            "pin_is_not_exact",
            "semantic_view_version_id must name an exact version; 'latest' is unstorable",
        )
    paths = _validate_allowed_paths(allowed_paths)
    note_value = str(note).strip() if note is not None else None
    if note_value is not None and len(note_value) > 500:
        raise AnswerableTopicRefused("invalid_note", "note must be at most 500 characters")

    spec = _operation_spec(
        command_type="answerable_topic.view_bound",
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        topic_key=key,
        request_payload={
            "topic": key,
            "semantic_view_version_id": version_id,
            "allowed_paths": paths,
            "note": note_value,
        },
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM app.answerable_topics
                WHERE org_id = %s AND project_id = %s AND topic_key = %s
                """,
                (org_id, project_id, key),
            )
            head = cur.fetchone()
            if head is None:
                raise AnswerableTopicNotFound("answerable topic not found in this Project")
            topic_id = str(head[0])

            # Scope is part of the LOOKUP, as it is for a Query Spec version and
            # for a knowledge pin: another Project's Semantic View version simply
            # does not resolve, and is refused exactly like an invented one -- so
            # comparing two refusals cannot reveal that it exists.
            cur.execute(
                """
                SELECT view_id, status FROM app.semantic_view_versions
                WHERE id = %s AND project_id = %s
                """,
                (version_id, project_id),
            )
            version = cur.fetchone()
            if version is None:
                raise AnswerableTopicRefused(
                    "unknown_semantic_view_version",
                    f"{version_id!r} names no Semantic View version in this Project",
                )
            view_id = str(version[0])
            status = str(version[1])
            if status not in PINNABLE_VIEW_STATUSES:
                raise AnswerableTopicRefused(
                    "version_not_pinnable",
                    f"this Semantic View version is {status}; a topic reads a published "
                    "or candidate version",
                )

            _validate_relations(cur, project_id, version_id, view_id, paths)

            binding_id = f"atvb_{ULID()}"
            cur.execute(
                """
                INSERT INTO app.answerable_topic_view_bindings
                    (id, answerable_topic_id, org_id, project_id, semantic_view_id,
                     semantic_view_version_id, allowed_paths, note, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    binding_id,
                    topic_id,
                    org_id,
                    project_id,
                    view_id,
                    version_id,
                    _canonical_json(paths),
                    note_value,
                    actor,
                ),
            )
            if cur.rowcount != 1:
                # Stated, never absorbed: a caller that believes it declared a
                # View must be told it did not.
                raise AnswerableTopicRefused(
                    "binding_conflict",
                    "that exact Semantic View version is already bound to this topic",
                )
        result = {
            "binding_id": binding_id,
            "topic": key,
            "semantic_view_id": view_id,
            "semantic_view_version_id": version_id,
            "allowed_paths": paths,
            "note": note_value,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"topic": key, "project_id": project_id,
                            "transition": "view_bound"},
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _public(dict(op_result.result or {}))


def _validate_relations(cur, project_id: str, version_id: str, view_id: str,
                        paths: list[dict]) -> None:
    """Every relation id of every path must be one THIS View version ratified,
    AND the legs of a path must actually chain.

    FOUR REFUSALS, NOT ONE, and that is the point of the story. An invented
    relation, a real relation borrowed from another Semantic View version, a name
    that means two relations, and two legs that never touch are four different
    mistakes; answering all four with "bad path" would tell the author nothing
    they can act on.

    THE CHAINING RULE. `_resolve_path` reports a path's `from` as its FIRST leg's
    `from_dataset` and its `to` as its LAST leg's `to_dataset`. That reading is
    only true of a walk: `["campaign_to_account", "market_to_region"]` would be
    served as "campaigns -> regions", a route that crosses nothing between
    accounts and markets and that no relationship of this View authorises. So a
    leg's `to_dataset` must be the next leg's `from_dataset`, and the refusal
    names the two relations and the two datasets that do not meet.
    """
    wanted = sorted({rid for path in paths for rid in path["relation_ids"]})
    if not wanted:
        return

    cur.execute(
        """
        SELECT name, from_dataset, to_dataset
        FROM app.semantic_view_version_relationships
        WHERE view_version_id = %s AND project_id = %s AND name = ANY(%s)
        ORDER BY name, ordinal
        """,
        (version_id, project_id, wanted),
    )
    # name -> its endpoints, in ordinal order. The COUNT the ambiguity check
    # needs is the length of this list, so the endpoints the chaining check needs
    # ride the same read rather than costing a second round trip.
    endpoints: dict[str, list[tuple]] = {}
    for row in cur.fetchall() or []:
        endpoints.setdefault(str(row[0]), []).append((row[1], row[2]))
    in_view = {name: len(rows) for name, rows in endpoints.items()}

    missing = [rid for rid in wanted if rid not in in_view]
    if missing:
        # A relation this Project ratified SOMEWHERE is a different mistake from
        # one it never ratified at all, so the graph is asked before the refusal
        # is worded.
        cur.execute(
            """
            SELECT DISTINCT name FROM app.semantic_view_version_relationships
            WHERE project_id = %s AND name = ANY(%s)
            """,
            (project_id, missing),
        )
        elsewhere = {str(row[0]) for row in (cur.fetchall() or [])}
        borrowed = [rid for rid in missing if rid in elsewhere]
        if borrowed:
            raise AnswerableTopicRefused(
                "relation_not_in_view",
                f"relation {borrowed[0]!r} is ratified in this Project but not by "
                f"Semantic View {view_id!r} at version {version_id!r}; a topic may "
                "only cross the relations of the View it binds",
            )
        raise AnswerableTopicRefused(
            "unknown_relation",
            f"relation {missing[0]!r} names no ratified relationship in this Project",
        )

    ambiguous = [rid for rid, count in in_view.items() if count > 1]
    if ambiguous:
        raise AnswerableTopicRefused(
            "ambiguous_relation",
            f"relation {sorted(ambiguous)[0]!r} names more than one relationship of "
            "this Semantic View version; choosing one would pick a join nobody named",
        )

    # Every relation now resolves to exactly one relationship, so the walk can be
    # read. Checked LAST, after the three identity refusals: telling an author
    # their chain is broken when one of its legs does not exist would name the
    # wrong repair.
    for path in paths:
        ids = path["relation_ids"]
        for left, right in zip(ids, ids[1:]):
            arrives_at = endpoints[left][0][1]
            leaves_from = endpoints[right][0][0]
            if arrives_at != leaves_from:
                raise AnswerableTopicRefused(
                    "path_not_chained",
                    f"relation {left!r} arrives at {arrives_at!r} and relation "
                    f"{right!r} leaves from {leaves_from!r}; a join path is a walk, "
                    "so each relation must start where the one before it arrived",
                )


def unbind_view(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    binding_id: str,
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Withdraw one View declaration. The View and its relationships are untouched."""
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        _canonical_hash,
        execute_operation,
    )

    spec = _operation_spec(
        command_type="answerable_topic.view_unbound",
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        topic_key=str(binding_id),
        request_payload={"binding_id": binding_id},
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM app.answerable_topic_view_bindings
                WHERE id = %s AND org_id = %s AND project_id = %s
                RETURNING semantic_view_id, semantic_view_version_id
                """,
                (binding_id, org_id, project_id),
            )
            row = cur.fetchone()
            if row is None:
                raise AnswerableTopicNotFound("view binding not found in this Project")
        result = {
            "binding_id": binding_id,
            "semantic_view_id": row[0],
            "semantic_view_version_id": row[1],
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"binding_id": binding_id, "project_id": project_id,
                            "transition": "view_unbound"},
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _public(dict(op_result.result or {}))


def _views_by_topic(project_id: str, conn) -> dict[str, list[dict]]:
    """topic_key -> what the model is told about its Views. Never raises.

    THE ONLY CALLER IS `verified_query_pairs`, and the key it adds is ADDITIVE:
    a topic that declares no View gets no entry here, so its pair keeps the exact
    payload it had before story 75-5. That is a rule, not an intention -- a
    consumer that never asked about Views cannot be made to read one.

    ONE QUESTION BEFORE THREE. Nearly every Project has bound no View at all, and
    for those `resolve_views` still spent an org lookup, a join over the bindings
    and -- with rows -- a relationship read, to answer "none". The EXISTS below is
    one index probe on the agent's critical path, and it is asked first.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM app.answerable_topic_view_bindings "
                "WHERE project_id = %s)",
                (project_id,),
            )
            row = cur.fetchone()
        if not (row and row[0]):
            return {}
        bindings = resolve_views(project_id, conn)
    except Exception as exc:  # noqa: BLE001 -- the pairing survives a view read failure
        logger.debug("answerable_topics: view_bindings_read_failed: %s", exc)
        return {}
    grouped: dict[str, list[dict]] = {}
    for binding in bindings:
        grouped.setdefault(binding["topic_key"], []).append(
            {
                "view_id": binding["view_id"],
                "view_version_id": binding["view_version_id"],
                "view_version_number": binding["view_version_number"],
                "view_name": binding["view_name"],
                "status": binding["status"],
                "stale": binding["stale"],
                "paths": binding["paths"],
            }
        )
    return grouped

# ---------------------------------------------------------------------------
# Writes. Every one of them goes through the governed operation path.
# ---------------------------------------------------------------------------

_ALLOWED_KINDS = ("kpi", "context")


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize and refuse before anything is stored."""
    from core import cards as _cards  # noqa: PLC0415

    title = str(payload.get("title") or "").strip()
    question = str(payload.get("answers_question") or "").strip()
    base = str(payload.get("base_template_id") or "").strip()
    kind = str(payload.get("kind") or _cards.CARD_KIND_KPI).strip()

    if not title or len(title) > 200:
        raise AnswerableTopicRefused("invalid_title", "title must be 1-200 characters")
    if not question or len(question) > 500:
        raise AnswerableTopicRefused(
            "invalid_question", "answers_question must be 1-500 characters"
        )
    if kind not in _ALLOWED_KINDS:
        raise AnswerableTopicRefused("invalid_kind", f"kind must be one of {_ALLOWED_KINDS}")
    # Refused at write time, never stored to fail later at render: a topic whose
    # base card does not exist is a question with no renderer.
    if _cards.get_template(base) is None:
        raise AnswerableTopicRefused(
            "unknown_base_template",
            f"base_template_id {base!r} names no registered card template",
        )

    def _str_list(key: str) -> list[str]:
        raw = payload.get(key) or []
        if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
            raise AnswerableTopicRefused("invalid_field", f"{key} must be a list of strings")
        return [v.strip() for v in raw if v.strip()]

    try:
        rank = int(payload.get("fallback_rank") or 0)
    except (TypeError, ValueError) as exc:
        raise AnswerableTopicRefused("invalid_field", "fallback_rank must be an integer") from exc

    # Story 52.3 AC5 lives or dies here. The column was added by migration 173 and
    # READ by `knowledge_citations`, and nothing wrote it -- so `requires` was
    # always False and a topic could never actually refuse. An acceptance criterion
    # whose behaviour cannot be reached is the "delivered but inert" defect this
    # repository hunts, and this one was mine.
    requires_knowledge = payload.get("requires_knowledge", False)
    if not isinstance(requires_knowledge, bool):
        raise AnswerableTopicRefused(
            "invalid_field", "requires_knowledge must be true or false"
        )

    return {
        "title": title,
        "answers_question": question,
        "kind": kind,
        "fallback_rank": rank,
        "required_metrics": _str_list("required_metrics"),
        "required_dimensions": _str_list("required_dimensions"),
        "optional_metrics": _str_list("optional_metrics"),
        "optional_dimensions": _str_list("optional_dimensions"),
        "base_template_id": base,
        "requires_knowledge": requires_knowledge,
    }


def _validate_key(topic_key: str) -> str:
    import re  # noqa: PLC0415

    key = str(topic_key or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", key):
        raise AnswerableTopicRefused(
            "invalid_topic_key",
            "topic_key must be lowercase alphanumeric with - or _, 1-64 characters",
        )
    return key


def _operation_spec(
    *,
    command_type: str,
    actor: str,
    org_id: str,
    project_id: str,
    topic_key: str,
    request_payload: dict[str, Any],
    idempotency_key: str,
    host_context: dict[str, Any] | None,
    trace_id: str | None,
):
    from core.operations import OperationSpec  # noqa: PLC0415

    return OperationSpec(
        command_type=command_type,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"org:{org_id}",
            f"project:{project_id}",
            f"answerable-topic:{topic_key}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={"policy": "answerable-topic-v1", "tool": "rest-v1"},
        request_payload=request_payload,
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"answerable-topic:{project_id}:{topic_key}:{command_type}",
        trace_id=trace_id,
    )


def _insert_version(
    cur,
    *,
    topic_id: str,
    org_id: str,
    project_id: str,
    version_number: int,
    fields: dict[str, Any],
    predecessor: str | None,
    actor: str,
) -> str:
    version_id = f"atv_{ULID()}"
    cur.execute(
        """
        INSERT INTO app.answerable_topic_versions
            (id, answerable_topic_id, org_id, project_id, version_number, title,
             answers_question, kind, fallback_rank, required_metrics,
             required_dimensions, optional_metrics, optional_dimensions,
             base_template_id, content_hash, predecessor_version_id, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                %s::jsonb, %s, %s, %s, %s)
        """,
        (
            version_id,
            topic_id,
            org_id,
            project_id,
            version_number,
            fields["title"],
            fields["answers_question"],
            fields["kind"],
            fields["fallback_rank"],
            _canonical_json(fields["required_metrics"]),
            _canonical_json(fields["required_dimensions"]),
            _canonical_json(fields["optional_metrics"]),
            _canonical_json(fields["optional_dimensions"]),
            fields["base_template_id"],
            content_hash({k: v for k, v in fields.items() if k != "requires_knowledge"}),
            predecessor,
            actor,
        ),
    )
    cur.execute(
        """
        UPDATE app.answerable_topics
        SET current_version_id = %s, updated_at = NOW()
        WHERE id = %s AND org_id = %s AND project_id = %s
        """,
        (version_id, topic_id, org_id, project_id),
    )
    return version_id


def create_topic(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    topic_key: str,
    payload: dict[str, Any],
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Store a topic head and its version 1. Rewording a default lands here too."""
    from core.operations import MutationResult, _canonical_hash, execute_operation  # noqa: PLC0415

    key = _validate_key(topic_key)
    fields = _validate_payload(payload)
    origin = ORIGIN_DEFAULT if _default_entry(key) is not None else ORIGIN_PROJECT

    spec = _operation_spec(
        command_type="answerable_topic.created",
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        topic_key=key,
        request_payload={"topic": key, "origin": origin, **fields},
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        topic_id = f"atp_{ULID()}"
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.answerable_topics
                    (id, org_id, project_id, topic_key, lifecycle_state,
                     base_template_id, origin, created_by, requires_knowledge)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (org_id, project_id, topic_key) DO NOTHING
                """,
                (
                    topic_id,
                    org_id,
                    project_id,
                    key,
                    LIFECYCLE_ACTIVE,
                    fields["base_template_id"],
                    origin,
                    actor,
                    fields["requires_knowledge"],
                ),
            )
            if cur.rowcount != 1:
                raise AnswerableTopicRefused(
                    "topic_exists",
                    f"topic_key {key!r} already exists in this Project -- append a version",
                )
            version_id = _insert_version(
                cur,
                topic_id=topic_id,
                org_id=org_id,
                project_id=project_id,
                version_number=1,
                fields=fields,
                predecessor=None,
                actor=actor,
            )
        result = {
            "topic_id": topic_id,
            "topic": key,
            "version_id": version_id,
            "version_number": 1,
            "origin": origin,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"topic": key, "project_id": project_id, "transition": "created"},
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _public(dict(op_result.result or {}))


def append_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    topic_key: str,
    payload: dict[str, Any],
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Reword a stored topic. The previous version is never edited (trigger, mig 170)."""
    from core.operations import MutationResult, _canonical_hash, execute_operation  # noqa: PLC0415

    key = _validate_key(topic_key)
    fields = _validate_payload(payload)

    spec = _operation_spec(
        command_type="answerable_topic.reworded",
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        topic_key=key,
        request_payload={"topic": key, **fields},
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, current_version_id FROM app.answerable_topics
                WHERE org_id = %s AND project_id = %s AND topic_key = %s
                """,
                (org_id, project_id, key),
            )
            head = cur.fetchone()
            if head is None:
                raise AnswerableTopicNotFound("answerable topic not found in this Project")
            topic_id, predecessor = str(head[0]), head[1]
            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM app.answerable_topic_versions
                WHERE answerable_topic_id = %s AND project_id = %s
                """,
                (topic_id, project_id),
            )
            version_number = int(cur.fetchone()[0])
            version_id = _insert_version(
                cur,
                topic_id=topic_id,
                org_id=org_id,
                project_id=project_id,
                version_number=version_number,
                fields=fields,
                predecessor=predecessor,
                actor=actor,
            )
            # The base card may change with a reword (a Project moves its question
            # onto another registered composition); the head follows the version.
            cur.execute(
                """
                UPDATE app.answerable_topics
                SET base_template_id = %s, requires_knowledge = %s
                WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (
                    fields["base_template_id"],
                    fields["requires_knowledge"],
                    topic_id,
                    org_id,
                    project_id,
                ),
            )
        result = {
            "topic_id": topic_id,
            "topic": key,
            "version_id": version_id,
            "version_number": version_number,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"topic": key, "project_id": project_id, "transition": "reworded"},
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _public(dict(op_result.result or {}))


def retire_topic(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    topic_key: str,
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Remove a topic from the catalog without deleting anything.

    Retiring a platform default stores the head it never had, with a version 1
    snapshotting the wording being retired -- so the record says WHAT was
    retired, not merely that something was.
    """
    from core.operations import MutationResult, _canonical_hash, execute_operation  # noqa: PLC0415

    key = _validate_key(topic_key)
    default_entry = _default_entry(key)

    spec = _operation_spec(
        command_type="answerable_topic.retired",
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        topic_key=key,
        request_payload={"topic": key},
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.answerable_topics SET lifecycle_state = %s, updated_at = NOW()
                WHERE org_id = %s AND project_id = %s AND topic_key = %s
                RETURNING id
                """,
                (LIFECYCLE_RETIRED, org_id, project_id, key),
            )
            row = cur.fetchone()
            if row is not None:
                topic_id = str(row[0])
            else:
                if default_entry is None:
                    raise AnswerableTopicNotFound(
                        "answerable topic not found in this Project"
                    )
                topic_id = f"atp_{ULID()}"
                cur.execute(
                    """
                    INSERT INTO app.answerable_topics
                        (id, org_id, project_id, topic_key, lifecycle_state,
                         base_template_id, origin, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        topic_id,
                        org_id,
                        project_id,
                        key,
                        LIFECYCLE_RETIRED,
                        key,
                        ORIGIN_DEFAULT,
                        actor,
                    ),
                )
                snapshot = {
                    "title": default_entry["title"],
                    "answers_question": default_entry["answers_question"],
                    "kind": default_entry["kind"],
                    "fallback_rank": default_entry["fallback_rank"],
                    "required_metrics": list(default_entry["required_metrics"]),
                    "required_dimensions": list(default_entry["required_dimensions"]),
                    "optional_metrics": list(default_entry["optional_metrics"]),
                    "optional_dimensions": list(default_entry["optional_dimensions"]),
                    "base_template_id": key,
                }
                _insert_version(
                    cur,
                    topic_id=topic_id,
                    org_id=org_id,
                    project_id=project_id,
                    version_number=1,
                    fields=snapshot,
                    predecessor=None,
                    actor=actor,
                )
        result = {"topic_id": topic_id, "topic": key, "lifecycle_state": LIFECYCLE_RETIRED}
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"topic": key, "project_id": project_id, "transition": "retired"},
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _public(dict(op_result.result or {}))
