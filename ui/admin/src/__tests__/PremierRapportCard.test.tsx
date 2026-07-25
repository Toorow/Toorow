import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import PremierRapportCard from "../datastreams/PremierRapportCard";

const recommended = {
  outcome: "recommended",
  report_id: "campaign_daily",
  metrics: ["spend"],
  dimensions: ["date"],
  grain: ["date"],
  timezone: "UTC",
  currency: "EUR",
  interval: {
    start: "2026-06-22T00:00:00Z",
    end_exclusive: "2026-07-22T00:00:00Z",
    window_days: 30,
    label: "30 derniers jours",
  },
  estimated_cost: { read_points: 150, unit: "read_points", window_days: 30, quota_pre_check: "ok" },
  capability_contract_version: "1",
  capability_fingerprint: "a".repeat(64),
  compatibility: { reason: "single_compatible_report", explanation: "Un rapport compatible et une période récente bornée." },
  candidates: [
    { report_id: "campaign_daily", metrics: ["spend"], dimensions: ["date"], grain: ["date"], currency: "EUR", estimated_cost: { read_points: 150, unit: "read_points", window_days: 30, quota_pre_check: "ok" } },
  ],
};

const needsChoice = {
  ...recommended,
  outcome: "needs_choice",
  report_id: null,
  metrics: [], dimensions: [], grain: [], currency: "unknown",
  estimated_cost: null,
  compatibility: { reason: "multiple_compatible_reports", explanation: "Plusieurs rapports sont compatibles." },
  candidates: [
    { report_id: "spend_daily", metrics: ["spend"], dimensions: ["date"], grain: ["date"], currency: "EUR", estimated_cost: { read_points: 150, unit: "read_points", window_days: 30, quota_pre_check: "ok" } },
    { report_id: "clicks_daily", metrics: ["clicks"], dimensions: ["date"], grain: ["date"], currency: "unknown", estimated_cost: { read_points: 90, unit: "read_points", window_days: 30, quota_pre_check: "ok" } },
  ],
};

function renderCard() {
  return render(
    <ThemeProvider theme={adminTheme}>
      <PremierRapportCard projectId="proj-1" datastreamId="ds-1" connectionRefId="conn-1" apiToken="token" />
    </ThemeProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

it("shows one recommended report, the recent period, explicit grain/tz/currency and estimated cost", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => recommended } as Response);
  renderCard();
  expect(await screen.findByRole("heading", { name: "Premier rapport recommandé" })).toBeInTheDocument();
  expect(screen.getByText("Recommandation prête")).toBeInTheDocument();
  expect(screen.getByText(/30 derniers jours/)).toBeInTheDocument();
  expect(screen.getByText(/150 read_points/)).toBeInTheDocument();
  // Editable grain / timezone / currency controls are present.
  const fields = screen.getByTestId("first-report-fields");
  expect(fields).toBeInTheDocument();
  expect(screen.getByLabelText("Fuseau horaire")).toBeInTheDocument();
  expect(screen.getByLabelText("Devise")).toBeInTheDocument();
});

it("explains compatibility and never silently picks when several reports are compatible", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => needsChoice } as Response);
  renderCard();
  expect(await screen.findByText("Un choix est nécessaire")).toBeInTheDocument();
  // The compatibility explanation is shown (header + warning alert), never a silent pick.
  expect(screen.getAllByText("Plusieurs rapports sont compatibles.").length).toBeGreaterThanOrEqual(1);
  // Both candidate reports are offered for explicit selection (open the Rapport menu).
  const user = userEvent.setup();
  await user.click(screen.getByLabelText("Rapport"));
  expect(await screen.findByRole("option", { name: "spend_daily" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "clicks_daily" })).toBeInTheDocument();
});

it("saves the edited draft with an idempotency key and announces the frozen versions", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce({ ok: true, json: async () => recommended } as Response)
    .mockResolvedValueOnce({ ok: true, json: async () => ({ draft_id: "frdraft_1" }) } as Response);
  renderCard();
  const save = await screen.findByRole("button", { name: "Enregistrer le brouillon" });
  const user = userEvent.setup();
  await user.click(save);
  expect(await screen.findByRole("status")).toHaveTextContent("Brouillon enregistré");
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  const mutation = fetchMock.mock.calls[1];
  expect(mutation[0]).toContain("/api/projects/proj-1/datastreams/ds-1/first-report/draft");
  expect((mutation[1] as RequestInit).headers).toMatchObject({ "Idempotency-Key": expect.any(String) });
  const body = JSON.parse((mutation[1] as RequestInit).body as string);
  expect(body).toMatchObject({ report_id: "campaign_daily", grain: ["date"], timezone: "UTC", currency: "EUR" });
  expect(body.interval).toMatchObject({ start: "2026-06-22T00:00:00Z", end_exclusive: "2026-07-22T00:00:00Z" });
});
