/**
 * The three things a screen says while it has no answer.
 *
 * They were written for the four Analyze artifact surfaces and are not about
 * artifacts at all: "I am reading", "I could not read it" and "you have not told
 * me which Project" are the three states every collection in the console passes
 * through, and each one is a different statement. Collapsing any two is how a
 * screen ends up showing a spinner forever after a 500, or an empty list to a
 * person who simply has no Project selected.
 *
 * WHY THEY MOVED HERE. `analyze-artifacts/Shared.tsx` is the right home for what
 * only Reports, Notebooks and Renders share — the replay-contract banner, the
 * refusal list, the artifact vocabulary. These three carry no artifact type and
 * no artifact word, so keeping them behind an `analyze-artifacts/` import meant
 * the next collection outside Analyze would write a fourth spinner rather than
 * import from a neighbour's folder.
 *
 * `RefusalList` STAYED THERE, deliberately. It reads `Refusal` from
 * `analyze-artifacts/client` — a `{code, subject, message}` the artifact routes
 * define — so promoting it would either drag that type into the primitive
 * library or restate it here under the same name. A shape with two definitions is
 * worse than a component in the wrong folder.
 *
 * NOTHING HERE IS A PLACEHOLDER. `Failure` names the cause it was given rather
 * than falling back to sample rows, and `NoScope` is distinct from an empty
 * state, which is a real answer to a real question.
 */
import type { ReactNode } from "react";
import { FileQuestionIcon, FolderOpenIcon } from "lucide-react";
import { Button } from "../components/ui/button";
import { EmptyState, Status } from "./Data";
import { Panel } from "./Surface";

/** A loading region that keeps its accessible name while it has nothing to say. */
export function Loading({ label }: { label: string }) {
  return (
    <Panel>
      <p role="status" className="text-ui text-text-secondary">
        Loading {label}…
      </p>
    </Panel>
  );
}

/**
 * A failure that names its cause instead of falling back to sample rows — and,
 * since 76-4, **never a dead end**.
 *
 * `action` IS REQUIRED, and it is required in the TYPE rather than checked at
 * runtime, because the defect this closes is a screen that stops: measured
 * 2026-09-05, all 19 call sites rendered a red block with a server sentence and
 * nothing to press. A person who cannot retry and cannot leave reloads the
 * browser, which loses their place. `console-presentation.md` §5 says an error
 * carries « one fallback action (retry, go back, open the object) »; a prop the
 * compiler will not let you omit is the only way that survives the next screen.
 *
 * `Retry` below is the word this console uses for the first of the three. It is
 * exported so nineteen screens do not spell one gesture nineteen ways — the
 * same reason `Status` exists rather than nineteen banners.
 *
 * `what` names the thing that could not be read. Without it the title reads
 * `This could not be loaded`, which is what every site said before; with it the
 * title is the reader's own noun. `shell/pages/RegressionRuns.tsx` had already
 * discovered both halves — a titled `what` AND a `Retry` — in a private
 * `FailureNote`, and that private copy is now this component.
 */
export function Failure({ message, what, action }: {
  message: string;
  /** `The offline evidence` → `The offline evidence could not be read`. */
  what?: string;
  /** Retry, go back, or open the object. Never absent. */
  action: ReactNode;
}) {
  return (
    <Status
      as="block"
      tone="error"
      title={what ? `${what} could not be read` : "This could not be loaded"}
      action={action}
    >
      {message}
    </Status>
  );
}

/**
 * The one wording for "ask again". A screen that reloads its own data says
 * `Retry`; a screen that cannot says something else and says why in a comment.
 */
export function Retry({ onClick }: { onClick: () => void }) {
  return (
    <Button variant="secondary" onClick={onClick}>
      Retry
    </Button>
  );
}

/**
 * No Project selected. Distinct from "empty", which is a real answer.
 *
 * It names a gesture (`Choose a project`) and mounts no control, and that is
 * deliberate rather than an oversight of §5: the project switcher is in the
 * shell's `TopBar`, on screen at this moment and on every other. Repeating it
 * inside the empty state would be a second switcher for one decision — the
 * exact defect §4 closed for labels. The gate's verb list therefore does not
 * carry `choose`; see `EmptyStatesDoNotInstructTheImpossible.test.tsx`.
 */
export function NoScope({ what }: { what: string }) {
  return (
    <EmptyState
      icon={<FolderOpenIcon />}
      title="No project selected"
      description={`Choose a project to see its ${what}.`}
    />
  );
}

/**
 * The Project in the address does not resolve — which is NOT `NoScope`.
 *
 * Four collections rendered `<EmptyState title="Project not found" />`: a title
 * and nothing else, which §5 refuses twice over (it says nothing about why, and
 * it offers nothing). The repair is not a button: the control that changes
 * Project is the switcher in the shell's `TopBar`, permanently on screen, so
 * this block's job is to SAY where it is. Naming a control that exists and is
 * visible is the guidance; a second copy of it here would not be.
 */
export function ProjectNotFound() {
  return (
    <EmptyState
      icon={<FolderOpenIcon />}
      title="Project not found"
      description="This project does not exist, or you cannot see it. Pick another one from the project switcher at the top of the screen."
    />
  );
}

/**
 * The address a person typed, or followed, that names nothing.
 *
 * SIX COPIES OF ONE BLOCK, measured 2026-09-05: the Report, Notebook, Render,
 * Dossier, Chart Template and Visualization Spec workbenches each rendered an
 * `EmptyState` with a title, a sentence and **no way out** — and four more sites
 * rendered `<EmptyState title="Project not found" />` with no sentence at all.
 * A workbench for an object that does not exist has no tabs, no rail and no
 * content; the only thing on the screen was a sentence, so the only gesture
 * left was the browser's back button, which the console cannot see.
 *
 * `collectionHref` is REQUIRED, and the workbenches that render this took it as
 * a required prop too, so the address is supplied by the router that knows it
 * rather than guessed here. The two answers this block deliberately does NOT
 * separate — the object does not exist, and you may not see it — are one answer
 * on purpose: `RegressionRuns` wrote the reason down first ("the two answer
 * identically on purpose"), and it is a disclosure rule, not a copy shortcut.
 */
export function ObjectNotFound({ what, collection, collectionHref, detail }: {
  /** The object's ratified noun — `Report`, `Render`, `Chart Template`. */
  what: string;
  /** The collection to go back to, in the reader's words — `Reports`. */
  collection: string;
  collectionHref: string;
  /** Replaces the default sentence where the absence has a sharper reason. */
  detail?: string;
}) {
  return (
    <EmptyState
      icon={<FileQuestionIcon />}
      title={`${what} not found`}
      description={detail ?? `This ${what} does not exist in this project, or you cannot see it. Nothing is shown in its place.`}
      action={
        <Button asChild variant="secondary">
          <a href={collectionHref}>Back to {collection}</a>
        </Button>
      }
    />
  );
}
