/**
 * The three bounded recovery verbs, and what each one actually stands for.
 *
 * WHAT THIS PANEL USED TO BE, AND WHY IT COULD NOT STAY. It was a two-date
 * picker with a `Prepare reload` button. The server has refused every bounded
 * verb since the engine review: `bounded_recovery` mints an execution nothing
 * would advance, so `prepare` answers a refusal for `synchronize`, `reload` and
 * `reprocess` alike, every single time. A person chose two days, read a span,
 * pressed a control and learned that the control cannot work. An absent verb is
 * better than that; a NAMED, EXPLAINED absence is better than both.
 *
 * AND THE THREE VERBS WERE NOT THE SAME PROMISE, WHICH IS THE WHOLE POINT.
 * `Synchronize` and `Reload` would call the provider and spend on the source
 * account. `Reprocess` reapplies a mapping to data the product already keeps: no
 * provider call, no spend, by a wide margin the cheapest of the three. Refusing
 * all three with one sentence told a person nothing they could act on.
 *
 * THE ARBITRATION, RATIFIED IN `datastream-workbench-and-wizard.md` ("The three
 * verbs are three different promises, and the screen says which"): two of them
 * are DELIVERED under other names and are retired as recovery verbs, and the
 * third is the one real gap. Day-by-day coverage — the panel directly above this
 * one on this tab, and the day grid on `Data` — already collects days that were
 * never collected and re-collects days that came back wrong, bounded, with a
 * confirmation naming the connector, the source account and every day before
 * anything is spent. A second door onto that collection is not a feature.
 * `Reprocess` alone has nothing standing in for it, so it stays on screen as a
 * named refusal: a person cannot ask for what they cannot see.
 *
 * NOTHING HERE IS PRESSABLE, AND THAT IS THE DESIGN. Every verb this panel
 * describes is one the product cannot grant. A control offered for a verb that
 * refuses is the defect being repaired, not a softer version of it.
 *
 * THE LIST IS READ, NOT TYPED. `unbuiltRecoveryVerbs()` filters the registry
 * mirror on `has_engine`, which is the server's measurement of its own build. A
 * verb whose engine lands leaves this panel by itself, on the same commit that
 * flips the registry — there is no second list to remember.
 *
 * AND IT IS FOLDED — amended 2026-08-18. Every sentence above survives; what
 * changed is where it stands. This panel sat at the TOP of the tab a person
 * opens during an incident, above the run list, and everything it says is that
 * three things cannot be asked for. An honest absence still costs the height it
 * takes: measured at 1600px it pushed the first run row below the fold, so the
 * screen answering "which run failed" opened on three refusals. Behind a
 * disclosure it is still there, still readable, still first if a person wants
 * it — and the history leads.
 */
import { Badge, Collapsible, CollapsibleContent, CollapsibleTrigger, Panel, Status } from "../../ui";
import { unbuiltRecoveryVerbs, verbCost, type RecoveryVerb } from "./boundedRecovery";

/**
 * Retired reads calmly; a gap the product means to close reads as an open item.
 *
 * `outline` is deliberately not used on the second: it renders any tone as the
 * same quiet grey, and the whole judgement of this panel is that one of these
 * three is not like the other two.
 */
function standingBadge(verb: RecoveryVerb) {
  return verb.standing === "covered" ? (
    <Badge tone="neutral" outline>Retired</Badge>
  ) : (
    <Badge tone="warning">Not built yet</Badge>
  );
}

/**
 * One verb, said once. Exported because the per-run recovery dialog says the
 * same thing when the server has refused every verb for that run, and two
 * spellings of one refusal is the defect this repository keeps finding.
 */
export function VerbStanding({ verb }: { verb: RecoveryVerb }) {
  return (
    <div className="grid gap-1" data-testid={`recovery-verb-${verb.origin.key}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-ui font-medium text-text-primary">{verb.origin.label}</span>
        {standingBadge(verb)}
      </div>
      <p className="m-0 text-caption text-text-secondary">{verb.does}</p>
      {/* Spend is stated on EVERY verb, never as a badge only the expensive ones
          carry: "would call nothing and spend nothing" is the fact that makes
          Reprocess worth waiting for rather than worth working around. */}
      <p className="m-0 text-caption text-text-secondary" data-testid={`recovery-cost-${verb.origin.key}`}>
        {verbCost(verb.origin)}
      </p>
      <p className="m-0 text-caption text-text-secondary">{verb.instead}</p>
    </div>
  );
}

export default function DatastreamReloadPanel(_props: {
  // The mount passes the Datastream it is looking at and a refresh callback.
  // This panel reads nothing and changes nothing, so it uses neither — it states
  // what this BUILD can grant, which is the same answer on every Datastream.
  // Dropping the three props is an edit to the tab that mounts it, and belongs
  // to whoever owns that file.
  projectId?: string;
  datastreamId?: string;
  onConfirmed?: () => void;
}) {
  const verbs = unbuiltRecoveryVerbs();
  if (verbs.length === 0) return null;

  return (
    // The handle keeps its old name: the tab that mounts this panel asserts its
    // position by it, and that file belongs to another lot.
    <Panel data-testid="recovery-verbs">
      <Collapsible>
        {/* THE TRIGGER SAYS WHAT IS BEHIND IT, AND HOW MUCH. A disclosure whose
            label does not name its content is a second click charged for finding
            out whether the first one was worth it — and the count comes from the
            registry, so a verb whose engine lands changes this number without
            anybody editing it. */}
        <CollapsibleTrigger
          className="flex w-full items-center justify-between gap-3 rounded-large border border-divider-base px-4 py-2 text-left"
          data-testid="recovery-verbs-trigger"
        >
          <span className="grid gap-1">
            <span className="text-ui font-medium text-text">
              {`${verbs.length} recovery verb(s) this build cannot grant`}
            </span>
            <span className="text-caption text-text-secondary">
              None of them is a control. Day-by-day coverage above performs the retired ones.
            </span>
          </span>
          <span aria-hidden className="text-caption text-text-secondary">Read</span>
        </CollapsibleTrigger>

        <CollapsibleContent data-testid="recovery-verbs-detail">
          <div className="mt-4 grid gap-4">
            {/* THE SENTENCE IS INTACT. Folding the panel does not soften what it
                says: asking for one of these would mint a treatment nothing
                finishes, and that treatment holds this Datastream's publication
                lock. It is the reason the verbs are named at all. */}
            <Status as="block" tone="info" title="Nothing here can be asked for">
              None of these is a control. Asking for one would create a treatment nothing
              would finish, and that treatment would block every later publication and
              every following night's collection on this Datastream.
            </Status>

            <div className="grid gap-4">
              {verbs.map((verb) => (
                <VerbStanding key={verb.origin.key} verb={verb} />
              ))}
            </div>
          </div>
        </CollapsibleContent>
      </Collapsible>
    </Panel>
  );
}
