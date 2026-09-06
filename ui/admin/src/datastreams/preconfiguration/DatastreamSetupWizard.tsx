import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Database, FileSpreadsheet, Table2 } from "lucide-react";
import {
  Accordion, AccordionContent, AccordionItem, AccordionTrigger, Badge, Button, Checkbox, ChoiceGroup,
  Collapsible, CollapsibleContent, CollapsibleTrigger,
  ConfirmDialog,
  Field, Input, NativeSelect, Panel, PanelHeader, SectionHeader, Status, Stepper, summarizeObject,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
  stateLabel,
  stateTone,
} from "../../ui";
import DiscoveredSchema from "./DiscoveredSchema";
import SourceConnectorPull, {
  buildStartingPoints,
  RecommendedStarts,
  STARTING_POINT_STATE,
  templateStartingPoint,
  type StartingPoint,
} from "./SourceConnectorPull";
import SourceExternalBq from "./SourceExternalBq";
import SourceManagedFeed from "./SourceManagedFeed";
import {
  accountAuthorizationId,
  AUTHORIZATION_COPY,
  AUTHORIZATION_UNNAMED_VALUE,
  authorizationOptions,
  authorizationValue,
  EVIDENCE_SOURCE,
  CONFIDENCE_TONE,
  EXTERNAL_BQ_COPY,
  GOOGLE_SHEETS_COPY,
  INBOUND_CHANNEL_COPY,
  sourceAccountId,
  type Cadence,
  type ChannelContract,
  type CommonInput,
  type FeedInput,
  type Mode,
  type SetupInput,
} from "./sourceStepShared";
import {
  accountServesConnector,
  compileDatastreamSetupDraft,
  confirmDatastreamDraft,
  createDatastreamSetupDraft,
  createDatastreamSetupPreview,
  createDatastreamSetupObservation,
  discardDatastreamSetupDraft,
  listDatastreamSetupTemplates,
  prepareDatastreamDraftConfirmation,
  prepareDatastreamFinalReview,
  readDatastreamMaterialization,
  readDatastreamPreconfigurationProposal,
  readDatastreamSetupDraft,
  readDatastreamSetupObservation,
  readDatastreamSetupPreviewJob,
  readDatastreamSetupSourceOptions,
  readProjectReportingTimezone,
  saveDatastreamSetupTemplate,
  stageDatastreamSetupAsset,
  updateDatastreamSetupDraft,
  type DatastreamConfirmation,
  type DatastreamFinalReview,
  type DatastreamMaterialization,
  type DatastreamMaterializationStatus,
  type DatastreamPreconfigurationProposal,
  type DatastreamSetupConnectorOption,
  type DatastreamSetupDraft,
  type DatastreamSetupObservation,
  type DatastreamSetupPreviewJob,
  type DatastreamSetupSourceAccount,
  type DatastreamSetupSourceOptions,
  type DatastreamSetupTemplate,
  type DatastreamSetupTemplateList,
  type PreconfigurationProposalItem,
  type ProjectReportingTimezone,
  type ProposalCapabilityValue,
  type ProposalOwnerReference,
  type WizardApiConfig,
} from "../wizard/wizardApi";

// `Mode`, `Cadence`, the three input shapes and `SetupInput` MOVED to
// `sourceStepShared.ts` (57.12, T2b): the three extracted Source panels and
// this wizard read one definition, or they drift.
/** The cadences that run ONCE A PERIOD, and are therefore the ones that can name
 *  an arrival hour (story 57.8, A3). The SAME two `schedule_mcp.ARRIVAL_HOUR_CADENCES`
 *  names — `("nightly", "weekly")` — but in the PLAN's vocabulary, which this
 *  draft speaks end to end: the column says `nightly`, the plan intent says
 *  `daily`, and activation refuses `nightly` on this path
 *  (`datastream_activation.py`, "Reviewed schedule mode is unsupported"). The
 *  operator-facing label carries the ratified word; the wire value stays the
 *  one the server accepts. */
const ARRIVAL_HOUR_CADENCES: readonly Cadence[] = ["daily", "weekly"];

interface Props {
  projectId: string;
  apiBase?: string;
  onCancel?: () => void;
  preview?: { draft: DatastreamSetupDraft; proposal: DatastreamPreconfigurationProposal };
  resumeDraftId?: string | null;
  onSourceSetup?: (draftId: string) => void;
  onCreated?: (datastreamId: string) => void;
  /** The draft was discarded (AI-336, ratified 2026-08-31). Distinct from
   *  `onCancel`, which LEAVES a draft that stays resumable: the route that owns
   *  the `datastream-setup-return` key has to forget it, or the next entry would
   *  resume a draft the server now refuses every write on. */
  onDiscarded?: () => void;
  /** Opens the owner of a proposed object by REFERENCE (story 57.4). The wizard
   *  never builds an address: it hands the reference to the shell, which is the only
   *  thing that knows the Organization segment the router requires. */
  onOpenOwner?: (owner: ProposalOwnerReference) => void;
}

/** THE ONE LIST OF SECTIONS — labels, sub-titles, ranks and persisted ids, all
 *  read from here (57.9).
 *
 *  There used to be TWO lists: this one, and a parallel array of section names
 *  used to persist `first_incomplete`. Two lists of the same thing drift, and
 *  the day one of them lost an entry the other kept pairing ranks with the
 *  wrong names in silence.
 *
 *  The third section is gone (57.9): it asked nothing in any of the three modes,
 *  bound no value, and displayed three constant sentences promising a location,
 *  a retention and proposed Outputs it never showed. What it said that is true
 *  is read where it is proved — the full grain in the compiler's `outputs`
 *  proposal, shown as `Output` in the right-hand summary, and the read-only
 *  object in the acknowledgement `Source` refuses to continue without.
 *
 *  THE LIST IS FIVE, AND THE DOCUMENT FIXES BOTH ITS LENGTH AND ITS NAMES —
 *  `datastream-workbench-and-wizard.md:35-39` names them, `:1144` counts them,
 *  and `:2757` says the constant below IS the ratified list. A build once
 *  declared seven, splitting `Mode` and `Identity` out of `Source` and citing
 *  an "amendment of 2026-08-11" that `grep` finds nowhere in that document.
 *  Both are questions of step 1 and they are back inside it: the mode is the
 *  first decision of the journey (`:17`), and the name and the data role are
 *  Required items of `1. Source` in all three modes (`:183`, "in that order").
 *  What the list owes is the five names; `:31` sets the temper of the rest —
 *  it refuses an extra stop even for a section, so it certainly does not buy
 *  one for a question that already belongs to one. */
const SECTIONS = [
  {
    id: "source",
    label: "Source",
    detail: "Mode, source, name and role",
    // NO STORY NUMBER AND NO AMENDMENT DATE IN A RENDERED STRING. One of these
    // read "One question per screen (57.12, amendment of 2026-08-11)" — a
    // tracker reference and a delivery date shown to an operator, who has
    // neither. Why the screen behaves this way belongs to the repository; what
    // it says is what to answer next.
    intro: "One question at a time: how this Datastream reads its source, then only that way's "
      + "own questions, then what it is called. Raw credentials never enter this draft.",
  },
  {
    id: "configure",
    label: "Configure",
    detail: "Template, report and fields",
    intro: "Start from a saved template or a declared preset, then adjust the fields. "
      + "Only fields relevant to the selected mode are persisted.",
  },
  {
    id: "classify_and_map",
    label: "Classify and map",
    detail: "Identity, grain and fields",
    intro: "Review the Business Domains, the full joint grain and every normalized physical field.",
  },
  {
    id: "preview_validate",
    label: "Preview and validate",
    detail: "Masked mode evidence",
    intro: "A bounded sample is collected so you can check it before anything is kept. "
      + "Sensitive values are hidden unless you have asked for them.",
  },
  {
    id: "schedule_activate",
    label: "Schedule and activate",
    detail: "Two explicit confirmations",
    intro: "The first confirmation creates the Datastream and collects once, without serving anything. "
      + "Publishing what that collection produced is a second, separate confirmation.",
  },
] as const;
type SectionId = (typeof SECTIONS)[number]["id"];
/** The rank of a section, BY NAME. No section integer is written anywhere else
 *  in this file: a literal rank is what turns "one section was removed" into
 *  "every operator after it is somewhere they did not ask to be". */
const SECTION_INDEX = Object.fromEntries(
  SECTIONS.map((section, index) => [section.id, index]),
) as Record<SectionId, number>;

/** WHAT A RANK SAVED BY AN OLDER WIZARD MEANT, spelled out.
 *
 *  A draft persists `wizard_state.active_section` as an INTEGER, and an integer
 *  has no name. Six sections became five, so reading that integer back as a rank
 *  moves rank 3 and rank 4 one section forward without saying so, and rank 5 —
 *  `Schedule and activate` — indexes past the end of the list and takes the
 *  whole screen down with `steps[activeSection][0]`.
 *
 *  Rank 2 was the removed third section. It falls back to `configure`: that
 *  panel used to co-render with `Configure`, which is also the last section
 *  before it that asked the operator for anything.
 *
 *  THE TABLE IS SIX LONG BECAUSE SIX IS WHAT A RANK-ONLY DRAFT CAN MEAN. Only
 *  builds older than `active_section_ref` ever saved a rank with no name beside
 *  it, and that build had six sections; every build since writes both keys. */
const LEGACY_SECTION_BY_RANK: readonly SectionId[] = [
  "source",
  "configure",
  "configure",
  "classify_and_map",
  "preview_validate",
  "schedule_activate",
];

/** SECTION NAMES THIS LIST NO LONGER CARRIES, and where their questions went.
 *
 *  A build that declared seven sections persisted `mode` and `identity` as
 *  positions. Neither was ever a stop of its own in the ratified list, and
 *  neither question moved: the mode is the first thing `Source` asks and the
 *  name and the role are the last. So a draft that names one reopens on
 *  `Source`, standing in front of the very question it was left on — nothing
 *  was taken away from the operator, so nothing is announced. */
const RETIRED_SECTION_ALIAS: Readonly<Record<string, SectionId>> = {
  mode: "source",
  identity: "source",
};

/** Where a saved draft reopens, and whether that position was understood.
 *
 *  `unknown` is not "empty": a draft with no `wizard_state` reopens on
 *  `Source` in silence, because that is what starting is. A saved position that
 *  names no section this wizard knows and no section it retired is different —
 *  the operator is moved, so the screen says so rather than landing them
 *  somewhere without a word. */
export function restoreSection(
  saved: { active_section?: unknown; active_section_ref?: unknown } | undefined | null,
): { index: number; unknown: boolean } {
  if (!saved) return { index: 0, unknown: false };
  const named = saved.active_section_ref;
  if (typeof named === "string") {
    const index = SECTIONS.findIndex((section) => section.id === named);
    if (index >= 0) return { index, unknown: false };
    const alias = RETIRED_SECTION_ALIAS[named];
    if (alias) return { index: SECTION_INDEX[alias], unknown: false };
    return { index: 0, unknown: true };
  }
  const rank = saved.active_section;
  if (rank == null) return { index: 0, unknown: false };
  if (typeof rank !== "number" || !Number.isInteger(rank) || rank < 0 || rank >= LEGACY_SECTION_BY_RANK.length) {
    return { index: 0, unknown: true };
  }
  return { index: SECTION_INDEX[LEGACY_SECTION_BY_RANK[rank]], unknown: false };
}

/** THE THREE MODES, AS THE VALIDATED MOCKUP DRAWS THEM — story 57.5.
 *
 *  `.source-choice` in `mockups/datastream-create.html:45-47` is an icon, a
 *  title and one sentence per mode. The console showed three bare radio buttons
 *  with their titles and no sentence at all, so the first decision of the
 *  wizard — the one that decides which of three different screens follows — was
 *  the only one made without a word of explanation. The control is
 *  `ChoiceGroup variant="card"`, which declares in its own header that it IS
 *  this mockup element; it had no caller, which is what "the mockup was never
 *  ported" looks like in a grep.
 *
 *  TWO DIVERGENCES FROM THE MOCKUP, both deliberate:
 *  - `Connector report` reads `Connector pull`. The mockup does not outrank the
 *    glossary, and `connector_pull` is the mode's name everywhere else.
 *  - the managed-feed sentence names the four channels this deployment offers
 *    (`file_upload`, `google_sheets`, `inbound_email`, `webhook`, ratified in
 *    `datastream-workbench-and-wizard.md:47`). The mockup predates two of them,
 *    and a sentence that names half of a list is read as the whole list. */
const MODE_CHOICES: ReadonlyArray<{ value: Mode; label: string; hint: string; icon: ReactNode }> = [
  {
    value: "connector_pull",
    label: "Connector pull",
    hint: "Select a provider report, fields, grain, history and supported schedule.",
    icon: <Table2 aria-hidden className="size-5" />,
  },
  {
    value: "external_bq",
    label: "External BigQuery",
    hint: "Reference a read-only table or view while its external writer stays authoritative.",
    icon: <Database aria-hidden className="size-5" />,
  },
  {
    value: "managed_feed",
    label: "Managed feed",
    hint: "Import a file, a Google Sheet, an inbound email or a webhook delivery into a governed toorow landing.",
    icon: <FileSpreadsheet aria-hidden className="size-5" />,
  },
];

/** The same three choices WITHOUT the sentence: the compact card has no room
 *  for it, and the caption under the Mode row prints the selected one. */
const MODE_CHOICES_COMPACT = MODE_CHOICES.map(({ hint: _hint, ...choice }) => choice);

/** The two sentences a resumed position can need. Never the same one: an
 *  ordinary start is not an anomaly, and a position that no longer exists is not
 *  a loss of what was typed. */
const RESUME_COPY = {
  unknownTitle: "This draft was saved on an older version of this wizard",
  unknownBody:
    // The fallback is index 0, `Source`. It is deliberately not named in the
    // sentence: the first step is where you are, and a reader who has just been
    // moved needs to know their work survived, not the label of a rail entry.
    "Its saved position no longer exists. You are back at the first step; nothing you entered was lost.",
} as const;

function newInput(mode: Mode): SetupInput {
  // NO DEFAULT DATA ROLE. It used to open on "Performance", which is an answer
  // nobody gave: what a Datastream collects is a business decision, and no
  // connector contract can observe it. A preselected role is silently accepted
  // and reaches `app.datastreams.data_role` as if someone had chosen it.
  const common: CommonInput = {
    name: "", domain_ids: [],
    schedule: { mode: mode === "external_bq" ? "daily" : "manual" },
    wizard_state: { active_section: 0, active_section_ref: "source", first_incomplete: "source" },
  };
  if (mode === "external_bq") return {
    ...common, mode,
    source: { access_ref: "", object_ref: "", declared_writer: "", readonly_acknowledged: false },
    configure: {
      watermark_semantics: "", logical_dataset_name: "", expected_freshness: "",
      verification_window: "", expected_history: "", row_filters: "",
    },
  };
  if (mode === "managed_feed") return {
    ...common, mode,
    source: {
      channel: "file_upload", source_account_ref: "", template_ref: "", staged_asset_ref: "", sheet_ref: "",
    },
    configure: {
      input_ref: "", parsing_contract: "header_row=1", logical_dataset_name: "",
      date_semantics: "", grain: "", write_mode: "replace",
    },
  };
  return {
    ...common, mode,
    source: { source_account_ref: "", connector_ref: "", connector_contract_version_ref: "", report_ref: "" },
    configure: {
      date_field: "", metrics: "", dimensions: "", date_window: "", filters: "",
      history_intent: "", cadence_intent: "", grain: "",
    },
  };
}

function requestKey(prefix: string): string { return `${prefix}-${crypto.randomUUID()}`; }
// The account/authorization helpers (`sourceAccountId`, `sourceAccountOption`,
// `accountAuthorizationId`, `authorizationLabel`, `AuthorizationOption`,
// `AUTHORIZATION_UNNAMED_VALUE`, `authorizationValue`, `authorizationOptions`,
// `shortRef`) MOVED to `sourceStepShared.ts` (57.12, T2b) with their comments;
// this file imports the ones it still reads.
function metadataText(value: unknown): string {
  if (value == null) return "Unavailable";
  if (Array.isArray(value)) return value.join(", ");
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
function csvValues(value: string): string[] { return value.split(",").map((item) => item.trim()).filter(Boolean); }

/** INLINE VALIDATION FOR THE STRUCTURED FREE-TEXT FIELDS (57.12, T5).
 *
 *  These fields accept text that only a format can make sense of, and until
 *  now the format was checked nowhere: the operator learned at `/compile`,
 *  three steps later, with a server refusal. The check runs ON BLUR — typing
 *  is not yet an answer — and the message says the rule in business words.
 *  Each rule is what the server's own reader accepts, no more: `filters` is
 *  parsed as a JSON array at compile (`datastream_preconfiguration.py`),
 *  `history_intent` is a JSON object, the parsing contract is `key=value`
 *  pairs, and a domain list is comma-separated tokens with no empty entry.
 *  Empty is always legal: these fields are all optional. */
function jsonShapeOrEmpty(raw: string, shape: "array" | "object"): boolean {
  const trimmed = raw.trim();
  if (!trimmed) return true;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (shape === "array") return Array.isArray(parsed);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed);
  } catch {
    return false;
  }
}
const STRUCTURED_FIELD_RULES = {
  date_window: {
    valid: (raw: string) => raw.trim() === "" || /^\d+\s*(days?)?$/i.test(raw.trim()),
    message: "A number of days, like 30 or 30 days — or empty.",
  },
  filters: {
    valid: (raw: string) => jsonShapeOrEmpty(raw, "array"),
    message: "A JSON array, like [\"country=FR\"] — or empty.",
  },
  history_intent: {
    valid: (raw: string) => jsonShapeOrEmpty(raw, "object"),
    message: "A JSON object, like {\"days\": 90} — or empty.",
  },
  parsing_contract: {
    valid: (raw: string) => raw.trim() === ""
      || raw.split(/[\n,]/).every((pair) => {
        const trimmed = pair.trim();
        const eq = trimmed.indexOf("=");
        return eq > 0 && trimmed.slice(eq + 1).trim() !== "";
      }),
    message: "One key=value per comma or line, like header_row=1 — or empty.",
  },
  domain_ids: {
    valid: (raw: string) => raw.trim() === "" || raw.split(",").every((token) => token.trim() !== ""),
    message: "Comma-separated identities, with no empty entry.",
  },
} as const;
type StructuredFieldKey = keyof typeof STRUCTURED_FIELD_RULES;

/** The SEVEN values of `app.datastreams.data_role`, mirroring
 *  `server/core/datastreams.py:37-45`, which itself mirrors the CHECK constraint
 *  of migration 093. Written once, here, so the next edit happens beside the
 *  comment that governs it: any change to the CHECK is made in all three places
 *  in the same commit.
 *
 *  This select used to offer `Performance`, `Finance`, `Reference`,
 *  `Operations`. Three of the four exist in no constraint and in no server code:
 *  choosing one produced `ActivationValidationError("Confirmed Datastream data
 *  role is invalid")` at the LAST step of the wizard, after six sections of work
 *  — the flow had six then, and has five since story 57.9 removed one.
 *
 *  The sentence beside each value is what it costs downstream, not a synonym.
 *  `Forecast & plan` is the future and is never summable with the observed;
 *  `Operational` and `Reference & targets` pair with no fee or tax rule at all. */
const DATA_ROLES: ReadonlyArray<readonly [string, string]> = [
  ["Spend", "what the platform billed"],
  ["Performance", "delivery and engagement observed"],
  ["Revenue & conversions", "what the business earned"],
  ["Forecast & plan", "what is planned, not observed"],
  ["Context", "signal that explains, not counts"],
  ["Reference & targets", "agreed values to compare against"],
  ["Operational", "how the pipeline itself behaves"],
];

/** What the `Identity` block says, kept out of the JSX so the markup stays
 *  readable and each sentence can be edited as a sentence. Every one names what
 *  is missing AND who writes it, which is the class of copy this screen already
 *  uses for its dead ends. */
const IDENTITY_COPY = {
  section:
    "What this Datastream is called, and what it collects. The data role decides which fee and tax rules can "
    + "ever match it, which is why it is asked beside the source rather than after it.",
  nameFromContract: "From connector contract; edit freely.",
  nameFree: "Accents and spaces belong in a name; nothing is normalized.",
  roleHint: "A business answer: no connector contract observes what a feed is for.",
  roleEmptyOption: "Select what this Datastream collects",
  roleUnproposable:
    "No data role can be proposed from a connector contract: what a Datastream is for is a business answer, "
    + "and this step is where it is given.",
  roleRequired: "A Datastream with no data role matches no fee or tax rule, and nothing downstream will say why.",
  nameRequired:
    "Every run, candidate and publication is reported under this name; nothing downstream can name an "
    + "unnamed Datastream.",
  // WHAT CANNOT BE ASKED, THEN THE GESTURE. The sentence used to stop at the
  // name and the category, which are the last two questions of the step; the
  // read also feeds the Connector grid and the account list, so an operator
  // reading it was told the smallest of its consequences.
  optionsFailed: "Source options could not be read, so no Connector, account, name or category can be "
    + "proposed here. Nothing was saved — save and exit, then reopen this draft to read them again.",
  categoryWithOrigin: "From connector manifest. Read-only here; corrected by an explicit source-type declaration.",
  categoryReadOnly: "Read-only here; a source type is corrected by an explicit declaration, never on this step.",
  categoryNoMode: "No connector in this mode, so no category is read",
  categoryNoneDeclared: "This connector's manifest declares no category",
  categoryNoConnector: "Select a Connector to read its category",
} as const;

// `EXTERNAL_BQ_COPY` MOVED to `sourceStepShared.ts` (57.12, T2b), with the
// `browse*` sentences of the live listing added there.

// `GOOGLE_SHEETS_COPY` MOVED to `sourceStepShared.ts` (57.12, T2b).

/** THE HOUR THE DAY IS EXPECTED TO ARRIVE/** THE HOUR THE DAY IS EXPECTED TO ARRIVE — story 57.8.
 *
 *  Only the cadences that run once a period can honour one (A3): `daily` and
 *  `weekly` (AI-217). The grain is a DATE, an hourly cadence re-fetches the day
 *  as it fills, and a manual Datastream runs when asked. The two that cannot get
 *  a sentence rather than a disabled control — a control greyed out without a
 *  word reads as a broken field, not as a refusal. */
const ARRIVAL_HOURS = Array.from({ length: 24 }, (_, hour) => hour);

const ARRIVAL_HOUR_COPY = {
  hint:
    "The hour of the project's day this Run should land, read in the operating timezone above — never in "
    + "your browser's. Leave it unset and the run is placed at local midnight, which is what every Datastream "
    + "did before this field existed.",
  hourly:
    "This cadence runs every hour, so there is no single moment for the day to arrive. The grain stays a "
    + "date; each run re-fetches the day as it fills.",
  manual:
    "This Datastream only runs on demand, so nothing arrives on a clock. Choose the daily or weekly cadence "
    + "to name an arrival hour.",
};

/** `06:00`, never a bare `0`. Local midnight is a legal choice and has to read
 *  as one, so that "nobody chose an hour" stays a different statement. */
function arrivalHourLabel(hour: number | undefined): string {
  return hour === undefined ? "Not set" : `${String(hour).padStart(2, "0")}:00`;
}

type WizardSchedule = NonNullable<CommonInput["schedule"]>;

/** Changing the cadence DROPS an hour the new cadence cannot honour, rather than
 *  carrying it silently: a draft holding an hour activation ignores would keep
 *  showing it in the summary the confirmation reads. */
function scheduleWithCadence(
  schedule: WizardSchedule | undefined,
  mode: Cadence,
): WizardSchedule {
  const { arrival_hour: _dropped, ...rest } = schedule ?? {};
  return ARRIVAL_HOUR_CADENCES.includes(mode) && schedule?.arrival_hour !== undefined
    ? { ...rest, mode, arrival_hour: schedule.arrival_hour }
    : { ...rest, mode };
}

/** The empty option means "no choice", so the key is REMOVED rather than set to
 *  a number that would read as midnight. */
function scheduleWithArrivalHour(
  schedule: WizardSchedule | undefined,
  raw: string,
): WizardSchedule {
  const { arrival_hour: _cleared, ...rest } = schedule ?? {};
  const base = { ...rest, mode: schedule?.mode ?? "daily" } as WizardSchedule;
  return raw === "" ? base : { ...base, arrival_hour: Number(raw) };
}

// `INBOUND_CHANNEL_COPY` MOVED to `sourceStepShared.ts` (57.12, T2b).

// `truncatedListingSentence` MOVED to `sourceStepShared.ts` (57.12, T2b).

/*
 * THE PROPOSAL-STATUS MAP IS GONE (76-2 review). Its disagreement: `blocked` red,
 * where the union says warning -- the same word five other screens argued about
 * and the arbitration settled. `missing`, `not_applicable` and `needs_review` are
 * declared words now. `REQUIREMENT_TONE` below is NOT this object: it grades how
 * badly a step is wanted, which is why it is the one map on this screen that may
 * still spend the accent.
 */

function displayProposalValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "Unavailable";
  if (typeof value === "object" && "object" in value
    && typeof (value as { object?: unknown }).object === "string") {
    return (value as { object: string }).object;
  }
  // `summarizeObject` reads a record field by field. The previous fallback was
  // `JSON.stringify`, which the library's own `Evidence` header names as a
  // defect already found twice in this codebase — a governed proposal printed as
  // a brace-and-quote blob is not something an operator can review.
  if (typeof value === "object") return summarizeObject(value as Record<string, unknown>);
  return String(value);
}

/** A machine key is not a label. `date_field` is not what a person calls it. */
function humanKey(key: string): string {
  const words = key.replaceAll("_", " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// `EVIDENCE_SOURCE` MOVED to `sourceStepShared.ts` (57.12, T2b);
// `REQUIREMENT_TONE` stays — only the proposal review reads it.

/** `Required` blocks continuation; `Recommended` is compiler-selected but
 *  editable; `Optional` is never preselected without evidence; `Automatic` is
 *  visible and inspectable but needs no input. The legend of the target (`:42`),
 *  which the operator could not read anywhere before. */
const REQUIREMENT_TONE: Record<PreconfigurationProposalItem["requirement"], "error" | "accent" | "neutral" | "info"> = {
  required: "error", recommended: "accent", optional: "neutral", automatic: "info",
};

// `CONFIDENCE_TONE` MOVED to `sourceStepShared.ts` (57.12, T2b).

// `NO_CONTRACT_COPY` MOVED to `sourceStepShared.ts` (57.12, T2b).

// `AUTHORIZATION_COPY` MOVED to `sourceStepShared.ts` (57.12, T2b).

// The Connector-card hint, the StartingPoint types and `STARTING_POINT_STATE`
// MOVED to `SourceConnectorPull.tsx` (57.12, T2b) with the catalogue they serve.

/** What the organization's saved configurations say (57.7). Kept out of the JSX
 *  like every other block of copy in this file, so each sentence is editable as
 *  a sentence.
 *
 *  FOUR ABSENCES, FOUR SENTENCES, and not one of them shares words with
 *  another. An organization that has saved nothing is the ORDINARY state of a
 *  new client and is `neutral`, never `error`; a list that failed to load is a
 *  fault and names its route, because an empty grid would read as "this
 *  organization has none", which would be false. A Connector that is not
 *  installed here is neither of those, and a saved account that vanished is the
 *  fifth state a preset cannot have -- the card stays applicable, because the
 *  account is precisely the variable the gesture reopens. */
const TEMPLATE_COPY = {
  title: "Organization templates",
  description:
    "A configuration this organization already validated. Applying one fills everything except the Source "
    + "Account — that is the variable duplication exists for, and it is the only thing you are asked again.",
  emptyTitle: "No saved configuration yet",
  noModeHint:
    "Choose a mode above — or start from a saved configuration below, which chooses its mode for you.",
  emptyBody:
    "A template is saved from a Datastream this wizard created, on its final review. Nothing here is written "
    + "by this step.",
  failedTitle: "Saved configurations could not be read",
  failedBody:
    "GET /api/projects/{project_id}/datastream-setup-templates did not answer. No card is drawn: an empty "
    + "grid would say this organization holds none, which is not what was measured.",
  otherProject: "Saved in another Project of this organization",
  accountGone: "The saved account is no longer exposed to this Project. Choose one.",
  /** WHAT IS LEFT TO CHOOSE IS READ, NEVER RECITED.
   *
   *  This block used to print one literal — "Origin — you will choose the
   *  account" — on every card, and that sentence is FALSE for two modes of
   *  three: `_OPERATOR_SOURCE_KEYS.external_bq` names no `source_account_ref`
   *  at all, so an external-BigQuery template announced an account that does
   *  not exist; and `managed_feed` reopens `template_ref` server-side, which no
   *  sentence mentioned. The server DECLARES what it reopened (`open_variables`,
   *  derived from the measured scope of each reference) and the card renders
   *  that declaration, one sentence per variable. A screen that recited its own
   *  list would be a second registry of the same fact, wrong the day the
   *  measurement changes. */
  openTitle: "What this template leaves open",
  openNone: "Nothing is left to choose: this template applies whole.",
  openUnknown:
    "This deployment did not declare what this template leaves open, so nothing is promised here. Check the "
    + "fields after applying.",
  originAccountGone: "Saved account is gone — you will choose one",
  connectorAbsent:
    "The Connector of this template is not installed in this deployment, so nothing here can be applied to "
    + "it.",
  applyConfirm:
    "Applying this template replaces the selections of the draft open in this Project — one resumable setup "
    + "draft exists per Project, and this is it. Continue?",
  saveTitle: "Save as template",
  saveDescription:
    "Reusable by every Project of this organization. Saving is reversible: a template is retired, never "
    + "destroyed.",
  saveCarries:
    "Carries: mode, Connector, report family, metrics, dimensions, grain, data role, business domains, "
    + "cadence.",
  saveDrops: "Does not carry: the Source Account, the discovery observation, any staged file.",
  saveLabel: "Template name",
  saveHint: "1 to 80 characters. Accents and spaces belong in a name; nothing is normalized.",
} as const;
/** One sentence per reference the server reopened, keyed by the reference's own
 *  name. The card looks its variables up here; it never decides which ones
 *  there are.
 *
 *  A key with no sentence is NAMED rather than skipped: an unknown reopened
 *  reference is still something the operator will have to answer, and dropping
 *  it silently would be the screen deciding a server declaration does not
 *  count. That fallback is what a deployment ahead of this build produces. */
const OPEN_VARIABLE_COPY: Record<string, string> = {
  source_account_ref:
    "The Source Account — this is the variable duplication exists for, and it is the one thing you are asked "
    + "again.",
  template_ref:
    "The file Template — a Template belongs to one Project (`file_source_templates.project_id`), so it "
    + "cannot travel with a template of the organization.",
  access_ref: "The BigQuery access — choose the governed access this Project exposes.",
};
function openVariableSentence(name: string): string {
  return OPEN_VARIABLE_COPY[name]
    ?? `${name.replaceAll("_", " ")} — reopened by the server, and it must be chosen again.`;
}

/** The sentence the confirmation of a Source Account change has always used.
 *  Hoisted out of the JSX by 57.7 so the click can decide, before confirming,
 *  whether the change will clear the Connector — and say so afterwards. */
const SOURCE_ACCOUNT_CONFIRM = "Changing the Source Account invalidates dependent evidence. Continue?";

// `SAFETY_COPY`, `SAFETY_TONE`, `RecommendedStarts`, `buildStartingPoints` and
// `templateStartingPoint` MOVED to `SourceConnectorPull.tsx` (57.12, T2b); the
// two projections are imported back from there, so the template cards below
// and question 4 of the panel keep ONE reading of the catalogue.

/** The organization's saved configurations, AFTER the Mode row (57.12).
 *
 *  They opened the step once, justified by "a template chooses the mode" — and
 *  the ratified order makes the template the LAST question of the step, not the
 *  zeroth. So the block follows the mode cards: with no mode chosen it stays
 *  readable but muted, an affordance rather than a question, and applying one
 *  preselects its mode through the same path as a mode card; once a mode IS
 *  chosen, only the templates of that mode are offered, ahead of the mode's
 *  own questions, and a mode with none draws nothing — "no template of this
 *  mode" is not a state the operator has to read.
 *
 *  Same primitives as the catalogue above and no new class: `Panel`,
 *  `PanelHeader`, `Badge`, `Status`, `Button`. What each card shows is what the
 *  table stores plus the report NAME joined the way 57.6 joins it — a card that
 *  printed `campaign_daily` would not be a card. */
function OrganizationTemplates({
  list, connectors, accounts, accountsLoaded, failed, currentProjectId, appliedRef, mode, onApply,
}: {
  list: DatastreamSetupTemplateList | null;
  connectors: DatastreamSetupConnectorOption[];
  accounts: DatastreamSetupSourceAccount[];
  accountsLoaded: boolean;
  failed: boolean;
  currentProjectId: string;
  appliedRef: string | null;
  /** The chosen mode, or null before question 0 is answered. Drives both the
   *  filtering and the muted presentation — never a second list from the
   *  server. */
  mode: Mode | null;
  onApply: (template: DatastreamSetupTemplate) => void;
}) {
  if (failed) {
    return <Status as="block" tone="error" title={TEMPLATE_COPY.failedTitle}>{TEMPLATE_COPY.failedBody}</Status>;
  }
  if (!list) return null;
  const visible = mode ? list.templates.filter((template) => template.mode === mode) : list.templates;
  // "No template OF THIS MODE" draws nothing — but an organization with NO
  // template at all is a different fact, and `emptyTitle` exists to say it.
  // This block only renders at `Configure`, which no draft reaches without a
  // mode, so the unfiltered `visible.length === 0` check made the empty state
  // unreachable. The filter hides the block only when it actually filtered
  // something out.
  //
  // The guard below said `visible.length === 0` — the very check the paragraph
  // above says was replaced — so the empty state it describes was unreachable
  // and the `emptyTitle` branch further down was dead code. An organization
  // that has saved nothing is the ORDINARY state of a new client, and
  // `CLAUDE.md` is explicit: an empty list says why, and names the gesture that
  // fills it. `emptyBody` names it. What must NOT draw is "no template of this
  // mode", which would be a non-sentence — so the block hides only when the
  // filter is what emptied it.
  if (visible.length === 0 && list.templates.length > 0) return null;
  return (
    // MUTED WHILE NO MODE IS CHOSEN: the mode cards are the question, this is
    // the shortcut past them. `opacity-75` is a utility of the design system,
    // not a new rule — the block stays fully operable, it just does not
    // compete with the question above it.
    <Panel className={`grid gap-3 p-4${mode ? "" : " opacity-75"}`}>
      {!mode && <p className="m-0 text-caption text-text-secondary">{TEMPLATE_COPY.noModeHint}</p>}
      <PanelHeader
        title={TEMPLATE_COPY.title}
        description={TEMPLATE_COPY.description}
        actions={
          <Badge tone={visible.length >= list.limit ? "warning" : "neutral"}>
            {visible.length} of {list.limit} templates
          </Badge>
        }
      />
      {visible.length === 0
        ? <Status as="block" tone="neutral" title={TEMPLATE_COPY.emptyTitle}>{TEMPLATE_COPY.emptyBody}</Status>
        // ONE CARD PER ROW, like `Starting points` above and for the same
        // measured reason: two abreast is ~178px here (measured at the grid).
        : <ul className="m-0 grid list-none gap-3 p-0">
          {visible.map((template) => {
            const point = templateStartingPoint(template, connectors);
            const connectorMissing = template.mode === "connector_pull"
              && Boolean(template.connector_ref)
              && !connectors.some((item) => item.connector_ref === template.connector_ref);
            const stateCopy = point && point.state !== "offered" ? STARTING_POINT_STATE[point.state] : null;
            const refused = connectorMissing || point?.state === "stale" || point?.state === "unavailable";
            const openVariables = template.open_variables;
            // The account can only be "gone" for a template that reopens one.
            // Reading `origin_source_account_ref` alone would say it of an
            // `external_bq` template, which asks for no account at all.
            const accountGone = accountsLoaded
              && (openVariables ?? []).includes("source_account_ref")
              && Boolean(template.origin_source_account_ref)
              && !accounts.some((account) => sourceAccountId(account) === template.origin_source_account_ref);
            const reportName = point?.report.display_name ?? template.report_ref ?? "No report family";
            const connectorName = connectors.find((item) => item.connector_ref === template.connector_ref)?.display_name
              ?? template.connector_ref
              ?? "No Connector";
            return (
              <li key={template.template_ref}>
                <Panel className="grid h-full content-start gap-2 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <strong className="text-label text-text">{template.label}</strong>
                    {stateCopy && <Badge tone={stateCopy.tone}>{stateCopy.badge}</Badge>}
                    <Badge tone="neutral">{template.mode.replaceAll("_", " ")}</Badge>
                  </div>
                  <p className="m-0 text-caption text-text-secondary">{connectorName} — {reportName}</p>
                  <dl className="m-0 grid grid-cols-2 gap-1 text-caption text-text-secondary">
                    <dt className="m-0">Saved</dt>
                    <dd className="m-0 text-text">{template.created_at?.slice(0, 10) ?? "Unavailable"}</dd>
                    <dt className="m-0">By</dt>
                    <dd className="m-0 truncate text-text">{template.created_by ?? "Unavailable"}</dd>
                  </dl>
                  {/* THE SERVER'S DECLARATION, RENDERED. One sentence per
                      reopened reference, and the emptiness of the list is an
                      answer of its own — `external_bq` reopens nothing, so its
                      card says the template applies whole instead of promising
                      an account that mode does not have. */}
                  <div className="grid gap-1">
                    <strong className="text-caption text-text">{TEMPLATE_COPY.openTitle}</strong>
                    {openVariables === undefined
                      ? <p className="m-0 text-caption text-text-secondary">{TEMPLATE_COPY.openUnknown}</p>
                      : openVariables.length === 0
                        ? <p className="m-0 text-caption text-text-secondary">{TEMPLATE_COPY.openNone}</p>
                        : <ul
                          className="m-0 grid list-none gap-1 p-0 text-caption text-text-secondary"
                          aria-label={`What ${template.label} leaves open`}
                        >
                          {openVariables.map((variable) => (
                            <li key={variable}>
                              {/* ONE PLACE SAYS THE ACCOUNT IS GONE, and the
                                  actionable sentence belongs to the field that
                                  will be empty. The same absence stated twice
                                  reads as two problems. */}
                              {variable === "source_account_ref" && accountGone
                                ? TEMPLATE_COPY.originAccountGone
                                : openVariableSentence(variable)}
                            </li>
                          ))}
                        </ul>}
                  </div>
                  {template.origin_project_ref !== currentProjectId && (
                    <p className="m-0 text-caption text-text-secondary">{TEMPLATE_COPY.otherProject}</p>
                  )}
                  {connectorMissing && (
                    <p className="m-0 text-caption text-text-secondary">{TEMPLATE_COPY.connectorAbsent}</p>
                  )}
                  {stateCopy && !connectorMissing && (
                    <p className="m-0 text-caption text-text-secondary">
                      {stateCopy.sentence}
                      {point?.reasonCode ? ` Reason code: ${point.reasonCode}.` : ""}
                    </p>
                  )}
                  <Button
                    variant={appliedRef === template.template_ref ? "secondary" : "default"}
                    disabled={refused}
                    aria-pressed={appliedRef === template.template_ref}
                    onClick={() => onApply(template)}
                    aria-label={`Start from template ${template.label}`}
                  >
                    {appliedRef === template.template_ref ? "Applied" : "Start from this template"}
                  </Button>
                </Panel>
              </li>
            );
          })}
        </ul>}
    </Panel>
  );
}

/** One proposal item, with the four things the target requires of every one:
 *  its evidence source, its confidence, its coverage and its exceptions.
 *
 *  All four were already on the wire (`datastream_preconfiguration.py:393-399`)
 *  and none was drawn — the review showed a status word, a raw machine key and a
 *  hex fingerprint. `owner_links` was dropped too, which is the boundary rule of
 *  `:62`: a Semantic View or Report stays a proposal until it is confirmed in
 *  its owner workspace, and the operator needs the way there. */
/** The sentence the screen owns, and the only one: an item that carries no effect.
 *  A deployment predating 57.4 sends none, and `0` or a generic phrase in its place
 *  would be the fabrication the whole story exists to remove. */
const CAPABILITY_EFFECT_UNKNOWN = "Effect unknown before the first run";
/** Told apart from an empty list on purpose. A Project with no capability row is
 *  stated by the compiler itself (`capabilities.none`); a proposal with NO
 *  capabilities section at all is a read that failed, and the two do not share a
 *  sentence — one is a state, the other is a fault. */
const CAPABILITY_SECTION_UNREADABLE = "Project capabilities could not be read";

/** A capability row, or null for every other item. The value is read, never
 *  recomputed: `Existing` / `Proposed in Project Settings` is the compiler's own
 *  verdict on `active_version_id` and `state`, and a second opinion here would drift
 *  from the row `Project Settings` shows for the same capability. */
function capabilityValue(item: PreconfigurationProposalItem): ProposalCapabilityValue | null {
  if (item.section !== "capabilities") return null;
  const value = item.proposed_value;
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<ProposalCapabilityValue>;
  return typeof candidate.label === "string" ? (candidate as ProposalCapabilityValue) : null;
}

/** A blocker says its CAUSE; the destination that repairs it is a reference, and a
 *  reference read aloud is noise. `summarizeObject` prints a nested object as `…`, and
 *  the composed path this replaced was worse: it printed an address that opened
 *  nothing. Dropped from the sentence, kept on the wire for whoever renders it. */
function withoutReferences(entry: Record<string, unknown>): Record<string, unknown> {
  const { repair_owner: _repairOwner, ...rest } = entry;
  return rest;
}

function ProposalItemCard({ item, onOpenOwner }: {
  item: PreconfigurationProposalItem;
  onOpenOwner?: (owner: ProposalOwnerReference) => void;
}) {
  const capability = capabilityValue(item);
  const confidence = CONFIDENCE_TONE[item.confidence?.level ?? "none"] ?? "neutral";
  const notes: Array<[string, Array<Record<string, unknown>>, "error" | "warning" | "neutral"]> = [
    ["Blocking", item.blockers ?? [], "error"],
    ["Warning", item.warnings ?? [], "warning"],
    ["Exception", item.exceptions ?? [], "neutral"],
  ];
  return (
    <Panel className="grid gap-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* A capability is called by its name. `humanKey("capability.country")` reads
            `Capability.country`, which is a machine key with a capital letter; the
            name is composed server-side with the sentence it belongs to. */}
        <strong className="text-label text-text">{capability?.name ?? humanKey(item.key)}</strong>
        <span className="flex items-center gap-2">
          {capability && (
            <Status tone={capability.label === "Existing" ? "success" : "neutral"}>{capability.label}</Status>
          )}
          <Badge tone={REQUIREMENT_TONE[item.requirement]}>{humanKey(item.requirement)}</Badge>
          <Status tone={stateTone(item.status)}>{stateLabel(item.status)}</Status>
        </span>
      </div>

      {/* ONE LINE PER CAPABILITY: its name, its state, what it would do to THIS
          Datastream, and the way to its owner. The sentence is derived, not written
          here — `datastream-workbench-and-wizard.md`, `Prefill and proposal rules`. */}
      <p className="m-0 text-body text-text">
        {capability ? capability.effect ?? CAPABILITY_EFFECT_UNKNOWN : displayProposalValue(item.proposed_value)}
      </p>

      {/* The two readings an operator needs before accepting a proposal: how
          sure the compiler is, and WHY. The rationale is the help — a level on
          its own is a number nobody can argue with. */}
      <div className="flex flex-wrap items-center gap-2 text-caption text-text-secondary">
        <Status tone={confidence}>{`${humanKey(item.confidence?.level ?? "none")} confidence`}</Status>
        {item.coverage?.state && (
          <Status tone="neutral">{`Coverage: ${item.coverage.state.replaceAll("_", " ")}`}</Status>
        )}
      </div>
      {item.confidence?.rationale && (
        <p className="m-0 text-caption text-text-secondary">{item.confidence.rationale}</p>
      )}

      {notes.map(([title, entries, tone]) =>
        entries.length ? (
          <Status
            key={title}
            as="block"
            tone={tone}
            title={`${title}${entries.length > 1 ? ` (${entries.length})` : ""}`}
          >
            <ul className="m-0 grid gap-1 pl-5">
              {entries.map((entry, index) => <li key={index}>{summarizeObject(withoutReferences(entry))}</li>)}
            </ul>
          </Status>
        ) : null,
      )}

      {item.downstream_impact?.length ? (
        <p className="m-0 text-caption text-text-secondary">
          Downstream: {item.downstream_impact.join(" · ")}
        </p>
      ) : null}

      <div className="grid gap-2">
        <strong className="text-caption text-text">Evidence</strong>
        {item.evidence_refs.length ? (
          item.evidence_refs.map((ref) => (
            <div
              key={`${ref.kind}-${ref.object_id}-${ref.version_id}`}
              className="grid gap-1 rounded-md bg-background-light p-3 text-caption text-text-secondary"
            >
              <span className="text-text">{EVIDENCE_SOURCE[ref.kind] ?? ref.kind.replaceAll("_", " ")}</span>
              <span>{ref.object_type} · version {ref.version_id}</span>
              <span className="break-all font-mono">{ref.fingerprint.slice(0, 12)} · observed {ref.observed_at}</span>
            </div>
          ))
        ) : (
          <Status tone="warning">Missing evidence</Status>
        )}
      </div>

      {/* `:62` — the wizard proposes and links; it never publishes in someone
          else's workspace. Without these the boundary is a sentence in a
          document and a dead end on the screen. */}
      {item.owner_links?.length ? (
        <div className="flex flex-wrap gap-2">
          {item.owner_links.map((link) =>
            // A REFERENCE IS OPENED BY THE ROUTER; a composed path is a link the
            // shell may not be able to parse. `Propose` is this button's label on an
            // inactive capability and it IS the link to its owner: it writes nothing,
            // calls no route, and the wizard has no way to enable anything.
            //
            // The `link.route` fallback that used to sit under this branch is
            // gone (57.5): it rendered `<a href="/projects/{id}/…">`, which
            // `parsePath` refuses on its first segment, so it was a button that
            // opened nothing. A link with no reference has no destination this
            // shell can resolve, and offering none says that.
            link.owner_reference ? (
              <Button
                key={link.object}
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => onOpenOwner?.(link.owner_reference!)}
              >
                {link.label ?? `Open ${link.object}`}
              </Button>
            ) : null,
          )}
        </div>
      ) : null}
    </Panel>
  );
}

function ProposalReview({ proposal, onOpenOwner }: {
  proposal: DatastreamPreconfigurationProposal;
  onOpenOwner?: (owner: ProposalOwnerReference) => void;
}) {
  const groups: Array<[string, Array<Record<string, unknown> | string>]> = [
    ["Existing", proposal.configuration_summary.existing],
    ["Will be created", proposal.configuration_summary.will_be_created],
    ["Will remain a proposal", proposal.configuration_summary.will_remain_a_proposal],
    ["Downstream impact", proposal.configuration_summary.downstream_impact],
  ];
  return <section className="grid gap-6" aria-labelledby="proposal-review-title">
    {/* The same heading primitive as the step above, one rank down: this
        section sits inside `Classify and map`, which is the screen's `h2`. */}
    <SectionHeader
      id="proposal-review-title"
      title="Review proposal"
      description={`Evidence is pinned to proposal ${proposal.proposal_ref}. `
        + "Compilation changed no active object."}
    />
    {proposal.is_stale && (
      <Status as="block" tone="error" title="Proposal is stale">
        Compile a new proposal before continuing.
      </Status>
    )}
    {proposal.sections.some((section) => section.key === "capabilities") ? null : (
      <Status as="block" tone="error" title={CAPABILITY_SECTION_UNREADABLE}>
        Every compiled proposal carries a capabilities section. Compile again — a Project with no
        active capability is a state this screen can show, and this is not it.
      </Status>
    )}
    {/* ONE PANEL PER ROW: two abreast is ~178px each (measured at the
        grid), and each carries a list of object names. */}
    <div className="grid gap-4">
      {groups.map(([title, values]) => (
        <Panel key={title} className="p-4">
          <PanelHeader title={title} />
          <ul className="m-0 grid gap-2 pl-5 text-body text-text-secondary">
            {values.map((value, index) => <li key={index}>{displayProposalValue(value)}</li>)}
          </ul>
        </Panel>
      ))}
    </div>
    <Accordion type="multiple" defaultValue={proposal.sections.map((section) => section.key)}>
      {proposal.sections.map((section) => (
        <AccordionItem key={section.key} value={section.key}>
          <AccordionTrigger>
            <span className="flex items-center gap-3">
              <span className="capitalize">{section.key.replaceAll("_", " ")}</span>
              <Status tone={stateTone(section.status)}>{stateLabel(section.status)}</Status>
            </span>
          </AccordionTrigger>
          <AccordionContent>
            <div className="grid gap-4">
              {section.items.map((item) => (
                <ProposalItemCard key={item.key} item={item} onOpenOwner={onOpenOwner} />
              ))}
            </div>
          </AccordionContent>
        </AccordionItem>
      ))}
    </Accordion>
    {/* NO "Proposal token" LINE (57.12, T6): a hex token is the evidence the
        review stands on, not a sentence for the person reviewing — the section
        header already names the proposal the evidence is pinned to. */}
  </section>;
}
export default function DatastreamSetupWizard({
  projectId, apiBase = "", onCancel, preview, resumeDraftId, onSourceSetup, onCreated, onOpenOwner,
  onDiscarded,
}: Props) {
  const cfg = useMemo<WizardApiConfig>(() => ({ projectId, apiBase }), [projectId, apiBase]);
  const [draft, setDraft] = useState<DatastreamSetupDraft | null>(preview?.draft ?? null);
  const [options, setOptions] = useState<DatastreamSetupSourceOptions | null>(null);
  /** Told apart from "no options yet". A failed `source-options` read cannot
   *  propose a name or a category, and the Identity block must say THAT rather
   *  than render as an honest emptiness it has not earned. */
  const [optionsFailed, setOptionsFailed] = useState(false);
  /** Re-read the step's options. The account question is answered from
   *  `source_accounts`, and an account the provider has just PROVEN -- one the
   *  enumeration never carried -- only appears once that list is read again. */
  const reloadSourceOptions = useCallback(async () => {
    const ref = draft?.draft_ref;
    if (!ref) return;
    try {
      const fresh = await readDatastreamSetupSourceOptions(cfg, ref);
      setOptions(fresh);
      setOptionsFailed(false);
    } catch {
      setOptionsFailed(true);
    }
  }, [cfg, draft?.draft_ref]);
  /** The organization's saved configurations (57.7), and the two states that
   *  are NOT the same absence: `null` with `templatesFailed` is a route that did
   *  not answer, `templates.length === 0` is an organization that has saved
   *  nothing yet. Drawing an empty grid for the first would claim a measurement
   *  nobody made. */
  const [templates, setTemplates] = useState<DatastreamSetupTemplateList | null>(null);
  const [templatesFailed, setTemplatesFailed] = useState(false);
  /** Which template this draft was started from, kept for the two things only
   *  it can say: whether the Source Account still has to be chosen, and what the
   *  right-hand summary reports as different from the original. */
  const [appliedTemplate, setAppliedTemplate] = useState<DatastreamSetupTemplate | null>(null);
  /** Why the Connector emptied itself, when it did. A field that clears in
   *  silence is how a configuration disappears with nobody able to name what
   *  took it. */
  const [narrowNotice, setNarrowNotice] = useState<string | null>(null);
  /** A DESTRUCTIVE CHANGE IS A PENDING ACTION, not a blocking native dialog
   *  (57.12). The four sites that asked through `window.confirm` — the mode
   *  change, the template apply, the Connector change, the account change —
   *  now describe what they are about to do and wait on the console's own
   *  `ConfirmDialog`: one dialog for the class, rendered once at the root. */
  const [pendingConfirm, setPendingConfirm] = useState<{
    title: string; message: string; run: () => void;
  } | null>(null);
  /** Step 1's second question, answered but never persisted: the draft accepts
   *  five source keys for `connector_pull` and refuses a sixth, and the account
   *  it does persist already names the authorization it belongs to. `""` is
   *  "not answered yet"; it is read only while no account has answered it. */
  const [pickedAuthorization, setPickedAuthorization] = useState("");
  const [templateLabel, setTemplateLabel] = useState("");
  /** Structured-field validation errors, keyed by field (57.12, T5). Set on
   *  blur, cleared on the next valid blur — never while typing, because an
   *  unfinished value is not yet an answer. */
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<StructuredFieldKey, string>>>({});
  /** The project's reporting timezone, READ (57.12, T5) — the field used to
   *  render the literal string "Project Settings timezone", a label wearing a
   *  value's clothes. `null` while unread or unreadable; the field then says
   *  where the value lives rather than inventing one. */
  const [reportingTimezone, setReportingTimezone] = useState<ProjectReportingTimezone | null>(null);
  const validateStructuredField = (key: StructuredFieldKey, raw: string) => {
    const rule = STRUCTURED_FIELD_RULES[key];
    setFieldErrors((current) => {
      const next = { ...current };
      if (rule.valid(raw)) delete next[key];
      else next[key] = rule.message;
      return next;
    });
  };
  const [templateSaved, setTemplateSaved] = useState<DatastreamSetupTemplate | null>(null);
  const [templateError, setTemplateError] = useState<string | null>(null);
  const previewInput = preview?.draft.operator_input as SetupInput | undefined;
  const [input, setInput] = useState<SetupInput | null>(() => previewInput ?? null);
  const [observation, setObservation] = useState<DatastreamSetupObservation | null>(null);
  const [proposal, setProposal] = useState<DatastreamPreconfigurationProposal | null>(preview?.proposal ?? null);
  const [previewJob, setPreviewJob] = useState<DatastreamSetupPreviewJob | null>(null);
  const [warningAcks, setWarningAcks] = useState<string[]>([]);
  const [finalReview, setFinalReview] = useState<DatastreamFinalReview | null>(null);
  /** Whether the creation review dialog is open (57.12, T4). The gesture is
   *  one — the dialog carries the review, its confirm runs the chain. */
  const [createReviewOpen, setCreateReviewOpen] = useState(false);
  /** Whether the discard confirmation is open (AI-336, ratified 2026-08-31).
   *  Its own flag rather than a `pendingConfirm` entry: this one has EVIDENCE to
   *  show — what the person is about to lose — and `pendingConfirm` carries a
   *  sentence and a gesture, nothing more. */
  const [discardOpen, setDiscardOpen] = useState(false);
  const [draftConfirmation, setDraftConfirmation] = useState<DatastreamConfirmation | null>(null);
  const [materialization, setMaterialization] = useState<DatastreamMaterialization | null>(null);
  const [materializationStatus, setMaterializationStatus] = useState<DatastreamMaterializationStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(Boolean(preview));
  const [error, setError] = useState<string | null>(null);
  // THE DRAFT READ THIS SCREEN OFFERS TO REPEAT (76-4). A setup that could not
  // create its draft used to be the end of the wizard: one red block, no
  // control, and a person whose only way on was the browser.
  const [draftAttempt, setDraftAttempt] = useState(0);
  // Which section the operator is working in. One at a time: the rail says where
  // the work stands, and `Back` must be able to move. Restored BY NAME through
  // `restoreSection`, never by reading the saved integer as a rank.
  const [activeSection, setActiveSection] = useState(
    () => restoreSection(previewInput?.wizard_state).index,
  );
  /** Set only when a saved position names no section this wizard knows. The
   *  screen then says it, instead of moving the operator without a word. */
  const [positionUnknown, setPositionUnknown] = useState(
    () => restoreSection(previewInput?.wizard_state).unknown,
  );
  // ONE REF PER SECTION OF `SECTIONS`, in its order — hooks cannot be mapped
  // from the constant, so the only defence is that the list is right beside it.
  const sectionRefs = [
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
  ];
  const generation = useRef(0);
  /** TWO GENERATIONS, because two different things are guarded (57.12, T1).
   *
   *  `generation` scopes the initial LOAD: the draft create/read and the
   *  options, templates and materialization reads that follow it. `editGeneration`
   *  is what `edit()` bumps, and it guards only what an edit INVALIDATES — the
   *  restore of a saved observation or proposal, which would otherwise land on
   *  top of the newer input the edit just wrote.
   *
   *  They were ONE counter, and the merge was a silent cancellation: an edit
   *  made while `source-options` was still in flight moved the only guard, so
   *  the arriving options were dropped with no state set at all — the Connector
   *  question simply never offered a card, and nothing named what happened. An
   *  edit has no business cancelling a read it does not invalidate. */
  const editGeneration = useRef(0);
  const revision = useRef(preview?.draft.current_revision ?? 0);
  const saveQueue = useRef(Promise.resolve<DatastreamSetupDraft | null>(preview?.draft ?? null));
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const scope = ++generation.current;
    setError(null);
    if (preview) return () => { generation.current += 1; };
    const loadDraft = resumeDraftId
      ? readDatastreamSetupDraft(cfg, resumeDraftId)
      : createDatastreamSetupDraft(cfg, requestKey(`create-${projectId}`));
    loadDraft.then(async (created) => {
        if (scope !== generation.current) return;
        // The edit guard is SNAPPED HERE, at the moment the draft answers: an
        // edit made after this point must stop the observation and proposal
        // restores below from landing on top of it, and an edit made before it
        // cannot exist — the draft renders nothing editable until this line.
        const editScope = editGeneration.current;
        revision.current = created.current_revision;
        setDraft(created);
        setSaved(true);
        const restored = created.operator_input as SetupInput | undefined;
        if (restored?.mode) {
          setInput(restored);
          const position = restoreSection(restored.wizard_state);
          setActiveSection(position.index);
          setPositionUnknown(position.unknown);
          const observationRef = restored.source.observation_ref;
          if (observationRef) {
            const restoredObservation = await readDatastreamSetupObservation(cfg, created.draft_ref, observationRef);
            if (scope === generation.current && editScope === editGeneration.current) {
              setObservation(restoredObservation);
            }
          }
        }
        if (created.current_proposal_ref) {
          const restoredProposal = await readDatastreamPreconfigurationProposal(
            cfg, created.draft_ref, created.current_proposal_ref,
          );
          if (scope === generation.current && editScope === editGeneration.current) setProposal(restoredProposal);
        }
        try {
          const sourceOptions = await readDatastreamSetupSourceOptions(cfg, created.draft_ref);
          // NO EDIT GUARD HERE, and that is the repair: an edit invalidates the
          // restored evidence above, never the catalogue. The load now either
          // lands or names its failure — the silent third outcome is gone.
          if (scope === generation.current) { setOptions(sourceOptions); setOptionsFailed(false); }
        } catch {
          // Named on the Identity step rather than thrown away: without options no
          // name and no category can be proposed, and nothing has been saved.
          if (scope === generation.current) setOptionsFailed(true);
        }
        try {
          const saved = await listDatastreamSetupTemplates(cfg);
          if (scope === generation.current) { setTemplates(saved); setTemplatesFailed(false); }
        } catch {
          // Named on the Source step, never swallowed: an unread list and an
          // organization with no template are two different facts.
          if (scope === generation.current) { setTemplates(null); setTemplatesFailed(true); }
        }
        try {
          const materialized = await readDatastreamMaterialization(cfg, created.draft_ref);
          if (scope === generation.current) setMaterializationStatus(materialized);
        } catch {
          // A legacy deployment may not expose Story 47.4 status yet. Source
          // configuration remains usable; materialization still fails closed.
        }
      })
      .catch((reason) => {
        if (scope === generation.current) {
          setError(reason instanceof Error ? reason.message : "Draft unavailable");
        }
      });
    return () => { generation.current += 1; if (timer.current) clearTimeout(timer.current); };
  }, [cfg, preview, projectId, resumeDraftId, draftAttempt]);

  const saveNow = useCallback((value: SetupInput, reason: string) => {
    if (!draft) return Promise.reject(new Error("Draft is not ready"));
    saveQueue.current = saveQueue.current.catch(() => null).then(async () => {
      const updated = await updateDatastreamSetupDraft(
        cfg, draft.draft_ref, revision.current,
        value as unknown as Record<string, unknown>, requestKey(reason), reason,
      );
      revision.current = updated.current_revision;
      setDraft(updated);
      setSaved(true);
      return updated;
    });
    return saveQueue.current;
  }, [cfg, draft]);

  const edit = useCallback((value: SetupInput) => {
    // The EDIT generation, not the load's: what this invalidates is restored
    // evidence, not the options read that may still be in flight (57.12, T1).
    editGeneration.current += 1;
    setInput(value); setProposal(null); setPreviewJob(null); setFinalReview(null);
    setDraftConfirmation(null); setMaterialization(null); setSaved(false);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      void saveNow(value, "autosave")
        .catch((reason) => setError(reason instanceof Error ? reason.message : "Autosave failed"));
    }, 500);
  }, [saveNow]);

  const editUpstream = (value: SetupInput, changed: boolean, message: string) => {
    const apply = () => {
      setObservation(null);
      edit(value);
    };
    if (changed) {
      setPendingConfirm({ title: "Invalidate dependent evidence?", message, run: apply });
      return;
    }
    apply();
  };

  /** The catalogue of the moment, and which of its cards the current source
   *  already matches — derived from the input rather than remembered in a
   *  second piece of state, so a resumed draft does not show every card
   *  unselected while the source is already set. */
  const startingPoints = input?.mode === "connector_pull"
    ? buildStartingPoints(options?.connectors ?? [], options?.recommendations ?? [], input.source)
      // OF THE CHOSEN CONNECTOR, since the amendment made this question 4. A
      // card is "a starting point when one fits", and what fits is measured
      // against the product already chosen; the whole-catalogue list belonged
      // to an order where this block was read before anything was chosen.
      .filter((point) => point.connector.connector_ref === input.source.connector_ref)
    : [];
  const selectedStartingPoint = input?.mode === "connector_pull"
    ? startingPoints.find((point) =>
        point.connector.connector_ref === input.source.connector_ref
        && point.report.report_ref === input.source.report_ref
        && (point.recommendation === null
          || point.recommendation.source_account_ref === input.source.source_account_ref))?.key ?? null
    : null;

  /** Selecting a starting point is an upstream edit like any other: it goes
   *  through the same confirmation that protects already-gathered evidence. A
   *  preset that silently discarded an observation would be the one place in
   *  this wizard where a proposal writes without asking.
   *
   *  It writes the source AND the two field lists the preset comes with — that
   *  is what "completable" means: the operator starts from the declared
   *  selection and adds their own on top. It writes nothing else: no window, no
   *  cadence, no filter, no date field. */
  const applyStartingPoint = (point: StartingPoint) => {
    if (!input || input.mode !== "connector_pull") return;
    const metrics = point.report.metrics.join(",");
    const dimensions = point.report.dimensions.join(",");
    const fromContract = `${point.connector.display_name} — ${point.report.display_name}`;
    const next = {
      ...input,
      name: input.name || fromContract,
      source: {
        ...input.source,
        ...(point.recommendation ? { source_account_ref: point.recommendation.source_account_ref } : {}),
        connector_ref: point.connector.connector_ref,
        connector_contract_version_ref: point.connector.contract_version_ref,
        report_ref: point.report.report_ref,
      },
      // The grain follows the dimensions, exactly as the `Dimensions` control
      // does on its own change. Two rules for one value would let a preset
      // and a click disagree about what the grain is.
      configure: { ...input.configure, metrics, dimensions, grain: dimensions },
    };
    // Same rule as `chooseReport`: the discovery a
    // starting point follows read no report, so a FIRST selection invalidates
    // nothing — and it must not go through `editUpstream`, which drops the
    // observation even when it asks nothing. A card paired to ANOTHER account
    // still invalidates: the account IS part of what the discovery read.
    const invalidates = Boolean(observation || proposal) && (Boolean(input.source.report_ref)
      || Boolean(point.recommendation
        && point.recommendation.source_account_ref !== input.source.source_account_ref));
    if (invalidates) {
      editUpstream(next, true, "Starting from this report family invalidates dependent evidence. Continue?");
      return;
    }
    edit(next);
  };

  /** Applying an organization template — ONE PATCH, no translation (57.7).
   *
   *  What the server stored is a `normalized_operator_input`, which is exactly
   *  what `PATCH …/datastream-setup-drafts/{id}` accepts, so this writes it as
   *  it is. The three draft-only keys were removed server-side, and the empty
   *  `wizard_state` of a fresh input is kept deliberately: a saved position
   *  belongs to the session that saved it.
   *
   *  It goes through `editUpstream` like every other upstream change, and its
   *  confirmation says the one thing an operator cannot guess —
   *  `uq_datastream_setup_draft_resumable` allows ONE resumable draft per
   *  Project, so applying a template here replaces what is open, it does not
   *  open a second one. */
  const applyOrganizationTemplate = (template: DatastreamSetupTemplate) => {
    const payload = template.operator_input as Partial<SetupInput> | undefined;
    if (!payload?.mode) return;
    // THE SAME PATH AS A MODE CARD (57.12): the base of the patch is the
    // identity-preserving fresh input of the template's mode, so applying one
    // PRESELECTS its mode exactly the way `selectMode` would have — and takes
    // no name or role the operator already gave.
    const base = freshModeInput(template.mode);
    const next = {
      ...base,
      ...payload,
      source: { ...base.source, ...(payload.source ?? {}) },
      configure: { ...base.configure, ...(payload.configure ?? {}) },
      wizard_state: base.wizard_state,
    } as SetupInput;
    const apply = () => {
      setAppliedTemplate(template);
      setNarrowNotice(null);
      editUpstream(next, false, TEMPLATE_COPY.applyConfirm);
    };
    if (input?.mode || observation || proposal) {
      setPendingConfirm({ title: "Start from this template?", message: TEMPLATE_COPY.applyConfirm, run: apply });
      return;
    }
    apply();
  };

  /** Answering question 3, and it now answers ONLY itself.
   *
   *  It used to re-decide the Connector on every account change — clearing it,
   *  its contract and the report family whenever the new account served
   *  something other than exactly one — because the account was asked first and
   *  was the only thing that could narrow anything. With the Connector asked
   *  first, question 3's options are computed FROM it (`accountChoices`), so an
   *  account that does not serve it is not offered and there is nothing left to
   *  re-decide. The invariant those forty lines defended is now structural.
   *
   *  The confirmation stays here rather than inside `editUpstream` so it is
   *  raised once, before the write; the cascade is unchanged, `editUpstream`
   *  still drops the observation. */
  const chooseSourceAccount = (accountRef: string) => {
    if (!input || input.mode !== "connector_pull") return;
    const apply = () => {
      setNarrowNotice(null);
      editUpstream(
        { ...input, source: { ...input.source, source_account_ref: accountRef } },
        false,
        SOURCE_ACCOUNT_CONFIRM,
      );
    };
    if (observation || proposal) {
      setPendingConfirm({ title: "Change the Source Account?", message: SOURCE_ACCOUNT_CONFIRM, run: apply });
      return;
    }
    apply();
  };

  /** Answering question 1, which is now the FIRST answer and therefore the one
   *  that invalidates the most. It clears the report family, as it always did,
   *  and it also clears a Source Account the new Connector is not served by —
   *  the case the old order could not produce, because the account came first.
   *  Silently keeping it would leave question 3 answered with a scope of a
   *  consent that does not open this product. */
  const chooseConnector = (value: string) => {
    if (!input || input.mode !== "connector_pull") return;
    const selected = options?.connectors.find((item) => item.connector_ref === value);
    const account = options?.source_accounts.find(
      (item) => sourceAccountId(item) === input.source.source_account_ref,
    );
    const keptAccount = account && accountServesConnector(account, value);
    const apply = () => {
      setPickedAuthorization("");
      setNarrowNotice(account && !keptAccount
        ? AUTHORIZATION_COPY.accountCleared(account.label, selected?.display_name ?? value)
        : null);
      editUpstream(
        {
          ...input,
          source: {
            ...input.source,
            source_account_ref: keptAccount ? input.source.source_account_ref : "",
            connector_ref: value,
            connector_contract_version_ref: selected?.contract_version_ref ?? "",
            report_ref: "",
          },
        },
        false,
        "Changing the Connector invalidates dependent evidence. Continue?",
      );
    };
    if (observation || proposal) {
      setPendingConfirm({
        title: "Change the Connector?",
        message: "Changing the Connector invalidates dependent evidence. Continue?",
        run: apply,
      });
      return;
    }
    apply();
  };

  /** Answering question 2. Changing the authorization takes the account with it
   *  when that account belonged to the previous one: an account is a scope OF a
   *  consent, so keeping it would leave the two answers naming different
   *  credentials. The account is cleared, never swapped for whatever the new
   *  authorization exposes first — the third question is the operator's. */
  const chooseAuthorization = (value: string) => {
    setPickedAuthorization(value);
    if (!input || input.mode !== "connector_pull" || !input.source.source_account_ref) return;
    const account = options?.source_accounts.find(
      (item) => sourceAccountId(item) === input.source.source_account_ref,
    );
    if (account && (accountAuthorizationId(account) ?? AUTHORIZATION_UNNAMED_VALUE) === value) return;
    setNarrowNotice(null);
    editUpstream(
      { ...input, source: { ...input.source, source_account_ref: "" } },
      Boolean(observation || proposal),
      SOURCE_ACCOUNT_CONFIRM,
    );
  };

  /** Declaring a clause of the channel contract. `edit`, not `editUpstream`: a
   *  declaration invalidates no evidence — the observation read the deployment's
   *  state, not this promise — so it costs no confirmation and no re-discovery.
   *  A clause set back to empty is REMOVED rather than stored as a zero. */
  const editChannelContract = (current: FeedInput, patch: ChannelContract) => {
    const merged: ChannelContract = { ...current.source.channel_contract, ...patch };
    if (merged.expected_interval_minutes == null) delete merged.expected_interval_minutes;
    if (!merged.allowed_senders?.length) delete merged.allowed_senders;
    const { channel_contract: _dropped, ...bare } = current.source;
    edit({ ...current, source: Object.keys(merged).length ? { ...current.source, channel_contract: merged } : bare });
  };

  /** A fresh input of a mode, MINUS what a mode change has no right to take
   *  (57.12). The mode owns its source and its configure intent, so those
   *  reset; the identity — the name the operator typed, the data role they
   *  chose, the domains — is not a mode's answer, and a click on another mode
   *  card used to erase it without a word. The schedule keeps the NEW mode's
   *  own proposal: it differs per mode deliberately (`newInput`). */
  const freshModeInput = (mode: Mode): SetupInput => {
    const next = newInput(mode);
    if (!input) return next;
    return {
      ...next,
      name: input.name ?? "",
      data_role: input.data_role,
      domain_ids: input.domain_ids ?? [],
    };
  };

  const selectMode = (mode: Mode) => {
    if (input?.mode === mode) return;
    const apply = () => {
      setObservation(null);
      edit(freshModeInput(mode));
    };
    if (input) {
      setPendingConfirm({
        title: "Change the source mode?",
        message: "Changing the source mode invalidates dependent observations. Continue?",
        run: apply,
      });
      return;
    }
    apply();
  };

  const discover = async () => {
    if (!draft || !input) return;
    setBusy(true); setError(null);
    try {
      if (timer.current) clearTimeout(timer.current);
      const persisted = await saveNow(input, "before_discovery");
      const expected = persisted?.current_revision ?? revision.current;
      let request: Record<string, unknown>;
      if (input.mode === "connector_pull") request = {
        expected_revision: expected, mode: input.mode, discovery_kind: "connector_contract",
        source_account_ref: input.source.source_account_ref,
        connector_ref: input.source.connector_ref,
        connector_contract_version_ref: input.source.connector_contract_version_ref,
        report_ref: input.source.report_ref || undefined,
      };
      else if (input.mode === "external_bq") request = {
        expected_revision: expected, mode: input.mode, discovery_kind: "warehouse_schema",
        access_ref: input.source.access_ref, object_ref: input.source.object_ref,
        declared_writer: input.source.declared_writer,
        readonly_acknowledged: input.source.readonly_acknowledged,
      };
      // `inbound_email` OBSERVES A FILE, exactly like an upload. THAT LINE STAYS
      // TRUE and 57.3 does not reverse it: an email is a TRANSPORT of the file,
      // not a source of its own, and the server already routes `inbound_email`
      // to `observe_first_delivery` under `file_schema`.
      //
      // What changed (57.3): `channel_contract` now has an observation function,
      // and it is the `webhook`'s -- that one has NO file at all until something
      // arrives, so its declared contract IS its observation. A draft carries a
      // single observation (`attached_source["observation_ref"]`), so the two
      // channels cannot ask for the same thing: the email keeps its file, the
      // webhook takes its contract. The channel contract itself is not an
      // observation -- it is a declaration by the operator, and it lives beside
      // it in `source.channel_contract`, never competing with it.
      else request = {
        expected_revision: expected, mode: input.mode,
        discovery_kind: input.source.channel === "google_sheets"
          ? "sheet_schema"
          : input.source.channel === "webhook" ? "channel_contract" : "file_schema",
        channel: input.source.channel,
        ...(input.source.source_account_ref ? { source_account_ref: input.source.source_account_ref } : {}),
        ...(input.source.template_ref ? { template_ref: input.source.template_ref } : {}),
        ...(input.source.staged_asset_ref ? { staged_asset_ref: input.source.staged_asset_ref } : {}),
        ...(input.source.sheet_ref ? { sheet_ref: input.source.sheet_ref } : {}),
      };
      const observed = await createDatastreamSetupObservation(
        cfg, draft.draft_ref, request, requestKey("observe"),
      );
      if (expected !== revision.current) return;
      setObservation(observed);
      revision.current = observed.draft_revision;
      setDraft((current) => current ? {
        ...current,
        current_revision: observed.draft_revision,
        current_revision_ref: observed.draft_revision_ref,
        operator_input: {
          ...input,
          source: { ...input.source, observation_ref: observed.observation_ref },
        },
      } : current);
      const next = { ...input, source: { ...input.source, observation_ref: observed.observation_ref } } as SetupInput;
      setInput(next);
      setSaved(true);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Discovery failed"); }
    finally { setBusy(false); }
  };

  const stageFile = async (file: File) => {
    if (!draft || input?.mode !== "managed_feed") return;
    setBusy(true); setError(null);
    try {
      const staged = await stageDatastreamSetupAsset(cfg, draft.draft_ref, file, requestKey("stage-file"));
      const next: FeedInput = {
        ...input,
        source: { ...input.source, staged_asset_ref: staged.asset_ref },
        configure: { ...input.configure, input_ref: staged.asset_ref },
      };
      setObservation(null); edit(next);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "File staging failed"); }
    finally { setBusy(false); }
  };

  const compile = async () => {
    if (!draft || !input || !observation) return;
    setBusy(true); setError(null);
    try {
      if (timer.current) clearTimeout(timer.current);
      const updated = await saveNow(input, "compile");
      const result = await compileDatastreamSetupDraft(
        cfg, updated?.draft_ref ?? draft.draft_ref, revision.current, requestKey("compile"),
      );
      setProposal(result);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Compilation failed"); }
    finally { setBusy(false); }
  };

  const safePreview = previewJob?.preview ?? null;

  const queuePreview = async () => {
    if (!draft || !proposal || !observation) return;
    setBusy(true); setError(null);
    try {
      const queued = await createDatastreamSetupPreview(
        cfg, draft.draft_ref, revision.current, proposal.proposal_ref,
        observation.observation_ref, requestKey("preview"),
      );
      setPreviewJob(queued);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Preview could not be queued"); }
    finally { setBusy(false); }
  };

  useEffect(() => {
    const jobId = previewJob?.job_id ?? previewJob?.job_ref;
    if (!draft || !jobId || !previewJob || !["queued", "running"].includes(previewJob.state)) return;
    let disposed = false;
    const timeout = setTimeout(() => {
      void readDatastreamSetupPreviewJob(cfg, draft.draft_ref, jobId)
        .then((status) => { if (!disposed) setPreviewJob(status); })
        .catch((reason) => {
          if (!disposed) {
            setError(reason instanceof Error ? reason.message : "Preview status is unavailable");
          }
        });
    }, 750);
    return () => { disposed = true; clearTimeout(timeout); };
  }, [cfg, draft, previewJob]);

  // The reporting timezone, read ONCE at mount (57.12, T5). It changes in
  // Project Settings, not in this draft, so nothing here re-reads it; a failed
  // read leaves the field stating where the value lives instead of inventing
  // one. Deliberately OUTSIDE the draft-load effect: a slow options read must
  // not hold the timezone hostage, and an edit has no business cancelling it.
  useEffect(() => {
    let disposed = false;
    readProjectReportingTimezone(cfg)
      .then((value) => { if (!disposed) setReportingTimezone(value); })
      .catch(() => { if (!disposed) setReportingTimezone(null); });
    return () => { disposed = true; };
  }, [cfg]);

  /** ONE GESTURE, TWO PHASES (57.12, T4).
   *
   *  The footer chained three buttons — `Freeze final review`, `Review and
   *  create Draft`, `Confirm create Draft` — three clicks for one decision,
   *  with machine words on each. The ratified ceremony is a review and a
   *  confirmation: the operator reads what will be created in the dialog,
   *  confirms once, and the three calls fire in order — `final-reviews`,
   *  `draft-confirmations`, `materialize` — each with its own idempotency key.
   *
   *  THE 409 PROTECTION IS PRESERVED. The server binds a confirmation to the
   *  idempotency key it was ISSUED with and refuses to consume it under any
   *  other (`entry_confirmations.py` `_consume`; `test_entry_confirmations.py`
   *  lists the mismatch among the overrides that must raise). This wizard once
   *  generated a fresh key per call, and "Confirm create Draft" was refused 409
   *  on every click — three review lenses found it independently on 2026-08-03.
   *  The key is therefore ONE LOCAL of the chain, held from issue to consume;
   *  it no longer needs to be state, because the two clicks are one gesture.
   *
   *  A failure names the PHASE that failed, because "could not be created"
   *  alone sends the operator looking at the wrong call. */
  const reviewAndCreateDraft = async () => {
    if (!draft || !safePreview) return;
    setBusy(true); setError(null);
    try {
      let prepared: DatastreamFinalReview;
      try {
        prepared = await prepareDatastreamFinalReview(
          cfg, draft.draft_ref, safePreview.preview_ref, warningAcks, requestKey("final-review"),
        );
      } catch (reason) {
        throw new Error(
          `The review could not be prepared: ${reason instanceof Error ? reason.message : "unavailable"}`,
        );
      }
      setFinalReview(prepared); setDraftConfirmation(null);
      // The SAME key at issue and at consume — a fresh one is refused 409.
      const key = requestKey("draft-confirmation");
      let confirmation: DatastreamConfirmation;
      try {
        confirmation = await prepareDatastreamDraftConfirmation(cfg, draft.draft_ref, prepared.final_review_ref, key);
      } catch (reason) {
        throw new Error(
          `The draft confirmation could not be issued: ${reason instanceof Error ? reason.message : "unavailable"}`,
        );
      }
      setDraftConfirmation(confirmation);
      let created: DatastreamMaterialization;
      try {
        created = await confirmDatastreamDraft(cfg, draft.draft_ref, prepared.final_review_ref, confirmation, key);
      } catch (reason) {
        throw new Error(
          `The Datastream Draft could not be created: ${reason instanceof Error ? reason.message : "unavailable"}`,
        );
      }
      setMaterialization(created); setDraftConfirmation(null);
      const matStatus = await readDatastreamMaterialization(cfg, draft.draft_ref);
      setMaterializationStatus(matStatus);
      sessionStorage.removeItem(`datastream-setup-return:${projectId}`);
      const newDsId = created.datastream_id ?? matStatus.datastream_ref;
      if (newDsId) {
        onCreated?.(newDsId);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Datastream Draft could not be created");
    } finally { setBusy(false); }
  };

  /** END THIS DRAFT (AI-336, ratified by Jean 2026-08-31).
   *
   *  The wizard's own end. `Save and exit` leaves a draft that stays resumable;
   *  this one says the person is not coming back. The server writes `archived` —
   *  a soft archive, never a row deletion — and the Project's single resumable
   *  slot is free the moment it answers, so the next `Add Datastream` starts on
   *  a clean first question.
   *
   *  The return key is forgotten by the ROUTE that owns it, through
   *  `onDiscarded`: writing it from here would make this the second writer of a
   *  key whose whole point is that there is one. */
  const discardDraft = async () => {
    if (!draft) return;
    setBusy(true); setError(null);
    try {
      await discardDatastreamSetupDraft(cfg, draft.draft_ref);
      setDiscardOpen(false);
      onDiscarded?.();
    } catch (reason) {
      setDiscardOpen(false);
      setError(reason instanceof Error ? reason.message : "This draft could not be discarded");
    } finally { setBusy(false); }
  };

  if (error && !draft) {
    return (
      <Status
        as="block"
        tone="error"
        title="Datastream setup unavailable"
        action={<Retry onClick={() => { setError(null); setDraftAttempt((value) => value + 1); }} />}
      >
        {error}
      </Status>
    );
  }
  if (!draft) return (
    <Status as="block" tone="neutral" active title="Creating setup draft">
      No discovery starts before this server-owned draft exists.
    </Status>
  );

  const connector = input?.mode === "connector_pull"
    ? options?.connectors.find((item) => item.connector_ref === input.source.connector_ref)
    : undefined;
  const report = input?.mode === "connector_pull"
    ? connector?.reports.find((item) => item.report_ref === input.source.report_ref)
    : undefined;
  /** What a field is CALLED, from the contract that declared it. Written once:
   *  the three field lists of `Configure` each asked the same question, and a
   *  list that fell back to the raw id in one of them would read as a different
   *  catalogue. */
  const fieldDescription = (fieldId: string) =>
    connector?.fields.find((field) => field.field_id === fieldId)?.description ?? fieldId;

  /** PROPOSED, never editable here. The category describes the PRODUCT the data
   *  comes from, so its one authority is the connector manifest; a Datastream
   *  screen that let it be typed would become a second authority that diverges
   *  at the next catalogue change. When it is wrong, the correction is an
   *  explicit source-type declaration, which wins over this derivation.
   *
   *  It blocks nothing: `external_bq` and `managed_feed` carry no module, so
   *  they carry no category, and gating on it would forbid two modes of three. */
  const sourceCategory = input?.mode === "connector_pull" ? connector?.source_category ?? null : null;
  const sourceCategoryHint = sourceCategory
    ? IDENTITY_COPY.categoryWithOrigin
    : IDENTITY_COPY.categoryReadOnly;
  const sourceCategoryPlaceholder = input?.mode !== "connector_pull"
    ? IDENTITY_COPY.categoryNoMode
    : connector
      ? IDENTITY_COPY.categoryNoneDeclared
      : IDENTITY_COPY.categoryNoConnector;
  /** The name the connector contract can propose: the connector and the report
   *  the operator just chose, from `source-options` — never a screen constant. */
  const proposedName = connector && report ? `${connector.display_name} — ${report.display_name}` : "";

  /** Choosing a report family is the moment a name becomes proposable, so the
   *  proposal lands in the SAME edit and the field says where it came from. A
   *  name the operator already typed is never re-authored. */
  const chooseReport = (reportRef: string) => {
    if (!input || input.mode !== "connector_pull") return;
    const chosen = connector?.reports.find((item) => item.report_ref === reportRef);
    const fromContract = connector && chosen ? `${connector.display_name} — ${chosen.display_name}` : "";
    const next = { ...input, name: input.name || fromContract, source: { ...input.source, report_ref: reportRef } };
    // FIRST CHOICE INVALIDATES NOTHING: the report family is a Required item
    // of `2. Configure` (`:184`), AFTER the discovery, which reads the
    // connector contract without one (`report_ref || undefined`). Evidence that
    // never named a report is not invalidated by naming one — and `editUpstream`
    // drops the observation even when it asks nothing, so a first choice must
    // be a plain `edit`. Only CHANGING an existing choice invalidates.
    if ((observation || proposal) && input.source.report_ref) {
      editUpstream(next, true, "Changing the report family invalidates dependent evidence. Continue?");
      return;
    }
    edit(next);
  };

  /** THE PRESET IS RESTORABLE, AND THE ACCIDENT THAT DEMANDED IT IS GONE.
   *
   *  Measured: a click WITHOUT `Ctrl` on a native `<select multiple>` replaced
   *  the whole selection, and the `Dimensions` control wrote the grain from the
   *  same event — so one keystroke could take away six dimensions and the grain
   *  with them. The two lists are CHECKBOXES now (57.12, T5): every change is
   *  one deliberate toggle, reversible by the same toggle, and nothing else
   *  moves. The restore stays because deviation is still possible — it is just
   *  no longer accidental: an operator who unchecked six declared fields on
   *  purpose still deserves the declared selection back in one click, and the
   *  notice above it counts the difference in words.
   *
   *  Both counts are DERIVED from the report, never from a remembered copy of
   *  what was applied: a second piece of state would disagree with the contract
   *  the day the contract changed. */
  const presetMetrics = report?.metrics ?? [];
  const presetDimensions = report?.dimensions ?? [];
  const chosenMetrics = input?.mode === "connector_pull" ? csvValues(input.configure.metrics) : [];
  const chosenDimensions = input?.mode === "connector_pull" ? csvValues(input.configure.dimensions) : [];
  const droppedFromPreset = presetMetrics.filter((field) => !chosenMetrics.includes(field)).length
    + presetDimensions.filter((field) => !chosenDimensions.includes(field)).length;
  const addedOverPreset = chosenMetrics.filter((field) => !presetMetrics.includes(field)).length
    + chosenDimensions.filter((field) => !presetDimensions.includes(field)).length;
  const presetDiffers = Boolean(report)
    && (chosenMetrics.length > 0 || chosenDimensions.length > 0)
    && (droppedFromPreset > 0 || addedOverPreset > 0);

  const resetToPreset = () => {
    if (!input || input.mode !== "connector_pull" || !report) return;
    const dimensions = report.dimensions.join(",");
    edit({
      ...input,
      configure: { ...input.configure, metrics: report.metrics.join(","), dimensions, grain: dimensions },
    });
  };

  /** ONE TOGGLE, ONE FIELD. The metrics list writes only itself; the dimensions
   *  list writes the grain from the same answer, exactly as the removed
   *  multi-select did — two rules for one value would let a preset and a click
   *  disagree about what the grain is. */
  const toggleMetricField = (fieldId: string, on: boolean) => {
    if (!input || input.mode !== "connector_pull") return;
    const next = on ? [...chosenMetrics, fieldId] : chosenMetrics.filter((field) => field !== fieldId);
    edit({ ...input, configure: { ...input.configure, metrics: next.join(",") } });
  };
  const toggleDimensionField = (fieldId: string, on: boolean) => {
    if (!input || input.mode !== "connector_pull") return;
    const next = on ? [...chosenDimensions, fieldId] : chosenDimensions.filter((field) => field !== fieldId);
    const dimensions = next.join(",");
    edit({ ...input, configure: { ...input.configure, dimensions, grain: dimensions } });
  };

  // `externalAccess`, `externalObjectIsRead` and `externalObjectHint` MOVED into
  // `SourceExternalBq.tsx` (57.12, T2b) — only that panel reads them.

  /** Choosing an access fills the object it names, in the SAME edit. A separate
   *  write would leave the two out of step for one render, and an operator would
   *  discover on a table they did not pick. A reference already typed is never
   *  re-authored, same rule as the name in 57.10. */
  const chooseExternalAccess = (accessRef: string) => {
    if (!input || input.mode !== "external_bq") return;
    const chosen = options?.external_access.find((account) => sourceAccountId(account) === accessRef);
    const named = chosen?.truncated === true ? "" : chosen?.external_object_ref ?? "";
    editUpstream(
      { ...input, source: { ...input.source, access_ref: accessRef, object_ref: named || input.source.object_ref } },
      Boolean(observation || proposal),
      "Changing BigQuery access invalidates dependent evidence. Continue?",
    );
  };

  /** Answering the Google Sheets channel's second question.
   *
   *  What is persisted is a `source_account_ref`, because the draft accepts no
   *  authorization key for `managed_feed` — so the chosen consent is written as
   *  its FIRST scope, and the choice among scopes is not withheld from the
   *  operator, it does not exist: `_sheets_connection_id` resolves any scope of
   *  a consent to the same `cr.id`, which is what the token is minted from.
   *
   *  A consent whose scopes are not on the wire cannot be written, and that is
   *  stated by the field rather than written as an empty reference. */
  const chooseSheetsAuthorization = (value: string) => {
    if (!input || input.mode !== "managed_feed") return;
    const chosen = sheetsAuthorizations.find((entry) => authorizationValue(entry) === value);
    const first = chosen?.accounts[0];
    editUpstream(
      {
        ...input,
        source: { ...input.source, source_account_ref: first ? sourceAccountId(first) : "" },
      },
      Boolean(observation || proposal),
      "Changing the Google authorization invalidates dependent evidence. Continue?",
    );
  };

  /** Picking a tab writes the SAME reference the operator would have typed —
   *  `{spreadsheetId}!{tabTitle}`, composed by the discovery that read the
   *  workbook, so the two ways of answering cannot disagree. It invalidates
   *  dependent evidence like every upstream change, and says so. */
  const chooseSheetTab = (objectRef: string) => {
    if (!input || input.mode !== "managed_feed") return;
    editUpstream(
      { ...input, source: { ...input.source, sheet_ref: objectRef } },
      Boolean(observation || proposal),
      "Changing the tab invalidates dependent evidence. Continue?",
    );
  };

  // The connector_pull derivations (`selectedAccount`, `authorizations`,
  // `activeAuthorization`, `accountChoices`, `connectorCards`,
  // `openedWithoutModule`) MOVED into `SourceConnectorPull.tsx` (57.12, T2b) —
  // only that panel reads them. What the wizard still owns is the state they
  // derived from (`pickedAuthorization`) and the callbacks that write it.

  /** The accounts this channel can use — through the SHARED predicate, never
   *  `connector_ref.id`, which for a Google direct grant is the provider string
   *  "google" and names no tool. This list was empty for every Google Sheets
   *  authorization that has ever existed in this deployment. */
  const sheetsAccounts = (options?.source_accounts ?? []).filter(
    (account) => accountServesConnector(account, "google-sheets"),
  );
  /** THIS CHANNEL ASKS FOR THE GRANT, NOT FOR A PRODUCT ACCOUNT.
   *
   *  The ratified companion says no Connector, no authorization and no account
   *  is asked on this path, "showing them would be asking about a product where
   *  the source is a channel" — and the screen asked for a `Source Account`
   *  anyway, because reading a private workbook needs a Google consent carrying
   *  `spreadsheets.readonly`. That select was the SAME collapse the amendment
   *  names for the Connector pull: one control answering two questions.
   *
   *  MEASURED, which is what makes the repair safe: the server never reads the
   *  account. `_sheets_connection_id` (`datastream_preconfiguration_api.py`)
   *  takes whatever `source_account_ref` it is given and selects `cr.id` from
   *  it — the connection the token is minted from — and every scope of one
   *  consent carries the same `connection_ref.id`. So the scope was never an
   *  answer: it was a detour to the grant, and which one was picked could not
   *  change a byte of what is read.
   *
   *  The operator therefore answers the GRANT. The draft keeps storing a
   *  `source_account_ref` because `_OPERATOR_SOURCE_KEYS["managed_feed"]` is a
   *  closed set that accepts no authorization key — so the answer is written as
   *  the first scope of the chosen consent, deterministically, and the read it
   *  produces is the same for every scope of that consent. */
  const sheetsAuthorizations = authorizationOptions(sheetsAccounts);
  const sheetsAuthorization = input?.mode === "managed_feed"
    && input.source.channel === "google_sheets"
    ? sheetsAuthorizations.find((entry) => entry.accounts.some(
      (account) => sourceAccountId(account) === input.source.source_account_ref,
    )) ?? null
    : null;
  /** `Discover source` IS NOT THE DOOR OF AN INBOUND CHANNEL, and requiring a
   *  `template_ref` for one was not a condition to patch — it was the wrong
   *  question. No Template is ever attached to `inbound_email` or `webhook`
   *  (`get_source_options` binds them to `file_upload` only), so the button was
   *  disabled FOREVER on both. What completes step 1 for an inbound channel is
   *  the declared contract, and the channel alone is enough to read it. */
  /** AND IT NO LONGER REQUIRES A PINNED CONTRACT (AI-279). Demanding
   *  `connector_contract_version_ref` here disabled the button for every
   *  Connector nobody had bound yet — which, with nothing pre-written, is all of
   *  them. The pin is what the binding PRODUCES; requiring it beforehand is what
   *  made 39 rows get written so the button would light up. */
  // THE REPORT FAMILY IS NOT REQUIRED HERE. It opens `Configure`, the section
  // right after `Source`, and `Configure` is itself locked behind the
  // observation. Requiring it here made discovery impossible — `report_ref`
  // already leaves as `|| undefined` in the request, and discovery reads the
  // connector CONTRACT, not a report.
  const canDiscover = input?.mode === "connector_pull"
    ? Boolean(input.source.source_account_ref && input.source.connector_ref)
    : input?.mode === "external_bq"
      ? Boolean(input.source.access_ref && input.source.object_ref
        && input.source.declared_writer && input.source.readonly_acknowledged)
      : input?.mode === "managed_feed"
        ? input.source.channel === "file_upload"
          ? Boolean(input.source.staged_asset_ref)
          : input.source.channel === "google_sheets"
            // THE GRANT AND THE WORKBOOK, which are this channel's two
            // questions. It read `source_account_ref` — the key the draft
            // stores — and the button then lit up for an answer nobody was
            // asked for; it now reads the answer the operator actually gives.
            ? Boolean(sheetsAuthorization && input.source.sheet_ref)
            : true
        : false;

  /** WHAT THIS DRAFT DIFFERS FROM THE TEMPLATE IT STARTED FROM (57.7).
   *
   *  Two differences and no third, because the measurement allows no third: the
   *  Source Account, which is the variable the gesture reopens, and the Project,
   *  which is where the geographic posture lives. The country is not one — 0 of
   *  133 report profiles declares a country filter, so a Datastream has no
   *  country to change. Computed on the client from the payload already
   *  received; no server field exists for it. */
  const currentAccountRef = input && input.mode !== "external_bq" ? input.source.source_account_ref : "";
  // The account line is drawn only when the SERVER said the account is open.
  // An `external_bq` template reopens none, and reporting "account not chosen"
  // for it would name a field that mode does not have.
  const accountIsOpen = (appliedTemplate?.open_variables ?? []).includes("source_account_ref");
  const templateDifference = appliedTemplate
    ? [
      appliedTemplate.label,
      !accountIsOpen
        ? null
        : !currentAccountRef
          ? "account not chosen"
          : currentAccountRef !== (appliedTemplate.origin_source_account_ref ?? "")
            ? "account changed"
            : null,
      appliedTemplate.origin_project_ref !== projectId ? "project differs" : null,
    ].filter(Boolean).join(" · ")
    : null;

  /** Saving the configuration of the Datastream this wizard just created.
   *
   *  HERE AND NOT ON THE WORKBENCH, and the reason is measured: only a
   *  Datastream materialized by this wizard carries a reusable operator input
   *  (`app.datastream_setup_materializations`), and this is the moment one
   *  exists and has just been validated. The Workbench `Overview` would be the
   *  second natural door and is named as missing in the ratified document.
   *
   *  ONE confirmation, not the two-step ceremony: saving writes no active
   *  object and is reversible — a template is retired, never destroyed. */
  const saveAsTemplate = async () => {
    const datastreamId = materialization?.datastream_id ?? materializationStatus?.datastream_ref;
    if (!datastreamId) return;
    setBusy(true); setTemplateError(null);
    try {
      const created = await saveDatastreamSetupTemplate(
        cfg, datastreamId, templateLabel.trim(), requestKey("save-template"),
      );
      setTemplateSaved(created);
      setTemplates((current) => current
        ? { ...current, templates: [created, ...current.templates], count: current.count + 1 }
        : current);
    } catch (reason) {
      // The server's own words, including the named refusals — the limit and
      // the Datastream nobody configured here. A generic sentence would hide
      // which of the two happened.
      setTemplateError(reason instanceof Error ? reason.message : "This configuration could not be saved");
    } finally {
      setBusy(false);
    }
  };

  // Section state is DERIVED, never a constant. Before this it was
  // `index === 0 ? "current" : "todo"`, so no section could ever read as done
  // and the rail was decoration over a form.
  //   Source      done once an observation exists for the current selection
  //   Configure   done once its mode-specific required intent is filled
  const configureComplete = input?.mode === "connector_pull"
    ? Boolean(input.configure.date_field && input.configure.metrics && input.configure.dimensions)
    : input?.mode === "external_bq"
      ? Boolean(input.configure.watermark_semantics && input.configure.logical_dataset_name)
      : input?.mode === "managed_feed"
        ? Boolean(input.configure.input_ref && input.configure.parsing_contract
          && input.configure.logical_dataset_name)
        : false;
  // Two different questions, kept apart: `done` answers "is this section's
  // evidence complete", `current` answers "where is the operator". Collapsing
  // them is what made the rail unusable -- the Stepper never offers the current
  // step as a control, so a rail whose current step is derived from evidence
  // can never be left.
  const mappingFields = proposal?.confirmed_intent_bundle?.field_mappings ?? [];
  const mappingComplete = Boolean(
    proposal && !proposal.is_stale && mappingFields.length > 0
    && proposal.sections.every((section) => section.items.every(
      (item) => item.requirement !== "required"
        || !["blocked", "missing", "needs_review"].includes(item.status),
    )),
  );
  // `Source` is settled when the source is OBSERVED and the two decisions that
  // belong to it are made. The role is what makes the refusal real: without it,
  // the section reads done and the operator only learns at materialization that
  // `data_role` is invalid. The source CATEGORY is deliberately absent from this
  // list -- it is read, not chosen, and two modes of three never carry one.
  //
  // ONE ENTRY PER SECTION OF `SECTIONS`, in its order. The removed third entry
  // was `Boolean(input)`, true from the moment a mode existed, so dropping it
  // changes no gate: what opened `Classify and map` before still opens it.
  const sectionComplete = [
    // `Source` IS THE WHOLE OF STEP 1, so its gate is the whole of step 1's
    // Required list (`:183`): the mode that decides which questions are asked,
    // the observation that proves the source answered, then the name and the
    // role. Splitting these into three rail entries did not make the gate
    // stricter — it only made two of the three stops that `:31` refuses.
    Boolean(input?.mode)
      && Boolean(observation)
      && Boolean(input?.name)
      && Boolean(input?.data_role),
    configureComplete,
    mappingComplete,
    safePreview?.status === "ready_for_review" && !safePreview.is_stale,
    Boolean(materialization || finalReview),
  ];
  const sectionStates: Array<"done" | "current" | "todo"> = sectionComplete.map(
    (complete, index) => (index === activeSection ? "current" : complete ? "done" : "todo"),
  );
  const warningIds = proposal?.sections.flatMap((section) => section.items
    .filter((item) => item.status === "warning" || item.status === "needs_review"
      || item.warnings.length > 0)
    .map((item) => item.key)) ?? [];
  const allWarningsAcknowledged = warningIds.every((warningId) => warningAcks.includes(warningId));
  /** The mode-specific sentence for "the read succeeded and found nothing".
   *  Never the adapter-failure sentence: a discovery that failed is an error the
   *  route reports (503, `observation_unavailable`) and the wizard renders in its
   *  own error status, naming the adapter. */
  const isSheetsChannel = input?.mode === "managed_feed" && input.source.channel === "google_sheets";
  const observationCoverage = observation?.coverage ?? {};
  const observationCodes = observation?.exceptions.map((item) => item.code) ?? [];
  const observedFieldCount = (observation?.safe_metadata.fields ?? []).length;
  /** The addressable objects the discovery listed — tabs, here. Read from the
   *  observation, never from a constant and never from a second route. */
  const sheetTabs = observation?.safe_metadata.objects ?? [];
  /** Three emptinesses for one channel, because they have three repairs: a
   *  reference that names no tab, a tab the workbook does not carry, and a first
   *  row that carries no usable name. None of them is a failure — the failure is
   *  the adapter's, and it names the adapter. */
  const sheetsEmptyState = observationCoverage.tab === "unnamed" ? (
    <Status as="block" tone="warning" title="This reference names a workbook, not a tab">
      {GOOGLE_SHEETS_COPY.tabUnnamed}
    </Status>
  ) : observationCoverage.tab === "not_found" ? (
    <Status as="block" tone="warning" title="This workbook carries no tab with that name">
      {GOOGLE_SHEETS_COPY.tabNotFound}
    </Status>
  ) : (
    <Status as="block" tone="neutral" title="This tab has no header row">
      {GOOGLE_SHEETS_COPY.headerUnusable}
    </Status>
  );
  /** The two inbound channels, and what this deployment says about them. The
   *  domain comes from the channel option — the SAME server read that decides
   *  `availability` — so the contract block and the channel list can never
   *  disagree about whether anything can be received. */
  const isInboundChannel = input?.mode === "managed_feed"
    && ["inbound_email", "webhook"].includes(input.source.channel);
  // `inboundOption`, `inboundDomain`, `channelContract`, `declaredSenders` and
  // `declaredArrival` MOVED into `SourceManagedFeed.tsx` (57.12, T2b) — only
  // that panel reads them.
  /** EXCEPT THIS ONE, which the Schedule step also reads: the arrival-monitor
   *  line says what the declaration armed. The declaration itself is edited in
   *  the panel; two readers of one value is not a second home for the rule. */
  const declaredArrival = input?.mode === "managed_feed"
    ? input.source.channel_contract?.expected_interval_minutes
    : undefined;
  const emptyDiscoveryState = input?.mode === "external_bq" ? (
    <Status as="block" tone="neutral" title="This authorization exposes no readable object">
      {EXTERNAL_BQ_COPY.emptySchema}
    </Status>
  ) : isInboundChannel ? (
    /* NOT A FAILURE, and the title says so before the sentence does. A channel
       that has received nothing is a channel waiting, and the repair belongs to
       whoever sends the first file — not to this screen and not to an adapter. */
    <Status as="block" tone="neutral" title="Nothing has arrived on this channel yet">
      {INBOUND_CHANNEL_COPY.emptyDelivery}
    </Status>
  ) : isSheetsChannel ? sheetsEmptyState : (
    <Status as="block" tone="neutral" title="No field was observed">
      The discovery read returned no column. Nothing is filled in from a guess.
    </Status>
  );

  /** What the header row is worth, said by CASE rather than by one hedge: a
   *  frozen header row is the sheet author's own declaration and needs no
   *  sentence, an assumption names the field that corrects it, and each way of
   *  being unusable names its own reason.
   *
   *  Said ONCE: when no column came back at all, the empty state above already
   *  carries the sentence, and printing it twice is how a screen stops being
   *  read. This notice belongs to the case where columns WERE listed and their
   *  header is still not to be trusted — a duplicated name, or a gap. */
  const sheetsHeaderNotice = observationCoverage.header === "assumed"
    ? { tone: "neutral" as const, title: "Row 1 is assumed to be the header", body: GOOGLE_SHEETS_COPY.headerAssumed }
    : observationCoverage.header !== "uncertain"
      ? null
      : observationCodes.includes("duplicate_headers")
        ? { tone: "warning" as const, title: "Row 1 repeats a column name", body: GOOGLE_SHEETS_COPY.headerDuplicate }
        : observationCodes.includes("header_row_incomplete")
          ? {
            tone: "warning" as const,
            title: "Row 1 has an unnamed column",
            body: GOOGLE_SHEETS_COPY.headerIncomplete,
          }
          : { tone: "neutral" as const, title: "This tab has no header row", body: GOOGLE_SHEETS_COPY.headerUnusable };

  /** ESTIMATE BEFORE SCAN, read before the click. The server refuses the same
   *  request with a 422 (`_create_preview`); the button is disabled on the same
   *  criterion so the operator meets the refusal here rather than after acting. */
  const previewNeedsEstimate = input?.mode === "external_bq"
    && Boolean(observation)
    && !observation?.safe_metadata.quota_cost;

  /** The Output the compiler decided, in the right panel (57.9).
   *
   *  The removed third panel claimed a "governed full-grain output
   *  intent" and displayed nothing. This reads the decision itself — kind and
   *  state — from the proposal, so it cannot promise an Output the compiler did
   *  not propose, and an empty list reads as an absence rather than as silence.
   *
   *  It does NOT carry the raw-landing policy. That value exists server-side
   *  (`datastream_preconfiguration.py` / `datastream_activation.py`) but reaches
   *  no client payload before creation, and re-deriving it from `mode` here
   *  would be a third home for a server rule. After creation it is read on the
   *  Workbench `Processing` tab. */
  const proposedOutputs = proposal?.confirmed_intent_bundle?.outputs ?? [];
  const outputSummary = !proposal
    // `:1147` FIXES THIS WORD: `Not compiled`, not a dash. Before compilation
    // the outputs are not unknown, they do not exist yet, and the two read
    // differently to whoever is deciding whether to compile.
    ? "Not compiled"
    : proposedOutputs.length === 0
      ? "None proposed"
      : proposedOutputs
          .map((output) => [
            humanKey(String(output.kind ?? "output")),
            String(output.state ?? "").replaceAll("_", " "),
          ].filter(Boolean).join(" — "))
          .join(", ");

  /** WHAT THE LAST STEP HAS IN FRONT OF IT, read once. Two different payloads
   *  answer "was it created" — the response of the create call and the polled
   *  status — and reading the pair at three sites is how one of them ends up
   *  saying yes while another says no. */
  /** What the cadence control is CALLED in this mode. Three names for one
   *  setting, because the three modes do not schedule the same act: a pull, a
   *  verification of somebody else's table, or the monitoring of an arrival
   *  nobody here triggers. */
  const cadenceLabel = input?.mode === "external_bq"
    ? "Verification cadence"
    : input?.mode === "managed_feed" && ["inbound_email", "webhook"].includes(input.source.channel)
      ? "Arrival monitoring"
      : "Cadence";

  const isMaterialized = Boolean(materialization) || materializationStatus?.state === "materialized";
  const createdDatastreamId = materialization?.datastream_id ?? materializationStatus?.datastream_ref ?? null;

  /** One section at a time — the target's "central task area" (`:31`). Every
   *  panel used to render at once on a single scrolling page, with the stepper
   *  only moving focus: stable review sections that all appear together are one
   *  section wearing several labels. */
  const sectionVisible = (index: number) => index === activeSection;

  const goToSection = (index: number) => {
    const bounded = Math.max(0, Math.min(SECTIONS.length - 1, index));
    setActiveSection(bounded);
    // The operator has moved, so the resumed-position notice has said its piece.
    setPositionUnknown(false);
    sectionRefs[bounded]?.current?.focus();
    if (input) {
      const incomplete = sectionComplete.findIndex((value) => !value);
      const firstIncomplete = incomplete < 0 ? "complete" : SECTIONS[incomplete].id;
      // BOTH KEYS, ALWAYS. The name is what this wizard reads back; the rank is
      // kept so an older build reading the same row still lands somewhere real.
      const next = {
        ...input,
        wizard_state: {
          active_section: bounded,
          active_section_ref: SECTIONS[bounded].id,
          first_incomplete: firstIncomplete,
        },
      } as SetupInput;
      setInput(next);
      void saveNow(next, "wizard_navigation").catch(
        (reason) => setError(reason instanceof Error ? reason.message : "Navigation state could not be saved"),
      );
    }
  };

  /** THE RIGHT-HAND PANEL, LINE BY LINE (`:31`).
   *
   *  It is not a recap read once at the end: it carries what the compiler has
   *  PROPOSED and what the operator has ANSWERED, as the answering happens. So
   *  it has to be readable at `Source`, where almost nothing is answered yet.
   *
   *  A DASH IS NOT AN ANSWER. It is the absence of one, drawn in the place of a
   *  value, and it leaves the operator to guess which of the five stops will
   *  end it. Every line therefore carries the gesture that fills it, named as a
   *  gesture and placed where the value will later sit.
   *
   *  `Automatic` IS THE LEGEND'S FOURTH LEVEL (`:41`): nobody decides these,
   *  the compiler or the Connector does — and they stay visible as evidence
   *  rather than disappearing because no one has to touch them.
   */
  type SummaryRow = {
    label: string;
    /** The answer, once there is one. `null` is "still waiting", never a dash. */
    value: ReactNode | null;
    /** What fills the line. A gesture and where it is made, never a state. */
    pending: string;
    /** No operator decision: compiled or derived, shown as evidence. */
    automatic?: boolean;
  };
  const summaryRows: SummaryRow[] = [
    {
      label: "Mode",
      value: input?.mode
        ? MODE_CHOICES.find((choice) => choice.value === input.mode)?.label ?? humanKey(input.mode)
        : null,
      pending: "Choose how this reads its source, at Source",
    },
    { label: "Name", value: input?.name || null, pending: "Name it at Source" },
    { label: "Role", value: input?.data_role || null, pending: "Choose a data role at Source" },
    // ONLY THE PULL HAS ONE. An external table or a delivered feed has no
    // Connector behind it, so the line would name a field that mode does not
    // have — the same reason the account line is drawn conditionally above.
    ...(input?.mode === "connector_pull" ? [{
      label: "Source category",
      value: sourceCategory ? humanKey(sourceCategory) : null,
      pending: "Comes with the Connector you pick at Source",
      automatic: true,
    }] : []),
    {
      label: "Domains",
      value: (input?.domain_ids ?? []).length || null,
      pending: "Confirm the Business Domains at Classify and map",
    },
    {
      label: "Fields",
      value: mappingFields.length || null,
      pending: "Compile the proposal at Configure",
    },
    {
      label: "Joint grain",
      value: (proposal?.confirmed_intent_bundle?.joint_grain ?? []).join(" / ") || null,
      pending: "Compile the proposal at Configure",
      automatic: true,
    },
    {
      label: "Output",
      value: proposal ? outputSummary : null,
      pending: "Not compiled",
      automatic: true,
    },
    {
      label: "Schedule",
      value: input?.schedule?.mode ? humanKey(input.schedule.mode) : null,
      pending: "Choose a cadence at Schedule and activate",
    },
    ...(ARRIVAL_HOUR_CADENCES.includes(input?.schedule?.mode ?? "manual") ? [{
      label: "Arrival hour",
      // Empty is a legal answer, so it is said in full rather than shown as
      // `00:00` — which would be a decision nobody made.
      value: input?.schedule?.arrival_hour === undefined
        ? null
        : arrivalHourLabel(input.schedule.arrival_hour),
      pending: "Not set — it runs at local midnight",
    }] : []),
    ...(appliedTemplate ? [{
      label: "From template",
      value: templateDifference,
      pending: "Apply a template at Configure",
    }] : []),
  ];

  {/* THE THREE-ZONE GRID IS RATIFIED (`:31`, amended 57.5), AND ALL THREE ARE
      DECLARED BELOW: stepper 210px, task area, `Configuration summary` 290px.
      The summary used to be folded into `Schedule and activate` and its column
      dropped, with no amendment to `:31` behind it. It is now a zone of its
      own, mounted at every section — what it carries, the compiler's
      proposals and the operator's answers, only helps while the answering is
      still going on.

      BELOW 1280px THERE IS NO THIRD COLUMN TO GIVE IT. The summary keeps its
      place in the reading order and follows the task area as a block, above
      the footer; the stepper and the task area keep the widths they already
      had, so nothing that fits today stops fitting.

      THE MEASUREMENT EVERY "one column" comment elsewhere points at: at
      1280px viewport the task area is ~372px wide once rail, summary, gaps
      and padding come out — so two cards abreast leave ~178px each, too
      narrow for a title with a sentence, and every block of this screen
      stacks in one column (57.12, T6: said once, here, where the widths are
      declared). */}
  return <div
    className="grid min-h-[680px] grid-cols-[210px_minmax(0,1fr)] gap-5 p-6
      xl:grid-cols-[210px_minmax(0,1fr)_290px]"
  >
    <aside aria-label="Datastream setup sections">
      <Stepper
        steps={SECTIONS.map((section, index) => ({
          label: section.label,
          detail: section.detail,
          // NO `automatic` STEP LEFT. The only section that carried the flag was
          // the removed third one: each of the five now asks for something, so a
          // rail entry nobody can click would be a stop that is not one.
          state: sectionStates[index],
          // Completed sections are directly revisitable, which the ratified
          // wizard document requires and the `Stepper` primitive already
          // supports; this rail simply never passed the handler.
          // Reachable = every section before it is complete. That is what lets
          // the operator move FORWARD once the evidence allows, and back into
          // anything already settled. The Stepper itself declines to make the
          // current step clickable.
          onSelect: index === 0 || sectionComplete.slice(0, index).every(Boolean)
            ? () => goToSection(index)
            : undefined,
        }))}
      />
    </aside>
    <main className="min-w-0 grid content-start gap-6">
      {/* ONE HEADING PRIMITIVE, at the rank this screen actually holds: the
          route's own `<h1>` is `Add Datastream`, so the step is its `h2`. It was
          a hand-written `<h2>` beside a `<p>`, which is a second way of titling
          a screen that already has `SectionHeader` and `PageHeader`. */}
      <SectionHeader
        level={2}
        title={SECTIONS[activeSection].label}
        description={`${SECTIONS[activeSection].detail}. ${SECTIONS[activeSection].intro} `
          + "Nothing here takes effect until you confirm it."}
      />
      {/* A POSITION THAT NO LONGER EXISTS IS SAID, NOT ABSORBED. A draft saved by
          a build with a sixth section reopens here on `Source`; without this the
          operator would simply find themselves at the beginning and conclude
          their work was lost. */}
      {positionUnknown && (
        <Status as="block" tone="warning" title={RESUME_COPY.unknownTitle}>
          {RESUME_COPY.unknownBody}
        </Status>
      )}
      {/* ONE FACT, ONE NAME, ONE PLACE (76-4). This refusal was rendered twice
          — `No scan estimate` inside `Configure`, and `A read cannot be launched
          without its scan estimate` inside `Preview and validate` — over the
          same sentence. Only one section is drawn at a time
          (`sectionVisible`, l.2286), so the two never met on screen, which is
          why one fact wearing two names went unnoticed for as long as it did.

          It is true from the moment the observation comes back without a quota
          estimate until discovery is re-run, which is not the property of any
          one step; so it is said ONCE, here, above the step — the banner §5
          asks for — and it carries the only gesture that clears it. */}
      {previewNeedsEstimate && (
        <Status
          as="block"
          tone="error"
          title="A read cannot be launched without its scan estimate"
          data-testid="preview-needs-estimate"
          action={activeSection === SECTION_INDEX.source ? undefined : (
            <Button variant="secondary" onClick={() => goToSection(SECTION_INDEX.source)}>
              Go to Source
            </Button>
          )}
        >
          {EXTERNAL_BQ_COPY.previewNeedsEstimate}
        </Status>
      )}
      {/* `1. Source` IS ONE STOP AND FOUR QUESTIONS (`:183`), asked in the
          ratified order and revealed one at a time: the mode, then only that
          mode's own source questions, then — once the source has actually
          answered — the name and the data role. Each answer is what makes the
          next question appear, which is the opposite of the single form this
          step used to be; and it is also the opposite of giving each question
          a rail entry of its own, which `:31` refuses in as many words. */}
      {sectionVisible(SECTION_INDEX.source) && <div
        ref={sectionRefs[SECTION_INDEX.source]}
        tabIndex={-1}
        aria-label="Source section"
        className="outline-none grid gap-6"
      >
      {/* THE FAILED READ IS SAID AT THE TOP OF THE STEP IT BLOCKS. It used to
          be a line inside the name-and-role panel, which is the last thing
          this step draws and is not drawn at all before the source answers —
          so the one screen state where the read had failed was also the one
          where nobody could see it said so. What the read feeds is the
          Connector grid and the account list, three questions above. */}
      {optionsFailed && (
        <Status as="block" tone="error" title="Source options could not be read"
          action={<Retry onClick={() => void reloadSourceOptions()} />}
        >
          {IDENTITY_COPY.optionsFailed}
        </Status>
      )}
      <Panel className="grid gap-5 p-5">
        {/* ONE COLUMN for the step's content — the measurement is at the grid
            declaration: two abreast would leave ~178px for an icon, a title
            and a sentence. The Mode row itself is the compact cut of the card
            (ChoiceGroup `density="compact"`), three abreast WITHOUT prose. */}
        <Field label="Mode" required>
          {({ id }) => (
            <>
            <ChoiceGroup
              id={id}
              variant="card"
              aria-label="Mode"
              // THREE ABREAST, compact (2026-08-10, UX pass): the ratified
              // three zones leave ~372px of task area at 1280px, and three
              // DETAILED cards there collapse to one word per line (measured
              // live). The mockup's sentence is not lost — it answers under
              // the row, for the mode the operator is standing on.
              density="compact"
              // One column of compact rows at 1280 (a ~110px card truncates
              // "External BigQuery" to "Ext…", measured live), three abreast
              // from the canonical 1440 composition up. Hints are stripped
              // from the cards: the caption under the row carries the
              // selected mode's sentence.
              className="sm:grid-cols-1 min-[1440px]:grid-cols-3"
              // `""`, never `undefined`: a group that starts uncontrolled and
              // becomes controlled at the first choice is a React warning and a
              // lost keyboard state, and no mode is chosen when the step opens.
              value={input?.mode ?? ""}
              onValueChange={(value) => selectMode(value as Mode)}
              choices={MODE_CHOICES_COMPACT}
            />
            {/* The mockup's sentence, under the row instead of inside a card
                squeezed to one word per line (57.12, UX pass): it answers for
                the mode the operator is standing on. */}
            <p className="mt-2 text-caption text-text-secondary" aria-live="polite">
              {MODE_CHOICES.find((choice) => choice.value === input?.mode)?.hint
                ?? "Three ways a Datastream reads its source."}
            </p>
            </>
          )}
        </Field>
      </Panel>

      {/* QUESTION 2 ONLY EXISTS ONCE QUESTION 1 IS ANSWERED. Until a mode is
          chosen there is no panel here at all — not a disabled one, not an
          empty one: the three modes ask three different things, and a form
          shown before the mode would be all three at once with most of it
          switched off. `input` is created by `selectMode`, so its presence IS
          the answer to the first question.

          THE MODE'S OWN QUESTIONS, EXTRACTED (57.12, T2b). One file per mode
          under `preconfiguration/`; the state, the callbacks and the
          confirmations stay HERE — the panels receive values and write
          nothing themselves. */}
      {input && <Panel className="grid gap-5 p-5">
        {input?.mode === "connector_pull" && (
          <SourceConnectorPull
            input={input}
            options={options}
            connector={connector}
            pickedAuthorization={pickedAuthorization}
            projectId={projectId}
            narrowNotice={narrowNotice}
            appliedTemplate={appliedTemplate}
            savedAccountGoneCopy={TEMPLATE_COPY.accountGone}
            onChooseConnector={chooseConnector}
            onChooseAuthorization={chooseAuthorization}
            onChooseSourceAccount={chooseSourceAccount}
            onAccountVerified={() => void reloadSourceOptions()}
          />
        )}
        {input?.mode === "external_bq" && (
          <SourceExternalBq
            input={input}
            options={options}
            hasDependentEvidence={Boolean(observation || proposal)}
            cfg={cfg}
            onChooseAccess={chooseExternalAccess}
            onEditUpstream={editUpstream}
            onEdit={edit}
          />
        )}
        {input?.mode === "managed_feed" && (
          <SourceManagedFeed
            input={input}
            options={options}
            busy={busy}
            hasDependentEvidence={Boolean(observation || proposal)}
            sheetsAccounts={sheetsAccounts}
            sheetsAuthorizations={sheetsAuthorizations}
            sheetsAuthorization={sheetsAuthorization}
            observation={observation}
            sheetTabs={sheetTabs}
            observationCoverage={observationCoverage}
            onEditUpstream={editUpstream}
            onStageFile={(file) => void stageFile(file)}
            onChooseSheetsAuthorization={chooseSheetsAuthorization}
            onChooseSheetTab={chooseSheetTab}
            onEditChannelContract={editChannelContract}
          />
        )}
        {/* `secondary`, never the rose. The footer ALREADY carries the one
            recommended action of the step, and the primitive says why: "two rose
            buttons side by side are two recommendations, therefore none"
            (button.tsx). Discovering the source is a panel action, not the
            direction of the screen. */}
        <Button variant="secondary" onClick={() => void discover()} disabled={!canDiscover || busy}>
          {busy ? "Discovering..." : "Discover source"}
        </Button>
      </Panel>}

      {/* THE LAST TWO QUESTIONS OF STEP 1, and they come last because `:183`
          puts them last: "Datastream name and data role — in that order, the
          one ratified above", Required in all three modes. They wait for the
          source to have answered, so the name can arrive proposed rather than
          blank; a draft that already carries either one shows them whatever
          the source has done, because hiding an answer someone already gave
          would read as losing it.

          The role is what pairs with the manifest category to decide whether
          this Datastream ever enters the cost cascade
          (`fee_tax_source_types.py:99`); materialized without one it carries
          `Operational`, matches no rule, and says only that the signals "do
          not agree". */}
      {input && (observation || input.name || input.data_role) && (
        <Panel className="grid gap-4 p-5">
          <PanelHeader title="Name and role" description={IDENTITY_COPY.section} />
          {/* ONE FIELD PER ROW, like every other block of the task area
              (the measurement is at the grid declaration). */}
          <div className="grid gap-4">
            <Field
              label="Datastream name"
              required
              hint={proposedName ? IDENTITY_COPY.nameFromContract : IDENTITY_COPY.nameFree}
            >
              {(props) => (
                <Input
                  {...props}
                  value={input.name ?? ""}
                  onChange={(event) => edit({ ...input, name: event.target.value })}
                />
              )}
            </Field>
            <Field label="Data role" required hint={IDENTITY_COPY.roleHint}>
              {({ id }) => (
                <NativeSelect
                  id={id}
                  value={input.data_role ?? ""}
                  onChange={(event) => edit({ ...input, data_role: event.target.value })}
                >
                  <option value="">{IDENTITY_COPY.roleEmptyOption}</option>
                  {DATA_ROLES.map(([value, sentence]) => (
                    <option key={value} value={value}>{`${value} — ${sentence}`}</option>
                  ))}
                </NativeSelect>
              )}
            </Field>
            <Field label="Source category" hint={sourceCategoryHint}>
              {(props) => (
                <Input
                  {...props}
                  readOnly
                  // The manifest key is `analytics_product`; a person reads
                  // "Analytics product" (57.12, UX pass — the summary already
                  // humanizes, the field was the leftover).
                  value={sourceCategory ? humanKey(sourceCategory) : ""}
                  placeholder={sourceCategoryPlaceholder}
                />
              )}
            </Field>
          </div>
          {!input.data_role && !observation && (
            <Status as="block" tone="neutral" title="No data role proposed">
              {IDENTITY_COPY.roleUnproposable}
            </Status>
          )}
          {!input.data_role && observation && (
            <Status as="block" tone="error" title="Data role required to continue">
              {IDENTITY_COPY.roleRequired}
            </Status>
          )}
          {observation && !input.name && (
            <Status as="block" tone="error" title="Datastream name required to continue">
              {IDENTITY_COPY.nameRequired}
            </Status>
          )}
        </Panel>
      )}
      </div>}

      {sectionVisible(SECTION_INDEX.configure) && <div
        ref={sectionRefs[SECTION_INDEX.configure]}
        tabIndex={-1}
        aria-label="Configure section"
        className="outline-none"
      >
      {input && <Panel className="grid gap-5 p-5">
        {/* THE TEMPLATE OPENS THE STEP WHOSE FIELDS IT FILLS. `:184` makes the
            report and the fields the Required list of `2. Configure`, and a
            saved configuration or a declared preset is HOW that list gets
            filled — so it is asked here, not as the zeroth question of the
            Source step, where it stood before anything at all was chosen.
            Applying one still replaces the mode and every selection of the
            draft, and says so in its own confirmation. */}
        <OrganizationTemplates
          list={templates}
          connectors={options?.connectors ?? []}
          accounts={options?.source_accounts ?? []}
          accountsLoaded={Boolean(options)}
          failed={templatesFailed}
          currentProjectId={projectId}
          appliedRef={appliedTemplate?.template_ref ?? null}
          mode={input?.mode ?? null}
          onApply={applyOrganizationTemplate}
        />
        {/* ONE FIELD PER ROW — two columns are narrower than the shortest
            value any of these controls holds (measured at the grid). */}
        {input.mode === "connector_pull" && <div className="grid gap-4">
          {/* QUESTION 4 OF THE RATIFIED ORDER, AT THE TOP OF CONFIGURE: the
              starting point or the report family. Both used to be read before
              the Connector, then inside Source; they are what fills the fields
              below, so they open this step. */}
          <RecommendedStarts
            points={startingPoints}
            platformDefaults={options?.platform_defaults}
            selectedKey={selectedStartingPoint}
            onSelect={applyStartingPoint}
          />
          {connector && (
          <Field label="Report family" required>
            {({ id }) => (
              <NativeSelect
                id={id}
                value={input.source.report_ref}
                onChange={(event) => chooseReport(event.target.value)}
              >
                <option value="">Select a compatible report</option>
                {connector.reports.map((item) => (
                  <option
                    key={item.report_ref}
                    value={item.report_ref}
                    disabled={(item.availability as { status?: string } | null)?.status !== "selectable"}
                  >
                    {item.display_name}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          )}
          {connector && (connector.contract_state ?? "verified") === "unverified" && (
            <Status as="block" tone="info" title="No contract pinned yet — declared by the Connector">
              {STARTING_POINT_STATE.unverified.sentence}
            </Status>
          )}
          {connector && (connector.contract_state ?? "verified") === "verified" && (
            <Status as="block" tone="neutral" title={`Contract ${connector.contract_version_ref}`}>
              Exact persisted Connector contract; no client provider dictionary is used.
            </Status>
          )}
          {connector?.contract_state === "stale" && (
            <Status
              as="block"
              tone="warning"
              title={`Contract ${connector.contract_version_ref} is behind the Connector`}
            >
              This Connector declares a different contract from the one pinned here. What is listed is
              what it declares today; Datastreams bound to the pinned contract keep it until they
              are re-bound.
            </Status>
          )}
          <Field label="Date field" required>
            {({ id }) => (
              <NativeSelect
                id={id}
                value={input.configure.date_field}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, date_field: event.target.value },
                })}
              >
                <option value="">Select from the contract</option>
                {(report?.dimensions ?? [])
                  .filter((fieldId) => connector?.fields.some((field) => field.field_id === fieldId
                    && (field.physical_type === "date" || field.field_id.toLowerCase().includes("date"))))
                  .map((fieldId) => (
                    <option key={fieldId} value={fieldId}>{fieldDescription(fieldId)}</option>
                  ))}
              </NativeSelect>
            )}
          </Field>
          {/* CHECKBOXES, NOT A MULTI-SELECT (57.12, T5). A click without Ctrl
              on `<select multiple>` replaced the whole selection; a checkbox
              toggles exactly one field and is reversible by the same click. The
              group carries the Field's label; each box is named by the
              contract's own description of its field. */}
          <Field label="Metrics" required>
            {({ id }) => (
              <div id={id} role="group" aria-label="Metrics" className="grid gap-2">
                {(report?.metrics ?? []).map((fieldId) => (
                  <label key={fieldId} className="flex items-center gap-2 text-body text-text">
                    <Checkbox
                      checked={chosenMetrics.includes(fieldId)}
                      onCheckedChange={(checked) => toggleMetricField(fieldId, checked === true)}
                      aria-label={`${fieldDescription(fieldId)} (metric)`}
                    />
                    {fieldDescription(fieldId)}
                  </label>
                ))}
              </div>
            )}
          </Field>
          <Field label="Dimensions" required>
            {({ id }) => (
              <div id={id} role="group" aria-label="Dimensions" className="grid gap-2">
                {(report?.dimensions ?? []).map((fieldId) => (
                  <label key={fieldId} className="flex items-center gap-2 text-body text-text">
                    <Checkbox
                      checked={chosenDimensions.includes(fieldId)}
                      onCheckedChange={(checked) => toggleDimensionField(fieldId, checked === true)}
                      aria-label={`${fieldDescription(fieldId)} (dimension)`}
                    />
                    {fieldDescription(fieldId)}
                  </label>
                ))}
              </div>
            )}
          </Field>
          <Field label="Resulting grain">
            {(props) => (
              <Input
                {...props}
                value={input.configure.grain}
                readOnly
                placeholder="Derived from selected dimensions"
              />
            )}
          </Field>
          <Field label="Date window" error={fieldErrors.date_window}>
            {(props) => (
              <Input
                {...props}
                value={input.configure.date_window}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, date_window: event.target.value },
                })}
                onBlur={(event) => validateStructuredField("date_window", event.target.value)}
                placeholder="Contract-bounded window"
              />
            )}
          </Field>
          <Field label="Filters" error={fieldErrors.filters}>
            {(props) => (
              <Input
                {...props}
                value={input.configure.filters}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, filters: event.target.value },
                })}
                onBlur={(event) => validateStructuredField("filters", event.target.value)}
                placeholder="Contract-supported filters"
              />
            )}
          </Field>
          <Field label="History intent" error={fieldErrors.history_intent}>
            {(props) => (
              <Input
                {...props}
                value={input.configure.history_intent}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, history_intent: event.target.value },
                })}
                onBlur={(event) => validateStructuredField("history_intent", event.target.value)}
                placeholder={metadataText(report?.history)}
              />
            )}
          </Field>
          {/* NO CADENCE HERE (57.12, T3). The step asked it twice: this select,
              and the Schedule step's own. The ratified table
              (`datastream-workbench-and-wizard.md:111,114`) names the cadence
              Required at step 5 ONLY, so the question lives there and nowhere
              else. What the compiler read here (`configure.cadence_intent`) is
              now fed by the Schedule answer — `datastream_preconfiguration.py`
              reads `operator_input.schedule.mode` first and keeps
              `cadence_intent` as the fallback for drafts written before this
              removal, so an old draft still compiles to what its operator
              chose. Changing the cadence at Schedule re-fingerprints the whole
              `operator_input`, which is what already marks the proposal stale
              (`_dependencies`), and `edit()` drops it client-side: no new
              invalidation path was needed. */}
          {/* DEVIATION IS COUNTED, NOT JUDGED (57.12, T5). The accident is gone
              with the multi-select; what remains is the honest sentence — how
              this selection differs from the declared preset — and the one-click
              way back for an operator who deviated on purpose. */}
          {presetDiffers && (
            <Status
              as="block"
              tone="neutral"
              title="This selection differs from the Connector's declared preset"
            >
              {droppedFromPreset} declared field{droppedFromPreset === 1 ? "" : "s"} not selected,
              {" "}{addedOverPreset} added on top.
              <Button variant="secondary" onClick={resetToPreset}>Back to the preset selection</Button>
            </Status>
          )}
          <Status
            as="block"
            tone={report?.history && report?.quota_cost && report?.cadence ? "neutral" : "warning"}
            title="Contract evidence"
          >
            History: {metadataText(report?.history)}. Cadence: {metadataText(report?.cadence)}.
            Quota/cost: {metadataText(report?.quota_cost)}.
          </Status>
        </div>}
        {input.mode === "external_bq" && <div className="grid gap-4">
          <Field label="Watermark semantics" required>
            {(props) => (
              <Input
                {...props}
                value={input.configure.watermark_semantics}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, watermark_semantics: event.target.value },
                })}
              />
            )}
          </Field>
          <Field label="Logical dataset name" required>
            {(props) => (
              <Input
                {...props}
                value={input.configure.logical_dataset_name}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, logical_dataset_name: event.target.value },
                })}
              />
            )}
          </Field>
          <Field label="Expected freshness">
            {(props) => (
              <Input
                {...props}
                value={input.configure.expected_freshness}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, expected_freshness: event.target.value },
                })}
              />
            )}
          </Field>
          <Field label="Verification window">
            {(props) => (
              <Input
                {...props}
                value={input.configure.verification_window}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, verification_window: event.target.value },
                })}
              />
            )}
          </Field>
          <Field label="Expected history">
            {(props) => (
              <Input
                {...props}
                value={input.configure.expected_history}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, expected_history: event.target.value },
                })}
              />
            )}
          </Field>
          <Field label="Row filters">
            {(props) => (
              <Input
                {...props}
                value={input.configure.row_filters}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, row_filters: event.target.value },
                })}
              />
            )}
          </Field>
        </div>}
        {input.mode === "managed_feed" && <div className="grid gap-4">
          <Field label="Input reference" required>
            {(props) => (
              <Input
                {...props}
                value={input.configure.input_ref}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, input_ref: event.target.value },
                })}
              />
            )}
          </Field>
          <Field label="Parsing contract" required error={fieldErrors.parsing_contract}>
            {(props) => (
              <Input
                {...props}
                value={input.configure.parsing_contract}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, parsing_contract: event.target.value },
                })}
                onBlur={(event) => validateStructuredField("parsing_contract", event.target.value)}
              />
            )}
          </Field>
          <Field label="Logical dataset name" required>
            {(props) => (
              <Input
                {...props}
                value={input.configure.logical_dataset_name}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, logical_dataset_name: event.target.value },
                })}
              />
            )}
          </Field>
          <Field label="Date semantics">
            {(props) => (
              <Input
                {...props}
                value={input.configure.date_semantics}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, date_semantics: event.target.value },
                })}
              />
            )}
          </Field>
          <Field label="Write mode">
            {({ id }) => (
              <NativeSelect
                id={id}
                value={input.configure.write_mode}
                onChange={(event) => edit({
                  ...input,
                  configure: { ...input.configure, write_mode: event.target.value as "replace" },
                })}
              >
                <option value="replace">Replace</option>
                <option value="append" disabled>Append — unavailable without a durable key</option>
              </NativeSelect>
            )}
          </Field>
        </div>}
        {/* THE SCHEMA THE OBSERVATION READ, drawn at last. It was on the wire and
            nothing rendered it, so this step showed six free inputs and could not
            state a grain. Shared with 57.2 and 57.3: one shape of evidence, one
            rendering. */}
        {observation && <DiscoveredSchema observation={observation} emptyState={emptyDiscoveryState} />}
        {/* WHAT A SHEET CANNOT DECLARE, said beside what it did. These sentences
            live here rather than in `DiscoveredSchema` because they are the
            channel's own, and the shared block already leaves what differs by
            mode to its caller. Each one is conditioned on a coverage key: none
            is printed on faith. */}
        {observation && isSheetsChannel && sheetsHeaderNotice && observedFieldCount > 0 && (
          <Status as="block" tone={sheetsHeaderNotice.tone} title={sheetsHeaderNotice.title}>
            {sheetsHeaderNotice.body}
          </Status>
        )}
        {observation && isSheetsChannel && observedFieldCount > 0 && (
          <Status as="block" tone="neutral" title="Columns inferred from the header row">
            {GOOGLE_SHEETS_COPY.columnsInferred}
          </Status>
        )}
        {observation && isSheetsChannel && observationCoverage.types === "not_inferred" && (
          <Status as="block" tone="neutral" title="No column type is declared by a spreadsheet">
            {GOOGLE_SHEETS_COPY.typesNotInferred}
          </Status>
        )}
        {observation && isSheetsChannel && observationCoverage.grid === "available" && (
          <Status as="block" tone="neutral" title="Grid size">
            {GOOGLE_SHEETS_COPY.gridHeight(String(observation.safe_metadata.row_count_bucket ?? ""))}
          </Status>
        )}
        {/* ONE FACT, ONE NAME (76-4). This step said the same sentence as
            `Preview and validate` below, under a second title (`No scan
            estimate`), so a person who met both read two problems. It is said
            once, where the refusal actually bites: on the step whose primary
            button is disabled by the very same criterion. */}
        {/* Quota/cost left this line: `DiscoveredSchema` above owns it for every
            mode now, and printing the same evidence twice — once as a sentence,
            once as a stringified record — is how a screen stops being read. */}
        {observation && (
          <Status
            as="block"
            tone={observation.exceptions.length ? "warning" : "success"}
            title={`Observation ${observation.observation_ref}`}
          >
            <span>
              History: {metadataText(observation.safe_metadata.history)}.
              Cadence: {metadataText(observation.safe_metadata.cadence)}.
            </span>
          </Status>
        )}
      </Panel>}
      </div>}

      {sectionVisible(SECTION_INDEX.classify_and_map) && <div
        ref={sectionRefs[SECTION_INDEX.classify_and_map]}
        tabIndex={-1}
        aria-label="Classify and map section"
        className="grid gap-5 outline-none"
      >
        {input && <Panel className="grid gap-5 p-5">
          {/* The name and the data role left this section for `Source` (57.10):
              a field that decides which cost rules can match must be asked when
              the source is chosen, not three sections later. What stays here is
              what the compiler actually produces evidence for. */}

          <div className="grid gap-4">
            <Field
              label="Business Domain IDs"
              hint="Canonical Project-scoped identities, comma separated"
              error={fieldErrors.domain_ids}
            >
              {(props) => (
                <Input
                  {...props}
                  value={(input.domain_ids ?? []).join(", ")}
                  onChange={(event) => edit({
                    ...input,
                    domain_ids: event.target.value.split(",").map((value) => value.trim()).filter(Boolean),
                  })}
                  onBlur={(event) => validateStructuredField("domain_ids", event.target.value)}
                />
              )}
            </Field>
            <Field label="Full joint grain">
              {(props) => (
                <Input
                  {...props}
                  readOnly
                  value={(proposal?.confirmed_intent_bundle?.joint_grain ?? []).join(" / ")}
                  placeholder="Compile to derive the complete grain"
                />
              )}
            </Field>
          </div>
          {!proposal && (
            <Status as="block" tone="warning" title="Proposal required">
              Compile the persisted observation to review normalized mappings and governance effects.
            </Status>
          )}
          {proposal && <>
            <TableScroll label="Normalized field mapping">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Source field</TableHead>
                    <TableHead>Role / semantic type</TableHead>
                    <TableHead>Target</TableHead>
                    <TableHead>Aggregation</TableHead>
                    <TableHead>Sensitivity</TableHead>
                    <TableHead>Included</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {mappingFields.map((field, index) => (
                    <TableRow key={String(field.source_field_id ?? field.source_identity ?? index)}>
                      <TableCell className="font-mono text-caption">
                        {displayProposalValue(field.source_identity ?? field.source_field_id)}
                      </TableCell>
                      <TableCell>
                        {displayProposalValue(field.role)} / {displayProposalValue(field.semantic_type)}
                      </TableCell>
                      <TableCell>{displayProposalValue(field.canonical_target ?? field.target)}</TableCell>
                      <TableCell>{displayProposalValue(field.aggregation)}</TableCell>
                      <TableCell>{displayProposalValue(field.sensitivity)}</TableCell>
                      <TableCell>{field.included === false ? "No" : "Yes"}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
            {!mappingComplete && (
              <Status as="block" tone="error" title="Mapping review is blocked">
                Every required field must have complete identity, classification and inclusion
                evidence before preview.
              </Status>
            )}
          </>}
        </Panel>}
        {proposal && <ProposalReview proposal={proposal} onOpenOwner={onOpenOwner} />}
      </div>}

      {sectionVisible(SECTION_INDEX.preview_validate) && <div
        ref={sectionRefs[SECTION_INDEX.preview_validate]}
        tabIndex={-1}
        aria-label="Preview and validate section"
        className="outline-none"
      >
        <Panel className="grid gap-5 p-5">
          {!previewJob && (
            <Status as="block" tone="neutral" title="No preview yet">
              Queue a mode-specific preview after the mapping is complete.
            </Status>
          )}
          {previewJob && ["queued", "running"].includes(previewJob.state) && (
            <Status as="block" tone="neutral" active title="Preview in progress">
              Shared worker state: {previewJob.state}. This page polls the durable job without
              resubmitting it.
            </Status>
          )}
          {previewJob && ["failed", "dead_letter"].includes(previewJob.state) && (
            <Status as="block" tone="error" title="Preview failed">
              {previewJob.error_code ?? "The registered mode adapter did not produce valid evidence."}
            </Status>
          )}
          {safePreview && <div className="grid gap-4">
            <Status
              as="block"
              tone={safePreview.status === "ready_for_review" && !safePreview.is_stale
                ? "success"
                : "error"}
              title={safePreview.status === "ready_for_review"
                ? "Masked preview ready"
                : "Preview blocked"}
            >
              Evidence {safePreview.evidence_hash.slice(0, 12)} is pinned to revision
              {" "}{safePreview.draft_revision_ref}.
            </Status>
            {/* ONE EVIDENCE PANEL PER ROW, for the width this task area has. */}
            <div className="grid gap-4">
              {Object.entries(safePreview.safe_evidence)
                .filter(([key]) => key !== "sample")
                .map(([key, value]) => (
                  <Panel key={key} className="p-3">
                    <span className="text-caption text-text-secondary">
                      {key.replaceAll("_", " ")}
                    </span>
                    <p className="mb-0 break-words text-body text-text">{metadataText(value)}</p>
                  </Panel>
                ))}
            </div>
            {(safePreview.safe_evidence.sample?.length ?? 0) > 0 && (
              <TableScroll label="Masked preview sample">
                <Table>
                  <TableHeader>
                    <TableRow>
                      {Object.keys(safePreview.safe_evidence.sample![0]).map((key) => (
                        <TableHead key={key}>{key}</TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {safePreview.safe_evidence.sample!.map((row, index) => (
                      <TableRow key={index}>
                        {Object.keys(safePreview.safe_evidence.sample![0]).map((key) => (
                          <TableCell key={key}>{displayProposalValue(row[key])}</TableCell>
                        ))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableScroll>
            )}
          </div>}
        </Panel>
      </div>}

      {sectionVisible(SECTION_INDEX.schedule_activate) && <div
        ref={sectionRefs[SECTION_INDEX.schedule_activate]}
        tabIndex={-1}
        aria-label="Schedule and activate section"
        className="grid gap-5 outline-none"
      >
        {input && <Panel className="grid gap-5 p-5">
          <div className="grid gap-4">
            <Field label={cadenceLabel} required>
              {({ id }) => (
                <NativeSelect
                  id={id}
                  value={input.schedule?.mode ?? "manual"}
                  onChange={(event) => edit({
                    ...input,
                    schedule: scheduleWithCadence(
                      input.schedule,
                      event.target.value as Cadence,
                    ),
                  })}
                >
                  {/* THE SERVER'S WORDS ON THE LABELS, the plan's vocabulary on
                      the wire (57.12, T3). The values stay `manual / daily /
                      weekly / hourly` because activation refuses `nightly` on
                      this path; the labels match `SchedulePanel.tsx`, so the
                      console says one thing in both places. */}
                  <option value="manual">On demand only (manual)</option>
                  <option value="daily">Every night (daily)</option>
                  {/* AI-217: legal since migration 204, offered by the Workbench,
                      the MCP tool and the REST seam, and dispatched by the
                      once-a-period dispatcher. Absent here, this step was the
                      only door that could not say it. */}
                  <option value="weekly">Once a week (weekly)</option>
                  <option value="hourly">Every hour (hourly)</option>
                </NativeSelect>
              )}
            </Field>
            {/* THE VALUE, NOT ITS NAME (57.12, T5). This field rendered the
                literal string "Project Settings timezone" — the place, instead
                of what lives there. The value is read from the same authority
                the final review binds (the active Configuration Version), so
                the step and the schedule cannot disagree; unconfirmed, the
                honest line is the UTC default the scheduler will run on. */}
            <Field
              label="Operating timezone"
              hint={reportingTimezone?.active
                ? "This project's reporting timezone — set in Project Settings."
                : reportingTimezone
                  ? "No reporting timezone is confirmed for this project — the schedule runs on UTC by default."
                  : "Project reporting timezone (set in Project Settings)"}
            >
              {(props) => (
                <Input
                  {...props}
                  value={reportingTimezone?.active ?? (reportingTimezone ? "UTC" : "")}
                  readOnly
                />
              )}
            </Field>
          </div>
          {/* WHEN THE DAY SHOULD LAND (story 57.8). Until this field the only
              thing this step could say about a schedule was how often it
              repeats, and every activated Datastream was written to local
              midnight. The hour reaches the stable row through
              `operator_input.schedule`, which activation already reads. */}
          {ARRIVAL_HOUR_CADENCES.includes(input.schedule?.mode ?? "manual") ? (
            <Field label="Arrival hour" hint={ARRIVAL_HOUR_COPY.hint}>
              {({ id }) => (
                <NativeSelect
                  id={id}
                  value={input.schedule?.arrival_hour === undefined
                    ? ""
                    : String(input.schedule.arrival_hour)}
                  onChange={(event) => edit({
                    ...input,
                    schedule: scheduleWithArrivalHour(input.schedule, event.target.value),
                  })}
                >
                  <option value="">Not set — runs at local midnight</option>
                  {ARRIVAL_HOURS.map((hour) => (
                    <option key={hour} value={String(hour)}>{arrivalHourLabel(hour)}</option>
                  ))}
                </NativeSelect>
              )}
            </Field>
          ) : (
            <Status as="block" tone="neutral" title="No arrival hour for this cadence">
              {input.schedule?.mode === "hourly" ? ARRIVAL_HOUR_COPY.hourly : ARRIVAL_HOUR_COPY.manual}
            </Status>
          )}
          {/* WHAT THE ARRIVAL MONITOR WILL DO, read from the declaration rather
              than asked again here. It used to be asked nowhere and defaulted to
              1440 minutes on the server, so this step set a cadence on a promise
              nobody had made. The declaration lives with the rest of the channel
              contract, in Source, and this line says what it means. */}
          {isInboundChannel && (
            <Status
              as="block"
              tone={declaredArrival == null ? "warning" : "success"}
              title={declaredArrival == null
                ? "Arrival monitoring is not armed"
                : `A delivery is expected every ${declaredArrival} minutes`}
            >
              {declaredArrival == null
                ? INBOUND_CHANNEL_COPY.arrivalUnarmed
                : INBOUND_CHANNEL_COPY.arrivalArmed(declaredArrival)}
            </Status>
          )}
          {warningIds.length > 0 && (
            <div className="grid gap-3">
              <strong className="text-label text-text">Non-blocking warnings</strong>
              {warningIds.map((warningId) => (
                <label key={warningId} className="flex items-start gap-2 text-body text-text">
                  <Checkbox
                    checked={warningAcks.includes(warningId)}
                    onCheckedChange={(checked) => {
                      setWarningAcks((current) => checked === true
                        ? [...new Set([...current, warningId])]
                        : current.filter((value) => value !== warningId));
                      setFinalReview(null); setDraftConfirmation(null);
                    }}
                    aria-label={`Acknowledge ${warningId}`}
                  />
                  <span>
                    I reviewed and accept <span className="font-mono text-caption">{warningId}</span>.
                  </span>
                </label>
              ))}
            </div>
          )}
          {finalReview && (
            <Status
              as="block"
              tone={finalReview.is_stale ? "error" : "success"}
              title="Review prepared"
            >
              Review {finalReview.final_review_ref} binds proposal {finalReview.proposal_ref},
              preview {finalReview.preview_ref}, exact schedule and
              {" "}{finalReview.acknowledged_warning_ids.length} warning acknowledgement(s).
            </Status>
          )}
          {draftConfirmation && (
            <Status as="block" tone="warning" title="Explicit confirmation required">
              Creating the Draft will materialize immutable non-live versions and one isolated
              candidate. It will not publish or start a recurring schedule. Confirmation expires at
              {" "}{draftConfirmation.expires_at}.
            </Status>
          )}
          {/* THE SUCCESS LINK NO LONGER COMPOSES AN ADDRESS (57.5). It wrote
              `/data/datastreams/o/{id}/overview`, and `parsePath`
              (`shell/router.tsx:130`) refuses every path whose first segment is
              not `/org/` — so the one gesture offered at the end of the whole
              wizard opened nothing. This file already says, at its own Props,
              that "the wizard never builds an address", and `onCreated(id)` was
              already called two lines after materialization: the shell is the
              only thing that knows the Organization segment. */}
          {isMaterialized && <Status
            as="block"
            tone="success"
            title="Datastream Draft created"
            action={onCreated && createdDatastreamId
              ? <Button variant="secondary" size="sm" onClick={() => onCreated(createdDatastreamId)}>
                  Open the Datastream
                </Button>
              : undefined}
          >
            {/* BUSINESS WORDS HERE (57.12, T6): the operator needs the name and
                the state, and the way in. The candidate reference and the
                publication pointer are the summary's `Technical evidence`. */}
            {input?.name ?? "This Datastream"} remains Draft. Nothing is published and no schedule is
            {" "}running; the candidate waits for its review on the Workbench.
          </Status>}
          {/* SAVE AS TEMPLATE, where a validated configuration exists (57.7).
              What it carries and what it does not are both listed: a save whose
              content nobody reads is a blind copy. */}
          {isMaterialized && <Panel className="grid gap-3 p-4">
            <PanelHeader
              title={TEMPLATE_COPY.saveTitle}
              description={TEMPLATE_COPY.saveDescription}
              actions={templates ? (
                <Badge tone={templates.count >= templates.limit ? "warning" : "neutral"}>
                  {templates.count} of {templates.limit} templates
                </Badge>
              ) : null}
            />
            <p className="m-0 text-caption text-text-secondary">{TEMPLATE_COPY.saveCarries}</p>
            <p className="m-0 text-caption text-text-secondary">{TEMPLATE_COPY.saveDrops}</p>
            {templateSaved
              ? <Status as="block" tone="success" title="Saved as a template">
                {templateSaved.label} is reusable by every Project of this organization. It opens at
                Source of the next Datastream.
              </Status>
              : <>
                <Field label={TEMPLATE_COPY.saveLabel} hint={TEMPLATE_COPY.saveHint}>
                  {({ id }) => (
                    <Input
                      id={id}
                      value={templateLabel}
                      maxLength={80}
                      onChange={(event) => setTemplateLabel(event.target.value)}
                    />
                  )}
                </Field>
                <div>
                  <Button
                    variant="secondary"
                    disabled={busy || templateLabel.trim().length === 0}
                    onClick={() => void saveAsTemplate()}
                  >
                    {TEMPLATE_COPY.saveTitle}
                  </Button>
                </div>
              </>}
            {templateError && (
              <Status as="block" tone="error" title="This configuration was not saved">
                {templateError}
              </Status>
            )}
          </Panel>}
        </Panel>}
      </div>}
      {error && <Status as="block" tone="error" title="Setup could not continue">{error}</Status>}
    </main>
    {/* ZONE 3 OF `:31`, AT EVERY SECTION. Business words on top; the machine
        artifacts stay behind `Technical evidence` for whoever audits.

        IT LIVED INSIDE `Schedule and activate` UNTIL NOW, which is the one
        step where a recap is least useful: everything it reports has already
        been decided by the time the operator reads it. `:31` asks for a
        persistent panel because the value of the thing is upstream — seeing,
        at `Configure`, that the name and the role are already answered and the
        grain is not.

        `col-start-2` BELOW 1280px is what keeps it under the task area rather
        than under the 210px rail: the grid has two columns there, and an
        auto-placed block would land in the stepper's. */}
    <aside
      aria-label="Configuration summary"
      className="col-start-2 min-w-0 xl:col-start-3 xl:row-start-1 xl:self-start xl:sticky xl:top-6"
    >
      <Panel className="grid gap-4 p-4">
        <PanelHeader
          title="Configuration summary"
          description="Every answer lands here as you give it."
        />
        <dl className="m-0 grid gap-3" data-testid="config-summary">
          {summaryRows.map((row) => {
            const answered = row.value !== null && row.value !== "";
            return (
              <div key={row.label} className="grid gap-0.5">
                <dt className="flex items-baseline justify-between gap-2 text-caption text-text-secondary">
                  <span className="min-w-0 truncate">{row.label}</span>
                  {row.automatic && <Badge tone="neutral">Automatic</Badge>}
                </dt>
                {/* THE PENDING SENTENCE SITS WHERE THE VALUE WILL SIT, in the
                    secondary tone: it is not the answer, and it must not read
                    as one. */}
                <dd className={answered
                  ? "m-0 min-w-0 break-words text-caption text-text"
                  : "m-0 min-w-0 break-words text-caption text-text-secondary"}
                >
                  {answered ? row.value : row.pending}
                </dd>
              </div>
            );
          })}
        </dl>
        <Collapsible>
          <CollapsibleTrigger
            className="rounded-pill border border-divider-base px-4 py-1.5 text-label font-label text-text"
          >
            Technical evidence
          </CollapsibleTrigger>
          <CollapsibleContent>
            <dl className="m-0 mt-3 grid gap-2" aria-label="Technical evidence">
              {([
                ["Draft", draft.draft_ref],
                ["Revision", revision.current],
                ["Observation", observation?.observation_ref ?? "Unavailable"],
                ["Preview", safePreview?.preview_ref ?? previewJob?.state ?? "Unavailable"],
                ["Candidate", materializationStatus?.candidate_state ?? "Not created"],
              ] as Array<[string, ReactNode]>).map(([label, value]) => (
                <div key={label} className="flex items-baseline justify-between gap-3">
                  <dt className="text-caption text-text-secondary">{label}</dt>
                  <dd className="m-0 min-w-0 truncate text-right text-caption text-text">{value}</dd>
                </div>
              ))}
            </dl>
          </CollapsibleContent>
        </Collapsible>
      </Panel>
    </aside>
    <footer className="col-start-2 flex items-center justify-between border-t border-divider-base pt-4">
      <div className="flex items-center gap-3">
      <Button
        variant="ghost"
        disabled={activeSection === 0}
        onClick={() => goToSection(Math.max(0, activeSection - 1))}
      >
        Back
      </Button>
      {/* The save state lives in the footer: always visible, never a panel of
          its own. `warning` stays for actual warnings — an autosave in flight
          is ordinary. */}
      <Status tone={saved ? "success" : "neutral"}>{saved ? "Draft saved" : "Saving valid edits"}</Status>
      </div>
      <div className="flex gap-3">
      <Button
        variant="secondary"
        onClick={() => input
          ? void saveNow(input, "source_setup_handoff")
            .then(() => onSourceSetup?.(draft.draft_ref))
            .catch((reason) => setError(reason instanceof Error ? reason.message : "Save failed"))
          : onSourceSetup?.(draft.draft_ref)}
      >
        Set up source access
      </Button>
      <Button
        variant="secondary"
        onClick={() => input
          ? void saveNow(input, "save_and_exit")
            .then(() => onCancel?.())
            .catch((reason) => setError(reason instanceof Error ? reason.message : "Save failed"))
          : onCancel?.()}
      >
        Save and exit
      </Button>
      {/* THE END OF A DRAFT (AI-336, ratified 2026-08-31), beside the gesture it
          is the opposite of: `Save and exit` keeps the draft resumable, this one
          ends it. `ghost` and not the rose — the footer's one recommendation is
          the step's own action — and not `destructive` either: the red belongs
          on the confirmation's accept button, where the choice is actually
          made, not on the control that merely asks the question.

          Never offered once the draft has materialized: at that point the
          object that can be archived is the Datastream, on its own screen, and
          the server refuses this route in exactly those words. */}
      {!isMaterialized && (
        <Button variant="ghost" onClick={() => setDiscardOpen(true)} disabled={busy}>
          Discard this draft
        </Button>
      )}
      {/* `:31` — the footer always carries ONE contextual primary action. The
          first sections had none: the only button before `Classify and map`
          was `Compile proposal`, so there was no way to move from `Source` to
          `Configure` at all. It went unnoticed because every panel rendered on
          one scrolling page and the operator scrolled; the rail was the only
          navigation, and it offers a step only once that step is already
          complete. Splitting the sections is what made the missing action
          visible. */}
      {/* The same completeness the rail reads. Gating only the rail would leave
          the footer as a way past a section it calls incomplete -- a refusal
          that refuses nothing. */}
      {activeSection === SECTION_INDEX.source && (
        <Button
          onClick={() => goToSection(SECTION_INDEX.configure)}
          disabled={!sectionComplete[SECTION_INDEX.source] || busy}
        >
          Continue to configure
        </Button>
      )}
      {activeSection === SECTION_INDEX.configure && (
        <Button
          onClick={() => proposal && !proposal.is_stale
            ? goToSection(SECTION_INDEX.classify_and_map)
            : void compile()}
          disabled={!observation || busy}
        >
          {proposal && !proposal.is_stale ? "Continue to classify and map" : "Compile proposal"}
        </Button>
      )}
      {activeSection === SECTION_INDEX.classify_and_map && (
        <Button
          onClick={() => proposal ? goToSection(SECTION_INDEX.preview_validate) : void compile()}
          disabled={!observation || busy}
        >
          {proposal ? "Continue to preview" : "Compile proposal"}
        </Button>
      )}
      {activeSection === SECTION_INDEX.preview_validate && (
        <Button
          onClick={() => safePreview
            ? goToSection(SECTION_INDEX.schedule_activate)
            : void queuePreview()}
          disabled={!mappingComplete || busy || (!safePreview && previewNeedsEstimate)
            || (previewJob ? ["queued", "running"].includes(previewJob.state) : false)}
        >
          {safePreview
            ? "Review schedule"
            : previewJob && ["queued", "running"].includes(previewJob.state)
              ? "Preview running"
              : "Create safe preview"}
        </Button>
      )}
      {activeSection === SECTION_INDEX.schedule_activate && !isMaterialized && (
        /* ONE PRIMARY ACTION for the whole ceremony (57.12, T4). The three
           calls still fire — in order, with their keys — but they fire from
           the dialog's single confirmation, not from three buttons whose
           labels named the plumbing. */
        <Button
          onClick={() => setCreateReviewOpen(true)}
          disabled={!safePreview || !allWarningsAcknowledged || busy}
        >
          Review and create draft
        </Button>
      )}
    </div></footer>
    {/* ONE DIALOG FOR THE WHOLE CLASS (57.12). The four destructive-change
        sites differ in what they ask, not in how the asking works: the pending
        action carries the sentence and the gesture, the dialog carries the
        choice — and cancelling leaves the draft exactly as it was. */}
    <ConfirmDialog
      open={pendingConfirm !== null}
      onOpenChange={(open) => { if (!open) setPendingConfirm(null); }}
      title={pendingConfirm?.title ?? ""}
      description={pendingConfirm?.message ?? ""}
      confirmLabel="Continue"
      destructive
      data-testid="datastream-setup-confirm"
      confirmTestId="datastream-setup-confirm-accept"
      cancelTestId="datastream-setup-confirm-cancel"
      onConfirm={() => {
        const run = pendingConfirm?.run;
        setPendingConfirm(null);
        run?.();
      }}
    />
    {/* THE CREATION CEREMONY (57.12, T4): one dialog, carrying what the
        gesture creates in business words — never "Freeze", never a token.
        What the review binds is read from the state the operator already
        built: the name, the schedule, the acknowledged warnings. Confirming
        runs the three calls in order (`reviewAndCreateDraft`); a failure
        names the phase that failed in the screen's error status. */}
    <ConfirmDialog
      open={createReviewOpen}
      onOpenChange={setCreateReviewOpen}
      title="Review and create draft"
      description={
        "Creating the Draft materializes immutable non-live versions and one isolated candidate. "
        + "It will not publish or start a recurring schedule."
      }
      evidenceLabel="What this gesture creates"
      evidence={{
        Datastream: input?.name || "Unnamed",
        Schedule: [
          input?.schedule?.mode ?? "manual",
          ARRIVAL_HOUR_CADENCES.includes(input?.schedule?.mode ?? "manual")
            ? `arrival ${arrivalHourLabel(input?.schedule?.arrival_hour)}`
            : null,
        ].filter(Boolean).join(", "),
        Output: outputSummary,
        "Warnings acknowledged": `${warningAcks.length} of ${warningIds.length}`,
      }}
      confirmLabel="Create draft"
      busy={busy}
      data-testid="datastream-create-review"
      confirmTestId="datastream-create-review-confirm"
      cancelTestId="datastream-create-review-cancel"
      onConfirm={() => {
        setCreateReviewOpen(false);
        void reviewAndCreateDraft();
      }}
    />
    {/* DISCARDING NAMES WHAT IS LOST (AI-336, ratified 2026-08-31), in the
        operator's words and never a state name. Three facts, because those are
        the three a person recognises their own draft by — what they were going
        to call it, what it reads from, and how far they got.

        The description carries the one sentence that makes the choice safe:
        this draft never created a Datastream, so nothing that collects data is
        touched. The way out says `Keep the draft`, for the reason the primitive
        documents — next to a destructive verb, the word that names what SURVIVES
        is what stops the wrong click. */}
    <ConfirmDialog
      open={discardOpen}
      onOpenChange={setDiscardOpen}
      title="Discard this draft?"
      description={
        "The answers below are no longer offered back, and the next Add Datastream starts from "
        + "the first question. No Datastream was created from this draft, so nothing that "
        + "collects data changes."
      }
      evidenceLabel="What this draft holds"
      evidence={{
        Datastream: input?.name || "Not named yet",
        Source: MODE_CHOICES.find((choice) => choice.value === input?.mode)?.label
          ?? "No source chosen yet",
        "Section reached": SECTIONS[activeSection]?.label ?? "Source",
      }}
      confirmLabel="Discard this draft"
      cancelLabel="Keep the draft"
      destructive
      busy={busy}
      data-testid="datastream-setup-discard"
      confirmTestId="datastream-setup-discard-confirm"
      cancelTestId="datastream-setup-discard-cancel"
      onConfirm={() => { void discardDraft(); }}
    />
  </div>;
}
