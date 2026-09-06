import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SkillEditorDrawer, {
  parseMdmTags,
  parseToolBindings,
  type SkillEditorSaveValue,
} from "../connaissances/SkillEditorDrawer";
import { serializeSkillFrontmatter } from "../connaissances/SkillStepList";
import type { ContextHubContentContext } from "../connaissances/ContextHubLayout";

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  }));
}

const domain = {
  id: "bdm_marketing",
  slug: "marketing",
  name: "Marketing",
  description: "Demand and customer growth",
  owner: null,
  status: "active" as const,
  version_number: 3,
  current_version_id: "mdver_marketing_3",
};

const classification = {
  id: "bcl_campaign",
  domain_id: domain.id,
  parent_id: null,
  classification_type: "operating_model",
  slug: "campaign-operations",
  name: "Campaign Operations",
  description: "Campaign planning and execution",
  owner: null,
  status: "active" as const,
  version_number: 2,
  current_version_id: "mdver_campaign_2",
};

const businessContext: ContextHubContentContext = {
  graph: null,
  selected: { type: "business_domain", id: domain.id },
  selectedNode: domain,
  domains: [domain],
  classifications: [classification],
  links: [],
  selectedLinks: [],
  selectTaxonomy: vi.fn(),
  refresh: vi.fn(async () => undefined),
};

beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = String(input);
    if (url.startsWith("/api/context/skill-tools")) {
      return response({
        catalog_version: "a".repeat(64),
        tools: [
          {
            name: "get_daily_report",
            description: "Read the governed daily KPI report.",
            profile: "insights",
            effect: "read",
            data_class: "operational",
            confirmation_mode: "none",
          },
          {
            name: "get_kpi_movers",
            description: "Explain the largest KPI movements.",
            profile: "insights",
            effect: "read",
            data_class: "operational",
            confirmation_mode: "none",
          },
        ],
      });
    }
    if (url.startsWith("/api/datamodel/fields")) {
      return response([
        {
          name: "spend",
          display_name: "Spend",
          field_kind: "metric",
          description: "Canonical media spend",
          status: "approved",
        },
        {
          name: "conversions",
          display_name: "Conversions",
          field_kind: "metric",
          description: "Canonical conversions",
          status: "approved",
        },
      ]);
    }
    return response({ code: "not_found", message: "Unexpected request" }, 404);
  });
});

afterEach(() => vi.restoreAllMocks());

test("builds ordered steps from live tools, MDM metrics and client taxonomy", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn<(value: SkillEditorSaveValue) => void>();
  render(
    <SkillEditorDrawer
      projectId="p1"
      businessContext={businessContext}
      saving={false}
      error={null}
      onClose={vi.fn()}
      onSave={onSave}
      initial={{
        mode: "create",
        procedureId: null,
        name: "Campaign pacing review",
        description: "Review paid-media pacing against governed targets.",
        owner: "growth@example.com",
        frontmatterYaml: "owner_team: growth-ops\n",
        bodyMd: "Review pacing against the approved daily budget.",
      }}
    />,
  );

  await screen.findByRole("option", { name: "get_daily_report" });
  await screen.findByRole("checkbox", { name: /Spend/ });

  await user.selectOptions(screen.getByLabelText("Add business key"), "business_classification:bcl_campaign");
  await user.click(screen.getByRole("checkbox", { name: /Spend/ }));
  await user.selectOptions(screen.getByLabelText("Server tool"), "get_daily_report");
  await user.type(screen.getByLabelText("Visualization tag"), "card_kpi_hero");
  await user.click(screen.getByRole("button", { name: "+ Add step" }));

  const instructions = screen.getAllByLabelText("Instruction");
  await user.type(instructions[1], "Explain the largest movement against target.");
  const toolSelectors = screen.getAllByLabelText("Server tool");
  await user.selectOptions(toolSelectors[1], "get_kpi_movers");
  await user.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
  expect(onSave.mock.calls[0][0]).toMatchObject({
    name: "Campaign pacing review",
    description: "Review paid-media pacing against governed targets.",
    owner: "growth@example.com",
    toolBindings: [
      { step: 1, tool: "get_daily_report", viz_tag: "card_kpi_hero" },
      { step: 2, tool: "get_kpi_movers" },
    ],
    mdmTags: ["spend"],
    businessLink: {
      taxonomy_type: "business_classification",
      taxonomy_id: "bcl_campaign",
      relation_type: "applies_to",
    },
  });
  expect(onSave.mock.calls[0][0].bodyMd).toBe(
    "## Step 1\n\nReview pacing against the approved daily budget.\n\n" +
      "## Step 2\n\nExplain the largest movement against target.\n",
  );
});

/**
 * AI-210 — la console pouvait RENDRE une sequence standardisee et ne pouvait pas
 * l'ECRIRE. Les cinq cles validees par `core/context_store.py` (`steps`,
 * `acceptance`, `common_errors`, `keywords`, `evidence_requirements`) n'avaient
 * aucun champ de saisie : une Skill creee ici ne pouvait pas etre standardisee.
 */
test("a step is standardized in the console — action, summary, what it acts on", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn<(value: SkillEditorSaveValue) => void>();
  render(
    <SkillEditorDrawer
      projectId="p1"
      saving={false}
      error={null}
      onClose={vi.fn()}
      onSave={onSave}
      initial={{
        mode: "create",
        procedureId: null,
        name: "Local gate",
        description: "Run the console gate before claiming a screen is done.",
        owner: "growth@example.com",
        frontmatterYaml: "",
        bodyMd: "Run the suite.",
      }}
    />,
  );

  await user.selectOptions(screen.getByLabelText("Step 1 action"), "run");
  await user.type(screen.getByLabelText("Step 1 summary"), "Run the console suite");
  await user.type(screen.getByLabelText("Step 1 command"), "npx vitest run");
  await user.type(screen.getByLabelText("Step 1 stop condition"), "any test is red");

  await user.click(screen.getByRole("button", { name: "+ Add acceptance" }));
  await user.type(screen.getByLabelText("Acceptance 1"), "The suite exits 0");
  await user.click(screen.getByRole("button", { name: "+ Add keywords" }));
  await user.type(screen.getByLabelText("Keywords 1"), "console");
  await user.click(screen.getByRole("button", { name: "+ Add error" }));
  await user.type(screen.getByLabelText("Common error 1 symptom"), "Connection refused");
  await user.type(screen.getByLabelText("Common error 1 cause 1"), "The MCP server is not running");

  // L'apercu lit l'etat en cours : la sequence est rendue AVANT l'enregistrement.
  expect(screen.getByTestId("skill-step-1")).toHaveTextContent("Run the console suite");

  await user.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
  expect(onSave.mock.calls[0][0].standard).toEqual({
    steps: [{
      step: 1,
      action: "run",
      label: "Run the console suite",
      command: "npx vitest run",
      stop_if: "any test is red",
    }],
    acceptance: ["The suite exits 0"],
    commonErrors: [{ symptom: "Connection refused", causes: ["The MCP server is not running"] }],
    keywords: ["console"],
    evidenceRequirements: [],
    antiTriggers: [],
  });
});

test("an incomplete step is refused at the keyboard, not by a 422", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn<(value: SkillEditorSaveValue) => void>();
  render(
    <SkillEditorDrawer
      projectId="p1"
      saving={false}
      error={null}
      onClose={vi.fn()}
      onSave={onSave}
      initial={{
        mode: "create",
        procedureId: null,
        name: "Local gate",
        description: "Run the console gate.",
        owner: "growth@example.com",
        frontmatterYaml: "",
        bodyMd: "Run the suite.",
      }}
    />,
  );

  await user.selectOptions(screen.getByLabelText("Step 1 action"), "run");
  expect(screen.getByTestId("skill-step-refusal-1")).toHaveTextContent("write a one-line summary");
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

  await user.type(screen.getByLabelText("Step 1 summary"), "Run the console suite");
  expect(screen.getByTestId("skill-step-refusal-1")).toHaveTextContent("name a target, a command or a tool");
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

  await user.type(screen.getByLabelText("Step 1 target"), "ui/admin");
  expect(screen.queryByTestId("skill-step-refusal-1")).toBeNull();
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
});

test("an existing sequence is loaded into the fields, and survives its own round trip", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn<(value: SkillEditorSaveValue) => void>();
  // Un libelle qui porte deux-points ET une guillemet : la forme exacte qu'un
  // scalaire YAML naif casse, et que l'aller-retour doit rendre identique.
  const label = 'Read the "Incomplete if" section: every criterion';
  const frontmatterYaml = serializeSkillFrontmatter({
    steps: [{ step: 1, action: "read", label, target: "docs/product-architecture" }],
    acceptance: ["Every criterion is quoted"],
    commonErrors: [],
    keywords: [],
    evidenceRequirements: ["The file and its line"],
    antiTriggers: ["billing"],
  }).join("\n");

  render(
    <SkillEditorDrawer
      projectId="p1"
      saving={false}
      error={null}
      onClose={vi.fn()}
      onSave={onSave}
      initial={{
        mode: "edit",
        procedureId: "proc_1",
        name: "Read the target",
        description: "Read the ratified surface before touching a screen.",
        owner: "growth@example.com",
        frontmatterYaml,
        bodyMd: "## Step 1\n\nOpen the surface document.\n",
      }}
    />,
  );

  expect(screen.getByLabelText("Step 1 summary")).toHaveValue(label);
  expect(screen.getByLabelText("Step 1 target")).toHaveValue("docs/product-architecture");
  expect(screen.getByLabelText("Acceptance 1")).toHaveValue("Every criterion is quoted");
  expect(screen.getByLabelText("Evidence requirements 1")).toHaveValue("The file and its line");
  // Story 45.5 : le seul champ qui RETIRE la Skill d'une question se saisit ici
  // comme les autres, et revient de son propre aller-retour.
  expect(screen.getByLabelText("Do not trigger on 1")).toHaveValue("billing");

  await user.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
  expect(serializeSkillFrontmatter(onSave.mock.calls[0][0].standard).join("\n")).toBe(frontmatterYaml);
});

test("parses existing block-style bindings and MDM tags without losing step numbers", () => {
  const raw = `name: skill\ndescription: governed\ntool_bindings:\n  - step: 1\n    tool: get_daily_report\n    viz_tag: card_kpi_hero\n  - step: 3\n    tool: get_kpi_movers\nmdm_tags:\n  - spend\n  - conversions\n`;

  expect(parseToolBindings(raw)).toEqual([
    { step: 1, tool: "get_daily_report", viz_tag: "card_kpi_hero" },
    { step: 3, tool: "get_kpi_movers" },
  ]);
  expect(parseMdmTags(raw)).toEqual(["spend", "conversions"]);
  expect(parseToolBindings('tool_bindings: ["invalid"]')).toEqual([]);
});

/**
 * Escape et le clic sur le fond FERMAIENT LE TIROIR SANS RIEN DEMANDER. Un
 * runbook entier — la sequence, les criteres, les erreurs courantes — partait
 * sur une touche, et rien ne le gardait ailleurs.
 */
describe("SkillEditorDrawer — an edited drawer does not close in silence", () => {
  function renderDrawer(onClose: () => void) {
    return render(
      <SkillEditorDrawer
        projectId="p1"
        saving={false}
        error={null}
        onClose={onClose}
        onSave={vi.fn()}
        initial={{
          mode: "edit",
          procedureId: "proc_1",
          name: "Campaign pacing review",
          description: "Review paid-media pacing against governed targets.",
          owner: "growth@example.com",
          frontmatterYaml: "name: \"Campaign pacing review\"\ndescription: \"Review pacing\"\n",
          bodyMd: "## Step 1\n\nReview pacing against the approved daily budget.\n",
        }}
      />,
    );
  }

  it("asks before discarding, and keeps the draft when the question is cancelled", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderDrawer(onClose);
    await screen.findByRole("option", { name: "get_daily_report" });

    await user.type(screen.getByLabelText("Step 1 summary"), "Check pacing");
    await user.keyboard("{Escape}");

    const dialog = await screen.findByTestId("skill-editor-discard-confirm");
    expect(dialog).toHaveTextContent(/have not been saved/i);
    expect(onClose).not.toHaveBeenCalled();

    await user.click(screen.getByTestId("skill-editor-discard-cancel"));
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Step 1 summary")).toHaveValue("Check pacing");
  });

  it("closes on Escape when nothing has been edited", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderDrawer(onClose);
    await screen.findByRole("option", { name: "get_daily_report" });

    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledOnce();
    expect(screen.queryByTestId("skill-editor-discard-confirm")).not.toBeInTheDocument();
  });

  it("closes once the discard is accepted", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderDrawer(onClose);
    await screen.findByRole("option", { name: "get_daily_report" });

    await user.type(screen.getByLabelText("Step 1 summary"), "Check pacing");
    await user.keyboard("{Escape}");
    await user.click(await screen.findByTestId("skill-editor-discard-accept"));

    expect(onClose).toHaveBeenCalledOnce();
  });

  it("asks the same question when the backdrop is clicked", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const { container } = renderDrawer(onClose);
    await screen.findByRole("option", { name: "get_daily_report" });

    await user.click(screen.getByRole("button", { name: "+ Add step" }));
    await user.click(container.querySelector("[role='presentation']") as HTMLElement);

    expect(await screen.findByTestId("skill-editor-discard-confirm")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});
