/**
 * L'import d'un fichier rempli — S4 de `unresolved-values.md`, sur l'écran.
 *
 * Le document demande trois choses et chacune est un défaut évité :
 *
 *   * **l'aperçu précède l'écriture** — « Never a partial write in silence » ;
 *   * **chaque ligne refusée porte son NUMÉRO et son motif** — une ligne
 *     silencieusement sautée présente un vocabulaire partiel comme complet ;
 *   * **le mode d'écriture est ÉNONCÉ sur la boîte, pas supposé** — « instead of
 *     silently replacing a file somebody spent a week filling ».
 *
 * Le test qui compte le plus est le premier : tant que l'aperçu n'a pas été
 * demandé, le bouton d'import est tenu. C'est la seule barrière entre un fichier
 * mal collé et une table réécrite.
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import UnresolvedImportDialog from "../governance/UnresolvedImportDialog";
import type { ValueMappingTableSummary } from "../governance/UnresolvedRepairDrawer";

const PROJECT = "proj_EXAMPLE";
const STREAM = "ds_EXAMPLE";
const TABLE = "vmt_EXAMPLE";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body, text: async () => "" } as Response;
}
function fail(status: number, body: unknown): Response {
  return { ok: false, status, json: async () => body, text: async () => "" } as Response;
}

function serve(routes: Array<[RegExp, Response]>) {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
      for (const [pattern, answer] of routes) if (pattern.test(url)) return Promise.resolve(answer);
      return Promise.resolve(fail(404, { code: "not_found", message: "refused" }));
    }),
  );
  return calls;
}

const GROUP = {
  key: `${STREAM}::country`,
  datastream_id: STREAM,
  dimension: "country",
  canonical_dimension: "country_code",
  connector: "youtube",
  state: "measured" as const,
  reason: null,
  message: null,
  unresolved: 3,
  observed_distinct: 5,
  occurrences: 9,
  window_rows: 20,
  row_share: 0.45,
  complete: true,
  truncated: false,
  by_reason: null,
  destination_table: null,
  destination_state: "known" as const,
  values: [],
};

const PAYLOAD = {
  schema: "unresolved_values.v1",
  project_id: PROJECT,
  datastream_id: STREAM,
  scope: "datastream" as const,
  grouping: "dimension" as const,
  title: "Values waiting to be mapped",
  window: { start: "2026-07-31", end: "2026-08-06", days: 7 },
  state: "measured" as const,
  reason: null,
  message: null,
  summary: {
    unresolved: 3,
    dimensions_measured: 1,
    dimensions_listed: 1,
    complete: true,
    by_reason: {},
  },
  groups: [GROUP],
  ranking: { by: "rows", metric_share: null as null, note: "" },
  repair_drawer: {
    available: true,
    note: "",
    match_modes: [],
    after_write: {
      recheck: true,
      still_listed_note:
        "The list that showed the gap was read again just now, and it still carries these values. A pair goes into the value table; this reading resolves the confirmed mappings instead, and nothing in the product carries a pair from one into the other. Which of the two a reading should resolve has not been decided, so nothing here can promise the next read will differ.",
      cleared_note:
        "The list that showed the gap was read again just now, and it no longer carries these values.",
      unknown_note:
        "The list that showed the gap could not be read again just now, so whether it still carries these values is unknown. This is not a repair that landed.",
    },
  },
  proposals: { available: false, note: "" },
  extract: { available: true, destination: "value_mapping_table" },
  import: {
    available: true,
    destination: "value_mapping_table",
    write_mode: "append_by_source_value",
    write_mode_note:
      "This appends by source value. A value the table already carries is reported as already there and is never overwritten.",
    other_destination_note: "Filling the mapping file behind a Template is not built here.",
  },
  bounds: {
    dimensions_truncated: false,
    datastreams_opened: null,
    datastreams_total: null,
    datastreams_truncated: false,
  },
};

const TABLES: ValueMappingTableSummary[] = [
  {
    id: TABLE,
    name: "Country vocabulary",
    scope_level: "PROJECT",
    entry_count: 2,
    assignment_count: 1,
    datastream_count: 1,
    current_version_id: null,
  },
];

function draw(props: Record<string, unknown> = {}) {
  return render(
    <UnresolvedImportDialog
      projectId={PROJECT}
      group={GROUP}
      payload={PAYLOAD}
      tables={TABLES}
      onClose={() => {}}
      onImported={async () => null}
      {...(props as Record<string, never>)}
    />,
  );
}

async function fill() {
  await userEvent.selectOptions(screen.getByTestId("import-destination"), TABLE);
  await userEvent.type(screen.getByTestId("import-text"), "FR,France");
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("S4 — l'import d'un fichier rempli", () => {
  it("TIENT l'import tant que le fichier n'a pas été vu", async () => {
    serve([]);
    draw();
    await fill();

    expect(screen.getByTestId("import-confirm")).toBeDisabled();
    expect(screen.getByTestId("import-confirm")).toHaveAttribute(
      "title",
      expect.stringContaining("Check the file first"),
    );
  });

  it("nomme chaque ligne refusée par son NUMÉRO et son motif", async () => {
    serve([
      [
        /import\/preview$/,
        ok({
          would_import_count: 1,
          rejected_count: 2,
          rejected: [
            { line: 2, reason: "not_two_columns", content: "solo" },
            { line: 3, reason: "empty_canonical_value", content: "IT," },
          ],
          over_limit: false,
          limit: 5000,
          impact: { impact_state: "known", datastream_count: 1 },
        }),
      ],
    ]);
    draw();
    await fill();
    await userEvent.click(screen.getByTestId("import-check"));

    const rejected = await screen.findByTestId("import-rejected");
    expect(rejected).toHaveTextContent("Line 2");
    expect(rejected).toHaveTextContent("does not carry exactly two columns");
    expect(rejected).toHaveTextContent("Line 3");
    expect(rejected).toHaveTextContent("canonical value is empty");
  });

  it("ÉNONCE le mode d'écriture au lieu de le laisser supposer", async () => {
    serve([]);
    draw();

    expect(screen.getByTestId("import-write-mode")).toHaveTextContent(
      "never overwritten",
    );
  });

  it("l'aperçu N'ÉCRIT PAS : la seule adresse touchée est celle de l'aperçu", async () => {
    const calls = serve([
      [
        /import\/preview$/,
        ok({
          would_import_count: 1,
          rejected_count: 0,
          rejected: [],
          over_limit: false,
          limit: 5000,
          impact: { impact_state: "known", datastream_count: 1 },
        }),
      ],
    ]);
    draw();
    await fill();
    await userEvent.click(screen.getByTestId("import-check"));
    await screen.findByTestId("import-preview");

    const written = calls.filter((c) => /value-mapping-tables\/[^/]+\/import$/.test(c.url));
    expect(written).toHaveLength(0);
  });

  it("tient l'import quand l'impact n'a pas pu être lu", async () => {
    serve([
      [
        /import\/preview$/,
        ok({
          would_import_count: 3,
          rejected_count: 0,
          rejected: [],
          over_limit: false,
          limit: 5000,
          impact: {
            impact_state: "unknown",
            datastream_count: null,
            message: "What depends on this table could not be read, so this import is held.",
          },
        }),
      ],
    ]);
    draw();
    await fill();
    await userEvent.click(screen.getByTestId("import-check"));

    expect(await screen.findByTestId("import-preview")).toHaveTextContent("could not be read");
    expect(screen.getByTestId("import-confirm")).toBeDisabled();
  });

  it("écrit une fois l'aperçu vu, et dit combien", async () => {
    serve([
      [
        /import\/preview$/,
        ok({
          would_import_count: 2,
          rejected_count: 0,
          rejected: [],
          over_limit: false,
          limit: 5000,
          impact: { impact_state: "known", datastream_count: 1 },
        }),
      ],
      [/import$/, ok({ imported_count: 2 })],
    ]);
    draw();
    await fill();
    await userEvent.click(screen.getByTestId("import-check"));
    await screen.findByTestId("import-preview");
    await userEvent.click(screen.getByTestId("import-confirm"));

    expect(await screen.findByTestId("import-done")).toHaveTextContent("2 pairs written");
  });

  it("NE PROMET RIEN : il relit, et dit que la liste porte encore les valeurs", async () => {
    /**
     * Le meme defaut que dans le tiroir, mesure le 2026-08-30 : la boite ecrit
     * dans la table de valeurs et la liste resout les correspondances
     * confirmees. Rien ne joint les deux, donc rien ici ne peut annoncer que la
     * prochaine lecture sera differente -- elle est relue, et ce qu'elle repond
     * est ce qui s'affiche.
     */
    serve([
      [
        /import\/preview$/,
        ok({
          would_import_count: 1,
          rejected_count: 0,
          would_import: [{ source_value: "FR", canonical_value: "France" }],
          rejected: [],
          over_limit: false,
          limit: 5000,
          impact: { impact_state: "known", datastream_count: 1 },
        }),
      ],
      [/import$/, ok({ imported_count: 1 })],
    ]);
    draw({
      onImported: async () => ({
        ...PAYLOAD,
        groups: [
          {
            ...GROUP,
            values: [
              {
                dimension: "country",
                source_value: "FR",
                connector: "youtube",
                occurrences: 3,
                row_share: 0.2,
                metric_share: null,
                reason: "unmapped",
                repair: "Add the pair in the value mapping table assigned to this field.",
                action: { kind: "map", label: "Map to…", pair_editor: true },
                datastream_id: STREAM,
              },
            ],
          },
        ],
      }),
    });
    await fill();
    await userEvent.click(screen.getByTestId("import-check"));
    await screen.findByTestId("import-preview");
    await userEvent.click(screen.getByTestId("import-confirm"));

    const done = await screen.findByTestId("import-done");
    expect(done).toHaveTextContent("Written, and the list still carries them");
    expect(done).toHaveTextContent("it still carries these values");
    // L'ancienne affirmation, celle que rien ne mesurait.
    expect(done).not.toHaveTextContent("It applies at the next read");
  });

  it("n'offre aucun bouton qui ne fasse rien une fois le fichier écrit", async () => {
    serve([
      [
        /import\/preview$/,
        ok({
          would_import_count: 1,
          rejected_count: 0,
          would_import: [{ source_value: "FR", canonical_value: "France" }],
          rejected: [],
          over_limit: false,
          limit: 5000,
          impact: { impact_state: "known", datastream_count: 1 },
        }),
      ],
      [/import$/, ok({ imported_count: 1 })],
    ]);
    draw();
    await fill();
    await userEvent.click(screen.getByTestId("import-check"));
    await screen.findByTestId("import-preview");
    await userEvent.click(screen.getByTestId("import-confirm"));

    await screen.findByTestId("import-done");
    // Le seul bouton NOMME qui reste referme la boite, et il fait exactement
    // cela. (La croix du panneau ne porte pas de texte et ferme aussi.)
    expect(screen.queryByText(/Reprocess/i)).not.toBeInTheDocument();
    expect(
      screen
        .getAllByRole("button")
        .map((one) => one.textContent)
        .filter((label) => (label ?? "").trim() !== ""),
    ).toEqual(["Close"]);
  });

  it("nomme la destination S4 qu'il ne sert PAS", async () => {
    serve([]);
    draw();

    expect(screen.getByTestId("import-other-destination")).toHaveTextContent(
      "Filling the mapping file behind a Template is not built here.",
    );
  });
});
