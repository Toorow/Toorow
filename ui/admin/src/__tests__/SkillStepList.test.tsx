/**
 * Ce que la console lit d'une Skill -- et ce qu'elle refuse de rendre a moitie.
 *
 * Jean, 2026-08-03 : « faut faire l'adaptation UI + test de la lecture ». Le
 * frontmatter valide par `core/context_store.py` depuis le meme jour porte
 * `steps`, `acceptance` et `common_errors` ; sans lecture ils existaient en base
 * et n'etaient rendus nulle part.
 *
 * LE CONTROLE NEGATIF COMPTE AUTANT QUE LE POSITIF : un pas incomplet doit etre
 * IGNORE, pas affiche a moitie. Le serveur refuse deja ces formes, donc une qui
 * arrive jusqu'ici veut dire que la lecture a derive -- et un rendu partiel le
 * cacherait.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import SkillStepList, {
  parseAcceptance,
  parseCommonErrors,
  parseSkillSteps,
} from "../connaissances/SkillStepList";

const FRONTMATTER = [
  'name: "Write a Skill the console can render"',
  'description: "d"',
  "steps:",
  "  - step: 1",
  '    action: "read"',
  '    label: "Read the SPEC document"',
  '    target: "docs/product-architecture/data.md"',
  '    stop_if: "NO RATIFIED DOCUMENT"',
  "  - step: 2",
  '    action: "run"',
  '    label: "Run the screen test"',
  '    command: "npx vitest run"',
  "  - step: 3",
  '    action: "edit"',
  '    label: "Fix the front file"',
  '    target: "ui/admin/src/shell/pages/Screen.tsx"',
  "acceptance:",
  '  - "Both RUN numbers appear in the answer"',
  "common_errors:",
  '  - symptom: "Connection refused"',
  "    causes:",
  '      - "the MCP server is not running"',
  '      - "the API key is invalid"',
].join("\n");

describe("la lecture du frontmatter", () => {
  it("rend chaque pas avec son action et ce sur quoi il agit", () => {
    expect(parseSkillSteps(FRONTMATTER)).toEqual([
      {
        step: 1,
        action: "read",
        label: "Read the SPEC document",
        target: "docs/product-architecture/data.md",
        stop_if: "NO RATIFIED DOCUMENT",
      },
      { step: 2, action: "run", label: "Run the screen test", command: "npx vitest run" },
      {
        step: 3,
        action: "edit",
        label: "Fix the front file",
        target: "ui/admin/src/shell/pages/Screen.tsx",
      },
    ]);
  });

  it("remet les pas dans l'ordre, quel que soit celui du fichier", () => {
    const shuffled = ["steps:", "  - step: 2", '    action: "run"', '    label: "b"',
      '    command: "x"', "  - step: 1", '    action: "read"', '    label: "a"',
      '    target: "y"'].join("\n");
    expect(parseSkillSteps(shuffled).map((step) => step.step)).toEqual([1, 2]);
  });

  it("IGNORE un pas incomplet plutot que de l'afficher a moitie", () => {
    const broken = ["steps:", "  - step: 1", '    action: "read"',
      "  - step: 2", '    action: "teleport"', '    label: "b"',
      "  - step: 3", '    action: "run"', '    label: "kept"', '    command: "ok"'].join("\n");
    expect(parseSkillSteps(broken)).toEqual([
      { step: 3, action: "run", label: "kept", command: "ok" },
    ]);
  });

  it("lit les criteres d'acceptation et les couples symptome/causes", () => {
    expect(parseAcceptance(FRONTMATTER)).toEqual(["Both RUN numbers appear in the answer"]);
    expect(parseCommonErrors(FRONTMATTER)).toEqual([
      {
        symptom: "Connection refused",
        causes: ["the MCP server is not running", "the API key is invalid"],
      },
    ]);
  });

  it("ne rend pas un symptome sans cause -- c'est un constat, pas une aide", () => {
    const orphan = ["common_errors:", '  - symptom: "Connection refused"', "    causes:"].join("\n");
    expect(parseCommonErrors(orphan)).toEqual([]);
  });

  it("ne lit rien quand le frontmatter ne porte aucune des trois cles", () => {
    const legacy = ['name: "old"', 'description: "d"', "tool_bindings:", "  - step: 1",
      "    tool: get_card"].join("\n");
    expect(parseSkillSteps(legacy)).toEqual([]);
    expect(parseAcceptance(legacy)).toEqual([]);
    expect(parseCommonErrors(legacy)).toEqual([]);
  });
});

describe("le rendu", () => {
  it("affiche la sequence, les criteres et les erreurs courantes", () => {
    render(<SkillStepList frontmatterYaml={FRONTMATTER} />);

    expect(screen.getByText("Sequence — 3 steps")).toBeTruthy();
    expect(screen.getByText("Read the SPEC document")).toBeTruthy();
    expect(screen.getByText("docs/product-architecture/data.md")).toBeTruthy();
    expect(screen.getByText("npx vitest run")).toBeTruthy();

    // La branche d'echec est visible, pas enfouie dans le corps.
    expect(screen.getByText("NO RATIFIED DOCUMENT")).toBeTruthy();

    expect(screen.getByText("Acceptance — 1")).toBeTruthy();
    expect(screen.getByText("Common errors — 1")).toBeTruthy();
    expect(screen.getByText("the API key is invalid")).toBeTruthy();
  });

  it("ne rend rien du tout quand une Skill ne porte aucune des trois cles", () => {
    const { container } = render(<SkillStepList frontmatterYaml={'name: "old"'} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("les références au modèle de données", () => {
  const MDM = {
    inline: {
      resolved: [
        { name: "cost", display_name: "Cost", field_kind: "metric",
          data_type: "currency", measure: "sum", status: "approved" },
      ],
      unresolved: ["produit"],
    },
    tags: { resolved: [], unresolved: [] },
  };

  it("montre un champ résolu avec ce qui le rend compréhensible", () => {
    render(<SkillStepList frontmatterYaml={FRONTMATTER} mdmReferences={MDM} />);
    expect(screen.getByText("Data model — 1 resolved, 1 unresolved")).toBeTruthy();
    expect(screen.getByText("Cost")).toBeTruthy();
    expect(screen.getByText("metric · currency · sum")).toBeTruthy();
  });

  it("MONTRE une référence qui ne résout pas -- l'omettre effacerait la seule trace", () => {
    render(<SkillStepList frontmatterYaml={FRONTMATTER} mdmReferences={MDM} />);
    expect(screen.getByText("{{produit}}")).toBeTruthy();
    expect(screen.getByText("names no governed field")).toBeTruthy();
  });

  it("ne rend rien de plus quand le serveur n'a rien résolu", () => {
    render(<SkillStepList frontmatterYaml={FRONTMATTER} mdmReferences={null} />);
    expect(screen.queryByText(/Data model/)).toBeNull();
  });
});
