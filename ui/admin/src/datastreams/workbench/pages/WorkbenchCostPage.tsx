/**
 * The `Cost` tab — what the governed fee/tax cascade adds to this Datastream.
 *
 * IT EXISTS BECAUSE THE CAPABILITY OPENED IT. `datastream-workbench-and-wizard.md`,
 * amendment « Une capacité activée AJOUTE son onglet » — named, never numbered,
 * because that document's line numbers moved in the commit that applied its
 * predecessor. When `tax_fees` is off there is no tab, no panel and no reserved
 * width: this component is not mounted at all, and its address does not resolve.
 *
 * NOTHING IS WRITTEN HERE. Rules are edited through the MCP and the governed Rule
 * Set; the analysis waterfall lives in Analyze. The one gesture is a semantic
 * `owner_reference` to Governance, resolved by the shell.
 *
 * THE STEP IS THE PHASE, AND THE PHASE NAMES ITS LEVELS. The mart aggregates by
 * phase and carries no relation of rule to contributed amount, so a step per rule
 * WITH its amount cannot be read from anything shipped — and the screen says so
 * instead of implying it. What it does say, which is the reading that was asked
 * for, is at which LEVEL each phase was laid: Project, plan version, Datastream.
 *
 * NO RULE IDENTIFIER IS PRINTED BESIDE AN AMOUNT. A name next to a number invites
 * reading the number as that rule's contribution; a false attribution is worse
 * than an aggregation that admits what it aggregates. The refusal footer names
 * rule keys, and nothing there carries an amount.
 *
 * EVERY NUMBER IS THE SERVER'S. No total is recomposed here, no percentage is
 * recomputed, and an absent amount is rendered as its reason — never as `0`.
 */
import {
  Badge,
  Button,
  EmptyState,
  Metric,
  Panel,
  PanelHeader,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  capabilityLabel,
} from "../../../ui";
import { fromMicros } from "../../../governance/TaxFeeLadderTabs";
import { numberText, record, records, text } from "../evidence";
import type { WorkbenchTabPayload } from "../workbenchTypes";
import type { OwnerReference } from "../../../shell/pages/ProjectSettings";

type Row = Record<string, unknown>;

/** An amount, in the currency the mart carried — or nothing at all.
 *
 *  `null` is an ABSENCE and never a zero: the mart makes a phase column NULL
 *  exactly so that a `0` can always be read as a measured zero. */
function amount(micros: unknown, currency: unknown): string | null {
  if (typeof micros !== "number") return null;
  const value = fromMicros(micros);
  if (value === null) return null;
  return typeof currency === "string" && currency ? `${value} ${currency}` : value;
}

function GapPhrase({ children }: { children: string }) {
  return <span className="text-text-secondary">{children}</span>;
}

function Measures({ measures, currency }: { measures: Row[]; currency: unknown }) {
  return (
    <Panel
      flush
      role="group"
      aria-label="Cost headline measures"
      className="grid grid-cols-4 divide-x divide-divider-base max-lg:grid-cols-2"
    >
      {measures.map((measure) => {
        const percent = text(measure.percent, "");
        const money = amount(measure.micros, currency);
        const value =
          measure.unit === "percent"
            ? percent && `${percent} %`
            : money;
        return (
          <Metric
            key={text(measure.key)}
            label={text(measure.label)}
            value={value || <GapPhrase>Not composed</GapPhrase>}
            hint={value ? undefined : text(measure.reason, "No reason was sent for this absence.")}
          />
        );
      })}
    </Panel>
  );
}

/** One phase, its amount, and the levels that laid it. */
function CascadeTable({ cascade, currency }: { cascade: Row; currency: unknown }) {
  const steps = records(cascade.steps);
  return (
    <Panel flush>
      <PanelHeader
        title="The cascade, phase by phase"
        description={text(cascade.aggregation_reason)}
      />
      <TableScroll label="Cost cascade phases">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Phase</TableHead>
              <TableHead>Amount</TableHead>
              <TableHead>Laid at</TableHead>
              <TableHead>What that level covers</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {steps.map((step) => {
              const money = amount(step.micros, currency);
              const levels = records(step.levels);
              const unresolved = typeof step.unresolved_rule_count === "number"
                ? step.unresolved_rule_count
                : 0;
              return (
                <TableRow key={text(step.key)}>
                  <TableCell>{text(step.label)}</TableCell>
                  <TableCell>
                    {money ?? <GapPhrase>{text(step.reason, "No amount")}</GapPhrase>}
                  </TableCell>
                  <TableCell>
                    {levels.length === 0 ? (
                      <GapPhrase>No rule reached this phase</GapPhrase>
                    ) : (
                      <div className="flex flex-wrap gap-2">
                        {levels.map((level) => (
                          <Badge key={text(level.kind)} tone="neutral">
                            {`${text(level.label)} · ${numberText(level.rule_count)} rule(s)`}
                          </Badge>
                        ))}
                      </div>
                    )}
                    {unresolved > 0 && (
                      <div className="text-ui text-text-secondary">
                        {`${numberText(unresolved)} ${text(step.unresolved_reason)}`}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    {levels.map((level) => text(level.covers)).join(" ") || "—"}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

/** The levels this store carries — and the two it does not, with their reason. */
function Levels({ levels }: { levels: Row }) {
  const rendered = records(levels.rendered);
  const absent = records(levels.absent);
  return (
    <Panel flush>
      <PanelHeader
        title="Where a rule can be laid"
        description="The levels the rule store carries, and the ones it does not."
      />
      <div className="grid gap-4 p-5 lg:grid-cols-2">
        <ul className="grid gap-2">
          {rendered.map((level) => (
            <li key={text(level.kind)} className="text-ui">
              <span className="font-medium">{text(level.label)}</span> — {text(level.covers)}
            </li>
          ))}
        </ul>
        <ul className="grid gap-2">
          {absent.map((level) => (
            <li key={text(level.name)} className="text-ui text-text-secondary">
              <span className="font-medium">{text(level.name)}</span> — {text(level.reason)}
            </li>
          ))}
        </ul>
      </div>
    </Panel>
  );
}

/** The rules examined and NOT applied, each with its code and its reason. */
function Refusals({ refused }: { refused: Row }) {
  const rules = records(refused.rules);
  return (
    <Panel flush>
      <PanelHeader
        title="Examined and not applied"
        description="A rule that does not apply here and a rule nobody considered need different repairs."
      />
      {rules.length === 0 ? (
        <div className="p-5">
          <EmptyState
            title="No refused rule"
            description={text(refused.reason, "Nothing was reported about refused rules.")}
          />
        </div>
      ) : (
        <TableScroll label="Refused rules">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Rule</TableHead>
                <TableHead>Code</TableHead>
                <TableHead>Why</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rules.map((rule) => (
                <TableRow key={text(rule.rule_key)}>
                  <TableCell className="font-mono">{text(rule.rule_key)}</TableCell>
                  <TableCell>{text(rule.code)}</TableCell>
                  <TableCell>{text(rule.reason)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      )}
    </Panel>
  );
}

export default function WorkbenchCostPage({
  payload,
  onOpenOwner,
}: {
  payload: WorkbenchTabPayload;
  onOpenOwner?: (owner: OwnerReference) => void;
}) {
  const evidence = payload.evidence as Row;
  const capability = record(evidence.capability) ?? {};
  const levels = record(evidence.levels) ?? {};
  const grain = record(evidence.grain);
  const window = record(evidence.window);
  const owner = record(evidence.governance_owner_reference);

  // THE CAPABILITY IS OFF AND THIS COMPONENT IS NOT MOUNTED. If it ever is —
  // a stale payload, a hand-built mount — it says the one true thing and draws
  // no cascade, rather than an empty one that reads as "nothing is added".
  if (capability.active !== true) {
    return (
      <Status as="block" tone="neutral" title={`${capabilityLabel("tax_fees")} is not active on this Project`}>
        {text(evidence.reason, "No cost cascade is computed for this Datastream.")}
      </Status>
    );
  }

  const governance = owner && onOpenOwner ? (
    <Button variant="secondary" size="sm" onClick={() => onOpenOwner(owner as unknown as OwnerReference)}>
      Open the governed rule set
    </Button>
  ) : null;

  if (evidence.state !== "available") {
    return (
      <div className="grid gap-4">
        <Panel flush>
          <PanelHeader title="Cost" description={text(window?.reason, "")} />
          <div className="p-5">
            <EmptyState
              title={text(evidence.reason, "No cost cascade on this window")}
              description={
                evidence.owner
                  ? `${text(evidence.owner)} is where a rule is published.`
                  : "Nothing was composed for this connector on this window."
              }
              action={governance ?? undefined}
            />
          </div>
        </Panel>
        <Levels levels={levels} />
      </div>
    );
  }

  const measures = records(evidence.measures);
  const cascade = record(evidence.cascade) ?? {};
  const currency = evidence.currency;

  return (
    <div className="grid gap-4">
      <Panel flush>
        <PanelHeader
          title="Cost"
          description={text(window?.reason, "")}
          actions={governance ?? undefined}
        />
        {/* THE SENTENCE IS WRITTEN ONCE. When the slice cannot be attributed to
            this Datastream the reason is a warning, not a footnote, and printing
            it in both places would read as two different findings. */}
        {grain?.ambiguous !== true && (
          <div className="p-5 text-ui text-text-secondary">{text(grain?.reason, "")}</div>
        )}
      </Panel>
      {grain?.ambiguous === true && (
        <Status as="block" tone="warning" title="This cascade is the connector's, not this Datastream's">
          {text(grain.reason)}
        </Status>
      )}
      <Measures measures={measures} currency={currency} />
      <CascadeTable cascade={cascade} currency={currency} />
      <Levels levels={levels} />
      <Refusals refused={record(evidence.refused_rules) ?? {}} />
    </div>
  );
}
