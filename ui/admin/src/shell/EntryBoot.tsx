/**
 * Passive application boot frame.
 *
 * Authentication and scope checks are technical waits, not user decisions. Keep
 * the final shell geometry and color tokens on screen without exposing stale
 * navigation or turning the wait into a modal-like onboarding step.
 */
export default function EntryBoot() {
  return (
    <div
      className="entry-boot-shell"
      role="status"
      aria-live="polite"
      aria-busy="true"
      aria-atomic="true"
      aria-label="Opening toorow"
    >
      <span className="sr-only">Opening toorow</span>
      <aside className="entry-boot-sidebar" aria-hidden="true">
        <div className="brand entry-boot-brand">
          <img
            className="brand-logo brand-logo--on-light"
            src="/brand/toorow-logo-horizontal-dark.png"
            alt=""
          />
          <img
            className="brand-logo brand-logo--on-dark"
            src="/brand/toorow-logo-horizontal-light.png"
            alt=""
          />
        </div>
      </aside>
      <section className="entry-boot-app" aria-hidden="true">
        <div className="entry-boot-topbar" />
        <div className="entry-boot-canvas" />
      </section>
    </div>
  );
}
