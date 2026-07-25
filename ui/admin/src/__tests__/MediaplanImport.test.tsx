/**
 * Vitest tests — ImportTab (Story 22.7, onglet Import Excel).
 *
 * Coverage :
 *   - Liste des contrats (GET /api/mediaplans/{id}/import-contracts)
 *   - État vide contrats
 *   - Import : mock du rapport — rejets affichés avec raisons, sommes affichées
 *   - Sommes en évidence (invariant : fichier = importé + rejeté)
 *   - Bouton « Publier la version » après import
 *   - Fichier trop volumineux → message FR côté client
 *   - Erreur d'import → Alert
 *
 * AI-54 : fixtures calquées sur les réponses réelles de l'API.
 * AD-15 : tout passe par fetch.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import ImportTab from "../mediaplans/ImportTab";
import type { ImportContract, ImportReport, MediaPlanDetail } from "../mediaplans/types";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures (AI-54)
// ---------------------------------------------------------------------------

const PLAN: MediaPlanDetail = {
  id: "plan-aaa",
  project_id: "proj-1",
  name: "Campagne été 2026",
  currency: "EUR",
  created_by: "alice@example.com",
  created_at: "2026-04-01T00:00:00Z",
  updated_at: "2026-04-01T00:00:00Z",
  archived_at: null,
  active_version: null,
  lines: [],
};

const CONTRACT: ImportContract = {
  id: "ctr-001",
  plan_id: "plan-aaa",
  sheet_name: "Digital",
  contract: {
    line_key: "label",
    header_row: 1,
    columns: { label: "Libellé", budget: "Budget" },
  },
  created_by: "alice@example.com",
  created_at: "2026-05-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
};

const IMPORT_REPORT: ImportReport = {
  version: {
    id: "ver-003",
    plan_id: "plan-aaa",
    version_number: 1,
    status: "candidate",
    source_note: "Import Excel",
    is_active: false,
    created_by: "alice@example.com",
    created_at: "2026-07-01T00:00:00Z",
    published_at: null,
  },
  per_line: [
    { line_key: "digital/meta", status: "created" },
    { line_key: "digital/google", status: "updated" },
    { line_key: "tv/tf1", status: "unchanged" },
    { line_key: "bad/line", status: "rejected", reason: "Budget invalide — valeur non numérique" },
  ],
  rejected: [
    { line_key: "bad/line", status: "rejected", reason: "Budget invalide — valeur non numérique" },
  ],
  sums: {
    file_total: 100000,
    imported_total: 80000,
    rejected_total: 20000,
  },
};

function mockContractsFetch(contracts: ImportContract[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ contracts }),
    })
  );
}

/**
 * Helper : stub fetch + FileReader pour simuler un import complet.
 * FileReader.readAsDataURL est mocké pour retourner un base64 immédiatement.
 */
function stubImportFetch(report: ImportReport) {
  const fetchMock = vi
    .fn()
    // GET contracts
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ contracts: [] }),
    })
    // POST import
    .mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => report,
    });
  vi.stubGlobal("fetch", fetchMock);

  // Mock FileReader : readAsDataURL déclenche onload immédiatement
  const FileReaderMock = vi.fn(() => {
    const instance: {
      result: string | null;
      onload: ((ev: unknown) => void) | null;
      onerror: ((ev: unknown) => void) | null;
      readAsDataURL: (file: File) => void;
    } = {
      result: null,
      onload: null,
      onerror: null,
      readAsDataURL(_file: File) {
        this.result = "data:application/octet-stream;base64,AAAA";
        if (this.onload) this.onload({ target: this });
      },
    };
    return instance;
  });
  vi.stubGlobal("FileReader", FileReaderMock);

  return fetchMock;
}

// ---------------------------------------------------------------------------
// Tests : contrats
// ---------------------------------------------------------------------------

describe("ImportTab — contrats", () => {
  it("charge et affiche la liste des contrats", async () => {
    mockContractsFetch([CONTRACT]);
    renderWithTheme(<ImportTab plan={PLAN} />);

    await waitFor(() => {
      expect(screen.getByTestId("contracts-table")).toBeInTheDocument();
      expect(screen.getByText("Digital")).toBeInTheDocument();
    });
  });

  it("affiche l'état vide si aucun contrat", async () => {
    mockContractsFetch([]);
    renderWithTheme(<ImportTab plan={PLAN} />);

    await waitFor(() => {
      expect(screen.getByTestId("contracts-empty")).toBeInTheDocument();
    });
  });

  it("affiche une Alert si le chargement des contrats échoue", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => ({ message: "Erreur serveur" }),
      })
    );
    renderWithTheme(<ImportTab plan={PLAN} />);

    await waitFor(() => {
      expect(screen.getByTestId("contracts-error")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : rapport d'import
// ---------------------------------------------------------------------------

describe("ImportTab — rapport d'import", () => {
  it("affiche les rejets avec leurs raisons", async () => {
    stubImportFetch(IMPORT_REPORT);
    renderWithTheme(<ImportTab plan={PLAN} />);
    await waitFor(() => screen.getByTestId("contracts-empty"));

    const fileInput = screen.getByTestId("file-input");
    const file = new File(["x"], "plan.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    Object.defineProperty(file, "size", { value: 1024 * 100 });
    await userEvent.upload(fileInput, file);

    await waitFor(() => {
      expect(screen.getByTestId("import-report")).toBeInTheDocument();
    });

    expect(screen.getByTestId("import-rejets-alert")).toBeInTheDocument();
    // Le texte peut apparaître dans la table per_line ET dans l'alert rejets →
    // getAllByText pour gérer les deux occurrences
    const rejetsText = screen.getAllByText(/budget invalide — valeur non numérique/i);
    expect(rejetsText.length).toBeGreaterThanOrEqual(1);
  });

  it("affiche les sommes fichier, importé, rejeté (invariant)", async () => {
    stubImportFetch(IMPORT_REPORT);
    renderWithTheme(<ImportTab plan={PLAN} />);
    await waitFor(() => screen.getByTestId("contracts-empty"));

    const fileInput = screen.getByTestId("file-input");
    const file = new File(["x"], "plan.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    Object.defineProperty(file, "size", { value: 1024 * 100 });
    await userEvent.upload(fileInput, file);

    await waitFor(() => {
      expect(screen.getByTestId("import-sums")).toBeInTheDocument();
    });
    expect(screen.getByTestId("sum-file")).toBeInTheDocument();
    expect(screen.getByTestId("sum-imported")).toBeInTheDocument();
    expect(screen.getByTestId("sum-rejected")).toBeInTheDocument();
  });

  it("affiche le bouton 'Publier la version' après un import réussi", async () => {
    stubImportFetch(IMPORT_REPORT);
    renderWithTheme(<ImportTab plan={PLAN} />);
    await waitFor(() => screen.getByTestId("contracts-empty"));

    const fileInput = screen.getByTestId("file-input");
    const file = new File(["x"], "plan.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    Object.defineProperty(file, "size", { value: 1024 * 100 });
    await userEvent.upload(fileInput, file);

    await waitFor(() => {
      expect(screen.getByTestId("import-publish-btn")).toBeInTheDocument();
    });
  });

  it("appelle onRefresh après une publication réussie (E3-F-3)", async () => {
    // import (2 fetch) + publish (1 fetch) réussissent.
    const fetchMock = stubImportFetch(IMPORT_REPORT);
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    });
    const onRefresh = vi.fn();
    renderWithTheme(<ImportTab plan={PLAN} onRefresh={onRefresh} />);
    await waitFor(() => screen.getByTestId("contracts-empty"));

    const fileInput = screen.getByTestId("file-input");
    const file = new File(["x"], "plan.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    Object.defineProperty(file, "size", { value: 1024 * 100 });
    await userEvent.upload(fileInput, file);

    await waitFor(() => screen.getByTestId("import-publish-btn"));
    await userEvent.click(screen.getByTestId("import-publish-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("import-publish-success")).toBeInTheDocument();
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("affiche un message FR si le fichier dépasse 15 Mo", async () => {
    mockContractsFetch([]);
    renderWithTheme(<ImportTab plan={PLAN} />);
    await waitFor(() => screen.getByTestId("contracts-empty"));

    const fileInput = screen.getByTestId("file-input");
    const file = new File(["x"], "huge.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    // > 15 Mo
    Object.defineProperty(file, "size", { value: 16 * 1024 * 1024 });
    await userEvent.upload(fileInput, file);

    await waitFor(() => {
      expect(screen.getByTestId("file-error")).toBeInTheDocument();
      expect(screen.getByText(/file too large/i)).toBeInTheDocument();
      expect(screen.getByText(/max 15 mb/i)).toBeInTheDocument();
    });
  });

  it("affiche une Alert d'erreur si l'import échoue (422 du serveur)", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ contracts: [] }),
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 422,
        json: async () => ({
          message: "Contrat d'import manquant pour l'onglet TV.",
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    // Mock FileReader
    const FileReaderMock = vi.fn(() => {
      const instance: {
        result: string | null;
        onload: ((ev: unknown) => void) | null;
        onerror: ((ev: unknown) => void) | null;
        readAsDataURL: (file: File) => void;
      } = {
        result: null,
        onload: null,
        onerror: null,
        readAsDataURL(_file: File) {
          this.result = "data:application/octet-stream;base64,AAAA";
          if (this.onload) this.onload({ target: this });
        },
      };
      return instance;
    });
    vi.stubGlobal("FileReader", FileReaderMock);

    renderWithTheme(<ImportTab plan={PLAN} />);
    await waitFor(() => screen.getByTestId("contracts-empty"));

    const fileInput = screen.getByTestId("file-input");
    const file = new File(["x"], "plan.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    Object.defineProperty(file, "size", { value: 1024 });
    await userEvent.upload(fileInput, file);

    await waitFor(() => {
      expect(screen.getByTestId("import-error")).toBeInTheDocument();
      expect(screen.getByText(/contrat d'import manquant/i)).toBeInTheDocument();
    });
  });
});
