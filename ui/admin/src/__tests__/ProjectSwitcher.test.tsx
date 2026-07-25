/**
 * Vitest tests for ProjectSwitcher (Story 7.1, AC6, AC8).
 *
 * Coverage (AC8 minimum, 4+):
 *   - renders the dropdown populated from GET /api/projects
 *   - selecting a project stores it in localStorage and fires onProjectChange
 *   - default selection = first project when localStorage empty
 *   - stored id honoured when still active
 *   - "Nouveau projet" dialog calls POST /api/projects and activates the new one
 *
 * UX-DR10: French copy assertions.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import ProjectSwitcher, {
  ACTIVE_PROJECT_STORAGE_KEY,
  type Project,
} from "../ProjectSwitcher";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

const PROJECTS: Project[] = [
  { id: "proj_a", name: "Acme Corp", slug: "acme-corp", status: "active" },
  { id: "proj_b", name: "Beta Client", slug: "beta-client", status: "active" },
];

function mockList(projects: Project[]) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ projects }),
  });
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("ProjectSwitcher", () => {
  it("renders the dropdown populated from GET /api/projects", async () => {
    vi.stubGlobal("fetch", mockList(PROJECTS));
    renderWithTheme(<ProjectSwitcher />);
    // First project becomes active and its name is shown in the select.
    await waitFor(() => {
      expect(screen.getByText("Acme Corp")).toBeInTheDocument();
    });
  });

  it("defaults to the first project and fires onProjectChange", async () => {
    vi.stubGlobal("fetch", mockList(PROJECTS));
    const onChange = vi.fn();
    renderWithTheme(<ProjectSwitcher onProjectChange={onChange} />);
    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith("proj_a");
    });
    expect(localStorage.getItem(ACTIVE_PROJECT_STORAGE_KEY)).toBe("proj_a");
  });

  it("honours a stored active project id when still active", async () => {
    localStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, "proj_b");
    vi.stubGlobal("fetch", mockList(PROJECTS));
    const onChange = vi.fn();
    renderWithTheme(<ProjectSwitcher onProjectChange={onChange} />);
    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith("proj_b");
    });
  });

  it("switching project stores the new id in localStorage", async () => {
    vi.stubGlobal("fetch", mockList(PROJECTS));
    const onChange = vi.fn();
    renderWithTheme(<ProjectSwitcher onProjectChange={onChange} />);
    await waitFor(() => expect(screen.getByText("Acme Corp")).toBeInTheDocument());

    // Open the MUI select and pick the second project.
    const select = screen.getByLabelText("Sélecteur de projet");
    await userEvent.click(select);
    const listbox = await screen.findByRole("listbox");
    await userEvent.click(within(listbox).getByText("Beta Client"));

    await waitFor(() => {
      expect(localStorage.getItem(ACTIVE_PROJECT_STORAGE_KEY)).toBe("proj_b");
    });
    expect(onChange).toHaveBeenCalledWith("proj_b");
  });

  it("'Nouveau projet' dialog calls POST /api/projects and activates the new project", async () => {
    const fetchMock = vi.fn((_url: string, opts?: RequestInit) => {
      if (opts?.method === "POST") {
        return Promise.resolve({
          ok: true,
          status: 201,
          json: async () => ({
            id: "proj_new",
            name: "New Client",
            slug: "new-client",
            status: "active",
          }),
        });
      }
      // GET list — after creation includes the new project.
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          projects: [...PROJECTS, { id: "proj_new", name: "New Client", slug: "new-client" }],
        }),
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const onChange = vi.fn();
    renderWithTheme(<ProjectSwitcher onProjectChange={onChange} />);

    await userEvent.click(screen.getByTestId("new-project-button"));
    const nameInput = await screen.findByLabelText("Nom du projet");
    await userEvent.type(nameInput, "New Client");
    await userEvent.click(screen.getByRole("button", { name: "Créer" }));

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        (c: unknown[]) => (c[1] as RequestInit | undefined)?.method === "POST",
      );
      expect(postCall).toBeTruthy();
      expect(postCall?.[0]).toBe("/api/projects");
    });
    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith("proj_new");
    });
  });
});
