"""Story 75-3 -- `get_exemplars`, the fourth door of the agent surface.

WHAT WAS MEASURED, 2026-09-05, before this file existed. `agent_surface_mcp.py`
exposes exactly three tools -- `search_context`, `get_procedure`,
`resolve_business_path` -- so an agent could read the governed context and the
governed route, and never what a good answer to a question of this kind has
already looked like in this Project. The Golden Questions serve the evaluation
layer and nothing else. This is the "sample query" of the 2026-09-05 comparative
analysis, fed by the evaluation instead of typed by hand.

The target is the amendment of 2026-09-05 in
`docs/product-architecture/context-hub.md` ("approved exemplars are served to
agents, pinned and budgeted"). `core.exemplars` owns the rows, the pins and the
budget; this module owns only the AD-1 dual-channel wrapping, the identity and
the scope refusal -- the same seam the three tools beside it use.

DECLARED (AD-43) `insights` / `read` / `operational` / no confirmation: it is a
governed-context read like its three neighbours. It writes nothing at all.

THE SUBJECT IS RESOLVED BEFORE THE STORE IS OPENED. The resilience path below
turns any read failure into a stated `unavailable`; a refusal swallowed by it
would be a guard that never refuses, so an unknown `subject_type` is answered
before the connection exists.

`core.main` imports this module, so `_envelope` and `_resolve_project` are
imported inside the body -- the same cycle-and-patch seam every extracted
surface uses. The scope guard comes from `core.mcp_scope`, at module level, so a
suite can patch it here without reaching into a 5 000-line file.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth import AccessToken
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core.exemplars import DEFAULT_BUDGET_TOKENS
from core.mcp_scope import refuse_unless_project_scope

logger = logging.getLogger(__name__)

#: AD-1 / NFR1: the summary never exceeds this, so the heavy detail cannot enter
#: the model's context by the back door.
_MAX_SUMMARY_LINES = 30


def _ai_settings_block(identity: str, project_id: str) -> dict | None:
    """`meta.ai_settings` for this door, through the ONE helper the surface uses.

    Deferred import, like every other reach into a sibling surface here:
    `core.main` imports this module, and the helper reaches back through
    `core.db`. One helper and not a copy, so the four agent doors cannot drift
    into two ways of resolving the same cascade.
    """
    from core.agent_surface_mcp import _ai_settings_block as _block  # noqa: PLC0415

    return _block(identity, project_id)


def _identity() -> str:
    """The authenticated caller, or `anonymous` when the host sent no token."""
    from core.main import get_access_token  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _summary(payload: dict) -> str:
    """The bounded LLM channel: what is served, what is pinned, what was cut."""
    subject = payload["subject"]
    if payload["exemplar_store_state"] != "available":
        # NOT "no exemplars". A model must be able to tell an outage from a
        # subject nobody has written an exemplar for: one is missing evidence,
        # the other is a fact about the Project.
        return (
            f"The governed exemplar store could not be read, so nothing can be said about "
            f"{subject['type']} '{subject['id']}'. Treat this as missing evidence, NOT as a "
            f"subject without exemplars."
        )
    if not payload["exemplars"]:
        return (
            f"No approved exemplar for {subject['type']} '{subject['id']}' "
            f"(only active Golden Questions are served)."
        )

    lines = [
        f"{payload['served']} approved exemplar(s) for {subject['type']} "
        f"'{subject['id']}', most recently stewarded first:"
    ]
    budget = _MAX_SUMMARY_LINES - 1
    for exemplar in payload["exemplars"]:
        if len(lines) >= budget:
            break
        question = " ".join((exemplar.get("question") or "").split())
        paths = len(exemplar["approved_paths"])
        lines.append(
            f"[{exemplar['expected_answer_shape']['result_type']}] {question} "
            f"— v{exemplar['version_number']}, {paths} approved path(s)"
        )
    if payload["truncated"]:
        # The two cuts are named apart because only ONE of them is the caller's
        # to lift: a bigger budget cannot reach past `max_exemplars`, and a
        # caller told only "left out" would raise the budget forever.
        cuts = []
        if payload["omitted_for_budget"]:
            cuts.append(
                f"{payload['omitted_for_budget']} left out of a "
                f"{payload['budget_tokens']}-token budget — raise budget_tokens"
            )
        if payload["omitted_beyond_max_exemplars"]:
            cuts.append(
                f"{payload['omitted_beyond_max_exemplars']} beyond the "
                f"{payload['max_exemplars']}-exemplar server bound — narrow the subject"
            )
        lines.append(f"[+{payload['omitted_total']} not shown: " + "; ".join(cuts) + "]")
    if payload.get("trimmed_for_budget"):
        # The cut INSIDE an exemplar. Left unsaid, a model reads a trimmed
        # expected shape as the whole of it and imitates an exemplar nobody wrote.
        lines.append(
            "[one exemplar was trimmed to fit the model channel: its `*_omitted` "
            "counters say what was cut — the pinned version holds the whole of it]"
        )
    return "\n".join(lines[:_MAX_SUMMARY_LINES])


def get_exemplars(
    subject_type: str,
    subject_id: str,
    project_id: str = "default",
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> ToolResult:
    """Approved exemplars for a metric, Semantic View or topic (AD-14 identity).

    The ACTIVE Golden Questions here -- question, expected path, expected
    answer shape -- plus the walks judged `pass` on that exact version.
    Drafts, deprecated and archived are never served, every identity is an
    exact version pin, and every cut says so.

    Parameters:
        subject_type:  'metric', 'view' or 'topic'.
        subject_id:    metric id / view (or version) id / topic key.
        project_id:    Project identifier (default: 'default').
        budget_tokens: Budget, clamped to the model channel.
    """
    from core import db as _core_db  # noqa: PLC0415
    from core import exemplars as _exemplars  # noqa: PLC0415
    from core.main import _envelope, _resolve_project  # noqa: PLC0415

    identity = _identity()

    # BEFORE the store, and before the scope resolution: a subject nobody can
    # name is the caller's typo, and answering it with `project_not_found` would
    # tell a caller its project is gone when its argument is.
    try:
        subject_type, subject_id = _exemplars.normalize_subject(subject_type, subject_id)
    except _exemplars.ExemplarSubjectRefused as refused:
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        raise ToolError(f"{refused.code}: {refused.message}") from refused

    budget = _exemplars.clamp_budget(budget_tokens)
    project_id = _resolve_project(project_id, identity)
    # Outside the try, exactly as `get_procedure` places it: the resilience path
    # below turns any exception into a stated `unavailable`, and a refusal
    # swallowed by it would be a guard that never refuses.
    refuse_unless_project_scope(project_id, identity)

    try:
        # ARMED CONNECTION (`core/db.py`): the connection that READS is the one
        # that carries the access context. The row-level floor is the second
        # barrier, and it is down on a connection nobody armed.
        with _core_db.request_connection(identity) as conn:
            org_id = _exemplars.org_for_project(conn, project_id)
            if org_id is None:
                payload = _exemplars.unavailable(subject_type, subject_id, project_id, budget)
            else:
                payload = _exemplars.list_exemplars(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    budget_tokens=budget,
                )
    except Exception as exc:  # noqa: BLE001 -- degrade, but never as "empty"
        logger.debug("get_exemplars: store_unavailable: %s", type(exc).__name__)
        payload = _exemplars.unavailable(subject_type, subject_id, project_id, budget)

    payload["identity"] = identity
    envelope = _envelope(
        payload,
        provenance={
            "source_system": "connector-core",
            "source_field": "get_exemplars",
            "pull_id": None,
        },
        freshness="live",
        # Story 75-4, on the fourth door like on the three beside it: the rules
        # a model must obey, and the scope each came from. Its own armed
        # connection, opened AFTER this tool's own work, so a failure there can
        # neither roll this read back nor hide it -- the block is absent, never
        # fabricated. Its weight is already paid for: `ENVELOPE_RESERVE_BYTES`
        # holds 1024 bytes for it, which is why the list budget is 512 tokens.
        ai_settings=_ai_settings_block(identity, project_id),
    )
    return ToolResult(
        content=[TextContent(type="text", text=_summary(payload))],
        structured_content=envelope,
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind `get_exemplars` on the given FastMCP app, declared like its neighbours."""
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_exemplars,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
