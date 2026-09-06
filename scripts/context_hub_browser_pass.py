"""Real-browser pass over the Context Hub — §B.3 of the control note.

    pnpm --filter @toorow/admin dev
    uv run python scripts/context_hub_browser_pass.py

WHY THIS EXISTS. The Context Hub screens are covered by Vitest/RTL and by live
API seams, and neither can see what §B.3 asks about: does the ELK graph actually
lay nodes out, does a drawer sit ABOVE the canvas, does the timeline draw. jsdom
has no layout engine — every element it reports is 0x0 at (0,0) — so a suite can
be entirely green while the graph renders as a single pile of nodes in one
corner. That is the exact failure mode elkjs has when its layout promise is
dropped, and no Vitest assertion in this repo can distinguish it.

WHAT IT REFUSES TO BE. Not a screenshot tool. `scripts/shoot_screen.py` already
captures, and a capture proves nothing until a human opens it — which is how a
"visual pass" becomes a folder nobody reads. Every check here is an ASSERTION
with a stated failure, and the script exits non-zero. The screenshots are
written too, as evidence beside the verdict, never in place of it.

WHAT IT CANNOT SEE. A layout that is geometrically correct and ugly. This
script proves the graph is laid out, the drawer stacks above, the timeline is
drawn and no screen scrolls sideways; it does not judge whether any of it looks
good. That judgement stays human, and this pass exists to stop wasting it on
screens that were simply broken.

The API is answered with fixtures shaped like the real payloads, carrying NO
production identifier (`proj_EXAMPLE`, `example.com`) per the repository rule.
"""

from __future__ import annotations

import json
import pathlib
import sys

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5174"
OUT = pathlib.Path("scratch/context-hub-browser")

PROJECT = "proj_EXAMPLE"

# --------------------------------------------------------------------------
# Fixtures. Shaped like the wire, not like the component: these screens check
# that a response echoes the project they asked for and refuse it otherwise, and
# a fixture that guesses a field name makes the screen render its schema-error
# state -- correctly. A red run here is as often a wrong fixture as a wrong
# screen, and the two are told apart by reading which surface the screen shows.
# --------------------------------------------------------------------------

TOPICS = [
    {
        "id": f"top_{i:02d}",
        "project_id": None if i % 4 == 0 else PROJECT,
        "title": title,
        "body_md": f"## {title}\n\n{body}\n",
        "status": "active",
        "owner": "owner@example.com",
        "created_by": "owner@example.com",
        "created_at": "2026-07-20T10:00:00+00:00",
        "updated_at": "2026-07-28T10:00:00+00:00",
        "version_number": 2,
        "capabilities": {"can_write": True, "version_history": True, "usage": False},
    }
    for i, (title, body) in enumerate(
        [
            ("ROAS calculation policy", "Return on ad spend is computed on deduplicated conversions."),
            ("Attribution window", "Post-click is 30 days, post-view is 1 day."),
            ("Currency handling", "Every amount is stored in micros and converted once, at read."),
            ("Market definition", "A market is a named, disjoint group of countries."),
            ("Brand vocabulary", "Competitor names are scoped to the organization."),
        ],
        start=1,
    )
]

PROCEDURES = [
    {
        "id": f"proc_{i:02d}",
        "project_id": None if i == 2 else PROJECT,
        "name": name,
        "description": description,
        "frontmatter_yaml": f"name: {name}\ndescription: {description}\n",
        "body_md": f"## Step 1\n\n{description}\n",
        "status": "active",
        "owner": "owner@example.com",
        "created_by": "owner@example.com",
        "created_at": "2026-07-20T10:00:00+00:00",
        "updated_at": "2026-07-28T10:00:00+00:00",
        "version_number": 1,
        "capabilities": {"can_write": True, "version_history": True, "usage": False},
    }
    for i, (name, description) in enumerate(
        [
            ("weekly-performance-review", "Pull the last 7 days and compare with the previous period."),
            ("budget-pacing-check", "Compare spend against the media plan, flag drift over 10%."),
            ("anomaly-triage", "Read the detector's verdict before writing any conclusion."),
        ],
        start=1,
    )
]

GRAPH_NODES = [
    {
        "id": topic["id"],
        "node_type": "topic",
        "title": topic["title"],
        "excerpt": topic["body_md"][:200],
        "owner": topic["owner"],
        "owner_raw": topic["owner"],
        "version_number": topic["version_number"],
        "scope": "platform" if topic["project_id"] is None else "project",
        "status": "active",
    }
    for topic in TOPICS
] + [
    {
        "id": proc["id"],
        "node_type": "procedure",
        "title": proc["name"],
        "excerpt": proc["description"],
        "owner": proc["owner"],
        "owner_raw": proc["owner"],
        "version_number": proc["version_number"],
        "scope": "platform" if proc["project_id"] is None else "project",
        "status": "active",
    }
    for proc in PROCEDURES
]

GRAPH_EDGES = [
    {
        "id": f"edge_{i:02d}",
        "from_id": frm,
        "to_id": to,
        "from_type": from_type,
        "to_type": to_type,
        "edge_type": edge_type,
        "project_id": PROJECT,
        "created_by": "owner@example.com",
        "created_at": "2026-07-20T10:00:00+00:00",
    }
    for i, (frm, from_type, to, to_type, edge_type) in enumerate(
        [
            ("top_01", "topic", "proc_01", "procedure", "explains"),
            ("top_02", "topic", "proc_01", "procedure", "explains"),
            ("top_03", "topic", "proc_02", "procedure", "relates_to"),
            ("top_04", "topic", "proc_03", "procedure", "relates_to"),
            ("top_05", "topic", "top_01", "topic", "relates_to"),
        ],
        start=1,
    )
]

AI_PATH = {
    "id": "aip_EXAMPLE",
    "project_id": PROJECT,
    "lifecycle": "finalized",
    "outcome": "succeeded",
    "actor": "agent@example.com",
    "started_at": "2026-08-06T09:00:00+00:00",
    "ended_at": "2026-08-06T09:00:12+00:00",
    "w3c_trace_id": "0af7651916cd43dd8448eb211c80319c",
    "model_ref": "claude-opus-5",
    "policy_snapshot_hash": "sha256:0000",
    "referenceable_as_evidence": True,
    "assessment": {"verdict": "pass", "assessed_by": "owner@example.com"},
    "steps": [
        {
            "step_order": order,
            "step_kind": step_kind,
            "outcome": outcome,
            "tool_name": tool_name,
            "owner_workspace": "context-hub" if owner_type else None,
            "owner_object_type": owner_type,
            "owner_object_id": owner_id,
            "owner_version_id": None,
            "skill_version_id": None,
            "evidence_record_id": None,
            "detail": detail,
        }
        for order, (step_kind, outcome, tool_name, owner_type, owner_id, detail) in enumerate(
            [
                # `step_kind` is the SERVER's vocabulary (`ai_paths.STEP_KINDS`),
                # not a word chosen here. The first version of this fixture
                # invented `retrieval`/`knowledge`/`skill`/`answer`; the family
                # drew four rungs saying "No rung" and was RIGHT -- a kind it
                # does not know names no rung. Two wrong fixtures in a row, same
                # cause: guessing the wire instead of reading it.
                #
                # A tool_call that JUDGED its candidates: the nine parallel lists
                # migration 176 records, which `decodeAiPathBranches` reads. A
                # step whose `detail` is null renders "branch count unknown" --
                # correctly, because nothing was recorded. Both are here, so the
                # pass sees the family tell them apart.
                ("tool_call", "succeeded", "search_context", "context-topic", "top_01",
                 {
                     "retrieval_mode": "title>description>graph_neighbor",
                     "reached_count": 3, "selected_count": 1, "rejected_count": 2,
                     "candidate_ids": ["top_01", "top_02", "proc_02"],
                     "candidate_kinds": ["topic", "topic", "procedure"],
                     "candidate_titles": [
                         "ROAS calculation policy", "Attribution window", "budget-pacing-check",
                     ],
                     "candidate_scores": [0.91, 0.34, 0.12],
                     "candidate_tiers": ["title", "description", "graph_neighbor"],
                     "candidate_matched": [True, False, False],
                     "candidate_ranks": [1, 2, 3],
                     "candidate_fates": ["selected", "rejected", "rejected"],
                     # ENUMERATED reasons only (`core.candidate_fate.REJECTION_REASONS`,
                     # mirrored in `AI_PATH_REJECTION_REASONS`). Free prose is
                     # deliberately NOT drawn -- "an unenumerated reason is prose
                     # that reads like a reason". This fixture carried prose and
                     # the subtree drew none of it, correctly. Third wrong
                     # fixture, same cause; the wire is read, never guessed.
                     "candidate_reasons": [None, "below_cutoff", "out_of_scope"],
                 }),
                ("knowledge_read", "succeeded", None, "context-topic", "top_01", None),
                ("skill_step", "succeeded", None, "skill", "proc_01", None),
                ("data_read", "succeeded", None, None, None, None),
            ]
        )
    ],
}

FIXTURES: dict[str, object] = {
    "/api/context/graph/edges": {"edges": GRAPH_EDGES},
    "/api/context/graph": {"nodes": GRAPH_NODES, "edges": GRAPH_EDGES},
    "/api/context/topics": {
        "topics": TOPICS,
        "capabilities": {"can_write": True, "version_history": True, "usage": False},
    },
    "/api/context/procedures": {
        "procedures": PROCEDURES,
        "capabilities": {"can_write": True, "version_history": True, "usage": False},
    },
    "/api/context/recurrent-fates": {"fates": [], "minimum": 3},
    "/api/context/review-requests": {"requests": [], "can_resolve": True},
    "/api/context/business-taxonomy": {"org_id": "org_EXAMPLE", "domains": [], "classifications": []},
    "/api/context/business-links": {"links": []},
    "/api/datamodel/mappings": {"mappings": []},
    "/context/ai-paths/stats": {"total": 1, "by_outcome": {"succeeded": 1}},
    "/context/ai-paths/aip_EXAMPLE": AI_PATH,
    "/context/ai-paths": {"paths": [AI_PATH]},
}


def body_for(url: str) -> object:
    """Longest matching key wins: `/graph/edges` must beat `/graph`."""
    path = url.split("?", 1)[0]
    best: tuple[int, object] | None = None
    for key, body in FIXTURES.items():
        if key in path and (best is None or len(key) > best[0]):
            best = (len(key), body)
    if best is not None:
        return best[1]
    # An object read by id: answer with the row the collection already carries,
    # so the drawer opens on the same record the canvas showed.
    for row in TOPICS + PROCEDURES:
        if row["id"] in path:
            return row
    return {}


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


class Failure(Exception):
    """One stated defect, in the words a reader can act on."""


def _boxes(page, selector: str) -> list[dict]:
    return page.eval_on_selector_all(
        selector,
        """els => els.map(el => {
             const r = el.getBoundingClientRect();
             return {w: Math.round(r.width), h: Math.round(r.height),
                     x: Math.round(r.x), y: Math.round(r.y)};
           })""",
    )


def check_graph_is_laid_out(page) -> list[str]:
    """§B.3, the ELK graph. jsdom cannot see any of this."""
    notes: list[str] = []
    page.wait_for_selector('[data-testid="kg-canvas"]', timeout=15_000)
    page.wait_for_selector(".react-flow__node", timeout=15_000)
    boxes = _boxes(page, ".react-flow__node")
    if len(boxes) < 2:
        raise Failure(f"the canvas drew {len(boxes)} node(s); the bundle carried {len(GRAPH_NODES)}")
    flat = [b for b in boxes if b["w"] == 0 or b["h"] == 0]
    if flat:
        raise Failure(f"{len(flat)} node(s) rendered with a zero dimension -- drawn but invisible")
    # THE elkjs failure: the promise is dropped and every node keeps its initial
    # coordinate, so the graph is one pile. A test with a layout engine is the
    # only thing that can tell this from a correct graph.
    positions = {(b["x"], b["y"]) for b in boxes}
    if len(positions) < len(boxes):
        raise Failure(
            f"{len(boxes)} nodes occupy only {len(positions)} distinct position(s) -- "
            "the ELK layout did not place them"
        )
    edges = _boxes(page, ".react-flow__edge")
    if not edges:
        raise Failure(f"no edge drawn; the bundle carried {len(GRAPH_EDGES)}")
    notes.append(f"{len(boxes)} nodes at {len(positions)} positions, {len(edges)} edges")
    return notes


def check_drawer_stacks_above(page) -> list[str]:
    """§B.3, the drawers and their z-index. The check is not "is it in the DOM"
    -- a drawer painted UNDER the canvas is in the DOM too. It is: at the
    drawer's own centre, what does the browser say the top element is."""
    page.wait_for_selector(".react-flow__node", timeout=15_000)
    page.click(".react-flow__node")
    try:
        page.wait_for_selector('[data-testid="kg-drawer"]', timeout=10_000)
    except PlaywrightError as exc:
        raise Failure(f"clicking a node opened no drawer ({type(exc).__name__})") from exc
    verdict = page.evaluate(
        """() => {
             const drawer = document.querySelector('[data-testid="kg-drawer"]');
             if (!drawer) return {ok: false, why: 'drawer vanished'};
             const r = drawer.getBoundingClientRect();
             if (r.width === 0 || r.height === 0) return {ok: false, why: 'drawer has no size'};
             const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
             if (!top) return {ok: false, why: 'nothing at the drawer centre'};
             return {ok: drawer.contains(top) || top === drawer,
                     why: top.className && String(top.className).slice(0, 60),
                     w: Math.round(r.width), h: Math.round(r.height)};
           }"""
    )
    if not verdict.get("ok"):
        raise Failure(
            f"the drawer is not the top element at its own centre -- {verdict.get('why')}. "
            "It is painted under the canvas."
        )
    return [f"drawer {verdict['w']}x{verdict['h']} stacks above the canvas"]


def check_timeline_is_drawn(page) -> list[str]:
    """§B.3, the timeline.

    Asserted on the WIRE keys (`step_kind`, `owner_object_id`), not on a label:
    the first version of this fixture invented `kind`/`label`, the screen drew
    four rungs saying "No rung" and "branch count unknown" -- and it was RIGHT,
    because that is what an unreadable step honestly looks like. The fixture was
    the defect. `wire_step_projection` (`server/core/ai_paths.py:423`) is the
    contract, and it is the one this now follows.
    """
    page.wait_for_selector("text=AI Path aip_EXAMPLE", timeout=15_000)
    text = page.inner_text("body")
    # The family capitalizes the rung label, so the comparison is case-folded:
    # asserting on the raw wire word would fail on a screen that is correct.
    lowered = text.lower()
    missing = [
        str(step["step_kind"])
        for step in AI_PATH["steps"]
        if str(step["step_kind"]).lower() not in lowered
    ]
    if missing:
        raise Failure(f"the timeline did not draw the step kinds {missing}")
    if "No rung" in text:
        raise Failure(
            "a step rendered as 'No rung' -- the family could not read `step_kind` "
            "off the wire shape it was handed"
        )
    # The judged branches, decoded. A step that carries the nine parallel lists
    # must show a COUNT; one that carries nothing must say so, and the two must
    # not read alike.
    if "branch count unknown" not in text:
        raise Failure("a step with no recorded detail did not say its branch count is unknown")
    judged = next(step for step in AI_PATH["steps"] if step["detail"])
    count = len(judged["detail"]["candidate_ids"])
    if f"{count} branches considered" not in text:
        raise Failure(
            f"the step that recorded {count} judged candidates does not say so -- "
            "the console is dropping `detail`, which the server sends it"
        )
    # The reasons live behind the disclosure, so the pass OPENS it: a subtree
    # that renders only when expanded is exactly the kind of thing a suite with
    # no layout engine reports as present and a person finds empty.
    page.click(f"text={count} branches considered")
    page.wait_for_timeout(400)
    opened = page.inner_text("body")
    reasons = [r for r in judged["detail"]["candidate_reasons"] if r]
    if not any(reason in opened for reason in reasons):
        raise Failure("the branch subtree opens without any of the recorded reasons")
    return [
        f"{len(AI_PATH['steps'])} steps drawn, {count} judged branches decoded and expandable"
    ]


def check_no_sideways_scroll(page) -> list[str]:
    """Every screen. A console that scrolls sideways is a layout defect on any
    surface, and it is invisible to a suite with no viewport."""
    overflow = page.evaluate(
        """() => {
             const d = document.documentElement;
             return {scroll: d.scrollWidth, client: d.clientWidth};
           }"""
    )
    if overflow["scroll"] > overflow["client"] + 1:
        raise Failure(
            f"the page scrolls sideways ({overflow['scroll']}px of content in "
            f"{overflow['client']}px of viewport)"
        )
    return []


SCREENS = [
    ("ContextKnowledgeGraph", [check_graph_is_laid_out, check_drawer_stacks_above, check_no_sideways_scroll]),
    ("ContextKnowledgeLibrary", [check_no_sideways_scroll]),
    ("ContextSkillsRegistry", [check_no_sideways_scroll]),
    ("ContextObjectWorkbench", [check_no_sideways_scroll]),
    ("ContextAiPath", [check_timeline_is_drawn, check_no_sideways_scroll]),
]


def main() -> int:
    only = sys.argv[1:]
    screens = [s for s in SCREENS if not only or s[0] in only]
    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for name, checks in screens:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            console_errors: list[str] = []
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )
            page.route(
                "**/api/**",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(body_for(route.request.url)),
                ),
            )
            print(f"\n{name}")
            try:
                page.goto(f"{BASE}/debug/screen?name={name}", wait_until="networkidle")
                page.wait_for_timeout(1500)
                for check in checks:
                    for note in check(page):
                        print(f"  ok   {note}")
                    print(f"  PASS {check.__name__}")
            except Failure as exc:
                failures.append(f"{name}: {exc}")
                print(f"  FAIL {exc}")
            except PlaywrightError as exc:
                failures.append(f"{name}: {type(exc).__name__} -- {str(exc).splitlines()[0]}")
                print(f"  FAIL {type(exc).__name__} -- {str(exc).splitlines()[0]}")
            # A console error is REPORTED, never fatal: React logs warnings that
            # are not layout defects, and a pass that dies on the first warning
            # would be turned off within a week.
            for err in console_errors[:3]:
                print(f"  note console error: {err[:140]}")
            shot = OUT / f"{name}.png"
            try:
                page.screenshot(path=str(shot), full_page=True)
                print(f"  shot {shot}")
            except PlaywrightError:
                print("  shot could not be taken")
            page.close()
        browser.close()

    print()
    if failures:
        print(f"{len(failures)} defect(s):")
        for line in failures:
            print(f"  - {line}")
        return 1
    print(f"{len(screens)} Context Hub screen(s) pass in a real browser.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
