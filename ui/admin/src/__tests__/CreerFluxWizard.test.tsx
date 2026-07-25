/**
 * Vitest tests for the Story 12.13 "Créer un flux" wizard.
 *
 * Coverage (mirrors the PremierRapportCard.test.tsx pattern):
 *   - stage navigation (revisitable stepper);
 *   - Save draft is always available and posts the versioned intent;
 *   - blocking validation disables activation (AC1);
 *   - source-type switching (connector <-> managed feed) reshapes Configure;
 *   - accessibility: the error + live regions carry the right roles;
 *   - drift invalidates the preview and re-blocks activation (AC4).
 *
 * French, WCAG-oriented assertions. fetch is mocked per-test; no real network.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import { CreerFluxWizard } from "../datastreams/wizard";

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------
const CONNECTIONS = {
  connections: [
    {
      id: "conn-1",
      connection_ref_id: "cref-1",
      provider: "meta_ads",
      display_name: "Meta Ads — Compte 1",
      status: "connected",
    },
  ],
};

const CAPABILITIES = {
  contract_version: "1",
  project_id: "proj-1",
  connection_ref_id: "cref-1",
  module: { name: "meta_ads", display_name: "Meta Ads", module_kind: "kpi" },
  fields: [
    {
      field_id: "spend",
      source_field: "spend",
      kind: "metric",
      physical_type: "number",
      description: "Dépense média",
      semantic_hints: ["cost"],
      canonical_target: "media_spend",
      aggregation: "sum",
      non_additive: false,
    },
    {
      field_id: "date",
      source_field: "date",
      kind: "dimension",
      physical_type: "date",
      description: "Jour de la donnée",
      semantic_hints: ["date"],
      canonical_target: "date",
      aggregation: null,
      non_additive: false,
    },
  ],
  reports: [
    {
      id: "campaign_daily",
      selection_mode: "explicit",
      availability: { status: "selectable" },
      metrics: ["spend"],
      dimensions: ["date"],
      supported_grains: [["date"]],
      compatibility: [],
      quota_cost: { read_points: 150, unit: "read_points" },
      cadence: { minimum_interval_minutes: 1440, supported_modes: ["daily"] },
    },
  ],
};

function renderWizard(overrides: Partial<Parameters<typeof CreerFluxWizard>[0]> = {}) {
  return render(
    <ThemeProvider theme={adminTheme}>
      <CreerFluxWizard projectId="proj-1" apiToken="token" {...overrides} />
    </ThemeProvider>,
  );
}

/**
 * A fetch mock that routes by URL to the right fixture. The create handler is
 * STRICT about the intent shape: it mirrors the real server contract by 422-ing
 * when `source.kind` is missing (the previous lax `{id}`-for-any-body mock is
 * exactly what let C1 through). A wrong-shaped intent now surfaces as an error,
 * so the shell tests exercise the real contract.
 */
function routedFetch(handlers: Record<string, unknown>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(
    (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/connections")) {
        return Promise.resolve({ ok: true, json: async () => CONNECTIONS } as Response);
      }
      if (url.includes("/api/source-capabilities")) {
        return Promise.resolve({ ok: true, json: async () => CAPABILITIES } as Response);
      }
      if (url.endsWith("/api/datastreams") || url.includes("/api/datastreams?")) {
        // Enforce the real intent contract: kind lives INSIDE source.
        try {
          const body = JSON.parse((init?.body as string) ?? "{}");
          const source = body?.intent?.source;
          if (!source || typeof source.kind !== "string") {
            return Promise.resolve({
              ok: false,
              status: 422,
              json: async () => ({
                code: "invalid_intent_schema",
                message: "source.kind manquant",
              }),
            } as Response);
          }
        } catch {
          /* fall through to success */
        }
        return Promise.resolve({
          ok: true,
          json: async () => handlers.create ?? { id: "ds-new-1" },
        } as Response);
      }
      if (url.includes("/managed-feed/configure")) {
        return Promise.resolve({
          ok: true,
          json: async () => handlers.configure ?? { datastream_id: "ds-new-1" },
        } as Response);
      }
      return Promise.resolve({ ok: false, status: 404, json: async () => ({}) } as Response);
    },
  );
}

afterEach(() => vi.restoreAllMocks());

// ---------------------------------------------------------------------------

it("opens on Source with the six-stage revisitable stepper", async () => {
  routedFetch({});
  renderWizard();
  expect(
    await screen.findByRole("heading", { name: /Créer un flux — Source/ }),
  ).toBeInTheDocument();
  // The six revisitable stages, asserted via the stepper's data-testids (a MUI
  // StepButton's computed accessible name is unreliable across versions).
  for (const key of ["source", "configure", "destination", "classify", "preview", "schedule"]) {
    expect(screen.getByTestId(`stepper-${key}`)).toBeInTheDocument();
  }
});

it("exposes assertive error and polite live regions (WCAG)", async () => {
  routedFetch({});
  renderWizard();
  await screen.findByRole("heading", { name: /Créer un flux/ });
  const live = screen.getByTestId("wizard-live");
  expect(live).toHaveAttribute("role", "status");
  expect(live).toHaveAttribute("aria-live", "polite");
  const err = screen.getByTestId("wizard-error");
  expect(err).toHaveAttribute("role", "alert");
  expect(err).toHaveAttribute("aria-live", "assertive");
});

it("switches source type and reshapes the Configure stage", async () => {
  routedFetch({});
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Créer un flux/ });

  // Name + pick the connector source, then go to Configure.
  await user.type(screen.getByLabelText(/Nom du flux/), "Mon flux");
  await user.click(screen.getByTestId("source-kind-connector_pull"));
  await user.click(screen.getByTestId("wizard-next"));
  expect(await screen.findByTestId("connector-config")).toBeInTheDocument();

  // Back to Source, switch to managed feed CSV -> Configure now shows file config.
  await user.click(screen.getByTestId("stepper-source"));
  await user.click(screen.getByTestId("source-kind-managed_feed"));
  const format = screen.getByTestId("managed-format");
  await user.selectOptions(within(format).getByRole("combobox"), "csv");
  await user.click(screen.getByTestId("stepper-configure"));
  expect(await screen.findByTestId("file-config")).toBeInTheDocument();
});

it("Save draft is always available and posts the versioned intent with an idempotency key", async () => {
  const fetchMock = routedFetch({ create: { id: "ds-new-1" } });
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Créer un flux/ });

  await user.type(screen.getByLabelText(/Nom du flux/), "Flux BQ");
  await user.click(screen.getByTestId("source-kind-external_bq"));
  await user.click(screen.getByTestId("wizard-save-draft"));

  await waitFor(() =>
    expect(screen.getByTestId("wizard-live")).toHaveTextContent("Brouillon enregistré"),
  );
  const call = fetchMock.mock.calls.find((c) => String(c[0]).endsWith("/api/datastreams"));
  expect(call).toBeTruthy();
  const init = call![1] as RequestInit;
  expect(init.headers).toMatchObject({ "Idempotency-Key": expect.any(String) });
  const body = JSON.parse(init.body as string);
  // C1 — the intent the wizard POSTs now matches the server contract: kind is
  // INSIDE source (not a top-level source_kind), with the external_read_only
  // destination policy and the schema-required contract_version.
  expect(body).toMatchObject({
    project_id: "proj-1",
    name: "Flux BQ",
    intent: {
      contract_version: "1",
      source: { kind: "external_bq", writer_kind: "external" },
      destination: { policy: "external_read_only" },
    },
  });
  expect(body.intent.source_kind).toBeUndefined();
});

it("blocking validation disables activation until issues clear", async () => {
  routedFetch({});
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Créer un flux/ });

  // Connector source with NO metrics selected -> validation is blocking.
  await user.type(screen.getByLabelText(/Nom du flux/), "Flux vide");
  await user.click(screen.getByTestId("source-kind-connector_pull"));

  // Jump straight to Preview and validate (revisitable stepper).
  await user.click(screen.getByTestId("stepper-preview"));
  await user.click(screen.getByTestId("preview-validate"));
  expect(await screen.findByTestId("preview-blocking")).toBeInTheDocument();

  // Schedule stage: activation is blocked + button disabled.
  await user.click(screen.getByTestId("stepper-schedule"));
  expect(screen.getByTestId("activation-blocked")).toBeInTheDocument();
  expect(screen.getByTestId("activate-datastream")).toBeDisabled();
});

it("saves a ready-to-activate flow when the plan validates cleanly, without fabricating activation (C2)", async () => {
  const fetchMock = routedFetch({ create: { id: "ds-new-1" } });
  const onActivated = vi.fn();
  const user = userEvent.setup();
  renderWizard({ onActivated });
  await screen.findByRole("heading", { name: /Créer un flux/ });

  await user.type(screen.getByLabelText(/Nom du flux/), "Flux OK");
  await user.click(screen.getByTestId("source-kind-connector_pull"));
  await user.click(screen.getByTestId("wizard-next"));

  // Choose the connection -> capabilities load -> report + a metric.
  const connSelect = within(screen.getByTestId("connector-connection")).getByRole("combobox");
  await user.click(connSelect);
  await user.click(await screen.findByRole("option", { name: /Meta Ads — Compte 1/ }));

  const reportSelect = within(await screen.findByTestId("connector-report")).getByRole("combobox");
  await user.click(reportSelect);
  await user.click(await screen.findByRole("option", { name: /campaign_daily/ }));

  await user.click(await screen.findByLabelText("Métrique spend"));

  // Validate -> no blocking issues -> final action enabled.
  await user.click(screen.getByTestId("stepper-preview"));
  await user.click(screen.getByTestId("preview-validate"));
  await waitFor(() => expect(screen.queryByTestId("preview-blocking")).not.toBeInTheDocument());

  await user.click(screen.getByTestId("stepper-schedule"));
  // The honest Phase-B affordance is present and the button no longer says "Activer".
  expect(screen.getByTestId("activation-phaseb")).toBeInTheDocument();
  const activate = screen.getByTestId("activate-datastream");
  expect(activate).toHaveTextContent(/prêt à activer/);
  expect(activate).toBeEnabled();
  await user.click(activate);
  await waitFor(() => expect(onActivated).toHaveBeenCalledWith("ds-new-1"));

  // C2 honesty: the announcement does NOT claim the server activated the flow;
  // it states the flow is ready to activate and activation is Phase B.
  const live = screen.getByTestId("wizard-live");
  expect(live).toHaveTextContent(/prêt à activer/);
  expect(live).not.toHaveTextContent(/^Flux activé\.?$/);

  // The final action actually POSTs a versioned intent (not a no-op / fabrication).
  const createCall = fetchMock.mock.calls.find(
    (c) => String(c[0]).endsWith("/api/datastreams") && (c[1] as RequestInit)?.method === "POST",
  );
  expect(createCall).toBeTruthy();
  const intent = JSON.parse((createCall![1] as RequestInit).body as string).intent;
  expect(intent.source.kind).toBe("connector_pull");
});

it("gates Google Sheets recurring sync when no Google connection exists (H2)", async () => {
  // The default CONNECTIONS fixture has only a meta_ads connection (not Google).
  routedFetch({});
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Créer un flux/ });

  await user.type(screen.getByLabelText(/Nom du flux/), "Flux Sheets");
  await user.click(screen.getByTestId("source-kind-managed_feed"));
  const format = screen.getByTestId("managed-format");
  await user.selectOptions(within(format).getByRole("combobox"), "google_sheets");

  await user.click(screen.getByTestId("stepper-configure"));
  // The Sheets sub-flow surfaces the honest no-Google-connection gate rather than
  // firing a configure call with a null connection_id.
  expect(await screen.findByTestId("sheets-config")).toBeInTheDocument();
  expect(screen.getByTestId("sheets-no-connection")).toBeInTheDocument();
  // MUI renders a disabled select as a combobox with aria-disabled="true".
  expect(
    within(screen.getByTestId("sheets-connection")).getByRole("combobox"),
  ).toHaveAttribute("aria-disabled", "true");
});

it("does NOT fire the Sheets configure call on Save draft when no Google connection is picked (H2)", async () => {
  const fetchMock = routedFetch({ create: { id: "ds-new-1" } });
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Créer un flux/ });

  await user.type(screen.getByLabelText(/Nom du flux/), "Flux Sheets");
  await user.click(screen.getByTestId("source-kind-managed_feed"));
  const format = screen.getByTestId("managed-format");
  await user.selectOptions(within(format).getByRole("combobox"), "google_sheets");

  // Provide spreadsheet + range but NO connection, then Save draft.
  await user.click(screen.getByTestId("stepper-configure"));
  await user.type(
    within(await screen.findByTestId("sheets-spreadsheet")).getByRole("textbox"),
    "sheet-123",
  );
  await user.type(within(screen.getByTestId("sheets-range")).getByRole("textbox"), "A:F");
  await user.click(screen.getByTestId("wizard-save-draft"));

  await waitFor(() =>
    expect(screen.getByTestId("wizard-live")).toHaveTextContent("Brouillon enregistré"),
  );
  // The doomed configure call was never made (only the /api/datastreams create).
  const configureCall = fetchMock.mock.calls.find((c) =>
    String(c[0]).includes("/managed-feed/configure"),
  );
  expect(configureCall).toBeUndefined();
  expect(screen.getByTestId("wizard-live")).toHaveTextContent(/connexion Google/);
});

it("drift after validation makes the preview obsolete and re-blocks activation", async () => {
  routedFetch({});
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Créer un flux/ });

  await user.type(screen.getByLabelText(/Nom du flux/), "Flux drift");
  await user.click(screen.getByTestId("source-kind-connector_pull"));
  await user.click(screen.getByTestId("wizard-next"));

  const connSelect = within(screen.getByTestId("connector-connection")).getByRole("combobox");
  await user.click(connSelect);
  await user.click(await screen.findByRole("option", { name: /Meta Ads — Compte 1/ }));
  const reportSelect = within(await screen.findByTestId("connector-report")).getByRole("combobox");
  await user.click(reportSelect);
  await user.click(await screen.findByRole("option", { name: /campaign_daily/ }));
  await user.click(await screen.findByLabelText("Métrique spend"));

  // Validate cleanly first.
  await user.click(screen.getByTestId("stepper-preview"));
  await user.click(screen.getByTestId("preview-validate"));
  await waitFor(() => expect(screen.queryByTestId("preview-blocking")).not.toBeInTheDocument());

  // Now change the selection (drift) -> back on Preview it is obsolete.
  await user.click(screen.getByTestId("stepper-configure"));
  await user.click(await screen.findByLabelText("Dimension date"));
  await user.click(screen.getByTestId("stepper-preview"));
  // Re-validating would clear it; assert the drift banner OR that activation is
  // blocked again on Schedule.
  await user.click(screen.getByTestId("stepper-schedule"));
  expect(screen.getByTestId("activation-blocked")).toBeInTheDocument();
  expect(screen.getByTestId("activate-datastream")).toBeDisabled();
});
