import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import PremierRapportRenduCard, {
  type RenderedFirstReport,
} from "../datastreams/PremierRapportRenduCard";

// Story 36.15 vitest: the read-only starter request renders a compact-text
// fallback with bounded evidence + an authenticated deep-link (never the full
// dataset); a validation checklist confirms the figures cite the active
// publication/mapping versions with a non-color state token; the degraded state is
// explicit (never "prêt"); and the second-user reproduction check reports "same
// result" when authorized and existence-hidden "introuvable" on denial WITHOUT
// leaking the first-user result. Mirrors RapportPretCard.test.tsx conventions.

const RENDER: RenderedFirstReport = {
  schema_version: "1",
  datastream_id: "ds-1",
  project_id: "proj-1",
  overall: "ready",
  host_cta: "enabled",
  ui_supported: false,
  render_mode: "compact_text",
  headline: "Premier rapport rendu : période récente publiée et contrôles au vert.",
  publication: { execution_id: "dse_pub", row_count: 120, published_at: "2026-07-22T00:00:00Z" },
  mapping: { mapping_version_id: "dmap_1", version_number: 3, mapping_contract_version: "1.2" },
  provenance: { content_hash: "c".repeat(64), source_schema_hash: "b".repeat(64) },
  freshness: { last_pull_at: "2026-07-22T00:00:00Z", connection_health: "healthy" },
  coverage: { recent: { state: "covered" }, historical: { state: "covered" } },
  exclusions: [],
  bounded_evidence: {
    row_count: 120,
    verified_row_count: 120,
    expected_row_count: 120,
    metric_names: ["clicks", "cost"],
    metric_count: 2,
    dimension_count: 1,
    content_hash: "c".repeat(64),
    truncated: false,
    note: "Preuve bornée : totaux, empreintes et quelques figures.",
  },
  report_deep_link: {
    kind: "authenticated_report",
    path: "/projects/proj-1/datastreams/ds-1/first-report/rendered",
    requires_authentication: true,
    publication_execution_id: "dse_pub",
    note: "Lien authentifié.",
  },
  validation: {
    figures_cite_publication: true,
    figures_cite_mapping_version: true,
    publication_execution_id: "dse_pub",
    mapping_version_id: "dmap_1",
    mapping_version_number: 3,
    content_hash: "c".repeat(64),
    state_token: "ready",
    is_degraded: false,
    degraded_note: null,
  },
  readiness_version: "v".repeat(32),
};

const DEGRADED: RenderedFirstReport = {
  ...RENDER,
  overall: "degraded",
  host_cta: "degraded",
  headline:
    "Premier rapport rendu (dégradé mais utilisable) : le résultat récent est publié ; l'historique reste partiel.",
  coverage: { recent: { state: "covered" }, historical: { state: "failed" } },
  validation: {
    ...RENDER.validation,
    state_token: "degraded",
    is_degraded: true,
    degraded_note:
      "Rapport degrade mais utilisable : le resultat recent est publie ; l'historique ou la qualite des donnees reste partiel.",
  },
};

function renderCard() {
  return render(
    <ThemeProvider theme={adminTheme}>
      <PremierRapportRenduCard projectId="proj-1" datastreamId="ds-1" apiToken="token" />
    </ThemeProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

it("renders compact-text bounded evidence + an authenticated deep-link (no full dataset)", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 200, json: async () => RENDER } as Response);
  renderCard();
  expect(await screen.findByRole("heading", { name: "Premier rapport rendu" })).toBeInTheDocument();
  expect(screen.getByTestId("render-mode")).toHaveTextContent(/texte compact/i);
  const evidence = screen.getByTestId("render-bounded-evidence");
  expect(evidence).toHaveTextContent(/Lignes publiées : 120/);
  expect(evidence).toHaveTextContent(/clicks, cost/);
  // Authenticated deep-link present; the full dataset is behind it, never inline.
  const link = screen.getByTestId("render-deeplink");
  expect(link).toHaveAttribute("href", "/projects/proj-1/datastreams/ds-1/first-report/rendered");
});

it("shows a validation checklist confirming figures cite the publication + mapping versions", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 200, json: async () => RENDER } as Response);
  renderCard();
  const list = await screen.findByTestId("render-validation");
  expect(list).toHaveTextContent(/citent la publication active \(dse_pub\)/);
  expect(list).toHaveTextContent(/citent la version de mapping \(v3\)/);
  // Non-color: each check carries an explicit textual token, not just a hue.
  expect(screen.getAllByTestId("check-ok").length).toBeGreaterThan(0);
});

it("shows the degraded state explicitly and never says 'prêt'", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 200, json: async () => DEGRADED } as Response);
  renderCard();
  expect(await screen.findByTestId("render-degraded")).toHaveTextContent(/dégradé/i);
  const headline = screen.getByTestId("render-headline");
  expect(headline.textContent?.toLowerCase()).not.toContain("contrôles au vert");
});

it("reports a SUCCESSFUL second-user reproduction (same publication)", async () => {
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => RENDER } as Response)
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ ...RENDER, reproduced: true }) } as Response);
  renderCard();
  const button = await screen.findByTestId("render-reproduce");
  fireEvent.click(button);
  await waitFor(() => expect(screen.getByTestId("repro-ok")).toBeInTheDocument());
  expect(screen.getByTestId("repro-ok")).toHaveTextContent(/dse_pub/);
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it("existence-hides an unauthorized second user (introuvable, no first-user leak)", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => RENDER } as Response)
    .mockResolvedValueOnce({ ok: false, status: 404 } as Response);
  renderCard();
  const button = await screen.findByTestId("render-reproduce");
  fireEvent.click(button);
  await waitFor(() => expect(screen.getByTestId("repro-denied")).toBeInTheDocument());
  const denied = screen.getByTestId("repro-denied");
  expect(denied).toHaveTextContent(/introuvable/i);
  // No first-user result leaked into the denial message.
  expect(denied).not.toHaveTextContent(/dse_pub/);
});

it("shows the not-renderable state when readiness is not yet ready (409)", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: false, status: 409 } as Response);
  renderCard();
  await waitFor(() => expect(screen.getByTestId("render-not-renderable")).toBeInTheDocument());
});

it("shows a not-found message when access is denied on render (existence-hidden)", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: false, status: 404 } as Response);
  renderCard();
  await waitFor(() =>
    expect(screen.getByText(/introuvable ou vous n’y avez pas accès/)).toBeInTheDocument(),
  );
});
