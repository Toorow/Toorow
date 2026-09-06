import { act, fireEvent, render, screen, within } from "@testing-library/react";
import ProjectOverview, { type OwnerReference } from "../shell/pages/ProjectOverview";

function response(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

const DATA_OWNER: OwnerReference = {
  surface: "project",
  workspace: "data",
  section: "datastreams",
  global_surface: null,
  global_section: null,
  object_type: "datastream",
  object_id: "ds-1",
  tab: "runs",
  action: null,
  version_id: null,
  evidence_id: "ev-1",
};

const READY = {
  schema_version: "project-overview.v1",
  project: {
    id: "project-1",
    name: "Acme",
    organization: { id: "org-1", name: "Acme Org" },
    business_domains: [{ id: "domain-1", name: "Commerce" }],
    active_configuration_version_id: "cfg-3",
    as_of: "2026-07-29T09:00:00Z",
  },
  posture: {
    operational_health: { state: "ready", explanation: "Current publications are verified.", evidence_horizon: "2026-07-28", owner: { ...DATA_OWNER, section: "data-overview", object_type: null, object_id: null, tab: null } },
    trust_readiness: { state: "degraded", explanation: "One regression requires review.", evidence_horizon: "2026-07-29", owner: { ...DATA_OWNER, workspace: "test", section: "regression-runs", object_type: null, object_id: null, tab: null } },
    business_signals: { state: "unknown", explanation: "No persisted business signal is available.", evidence_horizon: null, owner: { ...DATA_OWNER, workspace: "analyze", section: "explore", object_type: null, object_id: null, tab: null } },
    limiting_dimension: "trust_readiness",
  },
  next_action: { label: "Open Runs", permitted: true, handoff: null, cause: "The latest run failed.", owner: DATA_OWNER },
  attention: {
    items: [{
      id: "att-1",
      root_cause_key: "run:failed:ds-1",
      priority_class: 1,
      cause: "The latest run failed.",
      impact: ["Project data trust is limited."],
      scope: ["Search"],
      status: "blocked",
      first_observed_at: "2026-07-28T09:00:00Z",
      last_observed_at: "2026-07-29T09:00:00Z",
      evidence_horizon: "2026-07-28",
      owner: DATA_OWNER,
      action: { label: "Open Runs", permitted: true },
    }],
    total: 1,
    has_more: false,
  },
  coverage: [
    { kind: "data", key: "publication", state: "ready", denominator: 1, complete: 1, gaps: [], evidence_horizon: "2026-07-28", owner: DATA_OWNER, active: { state: "trusted", version_id: null }, pending: null },
    { kind: "capability", key: "currency_fx", state: "ready", denominator: 1, complete: 1, gaps: [], evidence_horizon: null, owner: { ...DATA_OWNER, surface: "global", workspace: null, section: null, global_surface: "project-settings", global_section: "capabilities", object_type: null, object_id: null, tab: null }, active: { state: "ready", version_id: "fx-v1" }, pending: null },
  ],
  outcomes: {
    status: "ready",
    items: [{
      id: "ins-1",
      label: "Paid revenue recovered week over week",
      kind: "validated_signal",
      period: { start: "2026-07-20", end: "2026-07-26" },
      freshness: "2026-07-27T06:00:00Z",
      limitations: ["Excludes one Datastream still backfilling"],
      provenance: { kind: "persisted_daily_insight", id: "ins-1" },
      owner: { ...DATA_OWNER, workspace: "analyze", section: "explore", object_type: "result", object_id: "ins-1", tab: null },
    }],
  },
  changes: {
    status: "ready",
    items: [{
      id: "chg-1",
      kind: "configuration_change",
      state: "confirmed",
      summary: "Project configuration changed.",
      occurred_at: "2026-07-28T11:00:00Z",
      version_id: "cfg-3",
      owner: { ...DATA_OWNER, surface: "global", workspace: null, section: null, global_surface: "project-settings", global_section: "changes", object_type: null, object_id: null, tab: null },
    }],
  },
};

beforeEach(() => {
  vi.restoreAllMocks();
  // Stubbing `fetch` is how this file drives the page, so the bearer is the one
  // thing a stub can silently swallow — the page must go through the apiFetch
  // seam, and the first test below asserts the header is really on the wire.
  localStorage.setItem("api_token", "tok-overview");
});
afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

test("renders the five stable zones and three separate posture dimensions", async () => {
  const fetchMock = vi.fn().mockResolvedValue(response(200, READY));
  vi.stubGlobal("fetch", fetchMock);

  render(<ProjectOverview projectId="project-1" />);

  expect(await screen.findByRole("heading", { name: "Acme" })).toBeInTheDocument();
  const zoneNames = ["Project posture", "Next action & attention", "Coverage & readiness", "Recent outcomes & signals", "Recent changes"];
  const zoneHeadings = zoneNames.map((name) => screen.getByRole("heading", { name }));
  expect(zoneHeadings.map((heading) => heading.textContent)).toEqual(zoneNames);
  expect(screen.getByText("Operational health")).toBeInTheDocument();
  expect(screen.getByText("Trust & readiness")).toBeInTheDocument();
  expect(screen.getByText("Business signals")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/projects/project-1/overview",
    expect.objectContaining({ method: "GET", cache: "no-store" }),
  );
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok-overview");
});

test("opens the exact semantic owner from the primary action", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, READY)));
  const onOpenOwner = vi.fn();

  render(<ProjectOverview projectId="project-1" onOpenOwner={onOpenOwner} />);
  fireEvent.click(await screen.findByRole("button", { name: "Open Runs" }));

  expect(onOpenOwner).toHaveBeenCalledWith(DATA_OWNER);
});

test("renders a nondisclosing denied state", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(404, { code: "not_found", message: "Project not found" })));

  render(<ProjectOverview projectId="hidden" />);

  // 76-5: the denied answer was a `PageHeader` and nothing else -- a title, a
  // sentence and no way out. It is `ProjectNotFound` now, the console's one
  // wording for "this address names nothing you can see", which names the
  // control that changes project instead of mounting a second one.
  expect(await screen.findByText("Project not found")).toBeInTheDocument();
  expect(screen.getByText(/project switcher at the top of the screen/)).toBeInTheDocument();
  expect(screen.queryByText("Acme")).not.toBeInTheDocument();
});

test("removes prior Project evidence immediately during a scope change", async () => {
  let resolveSecond: ((value: Response) => void) | undefined;
  const second = new Promise<Response>((resolve) => { resolveSecond = resolve; });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response(200, READY)).mockReturnValueOnce(second));
  const view = render(<ProjectOverview projectId="project-1" />);
  expect(await screen.findByRole("heading", { name: "Acme" })).toBeInTheDocument();

  await act(async () => {
    view.rerender(<ProjectOverview projectId="project-2" />);
    await Promise.resolve();
  });

  // The bare `<p>` became the shared `Loading`, so the wording is the console's
  // and not this screen's -- ellipsis included.
  // `role="status"` takes its accessible name from the author, and the shared
  // `Loading` sets none -- the words ARE the announcement, so the words are what
  // is pinned.
  expect(screen.getByRole("status")).toHaveTextContent("Loading Project Overview…");
  expect(screen.queryByRole("heading", { name: "Acme" })).not.toBeInTheDocument();
  await act(async () => { resolveSecond?.(response(200, { ...READY, project: { ...READY.project, id: "project-2", name: "Beta" } })); await second; });
  expect(await screen.findByRole("heading", { name: "Beta" })).toBeInTheDocument();
});

test("keeps the empty Project on the same page and opens the Data-owned wizard", async () => {
  const owner = { ...DATA_OWNER, object_id: "new", tab: null, action: "create", evidence_id: null };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    project: { ...READY.project, name: "Empty" },
    next_action: { label: "Add Datastream", permitted: true, handoff: null, cause: "No Datastream is attached.", owner },
    attention: { items: [], total: 0, has_more: false },
    coverage: [],
  })));
  const onOpenOwner = vi.fn();

  render(<ProjectOverview projectId="empty" onOpenOwner={onOpenOwner} />);
  fireEvent.click(await screen.findByRole("button", { name: "Add Datastream" }));

  expect(onOpenOwner).toHaveBeenCalledWith(owner);
  expect(screen.getByRole("heading", { name: "Project posture" })).toBeInTheDocument();
});


test("every persisted outcome carries its period, freshness, limitations and provenance", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, READY)));

  render(<ProjectOverview projectId="project-1" />);

  expect(await screen.findByText("Paid revenue recovered week over week")).toBeInTheDocument();
  expect(screen.getByText("2026-07-20 to 2026-07-26")).toBeInTheDocument();
  // 76-5: the freshness was a raw ISO instant read out to a person. It is a
  // `<Timestamp>` now, so what is pinned is the MACHINE half -- the rendering is
  // the console's one locale and asserting it here would pin a locale twice.
  expect(screen.getByTestId("outcome-freshness")).toHaveAttribute("datetime", "2026-07-27T06:00:00.000Z");
  expect(screen.getByText("Excludes one Datastream still backfilling")).toBeInTheDocument();
  expect(screen.getByText(/Persisted daily insight/)).toBeInTheDocument();
});

test("a silent day says why it was silent, not only that it was", async () => {
  // `proactive-assertions.md`: "Silence is a state, and it is disclosed". A blocked
  // run and a quiet day both leave the Project with nothing to show; only one of
  // them means nobody could look, and the reader must be able to tell them apart.
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    outcomes: { status: "unavailable", items: [] },
    posture: {
      ...READY.posture,
      business_signals: {
        ...READY.posture.business_signals,
        explanation:
          "No persisted business signal is available. The daily insight run was blocked: "
          + "the day's data was not ready. latest data 2026-07-19 is behind target 2026-07-21",
      },
    },
  })));

  render(<ProjectOverview projectId="project-1" />);

  expect(await screen.findByText(/latest data 2026-07-19 is behind target 2026-07-21/)).toBeInTheDocument();
});

test("the empty outcomes panel says why it is empty, not only that it is", async () => {
  // The SECOND place that asserts a quiet Project. Repairing the posture sentence
  // alone left this one hardcoded, saying the same false thing one panel below.
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    outcomes: {
      status: "unavailable",
      items: [],
      insight_silence: {
        state: "blocked",
        insight_date: "2026-07-21",
        explanation: "The daily insight run was blocked: the day's data was not ready. gap in cost",
        reason: "gap in cost",
      },
    },
  })));

  render(<ProjectOverview projectId="project-1" />);

  expect(await screen.findByText(/No recent outcomes/)).toBeInTheDocument();
  expect(screen.getByText(/The daily insight run was blocked/)).toBeInTheDocument();
  expect(screen.getByText(/gap in cost/)).toBeInTheDocument();
});

test("a project that is genuinely quiet is not given a reason it never had", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    outcomes: { status: "empty", items: [] },
  })));

  render(<ProjectOverview projectId="project-1" />);

  expect(await screen.findByText(
    "No persisted business signal is available for this project.",
  )).toBeInTheDocument();
});

// A CONFIDENCE WORD IS NEVER SHOWN WITHOUT ITS AUTHOR.
// `overview.md:277`: a surfaced signal must not state a confidence level that no
// server evidence backs. `proactive-assertions.md` goes one step further and
// refuses the level itself when the AUTHOR of the claim chose it. The word the
// screen prints is now the server's measurement (`authorship.derivedConfidence`,
// from `core.insight_confidence`); the model's own word is reported beside it,
// under its own name, and decides nothing.

test("the confidence a reader sees is the server's measurement, with its terms", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    outcomes: {
      status: "ready",
      items: [{
        ...READY.outcomes.items[0],
        confidence: "high",
        authorship: {
          modelAuthored: ["insight.title", "insight.summary"],
          confidence: "derived",
          declaredConfidence: "high",
          derivedConfidence: {
            reading: "low",
            terms: { completeness: 1, freshness: 1, provenance: 0.5 },
            limitingTerm: "provenance",
            unknownTerms: [],
            citedMembers: ["metric:revenue"],
            unresolvedRefs: [],
          },
          evidenceRefs: ["metric:revenue"],
        },
      }],
    },
  })));

  render(<ProjectOverview projectId="project-1" />);

  // The model shouted "high"; the server measured "low", and the server wins.
  expect(await screen.findByText(/low — derived by the server from the evidence this insight cites, limited by provenance/)).toBeInTheDocument();
  expect(screen.getByText(/high — the model's own estimate of its claim; it decides nothing here/)).toBeInTheDocument();
});

test("an insight the server could not measure reads unmeasurable, never a level", async () => {
  // The attack: an envelope published before the derivation existed. Promoting
  // the model's word into the gap is exactly the defect; so is a default level.
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    outcomes: {
      status: "ready",
      items: [{ ...READY.outcomes.items[0], confidence: "high" }],
    },
  })));

  render(<ProjectOverview projectId="project-1" />);

  await screen.findByRole("heading", { name: "Recent outcomes & signals" });
  expect(screen.getByText(/Unmeasurable — this insight was published before toorow derived confidence/)).toBeInTheDocument();
  expect(screen.queryByText("high")).not.toBeInTheDocument();
});

// `proactive-assertions.md` ("Incomplete if"): model-authored prose is not
// visibly distinguishable from cited server data.
test("the model-authored headline carries the marker and server data does not", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    outcomes: {
      status: "ready",
      // The shared fixture's `label` is not what the server emits for an insight:
      // `_signal_items` spreads the projection, which carries `title` and
      // `summary` — the two fields `authorship.modelAuthored` names. The item is
      // rebuilt here so the test attacks the real shape.
      items: [{
        ...READY.outcomes.items[0],
        label: undefined,
        title: "Paid revenue recovered week over week",
        summary: "Revenue is back above the prior week.",
        authorship: {
          modelAuthored: ["insight.title", "insight.summary"],
          confidence: "derived",
          declaredConfidence: null,
          derivedConfidence: null,
          evidenceRefs: ["metric:revenue"],
        },
      }],
    },
  })));

  const { container } = render(<ProjectOverview projectId="project-1" />);

  await screen.findByRole("heading", { name: "Recent outcomes & signals" });
  const marked = container.querySelectorAll('[data-slot="model-authored"]');
  expect(marked.length).toBeGreaterThan(0);
  expect([...marked].map((node) => node.getAttribute("data-model-authored"))).toContain("insight.title");
  // The panel heading is server-composed and stays bare.
  expect(screen.getByRole("heading", { name: "Recent outcomes & signals" }).closest("[data-slot='model-authored']")).toBeNull();
});

test("a recent change is dated, versioned and opens its exact owner record", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, READY)));
  const onOpenOwner = vi.fn();

  render(<ProjectOverview projectId="project-1" onOpenOwner={onOpenOwner} />);

  expect(await screen.findByText("Project configuration changed.")).toBeInTheDocument();
  // 76-5: the instant was raw ISO and the version was a bare ULID inside the
  // same sentence. The instant is a `<Timestamp>`, the version an `ObjectId`.
  expect(screen.getByTestId("change-occurred")).toHaveAttribute("datetime", "2026-07-28T11:00:00.000Z");
  expect(screen.getByTitle("Change version: cfg-3")).toHaveTextContent("cfg-3");

  fireEvent.click(screen.getByRole("button", { name: "Open record" }));
  expect(onOpenOwner).toHaveBeenCalledWith(
    expect.objectContaining({ surface: "global", global_surface: "project-settings", global_section: "changes" }),
  );
});

test("coverage states its evidence horizon and separates active from pending", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, READY)));

  render(<ProjectOverview projectId="project-1" />);

  await screen.findByRole("heading", { name: "Coverage & readiness" });
  // 76-5: the active state was the wire word and the version a ULID in a
  // parenthesis. The state is the declared label; the version is an `ObjectId`
  // with the full value on its title.
  expect(screen.getByText(/Active: Ready/)).toBeInTheDocument();
  expect(screen.getByTitle("Active version: fx-v1")).toHaveTextContent("fx-v1");
  expect(screen.getAllByText(/Evidence horizon: 28 Jul 2026/).length).toBeGreaterThan(0);
});

test("zero applicable objects reads Not applicable, never 0/0 and never 100%", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    coverage: [{
      kind: "context", key: "context", state: "unknown", status: "empty",
      denominator: 0, complete: 0, gaps: ["No governed context evidence"],
      evidence_horizon: null, owner: DATA_OWNER, active: null, pending: null,
    }],
  })));

  render(<ProjectOverview projectId="project-1" />);

  expect(await screen.findByText("Not applicable")).toBeInTheDocument();
  expect(screen.queryByText("0/0")).not.toBeInTheDocument();
  expect(screen.queryByText("100%")).not.toBeInTheDocument();
});

test("a media plan pace alert reads as a signal that names what it is not", async () => {
  // AI-190. The console screen that showed a plan's pacing was removed with
  // `ui/admin/src/mediaplans/`; the governed alert is what a person can still
  // see here, and it must not read as the whole plan-versus-actual reading
  // (`analyze-and-test.md`, Plan-versus-actual).
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    outcomes: {
      status: "ready",
      items: [{
        id: "fire_1",
        title: "Budget overrun on line Display FR",
        kind: "governed_alert",
        alert_type: "mediaplan_pace",
        severity: "error",
        observed_value: 0.31,
        threshold: 0.1,
        period: "2026-08-03",
        freshness: "2026-08-04T06:00:00Z",
        limitations: ["Threshold breach only; the full reading it came from is not reachable from this console yet."],
        provenance: { kind: "alert_firing:mediaplan_pace", id: "fire_1" },
      }],
    },
  })));

  render(<ProjectOverview projectId="project-1" />);

  expect(await screen.findByText("Budget overrun on line Display FR")).toBeInTheDocument();
  expect(screen.getByText("Governed alert")).toBeInTheDocument();
  // 76-5: the provenance kind was the wire token raw. `wireWord` takes the
  // base's punctuation off it and changes nothing else.
  expect(screen.getByText(/Alert firing:mediaplan pace/)).toBeInTheDocument();
  expect(screen.getByText(/not reachable from this console yet/)).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Une file BORNEE dit qu'elle est bornee.
// ---------------------------------------------------------------------------
//
// Le serveur envoyait deja `total` et `has_more` ; l'ecran n'affichait ni l'un
// ni l'autre, donc une file tronquee se lisait exactement comme une file
// complete. Une personne pouvait fermer la page en croyant avoir vu tout ce qui
// demande de l'attention. « 12 sur 47 » n'est pas le meme fait que « 12 ».

it("says how many attention items are NOT on the page", async () => {
  const truncated = {
    ...READY,
    attention: { ...READY.attention, total: 47, has_more: true },
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, truncated)));
  render(<ProjectOverview projectId="p1" onOpenOwner={vi.fn()} />);

  const line = await screen.findByTestId("attention-truncated");
  expect(line).toHaveTextContent("47");
  expect(line).toHaveTextContent(/not on this page/i);
});

it("says nothing when the queue is whole", async () => {
  const whole = {
    ...READY,
    attention: { ...READY.attention, total: READY.attention.items.length, has_more: false },
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, whole)));
  render(<ProjectOverview projectId="p1" onOpenOwner={vi.fn()} />);

  await screen.findByText("Next action & attention");
  // `has_more` decide, PAS `total > items.length` : le filtre de l'ecran retire
  // aussi la ligne de prochaine action, donc comparer les longueurs annoncerait
  // une troncature sur une file que le serveur a envoyee entiere.
  expect(screen.queryByTestId("attention-truncated")).toBeNull();
});

// --------------------------------------------------------------------------
// The composed-but-unrendered payload, and the promised-but-absent window.
// --------------------------------------------------------------------------

const READINESS = {
  version: "rdy-abc",
  components: ["project_foundation", "source", "datastream", "governance", "first_value"],
  project_foundation: { state: "ready", owner: { ...DATA_OWNER, surface: "global", workspace: null, section: null, global_surface: "project-settings", global_section: "general", object_type: null, object_id: null, tab: null }, evidence_ref: "cfg-3" },
  source: { state: "ready", owner: { ...DATA_OWNER, section: "sources", object_type: null, object_id: null, tab: null }, evidence_ref: "conn-1" },
  datastream: { state: "ready", owner: { ...DATA_OWNER, tab: null }, evidence_ref: "ds-1" },
  governance: { state: "blocked", owner: { ...DATA_OWNER, workspace: "governance", section: "controls-quality", object_type: null, object_id: null, tab: null }, evidence_ref: null },
  first_value: { state: "blocked", owner: { ...DATA_OWNER, workspace: "analyze", section: "renders", object_type: null, object_id: null, tab: null }, evidence_ref: null },
};

it("renders the setup readiness it was already composing", async () => {
  // The server composed `readiness` on every request and `ProjectOverviewEnvelope`
  // did not even declare it, so `overview.md:140` -- "Overview summarizes current
  // setup posture (...) then deep-links to Getting Started or the owning
  // workbench" -- was answered by a payload nobody could see.
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, { ...READY, readiness: READINESS })));
  render(<ProjectOverview projectId="p1" onOpenOwner={vi.fn()} />);

  await screen.findByText("Setup readiness");
  expect(screen.getByText("Governance")).toBeInTheDocument();
  // `wireWord`, not a local Title-Caser: `console-presentation.md` §4 reserves
  // Title Case for the ratified nouns, so a stored component key is sentence
  // case ("First value") and a reader can tell the two apart.
  expect(screen.getByText("First value")).toBeInTheDocument();
  // 76-5: the evidence reference was a bare identifier in a sentence. `ObjectId`
  // keeps the full value on the title and names what it identifies.
  expect(screen.getByTitle("Project foundation evidence: cfg-3")).toHaveTextContent("cfg-3");
});

it("opens the workbench that owns a blocked readiness component", async () => {
  const onOpenOwner = vi.fn();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, { ...READY, readiness: READINESS })));
  render(<ProjectOverview projectId="p1" onOpenOwner={onOpenOwner} />);

  await screen.findByText("Setup readiness");
  const governance = screen.getByText("Governance").closest("li")!;
  fireEvent.click(within(governance).getByRole("button", { name: "Open owner" }));
  expect(onOpenOwner).toHaveBeenCalledWith(expect.objectContaining({ workspace: "governance", section: "controls-quality" }));
});

it("states the window the governed alerts were read over", async () => {
  // `project_overview.py` CLAIMED in a comment that "the window is stated on the
  // zone's evidence horizon". No field carried it, so an empty alert list over
  // seven days read exactly like one over seven minutes.
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, { ...READY, outcomes: { ...READY.outcomes, alert_window_hours: 168 } })));
  render(<ProjectOverview projectId="p1" onOpenOwner={vi.fn()} />);

  // `day(s)` was this screen's hand-rolled plural. `formatCount` agrees the noun.
  expect(await screen.findByText(/Governed alerts cover the last 7 days/)).toBeInTheDocument();
});

it("draws no evidence link for an insight that was never rendered", async () => {
  // The owner used to be `object_type: "result"` with a daily-insight id -- a
  // Result that cannot exist. No render snapshot now means no owner, and the
  // screen draws no button rather than a link false by construction.
  const unrendered = { ...READY, outcomes: { ...READY.outcomes, items: [{ ...READY.outcomes.items[0], owner: undefined, limitations: ["This insight was not rendered, so it has no artifact to open."] }] } };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, unrendered)));
  render(<ProjectOverview projectId="p1" onOpenOwner={vi.fn()} />);

  await screen.findByText("Recent outcomes & signals");
  expect(screen.queryByRole("button", { name: "Open evidence" })).toBeNull();
  expect(screen.getByText(/not rendered, so it has no artifact/)).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// GOVERNED ALERTS OPEN SOMETHING -- gap 5 of the audit of 2026-08-17.
// The item rendered, stated a limitation, and had no `owner` key at all, so the
// screen drew no button: the most operationally urgent thing on the page was the
// one thing a person could not follow. Mounted on purpose -- the defect was
// exactly the distance between a well-formed payload and a clickable control.
// ---------------------------------------------------------------------------

const QUALITY_ALERT = {
  id: "fire_1",
  title: "Feed arrived 9h late",
  kind: "governed_alert",
  alert_type: "dq_timeliness",
  metric: "arrival_delay_hours",
  severity: "warning",
  datastream_id: "ds_01EXAMPLE",
  period: "2026-08-20",
  freshness: "2026-08-21T06:00:00Z",
  limitations: ["One observation against one threshold, not the full reading behind it."],
  provenance: { kind: "alert_firing:dq_timeliness", id: "fire_1" },
  owner: {
    surface: "project",
    workspace: "data",
    section: "datastreams",
    global_surface: null,
    global_section: null,
    object_type: "datastream",
    object_id: "ds_01EXAMPLE",
    tab: "runs",
    action: null,
    version_id: null,
    evidence_id: "fire_1",
  },
};

it("a governed alert opens the Datastream evidence it accuses", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    outcomes: { status: "ready", items: [QUALITY_ALERT], alert_window_hours: 168 },
  })));
  const onOpenOwner = vi.fn();

  render(<ProjectOverview projectId="p1" onOpenOwner={onOpenOwner} />);

  const alert = (await screen.findByText("Feed arrived 9h late")).closest("li")!;
  fireEvent.click(within(alert).getByRole("button", { name: "Open evidence" }));

  expect(onOpenOwner).toHaveBeenCalledWith(QUALITY_ALERT.owner);
});

it("a governed alert with nowhere to go says what is missing instead of going silent", async () => {
  // A Project-scoped finding names no Datastream, and no console list contains
  // it. `overview.md` (amendment of 2026-08-21) refuses a plausible destination:
  // the item carries no owner AND names what is missing on the item itself.
  const orphan = {
    ...QUALITY_ALERT,
    id: "fire_2",
    title: "Unresolved geography above threshold",
    alert_type: "dq_geography",
    datastream_id: null,
    owner: undefined,
    limitations: [
      "One observation against one threshold, not the full reading behind it.",
      "This alert names no Datastream, so there is nothing to open here; ask the assistant for the daily report to read the alert in full.",
    ],
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...READY,
    outcomes: { status: "ready", items: [orphan], alert_window_hours: 168 },
  })));

  render(<ProjectOverview projectId="p1" onOpenOwner={vi.fn()} />);

  const alert = (await screen.findByText("Unresolved geography above threshold")).closest("li")!;
  expect(within(alert).queryByRole("button", { name: "Open evidence" })).toBeNull();
  expect(within(alert).getByText(/names no Datastream, so there is nothing to open here/)).toBeInTheDocument();
});
