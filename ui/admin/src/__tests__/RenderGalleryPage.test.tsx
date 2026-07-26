import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import RenderGalleryPage from "../RenderGalleryPage";

const AT = "2026-07-21T14:00:00Z";
const response = (status: number, body?: unknown) => ({ ok: status >= 200 && status < 300, status, json: async () => body }) as Response;
const row = (project_id = "project-a", id = "rsn_1") => ({
  id, project_id, tool_name: "get_report", tool_args: {}, widget_uri: null,
  summary_snippet: "Persisted summary", question: "Real persisted question",
  identity: "analyst@example.com", trace_id: "trace-1", created_at: AT,
  meta: { freshness: { last_pull: "2026-07-21T13:00:00Z", cadence_hours: 24, stale_since: null }, provenance: { source_system: "ga4", source_field: "fact_daily_kpi", pull_id: "pull-42" }, alerts: [] },
});
const full = () => ({ ...row(), envelope: { schema_version: "1", meta: row().meta, data: { rows: [] } } });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }

beforeEach(() => localStorage.setItem("api_token", "render-token"));
afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); });

it("uses authenticated canonical scoped routes and renders typed detail evidence", async () => {
  const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => String(url).includes("/shares?") ? response(200, { shares: [] }) : String(url).includes("/rsn_1?") ? response(200, full()) : response(200, { snapshots: [row()] }));
  vi.stubGlobal("fetch", fetchMock);
  render(<RenderGalleryPage projectId="project-a" />);
  fireEvent.click(await screen.findByRole("button", { name: /Open render: Real persisted question/i }));
  expect(await screen.findByText("Envelope version")).toBeInTheDocument();
  expect(screen.getByText(/ga4.*fact_daily_kpi.*pull-42/i)).toBeInTheDocument();
  expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual(expect.arrayContaining([
    "/api/rendus/snapshots?project_id=project-a",
    "/api/rendus/snapshots/rsn_1?project_id=project-a",
    "/api/rendus/snapshots/rsn_1/shares?project_id=project-a",
  ]));
  expect(fetchMock.mock.calls[0][1]?.headers).toMatchObject({ Authorization: "Bearer render-token" });
});

it.each([
  [200, { snapshots: [] }, "No renders yet"],
  [403, { code: "forbidden", message: "Access denied" }, "Renders unavailable"],
  [500, { code: "db_error", message: "Database unavailable" }, "Could not load renders"],
  [200, { snapshots: [{ ...row(), meta: { freshness: "live", provenance: null, alerts: [] } }] }, "Could not load renders"],
])("keeps empty, denied, and error states distinct", async (status, body, title) => {
  vi.stubGlobal("fetch", vi.fn(async () => response(status, body)));
  render(<RenderGalleryPage projectId="project-a" />);
  expect(await screen.findByText(title)).toBeInTheDocument();
});

it("rejects cross-project and malformed payloads and ignores stale scope responses", async () => {
  const first = deferred<Response>(); const second = deferred<Response>();
  const fetchMock = vi.fn((url: string) => String(url).includes("project-a") ? first.promise : second.promise);
  vi.stubGlobal("fetch", fetchMock);
  const { rerender } = render(<RenderGalleryPage projectId="project-a" />);
  rerender(<RenderGalleryPage projectId="project-b" />);
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  second.resolve(response(200, { snapshots: [row("project-b", "rsn_b")] }));
  expect(await screen.findByText("Real persisted question")).toBeInTheDocument();
  first.resolve(response(200, { snapshots: [{ ...row("project-a"), question: "Stale A" }] }));
  await Promise.resolve();
  expect(screen.queryByText("Stale A")).not.toBeInTheDocument();

  rerender(<RenderGalleryPage projectId="project-c" />);
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  expect(await screen.findByText("Could not load renders")).toBeInTheDocument();
});

it("creates and revokes shares only after authoritative server refreshes", async () => {
  const createRefresh = deferred<Response>(); const revokeRefresh = deferred<Response>(); let reads = 0;
  const oldShare = { id: "rss_old", snapshot_id: "rsn_1", share_token: "old", share_url: "https://example.test/old", shared_at: AT, shared_by: "analyst", revoked_at: null };
  const newShare = { ...oldShare, id: "rss_new", share_token: "new", share_url: "https://example.test/new" };
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const path = String(url);
    if (path.includes("/shares?") && init?.method === "GET") { reads += 1; if (reads === 1) return Promise.resolve(response(200, { shares: [oldShare] })); return reads === 2 ? createRefresh.promise : revokeRefresh.promise; }
    if (path.endsWith("/share?project_id=project-a") && init?.method === "POST") return Promise.resolve(response(201, { share_id: "rss_new", share_token: "new", share_url: newShare.share_url }));
    if (path.includes("/shares/rss_old?") && init?.method === "DELETE") return Promise.resolve(response(204));
    if (path.includes("/rsn_1?")) return Promise.resolve(response(200, full()));
    return Promise.resolve(response(200, { snapshots: [row()] }));
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<RenderGalleryPage projectId="project-a" />);
  fireEvent.click(await screen.findByRole("button", { name: /Open render/i }));
  expect(await screen.findByText(oldShare.share_url)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Create share link" }));
  await waitFor(() => expect(reads).toBe(2));
  expect(screen.queryByText(newShare.share_url)).not.toBeInTheDocument();
  createRefresh.resolve(response(200, { shares: [oldShare, newShare] }));
  expect(await screen.findByText(newShare.share_url)).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: "Revoke" })[0]);
  await waitFor(() => expect(reads).toBe(3));
  revokeRefresh.resolve(response(200, { shares: [{ ...oldShare, revoked_at: "2026-07-21T15:00:00Z" }, newShare] }));
  expect(await screen.findByText("Revoked")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/rendus/shares/rss_old?project_id=project-a", expect.objectContaining({ method: "DELETE" }));
});
