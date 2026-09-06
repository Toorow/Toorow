/**
 * ONE STORE OF NOUNS, AND A CONTRACT WITHOUT ONE IS REFUSED.
 *
 * `ObjectTypeLabel.test.tsx` beside this file asserted the MECHANISM the day
 * `tracked-entity` got a label: that the crumb and the workbench read one
 * declaration. It asserted it on one type, and its third case asserted the
 * opposite for every other type — `objectTypeLabel("semantic-view")` was
 * expected to be `null`, because at that moment 36 of the 37 delivered contracts
 * declared nothing and the fallback map was still the answer.
 *
 * This file is the ratchet the mechanism was waiting for. `console-presentation.md`
 * §4: *"the registry is the only store"*, *"`OBJECT_TYPE_LABEL` is deleted after
 * every type it named is declared with a `label` in its workspace file"*. So the
 * measurement moves from one type to all of them, and the fallback that made a
 * missing declaration invisible is gone.
 *
 * MEASURED 2026-09-05, before the change: 37 delivered object types, 1 with a
 * label. `OBJECT_TYPE_LABEL` carried 14 more, and the other 22 were sentence-cased
 * by `TopBar.tsx` — which is why the crumb said `Semantic view` while the
 * workbench two lines below said `Semantic View`, and why `context-procedure`
 * read `Context procedure` inside a lens called Skills Registry.
 *
 * WHAT THIS DOES NOT DECIDE. It refuses a MISSING noun and two types sharing one
 * spelling for different objects; it does not judge which word is right. That is
 * `glossary.md`'s job, and `glossary.test.ts` next door asserts the four concepts
 * where the two documents overlap.
 */
import { render, screen } from "@testing-library/react";

import { objectTypeLabel, WORKSPACES } from "../shell/navigation";
import { ObjectTypeName } from "../shell/ObjectTypeName";
import {
  RELATIONSHIP_ENDPOINT_LABELS, RELATIONSHIP_ENDPOINT_TYPES, registryTypeOfEndpoint,
} from "../connaissances/relationshipVocabulary";
import { NODE_TYPE_LABELS, NODE_TYPE_ORDER } from "../KnowledgeGraphPage";
import { objectLabel, UNKNOWN_OBJECT_TYPE } from "../governance/contracts";
import type { ObjectRouteContract } from "../shell/navigation/vocabulary";

/** Every object contract the console delivers, with the address that owns it. */
function contracts(): { where: string; contract: ObjectRouteContract }[] {
  const out: { where: string; contract: ObjectRouteContract }[] = [];
  for (const workspace of WORKSPACES) {
    for (const section of workspace.subnav) {
      for (const contract of section.objects) {
        out.push({ where: `${workspace.key}/${section.slug}`, contract });
      }
    }
  }
  return out;
}

describe("every object type the console opens declares its noun", () => {
  it("reads the whole registry, not one workspace", () => {
    // The count is the one `scripts/object_coverage_audit.py` derives, asserted
    // as a lower bound: adding a workspace must not silently narrow this read.
    expect(contracts().length).toBeGreaterThanOrEqual(37);
    // FIVE workspaces, not six, and that is the measurement rather than a
    // rounding: `overview` declares sections and no object contract at all —
    // nothing under it is addressable as `/object/:type/:id`. Asserting six
    // would have been asserting a workspace that has nothing to declare.
    expect(new Set(contracts().map((entry) => entry.where.split("/")[0]))).toEqual(
      new Set(["analyze", "test", "data", "governance", "context-hub"]),
    );
  });

  it("declares a non-empty label on every contract", () => {
    const missing = contracts()
      .filter((entry) => !entry.contract.label?.trim())
      .map((entry) => `${entry.where}: ${entry.contract.type} declares no label`);
    expect(missing).toEqual([]);
  });

  it("never spells two different types the same way", () => {
    // `context-procedure` and `skill` are the ONE exception, and it is declared:
    // they are one object under two tokens, kept apart only until the
    // `context_procedure` → Skill migration lands (`alignment-register.md` item
    // 4). Two OTHER types sharing a word would be the two-stores defect again,
    // one level down.
    const byLabel = new Map<string, string[]>();
    for (const { contract } of contracts()) {
      const seen = byLabel.get(contract.label ?? "") ?? [];
      seen.push(contract.type);
      byLabel.set(contract.label ?? "", seen);
    }
    const shared = [...byLabel.entries()]
      .filter(([, types]) => types.length > 1)
      .map(([label, types]) => `${label}: ${types.sort().join(", ")}`);
    expect(shared).toEqual(["Skill: context-procedure, skill"]);
  });

  it("resolves the double spellings §4 names, behind the ratified word", () => {
    expect(objectTypeLabel("tracked-entity")).toBe("Competitor");
    expect(objectTypeLabel("context-procedure")).toBe("Skill");
    expect(objectTypeLabel("skill")).toBe("Skill");
    // And the third Context Hub noun `glossary.md` ratifies, which the crumb
    // used to sentence-case out of its token.
    expect(objectTypeLabel("context-topic")).toBe("Knowledge");
    expect(objectTypeLabel("ai-path")).toBe("AI Path");
  });

  it("gives one answer to the crumb and to the workbench", () => {
    for (const { contract } of contracts()) {
      expect(objectLabel(contract.type)).toBe(objectTypeLabel(contract.type));
    }
  });

  it("never prints an undeclared type's token as if it were a word", () => {
    expect(objectTypeLabel("nonexistent-type")).toBeNull();
    expect(objectLabel("nonexistent-type")).toBe(UNKNOWN_OBJECT_TYPE);
    expect(objectLabel("nonexistent-type")).not.toContain("nonexistent-type");
  });
});

describe("an undeclared type renders as a defect, not as a word", () => {
  it("says `Unknown object type` AND keeps the token, in monospace beside it", () => {
    render(<ObjectTypeName objectType="nonexistent-type" />);
    expect(screen.getByText(UNKNOWN_OBJECT_TYPE, { exact: false })).toBeInTheDocument();
    // The token survives — through `ObjectId`, the console's only rendering of
    // an identifier — so the missing declaration is attributable to a type.
    const token = screen.getByTitle("Object type: nonexistent-type");
    expect(token).toHaveTextContent("nonexistent-type");
    expect(token.className).toContain("font-mono");
  });

  it("renders a declared type as the word alone, with no token beside it", () => {
    render(<ObjectTypeName objectType="semantic-view" />);
    expect(screen.getByText("Semantic View")).toBeInTheDocument();
    expect(screen.queryByText("semantic-view")).toBeNull();
    expect(screen.queryByText(UNKNOWN_OBJECT_TYPE, { exact: false })).toBeNull();
  });
});

describe("the other two vocabularies read the registry, they do not answer beside it", () => {
  // MEASURED 2026-09-05: `RELATIONSHIP_ENDPOINT_LABELS` said `Semantic view`,
  // `Master data object` and `Knowledge item` where the registry said
  // `Semantic View`, `Master Data Object` and `Knowledge` — a THIRD store of the
  // same nouns, one screen away from the first two, and the mindmap's
  // `NODE_TYPE_LABELS` spread it and added `Semantic concept` and
  // `Business domain` on top. Both now derive.

  it("spells every relationship endpoint the way the registry does", () => {
    const disagreements = RELATIONSHIP_ENDPOINT_TYPES
      .map((type) => [type, objectTypeLabel(registryTypeOfEndpoint(type))] as const)
      .filter(([type, declared]) => declared !== null && RELATIONSHIP_ENDPOINT_LABELS[type] !== declared)
      .map(([type, declared]) => `${type}: ${RELATIONSHIP_ENDPOINT_LABELS[type]} vs ${declared}`);
    expect(disagreements).toEqual([]);
    // And the five that ARE object types really do come from the registry —
    // asserted by value, so a future edit that pastes a spelling back in fails.
    expect(RELATIONSHIP_ENDPOINT_LABELS.semantic_view).toBe("Semantic View");
    expect(RELATIONSHIP_ENDPOINT_LABELS.master_data_node).toBe("Master Data Object");
    expect(RELATIONSHIP_ENDPOINT_LABELS.topic).toBe("Knowledge");
    expect(RELATIONSHIP_ENDPOINT_LABELS.procedure).toBe("Skill");
    expect(RELATIONSHIP_ENDPOINT_LABELS.datastream).toBe("Datastream");
  });

  it("keeps the three endpoints the registry cannot name, and says nothing else", () => {
    // No route opens a schema doc, a dictionary field or a Concept measure, so
    // no route contract could carry their noun. They stay local, and this pins
    // the list so a fourth one cannot join them quietly.
    const local = RELATIONSHIP_ENDPOINT_TYPES
      .filter((type) => objectTypeLabel(registryTypeOfEndpoint(type)) === null);
    expect([...local].sort()).toEqual(["metric", "schema_doc", "target_field"]);
  });

  it("spells every mindmap node the way the registry does", () => {
    const disagreements = NODE_TYPE_ORDER
      .map((type) => [type, objectTypeLabel(registryTypeOfEndpoint(type))] as const)
      .filter(([type, declared]) => declared !== null && NODE_TYPE_LABELS[type] !== declared)
      .map(([type, declared]) => `${type}: ${NODE_TYPE_LABELS[type]} vs ${declared}`);
    expect(disagreements).toEqual([]);
    expect(NODE_TYPE_LABELS.canonical_field).toBe("Canonical Field");
    expect(NODE_TYPE_LABELS.business_domain).toBe("Business Domain");
    expect(NODE_TYPE_LABELS.semantic_concept).toBe("Semantic Concept");
    // The two the mindmap owns alone, because no route opens them either.
    expect(NODE_TYPE_LABELS.business_classification).toBe("Business layer");
    expect(NODE_TYPE_LABELS.report_view).toBe("Report view");
  });
});
