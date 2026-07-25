/**
 * ModulesCatalog — Data > Modules.
 *
 * The project-level catalog of ACTIVATABLE ALIGNMENT MODULES (the doctrine
 * family from epics 37/39/41): each is opt-in, OFF by default, lives ONLY in the
 * read/semantic layer, never rewrites facts, is reversible, and EXPLAINS a
 * discrepancy rather than forcing a transformation. An operator activates only
 * what their use case needs.
 *
 * Reached at Data > Modules; Country split drills in to its ModuleSettings
 * surface (Data > Modules > Country split — the built member). The other family
 * members (Currency/FX, Report timezone, Media cost) are declared here so the
 * architecture is visible; their configuration surfaces land with their epics.
 * Report timezone is the alert-natured member — it detects and warns on the
 * cross-source day-offset (grain is DATE, never hourly) and never realigns.
 *
 * Styling: application.css shell classes + modules.css. English, tokens only.
 */
import "../application.css";
import "./modules.css";

export interface AlignmentModule {
  id: string;
  name: string;
  purpose: string;
  /** What discrepancy this module explains (read-only). */
  explains: string;
  /** True when a config surface exists to open today. */
  configurable: boolean;
  /** The timezone module manifests as a detect-and-warn alert, not a config. */
  alertNatured?: boolean;
}

const MODULES: AlignmentModule[] = [
  {
    id: "country-split",
    name: "Country split",
    purpose: "Local markets",
    explains:
      "Publishes selected countries as their own reporting groups and everything else as Other, across every compatible Datastream — no per-Datastream country assignment.",
    configurable: true,
  },
  {
    id: "currency",
    name: "Currency & FX",
    purpose: "Money normalization",
    explains:
      "Normalizes each source's native currency and converts to your reporting currency at read with as-of-day rates. Cross-currency sums are refused, never silently added.",
    configurable: false,
  },
  {
    id: "timezone",
    name: "Report timezone",
    purpose: "Day-boundary signal",
    explains:
      "Detects and warns when sources draw their day boundaries in different timezones. It signals the cross-source day-offset and points to a source lever if one exists — it never silently realigns.",
    configurable: false,
    alertNatured: true,
  },
  {
    id: "media-cost",
    name: "Media cost composition",
    purpose: "Fee ladder",
    explains:
      "Composes the fee ladder — platform cost, verification (IAS/DoubleVerify), and agency markup — from the media plan to reconcile net media to total net media / invoice.",
    configurable: false,
  },
];

export default function ModulesCatalog({
  projectId = "default",
  onOpenModule,
}: {
  projectId?: string;
  onOpenModule?: (moduleId: string) => void;
}) {
  void projectId; // module activation state is per-project (TODO(api))

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Modules</h1>
          <p>
            Optional, project-level alignment modules. Each is off by default,
            lives only in the read layer, is reversible, and explains a
            discrepancy — activate only what your use case needs.
          </p>
        </div>
      </div>

      <section className="modules-grid">
        {MODULES.map((m) => (
          <article className="panel module-card" key={m.id}>
            <div className="module-card-head">
              <div>
                <h2>{m.name}</h2>
                <span className="module-purpose">{m.purpose}</span>
              </div>
              {/* TODO(api): real per-project activation state */}
              {m.alertNatured ? (
                <span className="signal-label info">
                  <span className="signal-mark" />
                  Alert
                </span>
              ) : (
                <span className="signal-label muted">
                  <span className="signal-mark" />
                  Off
                </span>
              )}
            </div>
            <p className="module-explains">{m.explains}</p>
            <div className="module-card-foot">
              {m.configurable ? (
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => onOpenModule?.(m.id)}
                >
                  Configure →
                </button>
              ) : (
                <span className="module-availability">
                  Available with its use case
                </span>
              )}
            </div>
          </article>
        ))}
      </section>
    </>
  );
}
