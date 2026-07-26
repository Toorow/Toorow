import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import { CreerFluxWizard } from "../datastreams/wizard";
import DatastreamCreate from "../shell/pages/DatastreamCreate";

const CONNECTIONS = {
  connections: [
    {
      id: "conn-1",
      connection_ref_id: "cref-1",
      provider: "meta_ads",
      display_name: "Meta Ads account",
      status: "active",
      health: { status: "ok", last_checked_at: "2026-07-26T12:00:00Z" },
    },
  ],
};

function capabilities(connectionRefId = "cref-1", reportId = "campaign_daily") {
  return {
    contract_version: "1",
    project_id: "proj-1",
    connection_ref_id: connectionRefId,
    module: { name: "meta_ads", display_name: "Meta Ads", module_kind: "kpi" },
    fields: [
      {
        field_id: "spend",
        source_field: "spend",
        kind: "metric",
        physical_type: "number",
        description: "Media spend",
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
        description: "Reporting day",
        semantic_hints: ["date"],
        canonical_target: "date",
        aggregation: null,
        non_additive: false,
      },
    ],
    reports: [
      {
        id: reportId,
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
}

type RouterOptions = {
  connections?: { connections: Array<Record<string, unknown>> };
  connectionsLoader?: () => Promise<{ connections: Array<Record<string, unknown>> }>;
  capabilityLoader?: (connectionRefId: string) => Promise<unknown>;
  failFirstCreate?: boolean;
  failFirstValidation?: boolean;
  validationCapabilityFingerprint?: string;
  draftIssues?: Array<{ code: string; path?: string; message: string }>;
};

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

function routedFetch(options: RouterOptions = {}) {
  let createAttempts = 0;
  let validationAttempts = 0;
  return vi.spyOn(globalThis, "fetch").mockImplementation(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/connections")) {
        return ok(
          options.connectionsLoader
            ? await options.connectionsLoader()
            : (options.connections ?? CONNECTIONS),
        );
      }
      if (url.includes("/api/source-capabilities")) {
        const connectionRefId = new URL(url, "http://localhost").searchParams.get(
          "connection_ref_id",
        )!;
        return ok(
          options.capabilityLoader
            ? await options.capabilityLoader(connectionRefId)
            : capabilities(connectionRefId),
        );
      }
      if (url.endsWith("/validate")) {
        validationAttempts += 1;
        if (options.failFirstValidation && validationAttempts === 1) {
          throw new TypeError("Validation response lost after draft save");
        }
        const body = JSON.parse(String(init?.body ?? "{}"));
        const intent = body.intent as Record<string, any>;
        const metrics = intent?.source?.selection?.metrics ?? [];
        const issues = metrics.length
          ? []
          : [
              {
                code: "missing_metrics",
                path: "$.source.selection.metrics",
                message: "Select at least one metric.",
              },
            ];
        return ok({
          normalized_intent: intent,
          content_hash: "hash-1",
          executable: issues.length === 0,
          issues,
          capability_contract_version: "1",
          capability_fingerprint: options.validationCapabilityFingerprint ?? "cap-1",
        });
      }
      if (url.endsWith("/api/datastreams") && init?.method === "POST") {
        createAttempts += 1;
        if (options.draftIssues) {
          return {
            ok: false,
            status: 422,
            json: async () => ({ code: "validation_error", errors: options.draftIssues }),
          } as Response;
        }
        if (options.failFirstCreate && createAttempts === 1) {
          throw new TypeError("Network connection lost after send");
        }
        const body = JSON.parse(String(init.body ?? "{}"));
        if (typeof body?.intent?.source?.kind !== "string") {
          return {
            ok: false,
            status: 422,
            json: async () => ({
              code: "invalid_intent_schema",
              message: "source.kind is required",
            }),
          } as Response;
        }
        return ok({
          id: "ds-new-1",
          plan_version: {
            id: "dsp-1",
            executable: true,
            content_hash: "hash-1",
            capability_contract_version: "1",
            capability_fingerprint: "cap-1",
          },
        });
      }
      if (url.endsWith("/api/datastreams/ds-new-1") && init?.method === "PATCH") {
        const body = JSON.parse(String(init.body ?? "{}"));
        if (body.enabled === true) return ok({ id: "ds-new-1", enabled: true });
        return ok({
          id: "ds-new-1",
          plan_version: {
            id: "dsp-2",
            executable: true,
            content_hash: "hash-1",
            capability_contract_version: "1",
            capability_fingerprint: "cap-1",
          },
        });
      }
      if (url.includes("/managed-feed/configure")) {
        return ok({ datastream_id: "ds-new-1" });
      }
      return {
        ok: false,
        status: 404,
        json: async () => ({ code: "not_found", message: "Not found" }),
      } as Response;
    },
  );
}

function renderWizard(overrides: Partial<Parameters<typeof CreerFluxWizard>[0]> = {}) {
  return render(
    <ThemeProvider theme={adminTheme}>
      <CreerFluxWizard projectId="proj-1" {...overrides} />
    </ThemeProvider>,
  );
}

async function chooseConnectorPlan(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/Datastream name/), "Campaign feed");
  await user.click(screen.getByTestId("source-kind-connector_pull"));
  await user.click(screen.getByTestId("wizard-next"));

  const connection = within(screen.getByTestId("connector-connection")).getByRole("combobox");
  await user.click(connection);
  await user.click(await screen.findByRole("option", { name: /Meta Ads account/ }));

  const report = within(await screen.findByTestId("connector-report")).getByRole("combobox");
  await user.click(report);
  await user.click(await screen.findByRole("option", { name: /campaign_daily/ }));
  await user.click(await screen.findByLabelText("Metric spend"));
}

afterEach(() => vi.restoreAllMocks());

it("mounts the canonical six-stage wizard on the routed Add Datastream page", async () => {
  routedFetch();
  render(
    <ThemeProvider theme={adminTheme}>
      <DatastreamCreate projectId="proj-1" onCancel={vi.fn()} onActivated={vi.fn()} />
    </ThemeProvider>,
  );

  expect(await screen.findByRole("heading", { name: "Add Datastream" })).toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: /Create a Datastream — Source/ }),
  ).toBeInTheDocument();
  for (const stage of ["source", "configure", "destination", "classify", "preview", "schedule"]) {
    expect(screen.getByTestId(`stepper-${stage}`)).toBeInTheDocument();
  }
  expect(screen.queryByText(/Apple|Acme|3h|7 datastreams/i)).not.toBeInTheDocument();
});

it("scopes provider-account reads and create writes to the active project", async () => {
  const fetchMock = routedFetch();
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Create a Datastream/ });

  await user.type(screen.getByLabelText(/Datastream name/), "External table");
  await user.click(screen.getByTestId("source-kind-external_bq"));
  await user.click(screen.getByTestId("wizard-save-draft"));

  await waitFor(() => expect(screen.getByTestId("wizard-live")).toHaveTextContent("Draft saved"));
  const connectionCall = fetchMock.mock.calls.find(([input]) =>
    String(input).includes("/api/connections"),
  );
  expect(String(connectionCall?.[0])).toContain("project_id=proj-1");
  const createCall = fetchMock.mock.calls.find(
    ([input, init]) => String(input).endsWith("/api/datastreams") && init?.method === "POST",
  );
  const createInit = createCall?.[1] as RequestInit;
  expect(createInit.headers).toMatchObject({ "Idempotency-Key": expect.any(String) });
  expect(JSON.parse(String(createInit.body))).toMatchObject({
    project_id: "proj-1",
    name: "External table",
    intent: {
      contract_version: "1",
      source: { kind: "external_bq", writer_kind: "external" },
      destination: { policy: "external_read_only" },
    },
  });
});

it("reuses the same create idempotency key after an outcome-unknown network failure", async () => {
  const fetchMock = routedFetch({ failFirstCreate: true });
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Create a Datastream/ });

  await user.type(screen.getByLabelText(/Datastream name/), "Retryable feed");
  await user.click(screen.getByTestId("source-kind-external_bq"));
  await user.click(screen.getByTestId("wizard-save-draft"));
  expect(await screen.findByTestId("wizard-error")).toHaveTextContent(/connection lost/i);
  await user.click(screen.getByTestId("wizard-save-draft"));
  await waitFor(() => expect(screen.getByTestId("wizard-live")).toHaveTextContent("Draft saved"));

  const creates = fetchMock.mock.calls.filter(
    ([input, init]) => String(input).endsWith("/api/datastreams") && init?.method === "POST",
  );
  expect(creates).toHaveLength(2);
  expect((creates[0][1] as RequestInit).headers).toMatchObject({
    "Idempotency-Key": ((creates[1][1] as RequestInit).headers as Record<string, string>)[
      "Idempotency-Key"
    ],
  });
});

it("reuses the saved draft ID when validation fails after create", async () => {
  const fetchMock = routedFetch({ failFirstValidation: true });
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Create a Datastream/ });
  await chooseConnectorPlan(user);

  await user.click(screen.getByTestId("stepper-preview"));
  await user.click(screen.getByTestId("preview-validate"));
  expect(await screen.findByTestId("wizard-error")).toHaveTextContent(/response lost/i);
  await user.click(screen.getByTestId("preview-validate"));
  expect(await screen.findByTestId("preview-plan")).toHaveTextContent("Plan version: dsp-2");

  const creates = fetchMock.mock.calls.filter(
    ([input, init]) => String(input).endsWith("/api/datastreams") && init?.method === "POST",
  );
  const revisions = fetchMock.mock.calls.filter(([input, init]) => {
    if (!String(input).endsWith("/api/datastreams/ds-new-1") || init?.method !== "PATCH") {
      return false;
    }
    return JSON.parse(String(init.body)).intent != null;
  });
  expect(creates).toHaveLength(1);
  expect(revisions).toHaveLength(1);
});

it("shows loading honestly and disables revoked or unhealthy provider accounts", async () => {
  let resolveConnections!: (value: { connections: Array<Record<string, unknown>> }) => void;
  const pendingConnections = new Promise<{ connections: Array<Record<string, unknown>> }>(
    (resolve) => {
      resolveConnections = resolve;
    },
  );
  routedFetch({ connectionsLoader: () => pendingConnections });
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Create a Datastream/ });
  await user.type(screen.getByLabelText(/Datastream name/), "Account health");
  await user.click(screen.getByTestId("source-kind-connector_pull"));
  await user.click(screen.getByTestId("wizard-next"));
  expect(screen.getByTestId("connections-loading")).toHaveTextContent(
    "Loading provider accounts",
  );
  expect(screen.queryByText("No project-authorized provider accounts")).not.toBeInTheDocument();

  resolveConnections({
    connections: [
      ...CONNECTIONS.connections,
      {
        id: "conn-revoked",
        connection_ref_id: "cref-revoked",
        provider: "meta_ads",
        display_name: "Revoked account",
        status: "revoked",
        health: { status: "revoked" },
      },
      {
        id: "conn-stale",
        connection_ref_id: "cref-stale",
        provider: "meta_ads",
        display_name: "Stale account",
        status: "active",
        health: { status: "stale" },
      },
    ],
  });
  await waitFor(() => expect(screen.queryByTestId("connections-loading")).not.toBeInTheDocument());
  const picker = within(screen.getByTestId("connector-connection")).getByRole("combobox");
  await user.click(picker);
  expect(
    screen.getByRole("option", { name: /Meta Ads account.*healthy/i }),
  ).not.toHaveAttribute("aria-disabled", "true");
  expect(screen.getByRole("option", { name: /Revoked account.*revoked/i })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(screen.getByRole("option", { name: /Stale account.*not healthy/i })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
});
it("ignores a stale capability response after the operator changes provider account", async () => {
  let resolveFirst!: (value: unknown) => void;
  const first = new Promise((resolve) => {
    resolveFirst = resolve;
  });
  routedFetch({
    connections: {
      connections: [
        ...CONNECTIONS.connections,
        {
          id: "conn-2",
          connection_ref_id: "cref-2",
          provider: "google_ads",
          display_name: "Google Ads account",
          status: "active",
      health: { status: "ok", last_checked_at: "2026-07-26T12:00:00Z" },
        },
      ],
    },
    capabilityLoader: (connectionRefId) =>
      connectionRefId === "cref-1"
        ? first
        : Promise.resolve(capabilities("cref-2", "google_daily")),
  });
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Create a Datastream/ });
  await user.type(screen.getByLabelText(/Datastream name/), "Switch accounts");
  await user.click(screen.getByTestId("source-kind-connector_pull"));
  await user.click(screen.getByTestId("wizard-next"));

  const connection = within(screen.getByTestId("connector-connection")).getByRole("combobox");
  await user.click(connection);
  await user.click(await screen.findByRole("option", { name: /Meta Ads account/ }));
  await user.click(connection);
  await user.click(await screen.findByRole("option", { name: /Google Ads account/ }));
  const report = within(await screen.findByTestId("connector-report")).getByRole("combobox");
  await user.click(report);
  await user.click(await screen.findByRole("option", { name: "google_daily" }));
  resolveFirst(capabilities("cref-1", "stale_meta_report"));
  await waitFor(() => expect(report).toHaveTextContent("google_daily"));
  await user.click(report);
  expect(screen.queryByRole("option", { name: "stale_meta_report" })).not.toBeInTheDocument();
  expect(screen.getByRole("option", { name: "google_daily" })).toBeInTheDocument();
});

it("rejects fresh validation evidence that does not match the saved immutable plan", async () => {
  routedFetch({ validationCapabilityFingerprint: "cap-changed" });
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Create a Datastream/ });
  await chooseConnectorPlan(user);
  await user.click(screen.getByTestId("stepper-preview"));
  await user.click(screen.getByTestId("preview-validate"));
  expect(await screen.findByTestId("wizard-error")).toHaveTextContent(
    /Capability evidence no longer matches/,
  );
  expect(screen.queryByTestId("preview-plan")).not.toBeInTheDocument();
});
it("uses server validation issues to block activation", async () => {
  routedFetch();
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Create a Datastream/ });
  await user.type(screen.getByLabelText(/Datastream name/), "Incomplete feed");
  await user.click(screen.getByTestId("source-kind-connector_pull"));
  await user.click(screen.getByTestId("stepper-preview"));
  await user.click(screen.getByTestId("preview-validate"));
  expect(await screen.findByTestId("preview-blocking")).toHaveTextContent("missing_metrics");
  await user.click(screen.getByTestId("stepper-schedule"));
  expect(screen.getByTestId("activate-datastream")).toBeDisabled();
});

it("validates, activates, and hands the created ID to post-create navigation", async () => {
  const fetchMock = routedFetch();
  const onActivated = vi.fn();
  const user = userEvent.setup();
  renderWizard({ onActivated });
  await screen.findByRole("heading", { name: /Create a Datastream/ });
  await chooseConnectorPlan(user);

  await user.click(screen.getByTestId("stepper-preview"));
  await user.click(screen.getByTestId("preview-validate"));
  expect(await screen.findByTestId("preview-plan")).toHaveTextContent("Plan version: dsp-1");
  expect(screen.getByTestId("preview-intent-content-hash")).toHaveTextContent(
    "Intent content hash ...hash-1",
  );
  expect(screen.getByTestId("preview-capability-fingerprint")).toHaveTextContent(
    /Capability contract: 1.*fingerprint.*cap-1/,
  );
  await user.click(screen.getByTestId("stepper-schedule"));
  const activate = screen.getByTestId("activate-datastream");
  expect(activate).toBeEnabled();
  await user.click(activate);
  await waitFor(() => expect(onActivated).toHaveBeenCalledWith("ds-new-1"));

  const activation = fetchMock.mock.calls.find(([input, init]) => {
    if (!String(input).endsWith("/api/datastreams/ds-new-1") || init?.method !== "PATCH") {
      return false;
    }
    return JSON.parse(String(init.body)).enabled === true;
  });
  expect(activation).toBeTruthy();
  expect(JSON.parse(String((activation?.[1] as RequestInit).body))).toMatchObject({
    project_id: "proj-1",
    enabled: true,
    plan_version_id: "dsp-1",
  });
  expect((activation?.[1] as RequestInit).headers).toMatchObject({
    "Idempotency-Key": expect.any(String),
  });
});

it("treats schedule changes after validation as drift and blocks activation", async () => {
  routedFetch();
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Create a Datastream/ });
  await chooseConnectorPlan(user);
  await user.click(screen.getByTestId("stepper-preview"));
  await user.click(screen.getByTestId("preview-validate"));
  expect(await screen.findByTestId("preview-plan")).toBeInTheDocument();
  await user.click(screen.getByTestId("stepper-schedule"));
  expect(screen.getByTestId("activate-datastream")).toBeEnabled();
  const timezone = within(screen.getByTestId("schedule-timezone")).getByRole("combobox");
  await user.click(timezone);
  await user.click(await screen.findByRole("option", { name: "UTC" }));
  expect(await screen.findByTestId("activation-blocked")).toHaveTextContent(/changed/i);
  expect(screen.getByTestId("activate-datastream")).toBeDisabled();
});

it("resets draft identity and evidence when the route project changes", async () => {
  const fetchMock = routedFetch();
  const user = userEvent.setup();
  const view = renderWizard();
  await screen.findByRole("heading", { name: /Create a Datastream/ });
  await user.type(screen.getByLabelText(/Datastream name/), "Old project draft");
  await user.click(screen.getByTestId("source-kind-external_bq"));

  view.rerender(
    <ThemeProvider theme={adminTheme}>
      <CreerFluxWizard projectId="proj-2" />
    </ThemeProvider>,
  );

  await waitFor(() => expect(screen.getByLabelText(/Datastream name/)).toHaveValue(""));
  expect(screen.getByRole("radio", { name: /Existing BigQuery/ })).toHaveAttribute(
    "aria-checked",
    "false",
  );
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(([input]) =>
        String(input).includes("/api/connections?project_id=proj-2"),
      ),
    ).toBe(true),
  );
});

it("surfaces structured draft-validation issues without a top-level message", async () => {
  routedFetch({
    draftIssues: [
      {
        code: "invalid_selection",
        path: "$.source.selection.metrics",
        message: "Choose at least one supported metric.",
      },
    ],
  });
  const user = userEvent.setup();
  renderWizard();
  await screen.findByRole("heading", { name: /Create a Datastream/ });
  await user.type(screen.getByLabelText(/Datastream name/), "Invalid draft");
  await user.click(screen.getByTestId("source-kind-external_bq"));
  await user.click(screen.getByTestId("wizard-save-draft"));

  expect(await screen.findByTestId("wizard-error")).toHaveTextContent(
    "Choose at least one supported metric. ($.source.selection.metrics)",
  );
});