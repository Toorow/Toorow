/**
 * Chantier C, the console half: one gesture, and what it would do said first.
 *
 * The defect these pin: the four operations that create a governed Event
 * Configuration had zero console callers, so the object could only exist if
 * someone knew their four addresses. And nothing about it was domain-specific,
 * so the panel must branch on no domain at all.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import EventStreamPanel from "../datastreams/workbench/EventStreamPanel";
import { apiGet, apiPost } from "../lib/apiFetch";

vi.mock("../lib/apiFetch", () => ({
  ApiError: class ApiError extends Error {},
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}));

const mockedGet = vi.mocked(apiGet);
const mockedPost = vi.mocked(apiPost);

const PLAN = {
  datastream_name: "Shop - product launches",
  configuration_name: "product_launch",
  source_mapping: {
    module: "shopify",
    report_id: "product_launch",
    events: ["product_launch"],
  },
  collection_policy: { cadence: "nightly", timezone: "UTC" },
};

function mount() {
  return render(<EventStreamPanel projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />);
}

afterEach(() => {
  mockedGet.mockReset();
  mockedPost.mockReset();
});

it("says what arming would create before anything is written", async () => {
  mockedGet.mockResolvedValue({ armed: null, plan: PLAN, blocked_reason: null } as never);
  mount();

  expect(await screen.findByText(/Nothing is written until you ask for it/)).toBeTruthy();
  // `product_launch` is both the event and the report id, so it appears twice --
  // which is the derivation working, not a duplicate.
  expect(screen.getAllByText("product_launch")).toHaveLength(2);
  expect(screen.getByText("shopify")).toBeTruthy();
  expect(mockedPost).not.toHaveBeenCalled();
});

it("arms a NON-video domain through the one address, then re-reads", async () => {
  mockedGet
    .mockResolvedValueOnce({ armed: null, plan: PLAN, blocked_reason: null } as never)
    .mockResolvedValueOnce({
      armed: {
        event_configuration_id: "ecfg_1",
        name: "product_launch",
        lifecycle_state: "active",
        version_number: 1,
        review_state: "active",
      },
      plan: PLAN,
      blocked_reason: null,
    } as never);
  mockedPost.mockResolvedValue({} as never);
  mount();

  await userEvent.click(await screen.findByRole("button", { name: "Arm this event stream" }));

  await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1));
  expect(mockedPost.mock.calls[0][0]).toBe(
    "/api/projects/proj_EXAMPLE/datastreams/ds_EXAMPLE/event-stream",
  );
  expect(await screen.findByText(/Armed as product_launch/)).toBeTruthy();
  // Armed: the gesture is gone rather than offered a second time.
  expect(screen.queryByRole("button", { name: "Arm this event stream" })).toBeNull();
});

it("offers no button on a Datastream that emits no event, and says why", async () => {
  mockedGet.mockResolvedValue({
    armed: null,
    plan: null,
    blocked_reason:
      "What this Datastream collects is measurements, not events. Add a Datastream on a report that declares events, and arm that one.",
  } as never);
  mount();

  expect(await screen.findByText(/This Datastream emits no event/)).toBeTruthy();
  expect(screen.getByText(/measurements, not events/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Arm/ })).toBeNull();
});

it("keeps the server's own sentence when arming is refused", async () => {
  mockedGet.mockResolvedValue({ armed: null, plan: PLAN, blocked_reason: null } as never);
  const { ApiError } = await import("../lib/apiFetch");
  mockedPost.mockRejectedValue(
    new (ApiError as unknown as new (message: string) => Error)(
      "This Datastream already collects events under `product_launch`. Open it to change what it collects.",
    ),
  );
  mount();

  await userEvent.click(await screen.findByRole("button", { name: "Arm this event stream" }));
  expect(await screen.findByText(/already collects events under/)).toBeTruthy();
});

it("does not claim a stream is unarmed when the read itself failed", async () => {
  mockedGet.mockRejectedValue(new Error("gateway timeout"));
  mount();

  expect(await screen.findByText(/could not be read/)).toBeTruthy();
  expect(screen.getByText(/nothing is claimed about what is/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Arm this event stream/ })).toBeNull();
});
