/** The Source step's shared vocabulary (57.12, T2b).
 *
 *  The three per-mode panels of step 1 were extracted out of
 *  `DatastreamSetupWizard.tsx` into `SourceConnectorPull.tsx`,
 *  `SourceExternalBq.tsx` and `SourceManagedFeed.tsx`. What lives here is what
 *  the panels AND the wizard both read: the input types, the authorization
 *  helpers, and the copy blocks — one home per sentence, or the panel and the
 *  wizard drift apart the day a sentence is edited in only one of them.
 *
 *  Everything here was MOVED verbatim from the wizard, comments included;
 *  nothing was rewritten. */
import type {
  DatastreamSetupSourceAccount,
  DatastreamSetupSourceOptions,
  ProposalEvidenceRef,
} from "../wizard/wizardApi";

export type Mode = "connector_pull" | "external_bq" | "managed_feed";
/** AI-217. The four cadences the platform accepts, in the plan intent's own
 *  vocabulary. `weekly` has been legal on the stable row since migration 204 and
 *  is offered by the Workbench, the MCP tool and the REST seam; this step was the
 *  only door that could not say it, which read as "there is no weekly cadence". */
export type Cadence = "manual" | "daily" | "weekly" | "hourly";
export type CommonInput = {
  name?: string; data_role?: string; domain_ids?: string[];
  /** `arrival_hour` (story 57.8) is the hour of the project's LOCAL day the pull
   *  should land, 0-23. It is carried here because activation reads
   *  `operator_input["schedule"]`, and it applies to the cadences that run once a
   *  period — `daily` and `weekly` (AI-217). An hourly cadence re-fetches the day
   *  as it fills and has no single moment of arrival. */
  schedule?: {
    mode: Cadence;
    interval_minutes?: number;
    expected_interval_minutes?: number;
    delay_minutes?: number;
    lookback_minutes?: number;
    arrival_hour?: number;
  };
  /** `active_section_ref` is the section's NAME and it is what this wizard reads
   *  back; `active_section` stays a rank so a deployment still running an older
   *  build reads the same line it always did. Written together, never apart. */
  wizard_state?: { active_section: number; active_section_ref?: string; first_incomplete: string };
};
export type ConnectorInput = CommonInput & {
  mode: "connector_pull";
  source: {
    source_account_ref: string; connector_ref: string; connector_contract_version_ref: string;
    report_ref: string; observation_ref?: string;
  };
  configure: {
    date_field: string; metrics: string; dimensions: string; date_window: string; filters: string;
    history_intent: string; cadence_intent: string; grain: string;
  };
};
export type ExternalInput = CommonInput & {
  mode: "external_bq";
  source: {
    access_ref: string; object_ref: string; declared_writer: string;
    readonly_acknowledged: boolean; observation_ref?: string;
  };
  configure: {
    watermark_semantics: string; logical_dataset_name: string; expected_freshness: string;
    verification_window: string; expected_history: string; row_filters: string;
  };
};
/** What an inbound channel PROMISES (57.3). A declaration, not evidence: an
 *  email address and a webhook token have no provider to list, so the only
 *  things that can be known before a delivery are the ones the operator states.
 *  Both are optional and neither is defaulted — an undeclared arrival leaves the
 *  monitor unarmed, and an undeclared sender means the token in the address
 *  remains the only authorization, which is what it has always been. */
export type ChannelContract = { expected_interval_minutes?: number; allowed_senders?: string[] };
export type FeedInput = CommonInput & {
  mode: "managed_feed";
  source: {
    channel: "file_upload" | "google_sheets" | "inbound_email" | "webhook";
    source_account_ref: string; template_ref: string; staged_asset_ref: string; sheet_ref: string;
    channel_contract?: ChannelContract; observation_ref?: string;
  };
  configure: {
    input_ref: string; parsing_contract: string; logical_dataset_name: string;
    date_semantics: string; grain: string; write_mode: "replace" | "append";
  };
};
export type SetupInput = ConnectorInput | ExternalInput | FeedInput;

export function sourceAccountId(item: DatastreamSetupSourceOptions["source_accounts"][number]): string {
  return item.object_ref.id;
}
/** What the option says. The label alone is the scope; an operator choosing
 *  between scopes of one authorization also needs to know WHICH tool each scope
 *  belongs to, and whether its authorization is healthy. */
export function sourceAccountOption(item: DatastreamSetupSourceOptions["source_accounts"][number]): string {
  const opened = (item.opens ?? []).filter((entry) => entry.available);
  const tool = opened.length === 1 ? opened[0].display_name : null;
  const health = item.states.authorization
    && item.states.authorization !== "ok" && item.states.authorization !== "healthy"
    ? item.states.authorization.replaceAll("_", " ")
    : null;
  return [item.label, tool, health].filter(Boolean).join(" — ");
}
/** THE AUTHORIZATION AN ACCOUNT BELONGS TO, or `null` when the payload named
 *  none. Never a fabricated grouping key: accounts that carry no authorization
 *  id are the deployment that predates `connection_ref` on the wire, and they
 *  are gathered under a stated absence rather than under an invented consent. */
export function accountAuthorizationId(item: DatastreamSetupSourceAccount): string | null {
  return item.connection_ref?.id || null;
}
/** What an authorization is CALLED. The payload gives a scope, a kind and the
 *  owning organization — never a name — so two Google consents of one
 *  organization read identically, and "choosing between identical options is
 *  not a choice". They are told apart by the tail of the id they are addressed
 *  by, and only when they would otherwise collide: a reference printed against
 *  a single authorization is noise. */
export function authorizationLabel(account: DatastreamSetupSourceAccount): string {
  const ref = account.authorization_ref ?? {};
  const kind = ref.kind && ref.kind !== "unavailable" ? ref.kind.replaceAll("_", " ") : null;
  const scope = ref.owner_scope === "delegated" ? "delegated" : null;
  return [ref.owner_org_name || null, kind, scope].filter(Boolean).join(" — ")
    || "Authorization — this deployment names neither its owner nor its kind";
}
export type AuthorizationOption = { id: string | null; label: string; accounts: DatastreamSetupSourceAccount[] };
/** The value the option carries in the DOM. An authorization the payload does
 *  not name still has to be selectable, and `""` is already "nothing chosen" —
 *  reusing it would make "not named" and "not answered" the same state. */
export const AUTHORIZATION_UNNAMED_VALUE = "authorization-not-named";
export function authorizationValue(entry: AuthorizationOption): string {
  return entry.id ?? AUTHORIZATION_UNNAMED_VALUE;
}
/** Question 2 of step 1, grouped out of question 3's answers. One entry per
 *  authorization that opens the chosen Connector, in first-seen order so the
 *  list does not reshuffle between two reads of the same payload. */
export function authorizationOptions(accounts: DatastreamSetupSourceAccount[]): AuthorizationOption[] {
  const grouped: AuthorizationOption[] = [];
  for (const account of accounts) {
    const id = accountAuthorizationId(account);
    const existing = grouped.find((entry) => entry.id === id);
    if (existing) { existing.accounts.push(account); continue; }
    grouped.push({ id, label: authorizationLabel(account), accounts: [account] });
  }
  return grouped.map((entry) => {
    const collides = grouped.some((other) => other !== entry && other.label === entry.label);
    if (entry.id === null) return { ...entry, label: AUTHORIZATION_COPY.unnamedOption };
    return collides ? { ...entry, label: `${entry.label} — ${shortRef(entry.id)}` } : entry;
  });
}
/** Shorten a reference for display: the tail is what tells two references apart,
 *  and the whole id is an address, not a name. */
export function shortRef(id: string): string { return `${id.slice(0, 5)}…${id.slice(-6)}`; }

/** ONE ENTRY PER LINE, and deliberately not `csvValues`. That helper serves two
 *  `NativeSelect multiple` controls whose value is a comma-joined string; making
 *  it split on newlines would change what those two read. A sender list is a
 *  list a person re-reads, so it gets a `Textarea` and its own splitter — the
 *  screen's instruction and the field's behaviour have to be the same thing. */
export function lineValues(value: string): string[] {
  return value.split("\n").map((item) => item.trim()).filter(Boolean);
}
/** An arrival interval in minutes, or `undefined`. NEVER a number when the field
 *  is empty: `datastream_activation` used to fall back to 1440 and promise a
 *  daily delivery nobody had declared, and the monitor then fired against it. */
export function arrivalMinutes(value: string): number | undefined {
  const parsed = Number.parseInt(value.trim(), 10);
  return Number.isFinite(parsed) && parsed >= 1 ? parsed : undefined;
}

/** What a capped listing says — and it says only what is measured.
 *
 *  TWO REASONS, TWO SENTENCES. `truncated` is true either because the dataset
 *  reached the walk's bound, or because that bound could not be read at all
 *  (`_bigquery_listing_bound` returns 0 and the server then refuses to call any
 *  list complete). Calling the second "the bound of the discovery walk" states a
 *  number that is not the bound — the count is simply what was listed. */
export function truncatedListingSentence(access: DatastreamSetupSourceAccount): string {
  const dataset = (access.external_object_ref ?? "").split(".").slice(0, 2).join(".");
  const listed = access.listed_objects ?? 0;
  const bound = access.listing_bound ?? 0;
  if (bound > 0) {
    return `${listed} objects are listed for ${dataset}, which is the bound of the discovery walk. `
      + "Others may exist: name the object yourself if yours is not among them.";
  }
  return `${listed} objects are listed for ${dataset}, and the bound the discovery walk applies could `
    + "not be read here — so this list cannot be called complete. Name the object yourself if yours is "
    + "not among them.";
}

/** The four evidence sources the target names, in its own words (`:59`). */
export const EVIDENCE_SOURCE: Record<ProposalEvidenceRef["kind"], string> = {
  connector_contract: "Connector contract",
  observed_metadata: "Observed schema",
  project_setting: "Project setting",
  governance_preset: "Governance preset",
  operator_input: "Your input",
};

export const CONFIDENCE_TONE: Record<string, "success" | "warning" | "error" | "neutral"> = {
  high: "success", medium: "warning", low: "error", none: "neutral",
};

/** THE ONE SENTENCE FOR AN EMPTY CATALOGUE, and it already existed beside the
 *  Connector select. Two formulations of the same void are two registries of
 *  truth at the scale of a sentence, so it is written once and read twice.
 *
 *  IT NO LONGER SAYS "no contract is persisted" (AI-279). The catalogue is the
 *  module registry, so this state means the deployment holds no Connector module
 *  that declares any capability — not that a verification run is owed. Naming
 *  the wrong absence sent an operator looking for a run to trigger. */
export const NO_CONTRACT_COPY = {
  title: "No Connector in this deployment",
  body:
    "No Connector declaring source capabilities is installed here, so no report family can be "
    + "offered. Adding a module to the registry adds it to this catalogue.",
} as const;

/** STEP 1'S SECOND QUESTION, in words — amendment ratified 2026-08-10.
 *
 *  Its empty state is the reason the amendment exists. An operator who had
 *  chosen nothing was told that no Connector contract is persisted in this
 *  deployment: true, and unusable. Asked after the Connector, the same void has
 *  a name and an address — THIS product has no authorization in THIS Project,
 *  and `Sources` is where one is created.
 *
 *  `unnamedOption` is the deployment that sends no `connection_ref`: the scopes
 *  are real, the consent behind them is not on the wire, and the option says so
 *  rather than pretending they share one. */
export const AUTHORIZATION_COPY = {
  hint:
    "Which consent opens this Connector. One consent can open several Connectors and expose several "
    + "accounts, so this is not the account — that is the next question.",
  defaultedHint: "The only authorization of this Project that opens this Connector; it is already chosen.",
  unnamedOption: "Authorization not named by this deployment",
  noneTitle: (connectorName: string) => `${connectorName} has no authorization in this Project`,
  noneBody: (authorizationKind?: string) =>
    authorizationKind === "google_direct"
      ? "Nothing in this Project opens this Connector yet. One Google consent opens every Google "
        + "Connector; it runs in this console and returns to this saved draft."
      : "Nothing in this Project opens this Connector yet. Authorize it here; the consent runs in "
        + "this console and returns to this saved draft.",
  accountClearedTitle: "Source Account cleared by this Connector",
  accountCleared: (account: string, connector: string) =>
    `${account} does not open ${connector}, so it was cleared. An account is a scope of one consent and a `
    + "consent does not open every Connector; choose one this Connector is served by.",
  noAccountTitle: "This authorization exposes no account for this Connector",
  noAccountBody:
    "The consent is present, but no scope of it was discovered for this Connector. Re-running account "
    + "discovery on the authorization in Data › Sources is what fills this list.",
} as const;

/** What the External BigQuery step says, kept out of the JSX like `IDENTITY_COPY`
 *  so each sentence can be edited as a sentence.
 *
 *  The two emptinesses are the point of this block and they are NOT the same
 *  thing: an access list with nothing in it is the ordinary state of a
 *  deployment where no BigQuery authorization exists, and it names who grants
 *  what and where. A discovery that failed names the ADAPTER, because the
 *  operator needs to know which half broke. Saying "discovery failed" for the
 *  first is a lie, and saying "nothing exposed" for the second hides a fault.
 *
 *  The `browse*` sentences belong to the live listing of 57.12 (T2b): a FAILED
 *  read names what failed, an EMPTY one states the absence — the two are never
 *  one message, here either. */
export const EXTERNAL_BQ_COPY = {
  noAccess:
    "No Source Account of this Project comes from a BigQuery authorization, so there is no governed access "
    + "to declare. This is an empty list, not a failure.",
  emptySchema:
    "This authorization exposes no readable column on that object. A dataset becomes visible when its owner "
    + "grants roles/bigquery.dataViewer to this deployment's principal — which is written in Google Cloud, "
    + "never here.",
  objectIsRead: "Read from the access you chose; discovery already named this object.",
  objectIsTyped: "Typed, because the chosen access does not name a project.dataset.table object.",
  objectIsFree: "Typed: the listing for this dataset is bounded, so the object you need may not be in it.",
  objectIsNoAccess: "Typed, because no access is chosen yet: the three-part reference works without a listing.",
  previewNeedsEstimate:
    "No scan estimate is attached to this observation, so a read cannot be launched. Re-run Discover source.",
  browseLoading: "Reading the live warehouse listing",
  browseEmpty:
    "This authorization listed no project, dataset or table it can read. The object can still be typed "
    + "below — an empty listing is not a failure.",
  browseFailedTitle: "The live warehouse listing could not be read",
  browseFailedBody:
    "The object can still be typed below — the three-part reference works without the listing.",
  browseTruncated: (dataset: string) =>
    `The listing of ${dataset} is bounded, so the table you need may not be in it — type it below if so.`,
} as const;

/** What the Google Sheets channel says (57.2), kept out of the JSX like the two
 *  blocks above so each sentence can be edited as a sentence.
 *
 *  FOUR EMPTINESSES, FOUR SENTENCES, and none of them is a failure. No account is
 *  an authorization that was never granted; an unnamed tab is an incomplete
 *  reference; a missing tab means the workbook WAS read, so access works; an
 *  unusable header row is a sheet without a header, which is not a broken sheet.
 *  The failure sentence is the adapter's, and it names the adapter. */
export const GOOGLE_SHEETS_COPY = {
  /** WHAT THIS CHANNEL ACTUALLY NEEDS, said as itself. It asked for a `Source
   *  Account`, which named a product where the source is a channel; what reading
   *  a private workbook needs is the Google consent carrying
   *  `spreadsheets.readonly`. The token is minted from the consent, so the scope
   *  under it changes nothing — and the hint says that rather than leaving an
   *  operator wondering which of five scopes they should have picked. */
  grantHint:
    "The Google consent this workbook is read with. The access token is minted from the consent itself, so "
    + "no account of it has to be chosen here.",
  /** ITS OWN WORDS, sharing none with `noAccess`: "this Project exposes exactly
   *  one consent" and "this Project exposes none" are opposite facts, and a
   *  sentence read in both would make a failed discovery look like a missing
   *  authorization. */
  grantDefaulted:
    "The only Google consent this Project exposes for Sheets; it is already chosen.",
  noAccess:
    "No Source Account of this Project comes from a Google authorization granting spreadsheets.readonly. The "
    + "consent is granted in Data > Sources, never here.",
  referenceHint:
    "The workbook, and the tab once discovery has listed them — A1 notation, spreadsheetId!Tab name. Type "
    + "the workbook id, discover, then choose the tab below.",
  tabUnnamed:
    "Choose one in Tab, then discover again. Reading the first tab instead would be a choice nobody made — a "
    + "four-tab workbook would then describe four different schemas depending on their order.",
  tabNotFound:
    "The workbook was read, so the authorization works; it simply carries no tab with that name. Choose one "
    + "in Tab, then discover again.",
  tabPickerHint:
    "Read from the workbook itself. Only grid tabs are offered: a chart sheet has no header row, so choosing "
    + "it would lead nowhere.",
  tabNoneListed:
    "This workbook reported no grid tab, so there is nothing to choose from. A chart sheet carries no header "
    + "row and is not offered.",
  tabListTruncated:
    "tabs are listed here; discovery evidence is bounded in size. The tabs beyond that bound exist and are "
    + "not shown — name one yourself in Sheet reference if yours is missing.",
  headerUnusable:
    "The first row of this tab carries no usable column name. That is a sheet without a header, not a broken "
    + "sheet — set header_row in Parsing contract, or add a header row in the sheet.",
  headerAssumed:
    "Row 1 is read as the header. The sheet does not freeze a header row, so this is an assumption — change "
    + "header_row in Parsing contract if it is wrong.",
  headerDuplicate:
    "Row 1 repeats a column name, so no mapping can say which of them it means. Fix the sheet, or point "
    + "header_row in Parsing contract at the right row.",
  headerIncomplete:
    "Row 1 has an empty cell, so at least one column has no name. No name is invented for it, so it produces "
    + "no field — fix the sheet, or point header_row in Parsing contract at the right row.",
  columnsInferred:
    "These columns are inferred from the header row. A spreadsheet declares no schema, so this list is what "
    + "row 1 says, never what the file guarantees.",
  typesNotInferred:
    "No column type is stated. A spreadsheet declares none, and reading values to guess one is a pull, not a "
    + "description. Types are confirmed in Classify and map — until then no metric is proposed from a sheet.",
  /** A BUCKET, NOT A COUNT, and the sentence says which: `rowCount` is the
   *  height of the grid, not the number of rows carrying data. */
  gridHeight: (bucket: string) =>
    `The grid is ${bucket} rows tall; how many carry data is unknown until the first import.`,
} as const;

/** What an inbound channel says (57.3), kept out of the JSX like the blocks
 *  above so each sentence can be edited as a sentence.
 *
 *  A CHANNEL CONTRACT IS NOT AN OBSERVATION. There is no provider to interrogate
 *  behind an email address or a webhook token — nobody at the other end will
 *  list anything, and until a sender sends there is nothing at all. So every
 *  sentence here states either a fact read from the deployment or an absence
 *  that names WHO settles it and WHEN. None of them fills a blank with a value.
 *
 *  FOUR CLAUSES, FOUR REPAIRS, and that is why they are four lines rather than
 *  one verdict: a missing domain is a platform administrator's, a missing
 *  address is materialization's, a missing Template is `Classify and map`'s, and
 *  a missing arrival is the operator's own — here, on this step. */
export const INBOUND_CHANNEL_COPY = {
  section:
    "What this channel promises. Nothing here is discovered: an inbound channel has no provider to ask, so "
    + "what step 2 can show is what you declare plus what this deployment already carries.",
  addressPending:
    "The address is issued against the Datastream, on its Overview, once this draft is created. There is "
    + "nothing to issue it against yet, so no address is shown — one displayed here would be a secret nobody "
    + "minted.",
  addressWebhook:
    "A webhook has no dedicated URL: its capability is a token, issued against the Datastream on its "
    + "Overview once this draft is created.",
  domainMissing:
    "No verified inbound domain is configured for this deployment, so nothing can be received yet. A "
    + "platform administrator configures it; this step cannot.",
  formatNone:
    "No governed template is bound to this channel. The shape is read from the first file that arrives, and "
    + "the canonical template is chosen in Classify and map.",
  senderTokenOnly:
    "Delivery is authorized by the token carried in the address. Declare senders below to narrow it — an "
    + "unsigned or unknown-recipient delivery is already refused before anything is parsed.",
  senderHint:
    "One address or one bare domain per line. Applied when a delivery is processed, never at reception: the "
    + "receipt answers every refusal identically so nobody can learn which addresses exist. A refused delivery "
    + "becomes a rejected receipt naming its reason.",
  senderDeclared: (count: number) =>
    `${count} sender declaration${count > 1 ? "s" : ""}. A delivery from anywhere else is kept, `
    + "refused and reported to you — it is never imported.",
  arrivalHint: "In minutes. Nothing is assumed: leave it empty and no arrival is expected.",
  arrivalUnarmed:
    "No expected arrival frequency is declared, so this Datastream will not report a late delivery. "
    + "Declaring one here is what arms the monitor.",
  arrivalArmed: (minutes: number) =>
    `A delivery is expected every ${minutes} minutes. Lateness is measured against that, `
    + "with one full interval of grace.",
  emptyDelivery:
    "No file has arrived on this channel yet. This is the normal state before the first delivery: the shape "
    + "of the feed is read from the first file that arrives, never guessed.",
  /* NO `adapterFailed` HERE, and its absence is the decision. The sentence an
     operator reads when the read fails is the SERVER'S -- raised as
     `ObservationUnavailable` by the closure in
     `datastream_preconfiguration_api._create_observation`, returned as a 503 and
     rendered by this screen's own error `Status`. A second copy of it in this
     file would drift from the one actually shown, and it is the copy nobody
     reads that stays correct. */
} as const;
