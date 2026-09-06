/**
 * AI settings — the panel where inheritance is VISIBLE. Story 75-4.
 *
 * The object, its cascade and its refusals are ratified in
 * `docs/product-architecture/context-hub.md`, amendment of 2026-09-05 — "AI
 * settings: one object per scope, one cascade, served in the agent envelope".
 * This file is its door and decides nothing of its own.
 *
 * WHAT THIS SCREEN IS FOR. Behaviour rules, how far a model may reach, the
 * fiscal calendar and the language an answer is written in — resolved through
 * PLATFORM > ORG > PROJECT. A person opening it must be able to answer two
 * questions without leaving: *what will an agent obey here*, and *who decided
 * it*. So every field carries its value AND exactly one of three sentences:
 * "Set here", "Inherited from organization", "Platform default".
 *
 * THE SECOND QUESTION ONLY APPEARS ONCE THE FIRST IS ANSWERED. A group that is
 * inherited offers no "return to the parent value": undoing nothing is not an
 * action, and offering it would be a control that reads as a state. The button
 * appears exactly when this scope states something, which is exactly when it
 * can be given back.
 *
 * IT IS NOT A DELETE, AND IT NEVER SAYS DELETE. Returning a value to the parent
 * appends a version at the server; the history keeps both acts. The word on the
 * button is the user's ("Return to the organization value"), never the verb of
 * the wire.
 *
 * AND IT RETURNS ONE GROUP, NOT THE WHOLE SCOPE. The four buttons used to call
 * the same DELETE: giving the language back also gave back the rules, the
 * calendar and the reach that nobody asked about. A group is returned by
 * RE-STATING the override without it -- the PUT states the whole override, so
 * the fields left out of it fall through to the parent -- and the scope-wide
 * clearing is reached only when the group being returned was the last thing
 * this scope stated.
 *
 * A SAVE SENDS WHAT THE PERSON CHANGED, AND WHAT THIS SCOPE ALREADY STATED. It
 * used to send all six fields, so pressing Save once turned every inherited
 * value into an override of this scope and the cascade went flat. The patch is
 * a diff: a field is sent when the draft moved it away from the resolved value,
 * or when this scope already owned it (a PUT states the override entire, so a
 * kept field has to be restated or it would be given back).
 *
 * THE EMPTY STATE SAYS WHY, AND NAMES THE GESTURE. "No AI setting is set on this
 * project" — followed by where the values below come from, and by the one move
 * that changes them. Never a deployment state, never a table name.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  Failure,
  Retry,
  Field,
  Input,
  Loading,
  NativeSelect,
  Panel,
  PanelBody,
  PanelHeader,
  SectionHeader,
  Stack,
  Status,
  Textarea,
} from "../ui";
import { ApiError } from "../lib/apiFetch";
import {
  AI_SETTING_FIELDS,
  clearOrgAiSettings,
  clearProjectAiSettings,
  fetchOrgAiSettings,
  fetchProjectAiSettings,
  saveOrgAiSettings,
  saveProjectAiSettings,
  type AiSettingField,
  type AiSettingsPatch,
  type AiSettingsResponse,
  type FiscalCalendar,
  type NarrativeRegister,
  type QueryScope,
  type Scope,
  type WeekStartDay,
} from "./aiSettingsClient";

export type AiSettingsPanelProps = {
  /** The project whose settings are edited. Omit for the organization panel. */
  projectId?: string;
  /** The organization whose settings are edited, when no project is named. */
  orgId?: string;
  /**
   * May this person change what an agent obeys here? A reader still sees every
   * value AND its origin — that is the question this screen answers — but the
   * two acts that write are refused in the console instead of at the door, so
   * the answer arrives before the click rather than as a 403 after it.
   */
  canEdit?: boolean;
};

/** The three sentences, and the only three. Written once so no row invents a fourth. */
const SOURCE_SENTENCE: Record<Scope, string> = {
  PROJECT: "Set here",
  ORG: "Inherited from organization",
  PLATFORM: "Platform default",
};

/** The same three, read from an organization panel: its own scope is "here". */
const SOURCE_SENTENCE_FROM_ORG: Record<Scope, string> = {
  PROJECT: "Set on the project",
  ORG: "Set here",
  PLATFORM: "Platform default",
};

const QUERY_SCOPE_LABEL: Record<QueryScope, string> = {
  governed_views_only: "Governed views only",
  any_published_view: "Any published view",
  any_field: "Any field, governed or not",
};

const REGISTER_LABEL: Record<NarrativeRegister, string> = {
  plain: "Plain",
  executive: "Executive",
  technical: "Technical",
};

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const WEEK_DAYS: WeekStartDay[] = [
  "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
];

/** A group is one control (or pair) plus the one clear action that covers it. */
type Group = {
  key: string;
  title: string;
  fields: AiSettingField[];
};

const GROUPS: Group[] = [
  { key: "rules", title: "Behaviour rules", fields: ["rules_always", "rules_never"] },
  { key: "reach", title: "Query scope", fields: ["query_scope"] },
  { key: "calendar", title: "Fiscal calendar", fields: ["fiscal_calendar"] },
  {
    key: "narrative",
    title: "Narrative",
    fields: ["narrative_language", "narrative_register"],
  },
];

function linesToRules(text: string): string[] {
  return text.split("\n").map((line) => line.trim()).filter(Boolean);
}

function rulesToLines(rules: string[] | undefined): string {
  return (rules ?? []).join("\n");
}

/** The draft the form edits: strings, because that is what controls carry. */
type Draft = {
  rulesAlways: string;
  rulesNever: string;
  queryScope: QueryScope;
  yearStartMonth: string;
  weekStartDay: WeekStartDay;
  narrativeLanguage: string;
  narrativeRegister: NarrativeRegister;
};

function draftFrom(settings: AiSettingsResponse): Draft {
  const resolved = settings.resolved;
  const calendar: FiscalCalendar = resolved.fiscal_calendar ?? {};
  return {
    rulesAlways: rulesToLines(resolved.rules_always),
    rulesNever: rulesToLines(resolved.rules_never),
    queryScope: resolved.query_scope,
    yearStartMonth: String(calendar.year_start_month ?? 1),
    weekStartDay: (calendar.week_start_day ?? "monday") as WeekStartDay,
    narrativeLanguage: resolved.narrative_language,
    narrativeRegister: resolved.narrative_register,
  };
}

/** What the draft would state for one field, in the shape the door reads. */
function draftValue(draft: Draft, field: AiSettingField): unknown {
  switch (field) {
    case "rules_always":
      return linesToRules(draft.rulesAlways);
    case "rules_never":
      return linesToRules(draft.rulesNever);
    case "query_scope":
      return draft.queryScope;
    case "fiscal_calendar":
      // STATED WHOLE. The server refuses a half calendar by name, because the
      // cascade sources a FIELD and a calendar merged key by key would carry two
      // origins under one badge.
      return {
        year_start_month: Number(draft.yearStartMonth),
        week_start_day: draft.weekStartDay,
      };
    case "narrative_language":
      return draft.narrativeLanguage.trim();
    case "narrative_register":
      return draft.narrativeRegister;
  }
}

/** Order-free equality, so `{month, day}` and `{day, month}` are one calendar. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return JSON.stringify(value);
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, held]) => held !== undefined && held !== null)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return JSON.stringify(entries);
  }
  return JSON.stringify(value ?? null);
}

function sameValue(left: unknown, right: unknown): boolean {
  return canonical(left) === canonical(right);
}

/**
 * The override this scope should state after the person's edits — A DIFF.
 *
 * Two reasons a field belongs in the patch, and no third: the person MOVED it
 * away from the value that was resolved for them, or this scope ALREADY states
 * it and a PUT that left it out would give it back. Everything else is
 * inherited and stays inherited, which is what the person saw on the screen.
 */
function patchFrom(
  draft: Draft,
  settings: AiSettingsResponse,
  ownScope: Scope,
): AiSettingsPatch {
  const patch: Record<string, unknown> = {};
  for (const field of AI_SETTING_FIELDS) {
    const value = draftValue(draft, field);
    const moved = !sameValue(value, settings.resolved[field]);
    if (moved || settings.sources[field] === ownScope) patch[field] = value;
  }
  return patch as AiSettingsPatch;
}

/** The override MINUS one group — what a "return this group" PUT restates. */
function patchWithout(
  settings: AiSettingsResponse,
  ownScope: Scope,
  dropped: readonly AiSettingField[],
): AiSettingsPatch {
  const own = settings.own as unknown as Record<string, unknown> | null;
  if (!own) return {};
  const patch: Record<string, unknown> = {};
  for (const field of AI_SETTING_FIELDS) {
    if (dropped.includes(field)) continue;
    if (settings.sources[field] !== ownScope) continue;
    const stated = own[field];
    if (stated !== null && stated !== undefined) patch[field] = stated;
  }
  return patch as AiSettingsPatch;
}

export default function AiSettingsPanel({
  projectId,
  orgId,
  canEdit = true,
}: AiSettingsPanelProps) {
  const isProject = Boolean(projectId);
  const scopeId = projectId ?? orgId ?? "";
  const [settings, setSettings] = useState<AiSettingsResponse | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [unreadable, setUnreadable] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<{ message: string; field: string | null } | null>(null);
  const [busy, setBusy] = useState(false);

  const parentName = isProject ? "organization" : "platform";
  const sentences = isProject ? SOURCE_SENTENCE : SOURCE_SENTENCE_FROM_ORG;

  /** Bumped by the failure block's `Retry` (76-4): an error is never a dead end. */
  const [reloadToken, setReloadToken] = useState(0);

  const accept = useCallback((next: AiSettingsResponse) => {
    setSettings(next);
    setDraft(draftFrom(next));
    setRefusal(null);
    setUnreadable(null);
  }, []);

  useEffect(() => {
    if (!scopeId) return;
    let live = true;
    const read = isProject ? fetchProjectAiSettings(scopeId) : fetchOrgAiSettings(scopeId);
    read
      .then((next) => { if (live) accept(next); })
      .catch((error: unknown) => {
        if (!live) return;
        setSettings(null);
        setUnreadable(
          error instanceof ApiError && error.unauthenticated
            ? "You are signed out. Sign in again to read the AI settings."
            : "The AI settings could not be read.",
        );
      });
    return () => { live = false; };
  }, [scopeId, isProject, accept, reloadToken]);

  const ownScope: Scope = isProject ? "PROJECT" : "ORG";

  const ownedFields = useMemo(() => {
    if (!settings) return new Set<AiSettingField>();
    return new Set(
      (Object.keys(settings.sources) as AiSettingField[]).filter(
        (field) => settings.sources[field] === ownScope,
      ),
    );
  }, [settings, ownScope]);

  const statesSomething = ownedFields.size > 0;

  async function save() {
    if (!draft || !settings || !scopeId) return;
    const patch = patchFrom(draft, settings, ownScope);
    if (Object.keys(patch).length === 0) {
      // NO REQUEST. Nothing moved and this scope states nothing, so the door
      // would refuse a payload that states nothing — and the person would read
      // a sentence about an act they did not make.
      setRefusal({
        message:
          "Nothing has changed yet. Change a value above to set it on this " +
          `${isProject ? "project" : "organization"}.`,
        field: null,
      });
      return;
    }
    setBusy(true);
    setRefusal(null);
    try {
      const next = isProject
        ? await saveProjectAiSettings(scopeId, patch)
        : await saveOrgAiSettings(scopeId, patch);
      accept(next);
    } catch (error: unknown) {
      const body = error instanceof ApiError ? (error.body as { message?: string; field?: string }) : null;
      setRefusal({
        message: body?.message ?? "The AI settings could not be saved.",
        field: body?.field ?? null,
      });
    } finally {
      setBusy(false);
    }
  }

  /**
   * Give ONE group back to the parent, and leave every other override standing.
   *
   * The override is restated without this group's fields (a PUT states it
   * entire, so what is left out falls through); the scope-wide clearing is
   * reached only when this group was the last thing the scope stated, which is
   * the one case where "return this group" and "return everything" are one act.
   */
  async function returnGroupToParent(group: Group) {
    if (!scopeId || !settings) return;
    const kept = patchWithout(settings, ownScope, group.fields);
    setBusy(true);
    setRefusal(null);
    try {
      const next =
        Object.keys(kept).length === 0
          ? isProject
            ? await clearProjectAiSettings(scopeId)
            : await clearOrgAiSettings(scopeId)
          : isProject
            ? await saveProjectAiSettings(scopeId, kept)
            : await saveOrgAiSettings(scopeId, kept);
      accept(next);
    } catch (error: unknown) {
      const body = error instanceof ApiError ? (error.body as { message?: string }) : null;
      setRefusal({
        message: body?.message ?? "This scope could not be returned to its parent.",
        field: null,
      });
    } finally {
      setBusy(false);
    }
  }

  if (unreadable) {
    return <Failure what="The AI settings" message={unreadable} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }
  if (!settings || !draft) return <Loading label="the AI settings" />;

  /** The one sentence a field owes its reader, beside its value. */
  const origin = (field: AiSettingField) => (
    <Badge outline data-testid={`ai-setting-source-${field}`}>
      {sentences[settings.sources[field]]}
    </Badge>
  );

  return (
    <Panel>
      <PanelHeader
        title="AI settings"
        description={
          isProject
            ? "What an agent obeys on this project, and which scope decided each value."
            : "What an agent obeys across this organization, and which scope decided each value."
        }
      />
      <PanelBody>
        <Stack>
          {!statesSomething && (
            <EmptyState
              title={
                isProject
                  ? "No AI setting is set on this project."
                  : "No AI setting is set on this organization."
              }
              description={
                // THE RATIFIED SENTENCE (context-hub.md, amendment of 2026-09-05,
                // « The panel »): where the values come from, then the gesture.
                // An organization has one parent and the project has two, so the
                // first half names exactly the scopes above THIS one.
                (isProject
                  ? "Everything below comes from the organization or from the platform. "
                  : "Everything below comes from the platform. ") +
                "Set one to change how agents answer here."
              }
            />
          )}

          {refusal && (
            <Status tone="error" as="block" data-testid="ai-settings-refusal">
              {refusal.message}
            </Status>
          )}

          {GROUPS.map((group) => {
            const groupIsOwned = group.fields.some((field) => ownedFields.has(field));
            return (
              <section key={group.key} data-testid={`ai-setting-group-${group.key}`}>
                <SectionHeader
                  title={
                    // ONE FIELD, TWO CONTROLS, ONE BADGE. The calendar is a
                    // single setting of the cascade and it is stated whole, so
                    // the month and the week start share one origin — said once,
                    // above both, rather than twice beside one of them or (as it
                    // was) beside the month alone while the week start carried
                    // no origin at all.
                    group.key === "calendar" ? (
                      <>
                        {group.title} {origin("fiscal_calendar")}
                      </>
                    ) : (
                      group.title
                    )
                  }
                  actions={
                    groupIsOwned ? (
                      <Button
                        variant="secondary"
                        size="sm"
                        disabled={busy || !canEdit}
                        onClick={() => returnGroupToParent(group)}
                        data-testid={`ai-setting-clear-${group.key}`}
                      >
                        {`Return to the ${parentName} value`}
                      </Button>
                    ) : undefined
                  }
                />
                <Stack className="gap-4">
                  {group.key === "rules" && (
                    <>
                      <Field
                        label={<>Always {origin("rules_always")}</>}
                        hint="One sentence per line. What every answer must do."
                      >
                        {(props) => (
                          <Textarea
                            {...props}
                            rows={4}
                            value={draft.rulesAlways}
                            onChange={(event) =>
                              setDraft({ ...draft, rulesAlways: event.target.value })
                            }
                          />
                        )}
                      </Field>
                      <Field
                        label={<>Never {origin("rules_never")}</>}
                        hint="One sentence per line. What no answer may do."
                      >
                        {(props) => (
                          <Textarea
                            {...props}
                            rows={4}
                            value={draft.rulesNever}
                            onChange={(event) =>
                              setDraft({ ...draft, rulesNever: event.target.value })
                            }
                          />
                        )}
                      </Field>
                    </>
                  )}

                  {group.key === "reach" && (
                    <Field
                      label={<>How far a model may reach {origin("query_scope")}</>}
                      hint="Governed views only is the narrowest, and the shipped default."
                    >
                      {(props) => (
                        <NativeSelect
                          {...props}
                          value={draft.queryScope}
                          onChange={(event) =>
                            setDraft({ ...draft, queryScope: event.target.value as QueryScope })
                          }
                        >
                          {(Object.keys(QUERY_SCOPE_LABEL) as QueryScope[]).map((value) => (
                            <option key={value} value={value}>
                              {QUERY_SCOPE_LABEL[value]}
                            </option>
                          ))}
                        </NativeSelect>
                      )}
                    </Field>
                  )}

                  {group.key === "calendar" && (
                    <>
                      <Field
                        label="The fiscal year opens in"
                        hint="A year that does not start in January changes every year-to-date figure."
                      >
                        {(props) => (
                          <NativeSelect
                            {...props}
                            value={draft.yearStartMonth}
                            onChange={(event) =>
                              setDraft({ ...draft, yearStartMonth: event.target.value })
                            }
                          >
                            {MONTHS.map((month, index) => (
                              <option key={month} value={String(index + 1)}>
                                {month}
                              </option>
                            ))}
                          </NativeSelect>
                        )}
                      </Field>
                      <Field label="The week starts on">
                        {(props) => (
                          <NativeSelect
                            {...props}
                            value={draft.weekStartDay}
                            onChange={(event) =>
                              setDraft({
                                ...draft,
                                weekStartDay: event.target.value as WeekStartDay,
                              })
                            }
                          >
                            {WEEK_DAYS.map((day) => (
                              <option key={day} value={day}>
                                {day.charAt(0).toUpperCase() + day.slice(1)}
                              </option>
                            ))}
                          </NativeSelect>
                        )}
                      </Field>
                    </>
                  )}

                  {group.key === "narrative" && (
                    <>
                      <Field
                        label={<>Language {origin("narrative_language")}</>}
                        hint="A BCP 47 tag: en, en-GB, fr-FR."
                        error={
                          refusal?.field === "narrative_language" ? refusal.message : undefined
                        }
                      >
                        {(props) => (
                          <Input
                            {...props}
                            value={draft.narrativeLanguage}
                            onChange={(event) =>
                              setDraft({ ...draft, narrativeLanguage: event.target.value })
                            }
                          />
                        )}
                      </Field>
                      <Field label={<>Register {origin("narrative_register")}</>}>
                        {(props) => (
                          <NativeSelect
                            {...props}
                            value={draft.narrativeRegister}
                            onChange={(event) =>
                              setDraft({
                                ...draft,
                                narrativeRegister: event.target.value as NarrativeRegister,
                              })
                            }
                          >
                            {(Object.keys(REGISTER_LABEL) as NarrativeRegister[]).map((value) => (
                              <option key={value} value={value}>
                                {REGISTER_LABEL[value]}
                              </option>
                            ))}
                          </NativeSelect>
                        )}
                      </Field>
                    </>
                  )}
                </Stack>
              </section>
            );
          })}

          <div>
            <Button onClick={save} disabled={busy || !canEdit} data-testid="ai-settings-save">
              {isProject ? "Save on this project" : "Save on this organization"}
            </Button>
          </div>
        </Stack>
      </PanelBody>
    </Panel>
  );
}
