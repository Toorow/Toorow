/** Step 1's managed_feed panel (57.12, T2b) — extracted from
 *  `DatastreamSetupWizard.tsx`.
 *
 *  THE ORDER IS THE CONTRACT (ratified document, `:86-92`): the channel first,
 *  then the channel's OWN input — the file for an upload, the authorization
 *  and the workbook then the tab for Sheets, the address for inbound email,
 *  the endpoint for a webhook. No Connector, no product account is asked on
 *  this path: showing one would be asking about a product where the source is
 *  a channel.
 *
 *  No state lives here: the wizard owns the draft and passes values and
 *  callbacks down; what this file derives is pure from the props. */
import {
  Field, Input, NativeSelect, Panel, PanelHeader, Status, Textarea,
} from "../../ui";
import type {
  DatastreamSetupObservation,
  DatastreamSetupObservedObject,
  DatastreamSetupSourceAccount,
  DatastreamSetupSourceOptions,
} from "../wizard/wizardApi";
import {
  arrivalMinutes,
  authorizationValue,
  GOOGLE_SHEETS_COPY,
  INBOUND_CHANNEL_COPY,
  lineValues,
  sourceAccountId,
  type AuthorizationOption,
  type ChannelContract,
  type FeedInput,
} from "./sourceStepShared";

export interface SourceManagedFeedProps {
  input: FeedInput;
  options: DatastreamSetupSourceOptions | null;
  busy: boolean;
  /** Boolean(observation || proposal) in the wizard — what makes an upstream
   *  change worth a confirmation. */
  hasDependentEvidence: boolean;
  /** The Sheets accounts of this Project (through the SHARED predicate, never
   *  `connector_ref.id`). The wizard keeps the list because defaulting the
   *  grant on a channel change is its own gesture. */
  sheetsAccounts: DatastreamSetupSourceAccount[];
  sheetsAuthorizations: AuthorizationOption[];
  sheetsAuthorization: AuthorizationOption | null;
  observation: DatastreamSetupObservation | null;
  /** The addressable objects the discovery listed — tabs, here. Read from the
   *  observation, never from a constant and never from a second route. */
  sheetTabs: DatastreamSetupObservedObject[];
  observationCoverage: Record<string, string>;
  onEditUpstream: (value: FeedInput, changed: boolean, message: string) => void;
  onStageFile: (file: File) => void;
  onChooseSheetsAuthorization: (value: string) => void;
  onChooseSheetTab: (objectRef: string) => void;
  onEditChannelContract: (current: FeedInput, patch: ChannelContract) => void;
}

export default function SourceManagedFeed({
  input, options, busy, hasDependentEvidence,
  sheetsAccounts, sheetsAuthorizations, sheetsAuthorization,
  observation, sheetTabs, observationCoverage,
  onEditUpstream, onStageFile, onChooseSheetsAuthorization, onChooseSheetTab, onEditChannelContract,
}: SourceManagedFeedProps) {
  /** The two inbound channels, and what this deployment says about them. The
   *  domain comes from the channel option — the SAME server read that decides
   *  `availability` — so the contract block and the channel list can never
   *  disagree about whether anything can be received. */
  const isInboundChannel = ["inbound_email", "webhook"].includes(input.source.channel);
  const inboundOption = options?.managed_channels.find((item) => item.channel === input.source.channel);
  const inboundDomain = inboundOption?.domain ?? null;
  const channelContract = input.source.channel_contract ?? {};
  const declaredSenders = channelContract.allowed_senders ?? [];
  const declaredArrival = channelContract.expected_interval_minutes;

  return <div className="grid gap-4">
    <Field label="Channel" required>
      {({ id }) => (
        <NativeSelect
          id={id}
          value={input.source.channel}
          onChange={(event) => onEditUpstream(
            {
              ...input,
              source: {
                ...input.source,
                channel: event.target.value as FeedInput["source"]["channel"],
                // ONE GRANT IS NOT A DECISION, it is the same decision
                // retyped — the rule step 1 already applies to a single
                // candidate. It is defaulted HERE, on the gesture that
                // selects the channel, rather than by an effect: a draft
                // that rewrites itself on mount would be a write nobody
                // asked for. Any other channel asks for no grant at all.
                source_account_ref: event.target.value === "google_sheets"
                  && sheetsAuthorizations.length === 1
                  ? sourceAccountId(sheetsAuthorizations[0].accounts[0])
                  : "",
                template_ref: "", staged_asset_ref: "", sheet_ref: "",
              },
            },
            hasDependentEvidence,
            "Changing the managed-feed channel invalidates dependent evidence. Continue?",
          )}
        >
          {!options?.managed_channels.some((item) => item.channel === input.source.channel) && (
            <option value={input.source.channel}>
              {input.source.channel.replaceAll("_", " ")} — options unavailable
            </option>
          )}
          {options?.managed_channels
            .filter((item, index, all) => all.findIndex(
              (candidate) => candidate.channel === item.channel,
            ) === index)
            .map((item) => (
              <option key={item.channel} value={item.channel}>
                {item.channel.replaceAll("_", " ")}
                {item.availability === "setup_required" ? " — setup required" : ""}
              </option>
            ))}
        </NativeSelect>
      )}
    </Field>
    {/* THE "NOT WIRED" WARNING IS GONE, and it had to go with the client:
        `observe_google_sheet` is now injected as a closure over
        `SheetsSetupReader` (57.2), so `Discover source` reads a real tab.
        A warning that outlives what it warned about is the defect this file
        has already been corrected for twice. */}
    {input.source.channel === "google_sheets" && options && sheetsAccounts.length === 0 && (
      <Status as="block" tone="neutral" title="No Google Sheets access is exposed to this Project">
        {GOOGLE_SHEETS_COPY.noAccess}
      </Status>
    )}
    {input.source.channel === "google_sheets" && <>
      {/* QUESTION 2 OF THIS CHANNEL — THE GRANT THAT OPENS THE WORKBOOK,
          and never a product account. See `sheetsAuthorizations` in the
          wizard for the measurement that makes the scope immaterial here. */}
      <Field
        label="Google authorization"
        required
        hint={sheetsAuthorizations.length === 1
          ? GOOGLE_SHEETS_COPY.grantDefaulted
          : GOOGLE_SHEETS_COPY.grantHint}
      >
        {({ id }) => (
          <NativeSelect
            id={id}
            value={sheetsAuthorization ? authorizationValue(sheetsAuthorization) : ""}
            onChange={(event) => onChooseSheetsAuthorization(event.target.value)}
          >
            <option value="">Select the Google authorization</option>
            {sheetsAuthorizations.map((entry) => (
              <option key={authorizationValue(entry)} value={authorizationValue(entry)}>
                {entry.label}
              </option>
            ))}
          </NativeSelect>
        )}
      </Field>
      {sheetsAuthorization && (
      <Field label="Sheet reference" required hint={GOOGLE_SHEETS_COPY.referenceHint}>
        {(props) => (
          <Input
            {...props}
            value={input.source.sheet_ref}
            onChange={(event) => onEditUpstream(
              { ...input, source: { ...input.source, sheet_ref: event.target.value } },
              hasDependentEvidence,
              "Changing the Sheet reference invalidates dependent evidence. Continue?",
            )}
          />
        )}
      </Field>
      )}
    </>}
    {/* THE TAB IS CHOSEN, NOT TYPED, as soon as the workbook has been
        read once. The list is `safe_metadata.objects`, written by the
        same discovery that read the structure — never a constant, never a
        second route. It appears only after a discovery, because before
        one nothing has seen the workbook; the reference field stays free
        so the first discovery can be run, and so a tab beyond the
        bounded list can still be named. */}
    {input.source.channel === "google_sheets" && observation && (
      <Field label="Tab" hint={GOOGLE_SHEETS_COPY.tabPickerHint}>
        {({ id }) => (
          <NativeSelect
            id={id}
            value={input.source.sheet_ref}
            onChange={(event) => onChooseSheetTab(event.target.value)}
          >
            <option value="">Select a tab of this workbook</option>
            {!sheetTabs.some((tab) => tab.object_ref === input.source.sheet_ref) && input.source.sheet_ref && (
              <option value={input.source.sheet_ref}>
                {input.source.sheet_ref} — not listed by this workbook
              </option>
            )}
            {sheetTabs.map((tab) => (
              <option key={tab.object_ref} value={tab.object_ref}>{tab.label}</option>
            ))}
          </NativeSelect>
        )}
      </Field>
    )}
    {input.source.channel === "google_sheets" && observation && sheetTabs.length === 0 && (
      <Status as="block" tone="neutral" title="This workbook lists no tab to choose from">
        {GOOGLE_SHEETS_COPY.tabNoneListed}
      </Status>
    )}
    {/* A BOUNDED LIST THAT DOES NOT SAY SO IS A FABRICATED COMPLETENESS —
        the same clause 57.1 wrote for BigQuery objects, held here by the
        normalizer's own counts rather than by this array's length, which
        IS the truncated one. */}
    {input.source.channel === "google_sheets" && observationCoverage.object_list === "truncated" && (
      <Status as="block" tone="warning" title="This tab list is shorter than the workbook">
        {`${observationCoverage.objects_listed ?? sheetTabs.length} of `
          + `${observationCoverage.objects_observed ?? "?"} `}
        {GOOGLE_SHEETS_COPY.tabListTruncated}
      </Status>
    )}
    {input.source.channel === "file_upload" && <>
      <Field label="CSV, Excel or SAV file" required>
        {({ id }) => (
          <Input
            id={id}
            type="file"
            accept=".csv,.tsv,.xlsx,.sav"
            disabled={busy}
            onChange={(event) => {
              const file = event.currentTarget.files?.[0];
              if (file) onStageFile(file);
            }}
            className="py-2"
          />
        )}
      </Field>
      {input.source.staged_asset_ref && (
        <Status as="block" tone="success" title="File staged">
          {/* NO ASSET ID (57.12, T6): `dsa_…` is the quarantine's address for
              the file, not something the operator acts on — the state is the
              sentence. */}
          The file is held in draft quarantine; its bytes never leave it until this Datastream is created.
        </Status>
      )}
    </>}
    {options?.managed_channels.some(
      (item) => item.channel === input.source.channel && item.template_ref,
    ) && (
      <Field label="Template" required>
        {({ id }) => (
          <NativeSelect
            id={id}
            value={input.source.template_ref}
            onChange={(event) => onEditUpstream(
              { ...input, source: { ...input.source, template_ref: event.target.value } },
              hasDependentEvidence,
              "Changing the channel template invalidates dependent evidence. Continue?",
            )}
          >
            <option value="">Select a governed template</option>
            {options.managed_channels
              .filter((item) => item.channel === input.source.channel && item.template_ref)
              .map((item) => (
                <option key={item.template_ref!} value={item.template_ref!}>
                  {item.display_name ?? item.template_ref}
                </option>
              ))}
          </NativeSelect>
        )}
      </Field>
    )}
    {options?.managed_channels.find(
      (item) => item.channel === input.source.channel,
    )?.availability === "setup_required" && (
      <Status as="block" tone="warning" title="Channel setup required">
        Save first, then use Set up source access. No delivery credential is issued here.
      </Status>
    )}
    {/* THE CHANNEL CONTRACT — a declaration, never a discovery.
        Four clauses, each with its own state and its own authority. The
        two that an operator can settle are editable here; the two that are
        read say where they come from. No address is composed: it is a
        secret minted against a Datastream that does not exist yet. */}
    {isInboundChannel && <Panel className="grid gap-4 p-4">
      <PanelHeader title="Channel contract" description={INBOUND_CHANNEL_COPY.section} />
      {inboundDomain ? (
        <Status
          as="block"
          tone="neutral"
          title={input.source.channel === "webhook"
            ? "Where deliveries arrive — a token, not a URL"
            : `Where deliveries arrive — ds_<token>@${inboundDomain}`}
        >
          {input.source.channel === "webhook"
            ? INBOUND_CHANNEL_COPY.addressWebhook
            : INBOUND_CHANNEL_COPY.addressPending}
        </Status>
      ) : (
        <Status as="block" tone="warning" title="No verified inbound domain">
          {INBOUND_CHANNEL_COPY.domainMissing}
        </Status>
      )}
      <Status
        as="block"
        tone="neutral"
        title={input.source.template_ref
          ? "What is expected — a governed template is bound"
          : "What is expected — no template bound"}
      >
        {input.source.template_ref
          ? "The first file that arrives is read against this template."
          : INBOUND_CHANNEL_COPY.formatNone}
      </Status>
      <Field label="Who may send" hint={INBOUND_CHANNEL_COPY.senderHint}>
        {(props) => (
          /* UNCONTROLLED, AND KEYED ON THE CHANNEL. Re-serializing the
             parsed list back into the box eats the newline the moment it
             is typed -- the empty second line is not yet a sender, so it
             is filtered, and the next character lands on line one. The
             operator could never write a second entry. The box therefore
             keeps the raw text and the state keeps the parsed list; the
             key re-seeds it when the channel changes, which is the only
             moment the two can legitimately diverge. */
          <Textarea
            {...props}
            key={input.source.channel}
            rows={3}
            defaultValue={declaredSenders.join("\n")}
            placeholder="Leave empty: the token in the address is the only authorization"
            onChange={(event) => onEditChannelContract(input, { allowed_senders: lineValues(event.target.value) })}
          />
        )}
      </Field>
      <Status
        as="block"
        tone="neutral"
        title={declaredSenders.length ? "Who may send — declared" : "Who may send — token only"}
      >
        {declaredSenders.length
          ? INBOUND_CHANNEL_COPY.senderDeclared(declaredSenders.length)
          : INBOUND_CHANNEL_COPY.senderTokenOnly}
      </Status>
      <Field label="Expected arrival every N minutes" hint={INBOUND_CHANNEL_COPY.arrivalHint}>
        {(props) => (
          <Input
            {...props}
            type="number"
            min={1}
            value={declaredArrival == null ? "" : String(declaredArrival)}
            placeholder="Not declared"
            onChange={(event) => onEditChannelContract(input, {
              expected_interval_minutes: arrivalMinutes(event.target.value),
            })}
          />
        )}
      </Field>
      <Status
        as="block"
        tone={declaredArrival == null ? "warning" : "neutral"}
        title={declaredArrival == null
          ? "How often it is expected — not declared"
          : "How often it is expected — declared"}
      >
        {declaredArrival == null
          ? INBOUND_CHANNEL_COPY.arrivalUnarmed
          : INBOUND_CHANNEL_COPY.arrivalArmed(declaredArrival)}
      </Status>
    </Panel>}
  </div>;
}
