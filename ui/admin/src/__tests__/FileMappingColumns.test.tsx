/**
 * Story 60.6 — 46 columns arrive, 35 are useless, and all 46 stay on screen.
 *
 * WHAT THIS FILE PINS. A person opens a spreadsheet-fed Datastream and has to
 * answer one question per column: what does this become? Until now the Mapping
 * tab answered it with a `Yes`/`Excluded` cell computed from a boolean nothing
 * else in the platform read (`field.included`), showed no example value at all,
 * and offered exactly one way to exclude a column — hand-editing a JSON contract
 * in a textarea.
 *
 * THE REFUSAL IS THE POINT. "Refuse : cacher les colonnes écartées. 35 colonnes
 * sur 46 ne servent à rien et restent listées — leur absence serait l'inventaire
 * perdu de ce qui reste à décider" (epic-60:136-138). A filtered-down table
 * would pass a naive test and lose the inventory.
 *
 * And "vide" and "cassé" are two sentences with no word in common: a version
 * that binds nothing is not a reading that failed.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WorkbenchMappingPage from "../datastreams/workbench/pages/WorkbenchMappingPage";
import type { WorkbenchHeader, WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const TOTAL_COLUMNS = 46;
const EXCLUDED_COLUMNS = 35;

function binding(status: string, canonical: string | null) {
  return {
    canonical_target: canonical,
    mdm_target: null,
    status,
    blocking_reason: null,
    confirmed_by: status === "confirmed" ? "owner@example.com" : null,
    confirmed_reason: status === "confirmed" ? "reviewed in the Mapping tab" : null,
  };
}

/** 46 source columns, 11 bound and 35 excluded — the shape the story describes. */
function fields() {
  return Array.from({ length: TOTAL_COLUMNS }, (_, index) => {
    const kept = index < TOTAL_COLUMNS - EXCLUDED_COLUMNS;
    return {
      field_id: `col_${index}`,
      source_identity: `col_${index}`,
      physical_type: "string",
      semantic_type: "string",
      aggregation: "none",
      sensitivity: "none",
      profile: { sample_values: kept ? [`value ${index}`] : [], confidence: 0.9 },
      binding: binding(kept ? "confirmed" : "excluded", kept ? `concept_${index}` : null),
    };
  });
}

/** What the SERVER computes: one reading per column, the vocabulary included. */
function columns() {
  return fields().map((field, index) => {
    const kept = index < TOTAL_COLUMNS - EXCLUDED_COLUMNS;
    return {
      field_id: field.field_id,
      treatment: kept ? "Direct" : "Excluded",
      canonical_target: field.binding.canonical_target,
      mdm_target: null,
      binding_status: field.binding.status,
      confidence: 0.9,
      sample_value: kept ? `value ${index}` : "no sample value",
      contributes_to: [],
      joined_with: [],
    };
  });
}

function payload(overrides: Record<string, unknown> = {}): WorkbenchTabPayload {
  return {
    evidence: {
      active_version: "mv_live",
      versions: [
        {
          id: "mv_live",
          blocking_count: 0,
          executable: true,
          mapping_payload: { fields: fields(), joint_grain: ["col_0"], grain: ["col_0"] },
          columns: columns(),
          ...overrides,
        },
      ],
      capabilities: [],
    },
  } as unknown as WorkbenchTabPayload;
}

const header = {
  identity: { mode: "managed_feed" },
  versions: {
    active_plan: "pv_1",
    active_mapping: "mv_live",
    proposed_plan: null,
    proposed_mapping: null,
    ready_mapping_proposal: null,
  },
} as unknown as WorkbenchHeader;

function mount(tab: WorkbenchTabPayload = payload(), onConfirmed = () => undefined) {
  return render(
    <WorkbenchMappingPage onRetry={() => {}}
      header={header}
      payload={tab}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      onConfirmed={onConfirmed}
    />,
  );
}

/**
 * The tab now READS the project's canonical vocabulary when the version is
 * editable — amendment 5 of the 2026-08-11 review put the concept selector in
 * the row. A suite that leaves that call unanswered is not testing an offline
 * screen; it is letting a rejected promise settle after the assertions, which
 * React reports as an update outside `act`. An empty catalog is the honest
 * default here: these tests are about columns, not about the vocabulary, and the
 * two files that ARE about it stub their own.
 */
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ fields: [] }) })),
  );
});

afterEach(() => vi.unstubAllGlobals());

/**
 * THE THREE ACTS OF A ROW NOW LIVE IN ONE MENU — finding D-2 of the 2026-08-12
 * visual review. `Inclusion` and `Join / split` were two columns holding three
 * controls, and `Split…` was cut off at the right edge of the frame on every
 * row at 1600px. The gestures below are the same gestures under the same test
 * hooks; they are reached through the row's menu instead of three cells.
 *
 * `pointerEventsCheck: 0`: a Radix menu opens on POINTERDOWN and puts
 * `pointer-events: none` on the body while it is open, which the default check
 * reads as "this element is not clickable".
 */
const user = userEvent.setup({ pointerEventsCheck: 0 });

async function act(id: string, hook: string) {
  await user.click(screen.getByTestId(`acts-${id}`));
  await user.click(await screen.findByTestId(`${hook}-${id}`));
}

describe("Mapping tab — one row per column, the discarded ones included", () => {
  it("lists all 46 columns, and the 35 excluded ones are among them", () => {
    mount();

    const table = within(screen.getByRole("region", { name: /Physical field bindings/i })).getByRole("table");
    // 46 body rows plus the header row.
    expect(within(table).getAllByRole("row")).toHaveLength(TOTAL_COLUMNS + 1);
    expect(screen.getByTestId("treatment-col_45")).toHaveTextContent("Excluded");
    expect(screen.getByTestId("treatment-col_0")).toHaveTextContent("Direct");
  });

  it("renders the treatment word and the example value the server computed", () => {
    mount();

    expect(screen.getByTestId("sample-col_0")).toHaveTextContent("value 0");
    // Never an empty cell, never a `0`: the absence is said in words.
    expect(screen.getByTestId("sample-col_45")).toHaveTextContent("no sample value");
  });

  it("says a joined column keeps its row and names what it was joined with", () => {
    const tab = payload();
    const version = (tab.evidence.versions as Record<string, unknown>[])[0];
    (version.columns as Record<string, unknown>[])[0] = {
      field_id: "col_0",
      treatment: "Joined",
      canonical_target: null,
      mdm_target: null,
      binding_status: "confirmed",
      confidence: 0.9,
      sample_value: "2026",
      contributes_to: ["event_date"],
      joined_with: ["col_1", "col_2"],
    };
    mount(tab);

    const cell = screen.getByTestId("treatment-col_0");
    expect(cell).toHaveTextContent("Joined");
    expect(cell).toHaveTextContent("with col_1, col_2");
  });
});

describe("Mapping tab — excluding and re-including", () => {
  it("names the scope before the act, counting what is being excluded", async () => {
    mount();

    await act("col_0", "toggle-exclusion");

    // The count BEFORE, not the remainder after.
    expect(screen.getByTestId("exclusion-review")).toHaveTextContent(
      "1 of 46 columns stop landing: col_0.",
    );
  });

  it("re-including an excluded column returns it to review, not to a confirmation nobody gave", async () => {
    mount();

    await act("col_45", "toggle-exclusion");

    expect(screen.getByTestId("exclusion-review")).toHaveTextContent(
      "1 column returns to review: col_45.",
    );
  });

  it("sends the exclusion through Prepare mapping change and never through the JSON textarea", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit = {}) => {
        calls.push({ url, init });
        return {
          ok: true,
          status: 201,
          json: async () => ({
            preparation_id: "dscp_1",
            confirmation_secret: "opaque",
            review_hash: "h",
            expires_in_seconds: 900,
            review: {
              diff: [{ path: "$.fields", before_hash: "a", after_hash: "b" }],
              consequence: "Append immutable non-live versions and dispatch one candidate",
              expected_plan_version_id: "pv_1",
              expected_mapping_version_id: "mv_live",
            },
          }),
        };
      }),
    );

    mount();
    await act("col_0", "toggle-exclusion");
    await userEvent.click(screen.getByTestId("prepare-exclusion-change"));

    // The contract is NOT editable text: the person confirms exactly what they
    // clicked, and there is nothing to mistype.
    expect(screen.queryByLabelText("Proposed mapping contract")).not.toBeInTheDocument();
    expect(screen.getByTestId("change-scope")).toHaveTextContent("1 of 46 columns stop landing");

    await userEvent.click(screen.getByTestId("prepare-exact-review"));

    const prepared = calls.find((call) => call.url.endsWith("/mapping/changes"));
    expect(prepared).toBeDefined();
    const sent = JSON.parse(String(prepared!.init.body)) as {
      proposed_payload: { fields: Array<{ field_id: string; binding: { status: string } }> };
    };
    const changed = sent.proposed_payload.fields.find((f) => f.field_id === "col_0");
    expect(changed!.binding.status).toBe("excluded");
    // Exactly one binding moved: an exclusion is not a rewrite of the mapping.
    expect(
      sent.proposed_payload.fields.filter((f) => f.binding.status === "excluded"),
    ).toHaveLength(EXCLUDED_COLUMNS + 1);
  });

  it("offers no exclusion control on a non-live version", async () => {
    const tab = payload();
    (tab.evidence as Record<string, unknown>).active_version = "mv_other";
    mount(tab);

    expect(screen.queryByTestId("acts-col_0")).not.toBeInTheDocument();
    expect(screen.queryByTestId("toggle-exclusion-col_0")).not.toBeInTheDocument();
  });
});

/**
 * The two verbs that were RENDERED and not WRITABLE.
 *
 * `column_treatments` could only ever be composed by hand in the raw JSON
 * contract — the textarea this screen exists to replace — so `Joined` and
 * `Split into N` were words the table could print from a key no person could
 * produce. These follow the gesture all the way to the declaration in the
 * request body; asserting a button exists would prove nothing.
 */
describe("Mapping tab — joining N columns and splitting one into N", () => {
  function capture() {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit = {}) => {
        calls.push({ url, init });
        return {
          ok: true,
          status: 201,
          json: async () => ({
            preparation_id: "dscp_1",
            confirmation_secret: "opaque",
            review_hash: "h",
            expires_in_seconds: 900,
            review: {
              diff: [{ path: "$.column_treatments", before_hash: "", after_hash: "b" }],
              consequence: "Append immutable non-live versions and dispatch one candidate",
              expected_plan_version_id: "pv_1",
              expected_mapping_version_id: "mv_live",
            },
          }),
        };
      }),
    );
    return calls;
  }

  async function sentPayload(calls: Array<{ url: string; init: RequestInit }>) {
    await userEvent.click(screen.getByTestId("prepare-exclusion-change"));
    await userEvent.click(screen.getByTestId("prepare-exact-review"));
    const prepared = calls.find((call) => call.url.endsWith("/mapping/changes"));
    expect(prepared).toBeDefined();
    return (
      JSON.parse(String(prepared!.init.body)) as {
        proposed_payload: {
          column_treatments?: {
            joins: Array<{ target: string; sources: string[]; separator: string }>;
            splits: Array<{
              source: string;
              pattern: string;
              targets: Array<{ group: string; target: string }>;
            }>;
          };
        };
      }
    ).proposed_payload;
  }

  it("writes a join of three columns into the mapping version, sources named", async () => {
    const calls = capture();
    mount();

    await act("col_0", "join-pick");
    await act("col_1", "join-pick");
    await act("col_2", "join-pick");
    await userEvent.type(screen.getByTestId("join-target"), "event_date");
    await userEvent.click(screen.getByTestId("join-add"));

    // The scope is named before the act, and it says the sources SURVIVE.
    expect(screen.getByTestId("exclusion-review")).toHaveTextContent(
      "3 columns join into event_date: col_0, col_1, col_2. They stay listed.",
    );

    const proposed = await sentPayload(calls);
    expect(proposed.column_treatments!.joins).toEqual([
      { target: "event_date", sources: ["col_0", "col_1", "col_2"], separator: "-" },
    ]);
    expect(proposed.column_treatments!.splits).toEqual([]);
  });

  it("refuses a join of a single column, before the click rather than after it", async () => {
    mount();

    await act("col_0", "join-pick");
    await userEvent.type(screen.getByTestId("join-target"), "event_date");

    expect(screen.getByTestId("join-needs-two")).toBeInTheDocument();
    expect(screen.getByTestId("join-add")).toBeDisabled();
  });

  it("refuses a join onto a concept another binding already produces", async () => {
    mount();

    await act("col_0", "join-pick");
    await act("col_1", "join-pick");
    // `concept_2` is the canonical target of `col_2`, which lands.
    await userEvent.type(screen.getByTestId("join-target"), "concept_2");

    expect(screen.getByTestId("join-target-claimed")).toBeInTheDocument();
    expect(screen.getByTestId("join-add")).toBeDisabled();
  });

  it("writes ONE split entry carrying its N targets, never N entries sharing a pattern", async () => {
    const calls = capture();
    mount();

    await act("col_0", "split");
    await userEvent.type(
      screen.getByTestId("split-pattern"),
      "^(?P<country>..)_(?P<theme>.+)$",
    );
    await userEvent.type(screen.getByTestId("split-group-0"), "country");
    await userEvent.type(screen.getByTestId("split-target-0"), "country");
    await userEvent.type(screen.getByTestId("split-group-1"), "theme");
    await userEvent.type(screen.getByTestId("split-target-1"), "campaign_theme");
    await userEvent.click(screen.getByTestId("split-add"));

    expect(screen.getByTestId("exclusion-review")).toHaveTextContent(
      "col_0 splits into 2: country, campaign_theme.",
    );

    const proposed = await sentPayload(calls);
    expect(proposed.column_treatments!.splits).toEqual([
      {
        source: "col_0",
        pattern: "^(?P<country>..)_(?P<theme>.+)$",
        targets: [
          { group: "country", target: "country" },
          { group: "theme", target: "campaign_theme" },
        ],
      },
    ]);
    expect(proposed.column_treatments!.joins).toEqual([]);
  });

  it("refuses a split toward a single concept", async () => {
    mount();

    await act("col_0", "split");
    await userEvent.type(screen.getByTestId("split-pattern"), "^(?P<country>..)");
    await userEvent.type(screen.getByTestId("split-group-0"), "country");
    await userEvent.type(screen.getByTestId("split-target-0"), "country");

    expect(screen.getByTestId("split-needs-two")).toBeInTheDocument();
    expect(screen.getByTestId("split-add")).toBeDisabled();
  });

  it("refuses the JavaScript spelling of a named group, before the append does", async () => {
    // `(?<name>…)` is a hard `re.error` where this pattern is read, so a screen
    // that let it through would compose a declaration the append refuses — a
    // control that looks like it worked. `test_column_treatments.py` holds the
    // same refusal on the other side.
    mount();

    await act("col_0", "split");
    await userEvent.type(screen.getByTestId("split-pattern"), "^(?<country>..)_(?<theme>.+)$");
    await userEvent.type(screen.getByTestId("split-group-0"), "country");
    await userEvent.type(screen.getByTestId("split-target-0"), "country");
    await userEvent.type(screen.getByTestId("split-group-1"), "theme");
    await userEvent.type(screen.getByTestId("split-target-1"), "campaign_theme");

    expect(screen.getByTestId("split-pattern-dialect")).toBeInTheDocument();
    expect(screen.getByTestId("split-add")).toBeDisabled();
  });

  it("refuses a concept sitting on a group the pattern does not capture", async () => {
    mount();

    await act("col_0", "split");
    await userEvent.type(screen.getByTestId("split-pattern"), "^(?P<country>..)_(?P<theme>.+)$");
    await userEvent.type(screen.getByTestId("split-group-0"), "country");
    await userEvent.type(screen.getByTestId("split-target-0"), "country");
    await userEvent.type(screen.getByTestId("split-group-1"), "format");
    await userEvent.type(screen.getByTestId("split-target-1"), "creative_format");

    expect(screen.getByTestId("split-group-missing")).toBeInTheDocument();
    expect(screen.getByTestId("split-add")).toBeDisabled();
  });

  it("offers neither verb on an excluded column, which lands nothing to read", async () => {
    mount();

    // The menu of the excluded row is OPENED and read: an assertion on a closed
    // menu proves nothing, because nothing is mounted until it opens.
    await user.click(screen.getByTestId("acts-col_45"));
    expect(await screen.findByText("Excluded columns feed nothing")).toBeInTheDocument();
    expect(screen.queryByTestId("join-pick-col_45")).not.toBeInTheDocument();
    expect(screen.queryByTestId("split-col_45")).not.toBeInTheDocument();
    // And the re-inclusion is still offered: an excluded column is a decision,
    // not a dead row.
    expect(screen.getByTestId("toggle-exclusion-col_45")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await user.click(screen.getByTestId("acts-col_0"));
    expect(await screen.findByTestId("join-pick-col_0")).toBeInTheDocument();
    expect(screen.getByTestId("split-col_0")).toBeInTheDocument();
  });

  it("a stored schema_splits survives a person adding a join", async () => {
    // The 70.1 regression: `withTreatments` used to rebuild column_treatments as
    // { joins, splits }, so declaring a join through the screen silently dropped
    // a schema_splits the version already carried. It is only reachable by
    // API/MCP today, so the screen must never destroy one it did not compose.
    const storedSplit = {
      name: "placement_code",
      source: "col_0",
      separator: "_",
      remainder: "placement_remainder",
      detection: { method: "anchor_min_offset", anchor_tokens: ["PUBALPHA"] },
      layouts: [{ name: "full", anchor_offset: 1, targets: ["contract", "publisher"] }],
    };
    const calls = capture();
    mount(
      payload({
        mapping_payload: {
          fields: fields(),
          joint_grain: ["col_0"],
          grain: ["col_0"],
          column_treatments: { joins: [], splits: [], schema_splits: [storedSplit] },
        },
      }),
    );

    await act("col_1", "join-pick");
    await act("col_2", "join-pick");
    await userEvent.type(screen.getByTestId("join-target"), "event_date");
    await userEvent.click(screen.getByTestId("join-add"));

    const proposed = (await sentPayload(calls)) as unknown as {
      column_treatments: {
        joins: Array<{ target: string }>;
        schema_splits: Array<{ name: string }>;
      };
    };
    expect(proposed.column_treatments.joins[0].target).toBe("event_date");
    expect(proposed.column_treatments.schema_splits).toHaveLength(1);
    expect(proposed.column_treatments.schema_splits[0].name).toBe("placement_code");
  });

  it("carries an exclusion and a join in ONE prepared change", async () => {
    const calls = capture();
    mount();

    await act("col_10", "toggle-exclusion");
    await act("col_0", "join-pick");
    await act("col_1", "join-pick");
    await userEvent.type(screen.getByTestId("join-target"), "event_date");
    await userEvent.click(screen.getByTestId("join-add"));

    const proposed = (await sentPayload(calls)) as unknown as {
      fields: Array<{ field_id: string; binding: { status: string } }>;
      column_treatments: { joins: Array<{ target: string }> };
    };
    expect(
      proposed.fields.find((field) => field.field_id === "col_10")!.binding.status,
    ).toBe("excluded");
    expect(proposed.column_treatments.joins[0].target).toBe("event_date");
  });

  it("leaves no column_treatments key when nothing declares one", async () => {
    const calls = capture();
    mount();

    await act("col_0", "toggle-exclusion");
    const proposed = await sentPayload(calls);

    // An empty declaration and an absent one are not the same statement.
    expect(proposed.column_treatments).toBeUndefined();
  });

  it("offers no join or split control on a non-live version", () => {
    const tab = payload();
    (tab.evidence as Record<string, unknown>).active_version = "mv_other";
    mount(tab);

    // No menu at all: the three acts share one door, so a non-live version has
    // no door rather than a door onto nothing.
    expect(screen.queryByTestId("acts-col_0")).not.toBeInTheDocument();
    expect(screen.queryByTestId("join-pick-col_0")).not.toBeInTheDocument();
    expect(screen.queryByTestId("split-col_0")).not.toBeInTheDocument();
  });
});

describe("Mapping tab — empty is not broken", () => {
  it("says a version binds no field, without claiming a reading failed", () => {
    const tab = payload();
    const version = (tab.evidence.versions as Record<string, unknown>[])[0];
    version.mapping_payload = { fields: [], joint_grain: [], grain: [] };
    version.columns = [];
    mount(tab);

    expect(screen.getByText(/carries no physical field bindings/i)).toBeInTheDocument();
    expect(screen.queryByTestId("columns-broken")).not.toBeInTheDocument();
  });

  it("says the column mapping could not be read when the server sent no reading", () => {
    const tab = payload();
    delete (tab.evidence.versions as Record<string, unknown>[])[0].columns;
    mount(tab);

    expect(screen.getByTestId("columns-broken")).toHaveTextContent(
      "This is not an absence of treatments.",
    );
    // And the bindings are still shown: a failed reading does not shorten the
    // inventory either.
    const table = within(screen.getByRole("region", { name: /Physical field bindings/i })).getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(TOTAL_COLUMNS + 1);
  });
});

/**
 * FINDING D-1 OF THE 2026-08-12 VISUAL REVIEW — thirteen columns, of which five
 * carried one fact each across ten rendered rows.
 *
 * "Une colonne qui dit la même chose partout n'est pas une colonne, c'est une
 * note de bas de page." What these pin is that the decision is taken from the
 * DATA and nowhere else: the same reading is a column on one version and a
 * footnote on the next, and no list of column names appears anywhere.
 */
describe("Mapping tab — a reading that says the same thing everywhere is a footnote", () => {
  it("drops the uniform readings out of the header and names where they went", () => {
    mount();

    // `Role`, `Semantic type`, `Aggregation` and `Sensitivity` are identical on
    // all 46 columns of this version; `Treatment` and `Example value` are not.
    for (const column of ["Role", "Semantic type", "Aggregation", "Sensitivity"]) {
      expect(screen.queryByRole("columnheader", { name: column })).not.toBeInTheDocument();
    }
    for (const column of ["Source identity", "Treatment", "Canonical / MDM target",
                          "Example value", "Binding", "Acts"]) {
      expect(screen.getByRole("columnheader", { name: column })).toBeInTheDocument();
    }
    // Folded is not gone: the screen says which readings folded and over how
    // many columns they agree.
    expect(screen.getByTestId("folded-readings")).toHaveTextContent(
      "read the same on all 46 columns shown",
    );
    expect(screen.getByTestId("folded-readings")).toHaveTextContent("Sensitivity");
  });

  it("keeps a reading as a column the moment one row disagrees", () => {
    const tab = payload();
    const version = (tab.evidence.versions as Record<string, unknown>[])[0];
    (version.mapping_payload as { fields: Array<Record<string, unknown>> }).fields[3]
      .sensitivity = "restricted";
    mount(tab);

    expect(screen.getByRole("columnheader", { name: "Sensitivity" })).toBeInTheDocument();
    expect(screen.getByTestId("folded-readings")).not.toHaveTextContent("Sensitivity");
  });

  it("puts every folded reading in the row's own detail, and only when it is opened", async () => {
    mount();

    // Closed: 46 body rows and no detail anywhere. A detail row that existed
    // collapsed would be an inventory of 92 lines pretending to be 46.
    const table = within(screen.getByRole("region", { name: /Physical field bindings/i })).getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(TOTAL_COLUMNS + 1);
    expect(screen.queryByTestId("detail-col_0")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("unfold-col_0"));

    const detail = within(screen.getByTestId("detail-col_0"));
    expect(detail.getByText("Role")).toBeInTheDocument();
    expect(detail.getByText("Aggregation")).toBeInTheDocument();
    expect(detail.getByText("Sensitivity")).toBeInTheDocument();
    // The reading keeps its name when it changes place, and carries THIS row's
    // value rather than a sentence about all of them.
    expect(detail.getByText("Binding confidence")).toBeInTheDocument();
    // `90.0 %`, not `90%`: since 76-1 a confidence goes through `formatPercent`
    // — one decimal, a thin no-break space before the sign
    // (`console-presentation.md` §2), the same reading as every other
    // confidence in the console. `\s` matches that space without pinning it.
    expect(screen.getByTestId("detail-col_0")).toHaveTextContent(/90\.0\s*% confidence/);
  });

  it("folds nothing when the triage leaves a single row", () => {
    const tab = payload();
    const fields = (tab.evidence.versions as Record<string, unknown>[])[0]
      .mapping_payload as { fields: Array<Record<string, unknown>> };
    // One blocking column among 46: the triage that isolates it is exactly when
    // its readings are wanted, and "the same on every row" says nothing of one.
    (fields.fields[1].binding as Record<string, unknown>).status = "blocking";
    mount(tab);

    fireEvent.click(within(screen.getByRole("radiogroup", { name: "Field binding triage" }))
      .getByRole("radio", { name: /Blocking/ }));

    expect(screen.queryByTestId("folded-readings")).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Sensitivity" })).toBeInTheDocument();
    expect(screen.queryByTestId("unfold-col_1")).not.toBeInTheDocument();
  });
});
