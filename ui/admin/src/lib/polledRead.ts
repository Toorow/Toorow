/**
 * How this console polls -- ONE answer for every screen that polls, and ONE
 * poll per address -- stories 63.3 and 63.5.
 *
 * WHY THIS IS SHARED AND NOT COPIED. Before 63.3 the repository had exactly one
 * network poll (`CacheHealthCard`, `setInterval(load, 60_000)`) and it never
 * stopped: not for a hidden tab, not after any number of failures, not on a
 * `401`. Adding a second poll with its own rules would have left the next
 * person two answers to "how do we poll here", which is how a convention dies
 * the day it is written. Both callers use this module; there are only two.
 *
 * WHY THERE IS A REGISTRY, AND WHY IT IS THE POINT OF 63.5. Until 2026-08-06
 * every state of this hook lived inside its own `useEffect`: the timer, the
 * backoff counter, the quiet-cadence counter and the instant of measurement
 * were per MOUNT. Two surfaces watching the same run -- the Workbench object
 * header and the `Runs` tab, which render from the same component and are
 * therefore co-mounted every time -- opened two timers on one address, each
 * with its own slow-down counter. One of them reaches the quiet cadence while
 * the other, mounted later, is still at the live one, so the two show numbers
 * measured up to fifteen seconds apart. Both are exact, they disagree, and they
 * sit side by side on one screen: "two surfaces, two answers", which story 53.9
 * refuses. Doubling the wake-ups of a service at `--min-instances=0` is the
 * second cost of the same defect.
 *
 * So the machinery is keyed by the ADDRESS, not by the mount: one timer, one
 * backoff, one quiet counter, one `measuredAt`, N consumers. The entry is born
 * with its first subscriber and destroyed with its last, so nothing survives a
 * screen being closed. A late subscriber joins the reading already in flight
 * and is handed the current snapshot -- it never opens a second read of an
 * address that is already being read.
 *
 * THE CADENCE OF AN ADDRESS BELONGS TO THE ADDRESS. The options are those of
 * the entry's FIRST living subscriber; when it leaves, the next one's are used.
 * Two mounts of one address configuring two cadences would be the divergence
 * this registry exists to close, expressed one level up.
 *
 * THE RULES IT ENFORCES, AND WHAT EACH ONE COSTS OTHERWISE:
 *
 *  - ONE `setTimeout`, ARMED AFTER EACH ANSWER -- never `setInterval`. A
 *    repeating interval accumulates ticks behind a suspended event loop, so a
 *    laptop that sleeps two hours wakes up owing every tick it slept through.
 *    This owes exactly one read.
 *  - A HIDDEN TAB IS NOT POLLED, and coming back reads ONCE, immediately. At
 *    `--min-instances=0` (`infra/scripts/deploy.sh:104`) with no connection pool
 *    (`server/core/db.py:4-5`), every tick wakes an instance and opens a
 *    connection: a tab left open overnight at five seconds is 17_280 wake-ups
 *    for a screen nobody is looking at.
 *  - A REFUSAL IS NOT RETRIED. `401`, `403` and `404` stop at once -- asking
 *    again cannot turn a refusal into an answer.
 *  - A FAILURE IS RETRIED FIVE TIMES, backing off exponentially, and then the
 *    poll stops. A screen behind a dead route must not hammer it forever.
 *  - THE VALUE TRAVELS WITH THE INSTANT IT WAS MEASURED. `measuredAt` is part
 *    of the contract, not a courtesy: figures left on screen after a failed
 *    reload with no age on them are a lie a reader cannot detect, which is
 *    precisely what `CacheHealthCard` did until this story.
 *
 * WHAT IT DELIBERATELY DOES NOT DO: decide what a value MEANS. Whether an
 * answer means "keep watching" is the caller's question and the caller's
 * registry -- this module never reads a domain state.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./apiFetch";

/** First backoff step; each further consecutive failure doubles it. */
export const RETRY_BASE_MS = 5_000;
/** After this many consecutive failures the poll stops until a person asks. */
export const MAX_ATTEMPTS = 5;
/** Statuses that stop the poll at once: asking again cannot change the answer. */
export const STOP_STATUSES: readonly number[] = [401, 403, 404];

/** The delay before attempt `attempts + 1`, given `attempts` failures so far. */
export function retryDelayMs(attempts: number): number {
  return RETRY_BASE_MS * 2 ** (attempts - 1);
}

/** Is the tab this poll lives in currently out of sight? */
export function tabIsHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

/** Where the poll itself is -- never confused with what it is watching. */
export type PollPhase = "idle" | "polling" | "stopped" | "error";

export interface PollFailure {
  /** HTTP status, or `0` when the network never answered at all. */
  status: number;
  message: string;
  /** True when the network refused to carry the request (offline, DNS, reset). */
  offline: boolean;
}

export interface PolledRead<T> {
  phase: PollPhase;
  /** The value of the last SUCCESSFUL read, kept across failures. */
  value: T | null;
  /**
   * When that read landed, in epoch milliseconds -- null while none has. A
   * caller that renders `value` without rendering this is presenting an old
   * number as a current one.
   */
  measuredAt: number | null;
  error: PollFailure | null;
  /** Consecutive failures so far. */
  attempts: number;
  /** True once nothing more will be read without a person asking. */
  stopped: boolean;
  /** Read now and resume -- the one way out of a stopped poll. */
  refresh: () => void;
}

export interface PolledReadOptions<T> {
  /**
   * What is being watched -- and the identity of the shared poll. Every mount
   * that passes the same key reads the same timer and the same answer; a change
   * moves this mount to another poll, and `null` means the caller's scope is not
   * resolved yet and nothing is read at all.
   */
  key: string | null;
  read: (signal: AbortSignal) => Promise<T>;
  /** Cadence while the answer says there is still something to watch. */
  intervalMs: number;
  /** Does this answer mean "keep watching"? Default: yes, indefinitely. */
  continues?: (value: T) => boolean;
  /** Optional slower cadence once the answer stops changing. */
  quiet?: {
    afterTicks: number;
    intervalMs: number;
    signature: (value: T) => string;
  };
  /** A failure that must stop at once whatever its status (a broken contract). */
  fatal?: (reason: unknown) => boolean;
}

/** Everything a subscriber renders. The `refresh` is added by the hook. */
type Snapshot<T> = Omit<PolledRead<T>, "refresh">;

function initial<T>(): Snapshot<T> {
  return { phase: "idle", value: null, measuredAt: null, error: null, attempts: 0, stopped: false };
}

interface Subscriber<T> {
  options: { current: PolledReadOptions<T> };
  notify: (snapshot: Snapshot<T>) => void;
}

interface Entry<T> {
  subscribers: Subscriber<T>[];
  snapshot: Snapshot<T>;
  /** The first read, armed once the first subscriber is registered. */
  start: () => void;
  refresh: () => void;
  teardown: () => void;
}

/**
 * One entry per address, for the whole console.
 *
 * Module-level state is exactly what makes two mounts share one reading, and it
 * is bounded by construction: an entry exists only while at least one component
 * subscribes to it, and its teardown clears the timer, aborts the read in
 * flight and removes the entry. Nothing is cached across a screen being closed
 * -- a poll that outlived its screen would answer a later mount with figures
 * measured before it existed.
 */
const ENTRIES = new Map<string, Entry<never>>();

function openEntry<T>(key: string): Entry<T> {
  const entry: Entry<T> = {
    subscribers: [],
    snapshot: initial<T>(),
    start: () => undefined,
    refresh: () => undefined,
    teardown: () => undefined,
  };

  let disposed = false;
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let controller: AbortController | null = null;
  let signature: string | null = null;
  let repeats = 0;
  let attempts = 0;
  let inFlight = false;

  // The options of the first LIVING subscriber. Every subscriber of one address
  // reads the same thing the same way, so this is a choice of instance, not of
  // behaviour -- and taking the first means a mount that leaves does not change
  // the cadence for the ones that stay.
  const options = (): PolledReadOptions<T> | null =>
    entry.subscribers.length > 0 ? entry.subscribers[0].options.current : null;

  const publish = (next: Snapshot<T>) => {
    entry.snapshot = next;
    for (const subscriber of [...entry.subscribers]) subscriber.notify(next);
  };

  const clearTimer = () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };

  const schedule = (delay: number) => {
    clearTimer();
    if (disposed || stopped || tabIsHidden()) return;
    timer = setTimeout(() => {
      timer = null;
      void tick();
    }, delay);
  };

  const stop = () => {
    stopped = true;
    clearTimer();
  };

  const tick = async () => {
    if (disposed || stopped || inFlight) return;
    const current = options();
    if (!current) return;
    inFlight = true;
    const active = new AbortController();
    controller = active;
    if (entry.snapshot.phase === "idle") publish({ ...entry.snapshot, phase: "polling" });
    try {
      const value = await current.read(active.signal);
      if (disposed) return;
      attempts = 0;
      const measuredAt = Date.now();
      const goesOn = current.continues?.(value) ?? true;
      if (!goesOn) {
        // The value is KEPT: it is the last true thing that was measured, and
        // it is final rather than stale.
        publish({ phase: "stopped", value, measuredAt, error: null, attempts: 0, stopped: true });
        stop();
        return;
      }
      const quiet = current.quiet;
      let delay = current.intervalMs;
      if (quiet) {
        const signed = quiet.signature(value);
        if (signed === signature) repeats += 1;
        else {
          repeats = 0;
          signature = signed;
        }
        if (repeats >= quiet.afterTicks) delay = quiet.intervalMs;
      }
      publish({ phase: "polling", value, measuredAt, error: null, attempts: 0, stopped: false });
      schedule(delay);
    } catch (reason) {
      // An abort is this module's own doing -- a teardown, or a key change.
      // Reporting it as a failure would put an error on a screen that is gone.
      if (disposed || active.signal.aborted) return;
      const fatal = current.fatal?.(reason) === true;
      const status = reason instanceof ApiError ? reason.status : 0;
      const message = reason instanceof Error ? reason.message : "The read failed.";
      attempts += 1;
      const giveUp = fatal || STOP_STATUSES.includes(status) || attempts >= MAX_ATTEMPTS;
      // The last successful value and its age are kept, and the phase says
      // `error`: a caller that renders them is required to render their age
      // too, so a stale number is never dressed up as a current one.
      publish({
        ...entry.snapshot,
        phase: "error",
        error: { status, message, offline: status === 0 && !fatal },
        attempts,
        stopped: giveUp,
      });
      if (giveUp) {
        stop();
        return;
      }
      schedule(retryDelayMs(attempts));
    } finally {
      inFlight = false;
    }
  };

  // The convention story 63.3 sets, because nothing in `ui/admin/src` had one:
  // a hidden tab is not polled, and coming back reads ONCE -- a person who
  // returns wants the current state, not the ticks they missed. One listener
  // per address, whatever the number of surfaces watching it.
  const onVisibilityChange = () => {
    if (disposed || stopped) return;
    if (tabIsHidden()) clearTimer();
    else void tick();
  };
  document.addEventListener("visibilitychange", onVisibilityChange);

  // A person asking is not a poll: it resumes a poll that gave up, and it is
  // the only thing that does. Every surface watching this address is resumed
  // together, because they are one reading.
  entry.refresh = () => {
    if (disposed) return;
    stopped = false;
    attempts = 0;
    repeats = 0;
    signature = null;
    clearTimer();
    void tick();
  };

  // Armed by the hook once the first subscriber is in place: `read` belongs to
  // a subscriber, so an entry with none has nothing to read with.
  entry.start = () => {
    if (!disposed && !tabIsHidden()) void tick();
  };

  entry.teardown = () => {
    disposed = true;
    clearTimer();
    controller?.abort();
    document.removeEventListener("visibilitychange", onVisibilityChange);
    if (ENTRIES.get(key) === (entry as unknown as Entry<never>)) ENTRIES.delete(key);
  };

  ENTRIES.set(key, entry as unknown as Entry<never>);
  return entry;
}

/**
 * Watch one address, sharing the reading with every other mount of it.
 *
 * Pass `key: null` (a screen whose scope has not resolved) and nothing is read
 * at all: an unscoped request cannot be routed to an answer.
 */
export function usePolledRead<T>(options: PolledReadOptions<T>): PolledRead<T> {
  // The options are read through a ref so an inline `read` closure -- which is
  // a new function on every render -- does not restart the poll. What restarts
  // it is the KEY, which is the thing that actually changed.
  const latest = useRef(options);
  latest.current = options;

  const [state, setState] = useState<Snapshot<T>>(initial<T>);
  const entryRef = useRef<Entry<T> | null>(null);

  const { key, intervalMs } = options;

  useEffect(() => {
    if (key === null) {
      entryRef.current = null;
      setState(initial<T>());
      return;
    }

    let live = true;
    const subscriber: Subscriber<T> = {
      options: latest,
      notify: (snapshot) => {
        if (live) setState(snapshot);
      },
    };

    // Joining an address already being watched costs NO read: the reading in
    // flight is the same reading, and its current snapshot is handed over as it
    // stands -- including the instant it was measured, so a surface mounted
    // late says how old its first number is instead of implying it is fresh.
    const existing = ENTRIES.get(key) as Entry<T> | undefined;
    const entry = existing ?? openEntry<T>(key);
    entry.subscribers.push(subscriber);
    entryRef.current = entry;
    setState(entry.snapshot);
    if (!existing) entry.start();

    return () => {
      live = false;
      entryRef.current = null;
      const index = entry.subscribers.indexOf(subscriber);
      if (index >= 0) entry.subscribers.splice(index, 1);
      // The last surface to leave takes the poll with it. A timer that outlives
      // every screen watching it is the overnight wake-up this module exists to
      // prevent.
      if (entry.subscribers.length === 0) entry.teardown();
    };
    // `intervalMs` restarts this subscription because it is part of how the
    // address is read; the key alone decides WHICH poll is joined.
  }, [key, intervalMs]);

  const refresh = useCallback(() => {
    entryRef.current?.refresh();
  }, []);

  return { ...state, refresh };
}
