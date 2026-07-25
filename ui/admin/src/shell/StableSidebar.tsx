/**
 * StableSidebar — faithful port of the validated mockup sidebar
 * (mockups/project-overview.html + data-workspace.html). Real logo image, the
 * exact nav icons and structure, application.css classes. Wired to the router.
 * No redesign — this mirrors the validated design; colors come from CSS tokens
 * (--rose/--ink/--line…) which OrgThemeProvider can override per organization.
 */
import type { ReactNode, KeyboardEvent } from "react";
import { WORKSPACES, type WorkspaceKey } from "./navigation";
import { useRoute } from "./router";
import DataTree from "./DataTree";

const ICONS: Record<WorkspaceKey, ReactNode> = {
  overview: (
    <svg viewBox="0 0 24 24">
      <rect x="3" y="3" width="7" height="7" />
      <rect x="14" y="3" width="7" height="7" />
      <rect x="3" y="14" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
    </svg>
  ),
  analyze: (
    <svg viewBox="0 0 24 24">
      <path d="M4 19V9m6 10V5m6 14v-7m4 7H2" />
    </svg>
  ),
  test: (
    <svg viewBox="0 0 24 24">
      <path d="M9 3h6m-5 0v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3M8 15h8" />
    </svg>
  ),
  data: (
    <svg viewBox="0 0 24 24">
      <ellipse cx="12" cy="5" rx="8" ry="3" />
      <path d="M4 5v7c0 2 4 3 8 3s8-1 8-3V5M4 12v7c0 2 4 3 8 3s8-1 8-3v-7" />
    </svg>
  ),
  governance: (
    <svg viewBox="0 0 24 24">
      <circle cx="6" cy="6" r="2" />
      <circle cx="18" cy="6" r="2" />
      <circle cx="12" cy="18" r="2" />
      <path d="M8 7l3 8m5-8l-3 8M8 6h8" />
    </svg>
  ),
  context: (
    <svg viewBox="0 0 24 24">
      <path d="M4 5a3 3 0 0 1 3-2h5v17H7a3 3 0 0 0-3 2V5zm16 0a3 3 0 0 0-3-2h-5v17h5a3 3 0 0 1 3 2V5z" />
    </svg>
  ),
};

export default function StableSidebar() {
  const { route, navigate } = useRoute();

  const activate = (ws: (typeof WORKSPACES)[number]) =>
    navigate({ workspace: ws.key, section: ws.subnav[0]?.slug ?? null });

  const onKey = (e: KeyboardEvent, fn: () => void) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fn();
    }
  };

  return (
    <aside className="sidebar">
      <div className="brand">
        <img className="brand-logo brand-logo--on-light" src="/brand/toorow-logo-horizontal-dark.png" alt="toorow" />
        <img className="brand-logo brand-logo--on-dark" src="/brand/toorow-logo-horizontal-light.png" alt="" aria-hidden="true" />
      </div>

      <nav className="nav" aria-label="Project">
        {WORKSPACES.map((ws) => {
          const active = route.workspace === ws.key;
          const hasSub = ws.subnav.length > 0;
          return (
            <div className="nav-group" key={ws.key}>
              <div
                className={`nav-item${active ? " active" : ""}`}
                role="button"
                tabIndex={0}
                aria-current={active ? "page" : undefined}
                data-testid={`ws-${ws.key}`}
                onClick={() => activate(ws)}
                onKeyDown={(e) => onKey(e, () => activate(ws))}
              >
                {ICONS[ws.key]}
                {ws.label}
                {hasSub ? <span className="chevron">{active ? "⌄" : "›"}</span> : null}
              </div>

              {active && hasSub && ws.key === "data" ? <DataTree /> : null}

              {active && hasSub && ws.key !== "data" ? (
                <div className="subnav">
                  {ws.subnav.map((s) => {
                    const on = route.section === s.slug;
                    return (
                      <div
                        key={s.slug}
                        className={`subnav-item${on ? " active" : ""}`}
                        role="button"
                        tabIndex={0}
                        aria-current={on ? "page" : undefined}
                        data-testid={`sec-${ws.key}-${s.slug}`}
                        onClick={() => navigate({ workspace: ws.key, section: s.slug })}
                        onKeyDown={(e) =>
                          onKey(e, () => navigate({ workspace: ws.key, section: s.slug }))
                        }
                      >
                        {s.label}
                      </div>
                    );
                  })}
                </div>
              ) : null}
            </div>
          );
        })}
      </nav>

      <div className="user">
        <div className="avatar">JB</div>
        <div>
          <strong>Jean B.</strong>
          <span>My account</span>
        </div>
        <div className="more">•••</div>
      </div>
    </aside>
  );
}
