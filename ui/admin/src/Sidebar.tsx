/**
 * Sidebar — left navigation shell for the admin console (Story 8.1, AC4).
 *
 * Information architecture per Epic 8 Part C:
 *   1. Vue d'ensemble   (control tower home)
 *   2. Data model       (data dictionary)
 *   3. Datastreams      (stream list + wizard)
 *   4. Rapports         (report packs)
 *   5. Qualité          (quality monitors)
 *   6. Autorisations    (connections / auth — renamed from Connexions)
 *   --- divider ---
 *   7. Notebooks
 *   8. Alertes
 *   9. Contexte
 *  10. Modules
 *
 * The workspace/project switcher lives at the top of the sidebar (Part C).
 * Design system alignment: outline icons, rose-3 selection, Toorow branding.
 *
 * Width: 236px fixed (permanent Drawer).
 * Origin design rules: no default-MUI colours, accent on selected item only,
 * near-black text, #EBEBF3 dividers, generous 8px-grid padding.
 *
 * UX-DR10: French-first copy.
 */
import {
  Box,
  Divider,
  Drawer,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Typography,
} from "@mui/material";
import ProjectSwitcher from "./ProjectSwitcher";

// ---------------------------------------------------------------------------
// Inline SVG icons (no @mui/icons-material — bundle weight concern)
// Each returns a minimal 20x20 SVG.
// ---------------------------------------------------------------------------

function IconDashboard() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="7" height="7" />
      <rect x="14" y="3" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
      <rect x="3" y="14" width="7" height="7" />
    </svg>
  );
}

function IconDataModel() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 4h12v8M16 5h4v5M4 14h16v4M4 20h14v1" />
    </svg>
  );
}

function IconDatastreams() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="4" y="4" width="8" height="8" rx="1" />
      <rect x="12" y="4" width="8" height="8" rx="1" />
      <path d="M12 12h8v8h-8z" />
      <path d="M4 12h8v8H4z" />
    </svg>
  );
}

function IconReports() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="4" y1="4" x2="20" y2="4" />
      <rect x="4" y="4" width="16" height="4" />
      <line x1="4" y1="11" x2="20" y2="11" />
      <rect x="4" y="11" width="16" height="2" />
      <line x1="4" y1="16" x2="20" y2="16" />
      <rect x="4" y="16" width="10" height="2" />
    </svg>
  );
}

function IconQuality() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function IconAuth() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
      <path d="M7 11V7a5 5 0 0 1 10 0v4" />
      <circle cx="12" cy="16" r="1" />
    </svg>
  );
}

function IconNotebooks() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    </svg>
  );
}

function IconAlertes() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M18 8a6 6 0 0 0-6-6H6" />
      <path d="M6 8v12" />
      <circle cx="6" cy="20" r="1" />
      <path d="M12 2v16" />
      <circle cx="12" cy="20" r="1" />
      <path d="M18 8v12" />
      <circle cx="18" cy="20" r="1" />
    </svg>
  );
}

function IconContexte() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
      <path d="M16 2v4" />
      <path d="M8 2v4" />
      <path d="M3 10h18" />
    </svg>
  );
}

function IconConnaissances() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 2a7 7 0 0 1 7 7c0 2.5-1.3 4.7-3.3 6l-.7.4V19h-6v-3.6l-.7-.4A7 7 0 0 1 12 2z" />
      <line x1="9" y1="22" x2="15" y2="22" />
    </svg>
  );
}

function IconModules() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="7" height="7" />
      <rect x="14" y="3" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
      <rect x="3" y="14" width="7" height="7" />
    </svg>
  );
}

function IconParametres() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M12 1v6m0 6v6M4.22 4.22l4.24 4.24m4.24 4.24l4.24 4.24M1 12h6m6 0h6M4.22 19.78l4.24-4.24m4.24-4.24l4.24-4.24" />
    </svg>
  );
}

function IconCartes() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
      <line x1="9" y1="9" x2="15" y2="9" />
      <line x1="9" y1="15" x2="15" y2="15" />
    </svg>
  );
}

function IconMediaplans() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="18" height="4" rx="1" />
      <rect x="3" y="10" width="18" height="4" rx="1" />
      <rect x="3" y="17" width="11" height="4" rx="1" />
      <path d="M18 18l2 2 3-3" />
    </svg>
  );
}

function IconRendus() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M3 9h18" />
      <path d="M9 21V9" />
      <circle cx="15" cy="15" r="2" />
    </svg>
  );
}

function IconOrganisations() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="5" r="2" />
      <circle cx="5" cy="19" r="2" />
      <circle cx="19" cy="19" r="2" />
      <path d="M12 7v4M12 11l-5 6M12 11l5 6" />
    </svg>
  );
}

function IconConflits() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

function IconDailyInsights() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <polyline points="12 7 12 12 15 15" />
      <path d="M9 17h6" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type NavSection =
  | "vue-ensemble"
  | "mise-en-route"
  | "data-model"
  | "creer-flux"
  | "datastreams"
  | "rapports"
  | "qualite"
  | "conflits"
  | "autorisations"
  | "cartes"
  | "rendus"
  | "daily-insights"
  | "organisations"
  | "mediaplans"
  | "notebooks"
  | "alertes"
  | "contexte"
  | "connaissances"
  | "modules"
  | "parametres";

interface SidebarProps {
  activeSection: NavSection;
  onSectionChange: (section: NavSection) => void;
  onProjectChange: (projectId: string) => void;
}

// ---------------------------------------------------------------------------
// Nav item definitions
// ---------------------------------------------------------------------------

interface NavItem {
  key: NavSection;
  label: string;
  icon: React.ReactNode;
}

const PRIMARY_NAV: NavItem[] = [
  { key: "vue-ensemble",  label: "Overview",             icon: <IconDashboard /> },
  { key: "mise-en-route", label: "Getting Started",     icon: <IconDashboard /> },
  { key: "data-model",    label: "Data Model",          icon: <IconDataModel /> },
  { key: "conflits",      label: "MDM Conflicts",       icon: <IconConflits /> },
  { key: "creer-flux",    label: "Create Stream",       icon: <IconDatastreams /> },
  { key: "datastreams",   label: "Data Streams",        icon: <IconDatastreams /> },
  { key: "rapports",      label: "Reports",             icon: <IconReports /> },
  { key: "cartes",        label: "Widget Cards",        icon: <IconCartes /> },
  { key: "rendus",        label: "Render Gallery",      icon: <IconRendus /> },
  { key: "daily-insights", label: "Daily Insights",     icon: <IconDailyInsights /> },
  { key: "qualite",       label: "Data Quality",        icon: <IconQuality /> },
  { key: "autorisations", label: "Authorizations",      icon: <IconAuth /> },
  { key: "organisations", label: "Organizations",      icon: <IconOrganisations /> },
  { key: "mediaplans",    label: "Media Plans",         icon: <IconMediaplans /> },
];

const SECONDARY_NAV: NavItem[] = [
  { key: "notebooks",      label: "Notebooks",          icon: <IconNotebooks /> },
  { key: "alertes",        label: "Alerts",             icon: <IconAlertes /> },
  { key: "contexte",       label: "Business Context",   icon: <IconContexte /> },
  { key: "connaissances",  label: "Knowledge Base",     icon: <IconConnaissances /> },
  { key: "modules",        label: "Connector Modules",  icon: <IconModules /> },
  { key: "parametres",     label: "Project Settings",   icon: <IconParametres /> },
];

export const SIDEBAR_WIDTH = 236;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function Sidebar({ activeSection, onSectionChange, onProjectChange }: SidebarProps) {
  return (
    <Drawer
      variant="permanent"
      sx={{
        width: SIDEBAR_WIDTH,
        flexShrink: 0,
        "& .MuiDrawer-paper": {
          width: SIDEBAR_WIDTH,
          boxSizing: "border-box",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          background: "var(--surface-card)",
          borderRight: "1px solid var(--border-subtle)",
        },
      }}
    >
      {/* ------------------------------------------------------------------ */}
      {/* Header with logo and workspace switcher                             */}
      {/* ------------------------------------------------------------------ */}
      <Box
        sx={{
          px: "20px",
          py: "18px",
          display: "flex",
          alignItems: "center",
          gap: 1,
          borderBottom: "1px solid var(--border-subtle)",
          flexShrink: 0,
        }}
      >
        <img
          src="../../assets/toorow_logo_horizontal_dark.png"
          alt="Toorow"
          style={{ height: 24, width: "auto", display: "block" }}
        />
      </Box>

      {/* Workspace switcher section */}
      <Box
        sx={{
          px: "12px",
          py: "14px",
          flexShrink: 0,
        }}
      >
        <ProjectSwitcher onProjectChange={onProjectChange} />
      </Box>

      {/* ------------------------------------------------------------------ */}
      {/* Primary navigation                                                  */}
      {/* ------------------------------------------------------------------ */}
      <Box sx={{ flex: 1, overflowY: "auto", pt: 1 }}>
        <List dense disablePadding sx={{ px: "12px" }}>
          {PRIMARY_NAV.map((item) => (
            <ListItemButton
              key={item.key}
              selected={activeSection === item.key}
              onClick={() => onSectionChange(item.key)}
              aria-label={item.label}
              data-testid={`nav-${item.key}`}
              sx={{
                display: "flex",
                alignItems: "center",
                gap: "10px",
                py: 1,
                px: "10px",
                borderRadius: "var(--radius-md)",
                border: "none",
                cursor: "pointer",
                background: activeSection === item.key ? "var(--surface-tint)" : "transparent",
                color: activeSection === item.key ? "var(--text-primary)" : "var(--text-secondary)",
                fontFamily: "var(--font-primary)",
                fontSize: "13.5px",
                fontWeight: activeSection === item.key ? 600 : 500,
                textAlign: "left",
                transition: "all 0.2s ease",
                "&:hover": {
                  background: activeSection === item.key ? "var(--surface-tint)" : "transparent",
                },
              }}
            >
              <ListItemIcon
                sx={{
                  minWidth: "auto",
                  color: "currentColor",
                }}
              >
                {item.icon}
              </ListItemIcon>
              <ListItemText
                primary={item.label}
                sx={{
                  "& .MuiTypography-root": {
                    fontFamily: "var(--font-primary)",
                    fontSize: "13.5px",
                    fontWeight: activeSection === item.key ? 600 : 500,
                  },
                }}
              />
            </ListItemButton>
          ))}
        </List>

        <Divider sx={{ my: 1, mx: "20px" }} />

        {/* Secondary nav section label */}
        <Box
          sx={{
            marginTop: 1,
            px: "20px",
            pb: 1,
            fontSize: "11px",
            fontWeight: 600,
            letterSpacing: ".04em",
            textTransform: "uppercase",
            color: "var(--text-tertiary)",
          }}
        >
          Paramètres
        </Box>

        <List dense disablePadding sx={{ px: "12px" }}>
          {SECONDARY_NAV.map((item) => (
            <ListItemButton
              key={item.key}
              selected={activeSection === item.key}
              onClick={() => onSectionChange(item.key)}
              aria-label={item.label}
              data-testid={`nav-${item.key}`}
              sx={{
                display: "flex",
                alignItems: "center",
                gap: "10px",
                py: 1,
                px: "10px",
                borderRadius: "var(--radius-md)",
                border: "none",
                cursor: "pointer",
                background: activeSection === item.key ? "var(--surface-tint)" : "transparent",
                color: activeSection === item.key ? "var(--text-primary)" : "var(--text-secondary)",
                fontFamily: "var(--font-primary)",
                fontSize: "13px",
                fontWeight: activeSection === item.key ? 600 : 500,
                textAlign: "left",
                transition: "all 0.2s ease",
                "&:hover": {
                  background: activeSection === item.key ? "var(--surface-tint)" : "transparent",
                },
              }}
            >
              <ListItemIcon
                sx={{
                  minWidth: "auto",
                  color: "currentColor",
                }}
              >
                {item.icon}
              </ListItemIcon>
              <ListItemText
                primary={item.label}
                sx={{
                  "& .MuiTypography-root": {
                    fontFamily: "var(--font-primary)",
                    fontSize: "13px",
                    fontWeight: activeSection === item.key ? 600 : 500,
                  },
                }}
              />
            </ListItemButton>
          ))}
        </List>
      </Box>

      {/* ------------------------------------------------------------------ */}
      {/* Footer user profile                                                 */}
      {/* ------------------------------------------------------------------ */}
      <Box
        sx={{
          px: 2,
          py: 2,
          borderTop: "1px solid var(--border-subtle)",
          display: "flex",
          alignItems: "center",
          gap: "10px",
          flexShrink: 0,
        }}
      >
        <Box
          sx={{
            width: 28,
            height: 28,
            borderRadius: "50%",
            background: "var(--toorow-lavande)",
            display: "grid",
            placeItems: "center",
            fontFamily: "var(--font-display)",
            fontSize: "12px",
            fontWeight: 600,
            color: "var(--neutral-700)",
            flexShrink: 0,
          }}
        >
          JB
        </Box>
        <Box sx={{ lineHeight: 1.2, minWidth: 0 }}>
          <Typography
            sx={{
              fontSize: "13px",
              fontWeight: 600,
              color: "var(--text-primary)",
            }}
          >
            Jean B.
          </Typography>
          <Typography
            sx={{
              fontSize: "11px",
              color: "var(--text-tertiary)",
            }}
          >
            Admin
          </Typography>
        </Box>
      </Box>
    </Drawer>
  );
}
