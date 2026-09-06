/**
 * The "Used by / Related" facet, read on its own.
 *
 * `ContextObjectPage.test.tsx` holds the tab-level facts — the door that is
 * called, the door that is NOT, the three answers. What is here is what only
 * the panel can be asked: an endpoint type this console has no word for, a
 * facet the server cut, and the peer control that must not exist when nobody
 * wired a handler.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import GraphLinks from "../connaissances/GraphLinks";

function response(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function item(overrides: Record<string, unknown> = {}) {
  return {
    relationship_id: "crel_1",
    relationship_kind: "relates_to",
    status: "active",
    provenance: "console",
    created_by: "owner@example.com",
    created_at: "2026-08-28T10:00:00Z",
    direction: "outgoing",
    other: { type: "datastream", id: "ds_EXAMPLE" },
    owner: { surface: "datastreams", href: "/api/projects/p1/datastreams/ds_EXAMPLE" },
    ...overrides,
  };
}

function stub(body: unknown, status = 200) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    calls.push(String(url));
    return Promise.resolve(response(status, body));
  }));
  return calls;
}

afterEach(() => vi.restoreAllMocks());

it("names a Datastream peer and where it is held", async () => {
  stub({ state: "ready", node: { type: "procedure", id: "proc_1" }, outgoing: [item()], incoming: [], limit: 100, truncated: false });
  render(<GraphLinks nodeId="proc_1" nodeType="procedure" projectId="p1" />);
  const row = await screen.findByTestId("graph-link-row-crel_1");
  expect(row).toHaveTextContent("Datastream");
  expect(row).toHaveTextContent("ds_EXAMPLE");
  expect(row).toHaveTextContent("Held on Datastreams.");
});

it("shows an endpoint type it has no word for, under its own token", async () => {
  // Migration 317 carried every incumbent edge "with the kind verbatim even
  // outside today's vocabulary". A row this console cannot label is a real
  // relation; hiding it would lose exactly what the carry preserved.
  stub({
    state: "ready",
    node: { type: "topic", id: "top_1" },
    outgoing: [item({ other: { type: "future_thing", id: "ft_1" }, owner: { surface: "elsewhere", href: "/x" } })],
    incoming: [],
    limit: 100,
    truncated: false,
  });
  render(<GraphLinks nodeId="top_1" nodeType="topic" projectId="p1" />);
  expect(await screen.findByTestId("graph-link-row-crel_1")).toHaveTextContent("future_thing");
});

it("says the facet was CUT rather than letting it read as complete", async () => {
  stub({ state: "ready", node: { type: "topic", id: "top_1" }, outgoing: [item()], incoming: [], limit: 100, truncated: true });
  render(<GraphLinks nodeId="top_1" nodeType="topic" projectId="p1" />);
  expect(await screen.findByTestId("graph-links-truncated")).toHaveTextContent(/first 100 relations are shown/);
});

it("offers no peer control when no handler was wired", async () => {
  // A shell that wired nothing must not turn into a button that does nothing.
  stub({
    state: "ready",
    node: { type: "topic", id: "top_1" },
    outgoing: [item({ other: { type: "topic", id: "top_2" }, owner: { surface: "knowledge", href: "/api/context/topics/top_2" } })],
    incoming: [],
    limit: 100,
    truncated: false,
  });
  render(<GraphLinks nodeId="top_1" nodeType="topic" projectId="p1" />);
  await screen.findByTestId("graph-link-row-crel_1");
  expect(screen.queryByTestId("graph-link-open-crel_1")).not.toBeInTheDocument();
  expect(screen.getByTestId("graph-links-manage-note")).toBeInTheDocument();
});

it("opens a Knowledge peer when one was", async () => {
  const user = userEvent.setup();
  const openPeer = vi.fn();
  stub({
    state: "ready",
    node: { type: "topic", id: "top_1" },
    outgoing: [item({ other: { type: "topic", id: "top_2" }, owner: { surface: "knowledge", href: "/api/context/topics/top_2" } })],
    incoming: [],
    limit: 100,
    truncated: false,
  });
  render(<GraphLinks nodeId="top_1" nodeType="topic" projectId="p1" onOpenPeer={openPeer} />);
  await user.click(await screen.findByTestId("graph-link-open-crel_1"));
  expect(openPeer).toHaveBeenCalledWith({ type: "topic", id: "top_2" });
});

it("reads the facet again when asked, after an unavailable answer", async () => {
  const user = userEvent.setup();
  let answered = 0;
  vi.stubGlobal("fetch", vi.fn(() => {
    answered += 1;
    return Promise.resolve(response(200, answered === 1
      ? { state: "unavailable", node: { type: "topic", id: "top_1" }, reason: "related_items_unreadable", message: "Open this panel again in a moment: the related items could not be read." }
      : { state: "ready", node: { type: "topic", id: "top_1" }, outgoing: [item()], incoming: [], limit: 100, truncated: false }));
  }));
  render(<GraphLinks nodeId="top_1" nodeType="topic" projectId="p1" />);
  await user.click(await screen.findByText("Read again"));
  expect(await screen.findByTestId("graph-link-row-crel_1")).toBeInTheDocument();
});

it("asks for the node by type and id, and for nothing else", async () => {
  const calls = stub({ state: "ready", node: { type: "procedure", id: "proc_1" }, outgoing: [], incoming: [], limit: 100, truncated: false });
  render(<GraphLinks nodeId="proc_1" nodeType="procedure" projectId="p1" />);
  await screen.findByText("Not linked to anything yet");
  expect(calls).toEqual(["/api/context/relationships?project_id=p1&node_type=procedure&node_id=proc_1"]);
});
