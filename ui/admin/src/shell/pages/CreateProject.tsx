/**
 * CreateProject — the "create the organization's first project" screen, reached
 * when an organization exists but carries no project yet (App.tsx).
 *
 * It creates a PROJECT, and a Project carries two reporting defaults: the
 * currency every figure is restituted in, and the day boundary every figure is
 * grouped by. This screen used to decide both by itself — a hardcoded EUR, and
 * the browser's IANA zone posted under the `timezone` key — with
 * neither shown to the person creating the project. `project_provenance.decide()`
 * records any value it receives as `operator`, so both were written down as human
 * decisions nobody made. The rules and the wording now come from
 * `reportingDefaults.ts`, shared with the two ENTRY doors, so the three cannot
 * drift apart again.
 *
 * Styling: migrated off `create-org.css` (waves 2-4 of `docs/ui-css-strategy.md`).
 * The focused-dialog surface is Tailwind utilities over the `@theme` tokens plus
 * the `Button` / `Input` / `Alert` primitives; it stacks at `--layer-modal`,
 * declared in `shell/application.css`. Same geometry as CreateOrg, deliberately.
 */
import { useMemo, useState, type FormEvent } from "react";
import { apiFetch } from "../../lib/apiFetch";
import { Alert, Button, Input } from "../../ui";
import type { OrgRef } from "../scope";
import {
  ReportingCurrencyField,
  ReportingTimezoneField,
  currencySummary,
  reportingPayload,
  timezoneSummary,
  useReportingDefaults,
} from "./reportingDefaults";
import "../application.css";

/* The focused-dialog surface, ported 1:1 from the retired `create-org.css`.
   The scrim dims with the scheme-stable `surface-dark` token. */
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

/* ScopeSummary rows: the first row after the title takes SCOPE_ROW, every
   following row takes SCOPE_ROW_RULED. */
const SCOPE_SUMMARY = "rounded-lg border border-divider-base bg-background-light px-4.5 py-4";
const SCOPE_TITLE = "mb-3 font-display text-label font-semibold";
const SCOPE_ROW =
  "grid grid-cols-[128px_minmax(0,1fr)] items-baseline gap-3.5 py-[7px] max-[1180px]:grid-cols-1 max-[1180px]:gap-[3px]";
const SCOPE_ROW_RULED = `${SCOPE_ROW} border-t border-divider-base`;
const SCOPE_KEY = "text-caption font-semibold text-text-secondary";
const SCOPE_VAL = "text-label leading-normal [overflow-wrap:anywhere]";
const SCOPE_VAL_MONO = `${SCOPE_VAL} font-mono`;

function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 50);
}

export interface CreateProjectProps {
  org: OrgRef;
  onCreated?: (projectId: string) => void;
}

export default function CreateProject({ org, onCreated }: CreateProjectProps) {
  const [name, setName] = useState("First project");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // "" means NOBODY CHOSE, and it must stay "" all the way to the payload.
  const [currency, setCurrency] = useState("");
  const [timezone, setTimezone] = useState("");
  const { currencyFallback, timezoneFallback, suggestedZone } = useReportingDefaults();
  const slug = useMemo(() => slugify(name), [name]);
  const trimmedName = name.trim();
  const canSubmit =
    trimmedName.length > 0 && trimmedName.length <= 100 && slug.length > 0 && !submitting;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const response = await apiFetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: trimmedName,
          slug,
          org_id: org.id,
          ...reportingPayload(currency, timezone, suggestedZone),
        }),
      });
      const payload = (await response.json().catch(() => ({}))) as {
        id?: string;
        message?: string;
      };
      if (response.status !== 201 || !payload.id) {
        throw new Error(payload.message ?? `The project could not be created (HTTP ${response.status}).`);
      }
      if (onCreated) onCreated(payload.id);
      else window.location.assign(`/p/${encodeURIComponent(payload.id)}/overview/getting-started`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The project could not be created.");
      setSubmitting(false);
    }
  }

  return (
    <div className={STAGE}>
      <div className={SCRIM}>
        <form
          className={DIALOG}
          role="dialog"
          aria-labelledby="create-project-title"
          onSubmit={submit}
          noValidate
        >
          <header className={HEADER}>
            <div>
              <h1 id="create-project-title" className={TITLE}>Create your first project</h1>
              <p className={SUBTITLE}>
                {org.name} already exists. Add its first real project before opening the application.
              </p>
            </div>
          </header>
          <div className={BODY}>
            <div className="grid gap-2">
              <label htmlFor="create-project-name" className="text-label font-semibold">
                Project name
              </label>
              <Input
                id="create-project-name"
                value={name}
                maxLength={100}
                required
                onChange={(event) => {
                  setName(event.target.value);
                  setError(null);
                }}
              />
              <p className="m-0 text-caption leading-snug text-text-secondary">
                Project slug: <span className="font-mono text-label">{slug || "?"}</span>
              </p>
            </div>

            <ReportingCurrencyField
              idPrefix="create-project"
              value={currency}
              fallback={currencyFallback}
              onChange={(code) => {
                setCurrency(code);
                setError(null);
              }}
            />

            <ReportingTimezoneField
              idPrefix="create-project"
              value={timezone}
              suggestedZone={suggestedZone}
              onChange={(code) => {
                setTimezone(code);
                setError(null);
              }}
            />

            {/* Everything this action is about to create, including the two
                defaults the screen just collected — a value asked for and not
                read back is a value nobody agreed to. */}
            <div className={SCOPE_SUMMARY} aria-label="What will be created">
              <div className={SCOPE_TITLE}>Before you create</div>
              <div className={SCOPE_ROW}>
                <span className={SCOPE_KEY}>Organization</span>
                <span className={SCOPE_VAL}>{org.name}</span>
              </div>
              <div className={SCOPE_ROW_RULED}>
                <span className={SCOPE_KEY}>Project</span>
                <span className={SCOPE_VAL}>{trimmedName || "—"}</span>
              </div>
              <div className={SCOPE_ROW_RULED}>
                <span className={SCOPE_KEY}>Slug</span>
                <span className={SCOPE_VAL_MONO}>{slug || "—"}</span>
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
            <span className="text-caption leading-snug text-text-secondary">
              This project will belong to {org.name}.
            </span>
            <Button type="submit" disabled={!canSubmit}>
              {submitting ? "Creating..." : "Create project"}
            </Button>
          </footer>
        </form>
      </div>
    </div>
  );
}
