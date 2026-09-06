/**
 * Chantier B, the console half: several feeds are BOUND, one is ASKED for.
 *
 * The defect these pin: the panel used to make a person pick one Datastream out
 * of the eight that carry `views`, bind that one, and record nothing about the
 * choice. Every later question at another grain then read as a cross-source one
 * and was refused.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import ConceptDatastreamBindings, {
  type BindingChoice,
  type ConceptFeeds,
} from "../governance/ConceptDatastreamBindings";
import { apiGet } from "../lib/apiFetch";

vi.mock("../lib/apiFetch", () => ({
  ApiError: class ApiError extends Error {},
  apiGet: vi.fn(),
}));

const mockedGet = vi.mocked(apiGet);

const VIEWS = { value: "sc_views|scv_1", name: "views", label: "Views" };
const COUNTRY = { value: "sc_country|scv_2", name: "country", label: "Country" };

function Harness({ onFeeds }: { onFeeds: (feeds: ConceptFeeds) => void }) {
  const [value, setValue] = useState<BindingChoice>({});
  return (
    <ConceptDatastreamBindings
      projectId="proj_EXAMPLE"
      concepts={[VIEWS, COUNTRY]}
      value={value}
      onChange={setValue}
      onFeedsChange={onFeeds}
      disabled={false}
    />
  );
}

beforeEach(() => {
  mockedGet.mockImplementation((path: string) => {
    if (path.includes("/views")) {
      return Promise.resolve({
        used_by: [
          { datastream_id: "ds_channel", datastream_name: "channel performance" },
          { datastream_id: "ds_country", datastream_name: "audience by country" },
          { datastream_id: "ds_device", datastream_name: "audience by device" },
        ],
      } as never);
    }
    return Promise.resolve({
      used_by: [{ datastream_id: "ds_country", datastream_name: "audience by country" }],
    } as never);
  });
});

afterEach(() => {
  mockedGet.mockReset();
});

it("reports EVERY feed of a measure, not the one a person picked", async () => {
  const seen: ConceptFeeds[] = [];
  render(<Harness onFeeds={(feeds) => seen.push(feeds)} />);

  await waitFor(() => expect(seen.length).toBeGreaterThan(0));
  const last = seen[seen.length - 1];
  expect(last["sc_views"]).toEqual(["ds_channel", "ds_country", "ds_device"]);
  expect(last["sc_country"]).toEqual(["ds_country"]);
});

it("asks which Datastream holds the total, and says the others are read as breakdowns", async () => {
  render(<Harness onFeeds={() => {}} />);

  const label = await screen.findByText(/which Datastream holds the total/i);
  expect(label).toBeTruthy();
  expect(
    screen.getByText(/All 3 Datastreams that measure it are bound/i),
  ).toBeTruthy();
  // A single-fed Concept is still not a question.
  expect(screen.queryByLabelText(/Country — which Datastream/i)).toBeNull();
});

it("does not report a pending arbitration once the total is named", async () => {
  const pending: string[][] = [];
  function PendingHarness() {
    const [value, setValue] = useState<BindingChoice>({});
    return (
      <ConceptDatastreamBindings
        projectId="proj_EXAMPLE"
        concepts={[VIEWS]}
        value={value}
        onChange={setValue}
        onPendingChange={(ids) => pending.push(ids)}
        disabled={false}
      />
    );
  }
  render(<PendingHarness />);

  await waitFor(() => expect(pending.at(-1)).toEqual(["sc_views"]));
  await userEvent.selectOptions(
    await screen.findByLabelText(/which Datastream holds the total/i),
    "ds_channel",
  );
  await waitFor(() => expect(pending.at(-1)).toEqual([]));
});
