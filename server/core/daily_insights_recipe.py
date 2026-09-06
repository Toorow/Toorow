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

from core import daily_insights_schema as schema

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


def _announced_rules() -> list[str]:
    """The rules the recipe tells the agent -- DERIVED from the door (AI-274).

    WHY THIS IS A FUNCTION AND NOT A LIST OF SENTENCES. The recipe stated the
    agent's rules and the publish gate enforced them, in two places, and they had
    already drifted apart: story 53.4 hardened the evidence obligation -- every
    insight must cite at least one server-measured fact, and each ref must be
    `<kind>:<id>` with `kind` from a closed set -- and the recipe never mentioned
    it. An agent following the recipe to the letter was refused by the door.

    That is the same defect as a hand-kept palette beside a server allowlist
    (story 60.2), one workspace over. The repair is the same: read the applied
    contract. `MAX_INSIGHTS_PER_DAY`, `EVIDENCE_KINDS` and `ALLOWED_CARD_MODES`
    come from `daily_insights_schema`, which is what `_validate_payload`
    enforces, so a bound that moves moves in the sentence too.
    """
    kinds = " or ".join(f"'{kind}:<id>'" for kind in schema.EVIDENCE_KINDS)
    return [
        f"No insight is better than a weak insight: publish 0..{schema.MAX_INSIGHTS_PER_DAY}, "
        "prefer 0 over a weak one.",
        "Never emit SQL, HTML, CSS, external URLs or free-form ratios; "
        "the server resolves every number.",
        "Stop (do not publish) when readiness is 'blocked': data not ready != no insight.",
        f"Select an existing card (mode '{schema.CARD_MODE_TEMPLATE}'); "
        f"'{schema.CARD_MODE_COMPOSE}' mode is disabled.",
        # AI-274 -- the obligation the door has enforced since 53.4 and the recipe
        # never announced. Both halves are stated, because citing the wrong SHAPE
        # is refused as firmly as citing nothing.
        "Every insight must cite at least one server-measured fact in "
        "`evidenceRefs`: an insight that points at no datum is refused.",
        f"An evidence ref is {kinds} -- the card template you render is NOT "
        "evidence, and citing it is refused.",
    ]


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
        "rules": _announced_rules(),
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
    # `list_runs` carries the COUNTS and no items; `get_run` carries the items. A
    # journal built from the list used to read `len(insights)` on a list that is
    # deliberately empty there, so every published day reported zero insights.
    # The count is preferred whenever the reader supplied one; `len(insights)`
    # stays the answer for `get_run`, which has the real rows.
    item_count = run.get("item_count")
    item_count = len(insights) if item_count is None else int(item_count)
    retracted_count = run.get("retracted_count")
    if retracted_count is None:
        retracted_count = sum(1 for i in insights if i.get("retracted_at"))
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
        "itemCount": item_count,
        # A WITHDRAWAL IS PART OF WHAT THE DAY DID (migration 321). A retracted
        # insight is not removed from the day -- it is shown as withdrawn, with its
        # reason, wherever the day is read. Reporting only `itemCount` would make a
        # day whose single claim was retracted read exactly like a day whose claim
        # still stands.
        "retractedCount": int(retracted_count),
        # Same defect class as `itemCount` (review of 9402f8b9, residue 2): on the
        # /runs list the item list is deliberately absent, so deriving `slots`
        # from it reported [] on every published day. `list_runs` now carries the
        # aggregate; the derivation stays as the fallback for the single-run read,
        # which does carry its items.
        "slots": (
            sorted(int(s) for s in run["slots"])
            if run.get("slots") is not None
            else sorted(int(i["slot"]) for i in insights if "slot" in i)
        ),
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
