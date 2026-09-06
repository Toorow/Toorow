/**
 * The surfaces that render BEFORE a project scope exists.
 *
 * AD-42, 2026-08-12, moved out of `App.tsx` with no edit to a body. A wait, a
 * failure and a refusal are screens; the file that composes the shell is not
 * where a screen is written.
 *
 * What each one owes, and why none of them may be replaced by a blank page:
 * `ScopeLoading` is a technical wait and says so; `ScopeError` names the failure
 * and offers a retry rather than falling through to a shell as if all were well
 * (F-010); `EntryBlocked` says what is missing and who can grant it.
 *
 * Styling: migrated off `create-org.css` (waves 2-4 of `docs/ui-css-strategy.md`).
 * The focused-dialog recipe below is CreateOrg's, copied verbatim so the entry
 * surfaces stay one family — change them together. Colors come exclusively from
 * theme tokens — no hex.
 */

import { useState, type ReactNode } from "react";
import { Menu } from "lucide-react";

import EntryBoot from "./EntryBoot";
import { OrgThemeProvider } from "./orgTheme";
import StableSidebar, { SidebarBody } from "./StableSidebar";
import { lastProjectScope } from "./lastProjectScope";
import { Button, Sheet, SheetContent, SheetTitle, Status } from "../ui";
import "./application.css";

/* ---- The focused-dialog surface — CreateOrg's recipe, verbatim. ----------
   Ported 1:1 from the retired `create-org.css`; geometry (not prose) keeps its
   px values, prose measures stay in `ch`. The scrim dims with the scheme-stable
   `surface-dark` token so it reads as dimming on both light and dark. */
const STAGE = "relative min-h-[828px]";
const SCRIM =
  "absolute inset-0 z-[var(--layer-modal)] grid items-start justify-items-center gap-4.5 bg-surface-dark/20 p-7";
const DIALOG =
  "w-[min(640px,calc(100%-56px))] overflow-hidden rounded-[18px] border border-divider-base bg-surface-light shadow-overlay max-[1180px]:w-[calc(100%-36px)]";
const HEADER = "flex items-start justify-between gap-4.5 border-b border-divider-base px-7 pb-5 pt-6";
const TITLE = "m-0 font-display text-[22px] font-semibold tracking-[-0.01em]";
const SUBTITLE = "m-0 mt-2 max-w-[44ch] text-label leading-normal text-text-secondary";
const BODY = "grid gap-5.5 px-7 py-6";
const FOOTER =
  "flex items-center justify-between gap-4.5 border-t border-divider-base px-7 py-4.5 max-[1180px]:flex-wrap";
const FOOTER_NOTE = "text-caption leading-snug text-text-secondary";
const ACTIONS = "flex flex-none items-center gap-2.5";

/**
 * THE ENTRY SURFACES HAD NO MENU EITHER — the same defect A.7.1 was written
 * against, reached by a third route.
 *
 * `page-structure.md §A.7.1` settled it on 2026-08-04 (*"en plus y a pas le
 * menu"*): **the rail stays on every surface**, because leaving a page is not
 * the same act as being in it. It enumerated the five scope surfaces that
 * existed then; the RULE it states is universal, and `/create-org`,
 * `/onboarding`, `/invite` and `/setup` were rendering outside the shell with
 * neither rail nor scope dialog — a person who opened "add another
 * organization" while holding a Project had no way back but the address bar.
 *
 * The rule A.7.1 already gives for a surface carrying no Project in its address
 * is the one applied here: the rail follows the Project the person was last in
 * ([[lastProjectScope]]) and renders only when there is one — never a fallback
 * to "the first Project". On an entry surface `useScope()` is deliberately
 * unread (an invitation must not wait on the org/project fetch), so the top
 * bar's switcher cannot be the second answer here and the remembered Project is
 * the only honest one.
 *
 * Below `lg` the rail is `hidden … lg:flex`, so the same rows are offered in a
 * drawer — `SidebarBody`, the SAME component (`§G.2.3`), never a second
 * navigation to keep in step with the registry.
 */
function EntryRail({ children }: { children: ReactNode }) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Read on render, like the shell does: `sessionStorage` is synchronous and a
  // rail that appeared a beat after the page reads as a glitch.
  const scope = lastProjectScope();
  // Nothing remembered: this browser has never been inside a Project, so there
  // is no Project to name and no list to offer. The surface's own form is then
  // the only gesture there is — see the 2026-08-31 amendment in §A.7.1.
  if (!scope) return <>{children}</>;
  return (
    <div className="grid min-h-screen min-w-0 grid-cols-1 bg-background text-foreground lg:grid-cols-[16rem_minmax(0,1fr)]">
      <StableSidebar scope={scope} />
      <section className="grid min-w-0 grid-rows-[auto_minmax(0,1fr)]">
        <div className="flex items-center border-b border-divider-base px-4 py-3 dark:border-divider-medium lg:hidden">
          <Button
            type="button"
            variant="secondary"
            size="icon"
            aria-label="Open navigation"
            aria-expanded={drawerOpen}
            title="Open navigation"
            onClick={() => setDrawerOpen(true)}
          >
            <Menu className="size-4" />
          </Button>
        </div>
        <div className="min-w-0">{children}</div>
      </section>
      <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
        {/* The rail's own width, not the Sheet's 384px: this IS the rail. */}
        <SheetContent side="left" className="w-64" aria-describedby={undefined}>
          <SheetTitle className="sr-only">Project navigation</SheetTitle>
          <SidebarBody scope={scope} onNavigate={() => setDrawerOpen(false)} />
        </SheetContent>
      </Sheet>
    </div>
  );
}

/**
 * Pre-shell frame: the neutral (unbranded) theme plus the CSS baseline, used by
 * every surface that renders before a project scope exists.
 */
export function EntryFrame({ children }: { children: ReactNode }) {
  return (
    <OrgThemeProvider branding={null}>
      {children}
    </OrgThemeProvider>
  );
}

/**
 * The frame for an ADDRESS a person deliberately opened: `/setup`, `/invite`,
 * `/create-org`, `/onboarding`. Same neutral theme, plus the rail.
 *
 * Why the four routes and not `EntryFrame` itself: the other users of that frame
 * are STATES of the root address, not addresses — the technical wait most of
 * all. A rail on `ScopeLoading` would offer six workspaces to click while the
 * console is still asking which Projects exist, i.e. a menu that navigates on
 * behalf of a scope nobody has resolved yet. `ScopeLoading` is a wait and says
 * so; `/create-org` is a place, and a place you cannot leave is the defect
 * A.7.1 named.
 */
export function EntryShell({ children }: { children: ReactNode }) {
  return (
    <EntryFrame>
      <EntryRail>{children}</EntryRail>
    </EntryFrame>
  );
}

/** A technical wait, not an onboarding or scope-selection step. */
export function ScopeLoading() {
  return <EntryBoot />;
}

/** state === "error": name the failure and offer a retry. Never a fabricated shell. */
export function ScopeError() {
  return (
    <div className={STAGE}>
      <div className={SCRIM}>
        <section
          className={DIALOG}
          role="alert"
          aria-labelledby="entry-error-title"
        >
          <header className={HEADER}>
            <div>
              <h1 id="entry-error-title" className={TITLE}>
                We could not load your organizations and projects
              </h1>
              <p className={SUBTITLE}>
                Your organizations and projects did not load, so the application cannot open yet.
              </p>
            </div>
          </header>
          <div className={BODY}>
            {/* The SECTION is the alert (unchanged ARIA). The `Alert` primitive
                would nest a second role="alert" inside it, so the signal keeps
                the inline `Status` form beside its explanation. */}
            <div className="grid gap-3.5">
              <Status tone="error">Organizations and projects unavailable</Status>
              <p className="m-0 text-label leading-normal [overflow-wrap:anywhere]">
                We will not show project data we could not verify. Nothing was changed. Check your
                connection and try again; if this keeps happening, sign out and sign back in,
                or ask an organization owner whether your access is still active.
              </p>
            </div>
          </div>
          <footer className={FOOTER}>
            <span className={FOOTER_NOTE}>
              Retrying reloads the console and asks for your access again.
            </span>
            <div className={ACTIONS}>
              <Button type="button" onClick={() => window.location.reload()}>
                Try again
              </Button>
            </div>
          </footer>
        </section>
      </div>
    </div>
  );
}

// `identity_activation_required` was removed on 2026-08-24 (67-17). The server
// can no longer answer it: it existed only while an environment flag could
// disarm canonical identity, and its screen asked a person to go and change a
// deployment variable -- which is the one thing an empty/blocked state must
// never do.
export type EntryState = "loading" | "hosted_entry_ready" | "local_entry_ready" | "invitation_required" | "setup_required" | "scoped" | "error";

export function EntryBlocked({
  kind,
}: {
  kind: "invitation_required" | "setup_required" | "scoped" | "organization_limit_reached";
}) {
  const copy =
    kind === "invitation_required"
      ? {
          title: "An invitation is required",
          detail: "Your identity has no accepted platform or organization invitation. Ask an administrator for a new invitation link.",
        }
      : kind === "setup_required"
        ? {
            title: "This instance is not claimed",
            detail: "An operator must mint and open the one-time /setup link from a trusted server shell.",
          }
        : kind === "organization_limit_reached"
          ? {
              title: "Organization creation is not available",
              detail:
                "You already have an active scope. Access to another organization comes from an invitation or an authorized administrator.",
            }
        : {
            title: "Your project access is incomplete",
            detail: "Your account has organization access but no usable project. Ask an owner to repair your project access.",
          };
  return (
    <div className={STAGE}>
      <div className={SCRIM}>
        <section className={DIALOG} role="alert">
          <header className={HEADER}>
            <div>
              <h1 className={TITLE}>{copy.title}</h1>
              <p className={SUBTITLE}>{copy.detail}</p>
            </div>
          </header>
        </section>
      </div>
    </div>
  );
}
