/**
 * TopBar + ScopeControl — faithful port of the validated mockup topbar
 * (mockups scope-control), wired to the real scope + router. Help on the left
 * of an Organization > Project scope switch (quick menu) and a settings/access
 * actions menu. application.css classes; colors via CSS tokens.
 */
import { useEffect, useRef, useState } from "react";
import { useScope } from "./scope";
import { useRoute } from "./router";

export default function TopBar() {
  const { org, orgs, activeProject } = useScope();
  const { navigate } = useRoute();
  const [open, setOpen] = useState<null | "quick" | "actions">(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (open && ref.current && !ref.current.contains(e.target as Node)) setOpen(null);
    };
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(null);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  const goProject = (id: string) => {
    setOpen(null);
    navigate({ projectId: id, workspace: "overview", section: null });
  };

  return (
    <header className="topbar">
      <div className="topbar-left" />
      <div className="top-actions">
        <button className="quiet-button" type="button">Help</button>

        <div
          className={`scope-control${open === "quick" ? " quick-open" : ""}${open === "actions" ? " actions-open" : ""}`}
          ref={ref}
          data-scope-control
        >
          <button
            className="scope-switch"
            type="button"
            aria-haspopup="dialog"
            aria-expanded={open === "quick"}
            aria-label={`Switch workspace: ${org.name}, ${activeProject.name}`}
            onClick={() => setOpen(open === "quick" ? null : "quick")}
          >
            <span className="org-icon">{org.name.charAt(0)}</span>
            <span>{org.name}</span>
            <span className="hierarchy" aria-hidden="true">›</span>
            <span>{activeProject.name}</span>
          </button>
          <button
            className="scope-actions-trigger"
            type="button"
            aria-haspopup="menu"
            aria-expanded={open === "actions"}
            aria-label="Workspace settings and access"
            title="Workspace settings and access"
            onClick={() => setOpen(open === "actions" ? null : "actions")}
          >
            <svg className="scope-config-icon" viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z" />
            </svg>
          </button>

          {open === "quick" ? (
            <div className="scope-popover scope-quick-menu" role="dialog" aria-label="Switch workspace">
              <div className="scope-popover-heading">
                <strong>Switch workspace</strong>
                <span>Choose an organization, then a project.</span>
              </div>
              <div className="scope-option-section">
                <div className="scope-option-label">Organization</div>
                {orgs.map((o) => (
                  <button
                    key={o.id}
                    className={`scope-option${o.id === org.id ? " current" : ""}`}
                    type="button"
                    onClick={() => o.projects[0] && goProject(o.projects[0].id)}
                  >
                    <span>{o.name}</span>
                    {o.id === org.id ? <span className="scope-option-check" aria-hidden="true">✓</span> : null}
                  </button>
                ))}
              </div>
              <div className="scope-menu-divider" />
              <div className="scope-option-section">
                <div className="scope-option-label">{`Project in ${org.name}`}</div>
                {org.projects.map((p) => (
                  <button
                    key={p.id}
                    className={`scope-option${p.id === activeProject.id ? " current" : ""}`}
                    type="button"
                    onClick={() => goProject(p.id)}
                  >
                    <span>{p.name}</span>
                    {p.id === activeProject.id ? <span className="scope-option-check" aria-hidden="true">✓</span> : null}
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {open === "actions" ? (
            <div className="scope-popover scope-actions-menu" role="menu" aria-label="Workspace settings and access">
              <div className="scope-action-context">
                <span>{org.name}</span>
                <strong>{activeProject.name}</strong>
              </div>
              <div className="scope-action-label">Organization</div>
              <button className="scope-action-row" type="button" role="menuitem" onClick={() => { setOpen(null); navigate({ section: "org-settings" }); }}>Organization settings</button>
              <button className="scope-action-row" type="button" role="menuitem" onClick={() => { setOpen(null); navigate({ section: "org-settings" }); }}>Members and roles</button>
              <div className="scope-menu-divider" />
              <div className="scope-action-label">Project</div>
              <button className="scope-action-row" type="button" role="menuitem" onClick={() => { setOpen(null); navigate({ section: "project-settings" }); }}>Project settings</button>
              <button className="scope-action-row" type="button" role="menuitem" onClick={() => { setOpen(null); navigate({ section: "project-settings" }); }}>Project access</button>
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}
