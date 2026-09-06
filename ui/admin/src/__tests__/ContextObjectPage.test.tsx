import { useState } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ContextObjectPage from "../ContextObjectPage";

const TOPIC = {
  id: "top_1", project_id: "p1", title: "Revenue policy", body_md: "Canonical revenue.",
  status: "active", owner: null, created_by: "owner@example.com",
  created_at: "2026-07-20T10:00:00Z", updated_at: "2026-07-21T10:00:00Z", version_number: 2,
};
function response(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}
afterEach(() => vi.restoreAllMocks());

it("loads a URL-scoped context topic content tab", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string) => { calls.push(String(url)); return Promise.resolve(response(200, TOPIC)); }));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("Revenue policy")).toBeInTheDocument();
  expect(screen.getByText("Canonical revenue.")).toBeInTheDocument();
  expect(calls).toContain("/api/context/topics/top_1?project_id=p1");
  // L'intention d'origine de cette assertion : l'onglet `content` ne va PAS
  // chercher les versions. Elle l'exprimait par une liste exacte, que la file
  // des remarques allonge legitimement (AI-158) — le fait teste est l'absence
  // de l'appel versions, pas le nombre d'appels.
  expect(calls.filter((url) => url.includes("/versions?"))).toEqual([]);
});

/**
 * ── A VERSION IS AN ADDRESS, AND THE ADDRESS OPENS IT
 *
 * The ledger used to be a disclosure list: `v2` expanded in place, and
 * `objectSurfaces.tsx` refused `/tab/versions/version/2` out loud because no
 * Context Hub workbench accepted a version identifier. Story 49.6 AC1/AC3/AC4
 * and `README.md` invariant 7 want the exact version deep-linkable — an AI Path
 * step pins a Skill version, and that pin had nowhere to land.
 */
const TOPIC_VERSIONS = {
  versions: [
    { version_number: 2, changed_by: "owner@example.com", changed_at: "2026-07-21T10:00:00Z", status: "active", title: "Revenue policy", body_md: "Canonical revenue." },
    { version_number: 1, changed_by: "owner@example.com", changed_at: "2026-07-20T10:00:00Z", status: "active", title: "Revenue policy", body_md: "The first wording." },
  ],
};

/** The workbench as the console mounts it: a version pins into the address, and
 *  the address is what the workbench reads back. */
function VersionAddressable({ initialVersionId = null }: { initialVersionId?: string | null }) {
  const [versionId, setVersionId] = useState<string | null>(initialVersionId);
  return (
    <ContextObjectPage
      projectId="p1"
      kind="topic"
      objectId="top_1"
      tab="versions"
      versionId={versionId}
      onNavigateTab={vi.fn()}
      onOpenVersion={setVersionId}
      versionHref={(id) => `/org/o1/project/p1/context-hub/knowledge-library/object/context-topic/top_1/tab/versions/version/${id}`}
      onBack={vi.fn()}
    />
  );
}

function stubVersionedTopic() {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    calls.push(String(url));
    if (String(url).includes("/versions?")) return Promise.resolve(response(200, TOPIC_VERSIONS));
    return Promise.resolve(response(200, TOPIC));
  }));
  return calls;
}

it("loads immutable version evidence only on the versions tab, and each row is an address", async () => {
  const calls = stubVersionedTopic();
  render(<VersionAddressable />);
  const version = await screen.findByText("v2");
  expect(calls).toContain("/api/context/topics/top_1/versions?project_id=p1");
  // The row is a real link: copy, middle-click and "open in a new tab" work
  // because the address exists, not because a handler simulates them.
  expect(version.closest("a")).toHaveAttribute(
    "href",
    "/org/o1/project/p1/context-hub/knowledge-library/object/context-topic/top_1/tab/versions/version/2",
  );
});

it("opens the pinned version's own snapshot from the row", async () => {
  const user = userEvent.setup();
  stubVersionedTopic();
  render(<VersionAddressable />);
  await user.click(await screen.findByText("v1"));
  const pinned = await screen.findByTestId("context-version-pinned");
  expect(pinned).toHaveTextContent("The first wording.");
  // The object has moved on, and the snapshot says so rather than letting a
  // reader take a retired wording for the current one.
  expect(pinned).toHaveTextContent("has since moved to v2");
  expect(screen.getByLabelText("Version 1 snapshot")).toHaveTextContent("The first wording.");
});

it("refuses a pinned version the ledger does not hold instead of showing the current one", async () => {
  stubVersionedTopic();
  render(<VersionAddressable initialVersionId="7" />);
  const refusal = await screen.findByTestId("context-version-unknown");
  expect(refusal).toHaveTextContent("This address pins version v7");
  expect(refusal).toHaveTextContent("none of them is v7");
  // THE MUTATION THIS TEST EXISTS FOR: no snapshot is opened in its place.
  expect(screen.queryByTestId("context-version-pinned")).toBeNull();
  expect(screen.queryByLabelText("Version 2 snapshot")).toBeNull();
});

// ── The Usage tab reads the ONE relationship authority
//
// It used to read EVERY edge of the project plus the whole graph bundle and
// filter both in the browser: the fan-out `context-hub.md` forbids, blind to
// the three endpoint types the authority exists for (`semantic_view`,
// `metric`, `datastream`), which the legacy projection cannot hold.

const FACET = {
  state: "ready",
  node: { type: "topic", id: "top_1" },
  outgoing: [
    {
      relationship_id: "crel_1", relationship_kind: "explains", status: "active",
      provenance: "console", created_by: "owner@example.com",
      created_at: "2026-08-28T10:00:00Z", direction: "outgoing",
      other: { type: "semantic_view", id: "sv_revenue" },
      owner: { surface: "semantic-model", href: "/api/projects/p1/governance/semantic-model" },
    },
  ],
  incoming: [
    {
      relationship_id: "crel_2", relationship_kind: "depends_on", status: "active",
      provenance: "console", created_by: "owner@example.com",
      created_at: "2026-08-27T10:00:00Z", direction: "incoming",
      other: { type: "procedure", id: "proc_9" },
      owner: { surface: "skills", href: "/api/context/procedures/proc_9" },
    },
  ],
  limit: 100,
  truncated: false,
};

function stubUsage(facet: unknown = FACET) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const href = String(url);
    calls.push(href);
    if (href.startsWith("/api/context/relationships")) return Promise.resolve(response(200, facet));
    return Promise.resolve(response(200, TOPIC));
  }));
  return calls;
}

it("asks the relationship authority for THIS node, and reads no project-wide list", async () => {
  const calls = stubUsage();
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("Used by / Related")).toBeInTheDocument();
  // The one door, naming the node by TYPE and id — an id alone names nothing:
  // the eight endpoint types share one identifier space.
  expect(calls).toContain("/api/context/relationships?project_id=p1&node_type=topic&node_id=top_1");
  // ...and neither of the two browser fan-outs it replaced.
  expect(calls.filter((url) => url.startsWith("/api/context/graph/edges"))).toEqual([]);
  expect(calls.filter((url) => url.startsWith("/api/context/graph?"))).toEqual([]);
});

it("renders BOTH directions, each said in words rather than as an arrow", async () => {
  stubUsage();
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByTestId("graph-links-outgoing")).toHaveTextContent("This object points at (1)");
  expect(screen.getByTestId("graph-links-incoming")).toHaveTextContent("Used by (1)");
  expect(screen.getByTestId("graph-link-row-crel_1")).toHaveTextContent("sv_revenue");
  expect(screen.getByTestId("graph-link-row-crel_2")).toHaveTextContent("proc_9");
});

it("names a Semantic View peer, which the legacy projection could not even hold", async () => {
  stubUsage();
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  const row = await screen.findByTestId("graph-link-row-crel_1");
  expect(row).toHaveTextContent("Semantic View");
  // No workbench here answers for it, so the row says WHERE it is held rather
  // than offering a control that opens nothing.
  expect(row).toHaveTextContent("Held on Semantic model.");
  expect(screen.queryByTestId("graph-link-open-crel_1")).not.toBeInTheDocument();
});

it("renders the typed unavailable sentence rather than an empty list", async () => {
  // The route answers 200 with `state: unavailable` precisely so this panel can
  // say "I could not look" instead of "nothing is related".
  stubUsage({
    state: "unavailable",
    node: { type: "topic", id: "top_1" },
    reason: "related_items_unreadable",
    message: "Open this panel again in a moment: the related items could not be read.",
  });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  const notice = await screen.findByTestId("graph-links-unavailable");
  expect(notice).toHaveTextContent(/could not be read/);
  expect(notice).toHaveTextContent(/not an object without relations/);
  expect(screen.queryByTestId("graph-links-outgoing")).not.toBeInTheDocument();
  expect(screen.queryByText("Not linked to anything yet")).not.toBeInTheDocument();
});

it("names the gesture that fills an empty facet", async () => {
  stubUsage({ state: "ready", node: { type: "topic", id: "top_1" }, outgoing: [], incoming: [], limit: 100, truncated: false });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("Not linked to anything yet")).toBeInTheDocument();
  expect(screen.getByText(/Draw a link between two nodes on the knowledge graph/)).toBeInTheDocument();
});

it("offers no write control on this tab — no create, no delete", async () => {
  stubUsage();
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  await screen.findByTestId("graph-links-outgoing");
  expect(screen.queryByTestId("btn-add-graph-edge")).not.toBeInTheDocument();
  expect(screen.queryByTestId("graph-edge-add-form")).not.toBeInTheDocument();
  expect(screen.queryByTestId("btn-delete-edge-crel_1")).not.toBeInTheDocument();
});

it("reads in English, on an English console", async () => {
  // The superseded component rendered « Liaisons », « + Ajouter », « Type
  // cible », « plateforme » and « Supprimer la liaison » on this very tab.
  stubUsage();
  const { container } = render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  await screen.findByTestId("graph-links-outgoing");
  const text = container.textContent ?? "";
  for (const french of ["Liaisons", "Ajouter", "Annuler", "Type cible", "Type de liaison", "plateforme", "Supprimer"]) {
    expect(text).not.toContain(french);
  }
});

it("hands the writing over to the surface that owns it", async () => {
  const user = userEvent.setup();
  const openGraph = vi.fn();
  stubUsage();
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} onOpenGraph={openGraph} />);
  await user.click(await screen.findByTestId("graph-links-manage"));
  expect(openGraph).toHaveBeenCalledTimes(1);
});

it("opens a peer this console answers for, naming the object it opens", async () => {
  const user = userEvent.setup();
  const openPeer = vi.fn();
  stubUsage();
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} onOpenPeer={openPeer} />);
  await user.click(await screen.findByTestId("graph-link-open-crel_2"));
  expect(openPeer).toHaveBeenCalledWith({ type: "procedure", id: "proc_9" });
});

it("tells a REFUSED facet read apart from an object with no relations", async () => {
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (String(url).startsWith("/api/context/relationships")) {
      return Promise.resolve(response(404, { code: "not_found", message: "Context object not found" }));
    }
    return Promise.resolve(response(200, TOPIC));
  }));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  const error = await screen.findByTestId("graph-links-error");
  expect(error).toHaveTextContent(/Context object not found/);
  expect(error).toHaveTextContent(/not an object without relations/);
});

// ── AC1 — denied, stale and error are three facts, not one red box
//
// The page answered `loading | error | ready`, so a caller without access, a
// store that failed and a RETIRED object rendered the same "Context object
// unavailable" with the same Retry — which repairs exactly one of the three.

it("says a denial is a denial, and offers the way back instead of a retry", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(404, { code: "not_found", message: "Topic not found" }))));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  const denied = await screen.findByTestId("context-object-denied");
  expect(denied).toHaveTextContent(/do not have access/i);
  expect(denied).toHaveTextContent(/Ask an owner of this project/);
  expect(denied).toHaveAttribute("role", "status");
  // Not the store failure, and not a Retry the server would refuse again.
  expect(screen.queryByTestId("context-object-error")).not.toBeInTheDocument();
  expect(screen.queryByText("Retry")).not.toBeInTheDocument();
});

it("says a store failure is a store failure, and interrupts", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(500, { code: "db_error", message: "Failed to retrieve topic" }))));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  const failure = await screen.findByTestId("context-object-error");
  expect(failure).toHaveTextContent(/Failed to retrieve topic/);
  expect(failure).toHaveTextContent(/has not been removed/);
  expect(failure).toHaveAttribute("role", "alert");
  expect(screen.queryByTestId("context-object-denied")).not.toBeInTheDocument();
});

it("marks a RETIRED object as no longer current, and still reads it", async () => {
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (String(url).includes("/review-requests")) return Promise.resolve(response(200, { requests: [], can_resolve: false }));
    return Promise.resolve(response(200, { ...TOPIC, status: "archived" }));
  }));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  const stale = await screen.findByTestId("context-object-stale");
  expect(stale).toHaveTextContent(/was retired/);
  expect(stale).toHaveTextContent(/Show archived/);
  expect(stale).toHaveAttribute("role", "status");
  // Retired is not unreadable: the content is still there, and so are its
  // versions — a stale banner, never a wall.
  expect(screen.getByText("Canonical revenue.")).toBeInTheDocument();
  expect(screen.queryByTestId("context-object-denied")).not.toBeInTheDocument();
  expect(screen.queryByTestId("context-object-error")).not.toBeInTheDocument();
});

it("does not mark a current object as retired", async () => {
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (String(url).includes("/review-requests")) return Promise.resolve(response(200, { requests: [], can_resolve: false }));
    return Promise.resolve(response(200, TOPIC));
  }));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("Revenue policy")).toBeInTheDocument();
  expect(screen.queryByTestId("context-object-stale")).not.toBeInTheDocument();
});

// ── The version ledger is READ, not dumped
//
// `changed_at` was printed as the raw ISO the wire carries, the pinned
// frontmatter as YAML in a `<pre>`, and one sentence — "No version history was
// returned" — served two different facts.

function stubVersions(payload: unknown, detail: unknown = TOPIC) {
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (String(url).includes("/versions?")) return Promise.resolve(response(200, payload));
    return Promise.resolve(response(200, detail));
  }));
}

it("reads a version's date as a person reads one, keeping the exact instant", async () => {
  stubVersions({ versions: [
    { version_number: 2, changed_by: "owner@example.com", changed_at: "2026-07-21T10:00:00Z",
      status: "active", title: "Revenue policy", body_md: "Canonical revenue." },
    { version_number: 1, changed_by: "owner@example.com", changed_at: "2026-07-20T10:00:00Z",
      status: "active", title: "Revenue policy", body_md: "First." },
  ] });
  const { container } = render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="versions" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  await screen.findByText("v2");
  // The raw ISO is gone from the reading...
  expect(container.textContent).not.toContain("2026-07-21T10:00:00Z");
  // ...and survives where a machine reads it.
  const times = [...container.querySelectorAll("time")].map((node) => node.getAttribute("dateTime"));
  expect(times).toContain(new Date("2026-07-21T10:00:00Z").toISOString());
});

it("says a single version is the FIRST one, not a short list", async () => {
  stubVersions({ versions: [
    { version_number: 1, changed_by: "owner@example.com", changed_at: "2026-07-20T10:00:00Z",
      status: "active", title: "Revenue policy", body_md: "First." },
  ] });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="versions" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByTestId("context-versions-first")).toHaveTextContent(/first version/i);
  expect(screen.queryByTestId("context-versions-unreadable")).not.toBeInTheDocument();
});

it("says an EMPTY ledger could not be read, because an object on screen has a v1", async () => {
  // Two different facts, and one sentence used to serve both.
  stubVersions({ versions: [] });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="versions" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  const notice = await screen.findByTestId("context-versions-unreadable");
  expect(notice).toHaveTextContent(/could not be read/);
  expect(notice).toHaveTextContent(/not an object without one/);
  expect(screen.queryByText("No version history was returned.")).not.toBeInTheDocument();
});

it("reads a pinned Skill frontmatter as named rows, not as YAML in a box", async () => {
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (String(url).includes("/versions?")) {
      return Promise.resolve(response(200, { versions: [
        { version_number: 1, changed_by: "owner@example.com", changed_at: "2026-07-20T10:00:00Z",
          status: "active", name: "publish-product-sheet",
          description: "Write a governed product sheet.",
          frontmatter_yaml: SKILL_FRONTMATTER, body_md: "The long prose rationale." },
      ] }));
    }
    if (String(url).includes("/review-requests")) {
      return Promise.resolve(response(200, { requests: [], can_resolve: false }));
    }
    return Promise.resolve(response(200, PROCEDURE));
  }));
  const { container } = render(<ContextObjectPage projectId="p1" kind="procedure" objectId="proc_1" tab="versions" versionId="1" onNavigateTab={vi.fn()} onOpenVersion={vi.fn()} onBack={vi.fn()} />);
  // The snapshot is opened by the ADDRESS, which is what a pinned reference —
  // an AI Path step, a relationship pin — actually carries.
  // The standardized sequence, through the same renderer the Content tab uses.
  expect(await screen.findByText("Sequence — 2 steps")).toBeInTheDocument();
  // The remaining keys as named rows — "Description", not `description:`.
  expect(screen.getByText("Description")).toBeInTheDocument();
  // And no YAML at a person: the stored text is nowhere on the tab.
  expect(container.textContent).not.toContain("- step: 1");
  expect(container.textContent).not.toContain("acceptance:");
});

it("keeps a frontmatter it could not name, verbatim, rather than dropping it", async () => {
  const opaque = "  not: a top level key\n";
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (String(url).includes("/versions?")) {
      return Promise.resolve(response(200, { versions: [
        { version_number: 1, changed_by: "owner@example.com", changed_at: "2026-07-20T10:00:00Z",
          status: "active", name: "publish-product-sheet", description: "",
          frontmatter_yaml: opaque, body_md: "Body." },
      ] }));
    }
    if (String(url).includes("/review-requests")) {
      return Promise.resolve(response(200, { requests: [], can_resolve: false }));
    }
    return Promise.resolve(response(200, PROCEDURE));
  }));
  const { container } = render(<ContextObjectPage projectId="p1" kind="procedure" objectId="proc_1" tab="versions" versionId="1" onNavigateTab={vi.fn()} onOpenVersion={vi.fn()} onBack={vi.fn()} />);
  await screen.findByLabelText("Version 1 snapshot");
  // Nothing could be named, so the evidence is shown as stored rather than
  // dropped: a shape we failed to read is not a shape that was not there.
  expect(container.textContent).toContain("not: a top level key");
});

it("keeps valid object detail available when version parsing fails", async () => {
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (String(url).includes("/versions?")) {
      return Promise.resolve(response(200, { versions: [{ version_number: 2 }] }));
    }
    return Promise.resolve(response(200, TOPIC));
  }));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="versions" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("Revenue policy")).toBeInTheDocument();
  expect(await screen.findByRole("alert")).toHaveTextContent(/Version changed_by/);
  expect(screen.queryByText("Context object unavailable")).not.toBeInTheDocument();
});
// ── AI-158 — ce qu'on reproche a cet objet se lit SUR l'objet
//
// Le panneau des remarques ne vivait que dans le tiroir du mindmap. Ouvrir une
// Skill sur son etabli, c'etait ne rien savoir de ce qu'on lui reproche. Ces
// tests tiennent le fait cote etabli ; `NodeRemarks` est le meme composant que
// le mindmap monte, et `KnowledgeGraphPage.edit` tient l'autre cote.

const REMARK = {
  id: "crr_1", node_id: "top_1", node_type: "topic", node_version: 2,
  note: "This constraint no longer exists.", requested_by: "reader@example.com",
  origin: "human", status: "open", created_at: "2026-08-04T09:00:00Z",
  proposed_change: null,
};

/** Repond au detail ET a la file, sans supposer l'ordre des deux appels. */
function stubWithRemarks(payload: unknown, detail: unknown = TOPIC) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    calls.push(String(url));
    if (String(url).includes("/review-requests")) return Promise.resolve(response(200, payload));
    return Promise.resolve(response(200, detail));
  }));
  return calls;
}

it("lists the open remarks of this object on its own workbench, scoped to it", async () => {
  const calls = stubWithRemarks({ requests: [REMARK], can_resolve: true });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("This constraint no longer exists.")).toBeInTheDocument();
  expect(screen.getByText("Open remarks (1)")).toBeInTheDocument();
  // Scopee au noeud : l'etabli d'un objet ne montre pas la file d'un autre.
  expect(calls).toContain("/api/context/review-requests?project_id=p1&node_id=top_1");
});

it("hides Accept and Decline when the server says this reader cannot close", async () => {
  // Le droit de clore est la REPONSE DU SERVEUR, pas une deduction de role cote
  // client. Sans droits les boutons sont ABSENTS, pas desactives et muets.
  stubWithRemarks({ requests: [REMARK], can_resolve: false });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByTestId("context-review-readonly-crr_1")).toBeInTheDocument();
  expect(screen.queryByTestId("context-review-accept-crr_1")).not.toBeInTheDocument();
  expect(screen.queryByTestId("context-review-decline-crr_1")).not.toBeInTheDocument();
});

it("marks a remark whose version this object has since moved past", async () => {
  // TOPIC est en v2 ; la remarque parle de la v1. « Cette contrainte n'existe
  // plus » ne se lit pas pareil selon la version dont elle parle.
  stubWithRemarks({ requests: [{ ...REMARK, node_version: 1 }], can_resolve: true });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByTestId("context-review-stale-crr_1")).toBeInTheDocument();
});

it("does not mark a remark that speaks of the version on screen", async () => {
  stubWithRemarks({ requests: [REMARK], can_resolve: true });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByTestId("context-review-request-crr_1")).toBeInTheDocument();
  expect(screen.queryByTestId("context-review-stale-crr_1")).not.toBeInTheDocument();
});

it("names a machine remark as such", async () => {
  // Une remarque de machine confondue avec une remarque humaine vaut moins que
  // rien : c'est la meme file, ce n'est pas le meme poids.
  stubWithRemarks({ requests: [{ ...REMARK, origin: "agent" }], can_resolve: true });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByTestId("context-review-origin-crr_1")).toBeInTheDocument();
});

it("closes a remark and RE-READS instead of guessing what the server did", async () => {
  const user = userEvent.setup();
  const calls: string[] = [];
  let listed = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const href = String(url);
    calls.push(href);
    if (href.includes("/resolve")) return Promise.resolve(response(200, { ...REMARK, status: "accepted" }));
    if (href.includes("/review-requests")) {
      listed += 1;
      // Apres la fermeture la file est vide : c'est le SERVEUR qui le dit.
      return Promise.resolve(response(200, { requests: listed === 1 ? [REMARK] : [], can_resolve: true }));
    }
    return Promise.resolve(response(200, TOPIC));
  }));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  await user.click(await screen.findByTestId("context-review-accept-crr_1"));
  expect(await screen.findByTestId("context-review-queue-empty")).toBeInTheDocument();
  expect(calls).toContain("/api/context/review-requests/crr_1/resolve?project_id=p1");
  // Une acceptation peut POSER un lien en base : la vue relit, elle ne retire
  // pas la ligne a la main.
  expect(listed).toBe(2);
});

it("does not translate the 404 of a close into « Not permitted »", async () => {
  // Le serveur rend le MEME 404 pour « pas a vous », « inconnue » et « DEJA
  // CLOSE », et ne les distingue pas expres. Choisir une cause sur trois se
  // tromperait souvent — le cas courant est une remarque close par un autre.
  const user = userEvent.setup();
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const href = String(url);
    if (href.includes("/resolve")) return Promise.resolve(response(404, { code: "not_found", message: "Review request not found" }));
    if (href.includes("/review-requests")) return Promise.resolve(response(200, { requests: [REMARK], can_resolve: true }));
    return Promise.resolve(response(200, TOPIC));
  }));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  await user.click(await screen.findByTestId("context-review-accept-crr_1"));
  const error = await screen.findByTestId("context-review-resolve-error");
  expect(error).toHaveTextContent(/may already be closed/);
  expect(error).not.toHaveTextContent(/^Not permitted$/);
});

it("keeps the object readable when its remark queue is unavailable", async () => {
  // Une file en panne ne doit pas emporter la lecture de l'objet.
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (String(url).includes("/review-requests")) return Promise.resolve(response(500, { code: "db_error", message: "Failed to retrieve review requests" }));
    return Promise.resolve(response(200, TOPIC));
  }));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("Canonical revenue.")).toBeInTheDocument();
  expect(await screen.findByTestId("context-review-queue-error")).toHaveTextContent(/Failed to retrieve review requests/);
});


// ── Lecture d'une Skill : la sequence standardisee d'abord, la prose ensuite
//
// Les etapes n'etaient visibles que dans l'aperçu de l'editeur. L'etabli d'une
// Skill doit la montrer comme elle est ecrite : steps, acceptance, puis body.

const SKILL_FRONTMATTER = [
  "name: publish-product-sheet",
  "description: Write a governed product sheet.",
  "steps:",
  "  - step: 1",
  "    action: read",
  '    label: "Read the product context"',
  "    target: search_context",
  "  - step: 2",
  "    action: analyze",
  '    label: "Draft the sheet"',
  "    target: product-sheet",
  "acceptance:",
  '  - "The sheet cites only governed context"',
].join("\n");

const PROCEDURE = {
  id: "proc_1", project_id: "p1", name: "publish-product-sheet",
  description: "Write a governed product sheet.",
  frontmatter_yaml: SKILL_FRONTMATTER,
  body_md: "The long prose rationale.",
  status: "active", owner: null, created_by: "owner@example.com",
  created_at: "2026-07-20T10:00:00Z", updated_at: "2026-07-21T10:00:00Z",
  version_number: 1, mdm_references: null,
};

function stubProcedure(detail: unknown) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    calls.push(String(url));
    if (String(url).includes("/review-requests")) {
      return Promise.resolve(response(200, { requests: [], can_resolve: false }));
    }
    return Promise.resolve(response(200, detail));
  }));
  return calls;
}

it("reads a Skill's standardized steps on its workbench, alongside the prose", async () => {
  stubProcedure(PROCEDURE);
  render(<ContextObjectPage projectId="p1" kind="procedure" objectId="proc_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("Sequence — 2 steps")).toBeInTheDocument();
  expect(screen.getByTestId("skill-step-1")).toHaveTextContent("Read the product context");
  expect(screen.getByTestId("skill-step-1")).toHaveTextContent("search_context");
  expect(screen.getByTestId("skill-step-2")).toHaveTextContent("Draft the sheet");
  expect(screen.getByText("The sheet cites only governed context")).toBeInTheDocument();
  expect(screen.getByText("The long prose rationale.")).toBeInTheDocument();
});

it("shows only the prose when a Skill has no standardized frontmatter", async () => {
  stubProcedure({
    ...PROCEDURE,
    frontmatter_yaml: "name: publish-product-sheet\ndescription: Write a governed product sheet.",
  });
  render(<ContextObjectPage projectId="p1" kind="procedure" objectId="proc_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("The long prose rationale.")).toBeInTheDocument();
  expect(screen.queryByText(/Sequence —/)).not.toBeInTheDocument();
  expect(screen.queryByTestId("skill-step-1")).not.toBeInTheDocument();
});

// ── A remark is ANSWERED here, not only closed
//
// The workbench showed a remark and offered Accept / Decline and nothing else,
// so the queue could be emptied without a word of the corpus changing — the
// defect `context-hub.md` names: "a remark can only be closed and not acted on
// … no path from the remark to the adjustment it asks for". The editor now
// lives where the remark is read, and it is the SAME editor the collection
// mounts, saving through the same PATCH with `expected_version`.

const WRITABLE = { can_write: true, version_history: true, usage: false };

/** Detail + remark queue, with the capability the DETAIL route now answers. */
function stubWritable(detail: unknown, remarks: unknown = { requests: [REMARK], can_resolve: true }) {
  const calls: Array<{ url: string; method: string; body: string | null }> = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    calls.push({
      url: String(url),
      method: String(init?.method ?? "GET"),
      body: typeof init?.body === "string" ? init.body : null,
    });
    if (String(url).includes("/review-requests")) return Promise.resolve(response(200, remarks));
    return Promise.resolve(response(200, detail));
  }));
  return calls;
}

it("offers the adjustment a remark asks for, on the workbench that shows it", async () => {
  const user = userEvent.setup();
  stubWritable({ ...TOPIC, capabilities: WRITABLE });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  await user.click(await screen.findByTestId("context-review-adjust-crr_1"));
  // The editor of the object the remark speaks of — not a navigation away.
  expect(await screen.findByTestId("knowledge-editor-body")).toBeInTheDocument();
});

it("names the object in the adjust gesture, following the node and not the screen", async () => {
  stubWritable({ ...TOPIC, capabilities: WRITABLE });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  // A Knowledge item is not "a Skill" because the button was born on the
  // Skills collection: one notion, one word.
  expect(await screen.findByTestId("context-review-adjust-crr_1")).toHaveTextContent("Adjust this Knowledge item");
});

it("offers no edit at all when the server says this caller cannot write", async () => {
  // The detail route answers `can_write` now; a button the server would refuse
  // is not an offer. Absent, never disabled-and-silent.
  stubWritable({ ...TOPIC, capabilities: { ...WRITABLE, can_write: false } });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("Revenue policy")).toBeInTheDocument();
  expect(screen.queryByTestId("context-object-edit")).not.toBeInTheDocument();
});

it("saves the adjustment as a NEW VERSION, carrying the version the reader was looking at", async () => {
  const user = userEvent.setup();
  const calls = stubWritable({ ...TOPIC, capabilities: WRITABLE });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  await user.click(await screen.findByTestId("context-object-edit"));
  await user.clear(await screen.findByTestId("knowledge-editor-body"));
  await user.type(screen.getByTestId("knowledge-editor-body"), "Corrected.");
  await user.click(screen.getByTestId("knowledge-editor-save"));

  const patch = await vi.waitFor(() => {
    const found = calls.find((call) => call.method === "PATCH");
    expect(found).toBeTruthy();
    return found!;
  });
  expect(patch.url).toBe("/api/context/topics/top_1?project_id=p1");
  // TOPIC is at v2: the save pins the version that was on screen, so a
  // workbench never overwrites a version it never read.
  expect(JSON.parse(patch.body ?? "{}")).toMatchObject({ expected_version: 2, body_md: "Corrected." });
});

it("re-reads the remark queue after a save, so a remark can mark itself older", async () => {
  const user = userEvent.setup();
  const calls = stubWritable({ ...TOPIC, capabilities: WRITABLE });
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  await user.click(await screen.findByTestId("context-object-edit"));
  const before = calls.filter((call) => call.url.includes("/review-requests")).length;
  await user.click(screen.getByTestId("knowledge-editor-save"));
  await vi.waitFor(() => {
    expect(calls.filter((call) => call.url.includes("/review-requests")).length).toBeGreaterThan(before);
  });
});

it("opens the Skill's own editor when the remark is on a Skill", async () => {
  const user = userEvent.setup();
  stubWritable(
    { ...PROCEDURE, capabilities: WRITABLE },
    { requests: [{ ...REMARK, id: "crr_2", node_id: "proc_1", node_type: "procedure", node_version: 1 }], can_resolve: true },
  );
  render(<ContextObjectPage projectId="p1" kind="procedure" objectId="proc_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByTestId("context-review-adjust-crr_2")).toHaveTextContent("Adjust this Skill");
  await user.click(screen.getByTestId("context-review-adjust-crr_2"));
  expect(await screen.findByRole("dialog", { name: /Edit publish-product-sheet/ })).toBeInTheDocument();
});

// ── WHOSE OBJECT THIS IS, ON THE OBJECT'S OWN PAGE
//
// Both collections badge a `project_id: null` row `Platform`
// (`KnowledgeBasePage.tsx`, `Procedures.tsx`); opening one lost the badge, so a
// person editing a definition every project reads could not tell it from one
// only this project reads. Story 49.6 AC3/AC4 ask the workbench for the owner
// scope and the owner explicitly.

it("names the owner scope of a platform Knowledge item, in the list's own word", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(200, {
    ...TOPIC, project_id: null, owner: "governance@example.com", capabilities: WRITABLE,
  }))));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("Platform")).toBeInTheDocument();
  expect(screen.getByText(/read by every project on this platform/)).toBeInTheDocument();
  expect(screen.getByTestId("context-object-stewardship")).toHaveTextContent("Owner: governance@example.com.");
});

it("names this project's own scope, and the gesture when no owner is named", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(200, { ...TOPIC, capabilities: WRITABLE }))));
  render(<ContextObjectPage projectId="p1" kind="topic" objectId="top_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  expect(await screen.findByText("This project")).toBeInTheDocument();
  expect(screen.getByTestId("context-object-stewardship"))
    .toHaveTextContent("No owner named — name one the next time this Knowledge item is edited.");
});

it("says why a Skill cannot be edited here, and names who repairs it", async () => {
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (String(url).includes("/review-requests")) return Promise.resolve(response(200, { requests: [], can_resolve: false }));
    return Promise.resolve(response(200, {
      ...PROCEDURE, project_id: null, capabilities: { can_write: false, version_history: true, usage: false },
    }));
  }));
  render(<ContextObjectPage projectId="p1" kind="procedure" objectId="proc_1" tab="content" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
  const stewardship = await screen.findByTestId("context-object-stewardship");
  expect(stewardship).toHaveTextContent("this account cannot change it from here");
  expect(stewardship).toHaveTextContent("Ask a platform administrator");
  // The refusal REPLACES the control; it does not sit beside one the server
  // would reject.
  expect(screen.queryByTestId("context-object-edit")).toBeNull();
});


describe("the Skill reads its own walks (2026-09-05)", () => {
  const WALKS = {
    schema_version: "ai-path-skill-walks.v1", project_id: "p1", procedure_id: "proc_1",
    walks: 3, window: "last 3 walk(s)", verdicts: { pass: 1, fail: 2 }, versions: { "proc_1@6": 3 },
    steps: [
      { step: "1", label: "Run the pinned query", tool: "execute_analyze_query_spec", crossed: 3, skipped: 0, unobservable: 0 },
      { step: "2", label: "Read the Result", tool: "analyze_result", crossed: 1, skipped: 2, unobservable: 0 },
    ],
    most_skipped: [{ step: "2", label: "Read the Result", tool: "analyze_result", crossed: 1, skipped: 2, unobservable: 0 }],
    recent: [{ path_id: "aip_a", started_at: "2026-09-05T10:00:00+00:00", verdict: "pass", skill_version: "proc_1@6", crossed: ["1", "2"], skipped: [] }],
  };

  it("draws the per-step counts and the most skipped step on the usage tab", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      if (String(url).includes("/context/ai-paths/skills/proc_1/walks")) return Promise.resolve(response(200, WALKS));
      return Promise.resolve(response(200, PROCEDURE));
    }));
    render(<ContextObjectPage projectId="p1" kind="procedure" objectId="proc_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
    const panel = await screen.findByTestId("skill-walks");
    expect(await within(panel).findByTestId("skill-walks-summary")).toHaveTextContent("last 3 walk(s): 1 pass, 2 fail.");
    expect(within(panel).getByTestId("skill-walks-most-skipped")).toHaveTextContent("step 2 (2×)");
    expect(within(panel).getByTestId("skill-walk-step-2")).toHaveTextContent("Read the Result");
    expect(within(panel).getByTestId("skill-walks-recent")).toHaveTextContent("aip_a · 2026-09-05T10:00 · pass");
  });

  it("says when the history is unreadable, and names the gesture when it is empty", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      if (String(url).includes("/walks")) return Promise.resolve(response(503, { code: "ai_paths_unavailable" }));
      return Promise.resolve(response(200, PROCEDURE));
    }));
    render(<ContextObjectPage projectId="p1" kind="procedure" objectId="proc_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
    expect(await screen.findByTestId("skill-walks-unavailable")).toBeInTheDocument();
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      if (String(url).includes("/walks")) return Promise.resolve(response(200, { ...WALKS, walks: 0, window: "no walk recorded yet", verdicts: {}, versions: {}, steps: [], most_skipped: [], recent: [] }));
      return Promise.resolve(response(200, PROCEDURE));
    }));
    render(<ContextObjectPage projectId="p1" kind="procedure" objectId="proc_1" tab="usage" onNavigateTab={vi.fn()} onBack={vi.fn()} />);
    expect(await screen.findByTestId("skill-walks-empty")).toHaveTextContent("No interaction has taken this Skill yet");
  });
});
