"""toorow -- the Test door on MCP: `get_evaluation_runs`, `get_context_adherence`.

WHY IT EXISTS. The audit of 2026-08-17 (`reviews/audit-2026-08-17/09-ai-route.md`,
"Le plan MCP du Test est vide") measured it: no `*_mcp` module existed for
evaluation, gate, cohorts or paths, and the whole Test loop -- runs, cases, six
dimensions, verdicts, adherence -- was reachable from the console and from
nowhere else. For a product whose thesis is "operate from the LLM host", the
judge had no agent door: the model could be measured and could not read what the
measure said about it.

TWO TOOLS, TWO QUESTIONS. `get_evaluation_runs` answers "what has been evaluated
here, and what did it conclude"; `get_context_adherence` answers "did the answers
consult the governed context before querying". They are separate because they are
repaired separately, and because a single tool returning both would be a page
rather than a catalogue entry (`mcp-tool-surface.md`, "un outil qui n'a pas de
question a lui n'a pas de place").

PROFILE `insights`. Both are reads of state that mutate nothing, and Insights is
the profile for safe reads. The alternative considered was a dedicated
`evaluation` profile; the code shows no such profile exists -- `mcp_profiles.PROFILES`
is `(insights, operations, governance, support)` -- and inventing a fifth to hold
two reads would be a second filtering mechanism beside the one AD-43 says must be
the only one. The cost is stated where costs are stated: these two tools enter
the DEFAULT catalogue every host pays for, and the amendment to
`mcp-tool-surface.md` records the new number the measuring command prints.

THE VERBS, SINCE 2026-09-05. This paragraph used to say *"Triggering an
evaluation run from MCP does not exist"*, and it was true for nineteen days. The
gap audit of 2026-09-05 measured what that cost: of the eighteen gestures
`user-bridge.md` 5 ratifies, ten were green on component, API and UI and dark on
MCP, and three of the ten were the Test. This row, verbatim: *"run a pinned
cohort and compare it to an approved baseline -- `get_evaluation_runs`,
`get_context_adherence` -- READS ONLY. Opening, executing, baselining and gating
a run are console-only."* Three verbs now answer, and they are grouped by the
question they answer rather than by the route they call:

  * ``open_evaluation_run``     -- Operations, WRITE. Judge this cohort under
                                   these pins. One act: the run, and the cases
                                   when the caller names them.
  * ``advance_evaluation_run``  -- Operations, WRITE. Move the judgement forward:
                                   `execute` unrolls the question set and freezes
                                   it, `finalize` closes a run that was unrolled
                                   case by case.
  * ``decide_evaluation_run``   -- Governance, WRITE. Decide what the judgement
                                   authorizes: `approve_baseline` says which run
                                   is the standard, `emit_gate_decision` records
                                   evidence for an owner about one candidate.

They are in THIS module and not in one of their own. `module-boundaries.md` cuts
by responsibility, and the Regression Run is one responsibility: splitting its
reads from its verbs would cut by EFFECT, and leave two modules answering for one
object. The Golden Question got its own module the same week for the opposite
reason -- a question's definition and a run's evidence are different objects.

WHAT IS STILL NOT BUILT, AND SAID SO. Creating a run profile and creating a
context version set are console-only: both are `core.evaluation_runs` functions
with no door here, and `open_evaluation_run` refuses by name when the pins they
mint are absent rather than inventing them. Re-reading a widget feedback in its
context is console-only too, and keeps its own `partial` row. A tool that cannot
answer must say so, and the honest form of that is not to exist while a docstring
names it.

Conventions mirror `project_posture_mcp`: lazy `core.*` imports inside function
bodies (no cycle with `core.main`), ASCII-only source, one canonical
existence-hiding refusal, and registration through `register_profiled`
(AD-42/AD-43).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

#: How many runs ride one answer by default. The Regression Runs collection is
#: append-only and grows forever; an unbounded read would spend the caller's
#: window on history it did not ask for.
_DEFAULT_RUN_LIMIT = 20
_MAX_RUN_LIMIT = 100

#: How many lines the text channel carries. The full payload rides
#: structuredContent -- the same AD-1 split the Context Hub discovery tools hold.
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
    """The organization the evaluation read model is scoped on.

    Read through `context_api._project_org_id` -- the same single reader
    `context_hub_mcp` already borrows -- rather than a second SELECT. Two readers
    of `app.projects.org_id` is how two doors start disagreeing about which
    tenant a Project belongs to.
    """
    from core.context_api import _project_org_id  # noqa: PLC0415

    return _project_org_id(conn, project_id)


def _runs_summary(project_id: str, runs: list[dict]) -> str:
    if not runs:
        return (
            f"No evaluation run in project {project_id!r}. Open one from the "
            "Regression Runs screen: a run pins a question set and a context "
            "version set before it judges anything."
        )
    lines = [f"{len(runs)} evaluation run(s) in {project_id!r}:"]
    for run in runs[: _SUMMARY_MAX_LINES - 1]:
        lines.append(
            f"- {run.get('id')} [{run.get('lifecycle')}, {run.get('evidence_mode')}]"
            f" profile={run.get('run_profile')} cases={run.get('case_count')}"
            f" unresolved_pins={run.get('unresolved_pin_count')}"
        )
    hidden = len(runs) - (len(lines) - 1)
    if hidden > 0:
        lines.append(f"[+{hidden} more in the detail]")
    return "\n".join(lines)


def _run_summary(run: dict, cases: dict) -> str:
    counts = run.get("verdict_counts") or {}
    # Per dimension, per verdict -- and never summed. `analyze-and-test.md:210`
    # forbids the merge, and a single pass rate here would be exactly that merge
    # wearing a friendlier number.
    per_dimension = "; ".join(
        f"{dimension}: " + ", ".join(f"{v}={n}" for v, n in sorted(verdicts.items()))
        for dimension, verdicts in sorted(counts.items())
    )
    return (
        f"Run {run.get('id')} [{run.get('lifecycle')}, {run.get('evidence_mode')}] "
        f"{len(cases.get('cases') or [])} case(s), "
        f"{len(run.get('unresolved_pins') or [])} unresolved pin(s).\n"
        f"{per_dimension or 'No verdict recorded yet.'}"
    )


#: How a basis reads in one line of a summary. The reader of this door is a model
#: that will quote the sentence, so the two kinds have to be nameable in it.
_BASIS_LABEL = {
    "observed_session": "observed exchange",
    "inferred_window": "inferred from proximity",
}


def _adherence_summary(payload: dict) -> str:
    """One line per BASIS, and never a line that sums them.

    `analyze-and-test.md:1330` -- a trace-identified session and a wall-clock
    inference "are reported apart and never merged". This door used to print
    `N observation(s), M adherent (share X)` straight off the pooled top-level
    figures, which is the same merge the console was making in its "All
    questions" row. The reader now gets the count of measurements, then each
    basis with its OWN denominator.
    """
    observations = payload.get("observations") or 0
    days = (payload.get("window") or {}).get("days")
    if not observations:
        empty = payload.get("empty_state") or {}
        detail = " ".join(
            part for part in (empty.get("detail"), empty.get("next_step")) if part
        )
        return f"No adherence observation in the last {days} day(s). {detail}".strip()
    lines = [
        f"{observations} observation(s) in the last {days} day(s), "
        "reported apart by kind of evidence -- never summed:"
    ]
    for bucket in payload.get("by_basis") or []:
        label = _BASIS_LABEL.get(bucket.get("basis"), bucket.get("basis"))
        lines.append(
            f"- {label}: {bucket.get('adherent')} of {bucket.get('observations')} "
            "consulted context first"
        )
    return "\n".join(lines)


def get_evaluation_runs(
    project_id: str,
    run_id: str | None = None,
    evidence_mode: str | None = None,
    limit: int | None = None,
    run_profiles: bool = False,
):
    """What has been evaluated in this Project, and what did it conclude.

    Without `run_id`: the Regression Runs collection. With `run_id`: that
    run's overview and its cases, each carrying the six dimensions with their
    verdict, reason code and evidence.

    Verdict counts travel per dimension and per verdict -- there is no pass
    rate and no score, because the six dimensions are never merged. `offline`
    and `observed_cohort` runs are never mixed; `evidence_mode` filters to
    one. A dimension nobody measured reads `unverifiable`, never `pass`.

    `limit` bounds the collection (default 20, maximum 100).

    `run_profiles=true` adds the run profiles this Project declares -- the
    `run_profile_id` and `evidence_mode` `open_evaluation_run` must pin -- each
    with its active baseline when it has one. It rides this read because it IS a
    read; the verbs are their own tools, and a write never rides a parameter of a
    read (`mcp-tool-surface.md`).

    Read-only. The verbs are `open_evaluation_run`, `advance_evaluation_run` and
    `decide_evaluation_run`.
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

    from core.evaluation_runs import (  # noqa: PLC0415
        EVIDENCE_MODES,
        EvaluationNotFound,
        active_baseline,
        list_evaluation_runs,
        list_run_profiles,
        run_cases,
        run_overview,
    )

    mode = (evidence_mode or "").strip() or None
    if mode is not None and mode not in EVIDENCE_MODES:
        raise _tool_error(
            "invalid_param",
            "evidence_mode must be one of: " + ", ".join(EVIDENCE_MODES) + ".",
        )
    bounded = max(1, min(int(limit or _DEFAULT_RUN_LIMIT), _MAX_RUN_LIMIT))
    wanted = (run_id or "").strip() or None

    try:
        with request_connection(identity) as conn:
            org_id = _org_of(conn, checked)
            profiles = None
            if run_profiles:
                # Each profile with its active baseline: a profile without one
                # can be opened and never compared, and the caller learns that
                # here rather than at the comparison.
                profiles = [
                    {
                        **profile,
                        "active_baseline": active_baseline(
                            conn,
                            org_id=org_id,
                            project_id=checked,
                            run_profile_id=str(profile.get("id")),
                        ),
                    }
                    for profile in list_run_profiles(
                        conn, org_id=org_id, project_id=checked
                    )
                ]
            if wanted:
                overview = run_overview(
                    conn, org_id=org_id, project_id=checked, run_id=wanted
                )
                cases = run_cases(
                    conn, org_id=org_id, project_id=checked, run_id=wanted
                )
                data = {"project_id": checked, "run": overview, **cases}
                if profiles is not None:
                    data["run_profiles"] = profiles
                return _result(_run_summary(overview, cases), data)
            runs = list_evaluation_runs(
                conn,
                org_id=org_id,
                project_id=checked,
                evidence_mode=mode,
                limit=bounded,
            )
    except EvaluationNotFound as exc:
        raise _tool_error(
            "run_not_found", "No evaluation run with this id in this Project."
        ) from exc
    except ValueError as exc:
        # `_project_org_id` raises for a Project with no active organization,
        # and the read model raises for a refused argument. Both are the
        # caller's own input inside a Project it may already view.
        raise _tool_error("invalid_param", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("evaluation_mcp: run read failed: %s", type(exc).__name__)
        raise _tool_error(
            "seam_unavailable", "Evaluation runs are unavailable."
        ) from exc

    data = {
        "project_id": checked,
        "evidence_mode": mode,
        "count": len(runs),
        "bound": bounded,
        "runs": runs,
    }
    summary = _runs_summary(checked, runs)
    if profiles is not None:
        data["run_profiles"] = profiles
        without = [p for p in profiles if not p.get("active_baseline")]
        summary += (
            f"\n{len(profiles)} run profile(s) declared here"
            + (
                f"; {len(without)} of them has no approved baseline yet, so a run "
                "opened on it can be judged and not compared."
                if without
                else "; each has an approved baseline."
            )
        )
    return _result(summary, data)


def get_context_adherence(project_id: str, days: int | None = None):
    """Did answers consult the governed context before querying, over a window.

    Returns the counts split by the BASIS on which adherence was decided,
    and by data tool WITHIN each basis, plus which context tools were
    consulted. Every bucket carries its own denominator beside its share.

    There is no overall adherence figure, and that is deliberate: a
    trace-identified session and a wall-clock inference are two kinds of
    evidence, and one number over both would let the inference read as a
    measure. Each basis says in words what it proves, and a window with no
    observation says so rather than reading as healthy.

    `days` sets the window (default 30). Read-only.
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

    from core.adherence import (  # noqa: PLC0415
        ADHERENCE_DEFAULT_WINDOW_DAYS,
        adherence_overview,
    )

    window = int(days) if days else ADHERENCE_DEFAULT_WINDOW_DAYS
    try:
        with request_connection(identity) as conn:
            payload = adherence_overview(conn, project_id=checked, days=window)
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("evaluation_mcp: adherence read failed: %s", type(exc).__name__)
        raise _tool_error(
            "seam_unavailable", "Context adherence is unavailable."
        ) from exc

    return _result(_adherence_summary(payload), payload)


#: One page of steps stays inside the model channel: ~150 bytes a step.
AI_PATH_STEPS_PER_PAGE = 10
AI_PATH_LIST_MAX = 25


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _owner_of(step: Mapping[str, Any]) -> str | None:
    parts = [step.get("owner_workspace"), step.get("owner_object_type"), step.get("owner_object_id")]
    if not any(parts):
        return None
    owner = "/".join(str(part) for part in parts if part)
    version = step.get("owner_version_id")
    return f"{owner}@{version}" if version else owner


def _step_line(step: Mapping[str, Any]) -> dict[str, Any]:
    from core.ai_path_recorder import level_of  # noqa: PLC0415
    from core.ai_paths import project_choices  # noqa: PLC0415

    skill = step.get("skill") if isinstance(step.get("skill"), Mapping) else {}
    line = {
        "n": step.get("ordinal"),
        "kind": step.get("step_kind"),
        "level": level_of(step.get("step_kind"), step.get("tool_name"), step.get("owner_object_type")),
        "tool": step.get("tool_name"),
        "outcome": step.get("outcome"),
        "owner": _owner_of(step),
        "skill": (
            f"{step.get('skill_version_id')}#{step.get('skill_step_id')}"
            + (f" {skill.get('label')}" if skill.get("label") else "")
            if step.get("skill_version_id")
            else None
        ),
        "chose": _first_keys(project_choices(step.get("detail")), 6),
    }
    return {key: value for key, value in line.items() if value is not None}


def _first_keys(mapping: Mapping[str, Any] | None, limit: int) -> dict[str, Any] | None:
    if not mapping:
        return None
    items = list(mapping.items())
    kept = dict(items[:limit])
    if len(items) > limit:
        kept["…"] = f"{len(items) - limit} more"
    return kept


def _judgement(
    conn, *, project_id: str, path: Mapping[str, Any], steps: list, interaction: list | None = None
) -> dict[str, Any]:
    """What the recorder lets a reader conclude about a path -- and what it does not.

    Four readings, each with its own denominator: the pinned-policy assessment
    (`ai_paths.assess`, three-valued, `unverifiable` when nothing was pinned to
    judge against); the Skills the trace took and the steps it crossed; the
    governed context it consulted (the CONTEXT rungs, with their owners); and the
    adherence verdicts recorded for the trace's data questions
    (`app.query_adherence`). None of them is a score, and a reader who wants one
    has to say which of the four it means.
    """
    from core.ai_path_recorder import level_of  # noqa: PLC0415

    assessment = path.get("assessment") if isinstance(path.get("assessment"), Mapping) else {}
    findings = [f for f in (assessment.get("findings") or []) if isinstance(f, Mapping)][:3]
    # THE EVIDENCE OF THE VERDICT (round 7, F3; read ONCE and guarded, round 8):
    # the verdict was judged over the interaction; the Skills, the crossings and
    # the context consulted are read over the SAME rows -- the ones the caller
    # widened once, or the path's own when no widening was possible.
    steps = list(interaction) if interaction is not None else list(steps)
    skills: dict[str, dict[str, Any]] = {}
    context: list[dict[str, Any]] = []
    for step in steps:
        version = step.get("skill_version_id")
        if version:
            entry = skills.setdefault(str(version), {"skill_version": str(version), "steps_crossed": []})
            crossed = str(step.get("skill_step_id"))
            if crossed not in entry["steps_crossed"]:
                entry["steps_crossed"].append(crossed)
            label = (step.get("skill") or {}).get("label") if isinstance(step.get("skill"), Mapping) else None
            if label and "label" not in entry:
                entry["label"] = label
        level = level_of(step.get("step_kind"), step.get("tool_name"), step.get("owner_object_type"))
        if level in ("CONTEXT", "PROCEDURE") and len(context) < 12:
            context.append({"tool": step.get("tool_name"), "owner": _owner_of(step), "outcome": step.get("outcome")})
    adherence: list[dict[str, Any]] = []
    trace_id = path.get("w3c_trace_id")
    if trace_id:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data_tool, adherent, context_tool FROM app.query_adherence "
                    "WHERE project_id = %s AND trace_id = %s ORDER BY created_at LIMIT 12",
                    (project_id, trace_id),
                )
                adherence = [
                    {"data_tool": row[0], "adherent": bool(row[1]), "context_tool": row[2]} for row in cur.fetchall()
                ]
        except Exception as exc:  # noqa: BLE001 -- a judgement never breaks the read
            logger.debug("evaluation_mcp: adherence rows unreadable: %s", type(exc).__name__)
            adherence = []
    coverage_lines = []
    try:
        from core.ai_paths import skill_coverage  # noqa: PLC0415

        # Over the whole interaction: the execution's path and the calls around it.
        for entry in skill_coverage(conn, steps):
            coverage_lines.append(
                {
                    "skill": entry.get("skill_name") or entry.get("skill_version"),
                    "version": entry.get("skill_version"),
                    "prescribed": entry.get("prescribed"),
                    "crossed": entry.get("crossed"),
                    "skipped": entry.get("skipped"),
                    "skipped_labels": [
                        str(line.get("label") or "")[:80]
                        for line in entry.get("steps") or []
                        if line.get("state") == "skipped"
                    ][:3],
                    "unobservable": entry.get("unobservable"),
                }
            )
    except Exception as exc:  # noqa: BLE001 -- a judgement never breaks the read
        logger.debug("evaluation_mcp: skill coverage unreadable: %s", type(exc).__name__)
    return {
        "skill_coverage": coverage_lines,
        "policy": {"verdict": assessment.get("verdict"), "findings": [
            {"finding": f.get("finding"), "detail": str(f.get("detail") or "")[:160]} for f in findings
        ]},
        "skills": list(skills.values())[:4],
        "context_consulted": context,
        "adherence": adherence,
        "reading": (
            "no Skill taken, no governed context consulted: the figures were produced without the Project's knowledge"
            if not skills and not context
            else ("a Skill was taken" if skills else "governed context was consulted")
            + (", and every data question consulted context first" if adherence and all(a["adherent"] for a in adherence) else "")
        ),
    }


def get_ai_path(
    project_id: str,
    path_id: str | None = None,
    result_id: str | None = None,
    skill: str | None = None,
    offset: int = 0,
    limit: int = 10,
):
    """Read the observed AI Paths of this Project -- the model's own included.

    Without `path_id`, `result_id` or `skill`: the latest paths, one line each
    (id, state, outcome, actor, when, how many steps, which tools). With
    `path_id`: that path's ordered steps -- kind, level (JOB / SKILL / PROCEDURE
    / CONTEXT / TOOL), tool, owner object and version, what it chose, outcome --
    a page of `AI_PATH_STEPS_PER_PAGE` from `offset`, plus a judgement (Skill
    coverage, policy verdict, context consulted, adherence). A path still
    `recording` is shown as such. With `result_id`: the path that produced that
    Result, or `No AI path` when a person ran it. With `skill` (a procedure id or
    name): what the last walks of that Skill in this Project say about it --
    verdicts, and per prescribed step how often it was crossed or skipped -- so
    a model reads its own habit before walking again. Read-only; what it shows
    is what the recorder observed, never a reconstruction (`context-hub.md`).
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
    from core.ai_paths import (  # noqa: PLC0415
        NO_AI_PATH,
        AiPathNotFound,
        list_paths,
        load_path,
        paths_sharing_trace,
        steps_digest,
    )

    wanted_path = (path_id or "").strip() or None
    wanted_result = (result_id or "").strip() or None
    wanted_skill = (skill or "").strip() or None
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = max(1, min(int(limit or 10), AI_PATH_LIST_MAX))
    except (TypeError, ValueError):
        limit = 10
    try:
        with request_connection(identity) as conn:
            if wanted_skill and not wanted_path and not wanted_result:
                from core.ai_paths import skill_walk_stats  # noqa: PLC0415

                procedure_id = wanted_skill
                if not procedure_id.startswith("proc_"):
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM app.procedures WHERE name = %s AND (project_id = %s OR project_id IS NULL) "
                            "ORDER BY (project_id IS NULL) LIMIT 1",
                            (procedure_id, checked),
                        )
                        row = cur.fetchone()
                    if row is None:
                        raise _tool_error("not_found", "No Skill of that name is readable in this Project.")
                    procedure_id = str(row[0])
                stats = skill_walk_stats(conn, project_id=checked, procedure_id=procedure_id)
                steps = [
                    {
                        "step": e.get("step"),
                        "tool": e.get("tool"),
                        "crossed": e.get("crossed"),
                        "skipped": e.get("skipped"),
                        "unobservable": e.get("unobservable"),
                        "label": str(e.get("label") or "")[:60],
                    }
                    for e in (stats.get("steps") or [])
                ][:12]
                most_skipped = [
                    f"step {e.get('step')} ({e.get('skipped')}x): {str(e.get('label') or '')[:60]}"
                    for e in (stats.get("most_skipped") or [])
                ]
                data = {
                    "skill": wanted_skill,
                    "procedure_id": procedure_id,
                    "walks": stats.get("walks"),
                    "window": stats.get("window"),
                    "verdicts": stats.get("verdicts"),
                    "steps": steps,
                    "most_skipped": most_skipped,
                    "recent": [
                        {"path_id": w.get("path_id"), "verdict": w.get("verdict"), "skipped": w.get("skipped")}
                        for w in (stats.get("recent") or [])
                    ][:3],
                }
                verdicts = ", ".join(f"{n} {v}" for v, n in (stats.get("verdicts") or {}).items()) or "none"
                summary = (
                    f"Skill `{wanted_skill}` in this Project: {stats.get('window')}; verdicts {verdicts}."
                    + (f" Most skipped: {'; '.join(most_skipped)}." if most_skipped else "")
                )
                return _result(summary, data)
            if wanted_result and not wanted_path:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT ai_path_id, ai_path_absent_literal FROM app.query_results "
                        "WHERE id = %s AND project_id = %s",
                        (wanted_result, checked),
                    )
                    row = cur.fetchone()
                if row is None:
                    raise _tool_error("not_found", "Result not found in this Project.")
                if not row[0]:
                    literal = row[1] or NO_AI_PATH
                    return _result(
                        f"Result {wanted_result}: {literal} -- a person ran it; there is no path to read.",
                        {"result_id": wanted_result, "state": "human_absent", "literal": literal},
                    )
                wanted_path = str(row[0])
            if wanted_path:
                try:
                    # No widening in the loader: the interaction is read ONCE below
                    # and serves the verdict, the coverage, the Skills and the context.
                    path = load_path(conn, path_id=wanted_path, project_id=checked, assess_over_interaction=False)
                except AiPathNotFound as exc:
                    raise _tool_error("not_found", "AI Path not found in this Project.") from exc
                steps = [step for step in (path.get("steps") or []) if isinstance(step, Mapping)]
                page = steps[offset : offset + AI_PATH_STEPS_PER_PAGE]
                # THE SIBLING INDEX, GUARDED (round 8, R8-B2): an unreadable index
                # narrows the reading to the path, it never takes the path away.
                try:
                    same_interaction = paths_sharing_trace(
                        conn, project_id=checked, trace_id=path.get("w3c_trace_id"), exclude=str(path.get("id") or "")
                    )
                except Exception as exc:  # noqa: BLE001 -- a sibling index that cannot be read narrows the reading
                    logger.debug("get_ai_path: sibling index unreadable: %s", type(exc).__name__)
                    same_interaction = []
                # THE INTERACTION, READ ONCE, GUARDED (round 8): a sibling that cannot
                # be read narrows the reading, never breaks it -- the same rule
                # `load_path` and the detail route hold.
                from core.ai_paths import INTERACTION_SIBLING_LIMIT, assessment_for_read, interaction_steps  # noqa: PLC0415

                try:
                    interaction = interaction_steps(conn, project_id=checked, path=path, siblings=same_interaction)
                except Exception as exc:  # noqa: BLE001 -- a sibling that cannot be read narrows the reading
                    logger.debug("get_ai_path: interaction narrowed to the path: %s", type(exc).__name__)
                    interaction = [dict(step) for step in steps]
                path["assessment"] = assessment_for_read(path, interaction)
                result_facts = None
                if wanted_result:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT outcome, row_count, truncated FROM app.query_results WHERE id = %s AND project_id = %s",
                            (wanted_result, checked),
                        )
                        row = cur.fetchone()
                    if row:
                        cells = list(row) + [None, None, None]
                        result_facts = {"outcome": cells[0], "row_count": cells[1], "truncated": bool(cells[2])}
                data = {
                    "path_id": path.get("id"),
                    "state": path.get("lifecycle"),
                    "outcome": path.get("outcome"),
                    "actor": path.get("actor"),
                    "started_at": _iso(path.get("started_at")),
                    "ended_at": _iso(path.get("ended_at")),
                    "trace_id": path.get("w3c_trace_id"),
                    "result_id": wanted_result,
                    "steps_total": len(steps),
                    "offset": offset,
                    "steps": [_step_line(step) for step in page],
                    "truncated": offset + len(page) < len(steps),
                    # The rest of the interaction, when the client named it with one trace.
                    "same_interaction": same_interaction,
                    # THE BOUND, IN THE STRUCTURED CHANNEL (round 10, N2): the index may
                    # list more siblings than the reading widened over.
                    "siblings_total": len(same_interaction),
                    "siblings_read": min(len(same_interaction), INTERACTION_SIBLING_LIMIT),
                    "result": result_facts,
                    "judgement": _judgement(conn, project_id=checked, path=path, steps=steps, interaction=interaction),
                }
                state = (
                    "still recording -- an interaction nobody finalized"
                    if path.get("lifecycle") != "finalized"
                    else f"finalized, {path.get('outcome')}"
                )
                tools = []
                for step in steps:
                    tool = step.get("tool_name")
                    if tool and tool not in tools:
                        tools.append(str(tool))
                summary = (
                    f"AI Path {path.get('id')}: {state}; {len(steps)} step(s)"
                    + (f" through {', '.join(tools[:6])}" if tools else "")
                    + (f"; showing {offset + 1}-{offset + len(page)}." if page else ".")
                    + (
                        f" {len(same_interaction)} other path(s) share this interaction's trace."
                        + (
                            f" The reading spans the first {INTERACTION_SIBLING_LIMIT} of them."
                            if len(same_interaction) > INTERACTION_SIBLING_LIMIT
                            else ""
                        )
                        if same_interaction
                        else ""
                    )
                )
                return _result(summary, data)
            listed = list_paths(conn, project_id=checked, limit=limit)
            digest = steps_digest(conn, path_ids=[str(p.get("id")) for p in listed])
        lines = []
        for entry in listed:
            extra = digest.get(str(entry.get("id")), {})
            lines.append(
                {
                    "path_id": entry.get("id"),
                    "state": entry.get("lifecycle"),
                    "outcome": entry.get("outcome"),
                    "actor": entry.get("actor"),
                    "started_at": _iso(entry.get("started_at")),
                    "steps": extra.get("steps", 0),
                    "tools": (extra.get("tools") or [])[:6],
                }
            )
        recording = sum(1 for line in lines if line["state"] != "finalized")
        summary = (
            f"{len(lines)} latest AI Path(s) of this Project; {recording} still recording. "
            "Call again with path_id for the steps of one, or result_id for the path behind a Result."
            if lines
            else "No AI Path recorded in this Project yet: a path appears when a tool is called with the Project named."
        )
        return _result(summary, {"paths": lines, "listed": len(lines), "next": "path_id=<id> | result_id=<id>"})
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("evaluation_mcp: ai path read failed: %s", type(exc).__name__)
        raise _tool_error("seam_unavailable", "AI Paths are unavailable.") from exc



#: The capability floor of the two ranks the console holds. `edit` opens and
#: unrolls a run -- the console's `member`; `manage` decides what a judgement
#: authorizes -- the console's `owner`. Two ranks, because approving a baseline
#: and emitting a gate decision are the two acts the console guards higher.
_RUN_CAPABILITY = "edit"
_DECISION_CAPABILITY = "manage"

#: The verbs, closed. An unknown action is refused by name rather than silently
#: falling through to the safest branch.
_RUN_ACTIONS = frozenset({"execute", "finalize"})
_DECISION_ACTIONS = frozenset({"approve_baseline", "emit_gate_decision"})


def _live_tool_catalog() -> str:
    """The catalog version, resolved HERE and never accepted from the caller.

    `evaluation_runs_api._open_evaluation_run` refuses a run when the live
    catalog is unavailable, in its own words: *"a run whose catalog version was
    guessed is not comparable with any other."* This door holds the same line.
    """
    import asyncio  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from core.skill_tool_catalog import list_skill_tool_catalog  # noqa: PLC0415

    def _read() -> dict:
        # A tool handler of this module is SYNC and FastMCP runs it off the loop,
        # so `asyncio.run` is normally legal. `normally` is not a guarantee: if a
        # host ever calls one on the loop thread, `asyncio.run` raises and a run
        # would be refused for a reason that has nothing to do with the catalog.
        # A one-shot worker owns the loop in that case, and the answer is the
        # same on both paths.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(list_skill_tool_catalog())
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(list_skill_tool_catalog())).result()

    try:
        catalog = _read()
    except Exception as exc:  # noqa: BLE001
        logger.error("evaluation_mcp: tool catalog unavailable: %s", type(exc).__name__)
        raise _tool_error(
            "tool_catalog_unavailable",
            "The live tool catalog is not available, so its exact version cannot "
            "be pinned; a run is not opened without it.",
        ) from exc
    version = (catalog or {}).get("catalog_version")
    if not version:
        raise _tool_error(
            "tool_catalog_unavailable",
            "The live tool catalog carries no version; a run is not opened without it.",
        )
    return str(version)


# --------------------------------------------------------------------------- #
# THE VERBS. Each one calls `core.evaluation_runs` (and, for `execute`,
# `core.evaluation_run_executor`) -- the same modules `evaluation_runs_api.py`
# translates HTTP into and the Regression Runs screen drives. No validation, no
# SQL, no lifecycle and no verdict logic lives here: a second implementation of
# any of them would produce a second answer to "is this run comparable", which is
# the defect the single service exists to remove.
# --------------------------------------------------------------------------- #


def _write_access(conn, project_id: str, identity: str, capability: str):
    """The org this caller may write in, or the one canonical refusal.

    `resolve_strict_resource_access`, the seam `governance_mcp` uses, and never a
    second reading of who may write where. An outage fails CLOSED.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        decision = resolve_strict_resource_access(
            identity,
            conn,
            project_id=project_id,
            minimum_capability=capability,
            hold_access=True,
        )
    except Exception as exc:  # noqa: BLE001 -- an outage fails CLOSED
        logger.error("evaluation_mcp: access resolution failed: %s", exc)
        raise _tool_error("project_not_found", "Project not found or archived.") from exc
    if not decision.allowed or not decision.org_id:
        raise _tool_error("project_not_found", "Project not found or archived.")
    return str(decision.org_id)


def _refuse_evaluation(exc) -> None:
    """A refusal travels with every reason and its subject, never a bare code."""
    reasons = "; ".join(
        f"{getattr(r, 'subject', None) or 'run'}: {getattr(r, 'message', '')}"
        for r in (getattr(exc, "refusals", None) or [])
    )
    raise _tool_error(
        str(getattr(exc, "code", "refused") or "refused"),
        (str(exc) or "The run was refused.") + (f" -- {reasons}" if reasons else ""),
    )


def _caller():
    """The identity behind this call, or the canonical existence-hiding refusal."""
    from core.mcp_scope import caller_identity  # noqa: PLC0415

    identity = caller_identity()
    if not identity or identity == "anonymous":
        raise _tool_error("project_not_found", "Project not found or archived.")
    return identity


def open_evaluation_run(project_id: str, run: dict, cases: list | None = None):
    """Judge this cohort under these pins -- open a Regression Run.

    `run` carries what a run is comparable BY, and every one of them is pinned
    rather than resolved later: `run_profile_id` (with its `evidence_mode`),
    `semantic_view_id` and `semantic_view_version_id`, `context_version_set_id`,
    `model_ref`, `host_capability_profile`, `data_snapshot_ref`, `as_of`, and
    `observed_cohort_id` when the profile is an observed one.
    `get_evaluation_runs(run_profiles=true)` serves the profiles and their active
    baselines.

    The **tool catalog version is resolved server-side** from the live catalog and
    is never accepted from the caller: a run whose catalog version was guessed is
    comparable with nothing. When the catalog is unavailable the run is refused
    rather than opened with an invented pin -- the same refusal the console gets.

    `cases` optionally pins the subject of each Golden Question version in the
    same act: a list of `{golden_question_version_id, result_id?, ai_path_id?,
    ai_path_expected?, capability_key?, render_ref?}`. Omit it to let
    `advance_evaluation_run(action="execute")` unroll the whole question set.

    Creating a run profile and creating a context version set are console-only:
    this door refuses by name when their ids are absent, it does not mint them.
    """
    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    if not isinstance(run, dict) or not run:
        raise _tool_error(
            "missing_param",
            "run is required: the pins this judgement is comparable by "
            "(call get_evaluation_runs with run_profiles=true for the profiles).",
        )
    profile_id = str(run.get("run_profile_id") or "").strip()
    if not profile_id:
        raise _tool_error(
            "missing_param",
            "run.run_profile_id is required: a run is comparable only with runs "
            "of the same profile. Creating a profile is console-only.",
        )
    if not str(run.get("context_version_set_id") or "").strip():
        raise _tool_error(
            "missing_param",
            "run.context_version_set_id is required: the context a run judged "
            "against is frozen, never resolved as `latest`. Creating a version "
            "set is console-only.",
        )

    from core.db import request_connection  # noqa: PLC0415
    from core.evaluation_runs import (  # noqa: PLC0415
        EvaluationNotFound,
        EvaluationRefused,
        add_run_case,
    )
    from core.evaluation_runs import open_evaluation_run as _open  # noqa: PLC0415

    identity = _caller()
    catalog = _live_tool_catalog()

    pinned = list(cases or [])
    if not isinstance(pinned, list):
        raise _tool_error("invalid_param", "cases is a list of case objects.")

    with request_connection(identity) as conn:
        org_id = _write_access(conn, checked, identity, _RUN_CAPABILITY)
        try:
            opened = _open(
                conn,
                org_id=org_id,
                project_id=checked,
                run_profile_id=profile_id,
                semantic_view_id=run.get("semantic_view_id"),
                semantic_view_version_id=run.get("semantic_view_version_id"),
                context_version_set_id=run.get("context_version_set_id"),
                model_ref=run.get("model_ref"),
                host_capability_profile=run.get("host_capability_profile") or {},
                tool_catalog_version=catalog,
                data_snapshot_ref=run.get("data_snapshot_ref") or {},
                as_of=run.get("as_of"),
                observed_cohort_id=run.get("observed_cohort_id"),
                actor=identity,
            )
            run_id = str(opened.get("id") or opened.get("run_id") or "")
            landed = []
            for case in pinned:
                if not isinstance(case, dict):
                    raise _tool_error("invalid_param", "each case is an object.")
                landed.append(
                    add_run_case(
                        conn,
                        org_id=org_id,
                        project_id=checked,
                        run_id=run_id,
                        golden_question_version_id=str(
                            case.get("golden_question_version_id") or ""
                        ),
                        result_id=case.get("result_id"),
                        ai_path_id=case.get("ai_path_id"),
                        ai_path_expected=bool(case.get("ai_path_expected", True)),
                        capability_key=case.get("capability_key"),
                        render_ref=case.get("render_ref"),
                    )
                )
            conn.commit()
        except EvaluationNotFound as exc:
            raise _tool_error(
                "run_not_found", "No evaluation run with this id in this Project."
            ) from exc
        except EvaluationRefused as exc:
            _refuse_evaluation(exc)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("evaluation_mcp: open failed: %s", type(exc).__name__)
            raise _tool_error(
                "seam_unavailable", "The evaluation run could not be opened."
            ) from exc

    unresolved = opened.get("unresolved_pins") or []
    summary = (
        f"Run {opened.get('id')} opened on profile {profile_id} "
        f"[{opened.get('evidence_mode')}], {len(landed)} case(s) pinned, "
        f"{len(unresolved)} unresolved pin(s). "
        "Unroll it with advance_evaluation_run(action='execute')."
    )
    return _result(summary, {"project_id": checked, "run": opened, "cases": landed})


def advance_evaluation_run(project_id: str, run_id: str, action: str, subjects: dict | None = None):
    """Move a judgement forward -- unroll it, or close it.

    - `action="execute"`: pins a case per Golden Question of the set, freezes the
      run and writes six verdicts per case through the writer that can judge each
      one. ATOMIC: a refusal on the seventh question leaves no run whose question
      set is an accident of where the walk stopped. `subjects` optionally maps a
      Golden Question version id to its subject.
    - `action="finalize"`: `recording` becomes `finalized` for a run whose cases
      were pinned one by one.

    **Finalizing creates, moves and updates no baseline**, and that is not an
    omission: a baseline is never updated automatically, and the only way one
    comes to exist is `decide_evaluation_run(action="approve_baseline")`.

    A dimension nobody measured reads `unverifiable`, never `pass`, and the six
    dimensions are never merged into a rate.
    """
    checked = (project_id or "").strip()
    wanted = (run_id or "").strip()
    verb = (action or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    if not wanted:
        raise _tool_error("missing_param", "run_id is required.")
    if verb not in _RUN_ACTIONS:
        raise _tool_error(
            "unknown_action", "action is one of: " + ", ".join(sorted(_RUN_ACTIONS)) + "."
        )
    if subjects is not None and not isinstance(subjects, dict):
        raise _tool_error(
            "invalid_param",
            "subjects maps a Golden Question version id to its subject.",
        )

    from core.db import request_connection  # noqa: PLC0415
    from core.evaluation_runs import (  # noqa: PLC0415
        EvaluationNotFound,
        EvaluationRefused,
        finalize_evaluation_run,
    )

    identity = _caller()
    with request_connection(identity) as conn:
        org_id = _write_access(conn, checked, identity, _RUN_CAPABILITY)
        try:
            if verb == "execute":
                from core.evaluation_run_executor import (  # noqa: PLC0415
                    execute_evaluation_run,
                )

                outcome = execute_evaluation_run(
                    conn,
                    org_id=org_id,
                    project_id=checked,
                    run_id=wanted,
                    actor=identity,
                    subjects=subjects or None,
                )
            else:
                outcome = finalize_evaluation_run(
                    conn, org_id=org_id, project_id=checked, run_id=wanted
                )
            conn.commit()
        except EvaluationNotFound as exc:
            raise _tool_error(
                "run_not_found", "No evaluation run with this id in this Project."
            ) from exc
        except EvaluationRefused as exc:
            _refuse_evaluation(exc)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("evaluation_mcp: %s failed: %s", verb, type(exc).__name__)
            raise _tool_error(
                "seam_unavailable", f"The run could not be {verb}d."
            ) from exc

    counts = outcome.get("verdict_counts") or {}
    per_dimension = "; ".join(
        f"{dimension}: " + ", ".join(f"{v}={n}" for v, n in sorted(verdicts.items()))
        for dimension, verdicts in sorted(counts.items())
    )
    summary = (
        f"Run {wanted} {verb}d [{outcome.get('lifecycle', 'finalized')}]. "
        + (per_dimension or "No verdict recorded.")
        + " No baseline was created or moved: approving one is an explicit act."
    )
    return _result(summary, {"project_id": checked, "action": verb, **outcome})


def decide_evaluation_run(project_id: str, action: str, decision: dict):
    """Decide what a judgement authorizes -- the standard, or one candidate.

    - `action="approve_baseline"`: `decision` carries `run_id`, `approved_by` and
      `approval_reason`, all three mandatory. Replacing a baseline inserts a NEW
      approval and marks the previous one superseded; the previous approval is
      never rewritten, so what was approved stays readable after it stops being
      current.
    - `action="emit_gate_decision"`: `decision` carries `comparison_id` and the
      candidate it judges -- `candidate_owner_workspace`, `candidate_object_type`,
      `candidate_object_id`, `candidate_version_id`. It writes **evidence for the
      owner and never a transition**: nothing owned by Governance or the Context
      Hub is written here, and the owning workflow reads the decision before its
      own publish or activate step.

    Both are the `owner` rank in the console, and they carry the Governance
    profile here for the same reason: they decide what every later comparison
    will be measured against.
    """
    checked = (project_id or "").strip()
    verb = (action or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    if verb not in _DECISION_ACTIONS:
        raise _tool_error(
            "unknown_action",
            "action is one of: " + ", ".join(sorted(_DECISION_ACTIONS)) + ".",
        )
    if not isinstance(decision, dict) or not decision:
        raise _tool_error("missing_param", "decision is required.")

    from core.db import request_connection  # noqa: PLC0415
    from core.evaluation_runs import (  # noqa: PLC0415
        EvaluationNotFound,
        EvaluationRefused,
        approve_baseline,
        emit_gate_decision,
    )

    identity = _caller()
    with request_connection(identity) as conn:
        org_id = _write_access(conn, checked, identity, _DECISION_CAPABILITY)
        try:
            if verb == "approve_baseline":
                for field in ("run_id", "approved_by", "approval_reason"):
                    if not str(decision.get(field) or "").strip():
                        raise _tool_error(
                            "missing_param",
                            f"decision.{field} is required: a baseline says which "
                            "run is the standard, and who said so, and why.",
                        )
                landed = approve_baseline(
                    conn,
                    org_id=org_id,
                    project_id=checked,
                    run_id=str(decision["run_id"]).strip(),
                    approved_by=str(decision["approved_by"]).strip(),
                    approval_reason=str(decision["approval_reason"]).strip(),
                )
            else:
                if not str(decision.get("comparison_id") or "").strip():
                    raise _tool_error(
                        "missing_param",
                        "decision.comparison_id is required: a gate decision "
                        "judges one comparison, never a run on its own.",
                    )
                landed = emit_gate_decision(
                    conn,
                    org_id=org_id,
                    project_id=checked,
                    comparison_id=str(decision["comparison_id"]).strip(),
                    candidate_owner_workspace=decision.get("candidate_owner_workspace"),
                    candidate_object_type=decision.get("candidate_object_type"),
                    candidate_object_id=decision.get("candidate_object_id"),
                    candidate_version_id=decision.get("candidate_version_id"),
                    decided_by=str(decision.get("decided_by") or identity),
                )
            conn.commit()
        except EvaluationNotFound as exc:
            raise _tool_error(
                "run_not_found", "No evaluation run or comparison with this id here."
            ) from exc
        except EvaluationRefused as exc:
            _refuse_evaluation(exc)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("evaluation_mcp: %s failed: %s", verb, type(exc).__name__)
            raise _tool_error(
                "seam_unavailable", "The decision could not be recorded."
            ) from exc

    summary = (
        (
            f"Run {decision.get('run_id')} is the approved baseline of its profile. "
            "The previous approval is superseded, never rewritten."
            if verb == "approve_baseline"
            else f"Gate decision recorded on comparison {decision.get('comparison_id')}. "
            "It is EVIDENCE for the owner and transitions nothing: the owning "
            "workflow reads it before its own publish or activate step."
        )
    )
    return _result(summary, {"project_id": checked, "action": verb, **landed})


def register(mcp) -> None:
    """Register the Test evidence door -- three reads and three verbs.

    Called once from `core.main` BEFORE `validate_catalog()`. The three reads are
    `profile="insights"` / `effect="read"` / `confirmation_mode="none"`: a read
    transitions no domain state, so there is nothing for a human to authorize.

    The three verbs carry the two ranks the console guards them at. Opening and
    advancing a run are `operations` -- they run something; deciding what a
    judgement authorizes is `governance`, the rank of every other tool that
    decides what later work is measured against. All three are `confirmed_write`
    with a `human` confirmation: a baseline nobody approved is not a baseline.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415


    # REGISTERED ONE BY ONE, AND NOT IN A LOOP. `register_profiled(mcp, handler)`
    # over a tuple is legal and reads well, but the independent census of
    # `scripts/check_legacy_analytics_migration.py` derives a tool's identity from
    # the AST of that call: a loop variable makes both tools land in the inventory
    # as `evaluation_mcp.py#tool:handler`, one identity for two doors. A tool that
    # cannot be named in the census cannot be tracked in it.
    register_profiled(
        mcp,
        get_ai_path,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_evaluation_runs,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_context_adherence,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        open_evaluation_run,
        # Opening a run spends compute and writes a judged cohort: an operation,
        # the rank `start_datastream_draft` and its neighbours hold.
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        advance_evaluation_run,
        # `execute` unrolls the whole question set in one atomic act; `finalize`
        # freezes it. Both are the same rank as opening.
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        decide_evaluation_run,
        # A baseline decides what every later comparison is measured against, and
        # a gate decision is evidence an owner acts on: the rank of
        # `publish_semantic_model_change`, for the same reason.
        profile="governance",
        effect="confirmed_write",
        data_class="sensitive",
        confirmation_mode="human",
    )
