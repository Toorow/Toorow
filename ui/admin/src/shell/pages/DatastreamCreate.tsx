/**
 * Routed Add Datastream focused task.
 *
 * The route deliberately owns no draft or source data. It mounts the one
 * canonical five-stage wizard and only bridges cancel/success navigation back to
 * the application router.
 */
import DatastreamSetupWizard from "../../datastreams/preconfiguration/DatastreamSetupWizard";
import { PageHeader } from "../../ui";
import type { ProposalOwnerReference } from "../../datastreams/wizard/wizardApi";

interface DatastreamCreateProps {
  projectId: string;
  onCancel: () => void;
  onSourceSetup?: (draftId: string) => void;
  onCreated?: (datastreamId: string) => void;
  /** Story 57.4 — a proposal names the OWNER of an object it will not create; this
   *  route only bridges that reference to the shell's resolver, exactly like the
   *  cancel and success bridges above it. */
  onOpenOwner?: (owner: ProposalOwnerReference) => void;
}

export default function DatastreamCreate({
  projectId,
  onCancel,
  onSourceSetup,
  onCreated,
  onOpenOwner,
}: DatastreamCreateProps) {
  const returnKey = `datastream-setup-return:${projectId}`;
  const resumeDraftId = sessionStorage.getItem(returnKey);
  const handoff = (draftId: string) => {
    sessionStorage.setItem(returnKey, draftId);
    onSourceSetup?.(draftId);
  };
  /** The draft was discarded (AI-336, ratified 2026-08-31). This route owns the
   *  return key, so this route forgets it: left behind, the next entry would
   *  resume a draft the server now refuses every write on, and the wizard would
   *  render a form whose first autosave fails. Leaving is then the ordinary
   *  cancel — there is nothing left to come back to. */
  const discarded = () => {
    sessionStorage.removeItem(returnKey);
    onCancel();
  };
  return (
    <section className="min-h-full bg-background-light" aria-labelledby="add-datastream-title">
      {/* The page title primitive, not a hand-written `<h1>` beside a `<p>`
          (57.5). The same defect as the wizard's own step header: a screen that
          titles itself has a second way of titling, and the two drift. */}
      <PageHeader
        className="mb-0 border-b border-divider-base px-6 py-5"
        id="add-datastream-title"
        title="Add Datastream"
        description={"Create a reviewable setup proposal. No Datastream is created or activated "
          + "in these steps."}
      />
      <DatastreamSetupWizard
        projectId={projectId}
        onCancel={onCancel}
        resumeDraftId={resumeDraftId}
        onSourceSetup={handoff}
        onDiscarded={discarded}
        onCreated={onCreated}
        onOpenOwner={onOpenOwner}
      />
    </section>
  );
}
