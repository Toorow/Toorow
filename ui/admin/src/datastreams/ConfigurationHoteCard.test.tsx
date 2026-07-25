/**
 * Vitest tests pour ConfigurationHoteCard (Story 36.14 / UX-DR31).
 *
 * Coverage :
 *   - Catalogue chargé : sélecteur d'hôtes compatibles rendu (fetch mock 200).
 *   - Classement piloté par capacité, PAS par marque (E36-NFR03) : l'hôte à
 *     interface applicative précède l'hôte outils-standard, indépendamment du nom.
 *   - Preflight app-UI : puce « Interface applicative », rôle requis affiché.
 *   - Preflight sans app-UI : chemin de repli outils standard + lien console
 *     « aide supplémentaire » (jamais uniquement un lien).
 *   - Handoff : consigne minimale (action + rôle + expiration + retour), sans
 *     donnée Toorow.
 *
 * Copie française accentuée ; WCAG 2.2 AA (régions alert/status, non-couleur).
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import ConfigurationHoteCard from "./ConfigurationHoteCard";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures -- deux hôtes qui ne diffèrent QUE par leur nom d'affichage et leur
// support d'UI. Le nom "zzz…" trie APRÈS "aaa…" alphabetiquement : si le
// classement etait alphabetique/marque, l'hote outils-standard passerait en
// premier. Le classement capacité doit placer l'app-UI en premier.
// ---------------------------------------------------------------------------
const CATALOG = {
  hosts: {
    host_standard_tools_v1: {
      host_key: "host_standard_tools_v1",
      display_name: "AAA Standard Host",
      dated_at: "2026-07-22",
      capabilities: ["mcp_tools", "tool_catalog_scan"],
      plan_constraints: { min_plan: "any" },
      required_role: "host_workspace_admin",
      app_ui_supported: false,
      catalog_refresh: { mode: "republish_on_version_change" },
      workspace_proof_required: false,
    },
    host_app_ui_v1: {
      host_key: "host_app_ui_v1",
      display_name: "ZZZ App UI Host",
      dated_at: "2026-07-22",
      capabilities: ["mcp_apps_ui", "tool_catalog_scan", "workspace_evidence"],
      plan_constraints: { min_plan: "team", custom_apps: true },
      required_role: "host_workspace_admin",
      app_ui_supported: true,
      catalog_refresh: { mode: "rescan_on_version_change" },
      workspace_proof_required: true,
    },
  },
};

const PREFLIGHT_APP_UI = {
  preflight_id: "hostpf_1",
  host_key: "host_app_ui_v1",
  state: "prepared",
  capabilities: ["mcp_apps_ui", "tool_catalog_scan"],
  plan_constraints: { min_plan: "team" },
  required_role: "host_workspace_admin",
  ui_support: {
    mode: "app_ui",
    bounded_evidence_required: true,
    console_deep_link: null,
    explanation: "L’hôte prend en charge l’interface applicative MCP.",
  },
  catalog_refresh: { mode: "rescan_on_version_change" },
  workspace_proof_required: true,
  dated_at: "2026-07-22",
  expires_at: "2026-07-25T00:00:00Z",
};

const PREFLIGHT_FALLBACK = {
  ...PREFLIGHT_APP_UI,
  preflight_id: "hostpf_2",
  host_key: "host_standard_tools_v1",
  workspace_proof_required: false,
  ui_support: {
    mode: "standard_tool_fallback",
    bounded_evidence_required: true,
    console_deep_link: { kind: "console", purpose: "aide_supplementaire", additional_only: true },
    explanation: "Bascule sur les outils standard avec preuve bornée obligatoire.",
  },
};

function mockCatalogOnly() {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true, status: 200, json: async () => CATALOG,
  }));
}

function mockCatalogThenPreflight(preflight: object) {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => CATALOG })
    .mockResolvedValueOnce({ ok: true, status: 201, json: async () => preflight });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("ConfigurationHoteCard — rendu et ordonnancement", () => {
  it("affiche le titre et le sélecteur d’hôtes compatibles", async () => {
    mockCatalogOnly();
    renderWithTheme(<ConfigurationHoteCard taskId="task-h" orgId="org-1" />);
    expect(await screen.findByText(/configuration de l’hôte mcp/i)).toBeInTheDocument();
    expect(await screen.findByTestId("host-selector")).toBeInTheDocument();
  });

  it("classe par capacité et non par marque (E36-NFR03)", async () => {
    mockCatalogOnly();
    renderWithTheme(<ConfigurationHoteCard taskId="task-h" orgId="org-1" />);
    // Ouvre le menu déroulant du sélecteur MUI.
    const selector = await screen.findByTestId("host-selector");
    await userEvent.click(within(selector).getByRole("combobox"));
    const options = await screen.findAllByRole("option");
    // L'hôte app-UI ("ZZZ…", trierait dernier par nom) doit venir EN PREMIER car
    // le classement est piloté par capacité, pas par la marque/le nom.
    expect(options[0]).toHaveAttribute("data-testid", "host-option-host_app_ui_v1");
    expect(options[1]).toHaveAttribute("data-testid", "host-option-host_standard_tools_v1");
  });
});

describe("ConfigurationHoteCard — preflight", () => {
  it("app-UI : affiche l’interface applicative et le rôle requis", async () => {
    mockCatalogThenPreflight(PREFLIGHT_APP_UI);
    renderWithTheme(<ConfigurationHoteCard taskId="task-h" orgId="org-1" />);
    const selector = await screen.findByTestId("host-selector");
    await userEvent.click(within(selector).getByRole("combobox"));
    await userEvent.click(await screen.findByTestId("host-option-host_app_ui_v1"));
    await userEvent.click(screen.getByTestId("host-preflight-run"));

    expect(await screen.findByTestId("host-ui-support")).toHaveTextContent(/interface applicative/i);
    expect(screen.getByTestId("host-required-role")).toHaveTextContent(/host_workspace_admin/i);
    expect(screen.getByTestId("host-proof-required")).toBeInTheDocument();
  });

  it("sans app-UI : chemin de repli outils standard + lien console additionnel", async () => {
    mockCatalogThenPreflight(PREFLIGHT_FALLBACK);
    renderWithTheme(<ConfigurationHoteCard taskId="task-h" orgId="org-1" />);
    const selector = await screen.findByTestId("host-selector");
    await userEvent.click(within(selector).getByRole("combobox"));
    await userEvent.click(await screen.findByTestId("host-option-host_standard_tools_v1"));
    await userEvent.click(screen.getByTestId("host-preflight-run"));

    const fallback = await screen.findByTestId("host-fallback-path");
    expect(fallback).toHaveTextContent(/outils standard/i);
    expect(fallback).toHaveTextContent(/preuve bornée/i);
    // Le lien console est une AIDE SUPPLÉMENTAIRE, présent en plus (jamais seul).
    expect(screen.getByTestId("host-console-deeplink")).toBeInTheDocument();
  });
});

describe("ConfigurationHoteCard — handoff", () => {
  it("génère une consigne minimale (action + rôle + expiration + retour)", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => CATALOG })
      .mockResolvedValueOnce({ ok: true, status: 201, json: async () => PREFLIGHT_APP_UI })
      .mockResolvedValueOnce({
        ok: true, status: 201, json: async () => ({
          preflight_id: "hostpf_1",
          state: "handed_off",
          admin_action: {
            action: "install_mcp_host",
            required_role: "host_workspace_admin",
            return_condition: { kind: "host_connected", resource_id: "task-h" },
          },
          handoff_id: "handoff_1",
          handoff_state: "created",
          expires_at: "2026-07-25T00:00:00Z",
          delivery_url: "https://console.toorow.test/handoff#handoff=abc",
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<ConfigurationHoteCard taskId="task-h" orgId="org-1" />);
    const selector = await screen.findByTestId("host-selector");
    await userEvent.click(within(selector).getByRole("combobox"));
    await userEvent.click(await screen.findByTestId("host-option-host_app_ui_v1"));
    await userEvent.click(screen.getByTestId("host-preflight-run"));
    await screen.findByTestId("host-preflight-result");
    await userEvent.click(screen.getByTestId("host-handoff-run"));

    const result = await screen.findByTestId("host-handoff-result");
    expect(within(result).getByTestId("host-handoff-state")).toHaveTextContent(/confiée/i);
    expect(result).toHaveTextContent(/installer l’application mcp/i);
    expect(result).toHaveTextContent(/host_workspace_admin/i);
    expect(result).toHaveTextContent(/expire le/i);
    expect(result).toHaveTextContent(/host_connected/i);
  });
});
