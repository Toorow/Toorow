"""Story 52.1 -- the REST door of the Answerable Topic catalog.

THIN BY CONSTRUCTION. Every handler translates HTTP into `core.answerable_topics`
and nothing else: no SQL, no validation and no lifecycle rule lives here, so the
console and any future adapter reach the same catalog through the same rules.

WHY IT IS A SEPARATE MODULE FROM `cards_api`. `cards_api` answers "what can this
Project render, and with which fields" -- a READ over the resolved catalog that
`WidgetCardsPage` already consumes. This module owns the WRITES, which go through
`core.operations.execute_operation` so audit and outbox commit with the change.
Mixing the two would have put an ungoverned write next to a best-effort read.

NON-DISCLOSURE IS THE DEFAULT. Foreign, denied and absent all answer with the same
404 envelope, and the denial answers before any work is done.

TWO THINGS THIS FILE GOT WRONG, both found by a fresh-context review and both
invisible to its own seam tests, which only ever exercised REFUSAL paths:

  * `host_context` carried `{"surface": "console"}`. `operations._HOST_KEYS` does
    not list `surface`, so `prepare_operation` raised before any mutation and
    every write route answered 500. No audit row and no outbox entry was ever
    written for this object -- the governance the acceptance criteria claim was
    empty. The rest of the repository passes `{"host": "rest"}`.
  * nothing called `conn.commit()`. `core.db.get_connection` closes without
    committing on purpose, and `core.operations` never commits either. Repairing
    only the first half would have made the API answer 201 while persisting
    nothing, which is worse than the 500 because it looks like it worked.

The lesson is in the tests, not here: a seam suite that asserts 401, 400 and 422
proves the door refuses, never that it opens. `test_answerable_topics_write_pg.py`
now performs a real write against a real database.

A THIRD THING, found by the security axis of the same review: migrations 170, 172
and 173 each install an Epic-36 RLS policy of the form
`current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on' OR ...`.
That predicate is unconditionally TRUE until something sets the flag on the
session, and nothing here did -- so the floor those three migrations describe in
their headers was never armed by the only routes that read those tables. Every
connection this module opens now comes from `topic_connection`, which arms it.

A FOURTH, found by the 75-1 review and repaired on 2026-09-06: `topic_connection`
armed the floor TRANSACTION-LOCALLY, and every write handler here commits inside
its own `with`. The floor was therefore down for everything a handler did after
its commit -- and an unarmed floor does not refuse, it silently permits. It now
takes `core.db.request_connection`, the repository's one acquisition of an armed
connection, which arms the SESSION, translates the caller's subject to its
canonical identity and refuses a connection that lost its context behind a
transaction-mode pooler. The repair is on the helper and therefore on ALL the
doors -- topics, query bindings, knowledge pins, Semantic View bindings -- because
a floor defect is a defect of the rail, never of one route.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.answerable_topics import (
    AnswerableTopicNotFound,
    AnswerableTopicRefused,
    append_version,
    bind_knowledge,
    bind_query,
    bind_view,
    create_topic,
    resolve_bindings,
    resolve_catalog_with_reason,
    resolve_knowledge,
    resolve_pinnable_views,
    resolve_views,
    retire_topic,
    unbind_knowledge,
    unbind_query,
    unbind_view,
)

logger = logging.getLogger(__name__)


def topic_connection(identity: str):
    """The request-scoped connection of `core.db` -- ONE acquisition, armed.

    THE SEAM, NOT A COPY OF IT, and this helper used to be the copy. It opened a
    bare `get_connection()` and armed the floor with
    `query_specs_api.arm_access_floor`. Three things were wrong with that, and
    all three bite every door of this module -- topics, query bindings, knowledge
    pins and Semantic View bindings alike, which is why the repair is here and
    not on one route:

      * THE LIFETIME. `arm_access_floor` uses `set_config(..., true)`, which
        reverts at the end of the transaction it ran in. EVERY write handler
        here calls `conn.commit()` mid-`with`, so the floor was down for anything
        the handler did after that commit. The policy predicate is
        `current_setting(...) IS DISTINCT FROM 'on' OR ...`, which is
        unconditionally TRUE unarmed: the floor did not fail, it silently
        vanished halfway through the request. `install_access_context` commits a
        SESSION setting for exactly this reason (`core/db.py:128-140`).
      * THE IDENTITY. `_check_auth` hands back whatever the token carried; the
        floor compares `toorow.identity` against `app.org_members.identity`,
        which holds the canonical `person_<ULID>`. `request_connection`
        translates; `arm_access_floor` does not.
      * THE POOLER. `request_connection` reads the context back after its own
        commit and REFUSES a connection that lost it -- a transaction-mode
        pooler hands the backend to somebody else. A helper that arms a
        connection it borrowed cannot make that check.

    Isolation does not currently DEPEND on the floor here -- `_require_datastream_role`
    runs first and every statement in `core.answerable_topics` carries
    `WHERE org_id = %s AND project_id = %s`. This is the floor UNDER those, so
    that the day one of those predicates is forgotten, the database refuses the
    query instead of returning another organization's rows. The knowledge-pin
    scope hole of this same epic is what a floor is for -- and a floor that drops
    at the first `commit()` is not one.

    The same repair story 75-1 made on `calculated_field_proposals_api._proposal_connection`.
    Two doors on one rail that armed the floor two ways would be two answers to
    one question.
    """
    from core.db import request_connection  # noqa: PLC0415

    return request_connection(identity)


_BASE = "/api/projects/{project_id}/answerable-topics"

#: One envelope for foreign, denied and absent.
_NOT_FOUND = {"code": "not_found", "message": "Not found"}


async def _authorize(request: Request, role: str = "viewer"):
    """Return (identity, org_id, project_id) or a Response. Denial answers first."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)
    project_id = request.path_params["project_id"]
    with get_connection() as conn:
        denied = _require_datastream_role(project_id, identity, role, conn)
        if denied is not None:
            return denied
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
    if row is None:
        return JSONResponse(_NOT_FOUND, 404)
    return str(identity), str(row[0]), str(project_id)


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def _idempotency_key(request: Request) -> str | None:
    key = (request.headers.get("Idempotency-Key") or "").strip()
    return key or None


def _missing_idempotency() -> Response:
    return JSONResponse(
        {
            "code": "missing_idempotency_key",
            "message": "Idempotency-Key is required for a governed write.",
        },
        400,
    )


async def _list_topics(request: Request) -> Response:
    """GET {base} -- the catalog this Project answers with, defaults layered.

    `origin` on an entry says where it came from; an entry without one is inherited
    from the platform default set and was never stored. That distinction is the
    whole point of the object, so it is carried, not flattened.

    `catalog_status` says whether this IS the Project's catalog. The route used to
    answer the platform default set, under this Project's id, when the store could
    not be read -- so a question the Project had retired reappeared in the console
    as one of its own, and a rewording it had authored vanished, both without a
    word. The degradation is kept (a discovery surface that answers nothing is
    worse) and it is now named.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, _org_id, project_id = auth

    with topic_connection(identity) as conn:
        catalog, catalog_reason = resolve_catalog_with_reason(project_id, conn)
    return JSONResponse(
        {
            "project_id": project_id,
            "topics": catalog,
            "topics_total": len(catalog),
            "catalog_status": "resolved" if catalog_reason is None else "defaults_only",
            "catalog_reason": catalog_reason,
        }
    )


async def _create(request: Request) -> Response:
    """POST {base} -- store a topic and its version 1 (add, or reword a default)."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    idem = _idempotency_key(request)
    if idem is None:
        return _missing_idempotency()
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with topic_connection(identity) as conn:
            result = create_topic(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                topic_key=str(body.get("topic_key") or ""),
                payload=body,
                idempotency_key=idem,
                host_context={"host": "rest"},
            )
            conn.commit()
    except AnswerableTopicRefused as exc:
        return JSONResponse(exc.as_dict(), 422)
    except AnswerableTopicNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(result, 201)


async def _add_version(request: Request) -> Response:
    """POST {base}/{topic_key}/versions -- reword. The prior version is never edited."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    idem = _idempotency_key(request)
    if idem is None:
        return _missing_idempotency()
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with topic_connection(identity) as conn:
            result = append_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                topic_key=request.path_params["topic_key"],
                payload=body,
                idempotency_key=idem,
                host_context={"host": "rest"},
            )
            conn.commit()
    except AnswerableTopicRefused as exc:
        return JSONResponse(exc.as_dict(), 422)
    except AnswerableTopicNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(result, 201)


async def _retire(request: Request) -> Response:
    """POST {base}/{topic_key}/retire -- leave the catalog, delete nothing."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    idem = _idempotency_key(request)
    if idem is None:
        return _missing_idempotency()

    try:
        with topic_connection(identity) as conn:
            result = retire_topic(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                topic_key=request.path_params["topic_key"],
                idempotency_key=idem,
                host_context={"host": "rest"},
            )
            conn.commit()
    except AnswerableTopicRefused as exc:
        return JSONResponse(exc.as_dict(), 422)
    except AnswerableTopicNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(result)


async def _list_queries(request: Request) -> Response:
    """GET {base}/{topic_key}/queries -- the governed queries bound to one topic.

    An empty list is returned WITH nothing else: this route answers about one
    topic of a project the caller can already read, so there is no absence to
    disambiguate here. The three-way distinction lives where it matters -- in
    `metric_verified_queries`, which an agent calls without knowing the project's
    state.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, _org_id, project_id = auth
    topic_key = request.path_params["topic_key"]

    with topic_connection(identity) as conn:
        bindings = resolve_bindings(project_id, conn, topic_key=topic_key)
    return JSONResponse(
        {"project_id": project_id, "topic_key": topic_key,
         "queries": bindings, "queries_total": len(bindings)}
    )


async def _bind(request: Request) -> Response:
    """POST {base}/{topic_key}/queries -- bind one EXACT Query Spec version."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    idem = _idempotency_key(request)
    if idem is None:
        return _missing_idempotency()
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with topic_connection(identity) as conn:
            result = bind_query(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                topic_key=request.path_params["topic_key"],
                query_spec_version_id=str(body.get("query_spec_version_id") or ""),
                role=str(body.get("role") or ""),
                position=body.get("position"),
                idempotency_key=idem,
                host_context={"host": "rest"},
            )
            conn.commit()
    except AnswerableTopicRefused as exc:
        return JSONResponse(exc.as_dict(), 422)
    except AnswerableTopicNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(result, 201)


async def _unbind(request: Request) -> Response:
    """DELETE {base}/{topic_key}/queries/{binding_id} -- unbind, touching no query."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    idem = _idempotency_key(request)
    if idem is None:
        return _missing_idempotency()

    try:
        with topic_connection(identity) as conn:
            result = unbind_query(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                binding_id=request.path_params["binding_id"],
                idempotency_key=idem,
                host_context={"host": "rest"},
            )
            conn.commit()
    except AnswerableTopicRefused as exc:
        return JSONResponse(exc.as_dict(), 422)
    except AnswerableTopicNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(result)


async def _list_knowledge(request: Request) -> Response:
    """GET {base}/{topic_key}/knowledge -- the governed knowledge this topic declares.

    Each pin carries `readable`: a declaration whose version row cannot be read is
    RETURNED, not omitted. Production holds 41 knowledge version rows and zero
    knowledge heads, so an unreadable pin is a state an operator will actually
    meet, and hiding it would make "context missing" look like "nothing declared".
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, _org_id, project_id = auth
    topic_key = request.path_params["topic_key"]

    with topic_connection(identity) as conn:
        pins = resolve_knowledge(project_id, conn, topic_key=topic_key)
    return JSONResponse(
        {
            "project_id": project_id,
            "topic_key": topic_key,
            "knowledge": [
                {
                    "binding_id": p["binding_id"],
                    "knowledge_kind": p["knowledge_kind"],
                    "knowledge_id": p["knowledge_id"],
                    "knowledge_version": p["knowledge_version"],
                    "title": p["title"],
                    "readable": p["readable"],
                }
                for p in pins
            ],
            "knowledge_total": len(pins),
        }
    )


async def _bind_knowledge(request: Request) -> Response:
    """POST {base}/{topic_key}/knowledge -- declare one exact knowledge version."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    idem = _idempotency_key(request)
    if idem is None:
        return _missing_idempotency()
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with topic_connection(identity) as conn:
            result = bind_knowledge(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                topic_key=request.path_params["topic_key"],
                knowledge_kind=str(body.get("knowledge_kind") or ""),
                knowledge_id=str(body.get("knowledge_id") or ""),
                knowledge_version=body.get("knowledge_version"),
                idempotency_key=idem,
                host_context={"host": "rest"},
            )
            conn.commit()
    except AnswerableTopicRefused as exc:
        return JSONResponse(exc.as_dict(), 422)
    except AnswerableTopicNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(result, 201)


async def _unbind_knowledge(request: Request) -> Response:
    """DELETE {base}/{topic_key}/knowledge/{binding_id} -- withdraw a declaration."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    idem = _idempotency_key(request)
    if idem is None:
        return _missing_idempotency()

    try:
        with topic_connection(identity) as conn:
            result = unbind_knowledge(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                binding_id=request.path_params["binding_id"],
                idempotency_key=idem,
                host_context={"host": "rest"},
            )
            conn.commit()
    except AnswerableTopicRefused as exc:
        return JSONResponse(exc.as_dict(), 422)
    except AnswerableTopicNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(result)


async def _list_views(request: Request) -> Response:
    """GET {base}/{topic_key}/views -- the Semantic Views this topic reads.

    Each binding carries `stale`: a pin whose Semantic View version has since
    become `superseded` or `archived` is RETURNED, not omitted. Dropping it would
    make "this View moved on" look like "this topic declared nothing", which is
    the confusion the knowledge door already refuses to create.

    Each path carries the relations it names AND what the model says about them
    today -- endpoints, cardinality, fan-out policy. Nothing derivable is stored,
    so nothing derivable is stale.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, _org_id, project_id = auth
    topic_key = request.path_params["topic_key"]

    with topic_connection(identity) as conn:
        bindings = resolve_views(project_id, conn, topic_key=topic_key)
    return JSONResponse(
        {
            "project_id": project_id,
            "topic_key": topic_key,
            "views": [
                {
                    "binding_id": b["binding_id"],
                    "view_id": b["view_id"],
                    "view_version_id": b["view_version_id"],
                    "view_version_number": b["view_version_number"],
                    "view_name": b["view_name"],
                    "status": b["status"],
                    "stale": b["stale"],
                    "paths": b["paths"],
                    "note": b["note"],
                }
                for b in bindings
            ],
            "views_total": len(bindings),
        }
    )


async def _list_pinnable_views(request: Request) -> Response:
    """GET {base}/semantic-views -- the View versions a topic MAY pin, with their
    relations.

    THE PICKER'S SOURCE, and the reason it exists: `bind_view` refuses a relation
    name the pinned version did not ratify, so a console that asked for one as
    free text was asking an operator to remember `campaign_to_account` and then
    refusing them when they misremembered. This door answers both questions the
    binder asks -- which version, then which of ITS relations -- so the chain that
    travels is composed from what the model declares and never typed.

    Project-scoped, not topic-scoped: what a Project may pin does not depend on
    which question is being answered, and asking it per topic would ask the same
    question twice.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, _org_id, project_id = auth

    with topic_connection(identity) as conn:
        choices, truncated = resolve_pinnable_views(project_id, conn)
    return JSONResponse(
        {
            "project_id": project_id,
            "semantic_views": choices,
            "semantic_views_total": len(choices),
            # Said out loud when it bites: a list that silently ends reads as
            # "that View is gone", which is the absence lie this surface refuses.
            "truncated": truncated,
        }
    )


async def _bind_view(request: Request) -> Response:
    """POST {base}/{topic_key}/views -- bind one EXACT Semantic View version."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    idem = _idempotency_key(request)
    if idem is None:
        return _missing_idempotency()
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with topic_connection(identity) as conn:
            result = bind_view(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                topic_key=request.path_params["topic_key"],
                semantic_view_version_id=str(body.get("semantic_view_version_id") or ""),
                allowed_paths=body.get("allowed_paths"),
                note=body.get("note"),
                idempotency_key=idem,
                host_context={"host": "rest"},
            )
            conn.commit()
    except AnswerableTopicRefused as exc:
        return JSONResponse(exc.as_dict(), 422)
    except AnswerableTopicNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(result, 201)


async def _unbind_view(request: Request) -> Response:
    """DELETE {base}/{topic_key}/views/{binding_id} -- withdraw a declaration."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    idem = _idempotency_key(request)
    if idem is None:
        return _missing_idempotency()

    try:
        with topic_connection(identity) as conn:
            result = unbind_view(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                binding_id=request.path_params["binding_id"],
                idempotency_key=idem,
                host_context={"host": "rest"},
            )
            conn.commit()
    except AnswerableTopicRefused as exc:
        return JSONResponse(exc.as_dict(), 422)
    except AnswerableTopicNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(result)

# Literal segments before parameterized ones: `versions`, `retire` and `queries`
# hang off a topic key, and no route can capture one as the other.
answerable_topic_routes = [
    Route(_BASE, endpoint=_list_topics, methods=["GET"]),
    Route(_BASE, endpoint=_create, methods=["POST"]),
    Route(f"{_BASE}/{{topic_key}}/versions", endpoint=_add_version, methods=["POST"]),
    Route(f"{_BASE}/{{topic_key}}/retire", endpoint=_retire, methods=["POST"]),
    Route(f"{_BASE}/{{topic_key}}/queries", endpoint=_list_queries, methods=["GET"]),
    Route(f"{_BASE}/{{topic_key}}/queries", endpoint=_bind, methods=["POST"]),
    Route(
        f"{_BASE}/{{topic_key}}/queries/{{binding_id}}",
        endpoint=_unbind,
        methods=["DELETE"],
    ),
    Route(f"{_BASE}/{{topic_key}}/knowledge", endpoint=_list_knowledge, methods=["GET"]),
    Route(f"{_BASE}/{{topic_key}}/knowledge", endpoint=_bind_knowledge, methods=["POST"]),
    Route(
        f"{_BASE}/{{topic_key}}/knowledge/{{binding_id}}",
        endpoint=_unbind_knowledge,
        methods=["DELETE"],
    ),
    # A LITERAL, and it collides with nothing: every topic-scoped route carries a
    # second segment (`versions`, `retire`, `queries`, `knowledge`, `views`), so
    # no `{topic_key}` route can capture this one.
    Route(f"{_BASE}/semantic-views", endpoint=_list_pinnable_views, methods=["GET"]),
    Route(f"{_BASE}/{{topic_key}}/views", endpoint=_list_views, methods=["GET"]),
    Route(f"{_BASE}/{{topic_key}}/views", endpoint=_bind_view, methods=["POST"]),
    Route(
        f"{_BASE}/{{topic_key}}/views/{{binding_id}}",
        endpoint=_unbind_view,
        methods=["DELETE"],
    ),
]
