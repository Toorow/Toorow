import { type FormEvent, useEffect, useRef, useState } from "react";
import AuthGate from "../AuthGate";
import { apiFetch } from "../../lib/apiFetch";

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
    <section className="createorg-dialog" role={unavailable ? "alert" : "status"}>
      <header className="createorg-header">
        <div>
          <h1>{state === "exchanging" ? "Verifying this installation" : "Setup unavailable"}</h1>
          <p className="createorg-subtitle">
            {state === "exchanging"
              ? "Checking the one-time installer capability."
              : "This setup link is missing, expired, already used, or not valid for this deployment."}
          </p>
        </div>
      </header>
      {state === "error" && (
        <footer className="createorg-footer">
          <span>The setup service could not be reached.</span>
          <button className="primary-button" type="button" onClick={onRetry}>
            Try again
          </button>
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
        currency: "EUR",
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
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
      <section className="createorg-dialog" role="status" aria-labelledby="claim-success-title">
        <header className="createorg-header">
          <div>
            <h1 id="claim-success-title">Instance claimed</h1>
            <p className="createorg-subtitle">
              The first organization and project are ready.
            </p>
          </div>
        </header>
        <div className="createorg-body">
          <span className="signal-label success">
            <span className="signal-mark" />
            Setup completed
          </span>
        </div>
        <footer className="createorg-footer">
          <span>Continue when you are ready to open the new project.</span>
          <button className="primary-button" type="button" onClick={() => window.location.assign(successUrl)}>
            Continue to getting started
          </button>
        </footer>
      </section>
    );
  }
  return (
    <section className="createorg-dialog" role="dialog" aria-labelledby="claim-instance-title">
      <header className="createorg-header">
        <div>
          <h1 id="claim-instance-title">Claim this toorow instance</h1>
          <p className="createorg-subtitle">
            Create the first organization and project. Your signed-in identity becomes their owner.
          </p>
        </div>
      </header>
      <form onSubmit={submit}>
        <div className="createorg-body">
          <div className="field">
            <label htmlFor="claim-org-name">Organization name</label>
            <input
              id="claim-org-name"
              className="text-input"
              value={organizationName}
              maxLength={100}
              required
              onChange={(event) => {
                setOrganizationName(event.target.value);
                setOrganizationSlug(slugify(event.target.value));
              }}
            />
          </div>
          <div className="field">
            <label htmlFor="claim-org-slug">Organization slug</label>
            <input
              id="claim-org-slug"
              className="text-input"
              value={organizationSlug}
              pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
              maxLength={50}
              required
              onChange={(event) => setOrganizationSlug(event.target.value.toLowerCase())}
            />
          </div>
          <div className="field">
            <label htmlFor="claim-project-name">First project</label>
            <input
              id="claim-project-name"
              className="text-input"
              value={projectName}
              maxLength={100}
              required
              onChange={(event) => {
                setProjectName(event.target.value);
                setProjectSlug(slugify(event.target.value));
              }}
            />
          </div>
          <div className="field">
            <label htmlFor="claim-project-slug">Project slug</label>
            <input
              id="claim-project-slug"
              className="text-input"
              value={projectSlug}
              pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
              maxLength={50}
              required
              onChange={(event) => setProjectSlug(event.target.value.toLowerCase())}
            />
          </div>
          {error && <div className="createorg-error" role="alert">{error}</div>}
        </div>
        <footer className="createorg-footer">
          <span>This one-time action claims the whole instance.</span>
          <button className="primary-button" type="submit" disabled={submitting}>
            {submitting ? "Claiming…" : "Claim instance"}
          </button>
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
    <div className="createorg-stage">
      <div className="createorg-scrim">
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