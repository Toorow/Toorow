/**
 * Vitest tests for FileSourceOnboardingReview (Story 22.19).
 *
 * Coverage:
 *   - Renders per-field mapping with confidence + the placement + gate status.
 *   - A passing gate enables Confirm; clicking locks (onConfirm).
 *   - A flagged gate (missing required / flagged field) disables Confirm and
 *     surfaces the offending fields (low confidence / missing required blocks lock).
 *   - Resolving a flagged field via the select calls onResolveField(source, id).
 *
 * English copy (project-context: all admin copy is English); WCAG: semantic table,
 * textual state, aria-live gate region.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import FileSourceOnboardingReview, {
  type Ambiguity,
  type GateStatus,
  type MappedField,
  type Placement,
} from "./FileSourceOnboardingReview";

// AD-35 : plus de ThemeProvider MUI. Le composant se compose depuis `../../ui`,
// dont les primitives lisent les tokens par CSS, sans fournisseur de theme.
function renderWithTheme(ui: React.ReactElement) {
  return render(ui);
}

const PLACEMENT: Placement = {
  class: "planned",
  metric: "mdm_net_cost",
  period: "mdm_media_date",
  dimension: ["mdm_channel"],
};

const CANONICAL = ["mdm_net_cost", "mdm_media_date", "mdm_channel", "mdm_impressions"];

const PASSING_FIELDS: MappedField[] = [
  { source_column: "Bruttokosten Gesamt", canonical_target: "mdm_net_cost", confidence: 0.91, status: "matched" },
  { source_column: "Datum", canonical_target: "mdm_media_date", confidence: 0.88, status: "matched" },
  { source_column: "Notiz", canonical_target: null, confidence: 0.1, status: "unmatched" },
];
const PASSING_GATE: GateStatus = { passed: true, missing_required: [], flagged: [] };

const FLAGGED_FIELDS: MappedField[] = [
  { source_column: "Bruttokosten Gesamt", canonical_target: "mdm_net_cost", confidence: 0.91, status: "matched" },
  { source_column: "???", canonical_target: null, confidence: 0.4, status: "flagged" },
];
const FLAGGED_GATE: GateStatus = {
  passed: false,
  missing_required: ["mdm_media_date"],
  flagged: [{ source_column: "???", blocking_reason: "low_confidence" }],
};

describe("FileSourceOnboardingReview — rendering", () => {
  it("shows per-field mapping with confidence and the placement", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
      />,
    );
    // Placement.
    expect(screen.getByTestId("placement-class")).toHaveTextContent(/planned/i);
    expect(screen.getByTestId("placement-summary")).toHaveTextContent(/mdm_net_cost/);
    // Per-field mapping + confidence.
    expect(screen.getByTestId("map-row-Bruttokosten Gesamt")).toBeInTheDocument();
    // `91.0 %`, not `91%`: since 76-1 a confidence goes through `formatPercent`
    // — one decimal, a thin no-break space before the sign
    // (`console-presentation.md` §2), the same reading as every other
    // confidence in the console. `\s` matches that space without pinning it.
    expect(screen.getByTestId("confidence-Bruttokosten Gesamt")).toHaveTextContent(/91\.0\s*%/);
    // Extra column is shown but marked ignored/unmatched.
    expect(screen.getByTestId("status-Notiz")).toHaveTextContent(/ignored/i);
  });
});

describe("FileSourceOnboardingReview — gate + confirm", () => {
  it("enables Confirm on a passing gate and locks on click", async () => {
    const onConfirm = vi.fn();
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
        onConfirm={onConfirm}
      />,
    );
    expect(screen.getByTestId("gate-passed")).toBeInTheDocument();
    const confirm = screen.getByTestId("confirm-lock");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("blocks lock on a flagged gate and surfaces the offending fields", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={CANONICAL}
        onConfirm={vi.fn()}
      />,
    );
    expect(screen.getByTestId("confirm-lock")).toBeDisabled();
    expect(screen.getByTestId("confirm-blocked-hint")).toBeInTheDocument();
    expect(screen.getByTestId("gate-missing-required")).toHaveTextContent(/mdm_media_date/);
    expect(screen.getByTestId("gate-flagged-fields")).toHaveTextContent(/\?\?\?/);
  });
});

describe("FileSourceOnboardingReview — resolve a flagged field", () => {
  it("calls onResolveField when the operator picks a canonical target", async () => {
    const onResolveField = vi.fn();
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={CANONICAL}
        onResolveField={onResolveField}
      />,
    );
    const select = screen.getByTestId("resolve-???");
    await userEvent.selectOptions(select, "mdm_media_date");
    expect(onResolveField).toHaveBeenCalledWith("???", "mdm_media_date");
  });
});

describe("FileSourceOnboardingReview — Blocking vs Warning", () => {
  const AMBIGUITIES: Ambiguity[] = [
    { code: "ambiguous_column_match", source_column: "Kosten", candidates: ["mdm_net_cost", "mdm_gross_cost"] },
    { code: "duplicate_target", canonical_target: "mdm_media_date", source_columns: ["Datum", "Tag"] },
  ];

  it("states ambiguities as a warning that does NOT block the lock", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
        ambiguities={AMBIGUITIES}
        onConfirm={vi.fn()}
      />,
    );
    // Le gate passe : une ambiguite est un Warning, pas un Blocking.
    expect(screen.getByTestId("gate-passed")).toBeInTheDocument();
    expect(screen.getByTestId("confirm-lock")).toBeDisabled();
    // Le Warning ne rend pas le gate Blocking, mais exige une decision durable.
    // Et il est DIT, avec ses deux formes.
    const warning = screen.getByTestId("gate-ambiguities");
    expect(warning).toHaveTextContent(/Kosten matched mdm_net_cost or mdm_gross_cost/);
    expect(warning).toHaveTextContent(/Datum and Tag both claim mdm_media_date/);
  });

  it("records explicit warning acceptance and its reason", async () => {
    const onAcceptance = vi.fn();
    const onReason = vi.fn();
    const { rerender } = renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
        ambiguities={AMBIGUITIES}
        onConfirm={vi.fn()}
        warningsAccepted={false}
        warningReason=""
        onWarningAcceptanceChange={onAcceptance}
        onWarningReasonChange={onReason}
      />,
    );
    await userEvent.click(screen.getByTestId("accept-warnings"));
    expect(onAcceptance).toHaveBeenCalledWith(true);

    rerender(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
        ambiguities={AMBIGUITIES}
        onConfirm={vi.fn()}
        warningsAccepted
        warningReason="Reviewed against the vendor contract"
        onWarningAcceptanceChange={onAcceptance}
        onWarningReasonChange={onReason}
      />,
    );
    expect(screen.getByTestId("warning-reason")).toHaveValue(
      "Reviewed against the vendor contract",
    );
    expect(screen.getByTestId("confirm-lock")).toBeEnabled();
  });

  it("says nothing about warnings when there are none", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
      />,
    );
    expect(screen.queryByTestId("gate-ambiguities")).toBeNull();
  });

  it("names the blocking gate as Blocking, both lists together", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={CANONICAL}
      />,
    );
    // `missing_required` ET `flagged` sont tous deux Blocking : le gate ne
    // contient de `flagged` que des champs REQUIS a binding bloquant.
    expect(screen.getByTestId("gate-flagged")).toHaveTextContent(/Blocking/);
    expect(screen.getByTestId("gate-flagged")).toHaveTextContent(/2 field\(s\)/);
  });
});

describe("FileSourceOnboardingReview — pas de contrôle mort", () => {
  it("n'affiche AUCUN contrôle quand aucun gestionnaire n'est fourni", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={CANONICAL}
      />,
    );
    // Le panneau de revue d'échantillon ne passe ni onResolveField ni onConfirm :
    // un `select` et un bouton s'y affichaient et ne faisaient RIEN.
    expect(screen.queryByTestId("resolve-???")).toBeNull();
    expect(screen.queryByTestId("confirm-lock")).toBeNull();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    // L'état reste lisible : c'est une revue, pas un formulaire.
    expect(screen.getByTestId("status-???")).toHaveTextContent(/Flagged/);
    expect(screen.getByTestId("gate-flagged")).toHaveTextContent(/Blocking/);
  });

  it("affiche le select dès qu'une résolution est possible", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={CANONICAL}
        onResolveField={vi.fn()}
      />,
    );
    expect(screen.getByTestId("resolve-???")).toBeInTheDocument();
    // Mais toujours pas de verrouillage : les deux gestionnaires sont distincts.
    expect(screen.queryByTestId("confirm-lock")).toBeNull();
  });
});

describe("FileSourceOnboardingReview — authoritative gate", () => {
  it("allows sending completed resolutions without fabricating a passed gate", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={CANONICAL}
        onResolveField={vi.fn()}
        onConfirm={vi.fn()}
        canSubmitResolutions
      />,
    );
    expect(screen.getByTestId("confirm-lock")).toBeEnabled();
    expect(screen.getByTestId("gate-flagged")).toBeInTheDocument();
    expect(screen.queryByTestId("gate-passed")).toBeNull();
    expect(screen.getByTestId("resolve-Bruttokosten Gesamt")).toBeInTheDocument();
  });
});
/**
 * AI-248 — le vocabulaire canonique peut etre VIDE, et l'ecran doit le dire.
 *
 * Mesure du 2026-08-08 : `app.mdm_canonical_fields` portait 0 ligne aux DEUX
 * portees alors que six modules le lisent. Le select s'affichait donc avec sa
 * seule option vide, `canSubmitResolutions` restait faux pour toujours, et rien
 * n'expliquait pourquoi. Une impasse muette.
 */
describe("FileSourceOnboardingReview — vocabulaire vide (AI-248)", () => {
  it("dit ce qui manque au lieu d'afficher un select sans issue", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={[]}
        onResolveField={() => {}}
      />,
    );
    expect(screen.getByTestId("empty-vocabulary-???")).toHaveTextContent(
      /has declared no canonical field/i,
    );
    // Le controle mort ne doit PAS coexister avec la phrase qui le remplace.
    expect(screen.queryByTestId("resolve-???")).not.toBeInTheDocument();
  });

  it("n'offre la porte que si un gestionnaire existe", async () => {
    // La regle que ce composant s'applique deja a lui-meme : « un controle
    // n'existe que s'il a un gestionnaire ». Un bouton sans `onDeclareField`
    // serait le defaut que son propre commentaire reproche au bouton de Warnings.
    const { unmount } = renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={[]}
        onResolveField={() => {}}
      />,
    );
    expect(screen.queryByTestId("declare-???")).not.toBeInTheDocument();
    unmount();

    const onDeclareField = vi.fn();
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={[]}
        onResolveField={() => {}}
        onDeclareField={onDeclareField}
      />,
    );
    await userEvent.click(screen.getByTestId("declare-???"));
    expect(onDeclareField).toHaveBeenCalledWith("???");
  });

  it("garde le select des que le vocabulaire porte au moins un champ", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={CANONICAL}
        onResolveField={() => {}}
        onDeclareField={() => {}}
      />,
    );
    expect(screen.getByTestId("resolve-???")).toBeInTheDocument();
    expect(screen.queryByTestId("empty-vocabulary-???")).not.toBeInTheDocument();
  });
});


/**
 * Story 38.16 AC3 — les lectures que l'apercu RAPPORTE et que cet ecran ne
 * montrait pas.
 *
 * La grille dit sur quel champ chaque colonne atterrit. Elle ne dit pas ce qui
 * arrive aux VALEURS : sous quel format une date sera lue, dans quelle unite un
 * montant sera compte, combien de lignes ne passeront pas. Une personne
 * l'apprenait en important — ce que l'etape 5 ratifiee refuse explicitement.
 */
describe("FileSourceOnboardingReview — ce qui va arriver a ce fichier", () => {
  it("dit combien de lignes atterriraient, et pourquoi les autres non", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
        rowValidation={{
          detected_row_count: 100,
          accepted_row_count: 80,
          rejected_row_count: 20,
          rejected_row_pct: 20,
          by_rule: [
            { rule: "subtotal_or_total", count: 12, first_row_number: 7, fields: [] },
            { rule: "bad_amount", count: 8, first_row_number: 3, fields: ["cost"] },
          ],
        }}
      />,
    );
    const line = screen.getByTestId("row-validation");
    expect(line).toHaveTextContent("80 of 100 row(s) would land");
    expect(line).toHaveTextContent("20% rejected");
    // LE NUMERO DE LIGNE EST CE QU'UNE PERSONNE OUVRE dans son propre fichier.
    expect(line).toHaveTextContent("first at line 7");
    expect(line).toHaveTextContent("12 x subtotal_or_total");
  });

  it("distingue un format DECLARE du defaut de l'analyseur", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
        coercions={[
          { column: "Start", coercion: "date", declared_format: "%d.%m.%Y" },
          { column: "Ende", coercion: "date", declared_format: null },
          { column: "Bruttokosten", coercion: "amount", declared_format: null },
        ]}
      />,
    );
    expect(screen.getByTestId("coercion-Start")).toHaveTextContent("using %d.%m.%Y");
    // Imprimer un defaut ici cacherait la declaration qui manque.
    expect(screen.getByTestId("coercion-Ende")).toHaveTextContent(
      "using the parser default (no format declared)",
    );
    expect(screen.getByTestId("coercion-Bruttokosten")).toHaveTextContent(
      "will be read as amount",
    );
  });

  // Les identifiants sont ceux que le registre FRAPPE (`mdm_<ULID>`), pas des
  // mots lisibles : une fixture lisible la ou la production est opaque rend vraie
  // toute assertion de nom -- c'est exactement ce qui a laisse revenir
  // `mdm_01KZ...` sur le rail du Builder (f3ca3d50).
  const NET_COST = "mdm_01EXAMPLE00000000000001";
  const CHANNEL = "mdm_01EXAMPLE00000000000002";

  it("dit dans quoi chaque champ cible est MESURE, et le NOMME", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
        units={[
          {
            field_id: NET_COST,
            canonical_name: "Net cost",
            concept_kind: "metric",
            unit: "micros",
            currency_scope: "reporting",
            aggregation: "sum",
            non_additive: false,
          },
          {
            field_id: CHANNEL,
            canonical_name: "Channel",
            concept_kind: "dimension",
            unit: null,
            currency_scope: null,
            aggregation: null,
            non_additive: false,
          },
        ]}
      />,
    );
    const cost = screen.getByTestId(`unit-${NET_COST}`);
    expect(cost).toHaveTextContent("Net cost is measured in micros");
    expect(cost).toHaveTextContent("currency scope reporting");
    // L'identifiant canonique n'apparait nulle part dans la phrase.
    expect(cost.textContent ?? "").not.toContain(NET_COST);
    // Une dimension ne porte ni unite ni agregation, et l'ecran le dit tel quel.
    expect(screen.getByTestId(`unit-${CHANNEL}`)).toHaveTextContent(
      "Channel declares no unit",
    );
  });

  it("ne rend aucune ligne d unite que le serveur n a pas nommee", () => {
    // Une enveloppe anterieure a `canonical_name`. La phrase disparait plutot que
    // de se composer autour d un identifiant.
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
        units={[
          {
            field_id: NET_COST,
            concept_kind: "metric",
            unit: "micros",
            currency_scope: "reporting",
            aggregation: "sum",
            non_additive: false,
          },
        ]}
      />,
    );
    expect(screen.queryByTestId(`unit-${NET_COST}`)).toBeNull();
  });

  it("nomme le refus de classification au lieu de rendre des cases vides", () => {
    // Six cases vides se liraient comme « rien de sensible ici ». Le refus se
    // nomme, et c'est la seule reponse honnete tant que le registre des champs
    // canoniques ne porte aucune classification.
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
        classification={{
          vocabulary: ["none", "pii", "financial", "credentials", "internal", "unknown"],
          declared_by: null,
          undeclared_reason: "canonical_field_registry_declares_no_classification",
          fields: [{ field_id: "mdm_net_cost", classification: "unknown" }],
        }}
      />,
    );
    const line = screen.getByTestId("classification");
    expect(line).toHaveTextContent("not declared");
    expect(line).toHaveTextContent("reads unknown");
  });

  it("n'affiche RIEN quand la lecture est absente", () => {
    // Absent ≠ vide. « Aucune ligne rejetee » pour une analyse qui n'a jamais
    // tourne serait la meme invention que le taux de 0 % que le serveur refuse
    // de calculer.
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
      />,
    );
    expect(screen.queryByTestId("ac3-readings")).toBeNull();
    expect(screen.queryByTestId("row-validation")).toBeNull();
  });
});
