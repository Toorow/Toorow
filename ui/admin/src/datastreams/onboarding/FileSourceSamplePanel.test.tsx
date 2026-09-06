/**
 * Story 22.19 — le panneau qui donne enfin une entrée à la chaîne source-fichier.
 *
 * Ce que ces tests épinglent, et qui manquait à tout l'epic 22 : que la ROUTE
 * soit appelée. Mesuré le 2026-08-01, aucun écran de la console n'appelait
 * `/imports/preview` ni `/imports` — le template, le recognizer, le placement et
 * le gate n'étaient atteignables que depuis un test.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import FileSourceSamplePanel from "./FileSourceSamplePanel";

const PREVIEW_OK = {
  columns: [{ name: "Date" }, { name: "Clics" }],
  file_source: {
    fields: [
      { source_column: "Date", canonical_target: "day", confidence: 0.93, status: "matched" },
      { source_column: "Clics", canonical_target: "clicks", confidence: 0.88, status: "matched" },
    ],
    placement: { class: "actual", metric: "clicks", period: "day", dimension: [] },
    gate: { passed: true, missing_required: [], flagged: [] },
    ambiguities: [],
    blocked: false,
    reason: null,
  },
};

function mockFetch(payload: unknown, ok = true) {
  const spy = vi.fn().mockResolvedValue({
    ok,
    json: async () => payload,
  } as unknown as Response);
  vi.stubGlobal("fetch", spy);
  return spy;
}

function file(name = "plan.csv") {
  return new File(["Date,Clics\n2026-07-01,5\n"], name, { type: "text/csv" });
}

async function pick(name?: string) {
  await userEvent.upload(screen.getByTestId("sample-file"), file(name));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("FileSourceSamplePanel", () => {
  it("posts the sample to the preview route and renders the review", async () => {
    const spy = mockFetch(PREVIEW_OK);
    render(<FileSourceSamplePanel projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />);

    await pick();

    await waitFor(() => expect(spy).toHaveBeenCalled());
    const [url, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/datastreams/ds_EXAMPLE/imports/preview");
    expect(init.method).toBe("POST");
    // Le corps porte le projet et les octets — jamais un chemin de fichier local.
    const body = JSON.parse(String(init.body));
    expect(body.project_id).toBe("proj_EXAMPLE");
    expect(body.filename).toBe("plan.csv");
    expect(typeof body.file_base64).toBe("string");

    await waitFor(() =>
      expect(screen.getByTestId("file-source-onboarding-review")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("gate-passed")).toBeInTheDocument();
    expect(screen.getByTestId("placement-class")).toHaveTextContent(/actual/);
  });

  it("says a Datastream without a template has nothing to review", async () => {
    mockFetch({ columns: [{ name: "a" }, { name: "b" }], file_source: null });
    render(<FileSourceSamplePanel projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />);

    await pick();

    // Pas une revue vide : l'absence de template est un état, et il se dit.
    await waitFor(() => expect(screen.getByTestId("sample-no-template")).toBeInTheDocument());
    expect(screen.queryByTestId("file-source-onboarding-review")).toBeNull();
  });

  it("explains a refusal instead of showing an empty review", async () => {
    mockFetch({
      columns: [],
      file_source: {
        fields: [],
        placement: null,
        gate: null,
        ambiguities: [],
        blocked: true,
        reason: "no_placement_class",
      },
    });
    render(<FileSourceSamplePanel projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />);

    await pick();

    await waitFor(() => expect(screen.getByTestId("sample-blocked")).toBeInTheDocument());
    expect(screen.getByTestId("sample-blocked")).toHaveTextContent(/matrix class/i);
    expect(screen.queryByTestId("file-source-onboarding-review")).toBeNull();
  });

  it("surfaces a server refusal rather than a blank panel", async () => {
    mockFetch({ message: "Preview unavailable" }, false);
    render(<FileSourceSamplePanel projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />);

    await pick();

    await waitFor(() => expect(screen.getByTestId("sample-error")).toBeInTheDocument());
    expect(screen.getByTestId("sample-error")).toHaveTextContent(/Preview unavailable/);
  });

  it("threads the mapping version so the gate replays it (AD-8)", async () => {
    const spy = mockFetch(PREVIEW_OK);
    render(
      <FileSourceSamplePanel
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        mappingVersionId="dmap_EXAMPLE"
      />,
    );

    await pick();

    await waitFor(() => expect(spy).toHaveBeenCalled());
    const body = JSON.parse(String((spy.mock.calls[0] as [string, RequestInit])[1].body));
    expect(body.mapping_version_id).toBe("dmap_EXAMPLE");
  });

  it("records an exact pending mapping confirmation without importing", async () => {
    const spy = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => PREVIEW_OK,
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          mapping_version_id: "dmap_PENDING",
          active_pointer_advanced: false,
        }),
      } as Response);
    vi.stubGlobal("fetch", spy);
    render(
      <FileSourceSamplePanel
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        mappingVersionId="dmap_BASE"
      />,
    );

    await pick();
    await waitFor(() => expect(screen.getByTestId("confirm-lock")).toBeEnabled());
    await userEvent.click(screen.getByTestId("confirm-lock"));

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    const [url, init] = spy.mock.calls[1] as [string, RequestInit];
    expect(url).toContain("/api/projects/proj_EXAMPLE/file-source-templates/confirm");
    const body = JSON.parse(String(init.body));
    expect(body.mapping_version_id).toBe("dmap_BASE");
    expect(body.file_base64).toBeTruthy();
    expect(screen.getByTestId("sample-confirmed")).toHaveTextContent("dmap_PENDING");
  });

  it("imports nothing: only the preview route is ever called", async () => {
    const spy = mockFetch(PREVIEW_OK);
    render(<FileSourceSamplePanel projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />);

    await pick();

    await waitFor(() => expect(spy).toHaveBeenCalled());
    // Un panneau d'aperçu qui posterait sur `/imports` importerait pour de vrai.
    for (const call of spy.mock.calls) {
      expect(String(call[0])).toMatch(/\/imports\/preview$/);
    }
  });
});

/**
 * AI-248 — le client déclare les dimensions dont il a besoin.
 *
 * Arbitrage de Jean, 2026-08-08 : les deux branches laissées ouvertes par
 * `file-source-ingestion.md:45-51` sont les deux PORTÉES, pas deux produits. Ce
 * qui l'a tranché est le cas d'usage : une vidéo demande 11 champs et les 13
 * gouvernés en couvrent ZÉRO — donc sans cette porte l'opérateur ouvre un select
 * vide, ne peut jamais résoudre, et le gate reste bloqué pour toujours.
 */
const PREVIEW_FLAGGED = {
  columns: [{ name: "duration_s" }],
  file_source: {
    fields: [
      { source_column: "duration_s", canonical_target: null, confidence: 0.2, status: "flagged" },
    ],
    placement: { class: "actual", metric: "clicks", period: "day", dimension: [] },
    gate: {
      passed: false,
      missing_required: [],
      flagged: [{ source_column: "duration_s", blocking_reason: "low_confidence" }],
    },
    ambiguities: [],
    blocked: false,
    reason: null,
  },
};

describe("FileSourceSamplePanel — déclarer un champ canonique (AI-248)", () => {
  /** Aperçu flagué, puis catalogue VIDE — l'état mesuré en production. */
  function mockEmptyVocabulary() {
    const spy = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => PREVIEW_FLAGGED } as unknown as Response)
      .mockResolvedValueOnce({ ok: true, json: async () => ({ fields: [] }) } as unknown as Response);
    vi.stubGlobal("fetch", spy);
    return spy;
  }

  it("déclare une métrique avec son agrégation, et le champ résout la colonne qui l'a fait naître", async () => {
    const spy = mockEmptyVocabulary();
    spy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ fields: [{ id: "mdm_MINTED" }], declared: 1 }),
    } as unknown as Response);

    render(<FileSourceSamplePanel projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />);
    await pick();

    // L'impasse est nommée plutôt que rendue comme un select sans option.
    await waitFor(() =>
      expect(screen.getByTestId("empty-vocabulary-duration_s")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByTestId("declare-duration_s"));
    // Le nom est pré-rempli avec la colonne : l'opérateur nomme rarement
    // autrement, et retaper ce qu'on vient de lire est du travail inutile.
    expect(screen.getByTestId("declare-name")).toHaveValue("duration_s");

    await userEvent.selectOptions(screen.getByTestId("declare-kind"), "metric");
    await userEvent.selectOptions(screen.getByTestId("declare-value-type"), "integer");
    await userEvent.selectOptions(screen.getByTestId("declare-aggregation"), "sum");
    await userEvent.click(screen.getByTestId("declare-submit"));

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(3));
    const [url, init] = spy.mock.calls[2] as [string, RequestInit];
    expect(url).toContain("/file-source-templates/canonical-fields");
    expect(init.method).toBe("POST");
    const body = JSON.parse(String(init.body));
    // Story 64.15 : l'ecran nomme le FLUX, jamais l'objet. Le serveur lit quel
    // objet ce Datastream alimente ; envoyer l'`object_kind` d'ici permettrait
    // d'accrocher la colonne a la definition d'un AUTRE objet, et les deux
    // repondraient ensuite differemment a la meme question.
    expect(body.datastream_id).toBe("ds_EXAMPLE");
    expect(body.fields[0].object_kind).toBeUndefined();
    // Une métrique DOIT porter son agrégation : le serveur refuse autrement, avec
    // « a measure must never be silently non-summable ». L'écran l'envoie plutôt
    // que de laisser le refus arriver après coup.
    // Story 64.14 : `value_type` voyage avec la declaration. Sans lui le champ ne
    // deviendrait jamais un Concept -- `semantic_concept_versions.value_type` est
    // NOT NULL -- et le serveur refuse desormais avant d ecrire.
    expect(body.fields[0]).toMatchObject({
      canonical_name: "duration_s",
      concept_kind: "metric",
      value_type: "integer",
      aggregation: "sum",
    });

    // Le champ né résout la colonne : redemander la sélection serait faire
    // refaire le geste qu'on vient de faire.
    await waitFor(() =>
      expect(screen.getByTestId("resolve-duration_s")).toHaveValue("mdm_MINTED"),
    );
  });

  it("n'envoie aucune agrégation pour une dimension", async () => {
    const spy = mockEmptyVocabulary();
    spy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ fields: [{ id: "mdm_DIM" }], declared: 1 }),
    } as unknown as Response);

    render(<FileSourceSamplePanel projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />);
    await pick();
    await waitFor(() => expect(screen.getByTestId("declare-duration_s")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("declare-duration_s"));

    // Le contrôle d'agrégation n'existe même pas pour une dimension : le serveur
    // refuse « a dimension carries no aggregation », et un champ que personne ne
    // lit est un champ que quelqu'un croit utilisé.
    expect(screen.queryByTestId("declare-aggregation")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("declare-submit"));
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(3));
    const body = JSON.parse(String((spy.mock.calls[2] as [string, RequestInit])[1].body));
    expect(body.fields[0]).toEqual({
      canonical_name: "duration_s",
      concept_kind: "dimension",
      value_type: "string",
    });
  });

  it("montre le refus du serveur au lieu de l'avaler", async () => {
    const spy = mockEmptyVocabulary();
    spy.mockResolvedValueOnce({
      ok: false,
      json: async () => ({ code: "invalid_declaration", message: "the metric declares neither" }),
    } as unknown as Response);

    render(<FileSourceSamplePanel projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />);
    await pick();
    await waitFor(() => expect(screen.getByTestId("declare-duration_s")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("declare-duration_s"));
    await userEvent.click(screen.getByTestId("declare-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("sample-error")).toHaveTextContent(/declares neither/i),
    );
  });
});
