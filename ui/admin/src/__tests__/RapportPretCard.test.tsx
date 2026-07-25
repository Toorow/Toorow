import { render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import RapportPretCard, { type FirstReportReadiness } from "../datastreams/RapportPretCard";

// Story 36.10 vitest: the host CTA state comes STRAIGHT from the server
// (host_cta) -- disabled when blocked, degraded when degraded; the degraded
// banner is shown honestly (never "prêt"); and an aria-live region is present for
// accessible state announcements. Mirrors PremierRapportCard.test.tsx conventions.

const READY: FirstReportReadiness = {
  schema_version: "1",
  readiness_version: "a".repeat(32),
  datastream_id: "ds-1",
  project_id: "proj-1",
  overall: "ready",
  host_cta: "enabled",
  headline: "Rapport prêt : période récente publiée et contrôles au vert.",
  current_publication: { execution_id: "dse_pub", row_count: 120, published_at: "2026-07-22T00:00:00Z" },
  selected_objects: { report_id: "report_x", metrics: ["clicks"], dimensions: ["date"], grain: ["day"], timezone: "UTC", currency: "EUR" },
  recent_coverage: { state: "covered" },
  historical_coverage: { state: "covered" },
  freshness: { last_pull_at: "2026-07-22T00:00:00Z", connection_health: "healthy" },
  verification: { verdict: "ok", row_count: 120 },
  dq: { total_unresolved: 0, monitors_unavailable: false, degraded: false },
  mapping: { mapping_version_id: "dmap_1", version_number: 3, mapping_contract_version: "1.2" },
  provenance: { content_hash: "c".repeat(64), source_schema_hash: "b".repeat(64) },
  exclusions: [],
  last_successful_pull: { execution_id: "dse_pub", row_count: 120, at: "2026-07-22T00:00:00Z" },
  phases: [
    { phase: "selection", state: "succeeded", interval: null, rows: null, attempts: 0, live_publication_execution_id: "dse_pub", next_action: null },
    { phase: "recent_pull", state: "succeeded", interval: { start: "2026-06-22T00:00:00Z", end_exclusive: "2026-07-22T00:00:00Z" }, rows: 120, attempts: 2, live_publication_execution_id: "dse_pub", next_action: null },
    { phase: "publication", state: "succeeded", interval: null, rows: 120, attempts: 0, live_publication_execution_id: "dse_pub", next_action: null },
  ],
};

const DEGRADED: FirstReportReadiness = {
  ...READY,
  readiness_version: "d".repeat(32),
  overall: "degraded",
  host_cta: "degraded",
  headline: "Rapport dégradé mais utilisable : le résultat récent est publié ; l'historique ou la qualité des données reste partiel.",
  historical_coverage: { state: "failed" },
  dq: { total_unresolved: 2, monitors_unavailable: false, degraded: true },
  phases: [
    ...READY.phases,
    { phase: "history", state: "degraded", interval: null, rows: null, attempts: 0, live_publication_execution_id: "dse_pub", next_action: { label: "L'historique est partiel : le résultat récent reste disponible.", owner: "systeme" } },
  ],
};

const BLOCKED: FirstReportReadiness = {
  ...READY,
  readiness_version: "e".repeat(32),
  overall: "blocked",
  host_cta: "disabled",
  headline: "Rapport indisponible : aucun résultat récent publié pour l'instant.",
  current_publication: null,
  recent_coverage: { state: "pending" },
  phases: [
    { phase: "selection", state: "succeeded", interval: null, rows: null, attempts: 0, live_publication_execution_id: null, next_action: null },
    { phase: "recent_pull", state: "waiting", interval: null, rows: null, attempts: 0, live_publication_execution_id: null, next_action: { label: "Lancer le premier pull récent.", owner: "operateur" } },
  ],
};

function renderCard() {
  return render(
    <ThemeProvider theme={adminTheme}>
      <RapportPretCard projectId="proj-1" datastreamId="ds-1" apiToken="token" />
    </ThemeProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

it("renders named phases with interval, rows, attempts and the live publication (UX-DR28)", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => READY } as Response);
  renderCard();
  expect(await screen.findByRole("heading", { name: "État du premier rapport" })).toBeInTheDocument();
  expect(screen.getByTestId("readiness-phases")).toBeInTheDocument();
  expect(screen.getByText("Pull récent")).toBeInTheDocument();
  expect(screen.getByText(/Tentatives : 2/)).toBeInTheDocument();
  expect(screen.getAllByText(/publication en cours : dse_pub/).length).toBeGreaterThan(0);
});

it("enables the host CTA from the SERVER result when overall is ready", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => READY } as Response);
  renderCard();
  const cta = await screen.findByTestId("readiness-connect-host");
  expect(cta).not.toBeDisabled();
  expect(cta).toHaveTextContent("Connecter un hôte MCP");
});

it("shows an honest degraded banner and a degraded (not enabled, not disabled) CTA — never 'prêt'", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => DEGRADED } as Response);
  renderCard();
  expect(await screen.findByTestId("readiness-degraded-banner")).toBeInTheDocument();
  const banner = screen.getByTestId("readiness-degraded-banner");
  expect(banner).toHaveTextContent(/dégradé/i);
  expect(banner).not.toHaveTextContent(/entièrement prêt/);
  const headline = screen.getByTestId("readiness-headline");
  expect(headline.textContent?.toLowerCase()).not.toContain("rapport prêt"); // never "fully ready"
  // CTA state is server-derived: degraded -> enabled-but-flagged, not disabled.
  const cta = screen.getByTestId("readiness-connect-host");
  expect(cta).not.toBeDisabled();
  expect(cta).toHaveTextContent(/rapport dégradé/i);
});

it("DISABLES the host CTA when the server says blocked (never UI-inferred)", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => BLOCKED } as Response);
  renderCard();
  const cta = await screen.findByTestId("readiness-connect-host");
  expect(cta).toBeDisabled();
  expect(screen.getByTestId("readiness-blocked-banner")).toBeInTheDocument();
});

it("renders an accessible polite live region for state announcements", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => READY } as Response);
  renderCard();
  await screen.findByRole("heading", { name: "État du premier rapport" });
  const live = screen.getByTestId("readiness-live");
  expect(live).toHaveAttribute("aria-live", "polite");
  expect(live).toHaveAttribute("aria-atomic", "true");
});

it("shows a not-found message when the server denies access (existence-hidden)", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: false, status: 404 } as Response);
  renderCard();
  await waitFor(() =>
    expect(screen.getByText(/introuvable ou vous n’y avez pas accès/)).toBeInTheDocument(),
  );
});
