"""Scheduled-task recipe + run observability (Epic 35, Story 35.5).

Gate 35.0 decided A. The daily-insight schedule belongs to the CLIENT LLM HOST, not
toorow: no model secret and no scheduler live in the backend at MVP (epic §5). This
module produces the copyable task recipe an operator pastes into the host, and formats a
persisted run into an observability record.

Pure functions (no DB / no MCP). The admin/MCP surface that fetches runs and serves the
recipe pairs with Story 35.4; its AI-56 seam test travels with it.

ASCII-only stdout (L-3).
"""

from __future__ import annotations

TASK_RECIPE_VERSION = "1"
DEFAULT_CALL_BUDGET = 20

# The exact tool names exposed by Story 35.2, in the order the task should call them.
_READINESS_TOOL = "get_daily_insight_readiness"
_DISCOVERY_TOOL = "get_card_capabilities"
_READ_TOOLS = (
    "get_daily_report",
    "get_report",
    "get_card",
    "search_context",
    "get_procedure",
    "get_data_quality_report",
)
_PREVIEW_TOOL = "preview_daily_insight"
_PUBLISH_TOOL = "publish_daily_insights"

_DEFAULT_PRIORITY_DOMAINS = (
    "declared-vs-verified conflicts",
    "pacing / budget breaks",
    "mix shift",
    "data-quality issues that invalidate a conclusion",
    "business-event neighbours",
)


def build_task_recipe(
    *,
    project_id: str,
    timezone: str,
    hour_local: int,
    contract_version: str,
    call_budget: int = DEFAULT_CALL_BUDGET,
    priority_domains: list[str] | None = None,
    prompt_version: str = "v1",
) -> dict:
    """Build a deterministic, copyable scheduled-task recipe for a project.

    The recipe is instructions for the LLM host: when to run, which project/date to target,
    the call budget, the tool sequence, the priority domains and the "no insight beats a weak
    insight" rule. It carries NO model secret and does not schedule anything server-side.
    """

    if not (0 <= int(hour_local) <= 23):
        raise ValueError(f"hour_local {hour_local} hors bornes [0, 23]")

    return {
        "recipeVersion": TASK_RECIPE_VERSION,
        "contractVersion": contract_version,
        "promptVersion": prompt_version,
        "promptRef": "_bmad-output/implementation-artifacts/research/35-0-agent-prompt.v1.md",
        "project": project_id,
        "schedule": {
            "timezone": timezone,
            "hourLocal": int(hour_local),
            "target": "J-1",
            "cadence": "one run per day",
        },
        "callBudget": int(call_budget),
        "priorityDomains": list(priority_domains or _DEFAULT_PRIORITY_DOMAINS),
        "toolSequence": [
            {"step": "readiness", "tool": _READINESS_TOOL, "stopIf": "blocked"},
            {"step": "discovery", "tool": _DISCOVERY_TOOL},
            {"step": "research", "tools": list(_READ_TOOLS)},
            {"step": "preview", "tool": _PREVIEW_TOOL},
            {"step": "publish", "tool": _PUBLISH_TOOL},
        ],
        "rules": [
            "No insight is better than a weak insight: publish 0..3, prefer 0 over a weak one.",
            "Never emit SQL, HTML, CSS, external URLs or free-form ratios; "
            "the server resolves every number.",
            "Stop (do not publish) when readiness is 'blocked': data not ready != no insight.",
            "Select an existing card (mode 'template'); compose mode is disabled.",
        ],
        "hostOwnsSchedule": True,
        "backendHostsModel": False,
    }


def render_recipe_text(recipe: dict) -> str:
    """Render a recipe as a copy-paste markdown block (stable, ASCII-safe)."""

    sched = recipe["schedule"]
    lines: list[str] = []
    lines.append(f"# toorow daily-insight task -- project {recipe['project']}")
    lines.append("")
    lines.append(
        f"- Recipe version: {recipe['recipeVersion']} (contract {recipe['contractVersion']}, "
        f"prompt {recipe['promptVersion']})"
    )
    lines.append(
        f"- Schedule: daily at {sched['hourLocal']:02d}:00 {sched['timezone']}, "
        f"target {sched['target']}"
    )
    lines.append(f"- Call budget: {recipe['callBudget']} tool calls")
    lines.append(f"- Task prompt: {recipe['promptRef']}")
    lines.append("")
    lines.append("## Tool sequence")
    for entry in recipe["toolSequence"]:
        if "tools" in entry:
            lines.append(f"- {entry['step']}: {', '.join(entry['tools'])}")
        else:
            suffix = f" (stop if {entry['stopIf']})" if entry.get("stopIf") else ""
            lines.append(f"- {entry['step']}: {entry['tool']}{suffix}")
    lines.append("")
    lines.append("## Priority domains")
    for dom in recipe["priorityDomains"]:
        lines.append(f"- {dom}")
    lines.append("")
    lines.append("## Rules")
    for rule in recipe["rules"]:
        lines.append(f"- {rule}")
    return "\n".join(lines)


def run_journal(run: dict | None) -> dict:
    """Format a persisted run (35.3 get_run/list_runs) into an observability record.

    Renders the distinct states honestly. An ABSENT run (None -> "task did not run") is
    kept distinct from 'blocked' (data not ready), 'no_insight' (nothing worth saying),
    'failed', and 'published'. Surfaces a rejection reason when a blocked/failed run carries
    ``{reason, reason_code}`` in its coverage manifest.
    """

    if not run:
        return {
            "state": "absent",
            "detail": "no run recorded for this project/date (task did not run)",
        }

    coverage = run.get("coverage") or {}
    insights = run.get("insights") or []
    rejection = None
    if isinstance(coverage, dict) and (coverage.get("reason") or coverage.get("reason_code")):
        rejection = {
            "reason": coverage.get("reason"),
            "reasonCode": coverage.get("reason_code"),
        }

    return {
        "state": run.get("status"),
        "insightDate": run.get("insight_date"),
        "period": {"from": run.get("period_from"), "to": run.get("period_to")},
        "itemCount": len(insights),
        "slots": sorted(int(i["slot"]) for i in insights if "slot" in i),
        "provenance": {
            "host": run.get("host"),
            "promptVersion": run.get("prompt_version"),
            "contractVersion": run.get("contract_version"),
            "identity": run.get("identity"),
            "traceId": run.get("trace_id"),
        },
        "coverage": coverage,
        "rejection": rejection,
        "timestamps": {"createdAt": run.get("created_at"), "updatedAt": run.get("updated_at")},
    }
