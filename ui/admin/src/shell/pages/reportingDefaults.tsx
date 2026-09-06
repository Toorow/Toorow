/**
 * The two Project reporting defaults — currency and timezone — asked once, the
 * same way, by every screen that creates a Project.
 *
 * Why this file exists. Three screens create a Project (CreateOrg, ClaimInstance,
 * CreateProject) and each carried its own copy of the same decision. Two of them
 * asked for the currency and offered the browser zone as a suggestion;
 * CreateProject posted a hardcoded EUR and the browser zone as if a human had
 * typed them. So the same product gave a person a different answer depending on
 * which door they came through — and the door that fabricated was the one that
 * recorded a fabricated value as an operator choice.
 *
 * Two rules the server already holds, mirrored here exactly once:
 *
 *   - An UNSET currency is OMITTED from the payload. `project_provenance.decide()`
 *     records any value it receives as `operator` ("an explicit human value", in
 *     its own words), so sending a value nobody picked writes a lie into the one
 *     column that says whether a number can be trusted. Absent, it travels as
 *     `default` — which is what it is (AD-9, `currency_refusal.py:25`).
 *   - The browser zone is a SUGGESTION, sent as `timezone_suggestion`, never as
 *     `timezone`. `decide_timezone` takes `suggestion` + `suggestion_evidence`
 *     precisely for this and refuses a suggestion that does not name its source.
 *
 * And the reason the summaries live here rather than in each screen: a decision
 * collected and never shown back is not a decision. The strings below are what a
 * confirmation step reads out, and they distinguish the three origins the server
 * distinguishes — chosen, suggested, platform fallback — instead of flattening
 * them into one displayed value.
 *
 * The platform fallbacks are SERVED (`/api/reference/{currencies,timezones}` →
 * `platform_default`), not copied: `project_provenance.PLATFORM_FALLBACK_*` is
 * the only place they are decided.
 *
 * ---------------------------------------------------------------------------
 * 2026-08-04 — why the control is `ReferenceSelect` and no longer a `<select>`
 * ---------------------------------------------------------------------------
 * The three doors used a plain `<select>` filled from `?limit=200` / `?limit=500`.
 * `reference_vocabulary_api._limit` clamps to `MAX_LIMIT = 50`, so what actually
 * rendered was the first 50 codes ALPHABETICALLY: 50 of 156 currencies and 50 of
 * 313 zones. `USD`, `JPY`, `Europe/Paris` and `America/New_York` were not in the
 * list. The field was visible, it was required to be honest, and it could not
 * carry most answers.
 *
 * The cap is not the defect: the endpoint is designed as a typeahead and says so.
 * The defect was consuming a typeahead with a control that cannot type. Story
 * 48.3 AC1 already requires "a searchable validated ISO 4217 selector" and "a
 * searchable validated IANA identifier selector; neither is a free-text or
 * hard-coded subset", and `ui/ReferenceSelect` is the ratified component for it —
 * already used by Project Settings, so the creation doors and the settings screen
 * now pick from one list ranked by one authority.
 *
 * One ratified behaviour was REVERSED by that move, deliberately (Jean,
 * 2026-08-04): an unreadable vocabulary used to HIDE the field. It no longer
 * does. `ReferenceSelect` shows the field and reports the outage inside its list,
 * on the same reasoning its own header gives for refusing "No matches" — a
 * missing field reads as "this product does not ask this", which is a different
 * and wrong claim when the list simply could not be read. The value still stays
 * unset, and the summary still says which origin that earns.
 */
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../lib/apiFetch";
import { ReferenceSelect, type ReferenceItem } from "../../ui";

export interface ReportingDefaults {
  /** The value the server writes when nobody chooses. "" until read. */
  currencyFallback: string;
  timezoneFallback: string;
  /** The browser's IANA zone — a guess, and labelled as one everywhere it appears. */
  suggestedZone: string;
}

const CURRENCY_ENDPOINT = "/api/reference/currencies";
const TIMEZONE_ENDPOINT = "/api/reference/timezones";

/**
 * Read the two platform fallbacks, so a screen can NAME the default it is about
 * to accept.
 *
 * It asks for `limit=1`: the items are no longer read here — `ReferenceSelect`
 * fetches and ranks them per keystroke — and only the envelope is wanted. A
 * failure leaves the fallback empty, and the summary then says a platform default
 * applies without inventing which code it would be.
 */
export function useReportingDefaults(): ReportingDefaults {
  const [currencyFallback, setCurrencyFallback] = useState("");
  const [timezoneFallback, setTimezoneFallback] = useState("");
  const suggestedZone = useMemo(
    () => Intl.DateTimeFormat().resolvedOptions().timeZone || "",
    [],
  );

  useEffect(() => {
    let cancelled = false;
    const load = async (path: string, setFallback: (value: string) => void) => {
      try {
        const resp = await apiFetch(`${path}?limit=1`);
        if (!resp.ok || cancelled) return;
        const body = (await resp.json()) as { platform_default?: string } | null;
        if (cancelled) return;
        if (typeof body?.platform_default === "string") setFallback(body.platform_default);
      } catch {
        // Deliberate: no fallback is named rather than a wrong one being shown.
      }
    };
    void load(CURRENCY_ENDPOINT, setCurrencyFallback);
    void load(TIMEZONE_ENDPOINT, setTimezoneFallback);
    return () => {
      cancelled = true;
    };
  }, []);

  return { currencyFallback, timezoneFallback, suggestedZone };
}

/** What the confirmation step says about the currency this Project will carry. */
export function currencySummary(chosen: string, fallback: string): string {
  if (chosen) return `${chosen} — your choice`;
  if (fallback) return `${fallback} — platform default, because nobody chose one`;
  return "Not set — the platform default applies";
}

/** Same, for the day boundary. A browser guess is named as a guess. */
export function timezoneSummary(chosen: string, suggested: string, fallback: string): string {
  if (chosen) return `${chosen.replace(/_/g, " ")} — your choice`;
  if (suggested) return `${suggested.replace(/_/g, " ")} — suggested by your browser`;
  if (fallback) return `${fallback.replace(/_/g, " ")} — platform default`;
  return "Not set — the platform default applies";
}

/**
 * The currency/timezone half of a Project-creation payload.
 *
 * One function, so no caller can reintroduce a fabricated value by writing the
 * spread itself.
 */
export function reportingPayload(
  chosenCurrency: string,
  chosenTimezone: string,
  suggestedZone: string,
): Record<string, string | null> {
  return {
    ...(chosenCurrency ? { currency: chosenCurrency } : {}),
    ...(chosenTimezone ? { timezone: chosenTimezone } : {}),
    timezone_suggestion: suggestedZone || null,
  };
}

export interface ReportingFieldProps {
  value: string;
  onChange: (code: string) => void;
  /** Distinguishes the three doors' input ids on the same page grammar. */
  idPrefix: string;
  /** Test seam — the same one `ReferenceSelect` exposes, passed straight through. */
  fetchItems?: (query: string) => Promise<ReferenceItem[]>;
}

/**
 * The shared shape of both fields: the same field recipe CreateOrg.tsx uses
 * (create-org.css is retired), the ratified searchable control, and one line
 * that says what NOT choosing means.
 *
 * `onClear` is what a `<select>` got for free from its first option and a
 * combobox does not: a way back to "nobody chose". It appears only once a value
 * exists, because before that there is nothing to undo.
 */
function ReportingField({
  id,
  label,
  vocabularyLabel,
  endpoint,
  value,
  placeholder,
  hint,
  clearLabel,
  onChange,
  fetchItems,
}: {
  id: string;
  label: string;
  vocabularyLabel: string;
  endpoint: string;
  value: string;
  placeholder: string;
  hint: string;
  clearLabel: string;
  onChange: (code: string) => void;
  fetchItems?: (query: string) => Promise<ReferenceItem[]>;
}) {
  return (
    <div className="grid gap-2">
      <label htmlFor={id} className="text-label font-semibold">{label}</label>
      <ReferenceSelect
        id={id}
        endpoint={endpoint}
        vocabularyLabel={vocabularyLabel}
        value={value || null}
        placeholder={placeholder}
        onChange={onChange}
        fetchItems={fetchItems}
      />
      {value ? (
        <button type="button" className="text-xs text-text-secondary hover:text-text-primary transition-colors hover:underline font-medium cursor-pointer" onClick={() => onChange("")}>
          {clearLabel}
        </button>
      ) : null}
      <p className="text-xs text-text-secondary mt-1">{hint}</p>
    </div>
  );
}

/** "Reporting currency", identical on all three doors. */
export function ReportingCurrencyField({
  value,
  onChange,
  fallback,
  idPrefix,
  fetchItems,
}: ReportingFieldProps & { fallback: string }) {
  return (
    <ReportingField
      id={`${idPrefix}-currency`}
      label="Reporting currency"
      vocabularyLabel="currencies"
      endpoint={CURRENCY_ENDPOINT}
      value={value}
      // The unset state is the one a person must be able to read at a glance,
      // so it takes the placeholder rather than a generic "Search currencies".
      placeholder={
        fallback
          ? `Not set — ${fallback} applies as a platform default`
          : "Not set — a platform default applies"
      }
      hint="Search the whole ISO 4217 vocabulary. Leave it unset and the value is recorded as a platform default, not as your choice."
      clearLabel="Clear — leave the currency unset"
      onChange={onChange}
      fetchItems={fetchItems}
    />
  );
}

/** "Reporting timezone", identical on all three doors. */
export function ReportingTimezoneField({
  value,
  onChange,
  suggestedZone,
  idPrefix,
  fetchItems,
}: ReportingFieldProps & { suggestedZone: string }) {
  return (
    <ReportingField
      id={`${idPrefix}-timezone`}
      label="Reporting timezone"
      vocabularyLabel="timezones"
      endpoint={TIMEZONE_ENDPOINT}
      value={value}
      placeholder={
        suggestedZone
          ? `Suggested by your browser — ${suggestedZone}`
          : "Not set — a platform default applies"
      }
      hint="Search the whole IANA vocabulary. Keep the browser suggestion and it is recorded as a suggestion; pick one and it is recorded as your decision."
      clearLabel="Clear — keep the browser suggestion"
      onChange={onChange}
      fetchItems={fetchItems}
    />
  );
}
