/**
 * The Connector installation seam — execution-substrate criterion 11.
 *
 * The routes have existed since story 38.2/38.4 and nothing in `ui/admin` called
 * them: `POST /api/connectors/{name}/installation` registers an installation and
 * `POST /api/connectors/{name}/verify` re-runs its check, so setting a Connector
 * up on a deployment meant issuing a REST call by hand. Production held exactly
 * one installation row as a result. This module is that seam, and
 * `ConnectorsCatalog` is the screen the ratified document names as owing the
 * gesture (`docs/product-architecture/execution-substrate.md`, AI-206 and
 * `Incomplete if` 11).
 *
 * Everything goes through `apiJson` — the ONE place the bearer is attached. A
 * bare `fetch("/api/…")` is the anomaly `apiSeamGuard.test.ts` refuses.
 *
 * THE READING IS HERE, NOT IN THE SCREEN. `installationReading` turns a stored
 * state into the sentence a person reads and the ONE gesture available next,
 * because the catalogue table and the setup panel must not be able to disagree
 * about what `DEGRADED` means — one word, one place, both readers.
 */
import { ApiError, apiJson } from "../../lib/apiFetch";
import { stateLabel, stateTone, type Tone } from "../../ui";

/** The six states of `app.connector_installations`. Stored words, never shown. */
export const INSTALLATION_STATES = [
  "NOT_INSTALLED",
  "DOMAIN_PENDING",
  "VERIFYING",
  "READY",
  "DEGRADED",
  "DISABLED",
] as const;
export type InstallationState = (typeof INSTALLATION_STATES)[number];

/**
 * `GET /api/connectors/{name}/installation`.
 *
 * `state` is absent from the response a NON platform-admin receives: that
 * projection is nondisclosing by contract (AD-5), and its absence is the only
 * honest signal the console has that this person cannot set Connectors up.
 */
export interface InstallationRead {
  connector_name: string;
  environment?: string;
  state?: string;
  catalog_availability?: string;
  catalog_status?: string;
  safe_next_action?: string;
  responsible_actor?: string | null;
  blocking_cause?: string | null;
  last_verified_at?: string | null;
}

/** `GET /api/connectors/{name}/verification`. Platform-admin only; 404 otherwise. */
export interface VerificationRead {
  connector_name: string;
  environment?: string;
  verified: boolean;
  installation_state?: string;
  last_outcome?: string | null;
  evidence_class?: string | null;
  first_seen_at?: string | null;
  last_run_at?: string | null;
  blocking_reason?: string | null;
  synthetic_delivery?: boolean;
  /** The re-check interval the evidence row carries. The expiry is derived below. */
  ttl_seconds?: number | null;
}

/** One row of `GET /api/connectors/available?project_id=…`. */
export interface AvailableConnector {
  connector_name: string;
  display_name: string;
}

function path(connectorName: string, suffix: string): string {
  const name = connectorName.trim();
  if (!name) throw new Error("Connector identity is unavailable");
  return `/api/connectors/${encodeURIComponent(name)}/${suffix}`;
}

/** A per-attempt key, so a double click replays instead of registering twice. */
export function newIdempotencyKey(prefix: string): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${random}`;
}

function mutationInit(key: string, body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": key },
    body: JSON.stringify(body),
  };
}

export function readInstallation(
  connectorName: string,
  signal?: AbortSignal,
): Promise<InstallationRead> {
  return apiJson<InstallationRead>(path(connectorName, "installation"), {
    method: "GET",
    cache: "no-store",
    signal,
  });
}

export function readVerification(
  connectorName: string,
  signal?: AbortSignal,
): Promise<VerificationRead> {
  return apiJson<VerificationRead>(path(connectorName, "verification"), {
    method: "GET",
    cache: "no-store",
    signal,
  });
}

/** Register the installation: `NOT_INSTALLED` -> its family's first step. */
export function registerInstallation(connectorName: string): Promise<InstallationRead> {
  return apiJson<InstallationRead>(
    path(connectorName, "installation"),
    mutationInit(newIdempotencyKey("connector-install"), {}),
  );
}

/**
 * Run the check again. `READY` is not the end of verification (AI-206): the
 * evidence carries a TTL, and this is the only gesture that can refresh it.
 */
export function verifyInstallation(connectorName: string): Promise<VerificationRead> {
  return apiJson<VerificationRead>(
    path(connectorName, "verify"),
    mutationInit(newIdempotencyKey("connector-verify"), {}),
  );
}

export async function listAvailableConnectors(
  projectId: string,
  signal?: AbortSignal,
): Promise<AvailableConnector[]> {
  const rows = await apiJson<AvailableConnector[]>(
    `/api/connectors/available?project_id=${encodeURIComponent(projectId)}`,
    { method: "GET", cache: "no-store", signal },
  );
  return Array.isArray(rows) ? rows : [];
}

// ---------------------------------------------------------------------------
// Evidence age — derived, never invented.
// ---------------------------------------------------------------------------

export type EvidenceAge =
  | { kind: "none" }
  | { kind: "unbounded"; checkedAt: string }
  | { kind: "fresh"; checkedAt: string; expiresAt: string }
  | { kind: "expired"; checkedAt: string; expiresAt: string };

/**
 * How old the evidence is, and whether the interval it is checked on has run out.
 *
 * `ttl_seconds` comes from the evidence row (migration 085) and the console is
 * its first reader. Without it the screen could only say "checked at X" and
 * would have had to invent an interval to call anything overdue — which is the
 * fabricated state this lot forbids. `unbounded` is what an evidence row with no
 * interval honestly reads as: checked, and nothing says when again.
 */
export function evidenceAge(
  verification: VerificationRead | null | undefined,
  now: number,
): EvidenceAge {
  const checkedAt = verification?.last_run_at;
  if (!verification?.verified || !checkedAt) return { kind: "none" };
  const checked = Date.parse(checkedAt);
  if (Number.isNaN(checked)) return { kind: "none" };
  const ttl = verification.ttl_seconds;
  if (typeof ttl !== "number" || !Number.isFinite(ttl) || ttl <= 0) {
    return { kind: "unbounded", checkedAt };
  }
  const expires = new Date(checked + ttl * 1000).toISOString();
  return {
    kind: now > checked + ttl * 1000 ? "expired" : "fresh",
    checkedAt,
    expiresAt: expires,
  };
}

// ---------------------------------------------------------------------------
// The reading: one state, one sentence, one gesture.
// ---------------------------------------------------------------------------

export type GestureKind = "install" | "verify";

export interface InstallationReading {
  /** The state as a person says it. Never the stored word. */
  label: string;
  tone: Tone;
  /** What is true right now, in one sentence. */
  sentence: string;
  /** The ONE gesture available on this screen, or null when there is none. */
  gesture: { kind: GestureKind; label: string } | null;
  /**
   * The step that IS next when this screen cannot carry it. Named precisely,
   * because a screen that says "blocked" and not "blocked ON WHAT" is a dead end.
   */
  elsewhere: string | null;
}

/**
 * Why a blocking cause is spelled out here rather than printed.
 *
 * `blocking_cause` is a closed set of seven codes
 * (`core/connector_installation.BLOCKING_CAUSE_CODES`) and every one of them is
 * a database word. A code this map has not read is still shown — never invented,
 * never dressed up as a verdict — but it is shown as the code it is.
 */
const BLOCKING_CAUSE_SENTENCE: Readonly<Record<string, string>> = {
  connector_disabled: "it was turned off on this deployment",
  dependency_unavailable: "something it depends on is not answering",
  domain_configuration_pending: "its delivery domain has not been configured yet",
  domain_route_misconfigured: "its delivery domain does not route here",
  installation_not_applied: "it has not been set up on this deployment",
  synthetic_verification_failed: "its last test delivery did not arrive",
  verification_failed: "its last check did not pass",
};

/** The cause, as a person reads it. Unknown codes are shown, not swallowed. */
export function blockingCauseSentence(cause: string | null | undefined): string | null {
  const code = (cause ?? "").trim();
  if (!code) return null;
  return BLOCKING_CAUSE_SENTENCE[code] ?? `the platform reported "${code}"`;
}

const NOT_ALLOWED: InstallationReading = {
  label: "Set up by a platform administrator",
  tone: "neutral",
  sentence:
    "Only a platform administrator can set a Connector up on this deployment, so this screen cannot tell you where this one stands.",
  gesture: null,
  elsewhere: "Ask a platform administrator to set this Connector up.",
};

/**
 * The whole vocabulary of this screen, in one function.
 *
 * `installation` may be the restricted projection (no `state`), which is not a
 * missing answer — it is the nondisclosing one, and it reads differently from
 * every other case.
 */
export function installationReading(
  installation: InstallationRead | null | undefined,
  verification: VerificationRead | null | undefined,
  now: number,
): InstallationReading {
  if (!installation) {
    return {
      label: "Unavailable",
      tone: "neutral",
      sentence: "This Connector's setup could not be read.",
      gesture: null,
      elsewhere: "Reload this screen.",
    };
  }
  const state = (installation.state ?? "").trim().toUpperCase();
  if (!state) return NOT_ALLOWED;
  const cause = blockingCauseSentence(installation.blocking_cause);

  switch (state) {
    case "NOT_INSTALLED":
      return {
        label: "Not set up",
        tone: "neutral",
        sentence:
          "This Connector is not set up on this deployment. Nothing can read from it until it is.",
        gesture: { kind: "install", label: "Set up this Connector" },
        elsewhere: null,
      };
    case "DOMAIN_PENDING":
      // The one family that owes a domain step (AI-206): an inbound transport.
      // Naming it is all this screen can do — the console holds no surface that
      // writes `app.connector_domain_configs` yet.
      return {
        label: "Waiting for its delivery address",
        tone: "warning",
        sentence:
          "This Connector receives files at an address of yours, and that address has not been set up. It cannot be checked until it is.",
        gesture: null,
        elsewhere:
          "Configure this Connector's delivery domain, then come back and verify it.",
      };
    case "VERIFYING":
      return {
        label: "Never checked",
        tone: "warning",
        sentence:
          "This Connector is set up and has never been checked. It becomes available to Projects the first time the check passes.",
        gesture: { kind: "verify", label: "Verify now" },
        elsewhere: null,
      };
    case "READY": {
      const age = evidenceAge(verification, now);
      if (age.kind === "expired") {
        return {
          label: "Available, check overdue",
          tone: "warning",
          sentence:
            "This Connector is available to Projects, but its last check is older than the interval it is checked on. Nothing has confirmed it since.",
          gesture: { kind: "verify", label: "Verify again" },
          elsewhere: null,
        };
      }
      if (age.kind === "none") {
        return {
          label: "Available, never checked since",
          tone: "warning",
          sentence:
            "This Connector is available to Projects and this deployment holds no check for it.",
          gesture: { kind: "verify", label: "Verify now" },
          elsewhere: null,
        };
      }
      return {
        label: "Available",
        tone: "success",
        sentence:
          "This Connector is available to Projects and its last check passed. Nothing is required of you.",
        gesture: { kind: "verify", label: "Verify again" },
        elsewhere: null,
      };
    }
    case "DEGRADED":
      return {
        label: "Not available — its last check failed",
        tone: "error",
        sentence: cause
          ? `Projects cannot use this Connector because ${cause}. Repair that, then verify it again.`
          : "Projects cannot use this Connector: its last check did not pass. Verify it again once whatever it depends on is back.",
        gesture: { kind: "verify", label: "Verify again" },
        elsewhere: null,
      };
    case "DISABLED":
      return {
        label: "Turned off",
        tone: "neutral",
        sentence:
          "This Connector was turned off on this deployment and no gesture here can bring it back.",
        gesture: null,
        elsewhere: "Ask platform support to turn this Connector back on.",
      };
    default:
      // A state this console has not read. Shown as it came, never coloured as
      // a verdict and never given a gesture that might not apply.
      return {
        label: state,
        tone: "neutral",
        sentence: `The platform reports this Connector as "${state}", which this screen does not know how to read.`,
        gesture: null,
        elsewhere: "Ask a platform administrator what this state means.",
      };
  }
}

// ---------------------------------------------------------------------------
// Refusals — mapped to the gesture that repairs them.
// ---------------------------------------------------------------------------

/**
 * What a refused install/verify tells the person to DO.
 *
 * `installation_unavailable` is the one code that means two different things
 * server-side — no installation row, or no domain config — so the state the
 * screen already holds decides which, rather than the console guessing from a
 * message that is deliberately identical for both.
 */
export function refusalSentence(error: unknown, state: string | null | undefined): string {
  const known = (state ?? "").trim().toUpperCase();
  if (!(error instanceof ApiError)) {
    return "The console could not complete that. Reload this screen and try again.";
  }
  if (error.status === 0) {
    return "The console could not reach the platform. Check your connection, then try again.";
  }
  if (error.status === 401 || error.status === 403) {
    return "Your session is no longer valid. Sign in again, then repeat this.";
  }
  if (error.status === 404) {
    return "Setting a Connector up is a platform administrator's gesture. Ask one to do it for you.";
  }
  if (error.code === "installation_unavailable") {
    if (known === "DOMAIN_PENDING") {
      return "This Connector cannot be checked until its delivery domain is configured. Configure it, then verify again.";
    }
    return "This Connector is not set up on this deployment yet. Set it up first, then verify it.";
  }
  if (error.code === "conflict") {
    return "That request was already sent with different details. Reload this screen, then try again.";
  }
  if (error.status === 422) {
    return "The platform refused those details. Reload this screen, then try again.";
  }
  if (error.status >= 500) {
    return "The platform could not complete that. Try again in a moment; if it keeps failing, ask a platform administrator to look at the deployment.";
  }
  return "The platform refused that. Reload this screen, then try again.";
}

// ---------------------------------------------------------------------------
// The same vocabulary, for a row that carries no evidence.
// ---------------------------------------------------------------------------

/**
 * The state word for a TABLE cell, which knows the installation and not its
 * evidence.
 *
 * Deliberately separate from `installationReading`: that function is allowed to
 * say "check overdue" because the panel has read the evidence row, and a table
 * row has not. A cell that borrowed the panel's sentence would be claiming a
 * measurement nobody took — the exact fabrication this screen must not make.
 */
export function installationStateReading(
  state: string | null | undefined,
): { label: string; tone: Tone } {
  // THE SWITCH IS GONE (76-2), AND WITH IT TWO DISAGREEMENTS IT NEVER DECLARED.
  //
  // `DEGRADED` was drawn RED here and amber everywhere else in the console, and
  // its sentence was `Not available` -- a second reading of a word `stale`,
  // `partial` and `blocked` share: something still works and it is not what was
  // promised. `NOT_INSTALLED` was grey, which said "nothing to see" about the
  // one row a person has to act on.
  //
  // Six of the seven readings are the union's now, sentences included: the two
  // this file spelled better than any fallback could -- `Waiting for its
  // delivery address`, `Never checked` -- are declared in `STATE_LABEL`, so the
  // words travel with their tone instead of living beside it.
  const word = (state ?? "").trim().toLowerCase();
  return { label: stateLabel(word), tone: stateTone(word) };
}

/**
 * The one gesture a row offers, named without reading its evidence.
 *
 * `null` means this row has no next step here — `DISABLED` is terminal, and a
 * state this console cannot read gets no button rather than a guessed one.
 */
export function nextStepLabel(state: string | null | undefined): string | null {
  switch ((state ?? "").trim().toUpperCase()) {
    case "NOT_INSTALLED": return "Set up";
    case "DOMAIN_PENDING": return "Review setup";
    case "VERIFYING": return "Verify";
    case "READY": return "Verify again";
    case "DEGRADED": return "Verify again";
    default: return null;
  }
}
