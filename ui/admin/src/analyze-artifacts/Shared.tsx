/**
 * The pieces the three Story 50.3 surfaces share.
 *
 * They exist because Reports, Notebooks and Renders all have to say the same
 * two things honestly, and saying them three different ways is how a console
 * ends up with six wordings for one state:
 *
 *   1. "this downstream contract does not exist yet, and here is exactly which";
 *   2. "the server refused, and here is every reason it gave".
 *
 * Composed only from `ui/admin/src/ui/index.ts`. No stylesheet, no hex colour,
 * no literal spacing, no per-screen class prefix.
 *
 * `Loading`, `Failure` and `NoScope` USED TO LIVE HERE and are `ui/AsyncStates`
 * now: they carry no artifact type and no artifact word, so a collection outside
 * Analyze needing the same three sentences had to either import from this folder
 * or write a fourth spinner. `RefusalList` stayed, because it reads `Refusal`
 * from `./client` and promoting it would put an artifact-route shape in the
 * primitive library. The four screens import all four names from where each one
 * lives; nothing is re-exported from here, so there is one name for each.
 */
import { Stack, Status, stateTone, type StatusLegendEntry, type Tone } from "../ui";
import type { ContractState, Refusal } from "./client";

/**
 * The named unavailable state. It is not a placeholder and not a spinner: it
 * names the contract, the story that owns it, and the exact table that is
 * missing, so a reader can tell "not built yet" from "broken".
 *
 * IT NOW NAMES THE THIRD OBJECT TOO — the one that had no table at all and was
 * the only one this list never mentioned. Until 2026-08-31 the banner listed the
 * two registries Stories 50.4/50.5 own, both of which ship, so it rendered
 * nothing; meanwhile the Chart Template — ratified, owned by Analyze, with no
 * table anywhere — was offered in a picker three panels down. A missing contract
 * and an unpinnable kind are two facts and the server sends them separately, so
 * this component renders whichever of the two is true, and both when both are.
 */
export function ContractUnavailable({
  contract,
  what,
  presentationKinds = false,
}: {
  contract: ContractState;
  /** What the reader was trying to do, in their words. */
  what: string;
  /**
   * Whether this screen's gesture CHOOSES a presentation kind. Off by default,
   * and that is the point: a Render pins a Visualization Spec version and never
   * a template, so telling its screen that the Chart Template has no registry
   * would be an alert about somebody else's gesture. Only a screen that offers
   * the choice reports what cannot be chosen.
   */
  presentationKinds?: boolean;
}) {
  const unpinnable = presentationKinds ? contract.unpinnable_kinds ?? [] : [];
  if (contract.available && unpinnable.length === 0) return null;
  const title = contract.available
    ? `${what} cannot use every presentation kind in this deployment`
    : `${what} is not available in this deployment`;
  return (
    // `not_offered`, not a hard-coded `info`: this banner IS the reading of
    // `ContractState.available`, and the word for it lives in the vocabulary now
    // (`console-presentation.md` §3). The title says which deployment fact it is.
    <Status as="block" tone={stateTone("not_offered")} title={title}>
      <Stack>
        {contract.available ? null : (
          <p>
            The replay contract a preserved artifact needs does not exist yet. Nothing is
            fabricated in its place, and no partial artifact is written.
          </p>
        )}
        {contract.missing.length > 0 ? (
          <ul className="list-disc pl-5">
            {contract.missing.map((missing) => (
              <li key={missing.contract}>
                <strong>{humanize(missing.contract)}</strong> — owned by Story{" "}
                {missing.owner_story}; missing <code>{missing.missing_link}</code>
              </li>
            ))}
          </ul>
        ) : null}
        {unpinnable.length > 0 ? (
          <>
            <p>
              These presentation kinds have no registry behind them yet, so they cannot be
              pinned. Nothing is offered that the server would refuse.
            </p>
            <ul className="list-disc pl-5">
              {unpinnable.map((kind) => (
                <li key={kind.kind}>
                  <strong>{humanize(kind.contract)}</strong> — owned by Story{" "}
                  {kind.owner_story}; missing <code>{kind.missing_link}</code>
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </Stack>
    </Status>
  );
}

/** Every reason the server gave, attached to its subject. Never one banner. */
export function RefusalList({ refusals }: { refusals: Refusal[] }) {
  if (refusals.length === 0) return null;
  return (
    <ul className="list-disc pl-5">
      {refusals.map((refusal, index) => (
        <li key={`${refusal.code}-${refusal.subject ?? index}`}>
          {refusal.subject ? <strong>{refusal.subject}: </strong> : null}
          {refusal.message}
        </li>
      ))}
    </ul>
  );
}

/** `snake_case_identifier` to `Snake case identifier`, for a server-sent key. */
export function humanize(value: string): string {
  const spaced = value.replace(/[_-]+/g, " ").trim();
  return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : value;
}

/*
 * `stamp()` used to live here and rendered `2026-08-17 09:00:00 UTC`, a third
 * spelling of an instant the rest of the console wrote two other ways. It is
 * `ui/Timestamp` now — same guarantee that the zone is named, in the format
 * fourteen other call sites already use. These four screens import `Timestamp`
 * directly; nothing is re-exported from here, so there is one name for it.
 */

/**
 * The Result/Run outcomes, mapped to the one tone scale — WITH NO EXCEPTION LEFT.
 *
 * THE OVERRIDE THAT USED TO LIVE HERE WAS ABOUT A DIFFERENT FIELD. It read
 * `{ unavailable: "info" }`, and its comment justified that by "the server said
 * the replay contract does not exist in this deployment". But the `unavailable`
 * this function colours arrives on `last_run_outcome`, `run.status`,
 * `entry.outcome` and `block.status`, and `server/core/query_execution.py`
 * writes it for an unreadable identifier, an unreadable relation or a warehouse
 * the runner could not reach — « we could not ask » (its own comment, l.2122).
 * That is the silence the Data collections mean by the word, so `neutral` was
 * right all along and the dissent was an argument about the wrong column.
 *
 * The fact the override wanted is `ContractState.available`, and it now has its
 * own word — `not_offered`, tone `info` — which `ContractUnavailable` above
 * reads. `console-presentation.md` §3.
 */
export function outcomeTone(outcome: string | null | undefined): Tone {
  return stateTone(outcome);
}

/**
 * THE KEY TO A RUN OUTCOME, in the words these two surfaces use — §3 asks every
 * screen drawing three tones or more to mount one, and asks the SCREEN for the
 * meanings, because the same amber means something else on Governance.
 *
 * `Not offered here` is the entry 76-2 made possible: the deployment does not
 * carry the replay contract, which used to be said by overriding `unavailable`
 * to `info` — a word that actually reaches these outcomes for a warehouse the
 * runner could not reach. Two facts, two words, two marks.
 */
const OUTCOME_MEANING: Record<Tone, { label: string; meaning: string }> = {
  success: { label: "Succeeded", meaning: "the run produced its answer" },
  warning: { label: "Partial", meaning: "an answer, but not the whole one — degraded, empty, rejected, or a word this console does not know" },
  error: { label: "Failed", meaning: "refused or failed — there is no answer to read" },
  neutral: { label: "Unavailable", meaning: "the source could not be asked, so nothing was measured" },
  info: { label: "Not offered here", meaning: "this deployment does not carry the contract the artifact needs" },
};

/**
 * The key to the outcomes ON THIS PAGE, built from the outcomes on this page.
 *
 * It was a fixed list of five, and a Reports screen whose every run succeeded
 * explained what a failure looks like — a legend describing rows that are not
 * there. The caller passes the outcome words it is about to render; the tones
 * come from the union, so the key cannot disagree with the marks beside it.
 */
export function outcomeLegend(
  outcomes: readonly (string | null | undefined)[],
): StatusLegendEntry[] {
  const shown = new Set<Tone>();
  for (const outcome of outcomes) {
    if (outcome) shown.add(outcomeTone(outcome));
  }
  return [...shown].map((tone) => ({ tone, ...OUTCOME_MEANING[tone] }));
}
