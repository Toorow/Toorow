/**
 * Vitest tests for CacheHealthCard (Story 19.3).
 *
 * Tests:
 *   - "disabled" state: appropriate message rendered.
 *   - "no-cache" state: "absent" message rendered.
 *   - "stale" state: warning alert rendered (never "fresh").
 *   - "fresh" state: tables + row_counts + hit_rate + age displayed.
 *   - "Rebuild" button clicked -> confirmation dialog opened.
 *   - Rebuild confirmation -> POST /api/admin/cache/rebuild called.
 *   - Button disabled when cache_enabled=false.
 *
 * Pattern: mock global.fetch (vitest).
 *
 * AD-35 (AI-208): the MUI `ThemeProvider` wrapper is gone with the MUI theme it
 * carried. The subject renders no MUI component, so the wrapper provided nothing
 * -- while its `@mui/material` import made this whole suite uncollectable, which
 * reads as a passing suite because a suite that yields zero tests never fails.
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CacheHealthCard from "../cache/CacheHealthCard";
import { MAX_ATTEMPTS, retryDelayMs } from "../lib/polledRead";

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderCard() {
  return render(<CacheHealthCard refreshIntervalMs={0} />);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Mock fetch helpers
// ---------------------------------------------------------------------------

function mockStatus(status: object, rebuildResult?: object) {
  vi.spyOn(globalThis, "fetch").mockImplementation((url: RequestInfo | URL, opts?: RequestInit) => {
    const urlStr = String(url);
    if (urlStr.includes("/api/admin/cache/status")) {
      return Promise.resolve(
        new Response(JSON.stringify(status), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    if (urlStr.includes("/api/admin/cache/rebuild") && opts?.method === "POST") {
      const result = rebuildResult ?? { status: "ok", performed_by: "test-user" };
      return Promise.resolve(
        new Response(JSON.stringify(result), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    return Promise.resolve(new Response("Not found", { status: 404 }));
  });
}

// ---------------------------------------------------------------------------
// Data fixtures
// ---------------------------------------------------------------------------

const STATUS_FRESH = {
  cache_state: "fresh",
  cache_enabled: true,
  cache_built_at: "2026-07-19T01:00:00+00:00",
  age_seconds: 7200,
  min_date: "2026-06-19",
  max_date: "2026-07-19",
  tables: ["fact_daily_kpi", "semantic_ctr"],
  row_counts: { fact_daily_kpi: 500, semantic_ctr: 120 },
  project_ids: ["proj_alpha"],
  hit_rate: 0.85,
  stats: { hit: 40, "miss-relation": 5 },
  last_rebuild_cause: null,
};

const STATUS_STALE = {
  ...STATUS_FRESH,
  cache_state: "stale",
  hit_rate: null,
};

const STATUS_NO_CACHE = {
  cache_state: "no-cache",
  cache_enabled: true,
  cache_built_at: null,
  age_seconds: null,
  min_date: null,
  max_date: null,
  tables: [],
  row_counts: {},
  project_ids: [],
  hit_rate: null,
  stats: {},
  last_rebuild_cause: null,
};

const STATUS_DISABLED = {
  cache_state: "disabled",
  cache_enabled: false,
  cache_built_at: null,
  age_seconds: null,
  min_date: null,
  max_date: null,
  tables: [],
  row_counts: {},
  project_ids: [],
  hit_rate: null,
  stats: {},
  last_rebuild_cause: null,
};

// ---------------------------------------------------------------------------
// Tests : etats
// ---------------------------------------------------------------------------

describe("CacheHealthCard - states", () => {
  it("renders the 'disabled' state with the right message", async () => {
    mockStatus(STATUS_DISABLED);
    renderCard();
    await waitFor(() => {
      expect(screen.getByTestId("cache-state-disabled")).toBeInTheDocument();
    });
    expect(screen.getByTestId("cache-state-disabled").textContent).toMatch(
      /disabled/i
    );
    // Rebuild button disabled (cache_enabled=false).
    const btn = screen.getByTestId("cache-rebuild-button");
    expect(btn).toBeDisabled();
  });

  it("renders the 'no-cache' state with the 'absent' message", async () => {
    mockStatus(STATUS_NO_CACHE);
    renderCard();
    await waitFor(() => {
      expect(screen.getByTestId("cache-state-no-cache")).toBeInTheDocument();
    });
    expect(screen.getByTestId("cache-state-no-cache").textContent).toMatch(
      /no cache|absent/i
    );
  });

  it("renders a warning for the 'stale' state (invariant c -- never a misleading 'fresh')", async () => {
    mockStatus(STATUS_STALE);
    renderCard();
    await waitFor(() => {
      expect(screen.getByTestId("cache-state-stale")).toBeInTheDocument();
    });
    const alertText = screen.getByTestId("cache-state-stale").textContent ?? "";
    // The message must mention "stale" or "bypass".
    expect(alertText.toLowerCase()).toMatch(/stale|bypass/i);
    // The state must NOT claim the cache is fresh (invariant c).
    expect(alertText.toLowerCase()).not.toMatch(/fresh/);
  });

  it("renders the metrics for the 'fresh' state (AI-56: asserted values)", async () => {
    mockStatus(STATUS_FRESH);
    renderCard();

    await waitFor(() => {
      expect(screen.getByTestId("cache-age")).toBeInTheDocument();
    });

    // Fenetre
    const window = screen.getByTestId("cache-window");
    expect(window.textContent).toMatch(/2026-06-19/);
    expect(window.textContent).toMatch(/2026-07-19/);

    // Hit rate 85 %
    const hitRate = screen.getByTestId("cache-hit-rate");
    expect(hitRate.textContent).toMatch(/85/);

    // Projets
    const projects = screen.getByTestId("cache-projects");
    expect(projects.textContent).toMatch(/proj_alpha/);

    // Tables + row counts
    expect(screen.getByText("fact_daily_kpi")).toBeInTheDocument();
    expect(screen.getByText("semantic_ctr")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tests : the rate carries its population -- caveats-register.md, "Incomplete if"
// ---------------------------------------------------------------------------

describe("CacheHealthCard - the hit rate names its denominator", () => {
  it("draws the number of routed queries beside the rate", async () => {
    // 40 hits + 5 misses = 45 decisions, which is the exact denominator the
    // server divided by. Without it, "85.0 %" over 45 queries and over 45 000
    // are the same three characters on screen.
    mockStatus(STATUS_FRESH);
    renderCard();

    await waitFor(() => {
      expect(screen.getByTestId("cache-hit-rate")).toBeInTheDocument();
    });
    const cell = screen.getByTestId("cache-hit-rate").textContent ?? "";
    expect(cell).toMatch(/85/);
    expect(cell).toMatch(/45/);
    expect(cell).toMatch(/queries/i);
  });

  it("says no query has been routed rather than showing a bare dash", async () => {
    // `stats: {}` is the fresh process nobody has queried yet. A dash alone
    // reads as a broken figure; the sentence says which of the two it is.
    mockStatus(STATUS_NO_CACHE);
    renderCard();

    await waitFor(() => {
      expect(screen.getByTestId("cache-hit-rate")).toBeInTheDocument();
    });
    expect(screen.getByTestId("cache-hit-rate").textContent).toMatch(
      /No query has been routed/i
    );
  });

  it("says the routed queries went past the cache when there is no rate", async () => {
    // The reading that was invisible: a null rate over a NON-empty counter map
    // is not an absence of data, it is 45 queries that all bypassed the cache.
    mockStatus(STATUS_STALE);
    renderCard();

    await waitFor(() => {
      expect(screen.getByTestId("cache-hit-rate")).toBeInTheDocument();
    });
    const cell = screen.getByTestId("cache-hit-rate").textContent ?? "";
    expect(cell).toMatch(/45/);
    expect(cell).toMatch(/none of them through the cache/i);
  });
});

// ---------------------------------------------------------------------------
// Tests : freshness -- story 63.3, the class this card was the precedent for
// ---------------------------------------------------------------------------

describe("CacheHealthCard - freshness", () => {
  it("says when the figures on screen were measured", async () => {
    mockStatus(STATUS_FRESH);
    renderCard();

    await waitFor(() => {
      expect(screen.getByTestId("cache-measured-at")).toBeInTheDocument();
    });
    const line = screen.getByTestId("cache-measured-at").textContent ?? "";
    expect(line).toMatch(/^Measured at/);
    expect(line).toMatch(/\d/);
  });

  it("never presents the previous figures as current after a failed reload", async () => {
    // The defect this closes: the error banner was mounted ABOVE figures that
    // stayed on screen unchanged, with no age on them anywhere -- so a reader
    // could not tell a live reading from one taken before the failure.
    let statusCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((url: RequestInfo | URL) => {
      if (String(url).includes("/api/admin/cache/status")) {
        statusCalls += 1;
        if (statusCalls === 1) {
          return Promise.resolve(new Response(JSON.stringify(STATUS_FRESH), { status: 200 }));
        }
        return Promise.resolve(new Response("upstream is down", { status: 503 }));
      }
      return Promise.resolve(new Response("Not found", { status: 404 }));
    });

    render(<CacheHealthCard refreshIntervalMs={10} />);

    await waitFor(() => {
      expect(screen.getByTestId("cache-measured-at").textContent).toMatch(/^Measured at/);
    });

    await waitFor(() => {
      expect(screen.getByTestId("cache-measured-at").textContent).toMatch(/Not current/i);
    });

    // The figures are KEPT -- discarding them would lose the last true reading.
    // What changed is that they now carry the instant they were taken.
    expect(screen.getByTestId("cache-hit-rate").textContent).toMatch(/85/);
    expect(screen.getByTestId("cache-measured-at").textContent).toMatch(/last successful/i);
    expect(screen.getByTestId("cache-measured-at").textContent).toMatch(/\d/);
  });
});

// ---------------------------------------------------------------------------
// Tests : the poll stops -- story 63.3, same convention as the progress poll
// ---------------------------------------------------------------------------

/**
 * This card was the repository's ONLY network poll, and it never stopped: not
 * for a hidden tab, not on a `401`, not after any number of failures, on a
 * service that runs at `--min-instances=0`. Leaving it outside the convention
 * story 63.3 sets would give the next reader two answers to "how do we poll
 * here" -- so it uses the same `usePolledRead`, and these are the same
 * assertions its own suite makes.
 *
 * Plain objects rather than `new Response(...)`: under `vi.useFakeTimers()` a
 * real `Response.json()` can settle on a platform timer the fake clock now
 * owns.
 */
describe("CacheHealthCard - the poll stops", () => {
  const INTERVAL = 60_000;
  let statusCalls: number;
  let answer: () => Response | Promise<Response>;

  function json(body: unknown, status = 200): Response {
    return { ok: status < 400, status, json: () => Promise.resolve(body) } as unknown as Response;
  }

  async function flush(): Promise<void> {
    await act(async () => {
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
  }

  async function advance(ms: number): Promise<void> {
    await act(async () => {
      vi.advanceTimersByTime(ms);
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
  }

  function setVisibility(value: "visible" | "hidden"): void {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
  }

  async function switchVisibility(value: "visible" | "hidden"): Promise<void> {
    setVisibility(value);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
  }

  beforeEach(() => {
    vi.useFakeTimers();
    statusCalls = 0;
    answer = () => json(STATUS_FRESH);
    vi.spyOn(globalThis, "fetch").mockImplementation((url: RequestInfo | URL) => {
      if (String(url).includes("/api/admin/cache/status")) {
        statusCalls += 1;
        return Promise.resolve(answer());
      }
      return Promise.resolve(json({ code: "not_found" }, 404));
    });
  });

  afterEach(() => {
    setVisibility("visible");
    vi.useRealTimers();
  });

  it("does not poll a hidden tab, and reads exactly once on coming back", async () => {
    setVisibility("hidden");
    render(<CacheHealthCard refreshIntervalMs={INTERVAL} />);
    await flush();
    expect(statusCalls).toBe(0);

    await switchVisibility("visible");
    expect(statusCalls).toBe(1);

    await switchVisibility("hidden");
    await advance(INTERVAL * 10);
    expect(statusCalls).toBe(1);

    await switchVisibility("visible");
    expect(statusCalls).toBe(2);
  });

  it("keeps refreshing on its interval while the route answers", async () => {
    render(<CacheHealthCard refreshIntervalMs={INTERVAL} />);
    await flush();
    expect(statusCalls).toBe(1);

    await advance(INTERVAL - 1);
    expect(statusCalls).toBe(1);
    await advance(1);
    expect(statusCalls).toBe(2);
    await advance(INTERVAL);
    expect(statusCalls).toBe(3);
  });

  it("stops at once on a refusal, says it stopped, and can be restarted by hand", async () => {
    answer = () => json({ code: "unauthorized", message: "Authentication required" }, 401);
    render(<CacheHealthCard refreshIntervalMs={INTERVAL} />);
    await flush();

    expect(statusCalls).toBe(1);
    expect(screen.getByTestId("cache-error").textContent).toMatch(/Automatic refresh stopped/i);

    // Asking a refusal again cannot turn it into an answer.
    await advance(INTERVAL * 10);
    expect(statusCalls).toBe(1);

    // A person asking IS allowed to restart it -- and the control says so.
    answer = () => json(STATUS_FRESH);
    await act(async () => {
      fireEvent.click(screen.getByTestId("cache-retry-button"));
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
    expect(statusCalls).toBe(2);
    expect(screen.queryByTestId("cache-error")).not.toBeInTheDocument();
  });

  it("gives up after five failed attempts instead of hammering a dead route", async () => {
    answer = () => json("upstream is down", 503);
    render(<CacheHealthCard refreshIntervalMs={INTERVAL} />);
    await flush();
    expect(statusCalls).toBe(1);

    for (let attempt = 1; attempt < MAX_ATTEMPTS; attempt += 1) {
      await advance(retryDelayMs(attempt) - 1);
      expect(statusCalls).toBe(attempt);
      await advance(1);
      expect(statusCalls).toBe(attempt + 1);
    }

    expect(screen.getByTestId("cache-error").textContent).toMatch(
      new RegExp(`stopped after ${MAX_ATTEMPTS} attempts`, "i"),
    );
    await advance(INTERVAL * 100);
    expect(statusCalls).toBe(MAX_ATTEMPTS);
  });
});

// ---------------------------------------------------------------------------
// Tests : rebuild
// ---------------------------------------------------------------------------

describe("CacheHealthCard - rebuild", () => {
  it("clicking 'Rebuild' opens the confirmation dialog", async () => {
    mockStatus(STATUS_FRESH);
    renderCard();

    // THE CONDITION, NOT THE NODE. The button is in the first paint already,
    // disabled, because nothing has been read yet -- so waiting for it to
    // EXIST returns before the status lands and the assertion below passes or
    // fails on scheduling. It failed once under a full-suite run and passed on
    // replay, which is the worst way for a test to behave: the next session
    // spends half an hour on a red that is not theirs.
    await waitFor(() => expect(screen.getByTestId("cache-rebuild-button")).toBeEnabled());

    const btn = screen.getByTestId("cache-rebuild-button");
    await userEvent.click(btn);

    // The confirmation dialog must be visible.
    await waitFor(() => {
      expect(screen.getByText(/Rebuild the cache/i)).toBeInTheDocument();
    });
  });

  it("rebuild confirmation -> POST /api/admin/cache/rebuild called", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: RequestInfo | URL, opts?: RequestInit) => {
        const urlStr = String(url);
        if (urlStr.includes("/api/admin/cache/status")) {
          return Promise.resolve(
            new Response(JSON.stringify(STATUS_FRESH), { status: 200 })
          );
        }
        if (urlStr.includes("/api/admin/cache/rebuild") && opts?.method === "POST") {
          return Promise.resolve(
            new Response(
              JSON.stringify({ status: "ok", performed_by: "test-user" }),
              { status: 200 }
            )
          );
        }
        return Promise.resolve(new Response("Not found", { status: 404 }));
      }
    );

    renderCard();
    // Same reason as above: the gesture is only available once the first read
    // has landed, so that is what is waited for.
    await waitFor(() => expect(screen.getByTestId("cache-rebuild-button")).toBeEnabled());

    // Click "Rebuild"
    await userEvent.click(screen.getByTestId("cache-rebuild-button"));
    // Confirm in the dialog
    await waitFor(() => screen.getByTestId("cache-rebuild-confirm"));
    await userEvent.click(screen.getByTestId("cache-rebuild-confirm"));

    // Verify POST /api/admin/cache/rebuild was called.
    await waitFor(() => {
      const postCalls = fetchMock.mock.calls.filter(
        ([url, opts]) =>
          String(url).includes("/api/admin/cache/rebuild") && opts?.method === "POST"
      );
      expect(postCalls.length).toBeGreaterThan(0);
    });
  });

  /**
   * THE SERVER'S SENTENCE, NOT A STRINGIFIED OBJECT.
   *
   * The outcome used to be one `string`, built in the catch as
   * `Error: ${String(e)}` and read back with `.startsWith("Error")` to choose a
   * colour. Two defects in one value, and this closes both:
   *
   *   * `String(e)` on the `ApiError` `apiPost` throws yields
   *     `ApiError: <message>` — the class name in front of the only sentence on
   *     the screen that says what to do next;
   *   * the tone rested on a prefix this file wrote to itself, so a refusal
   *     whose message did not start with that word was drawn as a success.
   */
  it("shows the server's own refusal, with no class name in front of it", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: RequestInfo | URL, opts?: RequestInit) => {
        const urlStr = String(url);
        if (urlStr.includes("/api/admin/cache/status")) {
          return Promise.resolve(new Response(JSON.stringify(STATUS_FRESH), { status: 200 }));
        }
        if (urlStr.includes("/api/admin/cache/rebuild") && opts?.method === "POST") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                code: "warehouse_unreachable",
                message: "The origin warehouse refused the connection. Check the service account.",
              }),
              { status: 503, headers: { "Content-Type": "application/json" } }
            )
          );
        }
        return Promise.resolve(new Response("Not found", { status: 404 }));
      }
    );

    renderCard();
    await waitFor(() => expect(screen.getByTestId("cache-rebuild-button")).toBeEnabled());
    await userEvent.click(screen.getByTestId("cache-rebuild-button"));
    await waitFor(() => screen.getByTestId("cache-rebuild-confirm"));
    await userEvent.click(screen.getByTestId("cache-rebuild-confirm"));

    const outcome = await screen.findByTestId("cache-rebuild-outcome");
    expect(outcome).toHaveTextContent(
      "The origin warehouse refused the connection. Check the service account."
    );
    // The class name and the `[object ...]` shape never reach the screen.
    expect(outcome.textContent ?? "").not.toMatch(/ApiError|\[object/);
    // And the refusal is framed as one, without the sentence having to spell it.
    expect(outcome).toHaveTextContent(/refused/i);
  });

  it("says a rebuild succeeded without parsing its own sentence for a colour", async () => {
    mockStatus(STATUS_FRESH);
    renderCard();
    await waitFor(() => expect(screen.getByTestId("cache-rebuild-button")).toBeEnabled());
    await userEvent.click(screen.getByTestId("cache-rebuild-button"));
    await waitFor(() => screen.getByTestId("cache-rebuild-confirm"));
    await userEvent.click(screen.getByTestId("cache-rebuild-confirm"));

    const outcome = await screen.findByTestId("cache-rebuild-outcome");
    expect(outcome).toHaveTextContent(/rebuilt from the origin warehouse/i);
    expect(outcome.textContent ?? "").not.toMatch(/^Error/);
  });

  it("button disabled when cache_enabled=false", async () => {
    mockStatus(STATUS_DISABLED);
    renderCard();
    // The state marker, not the button: waiting for the node alone would let
    // this pass while the card was still LOADING -- disabled for a completely
    // different reason than the one under test.
    await waitFor(() => expect(screen.getByTestId("cache-state-disabled")).toBeInTheDocument());
    expect(screen.getByTestId("cache-rebuild-button")).toBeDisabled();
  });
});
