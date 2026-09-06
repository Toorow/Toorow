"""toorow -- the Test DEFINITION door on MCP: what this Project calls trustworthy.

WHY IT EXISTS. The gap audit of 2026-09-05 measured the whole of the Test space
as console-only on its verbs (`reviews/audit-2026-09-05-gap/00-synthese.md`, 2):
of the eighteen gestures `user-bridge.md` 5 ratifies, ten are green on component,
API and UI and dark on MCP, and three of those ten ARE the Test. The row for this
one read, verbatim: *"define and version what 'trustworthy' means -- none.
`analyze_render_mcp` READS `app.golden_questions` as query context; no tool
creates, versions or retires one."* A model could be judged by a Golden Question
and could not write one.

TWO MODULES, TWO RESPONSIBILITIES. `evaluation_mcp.py` is the Test READ door --
what has been evaluated here and what did it conclude. This module is the Test
DEFINITION door -- what the Project holds as the measure in the first place. They
are separated the way `module-boundaries.md` separates by responsibility and not
by size: a question's definition and a run's evidence are repaired on different
days, by different people, against different tables.

THREE TOOLS, THREE QUESTIONS (`mcp-tool-surface.md`, "un outil qui n'a pas de
question a lui n'a pas de place"):

  * ``list_golden_questions``            -- Insights, READ. What does this Project
                                            call trustworthy, what does each head
                                            pin, and -- on request -- which
                                            governed domains and Semantic View
                                            versions a new version may pin.
  * ``publish_golden_question_version``  -- Governance, WRITE, confirmation "human".
                                            Validate a definition and land it: a
                                            new head plus version 1 when no id is
                                            given, a new version of an existing
                                            head when one is.
  * ``set_golden_question_lifecycle``    -- Governance, WRITE, confirmation "human".
                                            Stop trusting a question, or trust it
                                            again. Declared transitions only.

ONE STATE, TWO DOORS (AD-1). Every tool here calls `core.golden_questions` -- the
same module `golden_questions_api.py` translates HTTP into, and the same one the
console's Golden Questions screen drives. No validation, no SQL, no lifecycle
table and no hash lives in this file. Re-implementing any of them would produce a
second answer to "is this definition acceptable", which is the exact defect the
single service exists to remove.

WHY THE READ IS `insights` AND THE WRITES ARE `governance`. The read is the rank
its neighbour `get_evaluation_runs` already holds: reading what the Project holds
as its measure mutates nothing and is the safe default. The writes decide what
every future run will be judged against -- the rank of `publish_semantic_model_change`
and `publish_shared_identity`, for the same reason, and the ceiling moves in the
same commit as the declaration (`mcp-tool-surface.md`, amendment of 2026-09-05).

WHAT IS NOT BUILT, AND SAID SO. Recording a case verdict, opening a run, executing
it, finalizing it, approving a baseline and emitting a gate decision are still
console-only. `evaluation_mcp.py` says the same about its half. A tool that cannot
answer must not exist while a docstring names it.

Conventions mirror `evaluation_mcp` and `governance_mcp`: lazy `core.*` imports
inside function bodies (no cycle with `core.main`), ASCII-only source, one
canonical existence-hiding refusal, and registration through `register_profiled`
(AD-42/AD-43).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

#: How many heads ride one answer. The collection is small by design -- a Project
#: that has hundreds of Golden Questions has a taxonomy problem, not a paging
#: problem -- but an unbounded read still spends the caller's window.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

#: How many lines the text channel carries. The full payload rides
#: structuredContent -- the AD-1 split every door in this repository holds.
_SUMMARY_MAX_LINES = 12


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
    """The organization this Project belongs to.

    Read through `context_api._project_org_id`, the single reader
    `evaluation_mcp` and `context_hub_mcp` already borrow. Two readers of
    `app.projects.org_id` is how two doors start disagreeing about which tenant
    a Project belongs to.
    """
    from core.context_api import _project_org_id  # noqa: PLC0415

    return _project_org_id(conn, project_id)


def _refused(exc) -> None:
    """Turn a `GoldenQuestionRefused` into a ToolError carrying every reason.

    The console answers 422 with the full structured reason list because "a
    caller that cannot see why it was refused will guess"
    (`golden_questions_api.py:1-20`). A model guesses harder than a person, so
    the reasons travel here too -- subject and message, never a bare code.
    """
    reasons = "; ".join(
        f"{getattr(r, 'subject', None) or 'definition'}: {getattr(r, 'message', '')}"
        for r in (getattr(exc, "refusals", None) or [])
    )
    raise _tool_error(
        str(getattr(exc, "code", "refused") or "refused"),
        (str(exc) or "The definition was refused.") + (f" -- {reasons}" if reasons else ""),
    )


def _heads_summary(project_id: str, heads: list[dict], options: dict | None) -> str:
    """One line per head, and an empty list that names the gesture that fills it."""
    if not heads:
        pins = ""
        if options:
            domains = len(options.get("business_domains") or [])
            views = len(options.get("semantic_views") or [])
            pins = (
                f" {domains} governed Business Domain(s) and {views} pinnable "
                "Semantic View version(s) are available to pin."
            )
        return (
            f"No Golden Question in project {project_id!r}. Publish one with "
            "`publish_golden_question_version` (title, owner and a definition that "
            "pins a Business Domain version and a Semantic View version): a "
            "question is what every future run is judged against."
            + pins
        )
    lines = [f"{len(heads)} Golden Question(s) in {project_id!r}:"]
    for head in heads[: _SUMMARY_MAX_LINES - 1]:
        version = head.get("current_version") or {}
        lines.append(
            f"- {head.get('id')} [{head.get('lifecycle')}] {head.get('title')!r}"
            f" owner={head.get('owner')}"
            f" v{version.get('version_number')}"
            f" domain={version.get('business_domain_name') or version.get('business_domain_id')}"
            f" severity={version.get('severity')}"
        )
    hidden = len(heads) - (len(lines) - 1)
    if hidden > 0:
        lines.append(f"[+{hidden} more in the detail]")
    return "\n".join(lines)


def list_golden_questions(
    project_id: str,
    golden_question_id: str | None = None,
    lifecycle: str | None = None,
    options: bool = False,
    limit: int | None = None,
):
    """What this Project calls trustworthy, and what each question pins.

    Without `golden_question_id`: the heads, newest first, each with its
    lifecycle, its owner and the pins of its current version -- Business Domain
    and version, Semantic View version, result type, severity, capability tags.
    With `golden_question_id`: that head with its full version history and the
    reference paths each version declares.

    `lifecycle` filters to one of draft, active, deprecated, archived.
    `options=true` adds the governed choices a new version may pin -- the active
    Business Domains with their latest version, their classifications, and the
    Semantic View versions in a pinnable status -- read from the same tables the
    validator reads, so what this door offers and what it accepts cannot drift.

    Read-only. Publishing a version is `publish_golden_question_version`;
    retiring a question is `set_golden_question_lifecycle`.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import (
        caller_identity,  # noqa: PLC0415
        refuse_unless_project_scope,  # noqa: PLC0415
    )

    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    identity = caller_identity()
    refuse_unless_project_scope(checked, identity)

    from core.golden_questions import (  # noqa: PLC0415
        LIFECYCLES,
        GoldenQuestionNotFound,
        get_golden_question,
        golden_question_options,
    )
    from core.golden_questions import (  # noqa: PLC0415
        list_golden_questions as _list_heads,
    )

    wanted_lifecycle = (lifecycle or "").strip() or None
    if wanted_lifecycle is not None and wanted_lifecycle not in LIFECYCLES:
        raise _tool_error(
            "invalid_param",
            "lifecycle must be one of: " + ", ".join(LIFECYCLES) + ".",
        )
    bounded = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    wanted = (golden_question_id or "").strip() or None

    try:
        with request_connection(identity) as conn:
            org_id = _org_of(conn, checked)
            choices = (
                golden_question_options(conn, org_id=org_id, project_id=checked)
                if options
                else None
            )
            if wanted:
                head = get_golden_question(
                    conn, org_id=org_id, project_id=checked, golden_question_id=wanted
                )
                versions = head.get("versions") or []
                summary = (
                    f"{head.get('title')!r} [{head.get('lifecycle')}] owner="
                    f"{head.get('owner')}, {len(versions)} version(s). "
                    "A version is immutable: correcting a definition publishes the next one."
                )
                data = {"project_id": checked, "golden_question": head}
                if choices is not None:
                    data["options"] = choices
                return _result(summary, data)
            heads = _list_heads(
                conn, org_id=org_id, project_id=checked, lifecycle=wanted_lifecycle
            )
    except GoldenQuestionNotFound as exc:
        raise _tool_error(
            "golden_question_not_found", "Golden Question not found in this Project."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("golden_question_mcp: read failed: %s", type(exc).__name__)
        raise _tool_error(
            "seam_unavailable", "Golden Questions are unavailable."
        ) from exc

    page = heads[:bounded]
    data = {
        "project_id": checked,
        "golden_questions": page,
        "listed": len(page),
        "total": len(heads),
    }
    if choices is not None:
        data["options"] = choices
    return _result(_heads_summary(checked, page, choices), data)


def publish_golden_question_version(
    project_id: str,
    definition: dict,
    title: str | None = None,
    owner: str | None = None,
    golden_question_id: str | None = None,
):
    """Publish what "trustworthy" means here -- a new question, or its next version.

    Without `golden_question_id`: a new head is created with `title` and `owner`,
    carrying this definition as version 1. With one: this definition becomes the
    next version of that head, and the head's pointer advances. A published
    version is immutable -- correcting a definition publishes the next one, it
    never edits the last.

    `definition` is the same object the console's Golden Questions dialog posts
    and the same one `validate_golden_question_version` judges: the Business
    Domain and its version, the Semantic View version, the result type, the
    severity, the typed expected result, the required provenance, the expected
    AI Path when one is declared, and the capability tags. It is validated
    before anything is written; a refusal names every reason with its subject
    rather than a single code.

    The pins a definition may name are served by
    `list_golden_questions(options=true)` -- the active Business Domains with
    their latest version and the Semantic View versions in a pinnable status.
    """
    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    if not isinstance(definition, dict) or not definition:
        raise _tool_error(
            "missing_param",
            "definition is required: the pinned object the version judges "
            "(call list_golden_questions with options=true for the governed choices).",
        )
    head_id = (golden_question_id or "").strip() or None
    wanted_title = (title or "").strip()
    wanted_owner = (owner or "").strip()
    if head_id is None and not (wanted_title and wanted_owner):
        raise _tool_error(
            "missing_param",
            "A new Golden Question needs title and owner; pass golden_question_id "
            "instead to publish the next version of an existing one.",
        )

    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import caller_identity  # noqa: PLC0415

    identity = caller_identity()
    if not identity or identity == "anonymous":
        raise _tool_error("project_not_found", "Project not found or archived.")

    from core.golden_questions import (  # noqa: PLC0415
        GoldenQuestionNotFound,
        GoldenQuestionRefused,
        create_golden_question,
        create_golden_question_version,
        validate_golden_question_version,
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
            logger.error("golden_question_mcp: access resolution failed: %s", exc)
            raise _tool_error(
                "project_not_found", "Project not found or archived."
            ) from exc
        if not decision.allowed or not decision.org_id:
            raise _tool_error("project_not_found", "Project not found or archived.")
        org_id = str(decision.org_id)

        try:
            validated = validate_golden_question_version(
                conn, org_id=org_id, project_id=checked, payload=definition
            )
            if head_id is None:
                landed = create_golden_question(
                    conn,
                    org_id=org_id,
                    project_id=checked,
                    title=wanted_title,
                    owner=wanted_owner,
                    validated=validated,
                    actor=identity,
                )
                created = True
            else:
                landed = create_golden_question_version(
                    conn,
                    org_id=org_id,
                    project_id=checked,
                    golden_question_id=head_id,
                    validated=validated,
                    actor=identity,
                )
                created = False
            conn.commit()
        except GoldenQuestionNotFound as exc:
            raise _tool_error(
                "golden_question_not_found",
                "Golden Question not found in this Project.",
            ) from exc
        except GoldenQuestionRefused as exc:
            _refused(exc)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error(
                "golden_question_mcp: publish failed: %s", type(exc).__name__
            )
            raise _tool_error(
                "seam_unavailable", "The Golden Question could not be published."
            ) from exc

    # `create_golden_question` and `create_golden_question_version` return the
    # SAME flat shape -- golden_question_id, version_id, version_number,
    # content_hash, created_at -- so one reader serves both and neither invents
    # a key the service does not write.
    landed_id = landed.get("golden_question_id")
    summary = (
        (
            f"Golden Question {landed_id} created in {checked!r} with version "
            f"{landed.get('version_number')}."
            if created
            else f"Version {landed.get('version_number')} published on Golden "
            f"Question {landed_id}."
        )
        + " A published version is immutable: the next correction is the next version."
    )
    return _result(summary, {"project_id": checked, "created": created, **landed})


def set_golden_question_lifecycle(
    project_id: str,
    golden_question_id: str,
    lifecycle: str,
    owner: str | None = None,
):
    """Stop trusting a Golden Question, or trust it again.

    `lifecycle` is one of draft, active, deprecated, archived, and only the
    declared transitions are accepted: draft goes to active or archived, active
    to deprecated or archived, deprecated back to active or to archived.
    `archived` is TERMINAL -- a question whose history was retired is superseded
    by a new question, never resurrected under the same identity, or a past run's
    subject silently comes back to life.

    `owner` re-assigns stewardship in the same act; omitted, the current owner
    stands. The change is audited under the actor that called this door.
    """
    checked = (project_id or "").strip()
    head_id = (golden_question_id or "").strip()
    wanted = (lifecycle or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    if not head_id:
        raise _tool_error("missing_param", "golden_question_id is required.")

    from core.db import request_connection  # noqa: PLC0415
    from core.golden_questions import (  # noqa: PLC0415
        LIFECYCLES,
        GoldenQuestionNotFound,
        GoldenQuestionRefused,
        set_lifecycle,
    )
    from core.mcp_scope import caller_identity  # noqa: PLC0415

    if wanted not in LIFECYCLES:
        raise _tool_error(
            "invalid_param",
            "lifecycle must be one of: " + ", ".join(LIFECYCLES) + ".",
        )

    identity = caller_identity()
    if not identity or identity == "anonymous":
        raise _tool_error("project_not_found", "Project not found or archived.")

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
            logger.error("golden_question_mcp: access resolution failed: %s", exc)
            raise _tool_error(
                "project_not_found", "Project not found or archived."
            ) from exc
        if not decision.allowed or not decision.org_id:
            raise _tool_error("project_not_found", "Project not found or archived.")

        try:
            moved = set_lifecycle(
                conn,
                org_id=str(decision.org_id),
                project_id=checked,
                golden_question_id=head_id,
                lifecycle=wanted,
                actor=identity,
                owner=(owner or "").strip() or None,
            )
            conn.commit()
        except GoldenQuestionNotFound as exc:
            raise _tool_error(
                "golden_question_not_found",
                "Golden Question not found in this Project.",
            ) from exc
        except GoldenQuestionRefused as exc:
            _refused(exc)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error(
                "golden_question_mcp: lifecycle change failed: %s", type(exc).__name__
            )
            raise _tool_error(
                "seam_unavailable", "The lifecycle could not be changed."
            ) from exc

    terminal = (
        " `archived` is terminal: this question cannot return."
        if wanted == "archived"
        else ""
    )
    return _result(
        f"Golden Question {head_id} is now `{wanted}`, owner {moved.get('owner')}."
        + terminal,
        {"project_id": checked, **moved},
    )


def register(mcp) -> None:
    """Register the Test definition door on *mcp*.

    Called once from `core.main` BEFORE `validate_catalog()`.

    REGISTERED ONE BY ONE, AND NOT IN A LOOP, for the reason `evaluation_mcp`
    states: `scripts/check_legacy_analytics_migration.py` derives a tool's
    identity from the AST of the `register_profiled` call, and a loop variable
    lands every tool in the census under one locator -- one identity for three
    doors. A tool that cannot be named in the census cannot be tracked in it.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        list_golden_questions,
        # The rank its neighbour `get_evaluation_runs` holds: reading what the
        # Project holds as its measure transitions no domain state.
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        publish_golden_question_version,
        # A definition decides what every future run is judged against: the rank
        # of `publish_semantic_model_change` and `publish_shared_identity`, for
        # the same reason (`mcp-tool-surface.md`, 2026-09-05).
        profile="governance",
        effect="confirmed_write",
        data_class="sensitive",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        set_golden_question_lifecycle,
        # Retiring a question retires the measure. `archived` is terminal, so the
        # ceremony is the one a terminal act carries everywhere else here.
        profile="governance",
        effect="confirmed_write",
        data_class="sensitive",
        confirmation_mode="human",
    )
