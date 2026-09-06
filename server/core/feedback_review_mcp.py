"""toorow -- the Test REVIEW door on MCP: read a reaction back, and answer it.

WHY IT EXISTS. The gap audit of 2026-09-05 measured the whole of the Test space
as console-only on its verbs, and this was the last of its three rows still dark
(`reviews/audit-2026-09-05-gap/00-synthese.md`, 2). Verbatim: *"re-read an
explicit reaction in its exact context of result, render and trace --
`submit_feedback` WRITES one; `submit_analyze_feedback` is app-only. No tool
reads a feedback back, classifies it or resolves it."* A model could leave a
reaction on a figure it produced and never read one -- not its own, not anyone
else's -- so the loop the Widget Feedback screen exists to close was open on the
plane the product is built for.

TWO TOOLS, TWO QUESTIONS (`mcp-tool-surface.md`, "un outil qui n'a pas de
question a lui n'a pas de place"):

  * ``read_feedback``    -- Insights, READ. What was reacted to here, and what
                            was ONE reaction reacting to -- its pins, its lenses,
                            its links and its review head. Four lenses, because
                            the screen answers four questions and a model that
                            can only list gets the shallowest of them.
  * ``review_feedback``  -- Operations, WRITE, confirmation "human". Answer one
                            reaction: its state, the dimension it bears on, the
                            human verdict, the severity and the reason. Every
                            review is an IMMUTABLE VERSION appended to a head,
                            never an edit.

ONE STATE, TWO DOORS (AD-1). Every tool here calls `core.feedback_review` -- the
same module `feedback_review_api.py` translates HTTP into and the Widget Feedback
screen drives. No SQL, no vocabulary, no cursor and no hash lives in this file.
The closed vocabularies (`REVIEW_STATES`, `AFFECTED_DIMENSIONS`,
`HUMAN_VERDICTS`, `SEVERITIES`) are READ from that module rather than copied: a
copy ages, and the day it aged this door would accept a state the screen
refuses.

WHY THE READ IS `insights` AND THE WRITE IS `operations`. Reading a reaction back
is the rank its neighbours `get_evaluation_runs` and `list_golden_questions`
hold. Answering one is the rank the console guards it at -- `member`, the same
rank as opening and unrolling a run -- and not `governance`: a review triages a
reaction, it does not decide what later work is measured against. A review is
still `confirmed_write` with a `human` confirmation, because a reaction answered
by nobody is not answered.

WHAT IS NOT BUILT, AND SAID SO. **Leaving a reaction is not here.**
`submit_feedback` already does that from the widget, with the server-minted
handle the append needs, and a second writer of the same table on a different
path would be exactly the drift AD-1 forbids. Nor does this door delete or edit a
review: the head only ever gains a version.

Conventions mirror `evaluation_mcp` and `golden_question_mcp`: lazy `core.*`
imports inside function bodies (no cycle with `core.main`), ASCII-only source, one
canonical existence-hiding refusal, and registration through `register_profiled`
(AD-42/AD-43).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

#: How many reactions ride one answer. The collection is append-only and grows
#: with every widget a host renders; an unbounded read spends the caller's window
#: on history it did not ask for.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100

#: How many lines the text channel carries. The payload rides structuredContent.
_SUMMARY_MAX_LINES = 12

#: The four questions the Widget Feedback screen answers, and the only values
#: this door accepts. A fifth lens is a fifth question and gets refused by name
#: rather than falling through to the safest branch.
_LENSES = ("reactions", "aggregates", "critical-negatives", "reviews")


def _envelope(data: dict) -> dict:
    """Build the canonical AD-1 structured_content envelope."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": "live", "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _result(summary: str, data: dict):
    """Build the dual-channel ToolResult (lean text summary + AD-1 envelope)."""
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


def _org_of(conn, project_id: str) -> str:
    """The organization this Project belongs to -- the single reader, borrowed."""
    from core.context_api import _project_org_id  # noqa: PLC0415

    return _project_org_id(conn, project_id)


def _refused(exc) -> None:
    """A refusal travels with every reason and its subject, never a bare code."""
    reasons = "; ".join(
        f"{getattr(r, 'subject', None) or 'review'}: {getattr(r, 'message', '')}"
        for r in (getattr(exc, "refusals", None) or [])
    )
    raise _tool_error(
        str(getattr(exc, "code", "refused") or "refused"),
        (str(exc) or "The review was refused.") + (f" -- {reasons}" if reasons else ""),
    )


def _vocabularies() -> dict[str, tuple[str, ...]]:
    """The closed vocabularies, READ from the service and never copied here.

    A copy ages, and the day it aged this door would accept a state the screen
    refuses -- two answers to "is this review acceptable", which is the defect the
    single service exists to remove.
    """
    from core import feedback_review as service  # noqa: PLC0415

    return {
        "state": tuple(service.REVIEW_STATES),
        "affected_dimension": tuple(service.AFFECTED_DIMENSIONS),
        "human_verdict": tuple(service.HUMAN_VERDICTS),
        "severity": tuple(service.SEVERITIES),
    }


def _reactions_summary(project_id: str, page: dict) -> str:
    """One line per reaction, and an empty page that names the gesture."""
    items = page.get("items") or page.get("annotations") or []
    if not items:
        return (
            f"No reaction recorded in project {project_id!r}. A reaction is left "
            "from the widget a tool mounted, through `submit_feedback` and the "
            "handle that tool minted: nothing here creates one."
        )
    lines = [f"{len(items)} reaction(s) in {project_id!r}:"]
    for item in items[: _SUMMARY_MAX_LINES - 1]:
        review = item.get("review_head") or {}
        lines.append(
            f"- {item.get('id')} [{item.get('polarity') or 'no polarity'}]"
            f" on {item.get('target_kind')} {item.get('target_id')}"
            f" review={review.get('state') or 'unreviewed'}"
        )
    hidden = len(items) - (len(lines) - 1)
    if hidden > 0:
        lines.append(f"[+{hidden} more in the detail]")
    return "\n".join(lines)


def read_feedback(
    project_id: str,
    feedback_id: str | None = None,
    lens: str | None = None,
    polarity: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
):
    """Read a reaction back, in the exact context it was left in.

    With `feedback_id`: ONE reaction with everything that makes it readable --
    the Result, Render and trace it was pinned to, the lenses it names, its links,
    and the head of its review history. This is what "in its exact context" means:
    a reaction read without its pins is an opinion about nothing.

    Without an id, `lens` chooses the question:
      * `reactions` (default) -- the collection, newest first, each with its
        review state; `polarity` narrows it to one sign;
      * `aggregates` -- the counts, per target and per lens;
      * `critical-negatives` -- the negatives whose review has NOT reached a
        resolved state, which is the list a person works from;
      * `reviews` -- the immutable review versions of the reaction named by
        `feedback_id`, oldest first.

    `limit` bounds a collection (default 20, maximum 100); `cursor` pages it.

    Read-only. Answering a reaction is `review_feedback`; LEAVING one is not here
    at all -- `submit_feedback` does that from the widget that minted its handle.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import (
        caller_identity,  # noqa: PLC0415
        refuse_unless_project_scope,  # noqa: PLC0415
    )

    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    wanted = (feedback_id or "").strip() or None
    chosen = (lens or "").strip() or ("reviews" if wanted and lens else "reactions")
    if chosen not in _LENSES:
        raise _tool_error(
            "unknown_lens", "lens is one of: " + ", ".join(_LENSES) + "."
        )
    if chosen == "reviews" and not wanted:
        raise _tool_error(
            "missing_param",
            "the reviews lens reads the history of ONE reaction: name feedback_id.",
        )
    identity = caller_identity()
    refuse_unless_project_scope(checked, identity)

    from core.feedback_review import (  # noqa: PLC0415
        FeedbackNotFound,
        aggregate_feedback,
        get_annotation,
        list_annotations,
        list_review_versions,
        list_unresolved_critical_negatives,
    )

    bounded = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    page_cursor = (cursor or "").strip() or None

    try:
        with request_connection(identity) as conn:
            org_id = _org_of(conn, checked)
            if wanted and chosen != "reviews":
                one = get_annotation(
                    conn, org_id=org_id, project_id=checked, feedback_id=wanted
                )
                review = one.get("review_head") or {}
                summary = (
                    f"Reaction {wanted} [{one.get('polarity') or 'no polarity'}] on "
                    f"{one.get('target_kind')} {one.get('target_id')}, review "
                    f"{review.get('state') or 'unreviewed'}. Its pins are what make "
                    "it readable: a reaction without them is an opinion about nothing."
                )
                return _result(summary, {"project_id": checked, "feedback": one})
            if chosen == "reviews":
                versions = list_review_versions(
                    conn,
                    org_id=org_id,
                    project_id=checked,
                    feedback_id=wanted,
                    limit=bounded,
                )
                summary = (
                    f"{len(versions)} review version(s) on reaction {wanted}. "
                    "Every one is immutable: answering again appends, never edits."
                    if versions
                    else f"Reaction {wanted} has never been reviewed."
                )
                return _result(
                    summary,
                    {"project_id": checked, "feedback_id": wanted, "reviews": versions},
                )
            if chosen == "aggregates":
                page = aggregate_feedback(
                    conn,
                    org_id=org_id,
                    project_id=checked,
                    limit=bounded,
                    cursor=page_cursor,
                )
                rows = page.get("items") or []
                summary = (
                    f"{len(rows)} aggregate row(s) in {checked!r}, per target and lens."
                    if rows
                    else f"Nothing has been reacted to yet in {checked!r}."
                )
                return _result(summary, {"project_id": checked, "lens": chosen, **page})
            if chosen == "critical-negatives":
                page = list_unresolved_critical_negatives(
                    conn, org_id=org_id, project_id=checked, limit=bounded, cursor=page_cursor
                )
                rows = page.get("items") or []
                summary = (
                    f"{len(rows)} critical negative(s) whose review has not reached a "
                    "resolved state -- this is the list a person works from."
                    if rows
                    else "No unresolved critical negative here."
                )
                return _result(summary, {"project_id": checked, "lens": chosen, **page})
            page = list_annotations(
                conn,
                org_id=org_id,
                project_id=checked,
                limit=bounded,
                cursor=page_cursor,
                polarity=(polarity or "").strip() or None,
            )
    except FeedbackNotFound as exc:
        raise _tool_error(
            "feedback_not_found", "Feedback not found in this Project."
        ) from exc
    except ValueError as exc:
        raise _tool_error("invalid_param", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("feedback_review_mcp: read failed: %s", type(exc).__name__)
        raise _tool_error("seam_unavailable", "Feedback is unavailable.") from exc

    return _result(
        _reactions_summary(checked, page), {"project_id": checked, "lens": chosen, **page}
    )


def review_feedback(project_id: str, feedback_id: str, review: dict):
    """Answer one reaction -- and append a version, never edit one.

    `review` carries what a review IS, and each field is a closed vocabulary the
    service owns rather than this door:
      * `state` -- unreviewed, triaged, accepted, rejected, duplicate, resolved;
      * `affected_dimension` -- which dimension of the answer the reaction bears
        on: semantic_correctness, provenance_correctness, context_adherence,
        path_quality, dq_handling, mcp_app_behavior, not_applicable;
      * `human_verdict` -- pass, fail, unverifiable, not_applicable;
      * `severity` -- critical, major, minor;
      * `reason` -- the sentence, mandatory: a verdict nobody explained cannot be
        re-read later by the person who has to act on it;
      * `retry_key` -- mandatory, so a retried answer REPLAYS the same version
        instead of appending a second one;
      * `expected_head` -- optional; when given, the append is refused if another
        reviewer answered first, so two people never overwrite each other blind.

    Every review is an immutable version on the reaction's head. Nothing here
    deletes or edits one, and nothing here LEAVES a reaction: `submit_feedback`
    does that from the widget that minted its handle.

    A refusal names every reason with its subject rather than a single code.
    """
    checked = (project_id or "").strip()
    target = (feedback_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    if not target:
        raise _tool_error("missing_param", "feedback_id is required.")
    if not isinstance(review, dict) or not review:
        raise _tool_error(
            "missing_param",
            "review is required: state, affected_dimension, human_verdict, "
            "severity, reason and retry_key.",
        )

    from core.db import request_connection  # noqa: PLC0415
    from core.feedback_review import (  # noqa: PLC0415
        FeedbackNotFound,
        FeedbackRefused,
        append_review_version,
    )
    from core.mcp_scope import caller_identity  # noqa: PLC0415

    identity = caller_identity()
    if not identity or identity == "anonymous":
        raise _tool_error("project_not_found", "Project not found or archived.")

    # The closed vocabularies are checked HERE only to name the whole set in the
    # refusal -- the service checks them again and is the authority. A model that
    # is told `state must be one of ...` retries correctly; one told `refused`
    # guesses.
    vocabularies = _vocabularies()
    for field, allowed in vocabularies.items():
        value = review.get(field)
        if allowed and value is not None and str(value) not in allowed:
            raise _tool_error(
                "invalid_param",
                f"review.{field} must be one of: " + ", ".join(allowed) + ".",
            )
    if not str(review.get("retry_key") or "").strip():
        raise _tool_error(
            "missing_param",
            "review.retry_key is required so a retried answer replays the same "
            "version instead of appending a second one.",
        )

    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.semantic_model_api import _WRITE_CAPABILITY  # noqa: PLC0415

    with request_connection(identity) as conn:
        try:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=checked,
                minimum_capability=_WRITE_CAPABILITY,
                hold_access=True,
            )
        except Exception as exc:  # noqa: BLE001 -- an outage fails CLOSED
            logger.error("feedback_review_mcp: access resolution failed: %s", exc)
            raise _tool_error(
                "project_not_found", "Project not found or archived."
            ) from exc
        if not decision.allowed or not decision.org_id:
            raise _tool_error("project_not_found", "Project not found or archived.")

        try:
            appended = append_review_version(
                conn,
                org_id=str(decision.org_id),
                project_id=checked,
                feedback_id=target,
                reviewer=identity,
                payload=review,
            )
            conn.commit()
        except FeedbackNotFound as exc:
            raise _tool_error(
                "feedback_not_found", "Feedback not found in this Project."
            ) from exc
        except FeedbackRefused as exc:
            _refused(exc)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("feedback_review_mcp: review failed: %s", type(exc).__name__)
            raise _tool_error(
                "seam_unavailable", "The review could not be appended."
            ) from exc

    replayed = appended.get("status") == "replayed"
    summary = (
        f"Reaction {target} answered `{review.get('state')}` "
        f"({review.get('human_verdict')}, {review.get('severity')})."
        + (
            " This retry key had already been answered: the same version was "
            "replayed, not appended twice."
            if replayed
            else " The review is an immutable version on the head; answering "
            "again appends, never edits."
        )
    )
    return _result(summary, {"project_id": checked, "feedback_id": target, **appended})


def register(mcp) -> None:
    """Register the Test review door on *mcp*.

    Called once from `core.main` BEFORE `validate_catalog()`.

    REGISTERED ONE BY ONE, AND NOT IN A LOOP, for the reason `evaluation_mcp`
    states: `scripts/check_legacy_analytics_migration.py` derives a tool's
    identity from the AST of the `register_profiled` call, and a loop variable
    lands both tools in the census under one locator.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        read_feedback,
        # The rank its neighbours `get_evaluation_runs` and `list_golden_questions`
        # hold: reading a reaction back transitions no domain state.
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        review_feedback,
        # The rank the console guards it at -- `member`, the rank of opening and
        # unrolling a run. A review triages a reaction; it does not decide what
        # later work is measured against, so it is not `governance`.
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
