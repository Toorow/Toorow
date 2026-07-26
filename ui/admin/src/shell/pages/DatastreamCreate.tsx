/**
 * Routed Add Datastream focused task.
 *
 * The route deliberately owns no draft or source data. It mounts the one
 * canonical six-stage wizard and only bridges cancel/success navigation back to
 * the application router.
 */
import { CreerFluxWizard } from "../../datastreams/wizard";
import "../application.css";
import "./datastream-create.css";

interface DatastreamCreateProps {
  projectId: string;
  onCancel: () => void;
  onActivated: (datastreamId: string) => void;
}

export default function DatastreamCreate({
  projectId,
  onCancel,
  onActivated,
}: DatastreamCreateProps) {
  return (
    <div className="dialog-stage">
      <div className="dialog-stage-background" aria-hidden="true" />
      <div className="dialog-scrim">
        <section
          className="focused-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="add-datastream-title"
        >
          <header className="dialog-header">
            <h1 id="add-datastream-title">Add Datastream</h1>
          </header>
          <div className="dialog-body">
            <CreerFluxWizard
              projectId={projectId}
              onCancel={onCancel}
              onActivated={onActivated}
            />
          </div>
        </section>
      </div>
    </div>
  );
}
