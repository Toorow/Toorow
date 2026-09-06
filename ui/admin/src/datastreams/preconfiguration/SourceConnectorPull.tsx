/** Step 1's connector_pull panel (57.12, T2b) — extracted from
 *  `DatastreamSetupWizard.tsx`, where it grew past what one file can hold.
 *
 *  The FOUR QUESTIONS, in the ratified order (amendment of 2026-08-10): the
 *  Connector, then the authorization that opens it, then the account that
 *  authorization serves, then the template or the fields. Each one narrows the
 *  next, and no state lives here: the wizard owns the draft and passes values
 *  and callbacks down; what this file derives is pure from the props.
 *
 *  The starting-point catalogue machinery (`buildStartingPoints`,
 *  `templateStartingPoint`, `RecommendedStarts`) lives here too — it is this
 *  panel's question 4 — and the wizard imports it back for the organization
 *  templates, which project a saved template THROUGH the same catalogue. */
import { useState } from "react";
import { CheckIcon } from "lucide-react";
import {
  Badge, Button, ChoiceGroup, ConnectorMark, Field, Input, NativeSelect, Panel, PanelHeader, Status,
  TONE_TEXT,
  wireWord,
} from "../../ui";
import {
  accountServesConnector,
  type DatastreamSetupConnectorOption,
  type DatastreamSetupPlatformDefaults,
  type DatastreamSetupRecommendation,
  type DatastreamSetupReportOption,
  type DatastreamSetupSourceOptions,
  type DatastreamSetupTemplate,
} from "../wizard/wizardApi";
import ConnectButton from "../../ConnectButton";
import { verifyConnectionAccount } from "../wizard/wizardApi";
import ConnectGoogleButton from "../../authorizations/ConnectGoogleButton";
import {
  accountAuthorizationId,
  AUTHORIZATION_COPY,
  authorizationOptions,
  authorizationValue,
  CONFIDENCE_TONE,
  EVIDENCE_SOURCE,
  NO_CONTRACT_COPY,
  sourceAccountId,
  sourceAccountOption,
  type ConnectorInput,
} from "./sourceStepShared";

/** The one line a Connector card carries about its pin. `verified` says nothing:
 *  a card that annotates the ordinary case turns the annotation into noise, and
 *  the two states worth reading stop standing out. `unverified` says nothing
 *  EITHER (2026-08-10, UX pass): since AI-279 the catalogue IS the module
 *  registry and the pin is written at binding time, so "no pin" is the ordinary
 *  state of every base Connector of every instance — printing it on 39 cards
 *  made the one state worth reading (`stale`) invisible. */
/** The collapsed shape of an ANSWERED question — the chosen thing on the left,
 *  `Change` on the right. Named rather than inlined so the line fits the 120
 *  columns this package holds itself to, and so the next collapsed answer wears
 *  the same box instead of a second hand-tuned copy of it. */
const ANSWERED_ROW = "flex items-center justify-between gap-3 rounded-control "
  + "border border-border-quiet bg-surface-light p-3";

const CONNECTOR_CARD_HINT: Record<"verified" | "unverified" | "stale", string | undefined> = {
  verified: undefined,
  unverified: undefined,
  stale: "The Connector changed since this contract was pinned.",
};

/** A starting point offered by the catalogue: one report of one Connector, plus
 *  the account-paired recommendation that happens to name it, when there is
 *  one. A card is never invented — every one of them is a `report_profile` the
 *  Connector declares, which is what "a preset is derived, never written" means
 *  in `datastream-workbench-and-wizard.md:280`. */
type StartingPointState = "offered" | "unverified" | "unavailable" | "stale";
export interface StartingPoint {
  key: string;
  connector: DatastreamSetupConnectorOption;
  report: DatastreamSetupReportOption;
  recommendation: DatastreamSetupRecommendation | null;
  state: StartingPointState;
  reasonCode: string | null;
}

/** THREE ABSENCES, THREE SENTENCES, and never one badge that blurs them.
 *
 *  `stale`       the report WAS in the verified contract and is not any more —
 *                the provider withdrew it. Refused, because offering it would
 *                promise a report that no longer exists;
 *  `unavailable` the verified contract carries it and declares it not offered,
 *                with its own `reason_code`. Refused, and the code is shown;
 *  `unverified`  nothing has bound this Connector here, so no contract is
 *                pinned. NOT a fault and NOT stale: the module declares the
 *                report, no binding has committed to it yet, and the safety of a
 *                first pull is judged where it has always been judged.
 *                Selectable — binding is what writes the pin. */
type StartingPointStateCopy = { badge: string; tone: "warning" | "info"; sentence: string };
export const STARTING_POINT_STATE: Record<Exclude<StartingPointState, "offered">, StartingPointStateCopy> = {
  stale: {
    badge: "Stale",
    tone: "warning",
    sentence: "This report is no longer in the verified contract for this Connector.",
  },
  unavailable: {
    badge: "Unavailable",
    tone: "warning",
    sentence: "The verified contract declares this report as not offered.",
  },
  unverified: {
    badge: "Unverified",
    tone: "info",
    sentence:
      "Nothing has bound this Connector in this deployment yet, so what this card lists is what the "
      + "Connector declares. The contract is pinned when the Connector is bound.",
  },
};

/** What the safety engine answered about this report, said in words. Repeated
 *  from `recommend_first_report` (story 36.8), never re-derived: a second engine
 *  would be a second opinion on what a safe first pull is. */
const SAFETY_COPY: Record<string, string> = {
  recommended: "Safe first pull: the only compatible report family of this contract.",
  needs_choice: "Compatible, among several: the contract cannot choose for you.",
  no_safe_recommendation: "Not retained as a safe first pull by the contract.",
};
const SAFETY_TONE: Record<string, "success" | "warning" | "neutral"> = {
  recommended: "success", needs_choice: "warning", no_safe_recommendation: "neutral",
};

/** The catalogue, built from what the step already reads. No second route, no
 *  hand-written entry: a card exists because a report of a persisted contract
 *  or of a declared manifest exists, and for no other reason.
 *
 *  Narrowed to the chosen Connector as soon as there is one — before that, the
 *  operator is choosing WHICH Connector to start from, and the catalogue is how
 *  they see what each one offers. */
export function buildStartingPoints(
  connectors: DatastreamSetupConnectorOption[],
  recommendations: DatastreamSetupRecommendation[],
  selection: { connector_ref: string; report_ref: string },
): StartingPoint[] {
  const scoped = selection.connector_ref
    ? connectors.filter((item) => item.connector_ref === selection.connector_ref)
    : connectors;
  const points: StartingPoint[] = [];
  for (const connector of scoped) {
    const unverified = (connector.contract_state ?? "verified") === "unverified";
    for (const report of connector.reports) {
      const availability = (report.availability ?? null) as { status?: string; reason_code?: string } | null;
      const state: StartingPointState = availability?.status && availability.status !== "selectable"
        ? "unavailable"
        : unverified ? "unverified" : "offered";
      const matched = recommendations.filter(
        (item) => item.connector_ref === connector.connector_ref && item.report_ref === report.report_ref,
      );
      const reasonCode = availability?.reason_code ?? null;
      if (matched.length === 0) {
        points.push({
          key: `${connector.connector_ref}:${report.report_ref}`,
          connector, report, recommendation: null, state, reasonCode,
        });
        continue;
      }
      // One card per account-paired recommendation: two accounts serving the
      // same report family are two different starting points, and merging them
      // would hide the account choice the ranking exists to offer.
      for (const recommendation of matched) {
        points.push({ key: recommendation.recommendation_ref, connector, report, recommendation, state, reasonCode });
      }
    }
  }
  // A REPORT THE DRAFT NAMES AND THE CONTRACT NO LONGER CARRIES. Reachable on a
  // resumed draft, and it is the case the hand-written catalogue was refused
  // for: a card that keeps promising a report the provider withdrew.
  const chosen = scoped.find((item) => item.connector_ref === selection.connector_ref);
  if (selection.report_ref && chosen && !chosen.reports.some((item) => item.report_ref === selection.report_ref)) {
    points.push({
      key: `${chosen.connector_ref}:${selection.report_ref}`,
      connector: chosen,
      report: { report_ref: selection.report_ref, display_name: selection.report_ref, metrics: [], dimensions: [] },
      recommendation: null,
      state: "stale",
      reasonCode: null,
    });
  }
  return points;
}

/** How a saved template is projected — THROUGH THE CATALOGUE'S OWN PROJECTION
 *  (57.6), never a second one.
 *
 *  A template names a Connector and a report family, which is exactly what a
 *  starting point is, so `buildStartingPoints` answers `Stale` / `Unavailable` /
 *  `Unverified` for it with the same rule and the same words. Writing a second
 *  reading here would be two vocabularies for one fact, and the day the
 *  contract projection changes only one of them would follow.
 *
 *  `null` means the Connector itself is absent from `source-options` — a
 *  different fact from a withdrawn report, and it gets its own sentence. */
export function templateStartingPoint(
  template: DatastreamSetupTemplate,
  connectors: DatastreamSetupConnectorOption[],
): StartingPoint | null {
  if (template.mode !== "connector_pull" || !template.connector_ref) return null;
  const points = buildStartingPoints(connectors, [], {
    connector_ref: template.connector_ref,
    report_ref: template.report_ref ?? "",
  });
  return points.find((point) => point.report.report_ref === template.report_ref) ?? null;
}

/** Step 1's `Recommended`, which the target requires in its own words (`:48`):
 *  "best account and report family from project intent and observed metadata"
 *  — EXTENDED by story 57.6 into the completable preset catalogue.
 *
 *  It used to render at most six account-paired recommendations, and `return
 *  null` when there were none. That return is why nobody ever saw this grid:
 *  zero contract versions exist in this deployment, so the ranking is always
 *  empty and the empty state was invisible. It now shows a card per REPORT the
 *  Connector declares, carries the safety verdict rather than filtering on it,
 *  and states its emptiness instead of disappearing.
 *
 *  A card fills the source AND the two field lists of `Configure` it comes with
 *  — that is what makes it a preset — and nothing else: no window, no cadence,
 *  no filter. The window and offset shown are `Platform default`, because none
 *  of the 133 declared profiles carries either. */
export function RecommendedStarts({ points, platformDefaults, selectedKey, onSelect }: {
  points: StartingPoint[];
  platformDefaults?: DatastreamSetupPlatformDefaults | null;
  selectedKey: string | null;
  onSelect: (point: StartingPoint) => void;
}) {
  // THE DEPLOYMENT-WIDE VOID IS NO LONGER SAID HERE. Since the amendment of
  // 2026-08-10 this block is question 4, reached only once a Connector has been
  // chosen — so "no Connector module in this deployment" is unreachable from
  // here, and it is said at question 1, whose options are the ones missing. An
  // empty list here means this Connector declares no report family, which the
  // `Report family` select below states in its own words.
  if (points.length === 0) return null;
  return (
    <Panel className="grid gap-3 p-4">
      <PanelHeader
        title="Starting points"
        description={"One card per report family the Connector declares. Selecting one fills the source "
          + "and the metric and dimension lists of Configure, which stay editable — add your own on top, "
          + "or restore the preset."}
      />
      {/* ONE CARD PER ROW: two abreast leave ~178px for a title, two badges
          and a five-row list (the measurement lives at the wizard's grid). */}
      <ul className="m-0 grid list-none gap-3 p-0">
        {points.map((point) => {
          const { connector, report, recommendation, state } = point;
          const selected = selectedKey === point.key;
          const refused = state === "stale" || state === "unavailable";
          const stateCopy = state === "offered" ? null : STARTING_POINT_STATE[state];
          const sources = [
            ...new Set((recommendation?.evidence_refs ?? []).map((ref) => EVIDENCE_SOURCE[ref.kind])),
          ];
          const grain = report.smallest_declared_grain?.join(" / ")
            || recommendation?.derived_grain.join(" / ")
            || "Not declared";
          const safety = report.safety?.outcome ?? null;
          return (
            <li key={point.key}>
              <Panel className="grid h-full content-start gap-2 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <strong className="text-label text-text">{report.display_name}</strong>
                  {stateCopy && <Badge tone={stateCopy.tone}>{stateCopy.badge}</Badge>}
                  {recommendation && (
                    <Badge tone={CONFIDENCE_TONE[recommendation.confidence.level] ?? "neutral"}>
                      {recommendation.confidence.level} confidence
                    </Badge>
                  )}
                  {!recommendation && safety && (
                    <Badge tone={SAFETY_TONE[safety] ?? "neutral"}>{safety.replaceAll("_", " ")}</Badge>
                  )}
                </div>
                <p className="m-0 text-caption text-text-secondary">
                  {recommendation
                    ? `${recommendation.connector_display_name} — ${recommendation.source_account_label}`
                    : connector.display_name}
                </p>
                {recommendation && (
                  <p className="m-0 text-caption text-text-secondary">
                    {recommendation.confidence.rationale}
                  </p>
                )}
                {!recommendation && safety && (
                  <p className="m-0 text-caption text-text-secondary">{SAFETY_COPY[safety] ?? wireWord(safety)}</p>
                )}
                <dl className="m-0 grid grid-cols-2 gap-1 text-caption text-text-secondary">
                  <dt className="m-0">Smallest declared grain</dt>
                  <dd className="m-0 text-text">{grain}</dd>
                  <dt className="m-0">Fields</dt>
                  <dd className="m-0 text-text">
                    {report.metrics.length} metrics, {report.dimensions.length} dimensions
                  </dd>
                  {/* THE WINDOW IS THE PLATFORM'S, AND THE CARD SAYS SO. No
                      report profile declares one, so an unlabelled `30 days`
                      would attribute to the Connector a value it never
                      declared. Absent from the payload, the row is not drawn. */}
                  {platformDefaults && <>
                    <dt className="m-0">Window (Platform default)</dt>
                    <dd className="m-0 text-text">
                      {platformDefaults.date_window_days} days, offset{" "}
                      {platformDefaults.window_offset_days} day
                      {platformDefaults.window_offset_days === 1 ? "" : "s"}
                    </dd>
                  </>}
                  {recommendation && <>
                    <dt className="m-0">Estimated cost</dt>
                    <dd className="m-0 text-text">{recommendation.estimated_cost
                      ? (`${recommendation.estimated_cost.read_points ?? "?"} `
                        + `${recommendation.estimated_cost.unit ?? ""}`).trim()
                      : "Unavailable"}</dd>
                    <dt className="m-0">Evidence</dt>
                    <dd className="m-0 text-text">{sources.join(", ")}</dd>
                  </>}
                </dl>
                {stateCopy && (
                  <p className="m-0 text-caption text-text-secondary">
                    {stateCopy.sentence}
                    {point.reasonCode ? ` Reason code: ${point.reasonCode}.` : ""}
                  </p>
                )}
                <Button
                  variant={selected ? "secondary" : "default"}
                  disabled={refused}
                  aria-pressed={selected}
                  onClick={() => onSelect(point)}
                  aria-label={`Start from ${report.display_name}`
                    + (recommendation ? ` on ${recommendation.source_account_label}` : "")}
                >
                  {selected ? "Selected" : "Start from this"}
                </Button>
              </Panel>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

export interface SourceConnectorPullProps {
  input: ConnectorInput;
  options: DatastreamSetupSourceOptions | null;
  /** The Connector the draft names, resolved by the wizard (which reads it for
   *  the report and field lists of later steps too). */
  connector: DatastreamSetupConnectorOption | undefined;
  /** Step 1's second question, answered but never persisted: the draft accepts
   *  five source keys for `connector_pull` and refuses a sixth, and the account
   *  it does persist already names the authorization it belongs to. The wizard
   *  owns this client state; the panel reads it. */
  pickedAuthorization: string;
  /** The Project the consent is minted for. The authorization gesture lives in
   *  this panel now, and it is project-scoped on the server. */
  projectId: string;
  narrowNotice: string | null;
  appliedTemplate: DatastreamSetupTemplate | null;
  /** `TEMPLATE_COPY.accountGone` — the wizard owns the template copy block; the
   *  sentence itself is passed rather than duplicated. */
  savedAccountGoneCopy: string;
  onChooseConnector: (value: string) => void;
  onChooseAuthorization: (value: string) => void;
  onChooseSourceAccount: (accountRef: string) => void;
  /** Reload the step's options after the provider proved an account the
   *  enumeration did not carry. */
  onAccountVerified?: () => void;
}

export default function SourceConnectorPull({
  input, options, connector, pickedAuthorization, projectId, narrowNotice, appliedTemplate,
  savedAccountGoneCopy,
  onChooseConnector, onChooseAuthorization, onChooseSourceAccount, onAccountVerified,
}: SourceConnectorPullProps) {
  /** The cards the operator picks from: one per selectable Connector, carrying
   *  its brand mark. `connector_ref` is the module id, which is exactly the key
   *  `connector-logos.json` resolves, so the mark is the real logo or nothing —
   *  never a stand-in that would let a wrong product look right.
   *
   *  A Connector saved on the draft but absent from the options KEEPS A CARD.
   *  Dropping it would silently unselect a choice the operator made, and the
   *  radio would render as "nothing chosen" over a draft that does carry one. */
  const savedConnectorRef = input.source.connector_ref;
  /** WHICH CONNECTORS AN AUTHORIZATION OF THIS PROJECT OPENS. The one signal
   *  a card is worth carrying: the base catalogue is the same on every
   *  instance, so what an operator needs at a glance is which products they
   *  can actually connect HERE. */
  const openedConnectorNames = new Set(
    (options?.source_accounts ?? []).flatMap(
      (account) => (account.opens ?? []).filter((item) => item.available).map((item) => item.connector_name),
    ),
  );
  const isOpened = (connectorRef: string) =>
    openedConnectorNames.has(connectorRef)
    || (options?.source_accounts ?? []).some(
      (account) => account.connector_ref?.id === connectorRef,
    );
  const connectorCards = [
    ...(savedConnectorRef && !(options?.connectors ?? []).some(
      (item) => item.connector_ref === savedConnectorRef,
    )
      ? [{
        value: savedConnectorRef,
        label: savedConnectorRef,
        hint: "Saved on this draft — its contract is not available here.",
        icon: <ConnectorMark provider={savedConnectorRef} size={28} />,
      }]
      : []),
    ...(options?.connectors ?? [])
      // THE MODE NARROWS THE GRID. `Connector pull` offered all 39 modules,
      // `google-sheets` and `generic` among them — both `managed_feed`
      // channels, neither pullable. A manifest that declares no mode stays on
      // this path, which is what the catalogue did for every module before.
      .filter((item) => (item.onboarding_modes ?? ["connector_pull"]).includes("connector_pull"))
      // USABLE FIRST, then alphabetical: with the whole base catalogue on
      // every instance, the five this Project can connect must not page
      // below the thirty-four it cannot.
      .sort((a, b) =>
        Number(isOpened(b.connector_ref)) - Number(isOpened(a.connector_ref))
        || a.display_name.localeCompare(b.display_name))
      .map((item) => ({
        value: item.connector_ref,
        // The label stays a STRING: ChoiceGroup sets `aria-label` only from a
        // string label, and a node here would cost the card its accessible
        // name — the 27 `getByRole("radio", { name })` selectors, and every
        // spoken announcement (57.5's naming rule). The "Authorized" signal
        // is a `badge`, decorative and never part of the name.
        label: item.display_name,
        // A MARK, not a word: the "Authorized" badge measured ~90px wide and
        // ate the label of every compact card (live capture, 2026-08-10).
        // The sort already puts the usable products first; the check says
        // which and stays out of the accessible name.
        badge: isOpened(item.connector_ref)
          ? <CheckIcon className={`size-3.5 ${TONE_TEXT.success} [stroke-width:3]`} />
          : undefined,
        hint: CONNECTOR_CARD_HINT[item.contract_state ?? "verified"],
        icon: <ConnectorMark provider={item.connector_ref} size={20} />,
      })),
  ];
  /** The catalogue is the whole base registry — forty cards nobody should
   *  have to sweep. A filter names what the operator came for; nine or fewer
   *  fit in one look and the field would be furniture. */
  const [connectorFilter, setConnectorFilter] = useState("");
  /** The grid is back on screen because the operator asked for it, not because
   *  the draft forgot its answer. Client state: reopening a question is not a
   *  change to the Datastream and has nothing to persist. */
  const [changingConnector, setChangingConnector] = useState(false);
  const [typedAccount, setTypedAccount] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [verifyError, setVerifyError] = useState("");
  const visibleConnectorCards = connectorFilter.trim()
    ? connectorCards.filter((card) =>
      `${typeof card.label === "string" ? card.label : card.value}`
        .toLowerCase()
        .includes(connectorFilter.trim().toLowerCase()))
    : connectorCards;

  /** Named rather than silently dropped, and read at the question it belongs to.
   *
   *  A Connector this Project's authorizations open can be absent from the
   *  catalogue for ONE reason: the deployment holds no module for it, or the
   *  module declares no source capability. That is a missing MODULE, not a
   *  missing verification run — and an operator shown nothing cannot tell either
   *  from "nothing to connect".
   *
   *  It was measured on the SELECTED account, which the amendment moved after
   *  this question; read across every account of the Project, it answers at
   *  question 1, where the operator is actually looking. */
  const openedWithoutModule = [
    ...new Map(
      (options?.source_accounts ?? [])
        .flatMap((account) => (account.opens ?? []).filter((item) => item.available))
        .filter((opened) => !(options?.connectors ?? []).some(
          (item) => item.connector_ref === opened.connector_name,
        ))
        .map((opened) => [opened.connector_name, opened] as const),
    ).values(),
  ];

  /** THE CHAIN THIS STEP IS, since the amendment of 2026-08-10: the Connector,
   *  then the authorization that opens it, then the account that authorization
   *  serves, then the template or the fields.
   *
   *  It ran the other way — account, then Connector — and the inversion is what
   *  made every empty state unreadable: an operator who had chosen nothing was
   *  told no Connector contract was persisted in this deployment, when what they
   *  needed was that this PRODUCT has no authorization here and where to make
   *  one. The Connector is also the only question of the four answerable with no
   *  prior setup, which is why it is asked first.
   *
   *  So the catalogue is NOT narrowed by an account: nothing has been chosen
   *  when it is read. `opens` still answers, one question later, which
   *  authorizations and accounts can serve the Connector just chosen. */
  const chosenConnectorRef = input.source.connector_ref;
  const selectedAccount = (options?.source_accounts ?? []).find(
    (account) => sourceAccountId(account) === input.source.source_account_ref,
  );
  /** Question 2's options: the authorizations of this Project that open the
   *  chosen Connector, each carrying the accounts it serves. Empty before a
   *  Connector is chosen — the question does not exist yet, and a list drawn
   *  from every account would be answering a question nobody asked. */
  const connectorAccounts = chosenConnectorRef
    ? (options?.source_accounts ?? []).filter(
      (account) => accountServesConnector(account, chosenConnectorRef),
    )
    : [];
  const authorizations = authorizationOptions(connectorAccounts);
  /** WHICH authorization is answering. A chosen account NAMES its own — the id
   *  travels on the account, so the two can never disagree — and before an
   *  account exists the operator's explicit pick answers, or the single
   *  candidate does. Defaulting stops at one: "choosing it is a decision only
   *  when there are several", and guessing between two consents would bind the
   *  Datastream to a credential nobody picked.
   *
   *  It is CLIENT state and not an operator field, because the draft accepts
   *  none: `_OPERATOR_SOURCE_KEYS["connector_pull"]` is a closed set of five
   *  keys and a sixth is refused at PATCH. Nothing is lost — the account the
   *  step does persist carries the authorization it belongs to. */
  const authorizationOfAccount = selectedAccount ? accountAuthorizationId(selectedAccount) : null;
  const activeAuthorization = selectedAccount
    ? authorizations.find((entry) => entry.id === authorizationOfAccount) ?? null
    : authorizations.length === 1
      ? authorizations[0]
      : authorizations.find((entry) => authorizationValue(entry) === pickedAuthorization) ?? null;
  const authorizationIsDefaulted = !selectedAccount && authorizations.length === 1;
  /** Question 3's options: the accounts of THAT authorization, never the whole
   *  registry. An account of another consent listed here would let the two
   *  answers contradict each other. */
  const accountChoices = activeAuthorization?.accounts ?? [];

  /** The provider is asked; nothing here decides. On success the step reloads
   *  its options, because a proven account is a Source Account like any other --
   *  the server projects it into `credential_accounts`, which every surface
   *  reads. */
  async function verifyTypedAccount() {
    const authorizationId = activeAuthorization?.id;
    if (!authorizationId) return;
    setVerifying(true);
    setVerifyError("");
    try {
      const outcome = await verifyConnectionAccount(
        authorizationId, typedAccount.trim(), chosenConnectorRef,
      );
      if (outcome.ok) {
        setTypedAccount("");
        onAccountVerified?.();
      } else {
        setVerifyError(outcome.message);
      }
    } catch (err) {
      setVerifyError(err instanceof Error ? err.message : String(err));
    } finally {
      setVerifying(false);
    }
  }


  return <div className="grid gap-4">
    {/* QUESTION 1 — THE CONNECTOR, and it is asked first because it is the
        only one of the four answerable with no prior setup: a person
        arrives wanting a product, not wanting to expose an account
        (amendment of 2026-08-10).

        IT IS PICKED FROM CARDS, NOT FROM A SYSTEM DROPDOWN. A native
        `<option>` cannot carry an image, so the one field where the
        operator most needs to recognise a product at a glance was the one
        field with no brand mark — while six other surfaces of the console
        already render `ConnectorMark`. The card grid is the same
        `ChoiceGroup variant="card"` as the Mode row directly above, so
        step 1 reads as one screen instead of two eras.

        The mark resolves from `connector_ref`, which IS the module id
        (`meta-ads`, `youtube-analytics`) — the exact key
        `connector-logos.json` is generated against, so a Connector that
        exists always shows its own logo and never a placeholder. */}
    <Field label="Connector" required>
      {({ id }) => chosenConnectorRef && !changingConnector ? (
        /* THE QUESTION IS ANSWERED, SO IT STOPS BEING A CATALOGUE.
           Thirty-nine cards stayed on screen after the choice, and the
           questions the answer UNLOCKS sat below them -- a person who had
           already decided still had to scroll past everything they had just
           declined. The answer collapses to itself and the next question comes
           up to meet it; `Change` puts the grid back, because a choice one
           cannot revisit is a trap rather than a step. */
        <div className={ANSWERED_ROW}>
          <span className="flex min-w-0 items-center gap-2.5">
            <ConnectorMark provider={chosenConnectorRef} size={24} />
            <span className="truncate text-ui text-text">
              {connector?.display_name ?? chosenConnectorRef}
            </span>
            {isOpened(chosenConnectorRef) && <Badge tone="success">Authorized</Badge>}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setChangingConnector(true)}
          >
            Change
          </Button>
        </div>
      ) : (
        <>
          {connectorCards.length > 9 && (
            <Input
              type="search"
              aria-label="Filter connectors"
              placeholder="Filter connectors"
              value={connectorFilter}
              onChange={(event) => setConnectorFilter(event.target.value)}
              className="mb-3"
            />
          )}
          <ChoiceGroup
            id={id}
            variant="card"
            density="compact"
            aria-label="Connector"
            // Two abreast at 1280 (the task area is ~372px; a third column
            // truncates every product name, measured live), three from the
            // canonical 1440 composition up.
            className="sm:grid-cols-2 min-[1440px]:grid-cols-3"
            // `""` and never `undefined`: the group is controlled from the
            // first render, as on the Mode row (an uncontrolled-to-controlled
            // switch loses the keyboard position and warns in React).
            value={input.source.connector_ref || ""}
            onValueChange={(value) => {
              setChangingConnector(false);
              setConnectorFilter("");
              onChooseConnector(value);
            }}
            choices={visibleConnectorCards}
          />
          {connectorFilter.trim() && visibleConnectorCards.length === 0 && (
            <Status as="block" tone="neutral" title="No Connector matches this filter">
              The base catalogue carries no product under that name.
            </Status>
          )}
        </>
      )}
    </Field>
    {/* THE DEPLOYMENT HOLDS NO CONNECTOR AT ALL. Said here, at the
        question whose options are missing — the catalogue of starting
        points used to say it, and that block is now question 4, which a
        deployment with no Connector never reaches. */}
    {options && connectorCards.length === 0 && (
      <Status as="block" tone="warning" title={NO_CONTRACT_COPY.title}>{NO_CONTRACT_COPY.body}</Status>
    )}
    {/* The other way this question dead-ends, told apart: authorizations
        of this Project DO open Connectors, and this deployment carries no
        module for them. "Nothing to connect" and "nothing installed" are
        different problems with different repairs. */}
    {options && openedWithoutModule.length > 0 && (
      <Status
        as="block"
        tone="warning"
        title="No Connector for what this Project authorizes"
      >
        The authorizations of this Project open
        {" "}{openedWithoutModule.map((item) => item.display_name).join(", ")}, but this
        deployment holds no module declaring source capabilities for
        {" "}{openedWithoutModule.length > 1 ? "any of them" : "it"}. Until one is installed
        {" "}{openedWithoutModule.length > 1 ? "they cannot" : "it cannot"} be pulled from.
      </Status>
    )}
    {/* WHICH ANSWER THIS CONNECTOR TOOK WITH IT. A field that empties
        itself without a word is how an operator loses a configuration and
        cannot say what took it. */}
    {narrowNotice && (
      <Status as="block" tone="warning" title={AUTHORIZATION_COPY.accountClearedTitle}>
        {narrowNotice}
      </Status>
    )}
    {/* QUESTIONS 2 AND 3 ARE ASKED INSIDE QUESTION 1'S ANSWER, so they are
        not drawn before it: an authorization select filled from every
        consent of the Project would be answering a question nobody asked,
        and its emptiness would name the wrong absence — which is the whole
        reason the order was amended. */}
    {connector && <>
      {/* QUESTION 2 — THE AUTHORIZATION. Defaulted when exactly one opens
          this Connector; a decision only when there are several. */}
      <Field
        label="Authorization"
        required
        hint={authorizationIsDefaulted ? AUTHORIZATION_COPY.defaultedHint : AUTHORIZATION_COPY.hint}
      >
        {({ id }) => (
          <NativeSelect
            id={id}
            value={activeAuthorization ? authorizationValue(activeAuthorization) : ""}
            onChange={(event) => onChooseAuthorization(event.target.value)}
          >
            <option value="">Select an authorization</option>
            {authorizations.map((entry) => (
              <option key={authorizationValue(entry)} value={authorizationValue(entry)}>
                {entry.label}
              </option>
            ))}
          </NativeSelect>
        )}
      </Field>
      {/* THE SENTENCE THE AMENDMENT EXISTS FOR. An operator who has chosen
          a product and holds no consent for it is told THAT, and where a
          consent is made — never that no Connector contract is persisted
          in this deployment, which is true and unusable. */}
      {options && authorizations.length === 0 && (
        <Status
          as="block"
          tone="warning"
          title={AUTHORIZATION_COPY.noneTitle(connector.display_name)}
        >
          <div className="grid gap-3 justify-items-start">
            <span>{AUTHORIZATION_COPY.noneBody(connector.authorization_kind)}</span>
            {connector.authorization_kind === "google_direct"
              ? <ConnectGoogleButton projectId={projectId} label="Connect Google" />
              : <ConnectButton
                  projectId={projectId}
                  fixedProvider={connector.connector_ref}
                  label={`Connect ${connector.display_name}`}
                  // The step reloads its options, exactly as after a verified
                  // account: a new consent changes what question 2 can answer.
                  onSuccess={() => onAccountVerified?.()}
                />}
          </div>
        </Status>
      )}
      {/* QUESTION 3 — THE ACCOUNT that authorization serves. Its options
          come from the authorization, never from the registry: an account
          of another consent listed here would let two answers of the same
          step name different credentials.

          ASKED ONLY ONCE THE AUTHORIZATION ANSWERS (2026-08-10, UX pass):
          a select whose options cannot exist yet is a question nobody was
          asked — the same rule that keeps questions 2 and 3 inside
          question 1's answer, applied one rank down. */}
      {activeAuthorization && <>
      <Field label="Source Account" required>
        {({ id }) => (
          <NativeSelect
            id={id}
            value={input.source.source_account_ref}
            onChange={(event) => onChooseSourceAccount(event.target.value)}
          >
            <option value="">Select an exposed account</option>
            {input.source.source_account_ref && !accountChoices.some(
              (account) => sourceAccountId(account) === input.source.source_account_ref,
            ) && (
              <option value={input.source.source_account_ref}>
                Saved Source Account — options unavailable
              </option>
            )}
            {accountChoices.map((account) => (
              <option key={sourceAccountId(account)} value={sourceAccountId(account)}>
                {sourceAccountOption(account)}
              </option>
            ))}
          </NativeSelect>
        )}
      </Field>
      {activeAuthorization && accountChoices.length === 0 && (
        <Status as="block" tone="warning" title={AUTHORIZATION_COPY.noAccountTitle}>
          {AUTHORIZATION_COPY.noAccountBody}
        </Status>
      )}
      {/* NAME WHAT THE PROVIDER WILL NOT LIST. Enumeration and authorization are
          two different questions: `channels.list(mine=true)` returns the one
          channel of the consenting identity, while `reports.query` answers for
          every channel that identity MANAGES -- an agency's whole business. The
          server runs this Connector's own declared access check and refuses when
          the provider refuses; nothing is trusted from here. */}
      {activeAuthorization?.id && (
        <details className="rounded-control border border-border-quiet p-3">
          <summary className="cursor-pointer text-caption text-text-secondary">
            The account you need is not listed?
          </summary>
          <div className="mt-3 grid gap-2">
            <p className="m-0 text-caption text-text-secondary">
              {connector.display_name} lists only what its provider enumerates. Paste what you
              have -- the account's handle, its URL, or its identifier -- and it will be checked
              against the provider itself.
            </p>
            <div className="flex gap-2">
              <Input
                aria-label="Account handle, URL or identifier"
                placeholder="@handle, URL, or identifier"
                value={typedAccount}
                onChange={(event) => setTypedAccount(event.target.value)}
              />
              <Button
                type="button"
                variant="secondary"
                disabled={!typedAccount.trim() || verifying}
                onClick={() => void verifyTypedAccount()}
              >
                {verifying ? "Checking..." : "Check access"}
              </Button>
            </div>
            {verifyError && (
              <Status as="block" tone="error" title="The provider did not grant this account">
                {verifyError}
              </Status>
            )}
          </div>
        </details>
      )}
      {/* THE FIFTH STATE OF A TEMPLATE, and it is not `Stale` (57.7): the
          saved account is gone or is no longer exposed here. The card stays
          applicable — the account is the variable this gesture reopens — so
          the sentence belongs to the field that is now empty. */}
      {appliedTemplate && (appliedTemplate.open_variables ?? []).includes("source_account_ref")
        && !input.source.source_account_ref && Boolean(appliedTemplate.origin_source_account_ref) && options
        && !options.source_accounts.some(
          (account) => sourceAccountId(account) === appliedTemplate.origin_source_account_ref,
        )
        && <Status as="block" tone="warning" title="The saved Source Account is not available here">
          {savedAccountGoneCopy}
        </Status>}
      </>}
      {/* QUESTION 4 IS ASKED AT `Configure`: the starting point and the report
          family are what fills the fields of that step (`:184` makes the
          report its Required item), so they open it — the wizard renders
          `RecommendedStarts` there. This panel keeps the three questions that
          NAME the source. */}
    </>}
  </div>;
}
