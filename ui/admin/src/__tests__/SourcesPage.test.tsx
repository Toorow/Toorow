import { fireEvent, render, screen } from "@testing-library/react";
import Sources from "../shell/pages/Sources";

function envelope(items: unknown[]) {
  return {
    schema_version: "data-sources.v1",
    project_ref: { object_type: "project", id: "p1" },
    generated_at: "2026-07-29T10:00:00Z",
    evidence_as_of: "2026-07-29T09:00:00Z",
    items,
    unavailable_reasons: [],
    allowed_actions: [],
  };
}

const ACCOUNT = {
  object_ref: { object_type: "source-account", id: "sacct_1" },
  connector_ref: { object_type: "connector", id: "generic" },
  label: "Real Source Account",
  states: { availability: "available", freshness: "observed", usage: "used" },
  evidence: { used_by_count: 2, last_seen_at: "2026-07-29T09:00:00Z" },
  evidence_as_of: "2026-07-29T09:00:00Z",
  links: {},
};

function response(body: unknown, ok = true, status = ok ? 200 : 503): Response {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("renders the Source Account inventory with Connect Source button and without credential management controls", async () => {
  const fetchMock = vi.fn((url: string) => {
    if (url.includes("/api/connectors/available")) return Promise.resolve(response([]));
    return Promise.resolve(response(envelope([ACCOUNT])));
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<Sources projectId="p1" />);
  expect(await screen.findByText("Real Source Account")).toBeInTheDocument();
  expect(screen.getByText("sacct_1")).toBeInTheDocument();
  // DEUX entrées, et elles sont nommées séparément. `/Connect/i` en attrapait
  // une seule quand il n'y en avait qu'une, et rougissait le jour où la seconde
  // est arrivée — sans jamais dire laquelle manquait. Google a son propre bouton
  // parce que la pile Google passe par un consentement OAuth direct et non par
  // Nango ; les deux doivent être là, et un test qui n'en épingle aucune par son
  // nom laisserait disparaître l'une des deux en silence.
  expect(screen.getByRole("button", { name: "Connect Google" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Connect another source" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Manage|Reconnect|Revoke/i })).not.toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/projects/p1/source-accounts", expect.objectContaining({ cache: "no-store" }));
});

it("never renders raw provider, credential or Nango identifiers", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([{ ...ACCOUNT, external_account_id: "raw-account", credential_id: "conn-secret", nango_connection_id: "nango-raw" }])))));
  render(<Sources projectId="p1" />);
  expect(await screen.findByText("Real Source Account")).toBeInTheDocument();
  expect(screen.queryByText("raw-account")).not.toBeInTheDocument();
  expect(screen.queryByText("conn-secret")).not.toBeInTheDocument();
  expect(screen.queryByText("nango-raw")).not.toBeInTheDocument();
});

it("opens the Source Account workbench through its supplied owner callback", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([ACCOUNT])))));
  const open = vi.fn();
  render(<Sources projectId="p1" onOpenSourceAccount={open} />);
  fireEvent.click(await screen.findByText("Real Source Account"));
  expect(open).toHaveBeenCalledWith("sacct_1");
});

it("states denied or unavailable reads without substituting accounts", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response({ code: "not_found" }, false, 404))));
  render(<Sources projectId="private-project" />);
  expect(await screen.findByRole("alert")).toHaveTextContent(/Sources unavailable/i);
  expect(screen.queryByText("Real Source Account")).not.toBeInTheDocument();
});