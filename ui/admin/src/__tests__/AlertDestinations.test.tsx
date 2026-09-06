/**
 * Story 59.6 — the alert-destinations screen, in its three states.
 *
 * Two of those states are DIFFERENT SENTENCES, and that is what this file is
 * for. "No destination configured" is a claim about the project; "Destinations
 * could not be read" is a claim about the request. Rendering the first when the
 * second is true tells a person their alerts are configured-empty when in fact
 * nobody knows — which is the failure mode this console has already shipped
 * once, and the reason the empty state carries a MEASURED number instead of a
 * zero.
 *
 * The screen lives in Governance (`alignment-register.md:98`, "alert rules in
 * Governance"), so `ProjectSettingsPage.test.tsx` is deliberately NOT extended:
 * arbitrage 2 landed on (b), and Project Settings is untouched.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AlertDestinations from "../governance/AlertDestinations";

const PROJECT = "proj_EXAMPLE";

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function failure(status: number, code: string, message: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ code, message }),
    text: async () => JSON.stringify({ code, message }),
  } as Response;
}

function envelope(overrides: Record<string, unknown> = {}) {
  return {
    destinations: [],
    firings_last_24h: 0,
    routable_alert_types: ["dq_volume", "dq_timeliness", "dq_schema"],
    console_only_signals: [
      "dead_letter_count",
      "verification_failure_count",
      "health_poller_staleness_seconds",
      "mirror_sync_lag_seconds",
    ],
    kinds: ["email", "slack_webhook", "webhook"],
    ...overrides,
  };
}

const SLACK = {
  id: "adest_SLACK",
  project_id: PROJECT,
  kind: "slack_webhook",
  label: "Ops Slack",
  target_masked: "https://hooks.example.invalid/…XXXX",
  alert_types: ["dq_timeliness"],
  enabled: true,
  created_at: "2026-08-08T09:00:00Z",
  has_secret: true,
  last_delivery: {
    delivered_at: "2026-08-08T02:05:00Z",
    state: "delivered",
    last_error_class: null,
    delivered_count: 3,
    failed_count: 0,
  },
};

const MAILBOX = {
  id: "adest_MAIL",
  project_id: PROJECT,
  kind: "email",
  label: "Ops mailbox",
  target_masked: "a…@example.invalid",
  alert_types: [],
  enabled: true,
  created_at: "2026-08-08T09:00:00Z",
  has_secret: false,
  last_delivery: null,
};

function serve(answer: Response | ((input: string, init?: RequestInit) => Response)) {
  const spy = vi.fn((input: string, init?: RequestInit) =>
    Promise.resolve(typeof answer === "function" ? answer(input, init) : answer),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Populated
// ---------------------------------------------------------------------------

test("a configured project shows each destination, its rule and its last send", async () => {
  serve(ok(envelope({ destinations: [SLACK, MAILBOX], firings_last_24h: 12 })));
  render(<AlertDestinations projectId={PROJECT} />);

  const slackRow = await screen.findByTestId("destination-adest_SLACK");
  expect(within(slackRow).getByText("Ops Slack")).toBeInTheDocument();
  expect(within(slackRow).getByText("Slack webhook")).toBeInTheDocument();
  // The target is MASKED. A Slack incoming-webhook URL is itself a credential.
  expect(within(slackRow).getByText("https://hooks.example.invalid/…XXXX")).toBeInTheDocument();
  expect(within(slackRow).getByText("dq_timeliness")).toBeInTheDocument();
  expect(within(slackRow).getByText(/3 delivered/)).toBeInTheDocument();

  // A destination with no rule receives everything, and says so rather than
  // showing an empty cell.
  const mailRow = screen.getByTestId("destination-adest_MAIL");
  expect(within(mailRow).getByText("Every alert of this project")).toBeInTheDocument();
  expect(within(mailRow).getByText("Never sent")).toBeInTheDocument();
});

test("the screen names the alerts no destination can receive", async () => {
  serve(ok(envelope({ destinations: [MAILBOX] })));
  render(<AlertDestinations projectId={PROJECT} />);

  expect(await screen.findByText(/What no destination can receive/)).toBeInTheDocument();
  expect(screen.getByText(/mirror_sync_lag_seconds/)).toBeInTheDocument();
  expect(screen.getByText(/carry no project/)).toBeInTheDocument();
});

test("a test send reports the transport's verdict, including an undeployed one", async () => {
  const user = userEvent.setup();
  serve((input) => {
    if (input.includes("/test")) {
      return ok({
        code: "transport_unavailable",
        delivered: false,
        detail: "SMTP_HOST is not set on this deployment, so no email can leave it.",
        destination_id: MAILBOX.id,
      });
    }
    return ok(envelope({ destinations: [MAILBOX] }));
  });
  render(<AlertDestinations projectId={PROJECT} />);

  await user.click(await screen.findByTestId(`test-${MAILBOX.id}`));

  const verdict = await screen.findByTestId(`verdict-${MAILBOX.id}`);
  // The refusal NAMES the missing variable: "it did not send" is not actionable.
  expect(verdict).toHaveTextContent("SMTP_HOST");
  expect(verdict).toHaveTextContent(/no email transport/);
});

test("removing a destination confirms with what will stop leaving, counted before", async () => {
  const user = userEvent.setup();
  serve(ok(envelope({ destinations: [SLACK] })));
  render(<AlertDestinations projectId={PROJECT} />);

  await user.click(await screen.findByTestId(`delete-${SLACK.id}`));

  const dialog = await screen.findByTestId("delete-destination-confirm");
  expect(dialog).toHaveTextContent("Ops Slack");
  expect(dialog).toHaveTextContent("dq_timeliness");
  // The count is the one BEFORE the act.
  expect(dialog).toHaveTextContent("3 alert(s) have already been delivered");
});

// ---------------------------------------------------------------------------
// Empty — the sentence, and a measured number
// ---------------------------------------------------------------------------

test("a project with no destination reads a sentence and the real firing count", async () => {
  serve(ok(envelope({ destinations: [], firings_last_24h: 522 })));
  render(<AlertDestinations projectId={PROJECT} />);

  const empty = await screen.findByTestId("destinations-empty");
  expect(empty).toHaveTextContent("Alerts stay in the console");
  // The number is measured, not a zero standing in for an absence.
  expect(empty).toHaveTextContent("522 firings in the last 24 hours");
  expect(screen.queryByTestId("destinations-broken")).not.toBeInTheDocument();
  // The screen is readable, so a destination can be added.
  expect(screen.getByTestId("add-destination")).toBeEnabled();
});

test("a project with no destination and no firing says zero because it counted", async () => {
  serve(ok(envelope({ destinations: [], firings_last_24h: 0 })));
  render(<AlertDestinations projectId={PROJECT} />);

  const empty = await screen.findByTestId("destinations-empty");
  expect(empty).toHaveTextContent("0 firings in the last 24 hours");
});

// ---------------------------------------------------------------------------
// Broken — the OTHER sentence
// ---------------------------------------------------------------------------

test("a read that fails says so, disables the add button and renders no list", async () => {
  serve(failure(500, "db_error", "Database error: connection refused"));
  render(<AlertDestinations projectId={PROJECT} />);

  const broken = await screen.findByTestId("destinations-broken");
  expect(broken).toHaveTextContent("Destinations could not be read");
  expect(broken).toHaveTextContent("this is not a project without destinations");

  // Neither an empty list nor an empty state: nothing was read.
  expect(screen.queryByTestId("destinations-empty")).not.toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.getByTestId("add-destination")).toBeDisabled();
});

test("a retry after a failed read shows the list without a reload", async () => {
  const user = userEvent.setup();
  let broken = true;
  serve(() =>
    broken
      ? failure(503, "unavailable", "Database error")
      : ok(envelope({ destinations: [MAILBOX] })),
  );
  render(<AlertDestinations projectId={PROJECT} />);

  await screen.findByTestId("destinations-broken");
  broken = false;
  await user.click(screen.getByRole("button", { name: "Retry" }));

  await waitFor(() => expect(screen.getByTestId(`destination-${MAILBOX.id}`)).toBeInTheDocument());
  expect(screen.queryByTestId("destinations-broken")).not.toBeInTheDocument();
});
