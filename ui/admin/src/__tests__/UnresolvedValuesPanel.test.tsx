/**
 * Values waiting to be mapped — S1 and S2 of `unresolved-values.md`, on screen.
 *
 * Five things are held here, and they are five of that document's own
 * "Incomplete if" bullets:
 *
 *   1. the three reasons are never one count, and one of them never offers the
 *      gesture of another — a row whose reason is `absent_at_source` gets NO
 *      pair editor, only the step that repairs it;
 *   2. the list never renders `0` when the mart could not be read: the mandated
 *      sentence appears and NO table is drawn underneath;
 *   3. the empty case says what was MEASURED, in the mandated words;
 *   4. the Workbench and the Governance lens cannot answer two different numbers
 *      for one Project — one component, one route, asserted on the address each
 *      scope reads;
 *   5. what is not built is NAMED — the repair drawer and the proposal column
 *      are absent controls that say so, never controls that open nothing.
 *
 * `fetch` is stubbed and `apiFetch` is not: the seam guard is what proves the
 * bearer is attached, and stubbing the seam would prove nothing.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import UnresolvedValuesPanel from "../governance/UnresolvedValuesPanel";

const PROJECT = "proj_EXAMPLE";
const STREAM = "ds_EXAMPLE";

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
    blob: async () => ({ size: 1, type: "text/csv" }) as Blob,
  } as Response;
}

function fail(status: number, code: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ code, message: "refused" }),
    text: async () => code,
  } as Response;
}

/** Route the fetch mock by URL, so each test states what each address answers. */
function serve(routes: Array<[RegExp, Response | (() => Response)]>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      calls.push(url);
      for (const [pattern, answer] of routes) {
        if (pattern.test(url)) {
          return Promise.resolve(typeof answer === "function" ? answer() : answer);
        }
      }
      return Promise.resolve(fail(404, "not_found"));
    }),
  );
  return calls;
}

const WINDOW = { start: "2026-07-31", end: "2026-08-06", days: 7 };

function row(overrides: Record<string, unknown> = {}) {
  return {
    dimension: "video",
    source_value: "vid_heavy",
    connector: "youtube",
    occurrences: 3,
    row_share: 0.5,
    metric_share: null,
    reason: "unmapped",
    repair:
      "This value has no canonical name yet. Add the pair in the value mapping table assigned to this field.",
    action: { kind: "map", label: "Map to…", pair_editor: true },
    datastream_id: STREAM,
    ...overrides,
  };
}

function group(overrides: Record<string, unknown> = {}) {
  return {
    key: `${STREAM}::video`,
    datastream_id: STREAM,
    datastream_name: "YouTube performance",
    dimension: "video",
    canonical_dimension: "video_id",
    connector: "youtube",
    state: "measured",
    reason: null,
    message: null,
    unresolved: 2,
    observed_distinct: 5,
    occurrences: 4,
    window_rows: 6,
    row_share: 0.666,
    complete: true,
    truncated: false,
    by_reason: { absent_at_source: 1, unmapped: 1, no_reference: 0 },
    destination_table: null,
    destination_state: "known",
    values: [
      row(),
      row({
        source_value: "",
        occurrences: 1,
        row_share: 0.166,
        reason: "absent_at_source",
        repair:
          "This column arrived with no value, so no mapping would fill it. Repair the collection or the field mapping.",
        action: { kind: "repair_source", label: null, pair_editor: false },
      }),
    ],
    ...overrides,
  };
}

function envelope(overrides: Record<string, unknown> = {}) {
  return {
    schema: "unresolved_values.v1",
    project_id: PROJECT,
    datastream_id: STREAM,
    scope: "datastream",
    grouping: "dimension",
    title: "Values waiting to be mapped",
    window: WINDOW,
    state: "measured",
    reason: null,
    message: null,
    summary: {
      unresolved: 2,
      dimensions_measured: 1,
      dimensions_listed: 1,
      complete: true,
      by_reason: { absent_at_source: 1, unmapped: 1, no_reference: 0 },
    },
    groups: [group()],
    ranking: { by: "rows", metric_share: null, note: "Values are ranked by the rows that carry them." },
    repair_drawer: {
      available: true,
      note: "A rule is unfolded into one pair per value, not kept as a pattern.",
      match_modes: [
        { value: "exact", label: "exact" },
        { value: "exact_ci", label: "exact (case-insensitive)" },
        { value: "contains", label: "contains" },
        { value: "regex", label: "regex" },
      ],
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
    proposals: { available: false, note: "No proposal is offered on these rows." },
    extract: { available: true, destination: "value_mapping_table" },
    import: {
      available: true,
      destination: "value_mapping_table",
      write_mode: "append_by_source_value",
      write_mode_note: "This appends by source value.",
      other_destination_note: "Filling the mapping file behind a Template is not built here.",
    },
    bounds: {
      dimensions_truncated: false,
      datastreams_opened: null,
      datastreams_total: null,
      datastreams_truncated: false,
    },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("S1 — the Workbench Map tab", () => {
  it("heads the panel with the count and the dimensions it was measured over", async () => {
    serve([[/unresolved-values$/, ok(envelope())]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    expect(
      await screen.findByText("Values waiting to be mapped · 2 across 1 dimension"),
    ).toBeInTheDocument();
    expect(screen.getByText(/2026-07-31 → 2026-08-06 \(7 days\)/)).toBeInTheDocument();
  });

  it("reads the per-Datastream address, never the Project one", async () => {
    const calls = serve([[/unresolved-values$/, ok(envelope())]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    await screen.findByTestId(`unresolved-group-${STREAM}::video`);
    expect(calls[0]).toBe(
      `/api/projects/${PROJECT}/datastreams/${STREAM}/unresolved-values`,
    );
  });

  it("gives an `absent_at_source` row NO pair editor, only the step that repairs it", async () => {
    serve([[/unresolved-values$/, ok(envelope())]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    // La ligne qu'une paire PEUT reparer offre le controle, et il OUVRE
    // desormais quelque chose (S3, 2026-08-22).
    const mappable = await screen.findByTestId("unresolved-map-vid_heavy");
    expect(mappable).toBeEnabled();

    // The row no pair can repair offers none at all. This is the defect the
    // typing exists to prevent.
    expect(screen.queryByTestId("unresolved-map-blank")).not.toBeInTheDocument();
    expect(screen.getByTestId("unresolved-repair-blank")).toHaveTextContent(
      "Repair the collection or the field mapping.",
    );
  });

  it("names the three reasons in the person's words, never in the stored one", async () => {
    serve([[/unresolved-values$/, ok(envelope())]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    const blank = await screen.findByTestId("unresolved-row-blank");
    expect(within(blank).getByText("Nothing arrived")).toBeInTheDocument();
    expect(screen.queryByText("absent_at_source")).not.toBeInTheDocument();
    const heavy = screen.getByTestId("unresolved-row-vid_heavy");
    expect(within(heavy).getByText("No canonical name")).toBeInTheDocument();
    // The ranking is by rows, and the metric share is not invented.
    expect(within(heavy).getByText("50.0 %")).toBeInTheDocument();
    expect(within(heavy).getByText("not measured")).toBeInTheDocument();
  });

  it("says what was MEASURED when everything resolves", async () => {
    serve([
      [
        /unresolved-values$/,
        ok(
          envelope({
            groups: [],
            summary: {
              unresolved: 0,
              dimensions_measured: 3,
              dimensions_listed: 3,
              complete: true,
              by_reason: { absent_at_source: 0, unmapped: 0, no_reference: 0 },
            },
            message: "Every value of the 3 mapped dimensions resolves over the last 7 days.",
          }),
        ),
      ],
    ]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    expect(
      await screen.findByText(
        "Every value of the 3 mapped dimensions resolves over the last 7 days.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("prints `unknown` and NO table when the warehouse could not be read", async () => {
    serve([
      [
        /unresolved-values$/,
        ok(
          envelope({
            state: "unavailable",
            reason: "warehouse_unavailable",
            groups: [],
            summary: {
              unresolved: null,
              dimensions_measured: 0,
              dimensions_listed: 0,
              complete: true,
              by_reason: { absent_at_source: 0, unmapped: 0, no_reference: 0 },
            },
            message:
              "The warehouse could not be read, so unresolved values are unknown for this window. This is not a count of zero.",
          }),
        ),
      ],
    ]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    const said = await screen.findByTestId("unresolved-unavailable");
    expect(said).toHaveTextContent(
      "The warehouse could not be read, so unresolved values are unknown for this window. This is not a count of zero.",
    );
    // The count reads `unknown`, and it is NEVER a zero.
    expect(screen.getByText(/Values waiting to be mapped · unknown across 0/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("names the gesture when no dimension is mapped yet", async () => {
    serve([
      [
        /unresolved-values$/,
        ok(
          envelope({
            state: "not_applicable",
            reason: "no_mapped_dimension",
            groups: [],
            summary: {
              unresolved: null,
              dimensions_measured: 0,
              dimensions_listed: 0,
              complete: true,
              by_reason: { absent_at_source: 0, unmapped: 0, no_reference: 0 },
            },
            message:
              "No column of this Datastream is both part of the declared grain and bound to a canonical field, so there is no dimension whose values could be resolved. Bind a grain column to a canonical field in the bindings panel above.",
          }),
        ),
      ],
    ]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    expect(await screen.findByTestId("unresolved-not-applicable")).toHaveTextContent(
      "Bind a grain column to a canonical field in the bindings panel above.",
    );
  });

  it("says « could not be read » and draws no list when the route fails", async () => {
    serve([[/unresolved-values$/, fail(503, "unavailable")]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    const broken = await screen.findByTestId("unresolved-broken");
    expect(broken).toHaveTextContent("This is not a Datastream whose values all resolve.");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("names what is STILL absent, and nothing that has since been built", async () => {
    /**
     * Le defaut symetrique de celui que ce test gardait. Il verifiait qu'un
     * controle mort DIT pourquoi ; depuis le 2026-08-22 le tiroir et l'import
     * existent, et continuer a les annoncer absents serait le meme mensonge
     * dans l'autre sens -- une personne lirait "pas encore bati" au-dessus d'un
     * bouton qui marche. Ce qui reste absent, la colonne de propositions, garde
     * sa phrase.
     */
    serve([[/unresolved-values$/, ok(envelope())]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    const note = await screen.findByTestId("unresolved-not-built");
    expect(note).toHaveTextContent("No proposal is offered on these rows.");
    expect(note).not.toHaveTextContent("repair drawer");
    expect(screen.getByTestId(`unresolved-import-${STREAM}::video`)).toBeEnabled();
  });

  it("downloads the two-column extract from the address that builds it", async () => {
    const calls = serve([
      [/unresolved-values\/extract/, ok("source_value,canonical_value\nvid_heavy,\n")],
      [/unresolved-values$/, ok(envelope())],
    ]);
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:x"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    await userEvent.click(await screen.findByTestId(`unresolved-export-${STREAM}::video`));
    await waitFor(() =>
      expect(
        calls.some((url) =>
          url.includes(
            `/api/projects/${PROJECT}/datastreams/${STREAM}/unresolved-values/extract?dimension=video`,
          ),
        ),
      ).toBe(true),
    );
  });

  it("states that a listing stopped at its bound rather than reading as everything", async () => {
    serve([[/unresolved-values$/, ok(envelope({ groups: [group({ truncated: true })] }))]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    expect(await screen.findByText(/This list stopped at its bound/)).toBeInTheDocument();
  });
});

describe("S2 — the Value Tables lens, the same panel one projection wider", () => {
  const wide = () =>
    envelope({
      datastream_id: null,
      scope: "project",
      grouping: "destination_table",
      groups: [group({ destination_table: { table_id: "vmt_1", table_name: "Video catalogue" } })],
    });

  it("reads the Project address and answers the SAME number as the Datastream one", async () => {
    const calls = serve([[/unresolved-values$/, ok(wide())]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} scope="project" />);

    await screen.findByTestId(`unresolved-group-${STREAM}::video`);
    expect(calls[0]).toBe(`/api/projects/${PROJECT}/unresolved-values`);
    // One reading, one component: the headline is composed from the same
    // envelope both surfaces receive, so the two cannot disagree.
    expect(
      screen.getByText("Values waiting to be mapped · 2 across 1 dimension"),
    ).toBeInTheDocument();
  });

  it("adds a Datastream column and groups by the destination table", async () => {
    serve([[/unresolved-values$/, ok(wide())]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} scope="project" />);

    expect(await screen.findByText("Video catalogue")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Datastream" })).toBeInTheDocument();
  });

  it("deep-links every row to the Datastream that emitted the value", async () => {
    serve([[/unresolved-values$/, ok(wide())]]);
    const opened: string[] = [];
    render(
      <UnresolvedValuesPanel
        projectId={PROJECT}
        scope="project"
        onOpenDatastream={(id) => opened.push(id)}
      />,
    );

    await userEvent.click(await screen.findByTestId("unresolved-open-vid_heavy"));
    expect(opened).toEqual([STREAM]);
  });

  it("says how many Datastreams it opened rather than implying it read them all", async () => {
    serve([
      [
        /unresolved-values$/,
        ok(
          envelope({
            scope: "project",
            grouping: "destination_table",
            bounds: {
              dimensions_truncated: false,
              datastreams_opened: 8,
              datastreams_total: 42,
              datastreams_truncated: true,
            },
          }),
        ),
      ],
    ]);
    render(<UnresolvedValuesPanel projectId={PROJECT} scope="project" />);

    expect(await screen.findByTestId("unresolved-streams-bounded")).toHaveTextContent(
      "8 of 42 Datastreams were read for this list.",
    );
  });

  it("states the Datastream bound even when everything it DID read resolves", async () => {
    /**
     * Le defaut mesure le 2026-08-30. L'avertissement de borne vivait DANS la
     * branche `unresolved > 0`, donc un Projet dont les huit flux ouverts sont
     * propres imprimait « Everything resolves » pendant que le panneau du
     * neuvieme listait ses trous : les deux surfaces repondaient deux nombres
     * pour un meme Projet, ce que le document refuse. La borne se dit ici
     * PRECISEMENT parce que la liste est vide.
     */
    serve([
      [
        /unresolved-values$/,
        ok(
          envelope({
            scope: "project",
            grouping: "destination_table",
            groups: [],
            summary: {
              unresolved: 0,
              dimensions_measured: 4,
              dimensions_listed: 4,
              complete: true,
              by_reason: { absent_at_source: 0, unmapped: 0, no_reference: 0 },
            },
            message: "Every value of the 4 mapped dimensions resolves over the last 7 days.",
            bounds: {
              dimensions_truncated: false,
              datastreams_opened: 8,
              datastreams_total: 9,
              datastreams_truncated: true,
            },
          }),
        ),
      ],
    ]);
    render(<UnresolvedValuesPanel projectId={PROJECT} scope="project" />);

    const bound = await screen.findByTestId("unresolved-streams-bounded");
    expect(bound).toHaveTextContent("8 of 9 Datastreams were read for this list.");
    expect(bound).toHaveTextContent("this is not the whole Project");
    // Le geste qui lit le reste est NOMME, et il existe.
    expect(bound).toHaveTextContent("open a Datastream and read its own Map tab");
    // Et le vert non qualifie a disparu.
    expect(screen.queryByText("Everything resolves")).not.toBeInTheDocument();
    expect(screen.getByText("Everything that was read resolves")).toBeInTheDocument();
  });

  it("states the dimension bound, which was typed on the payload and rendered nowhere", async () => {
    /**
     * `bounds.dimensions_truncated` etait declare dans le type et lu par aucun
     * rendu : une troncature silencieuse se lit « on a tout regarde », ce que le
     * document interdit en toutes lettres.
     */
    serve([
      [
        /unresolved-values$/,
        ok(
          envelope({
            bounds: {
              dimensions_truncated: true,
              datastreams_opened: null,
              datastreams_total: null,
              datastreams_truncated: false,
            },
          }),
        ),
      ],
    ]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    const bound = await screen.findByTestId("unresolved-dimensions-bounded");
    expect(bound).toHaveTextContent(
      "More dimensions are declared here than one reading opens",
    );
    expect(bound).toHaveTextContent("none of their values is counted below");
    // Rien ne fait semblant d'exister : aucun geste n'elargit la lecture, et la
    // phrase le dit plutot que d'en inventer un.
    expect(bound).toHaveTextContent("Nothing on this screen widens the reading");
  });

  it("qualifies the empty answer when the dimension bound cut the reading", async () => {
    serve([
      [
        /unresolved-values$/,
        ok(
          envelope({
            groups: [],
            summary: {
              unresolved: 0,
              dimensions_measured: 12,
              dimensions_listed: 12,
              complete: true,
              by_reason: { absent_at_source: 0, unmapped: 0, no_reference: 0 },
            },
            message: "Every value of the 12 mapped dimensions resolves over the last 7 days.",
            bounds: {
              dimensions_truncated: true,
              datastreams_opened: null,
              datastreams_total: null,
              datastreams_truncated: false,
            },
          }),
        ),
      ],
    ]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    expect(await screen.findByTestId("unresolved-dimensions-bounded")).toBeInTheDocument();
    expect(screen.queryByText("Everything resolves")).not.toBeInTheDocument();
    expect(screen.getByText("Everything that was read resolves")).toBeInTheDocument();
  });

  it("says nothing about a bound when the reading opened everything", async () => {
    // Le defaut symetrique : un avertissement de borne permanent se lit comme un
    // decor et rend le vrai illisible.
    serve([[/unresolved-values$/, ok(envelope())]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    await screen.findByTestId(`unresolved-group-${STREAM}::video`);
    expect(screen.queryByTestId("unresolved-streams-bounded")).not.toBeInTheDocument();
    expect(screen.queryByTestId("unresolved-dimensions-bounded")).not.toBeInTheDocument();
  });

  it("shows no Datastream column on the per-Datastream surface", async () => {
    serve([[/unresolved-values$/, ok(envelope())]]);
    render(<UnresolvedValuesPanel projectId={PROJECT} datastreamId={STREAM} scope="datastream" />);

    await screen.findByTestId(`unresolved-group-${STREAM}::video`);
    expect(screen.queryByRole("columnheader", { name: "Datastream" })).not.toBeInTheDocument();
  });
});
