import { render, screen } from "@testing-library/react";
import OrgSettings from "../shell/pages/OrgSettings";

afterEach(() => vi.unstubAllGlobals());

describe("Organization Settings", () => {
  it("keeps membership at Organization scope and points Project grants elsewhere", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ members: [{ identity: "member@example.com", role: "member", status: "active" }] }) }));
    render(<OrgSettings orgId="org-1" section="members" />);
    expect(await screen.findByText("member@example.com")).toBeInTheDocument();
    expect(screen.getByText(/Project grants are intentionally managed in Project Access/i)).toBeInTheDocument();
  });

  /**
   * Story 75-4. The cascade is PLATFORM > ORG > PROJECT and the console carried
   * only the bottom of it: the Project tab existed, the ORG scope had no screen
   * at all, so the layer every project inherits from could be written by nobody.
   * The tab is the twin of `project-settings/ai` and it mounts the SAME panel,
   * addressed at the organization.
   */
  it("mounts the AI settings of the ORGANIZATION scope on its own tab", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        calls.push(url);
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({
            scope: "ORG",
            scope_id: "org-1",
            org_id: "org-1",
            resolved: {
              rules_always: [],
              rules_never: [],
              query_scope: "governed_views_only",
              fiscal_calendar: { year_start_month: 1, week_start_day: "monday" },
              narrative_language: "en",
              narrative_register: "plain",
            },
            sources: {
              rules_always: "PLATFORM",
              rules_never: "PLATFORM",
              query_scope: "PLATFORM",
              fiscal_calendar: "PLATFORM",
              narrative_language: "ORG",
              narrative_register: "PLATFORM",
            },
            own: null,
            inherited: null,
            platform_defaults: {},
            history: [],
          }),
          text: async () => "",
        } as Response);
      }),
    );

    render(<OrgSettings orgId="org-1" section="ai" />);

    // The ORG door, not the project one -- the panel is the same component and
    // the scope it edits is the whole point.
    expect(await screen.findByTestId("ai-setting-source-narrative_language")).toHaveTextContent(
      "Set here",
    );
    expect(calls).toContain("/api/organizations/org-1/ai-settings");
    expect(screen.getByTestId("ai-settings-save")).toHaveTextContent(
      "Save on this organization",
    );
  });
});
