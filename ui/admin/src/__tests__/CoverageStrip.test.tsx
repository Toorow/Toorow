/**
 * CoverageStrip — the distinction the whole component exists for.
 *
 * `empty` and `never_fetched` look identical on any chart that merges them, and
 * they mean opposite things: one is the provider answering "no traffic that
 * day", the other is us never having asked. Merging them turns an answer into
 * an absence, which is exactly the class of quiet lie this codebase keeps
 * producing. These tests pin the difference and the refetch target set.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import CoverageStrip from "../shell/pages/CoverageStrip";

const LEDGER = [
  { date: "2026-07-20", status: "ok", row_count: 120 },
  { date: "2026-07-21", status: "empty", row_count: 0 },
  { date: "2026-07-22", status: "never_fetched", row_count: null },
  { date: "2026-07-23", status: "failed", row_count: null },
  { date: "2026-07-24", status: "partial", row_count: 12 },
  // Story 58.1: a day covered by a MULTI-DAY collection window. The ledger no
  // longer copies that window's total onto it, so it has no count of its own --
  // and it says which kind of absence that is.
  {
    date: "2026-07-25",
    status: "ok",
    row_count: null,
    row_count_reason: "measured_per_window",
  },
];

/**
 * A window the source REFUSED, beside a day nobody ever asked for.
 *
 * AI-307: the ledger answers `never_fetched` at the day grain for a prevented
 * window -- by design -- and publishes the window's OWN state beside it
 * (`extract_ledger._entry`). Read on the verdict alone, the two days merge into
 * one count, which is the same merge of an answer and an absence this component
 * exists to refuse. Its own fixture, because the days of `LEDGER` are counted
 * one by one by the tests above.
 */
const LEDGER_WITH_REFUSAL = [
  { date: "2026-07-20", status: "ok", row_count: 120 },
  { date: "2026-07-21", status: "never_fetched", row_count: null, job_state: null },
  { date: "2026-07-22", status: "never_fetched", row_count: null, job_state: "prevented" },
];

function mockFetch(onRefetch?: (body: unknown) => void) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (String(url).includes("/ledger")) {
      return { ok: true, status: 200, json: async () => ({ ledger: LEDGER }) } as Response;
    }
    if (String(url).includes("/refetch")) {
      onRefetch?.(JSON.parse(String(init?.body ?? "{}")));
      return { ok: true, status: 202, json: async () => ({ jobs: [{ job_id: "j1" }] }) } as Response;
    }
    throw new Error(`unexpected ${url}`);
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("CoverageStrip", () => {
  it("counts an empty day apart from a day that was never requested", async () => {
    vi.stubGlobal("fetch", mockFetch());
    render(<CoverageStrip projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("coverage-strip")).toBeInTheDocument());

    // The legend, entry by entry. Two SEPARATE entries each carrying its own
    // count of 1 — never one merged entry of 2, which is the whole point of the
    // component. Read off the list rather than by text, because every bar also
    // carries the same words in a screen-reader label.
    const legend = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(legend).toContain("Empty — the provider returned nothing1");
    expect(legend).toContain("Never requested1");
  });

  it("counts a refused window apart from a day nobody asked for", async () => {
    // The ledger's day-grain answer for a prevented window IS `never_fetched`,
    // so a strip reading the verdict alone counted two days under one word --
    // and the two call for different gestures: one is a schedule, the other is a
    // grant at the provider.
    vi.stubGlobal("fetch", vi.fn(async () => (
      { ok: true, status: 200, json: async () => ({ ledger: LEDGER_WITH_REFUSAL }) } as Response
    )));
    render(<CoverageStrip projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("coverage-strip")).toBeInTheDocument());

    const legend = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(legend).toContain("Never requested1");
    expect(legend).toContain("Prevented — the source did not allow it1");
    expect(legend).not.toContain("Never requested2");
    // And the mark itself says it, for the reader who never opens the legend.
    expect(
      screen.getByTitle("2026-07-22 — Prevented — the source did not allow it"),
    ).toBeInTheDocument();
  });

  it("re-asks the days that can improve, and the empty one is now among them", async () => {
    // CHANGED ON 2026-08-06, by Jean, and the old expectation is the reason this
    // line is here: `empty` used to be excluded « because re-asking a day the
    // provider already answered emptily burns quota and changes nothing ». The
    // fact was true, the prohibition was not its to give — a provider can have
    // filled its own hole since. `ok` stays out; nothing is re-asked twice.
    let sent: unknown = null;
    vi.stubGlobal("fetch", mockFetch((body) => { sent = body; }));
    render(<CoverageStrip projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("coverage-strip")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /Collect 4 days/ }));
    await userEvent.click(await screen.findByTestId("repull-go"));

    await waitFor(() => expect(sent).not.toBeNull());
    const dates = (sent as { dates: string[] }).dates;
    expect(dates).toEqual(["2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24"]);
    expect(dates).not.toContain("2026-07-20");
    expect(dates).not.toContain("2026-07-25");
  });

  it("confirms before it spends, at the same door and with the same words", async () => {
    // THE CLASS, NOT THE INSTANCE. This screen shipped the gesture first and had
    // no confirmation at all: a click posted to `/refetch` with no scope named
    // and no account. A dialog added to the day grid alone would have left this
    // door spending in silence.
    let sent: unknown = null;
    vi.stubGlobal("fetch", mockFetch((body) => { sent = body; }));
    render(
      <CoverageStrip
        projectId="proj_1"
        datastreamId="ds_EXAMPLE"
        connector="meta-ads"
        sourceAccountRef="sacct_EXAMPLE"
      />,
    );

    await waitFor(() => expect(screen.getByTestId("coverage-strip")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /Collect 4 days/ }));

    const dialog = await screen.findByTestId("repull-confirm");
    expect(dialog).toHaveTextContent("ds_EXAMPLE");
    expect(dialog).toHaveTextContent("meta-ads");
    expect(dialog).toHaveTextContent("sacct_EXAMPLE");
    // The same refusal to invent a cost as the other door.
    expect(dialog).toHaveTextContent("Not measured");
    expect(dialog).toHaveTextContent(/open\/closed breaker and no balance/);
    // Nothing is written until it is confirmed, and Cancel writes nothing.
    expect(sent).toBeNull();
    await userEvent.click(screen.getByTestId("repull-cancel"));
    await waitFor(() => expect(screen.queryByTestId("repull-confirm")).toBeNull());
    expect(sent).toBeNull();
  });

  it("posts to the project-scoped address, the only one left", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      urls.push(String(url));
      if (String(url).includes("/ledger")) {
        return { ok: true, status: 200, json: async () => ({ ledger: LEDGER }) } as Response;
      }
      void init;
      return { ok: true, status: 202, json: async () => ({ jobs: [{ state: "queued" }] }) } as Response;
    }));
    render(<CoverageStrip projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />);

    await waitFor(() => expect(screen.getByTestId("coverage-strip")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /Collect 4 days/ }));
    await userEvent.click(await screen.findByTestId("repull-go"));

    await waitFor(() =>
      expect(urls.some((url) => url.includes("/refetch"))).toBe(true));
    const write = urls.find((url) => url.includes("/refetch"))!;
    expect(write).toContain("/api/projects/proj_EXAMPLE/datastreams/ds_EXAMPLE/refetch");
  });

  it("draws an empty day and a never-requested day as two different shapes", async () => {
    // JEAN, 2026-08-06: *« l'écran doit le dire sans qu'on survole »*. The two
    // states share the neutral tone ON PURPOSE — a semantic colour would make
    // the strip scannable by hue and stop the word being read — so the shape is
    // what separates them, on the strip and on the grid alike.
    vi.stubGlobal("fetch", mockFetch());
    render(<CoverageStrip projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("coverage-strip")).toBeInTheDocument());

    const empty = screen.getByRole("button", { name: /2026-07-21/ }).className;
    const never = screen.getByRole("button", { name: /2026-07-22/ }).className;
    expect(empty).not.toBe(never);
    // One is filled, the other is an open outline — and NEITHER borrows a
    // semantic tone, which is what would re-merge them for anybody scanning.
    expect(empty).toMatch(/bg-surface-muted/);
    expect(never).toMatch(/dashed/);
    for (const mark of [empty, never]) {
      expect(mark).not.toMatch(/success|warning|error/);
    }
  });

  it("says a backfilled day is counted per window instead of showing nothing", async () => {
    // A NUMBER THAT VANISHES READS AS ZERO. Before story 58.1 this day carried
    // its window's whole total; the repair removes the wrong number, and on its
    // own it would leave the tooltip of every backfilled day silent -- which a
    // reader takes for "collected nothing". The reason travels to the tooltip.
    vi.stubGlobal("fetch", mockFetch());
    render(<CoverageStrip projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("coverage-strip")).toBeInTheDocument());

    const day = screen.getByRole("button", { name: /2026-07-25/ });
    expect(day).toHaveAttribute(
      "title",
      "2026-07-25 — Collected · rows counted per collection window, not per day",
    );
    // A day that DOES own its count still shows the number, unchanged.
    expect(screen.getByRole("button", { name: /2026-07-20/ })).toHaveAttribute(
      "title",
      "2026-07-20 — Collected · 120 rows",
    );
  });

  it("shows no coverage at all when the ledger cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 500 }) as Response));
    render(<CoverageStrip projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/would look like evidence/i),
    );
    expect(screen.queryByTestId("coverage-strip")).not.toBeInTheDocument();
  });
});
