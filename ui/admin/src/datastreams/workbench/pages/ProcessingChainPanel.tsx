/**
 * The ordered processing chain — what this Datastream DOES, in what order.
 *
 * ## Why this block is first on the tab, and why it is a stepper
 *
 * Finding D-5 of the 2026-08-12 visual review (github issue #69): on
 * `Processing` the nine-step chain was the LAST block of the page, rendered as a
 * four-column table. `Processing` answers « what does this Datastream do, in
 * what order » — the schedule is *when* it does it, a parameter of the plan and
 * not the plan. The thing a person comes for was below four other panels.
 *
 * A table is the right DETAIL and the wrong FIRST READING: nine rows of four
 * columns is a lookup, not a sequence. So the block opens with a horizontal
 * `Stepper` carrying each step's name and its state, and the table stays under
 * it as the unfolding that names what pins each step, its evidence and the
 * surface that repairs it.
 *
 * `Stepper` is the LIBRARY primitive (`ui/Stepper.tsx`), used and not rebuilt.
 * Its header separates it from `PhaseTimeline` — "a stepper is navigation
 * through your own work; a phase timeline is evidence of the machine's". This
 * chain is neither a run nor a wizard: it is the ordered plan a person reads and
 * then goes and repairs, step by step, on the surface each one names. It has no
 * per-phase time and every step is a door, so `Stepper` is the object; a third
 * primitive for one caller would be the drift this library exists against.
 *
 * ## The two properties of the old table that had to survive
 *
 *   * **A step whose evidence is absent SAYS SO** rather than disappearing. That
 *     is what `ChainState` is: `unknown` is not `none`. "No cleanup rule reaches
 *     this Datastream" and "the cleanup rules could not be read" are two
 *     different sentences, and rendering the second as the first would tell a
 *     person nothing is being removed from their data when nobody knows.
 *   * **Each step names the owner surface that repairs it.** The table's Owner
 *     column is unchanged and still a live door (`tab`, `href`, `ownerRef`); the
 *     stepper names that surface again, as text, on exactly the steps that are
 *     `unknown` — the ones where "go there" is the next gesture.
 */
import type { ReactNode } from "react";
import {
  Badge, EmptyState, Panel, PanelHeader, Status, Stepper,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
} from "../../../ui";
import type { Step as StepperStep } from "../../../ui";
import { filledRecord, record, records, text, titleCase } from "../evidence";
import type { OwnerReference } from "../../../shell/pages/ProjectSettings";
import type { Tab } from "../../../shell/pages/datastreamTabs";
import type { WorkbenchHeader } from "../workbenchTypes";

/** The owner of a step — and the way to reach it.
 *
 *  The column named five surfaces ("Mapping", "Outputs", "Project Settings"…)
 *  as plain text: it told the reader where to go and refused to take them
 *  there. An eight-step chain that ends every row with a dead-end name is a
 *  table of instructions, not a control plane. `tab` routes inside the
 *  Workbench, `href` leaves it; a step with neither keeps its label, because
 *  "Controls & Quality" naming the owner is still worth more than nothing.
 *
 *  `ownerRef` is the THIRD way, and the only one that can name a LENS. The
 *  header composes three addresses — source, project settings, governance — and
 *  none of them reaches a collection inside a section: a reference stopping at
 *  `semantic-model` lands on `concepts`, that section's declared default, which
 *  is a different collection (the failure story 58.9 paid for with `Add a
 *  check`). A semantic `OwnerReference` carries the lens and is resolved by the
 *  shell against `navigation.ts`, so an untrusted or retired lens becomes an
 *  absence instead of a dead link — the same guarantee `href` gets from
 *  `resolvableHrefs`. */
interface StepOwner {
  label: string;
  tab?: Tab;
  href?: string | null;
  ownerRef?: OwnerReference;
}

/** The header's three owner addresses, each already judged by the router. */
export type OwnerLinks = { [K in keyof WorkbenchHeader["links"]]: string | null };

/**
 * Whether this version settles a step, and the difference between the two ways
 * it can fail to.
 *
 *   `pinned`   this plan version settles it, and the table says with what.
 *   `none`     it is settled, and the answer is that nothing applies here.
 *   `unknown`  nothing settles it — the evidence is absent or unreadable.
 *
 * Every step declares its own state beside the sentence it renders, so the two
 * can never disagree. Deriving it by reading the `pinned` string back would make
 * "Unavailable" and a plan that genuinely pins the word indistinguishable.
 */
type ChainState = "pinned" | "none" | "unknown";

/** What the stepper says about a step before anything is unfolded. */
const CHAIN_STATE: Record<ChainState, string> = {
  pinned: "Pinned",
  none: "Nothing applies",
  unknown: "Nothing pins it",
};

interface ChainStep {
  /** The step without its number: the stepper numbers its own marks. */
  title: string;
  pinned: string;
  detail: string;
  state: ChainState;
  owner?: StepOwner;
}

/** A step once the chain has fixed its rank. `name` is the table's own column. */
interface NumberedStep extends ChainStep {
  name: string;
}

function list(value: unknown): string[] {
  return Array.isArray(value) ? value.map((entry) => String(entry)) : [];
}

/** What the cleanup rules of this Datastream contribute to the chain — Story 60.3.
 *
 *  READ-ONLY, and the absence of any control here is the contract, not an
 *  omission: `datastream-workbench-and-wizard.md:989` says governed rules are
 *  referenced and not edited, and `data.md:82-85` says this tab applies published
 *  Governance policy and does not redefine it. The one way in is the Owner
 *  column, and it is a LIVE door: `CLEANUP_RULES_OWNER` names the lens, not just
 *  the section, so it opens the collection this step is about instead of the
 *  section's default one. A step that names a surface and refuses to take the
 *  reader there is the dead-end this file's own header was written against.
 *
 *  Three states and three sentences. `unavailable` is NOT `empty`: a store that
 *  could not be read must not print "no cleanup rule", which a person would read
 *  as "nothing is being removed from my data". All three carry the same door:
 *  a store that could not be read is precisely when a person needs to go look.
 */
const CLEANUP_RULES_OWNER: OwnerReference = {
  surface: "workspace",
  workspace: "governance",
  section: "semantic-model",
  global_surface: null,
  global_section: null,
  // The whole reason this is an OwnerReference and not an href: the shell
  // validates the lens against `navigation.ts` (`sectionOwnsLens`) before it
  // navigates, and drops it rather than landing on `concepts`.
  lens: "cleanup-rules",
  object_type: null,
  object_id: null,
  tab: null,
  action: null,
  version_id: null,
  evidence_id: null,
};

function cleanupStep(evidence: unknown): ChainStep {
  const chain = record(evidence);
  const state = text(chain?.state, "unavailable");
  const rules = records(chain?.rules);
  const owner: StepOwner = {
    label: "Governance › Cleanup Rules",
    ownerRef: CLEANUP_RULES_OWNER,
  };

  if (state === "unavailable") {
    return {
      title: "Cleanup rules",
      pinned: "Unavailable",
      state: "unknown",
      detail: text(
        chain?.reason,
        "The cleanup rules of this Project could not be read, so this step is unknown.",
      ),
      owner,
    };
  }
  if (rules.length === 0) {
    return {
      title: "Cleanup rules",
      pinned: "None",
      state: "none",
      detail:
        "No cleanup rule reaches this Datastream, so nothing is removed or rewritten when it is read.",
      owner,
    };
  }
  const enabled = rules.filter((rule) => rule.enabled === true);
  return {
    title: "Cleanup rules",
    pinned: `${enabled.length} of ${rules.length} enabled`,
    state: "pinned",
    // Each rule states its own condition, in words, exactly as the server
    // composed it — this tab never re-derives a sentence Governance owns.
    detail: rules
      .map(
        (rule) =>
          `${text(rule.name)} (${rule.enabled === true ? "enabled" : "disabled"}): ${text(rule.condition)}`,
      )
      .join(" · "),
    owner,
  };
}

/**
 * The ordered chain, derived from the plan version that pins it.
 *
 * AC6 asks for "the ordered source/import, parse, mapping, capability
 * projection, transformation, DQ and Output steps from immutable
 * plan/mapping/projection evidence". A step whose evidence is absent says so
 * instead of disappearing — an absent step is the finding, not a gap in the list.
 *
 * EIGHT of the nine steps read the plan's normalized intent. The ninth does not,
 * and that is why the transformation step was missing for so long: a cleanup rule
 * is NOT pinned to a plan version — it is a governed object, edited in Governance
 * and applied at read (story 60.3). It therefore reads its own evidence key
 * (`cleanup_rules`, served beside the plans by `core/datastream_workbench.py`)
 * and carries no control of any kind, because
 * `datastream-workbench-and-wizard.md:989` says governed rules are referenced
 * here and not edited. Until it was added, this comment promised a step the
 * chain below did not render.
 *
 * The rank is applied HERE, from the array order, and never typed into a label:
 * `6 · Cleanup rules` was written by hand in three branches of `cleanupStep`, so
 * inserting a step anywhere above it would have renumbered nothing.
 */
export function processingSteps(
  plan: Record<string, unknown> | null,
  mappingVersion: string | null,
  links: OwnerLinks | undefined,
  cleanup: unknown,
): NumberedStep[] {
  const source = record(plan?.source) ?? {};
  const selection = record(source.selection) ?? {};
  const destination = record(plan?.destination) ?? {};
  const schedule = record(plan?.schedule) ?? {};
  const historical = record(plan?.historical) ?? {};
  const geographic = record(plan?.geographic);
  const metrics = list(selection.metrics);
  const dimensions = list(selection.dimensions);
  const filters = Array.isArray(selection.filters) ? selection.filters.length : 0;
  const grain = list(selection.grain);
  const external = record(source.external_object);
  const feed = record(source.managed_feed);

  const sourceDetail = external
    ? `${text(external.project)}.${text(external.dataset)}.${text(external.object)}`
    : feed
      ? `${text(feed.format)} feed · ${text(feed.source_ref)}`
      : `${text(source.module)} · report ${text(source.report_id)}`;

  const steps: ChainStep[] = [
    {
      title: "Source or import",
      pinned: titleCase(source.kind),
      state: typeof source.kind === "string" && source.kind.trim() ? "pinned" : "unknown",
      detail: sourceDetail,
      owner: { label: "Source Account", href: links?.source },
    },
    {
      title: "Parse and select",
      pinned: titleCase(selection.selection_mode),
      state: metrics.length || dimensions.length || filters ? "pinned" : "unknown",
      detail: metrics.length || dimensions.length || filters
        ? `${metrics.length} metric(s), ${dimensions.length} dimension(s), ${filters} filter(s)`
        : "No selection is pinned in this plan version",
    },
    {
      title: "Map to governed fields",
      pinned: mappingVersion ?? "Unavailable",
      state: mappingVersion ? "pinned" : "unknown",
      detail: mappingVersion
        ? "The active mapping version binds each physical field; see the Mapping tab"
        : "No active mapping version, so nothing binds the physical fields",
      owner: { label: "Mapping", tab: "mapping" },
    },
    {
      title: "Capability projection",
      pinned: geographic ? titleCase(geographic.mode) : "None active",
      // A plan no capability reaches is SETTLED, not unreadable: nothing is
      // projected into it and that is the answer, not a missing one.
      state: geographic ? "pinned" : "none",
      detail: geographic
        ? `${list(geographic.country_codes).length} country code(s), ${list(geographic.markets).length} market(s) · ${titleCase(geographic.compilation_status)}`
        : "No capability projects an effect into this plan version",
      owner: { label: "Project Settings", href: links?.project_settings },
    },
    {
      title: "Resulting grain",
      pinned: grain.length ? grain.join(", ") : "Not established",
      state: grain.length ? "pinned" : "unknown",
      detail: grain.length
        ? "Rows are unique on these columns after processing"
        : "This plan version pins no grain, so uniqueness after processing is unproven",
    },
    // Story 60.3: the transformation step the docstring above has promised since
    // this page was written, and that the chain did not render.
    cleanupStep(cleanup),
    {
      title: "Data quality",
      pinned: "Governed policies",
      state: "pinned",
      detail: "DQ rules are referenced, never redefined here",
      owner: { label: "Controls & Quality", href: links?.governance },
    },
    {
      title: "Output",
      pinned: titleCase(destination.policy),
      state: typeof destination.policy === "string" && destination.policy.trim() ? "pinned" : "unknown",
      detail: "Publication advances the Output pointers atomically, from Outputs",
      owner: { label: "Outputs", tab: "outputs" },
    },
    {
      title: "Cadence and history",
      pinned: schedule.mode
        ? `${titleCase(schedule.mode)}${schedule.interval_minutes ? ` · every ${String(schedule.interval_minutes)} min` : ""}`
        : "Unavailable",
      state: schedule.mode ? "pinned" : "unknown",
      detail: `${text(schedule.timezone, "No timezone")} · history ${text(historical.start, "open")} → ${text(historical.end_exclusive, "open")}`,
      owner: { label: "Project Settings", href: links?.project_settings },
    },
  ];

  return steps.map((step, index) => ({ ...step, name: `${index + 1} · ${step.title}` }));
}

/** The Owner cell — one door, chosen by the address the step actually carries. */
function OwnerCell({
  owner,
  onOpenOwner,
  onNavigateTab,
}: {
  owner?: StepOwner;
  onOpenOwner?: (owner: OwnerReference) => void;
  onNavigateTab?: (tab: Tab) => void;
}): ReactNode {
  if (owner?.tab && onNavigateTab) {
    return (
      <button type="button" className="text-primary underline" onClick={() => onNavigateTab(owner.tab!)}>
        {owner.label}
      </button>
    );
  }
  if (owner?.href) {
    return <a className="text-primary underline" href={owner.href}>{owner.label}</a>;
  }
  if (owner?.ownerRef && onOpenOwner) {
    // A semantic reference, handed to the shell so the LENS is validated before
    // anything navigates. Without this branch the step named Governance and
    // refused to open it — the dead end this column was built to end.
    return (
      <button type="button" className="text-primary underline" onClick={() => onOpenOwner(owner.ownerRef!)}>
        {owner.label}
      </button>
    );
  }
  return owner?.label ?? "This tab";
}

export default function ProcessingChainPanel({
  plan,
  isActive,
  mappingVersion,
  cleanup,
  links,
  onOpenOwner,
  onNavigateTab,
}: {
  /** The plan version being read, or `null` when none has ever been persisted. */
  plan: Record<string, unknown> | null;
  isActive: boolean;
  mappingVersion: string | null;
  cleanup: unknown;
  links?: OwnerLinks;
  onOpenOwner?: (owner: OwnerReference) => void;
  onNavigateTab?: (tab: Tab) => void;
}) {
  // THE BLOCK NEVER DISAPPEARS, because it is now the first thing on the tab and
  // a hole at the top of a page is not an answer. With no plan version at all it
  // says that, and names the ledger below as the place a first one is appended.
  if (plan === null) {
    return (
      <Panel flush>
        <PanelHeader
          title="Ordered processing chain"
          description="What this Datastream does, in order, as pinned by a plan version."
        />
        <div className="p-5">
          {/* THE LEDGER IS NAMED BY ITS EXACT TITLE, not paraphrased. « nomme le
              registre en dessous comme l'endroit où une première version
              s'ajoute » (`datastream-workbench-and-wizard.md`, D-5) — and a
              person scanning for "the processing versions" finds no heading of
              that name on the page, which is a gesture named and not findable.

              The title says what is missing rather than what the Datastream is
              not doing: a Datastream with no plan version still collects, so
              "nothing runs on this Datastream's data" was wider than the fact. */}
          <EmptyState
            title="No plan version pins this chain yet"
            description="No processing has been agreed, so what this Datastream does to what it collects — and in what order — is undecided. Append a first version from Immutable processing plan versions below."
          />
        </div>
      </Panel>
    );
  }

  const normalized = filledRecord(plan.normalized_payload);
  const issues = records(plan.validation_issues);
  const steps = normalized === null
    ? []
    : processingSteps(normalized, mappingVersion, links, cleanup);

  return (
    <Panel flush>
      <PanelHeader
        title={`Ordered processing chain · ${text(plan.id)}`}
        description={
          isActive
            ? "What this Datastream does, in order, as pinned by the active plan version."
            : "A non-live proposal. It runs only once its Ready candidate is published from Outputs."
        }
        actions={
          <Badge tone={plan.executable === true ? "success" : "error"}>
            {plan.executable === true ? "Executable" : "Not executable"}
          </Badge>
        }
      />
      {normalized === null ? (
        <div className="p-5">
          <Status as="block" tone="warning" title="No plan contract in this version">
            This version carries no normalized contract, so its ordered chain cannot be read.
          </Status>
        </div>
      ) : (
        <>
          {/* THE FLOW, FIRST. Nine names and nine states, read left to right in
              one pass. The table under it is the same nine steps unfolded — the
              detail, which is what a table is good at and a first reading is
              not. */}
          <div className="border-b border-divider-base p-5">
            <Stepper
              orientation="horizontal"
              steps={steps.map((step): StepperStep => ({
                label: step.title,
                // A step nothing pins is greyed and SAYS which surface repairs
                // it. The table below carries the full sentence and the live
                // door; this is the summary that makes the gap findable.
                state: step.state === "unknown" ? "todo" : "done",
                detail: (
                  <>
                    <span className="block">{CHAIN_STATE[step.state]}</span>
                    {step.state === "unknown" && step.owner ? (
                      <span className="mt-0.5 block">Repair in {step.owner.label}</span>
                    ) : null}
                  </>
                ),
              }))}
            />
          </div>
          <TableScroll label="Ordered processing steps">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Step</TableHead>
                  <TableHead>Pinned to</TableHead>
                  <TableHead>Evidence</TableHead>
                  <TableHead>Owner</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {steps.map((step) => (
                  <TableRow key={step.name}>
                    <TableCell className="font-semibold">{step.name}</TableCell>
                    <TableCell className="font-mono text-caption">{step.pinned}</TableCell>
                    <TableCell className="text-ui text-text-secondary">{step.detail}</TableCell>
                    <TableCell className="text-caption text-text-secondary">
                      <OwnerCell
                        owner={step.owner}
                        onOpenOwner={onOpenOwner}
                        onNavigateTab={onNavigateTab}
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        </>
      )}
      {issues.length > 0 && (
        <div className="border-t border-divider-base p-5">
          <Status as="block" tone="error" title={`${issues.length} validation issue(s) on this plan version`}>
            {issues.map((issue) => `${text(issue.path, "$")}: ${text(issue.message)}`).join(" · ")}
          </Status>
        </div>
      )}
    </Panel>
  );
}
