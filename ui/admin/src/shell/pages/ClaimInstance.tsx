import { type FormEvent, useEffect, useRef, useState } from "react";
import AuthGate from "../AuthGate";
import { apiFetch } from "../../lib/apiFetch";
import { Alert, Button, Input, Status } from "../../ui";
import {
  ReportingCurrencyField,
  ReportingTimezoneField,
  currencySummary,
  reportingPayload,
  timezoneSummary,
  useReportingDefaults,
} from "./reportingDefaults";
import "../application.css";

/* ---- The focused-dialog surface — CreateOrg's recipe, verbatim. ----------
   Styling: migrated off `create-org.css` (waves 2-4 of `docs/ui-css-strategy.md`).
   Ported 1:1 from the retired sheet; geometry (not prose) keeps its px values,
   prose measures stay in `ch`. The scrim dims with the scheme-stable
   `surface-dark` token so it reads as dimming on both light and dark. The two
   sign-up doors must stay one family — change these with CreateOrg's. */
const STAGE = "relative min-h-[828px]";
const SCRIM =
  "absolute inset-0 z-[var(--layer-modal)] grid items-start justify-items-center gap-4.5 bg-surface-dark/20 p-7";
const DIALOG =
  "w-[min(640px,calc(100%-56px))] overflow-hidden rounded-[18px] border border-divider-base bg-surface-light shadow-overlay max-[1180px]:w-[calc(100%-36px)]";
const HEADER = "flex items-start justify-between gap-4.5 border-b border-divider-base px-7 pb-5 pt-6";
const TITLE = "m-0 font-display text-[22px] font-semibold tracking-[-0.01em]";
const SUBTITLE = "m-0 mt-2 max-w-[44ch] text-label leading-normal text-text-secondary";
const BODY = "grid gap-5.5 px-7 py-6";
const FOOTER =
  "flex items-center justify-between gap-4.5 border-t border-divider-base px-7 py-4.5 max-[1180px]:flex-wrap";
const FOOTER_NOTE = "text-caption leading-snug text-text-secondary";
const ACTIONS = "flex flex-none items-center gap-2.5";

/* Field group: label / control — same recipe as CreateOrg; change them together. */
const FIELD = "grid gap-2";
const FIELD_LABEL = "text-label font-semibold";

/* ScopeSummary (DESIGN.md: page surface, read-only). The first row after the
   title takes SCOPE_ROW; every following row takes SCOPE_ROW_RULED. */
const SCOPE_SUMMARY = "rounded-lg border border-divider-base bg-background-light px-4.5 py-4";
const SCOPE_TITLE = "mb-3 font-display text-label font-semibold";
const SCOPE_ROW =
  "grid grid-cols-[128px_minmax(0,1fr)] items-baseline gap-3.5 py-[7px] max-[1180px]:grid-cols-1 max-[1180px]:gap-[3px]";
const SCOPE_ROW_RULED = `${SCOPE_ROW} border-t border-divider-base`;
const SCOPE_KEY = "text-caption font-semibold text-text-secondary";
const SCOPE_VAL = "text-label leading-normal [overflow-wrap:anywhere]";
const SCOPE_VAL_MONO = `${SCOPE_VAL} font-mono`;

type ExchangeState = "exchanging" | "ready" | "unavailable" | "error";

interface ClaimResponse {
  next_url: string;
}

interface EntryConfirmationResponse {
  confirmation_id: string;
  confirmation_secret: string;
}
let bootstrapFragmentCapture: { locationKey: string; bearer: string } | null = null;


function readBootstrapFragment(): string {
  if (typeof window === "undefined") return "";
  const locationKey = window.location.pathname + window.location.search;
  const raw = window.location.hash;
  let bearer = "";
  try {
    bearer = raw.startsWith("#bootstrap=")
      ? decodeURIComponent(raw.slice("#bootstrap=".length))
      : "";
  } catch {
    bearer = "";
  } finally {
    if (raw) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }
  if (bearer) {
    bootstrapFragmentCapture = { locationKey, bearer };
    return bearer;
  }
  if (bootstrapFragmentCapture?.locationKey === locationKey) {
    return bootstrapFragmentCapture.bearer;
  }
  return "";
}

function operationKey(prefix: string): string {
  const value =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${value}`;
}

function slugify(value: string): string {
  return value
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 50);
}

function safeNextUrl(value: string): string | null {
  if (!value.startsWith("/p/") || value.startsWith("//") || value.includes("\\")) return null;
  const parsed = new URL(value, window.location.origin);
  return parsed.origin === window.location.origin ? parsed.pathname + parsed.search : null;
}

function SetupMessage({
  state,
  onRetry,
}: { state: Exclude<ExchangeState, "ready">; onRetry: () => void }) {
  const unavailable = state === "unavailable";
  return (
    <section className={DIALOG} role={unavailable ? "alert" : "status"}>
      <header className={HEADER}>
        <div>
          <h1 className={TITLE}>{state === "exchanging" ? "Verifying this installation" : "Setup unavailable"}</h1>
          <p className={SUBTITLE}>
            {state === "exchanging"
              ? "Checking the one-time installer capability."
              : "This setup link is missing, expired, already used, or not valid for this deployment."}
          </p>
        </div>
      </header>
      {state === "error" && (
        <footer className={FOOTER}>
          <span className={FOOTER_NOTE}>The setup service could not be reached.</span>
          <div className={ACTIONS}>
            <Button type="button" onClick={onRetry}>
              Try again
            </Button>
          </div>
        </footer>
      )}
    </section>
  );
}

function ClaimForm() {
  const [organizationName, setOrganizationName] = useState("");
  const [organizationSlug, setOrganizationSlug] = useState("");
  const [projectName, setProjectName] = useState("First project");
  const [projectSlug, setProjectSlug] = useState("first-project");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successUrl, setSuccessUrl] = useState<string | null>(null);
  const [idempotencyKey] = useState(() => operationKey("instance-claim"));
  // Identical contract to CreateOrg, deliberately: the two sign-up paths post
  // the same payload, so a field present on one and absent on the other means
  // the same product answers a person differently depending on the door.
  const [currency, setCurrency] = useState("");
  const [timezone, setTimezone] = useState("");
  const { currencyFallback, timezoneFallback, suggestedZone } = useReportingDefaults();

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!organizationName.trim() || !organizationSlug || !projectName.trim() || !projectSlug) return;
    setSubmitting(true);
    setError(null);
    try {
      const payload = {
        organization_name: organizationName.trim(),
        organization_slug: organizationSlug,
        project_name: projectName.trim(),
        project_slug: projectSlug,
        // Omit an unchosen currency, send the browser zone as the SUGGESTION it
        // is — the rules live in `reportingDefaults`, shared with the other
        // Project-creation doors so this one cannot drift from them.
        ...reportingPayload(currency, timezone, suggestedZone),
      };
      const confirmationResponse = await apiFetch("/api/instance/claim/confirmation", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify(payload),
      });
      if (!confirmationResponse.ok) {
        const errorPayload = (await confirmationResponse.json().catch(() => ({}))) as { message?: string };
        throw new Error(errorPayload.message || "The instance claim could not be confirmed.");
      }
      const confirmation = (await confirmationResponse.json()) as EntryConfirmationResponse;
      const response = await apiFetch("/api/instance/claim", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
          "X-Confirmation-Id": confirmation.confirmation_id,
          "X-Confirmation-Secret": confirmation.confirmation_secret,
        },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const errorPayload = (await response.json().catch(() => ({}))) as { message?: string };
        throw new Error(errorPayload.message || "The instance could not be claimed.");
      }
      const result = (await response.json()) as ClaimResponse;
      const next = safeNextUrl(result.next_url);
      if (!next) throw new Error("The server returned an invalid destination.");
      setSuccessUrl(next);
      setSubmitting(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The instance could not be claimed.");
      setSubmitting(false);
    }
  }

  if (successUrl) {
    return (
      <section className={DIALOG} role="status" aria-labelledby="claim-success-title">
        <header className={HEADER}>
          <div>
            <h1 id="claim-success-title" className={TITLE}>Instance claimed</h1>
            <p className={SUBTITLE}>
              The first organization and project are ready.
            </p>
          </div>
        </header>
        <div className={BODY}>
          <Status tone="success">Setup completed</Status>
        </div>
        <footer className={FOOTER}>
          <span className={FOOTER_NOTE}>Continue when you are ready to open the new project.</span>
          <div className={ACTIONS}>
            <Button type="button" onClick={() => window.location.assign(successUrl)}>
              Continue to getting started
            </Button>
          </div>
        </footer>
      </section>
    );
  }
  return (
    <section className={DIALOG} role="dialog" aria-labelledby="claim-instance-title">
      <header className={HEADER}>
        <div>
          <h1 id="claim-instance-title" className={TITLE}>Claim this toorow instance</h1>
          <p className={SUBTITLE}>
            Create the first organization and project. Your signed-in identity becomes their owner.
          </p>
        </div>
      </header>
      <form onSubmit={submit}>
        <div className={BODY}>
          <div className={FIELD}>
            <label htmlFor="claim-org-name" className={FIELD_LABEL}>Organization name</label>
            <Input
              id="claim-org-name"
              value={organizationName}
              maxLength={100}
              required
              onChange={(event) => {
                setOrganizationName(event.target.value);
                setOrganizationSlug(slugify(event.target.value));
              }}
            />
          </div>
          <div className={FIELD}>
            <label htmlFor="claim-org-slug" className={FIELD_LABEL}>Organization slug</label>
            <Input
              id="claim-org-slug"
              value={organizationSlug}
              pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
              maxLength={50}
              required
              onChange={(event) => setOrganizationSlug(event.target.value.toLowerCase())}
            />
          </div>
          <div className={FIELD}>
            <label htmlFor="claim-project-name" className={FIELD_LABEL}>First project</label>
            <Input
              id="claim-project-name"
              value={projectName}
              maxLength={100}
              required
              onChange={(event) => {
                setProjectName(event.target.value);
                setProjectSlug(slugify(event.target.value));
              }}
            />
          </div>
          <ReportingCurrencyField
            idPrefix="claim"
            value={currency}
            fallback={currencyFallback}
            onChange={setCurrency}
          />
          <ReportingTimezoneField
            idPrefix="claim"
            value={timezone}
            suggestedZone={suggestedZone}
            onChange={setTimezone}
          />
          <div className={FIELD}>
            <label htmlFor="claim-project-slug" className={FIELD_LABEL}>Project slug</label>
            <Input
              id="claim-project-slug"
              value={projectSlug}
              pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
              maxLength={50}
              required
              onChange={(event) => setProjectSlug(event.target.value.toLowerCase())}
            />
          </div>
          {/* Same confirmation summary as the two sibling doors. This screen
              asked for a currency and a timezone and read neither back, on the
              one action that cannot be replayed — a claim happens once per
              instance. */}
          <div className={SCOPE_SUMMARY} aria-label="What will be created">
            <div className={SCOPE_TITLE}>Before you claim</div>
            <div className={SCOPE_ROW}>
              <span className={SCOPE_KEY}>Organization</span>
              <span className={SCOPE_VAL}>{organizationName.trim() || "—"}</span>
            </div>
            <div className={SCOPE_ROW_RULED}>
              <span className={SCOPE_KEY}>Slug</span>
              <span className={SCOPE_VAL_MONO}>{organizationSlug || "—"}</span>
            </div>
            <div className={SCOPE_ROW_RULED}>
              <span className={SCOPE_KEY}>First project</span>
              <span className={SCOPE_VAL}>{projectName.trim() || "—"}</span>
            </div>
            <div className={SCOPE_ROW_RULED}>
              <span className={SCOPE_KEY}>Reporting currency</span>
              <span className={SCOPE_VAL}>{currencySummary(currency, currencyFallback)}</span>
            </div>
            <div className={SCOPE_ROW_RULED}>
              <span className={SCOPE_KEY}>Reporting timezone</span>
              <span className={SCOPE_VAL}>
                {timezoneSummary(timezone, suggestedZone, timezoneFallback)}
              </span>
            </div>
          </div>
          {error && <Alert tone="error">{error}</Alert>}
        </div>
        <footer className={FOOTER}>
          <span className={FOOTER_NOTE}>This one-time action claims the whole instance.</span>
          <div className={ACTIONS}>
            <Button type="submit" disabled={submitting}>
              {submitting ? "Claiming…" : "Claim instance"}
            </Button>
          </div>
        </footer>
      </form>
    </section>
  );
}

export default function ClaimInstance() {
  const [bootstrapBearer] = useState(readBootstrapFragment);
  const [state, setState] = useState<ExchangeState>("exchanging");
  const [exchangeAttempt, setExchangeAttempt] = useState(0);
  const exchangeRequest = useRef<{ attempt: number; promise: Promise<Response> } | null>(null);

  useEffect(() => {
    let request = exchangeRequest.current;
    if (!request || request.attempt !== exchangeAttempt) {
      if (bootstrapFragmentCapture?.bearer === bootstrapBearer) {
        bootstrapFragmentCapture = null;
      }
      const promise = bootstrapBearer
        ? apiFetch("/api/instance/bootstrap/exchange", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ bootstrap_bearer: bootstrapBearer }),
          })
        : apiFetch("/api/instance/claim/session", {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
          });
      request = { attempt: exchangeAttempt, promise };
      exchangeRequest.current = request;
    }
    let cancelled = false;
    setState("exchanging");
    request.promise
      .then((response) => {
        if (!cancelled) setState(response.ok ? "ready" : "unavailable");
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [bootstrapBearer, exchangeAttempt]);

  function retryExchange() {
    setExchangeAttempt((attempt) => attempt + 1);
  }

  return (
    <div className={STAGE}>
      <div className={SCRIM}>
        {state === "ready" ? (
          <AuthGate>
            <ClaimForm />
          </AuthGate>
        ) : (
          <SetupMessage state={state} onRetry={retryExchange} />
        )}
      </div>
    </div>
  );
}