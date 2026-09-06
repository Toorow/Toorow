/**
 * The backfill panel, pinned on the three things that were actually broken.
 *
 * None of them is "does it render". The defect this repairs was never in a
 * component: `data_surface.py` never selected `app.connection_ref.id`, so the
 * screen could not address the connection, and every action the server offers on
 * one was unreachable. A test that mounts the panel with a hand-written id would
 * have passed on the broken tree — which is why the first assertion here is
 * about the ENVELOPE, not the panel.
 */
import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import SourceBackfillPanel, { daysRefusal, windowCount, resetBackfillAsks } from "../data/SourceBackfillPanel";

describe("the wire, which is where the defect lived", () => {
  it("the sources query selects the connection id it needs to address", () => {
    // Read as SQL, not as prose: the id must be selected, not merely joined.
    // The query has joined `app.connection_ref` since it was written and read
    // four of its columns without this one.
    const source = readFileSync(
      resolve(__dirname, "../../../../server/core/data_surface.py"),
      "utf-8",
    );
    const sources = source.slice(source.indexOf('"sources": """'));
    expect(sources.slice(0, sources.indexOf('"""', 14))).toContain("cr.id AS connection_ref_id");
  });

  it("and puts it on the item under the name the route uses", () => {
    const source = readFileSync(
      resolve(__dirname, "../../../../server/core/data_surface.py"),
      "utf-8",
    );
    expect(source).toContain('"connection_ref": {');
    expect(source).toContain('"id": str(row.get("connection_ref_id") or ""),');
  });
});

describe("the refusal, stated before any round trip", () => {
  it("refuses an empty ask", () => {
    expect(daysRefusal("")).toBe("State how many days of history to collect.");
  });

  it("refuses beyond the ceiling the server enforces", () => {
    // `account_topology.validate_backfill_days` rejects this with `invalid_days`.
    // Mirrored here so the person is told before spending a request, never
    // re-decided: the numbers must match.
    expect(daysRefusal("366")).toContain("366 days is beyond the 365-day ceiling");
    expect(daysRefusal("365")).toBeNull();
    expect(daysRefusal("0")).toContain("at least 1 day");
    expect(daysRefusal("1")).toBeNull();
  });

  it("refuses a value that is not a whole number of days", () => {
    expect(daysRefusal("7.5")).toBe("Days must be a whole number.");
  });

  it("forecasts the windows the server will cut, because that is the real consent", () => {
    // 365 days is twelve pulls against a provider, not one long one --
    // `execution-substrate.md:92` states the same arithmetic.
    expect(windowCount(365)).toBe(12);
    expect(windowCount(31)).toBe(1);
    expect(windowCount(32)).toBe(2);
  });
});

describe("the panel", () => {
  beforeEach(() => {
    resetBackfillAsks();
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    cleanup();
    resetBackfillAsks();
    vi.unstubAllGlobals();
  });

  it("pushes the route the router registers, with the id it was given", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({ windows: [{ date_from: "2026-01-01", date_to: "2026-01-31", job_id: "job_1", state: "queued" }] }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SourceBackfillPanel connectionId="conn_EXAMPLE" label="Analytics" />);
    fireEvent.change(screen.getByLabelText(/Days of history/i), { target: { value: "31" } });
    fireEvent.click(screen.getByRole("button", { name: /Request backfill/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/connections/conn_EXAMPLE/backfill");
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ days: 31 });
  });

  it("shows a deduplicated window instead of hiding it", async () => {
    // The answer to "why did my backfill do less than I asked" exists only in
    // this response. Summarising it away is the quiet loss this project keeps
    // finding.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({
        windows: [
          { date_from: "2026-01-01", date_to: "2026-01-31", job_id: "job_1", state: "queued", deduplicated: false },
          { date_from: "2026-02-01", date_to: "2026-02-28", job_id: "job_0", state: "queued", deduplicated: true },
        ],
      }),
    }));

    render(<SourceBackfillPanel connectionId="conn_EXAMPLE" label="Analytics" />);
    fireEvent.change(screen.getByLabelText(/Days of history/i), { target: { value: "60" } });
    fireEvent.click(screen.getByRole("button", { name: /Request backfill/i }));

    expect(await screen.findByText(/2 windows enqueued/i)).toBeTruthy();
    expect(screen.getByLabelText(/Backfill window 2/i)).toBeTruthy();
  });

  it("names a destination that exists, and no job list", async () => {
    // « Follow them in this authorization's jobs » sent the reader to a screen
    // this console does not have: a Source Account carries Overview, Accounts,
    // Health and Used by, and none of them is a job list. There is nowhere else
    // either — `enqueue_backfill` calls `enqueue_pull` with no `datastream_id`
    // and nothing sets that column afterwards, so neither the Runs tab nor the
    // day-by-day extract registry can list these windows. The banner therefore
    // names THIS list, and the one real tab that answers who the history is for.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({ windows: [{ date_from: "2026-01-01", date_to: "2026-01-31", job_id: "job_1", state: "queued" }] }),
    }));

    render(
      <SourceBackfillPanel
        connectionId="conn_EXAMPLE"
        label="Analytics"
        usedByHref="/org/org_EXAMPLE/project/proj_EXAMPLE/data/sources/object/source-account/sacct_1/tab/used-by"
      />,
    );
    fireEvent.change(screen.getByLabelText(/Days of history/i), { target: { value: "31" } });
    fireEvent.click(screen.getByRole("button", { name: /Request backfill/i }));

    expect(await screen.findByText(/1 window enqueued/i)).toBeTruthy();
    expect(screen.getByText(/this list is the record of what was asked/i)).toBeTruthy();
    expect(screen.queryByText(/authorization's jobs/i)).toBeNull();
    expect(screen.getByRole("link", { name: "Which Datastreams read this account" }))
      .toHaveAttribute("href", "/org/org_EXAMPLE/project/proj_EXAMPLE/data/sources/object/source-account/sacct_1/tab/used-by");
  });

  it("keeps the windows it already asked for when the panel is mounted again", async () => {
    // The panel is unmounted the instant the reader changes tab, and the list
    // lived in `useState`: walking to `Used by` and back showed an empty form
    // and no trace of the pulls just requested, which reads as "nothing
    // happened" on the one screen whose job is to say what was asked.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({
        windows: [
          { date_from: "2026-01-01", date_to: "2026-01-31", job_id: "job_1", state: "queued" },
          { date_from: "2026-02-01", date_to: "2026-02-28", job_id: "job_2", state: "queued" },
        ],
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const first = render(<SourceBackfillPanel connectionId="conn_EXAMPLE" label="Analytics" />);
    fireEvent.change(screen.getByLabelText(/Days of history/i), { target: { value: "60" } });
    fireEvent.click(screen.getByRole("button", { name: /Request backfill/i }));
    expect(await screen.findByText(/2 windows enqueued/i)).toBeTruthy();
    // The instant is carried, machine-readable, so the record says WHEN.
    const asked = document.querySelector("time")?.getAttribute("datetime");
    expect(asked).toBeTruthy();
    first.unmount();

    render(<SourceBackfillPanel connectionId="conn_EXAMPLE" label="Analytics" />);
    expect(await screen.findByText(/2 windows enqueued/i)).toBeTruthy();
    expect(screen.getByLabelText(/Backfill window 2/i)).toBeTruthy();
    expect(document.querySelector("time")?.getAttribute("datetime")).toBe(asked);
    // Nothing was asked again to show it: the cache is a record, not a re-read.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps that record to the connection it belongs to", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({ windows: [{ date_from: "2026-01-01", date_to: "2026-01-31", job_id: "job_1", state: "queued" }] }),
    }));

    const first = render(<SourceBackfillPanel connectionId="conn_ONE" label="Analytics" />);
    fireEvent.change(screen.getByLabelText(/Days of history/i), { target: { value: "31" } });
    fireEvent.click(screen.getByRole("button", { name: /Request backfill/i }));
    await screen.findByText(/1 window enqueued/i);
    first.unmount();

    // A different authorization has asked for nothing, and must not inherit the
    // answer given to another one.
    render(<SourceBackfillPanel connectionId="conn_TWO" label="Other" />);
    expect(screen.queryByText(/window enqueued/i)).toBeNull();
  });

  it("names the server's refusal rather than a generic failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({ code: "invalid_days", message: "days must be an integer in [1, 365]." }),
    }));

    render(<SourceBackfillPanel connectionId="conn_EXAMPLE" label="Analytics" />);
    fireEvent.change(screen.getByLabelText(/Days of history/i), { target: { value: "31" } });
    fireEvent.click(screen.getByRole("button", { name: /Request backfill/i }));

    expect(await screen.findByText(/days must be an integer/i)).toBeTruthy();
  });
});
