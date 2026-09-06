/**
 * Plan & Entitlements — the trial ceiling finally has a surface (AI-176).
 *
 * Measured 2026-08-04, before writing anything: `grep -ril entitlement
 * ui/admin/src` returned ZERO files. Epic 34 shipped `check_datastream_limit`
 * (a typed 409 `trial_datastream_limit` on the 4th datastream) and a
 * super-admin-only POST hidden behind a 404 — so the cap was enforced against
 * people who had no way to read it. The QA gate G5-T05 ("compteur d'essai
 * affiché et exact") had nothing to point at.
 *
 * These tests hold the ways this section is worse than nothing if it drifts:
 *
 *   - `null` limits (full/internal = no cap) rendered as a NUMBER: an unlimited
 *     org would read as capped, and 0 is the most alarming number available;
 *   - the counter and the refusal disagreeing — so the used/limit pair comes
 *     from the server's own `usage`, never recomputed here;
 *   - a reassuring "0 of 3" painted over a FAILED read;
 *   - a trial COUNTDOWN. `max_backfill_days` is the depth of history an import
 *     may reach, not the lifetime of the organization. epic-34 §7 leaves
 *     "30 days = backfill window OR org expiry?" explicitly open, so the screen
 *     labels the field for what the server holds and invents no clock.
 */
import { render, screen, waitFor } from "@testing-library/react";
import OrgSettings from "../shell/pages/OrgSettings";

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock("../lib/apiFetch", () => ({
  apiGet: apiGetMock,
  apiJson: vi.fn(),
  apiPost: vi.fn(),
  ApiError: class extends Error {},
}));

vi.mock("../shell/GlobalScopeLayout", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const TRIAL = {
  org_id: "org_EXAMPLE",
  plan: "trial",
  limits: { max_datastreams: 3, max_backfill_days: 30 },
  usage: { active_datastreams: 2 },
  granted_at: null,
};

function renderPlan() {
  return render(<OrgSettings orgId="org_EXAMPLE" section="plan" />);
}

beforeEach(() => {
  apiGetMock.mockReset();
});

test("the plan section reads the plan route, not the org record", async () => {
  apiGetMock.mockResolvedValue(TRIAL);
  renderPlan();
  await waitFor(() =>
    expect(apiGetMock).toHaveBeenCalledWith("/api/organizations/org_EXAMPLE/plan"),
  );
});

test("a trial org sees its plan, its usage and its ceiling", async () => {
  apiGetMock.mockResolvedValue(TRIAL);
  renderPlan();
  expect(await screen.findByText("trial")).toBeInTheDocument();
  // The pair, not two loose numbers: 2 alone says nothing about the ceiling.
  expect(screen.getByText("2 / 3")).toBeInTheDocument();
  expect(screen.getByText("30 days")).toBeInTheDocument();
});

test("the backfill window is labelled as a window, never as a trial countdown", async () => {
  apiGetMock.mockResolvedValue(TRIAL);
  renderPlan();
  expect(await screen.findByText("Backfill window")).toBeInTheDocument();
  // No clock, no expiry, no "days left" — that arbitration is open (epic-34 §7).
  expect(screen.queryByText(/days left/i)).toBeNull();
  expect(screen.queryByText(/expires/i)).toBeNull();
  expect(screen.queryByText(/trial ends/i)).toBeNull();
});

test("an unlimited plan reports no cap, and never the number 0", async () => {
  apiGetMock.mockResolvedValue({
    org_id: "org_EXAMPLE",
    plan: "full",
    limits: { max_datastreams: null, max_backfill_days: null },
    usage: { active_datastreams: 41 },
  });
  renderPlan();
  expect(await screen.findByText("full")).toBeInTheDocument();
  expect(screen.getByText("Unlimited")).toBeInTheDocument();
  // Usage still shown — an unlimited org still gets to see what it runs.
  expect(screen.getByText("41")).toBeInTheDocument();
  // The tell-tale of a null rendered as a number.
  expect(screen.queryByText("41 / 0")).toBeNull();
  expect(screen.queryByText("0")).toBeNull();
});

test("at the limit, the screen names the refusal the API will actually return", async () => {
  apiGetMock.mockResolvedValue({ ...TRIAL, usage: { active_datastreams: 3 } });
  renderPlan();
  expect(await screen.findByText("The datastream limit is reached")).toBeInTheDocument();
  // The exact code the create path raises, so the two can be matched by anyone
  // reading a failed request.
  expect(screen.getByText("trial_datastream_limit")).toBeInTheDocument();
  // And it names the REACH that is enforced, not a narrower one. The sentence
  // used to read "the cap bounds creation only" while `check_datastream_limit`
  // had five callers; a person turning a paused datastream back on was refused
  // by a ceiling this screen had told them did not apply.
  expect(screen.getByText(/creating one, publishing one/)).toBeInTheDocument();
  expect(screen.getByText(/turning a paused one back on/)).toBeInTheDocument();
  expect(screen.getByText(/restoring one from a rollback/)).toBeInTheDocument();
  // And what is NOT affected, still said.
  expect(screen.getByText(/Datastreams already on keep running/)).toBeInTheDocument();
  expect(screen.queryByText(/bounds creation only/)).toBeNull();
});

test("below the limit, no refusal banner is shown", async () => {
  apiGetMock.mockResolvedValue(TRIAL);
  renderPlan();
  await screen.findByText("2 / 3");
  expect(screen.queryByText("The datastream limit is reached")).toBeNull();
});

test("a failed read shows the failure and NO counter", async () => {
  apiGetMock.mockRejectedValue(new Error("503"));
  renderPlan();
  expect(await screen.findByText(/Organization Settings unavailable/)).toBeInTheDocument();
  // The whole point: no number is invented over a dead read.
  expect(screen.queryByText("2 / 3")).toBeNull();
  expect(screen.queryByText("Active datastreams")).toBeNull();
});

test("the section offers no upgrade control, because no self-serve path exists", async () => {
  apiGetMock.mockResolvedValue(TRIAL);
  renderPlan();
  await screen.findByText("2 / 3");
  expect(screen.queryByRole("button", { name: /upgrade/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /buy|billing|subscribe/i })).toBeNull();
  expect(screen.getByText(/changed by toorow, not from this screen/)).toBeInTheDocument();
});
