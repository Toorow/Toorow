/**
 * The repair drawer on screen — S3 of `unresolved-values.md`.
 *
 * Le panneau S1/S2 prouvait qu'un controle mort DIT pourquoi. Ce fichier prouve
 * l'autre moitie, celle qui manquait : que le geste se fait ICI. Chaque test
 * tient une phrase du document, et deux d'entre eux tiennent la propriete qui
 * distingue ce tiroir d'un formulaire :
 *
 *   * **la portee est lue AVANT la confirmation** — le document en donne la
 *     raison en une ligne, « that sentence is what turns 200 pairs into one
 *     rule », et une phrase qui arrive apres le clic ne transforme rien ;
 *   * **un impact illisible TIENT la confirmation** — « I could not check » n'est
 *     pas « nothing depends on this », et c'est la seule facon de rendre un
 *     refus honnete distinguable d'un feu vert.
 *
 * `fetch` est stubbe, `apiPost` ne l'est pas : c'est la couture qui prouve que
 * le jeton voyage, et la stubber ne prouverait rien.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import UnresolvedRepairDrawer from "../governance/UnresolvedRepairDrawer";
import type { ValueMappingTableSummary } from "../governance/UnresolvedRepairDrawer";

const PROJECT = "proj_EXAMPLE";
const STREAM = "ds_EXAMPLE";
const TABLE = "vmt_EXAMPLE";

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function fail(status: number, body: unknown): Response {
  return {
    ok: false,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function serve(routes: Array<[RegExp, Response | (() => Response)]>) {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({
        url,
        body: typeof init?.body === "string" ? JSON.parse(init.body) : null,
      });
      for (const [pattern, answer] of routes) {
        if (pattern.test(url)) {
          return Promise.resolve(typeof answer === "function" ? answer() : answer);
        }
      }
      return Promise.resolve(fail(404, { code: "not_found", message: "refused" }));
    }),
  );
  return calls;
}

const ROW = {
  dimension: "video",
  source_value: "FR - Paris",
  connector: "youtube",
  occurrences: 12,
  row_share: 0.5,
  metric_share: null,
  reason: "unmapped",
  repair: "Add the pair in the value mapping table assigned to this field.",
  action: { kind: "map", label: "Map to…", pair_editor: true },
  datastream_id: STREAM,
};

const GROUP = {
  key: `${STREAM}::video`,
  datastream_id: STREAM,
  datastream_name: "YouTube performance",
  dimension: "video",
  canonical_dimension: "video_id",
  connector: "youtube",
  state: "measured" as const,
  reason: null,
  message: null,
  unresolved: 4,
  observed_distinct: 9,
  occurrences: 30,
  window_rows: 60,
  row_share: 0.5,
  complete: true,
  truncated: false,
  by_reason: null,
  destination_table: null,
  destination_state: "known" as const,
  values: [ROW],
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
    unresolved: 4,
    dimensions_measured: 1,
    dimensions_listed: 1,
    complete: true,
    by_reason: {},
  },
  groups: [GROUP],
  ranking: { by: "rows", metric_share: null as null, note: "" },
  repair_drawer: {
    available: true,
    note: "A rule is unfolded into one pair per value, not kept as a pattern.",
    match_modes: [
      { value: "exact", label: "exact" },
      { value: "contains", label: "contains" },
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
  proposals: { available: false, note: "No proposal is offered." },
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
};

const TABLES: ValueMappingTableSummary[] = [
  {
    id: TABLE,
    name: "Country vocabulary",
    scope_level: "PROJECT",
    entry_count: 4,
    assignment_count: 1,
    datastream_count: 2,
    current_version_id: "vmv_EXAMPLE",
  },
];

function reach(overrides: Record<string, unknown> = {}) {
  return {
    valid: true,
    reason: null,
    message: null,
    mode: "contains",
    mode_label: "contains",
    pattern: "FR - ",
    also_matches: 3,
    remaining: 4,
    occurrences: 18,
    sample: [
      { source_value: "FR - Lyon", occurrences: 8 },
      { source_value: "FR - Nice", occurrences: 6 },
      { source_value: "FR - Brest", occurrences: 4 },
    ],
    sample_truncated: false,
    matched_values: ["FR - Brest", "FR - Lyon", "FR - Nice"],
    corpus_truncated: false,
    sentence: "This rule also matches 3 of the 4 remaining values.",
    ...overrides,
  };
}

function preview(overrides: Record<string, unknown> = {}) {
  return {
    table_id: TABLE,
    table_name: "Country vocabulary",
    would_import_count: 4,
    rejected_count: 0,
    would_import: [],
    rejected: [],
    over_limit: false,
    limit: 5000,
    impact: {
      impact_state: "known",
      datastream_count: 2,
      assignment_count: 1,
      assignments: [],
    },
    ...overrides,
  };
}

function draw(props: Partial<Record<string, unknown>> = {}) {
  return render(
    <UnresolvedRepairDrawer
      projectId={PROJECT}
      row={ROW}
      group={GROUP}
      payload={PAYLOAD}
      tables={TABLES}
      onClose={() => {}}
      onRepaired={async () => null}
      onCreateTable={async () => TABLES[0]}
      {...(props as Record<string, never>)}
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("S3 — the repair drawer", () => {
  it("opens on the value, its occurrences and its window", async () => {
    serve([[/reach$/, ok(reach())]]);
    draw();

    // Le sujet est le TITRE du panneau, pas une ligne de plus dedans : un second
    // en-tete ecrit a la main serait libre de nommer la valeur autrement.
    const drawer = screen.getByTestId("unresolved-repair-drawer");
    expect(drawer).toHaveTextContent("FR - Paris");
    expect(drawer).toHaveTextContent(
      "12 occurrences in video, between 2026-07-31 and 2026-08-06.",
    );
  });

  it("reads the reach of the rule BEFORE anything is confirmed", async () => {
    /**
     * Le coeur de S3.3. La phrase doit etre a l'ecran sans qu'aucun bouton ait
     * ete presse -- sinon elle constate un choix au lieu de le former.
     */
    serve([[/reach$/, ok(reach())]]);
    draw();

    const sentence = await screen.findByTestId("repair-reach");
    await waitFor(() =>
      expect(sentence).toHaveTextContent("This rule also matches 3 of the 4 remaining values."),
    );
    // Rien n'a ete confirme : le bouton est toujours tenu.
    expect(screen.getByTestId("repair-confirm")).toBeDisabled();
  });

  it("excludes the value it was opened on, because the sentence says « also »", async () => {
    const calls = serve([[/reach$/, ok(reach())]]);
    draw();

    await waitFor(() => expect(calls.some((c) => /reach$/.test(c.url))).toBe(true));
    const asked = calls.find((c) => /reach$/.test(c.url))!.body as Record<string, unknown>;
    expect(asked.exclude).toBe("FR - Paris");
    expect(asked.dimension).toBe("video");
  });

  it("folds the values it matches, and unfolds them on demand", async () => {
    serve([[/reach$/, ok(reach())]]);
    draw();

    const toggle = await screen.findByTestId("repair-toggle-matches");
    expect(screen.queryByTestId("repair-matches")).not.toBeInTheDocument();
    await userEvent.click(toggle);
    expect(await screen.findByTestId("repair-matches")).toHaveTextContent("FR - Lyon");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
  });

  it("shows the server's own refusal for a rule that cannot run, and never a zero", async () => {
    serve([
      [
        /reach$/,
        fail(422, {
          ...reach(),
          valid: false,
          also_matches: null,
          occurrences: null,
          matched_values: [],
          message: "The regular expression does not compile. Fix it, or switch to `contains`.",
          sentence: "The regular expression does not compile. Fix it, or switch to `contains`.",
        }),
      ],
    ]);
    draw();

    const zone = await screen.findByTestId("repair-reach");
    await waitFor(() => expect(zone).toHaveTextContent("does not compile"));
    // ET SURTOUT : aucun « 0 », qui se lirait « cette regle ne touche rien ».
    expect(zone).not.toHaveTextContent("also matches 0");
  });

  it("holds the confirm until the repair has been checked", async () => {
    serve([[/reach$/, ok(reach())]]);
    draw();

    await screen.findByTestId("repair-reach");
    await userEvent.selectOptions(screen.getByTestId("repair-destination"), TABLE);
    await userEvent.type(screen.getByTestId("repair-canonical"), "France");

    expect(screen.getByTestId("repair-confirm")).toBeDisabled();
    expect(screen.getByTestId("repair-blocker")).toHaveTextContent(
      "Check this repair first",
    );
  });

  it("HOLDS THE CONFIRM when the impact could not be read", async () => {
    /**
     * S3.4, mot pour mot : « Unreadable impact reads `unknown` and disables the
     * confirm — "I could not check" is not "nothing depends on this" ». C'est le
     * seul test de ce fichier dont l'echec livrerait une ecriture aveugle.
     */
    serve([
      [/reach$/, ok(reach())],
      [
        /import\/preview$/,
        ok(
          preview({
            impact: {
              impact_state: "unknown",
              datastream_count: null,
              assignment_count: null,
              message: "What depends on this table could not be read, so this import is held.",
            },
          }),
        ),
      ],
    ]);
    draw();

    await screen.findByTestId("repair-reach");
    await userEvent.selectOptions(screen.getByTestId("repair-destination"), TABLE);
    await userEvent.type(screen.getByTestId("repair-canonical"), "France");
    await userEvent.click(screen.getByTestId("repair-check"));

    const impact = await screen.findByTestId("repair-impact");
    expect(impact).toHaveTextContent("could not be read");
    expect(screen.getByTestId("repair-confirm")).toBeDisabled();
  });

  it("states what depends on the table and the version the write would mint", async () => {
    serve([
      [/reach$/, ok(reach())],
      [/import\/preview$/, ok(preview())],
    ]);
    draw();

    await screen.findByTestId("repair-reach");
    await userEvent.selectOptions(screen.getByTestId("repair-destination"), TABLE);
    await userEvent.type(screen.getByTestId("repair-canonical"), "France");
    await userEvent.click(screen.getByTestId("repair-check"));

    const impact = await screen.findByTestId("repair-impact");
    expect(impact).toHaveTextContent("2 Datastreams read this table");
    expect(impact).toHaveTextContent("mints the next version");
    expect(screen.getByTestId("repair-confirm")).toBeEnabled();
  });

  it("writes ONE pair per value the rule reaches, in a single act", async () => {
    /** Le magasin tient des paires : la regle est DEPLIEE, et le fichier envoye
     *  doit porter la valeur ouverte plus chacune de celles que la regle atteint. */
    const calls = serve([
      [/reach$/, ok(reach())],
      [/import\/preview$/, ok(preview())],
      [/import$/, ok({ imported_count: 4 })],
    ]);
    draw();

    await screen.findByTestId("repair-reach");
    await userEvent.selectOptions(screen.getByTestId("repair-destination"), TABLE);
    await userEvent.type(screen.getByTestId("repair-canonical"), "France");
    await userEvent.click(screen.getByTestId("repair-check"));
    await screen.findByTestId("repair-impact");
    await userEvent.click(screen.getByTestId("repair-confirm"));

    await screen.findByTestId("repair-done");
    const written = calls.filter((c) => /value-mapping-tables\/[^/]+\/import$/.test(c.url));
    expect(written).toHaveLength(1);
    const text = (written[0].body as { text: string }).text;
    for (const value of ["FR - Paris", "FR - Brest", "FR - Lyon", "FR - Nice"]) {
      expect(text).toContain(`${value},France`);
    }
  });

  /** Une lecture rendue au tiroir apres l'ecriture, avec les valeurs qu'elle
   *  porte encore. C'est la MEME lecture que la personne a sous les yeux. */
  function reading(values: string[]) {
    return {
      ...PAYLOAD,
      groups: [
        {
          ...GROUP,
          values: values.map((source_value) => ({ ...ROW, source_value })),
        },
      ],
    };
  }

  async function writeIt(onRepaired: () => Promise<typeof PAYLOAD | null>) {
    serve([
      [/reach$/, ok(reach({ also_matches: 0, matched_values: [], sample: [] }))],
      [/import\/preview$/, ok(preview())],
      [/import$/, ok({ imported_count: 1 })],
    ]);
    draw({ onRepaired });

    await screen.findByTestId("repair-reach");
    await userEvent.selectOptions(screen.getByTestId("repair-destination"), TABLE);
    await userEvent.type(screen.getByTestId("repair-canonical"), "France");
    await userEvent.click(screen.getByTestId("repair-check"));
    await screen.findByTestId("repair-impact");
    await userEvent.click(screen.getByTestId("repair-confirm"));
    return await screen.findByTestId("repair-done");
  }

  it("NE PROMET RIEN : il relit, et dit que la liste porte encore la valeur", async () => {
    /**
     * Le defaut mesure le 2026-08-30, et c'est le dernier « Incomplete if » de
     * la page : « the screen says a value was repaired while the reading that
     * showed it still renders the old one ». L'ecriture va dans la table de
     * valeurs, la lecture resout les correspondances confirmees, et rien ne
     * joint les deux -- ce que l'ecran doit DIRE, faute de pouvoir le reparer.
     */
    const done = await writeIt(async () => reading(["FR - Paris"]));

    expect(done).toHaveTextContent("Written, and the list still carries it");
    expect(done).toHaveTextContent("it still carries these values");
    expect(done).toHaveTextContent("nothing in the product carries a pair from one into the other");
    // L'ancienne affirmation, celle que rien ne mesurait, a disparu.
    expect(done).not.toHaveTextContent("It applies at the next read");
  });

  it("dit que la liste ne la porte plus, quand la relecture le montre", async () => {
    const done = await writeIt(async () => reading([]));

    expect(done).toHaveTextContent("Written, and the list no longer carries it");
    expect(done).toHaveTextContent("it no longer carries these values");
    expect(done).not.toHaveTextContent("It applies at the next read");
  });

  it("dit INCONNU quand la relecture n'a pas eu lieu, et jamais reussi", async () => {
    const done = await writeIt(async () => null);

    expect(done).toHaveTextContent("Written, and the list could not be read again");
    expect(done).toHaveTextContent("This is not a repair that landed");
  });

  it("dit INCONNU quand la liste relue s'est arretee a sa borne", async () => {
    /** Une liste tronquee ne distingue pas « la valeur a disparu » de « la
     *  valeur n'a pas ete regardee » : lire cela comme repare serait le vert
     *  fabrique que toute cette surface refuse. */
    const done = await writeIt(async () => ({
      ...PAYLOAD,
      groups: [{ ...GROUP, truncated: true, values: [] }],
    }));

    expect(done).toHaveTextContent("could not be read again");
  });

  it("n'offre plus de retraiter la fenetre : le bouton inerte est retire", async () => {
    /**
     * `Reprocess the window` ne posait aucun geste -- il ne faisait que changer
     * un etat local. Et le cabler serait pire : un rejeu ne peut pas faire
     * resoudre a une lecture un magasin qu'elle ne consulte pas.
     */
    const done = await writeIt(async () => reading(["FR - Paris"]));

    expect(screen.queryByTestId("repair-reprocess")).not.toBeInTheDocument();
    expect(done).not.toHaveTextContent("Reprocess");
  });

  it("creates a destination HERE rather than sending the person elsewhere", async () => {
    /** « Un écran qui renvoie ailleurs pour l'action qu'il réclame est
     *  inachevé » — et choisir une destination est l'action que ce tiroir
     *  réclame. */
    serve([[/reach$/, ok(reach())]]);
    const created = vi.fn(async () => TABLES[0]);
    draw({ onCreateTable: created });

    await userEvent.click(screen.getByTestId("repair-create-inline"));
    await userEvent.type(screen.getByTestId("repair-new-table-name"), "Country vocabulary");
    await userEvent.click(screen.getByTestId("repair-create-table"));

    await waitFor(() => expect(created).toHaveBeenCalledWith("Country vocabulary"));
  });

  it("says nothing was written when the write refuses", async () => {
    serve([
      [/reach$/, ok(reach())],
      [/import\/preview$/, ok(preview())],
      [/import$/, fail(409, { code: "conflict", message: "This table is locked." })],
    ]);
    draw();

    await screen.findByTestId("repair-reach");
    await userEvent.selectOptions(screen.getByTestId("repair-destination"), TABLE);
    await userEvent.type(screen.getByTestId("repair-canonical"), "France");
    await userEvent.click(screen.getByTestId("repair-check"));
    await screen.findByTestId("repair-impact");
    await userEvent.click(screen.getByTestId("repair-confirm"));

    expect(await screen.findByTestId("repair-broken")).toHaveTextContent("Nothing was written");
    expect(screen.queryByTestId("repair-done")).not.toBeInTheDocument();
  });

  it("names the S4 destination it does NOT serve rather than leaving it absent", async () => {
    serve([[/reach$/, ok(reach())]]);
    draw();

    expect(screen.getByTestId("repair-other-destination")).toHaveTextContent(
      "Filling the mapping file behind a Template is not built here.",
    );
  });
});
