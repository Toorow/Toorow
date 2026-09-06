/**
 * The ONE writer of a Skill's frontmatter YAML, shared by every surface that
 * saves a Skill.
 *
 * WHY THIS FILE EXISTS. These two functions lived inside `shell/pages/
 * Procedures.tsx`, the collection screen — the only surface that could save a
 * Skill. The moment a second surface saves one (the object workbench, so that a
 * remark can be *acted on* where it is read — `context-hub.md`, "A remark is
 * answered with a new version, not with a closed note"), copying them would
 * give the product two spellings of one save: two screens emitting different
 * YAML for the same edit, drifting the first time one of them learns a new key.
 * Extracted verbatim, comments included; no behaviour changed in the move.
 *
 * The standardized blocks (`steps`, `acceptance`, …) are serialized by
 * `SkillStepList.serializeSkillFrontmatter`, which stays their owner: this
 * module only decides WHERE they land in the document and what survives around
 * them.
 */
import { serializeSkillFrontmatter, STANDARD_FRONTMATTER_KEYS, type StandardFrontmatter } from "./SkillStepList";

/**
 * Update ONLY the `name` and `description` top-level keys of an existing
 * frontmatter YAML blob, preserving every other line verbatim (Story 44.1
 * review: the server accepts any valid YAML mapping and preserves extra keys
 * across an edit — the client must not silently drop them by rebuilding the
 * frontmatter from just these two fields).
 *
 * This is a minimal flat-key round-trip, not a general YAML parser: it only
 * recognises un-indented `key: value` lines at the top level (which is all
 * `name`/`description` ever are) and leaves every other line — including
 * nested mappings/lists under other keys — untouched and in place. JSON is a
 * valid YAML flow-scalar subset, so the emitted name/description values
 * always round-trip through yaml.safe_load server-side regardless of quotes
 * or newlines in the input.
 */
export function updateFrontmatterYaml(rawYaml: string, name: string, description: string): string {
  const lines = rawYaml.length > 0 ? rawYaml.split(/\r?\n/) : [];
  const out: string[] = [];
  let sawName = false;
  let sawDescription = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const match = /^([A-Za-z0-9_-]+):/.exec(line);
    const key = match?.[1];
    if (key === "name" || key === "description") {
      if (key === "name") {
        out.push(`name: ${JSON.stringify(name)}`);
        sawName = true;
      } else {
        out.push(`description: ${JSON.stringify(description)}`);
        sawDescription = true;
      }
      // Consume the replaced key's continuation lines (block/folded scalars,
      // e.g. `description: >` followed by indented lines — the server stores
      // them verbatim): re-emitting them after the flow-scalar replacement
      // would orphan indented lines and produce invalid YAML (44.1
      // re-review). Cosmetic blank lines directly after the replaced key are
      // consumed too — an accepted, content-free loss.
      while (i + 1 < lines.length && (lines[i + 1].trim() === "" || /^\s/.test(lines[i + 1]))) {
        i++;
      }
      continue;
    }
    out.push(line);
  }

  // Drop trailing blank lines left over from the split so appended keys land
  // cleanly, without touching any other verbatim content above them.
  while (out.length > 0 && out[out.length - 1].trim() === "") {
    out.pop();
  }

  if (!sawName) out.push(`name: ${JSON.stringify(name)}`);
  if (!sawDescription) out.push(`description: ${JSON.stringify(description)}`);

  return `${out.join("\n")}\n`;
}

/**
 * Merge the normalized skill fields into the frontmatter, preserving every other
 * key.
 *
 * `tool_bindings` and `mdm_tags` are written in YAML flow style (a JSON array is
 * valid YAML), which is what `context_store.py` validates. Unknown keys above and
 * below are re-emitted verbatim: Story 45.2's contract is that a normalized save
 * must not eat frontmatter it does not understand.
 */
export function updateSkillFrontmatter(
  rawYaml: string,
  value: {
    name: string;
    description: string;
    toolBindings: unknown[];
    mdmTags: string[];
    standard: StandardFrontmatter;
  },
): string {
  const base = updateFrontmatterYaml(rawYaml, value.name, value.description);
  const lines = base.split(/\r?\n/);
  const replaced = new Set<string>(["tool_bindings", "mdm_tags", ...STANDARD_FRONTMATTER_KEYS]);
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const key = /^([A-Za-z0-9_-]+):/.exec(lines[i])?.[1];
    if (key && replaced.has(key)) {
      // Consume the replaced key and its block continuation lines.
      while (i + 1 < lines.length && (lines[i + 1].trim() === "" || /^\s/.test(lines[i + 1]))) i++;
      continue;
    }
    out.push(lines[i]);
  }
  while (out.length > 0 && out[out.length - 1].trim() === "") out.pop();
  if (value.toolBindings.length > 0) out.push(`tool_bindings: ${JSON.stringify(value.toolBindings)}`);
  if (value.mdmTags.length > 0) out.push(`mdm_tags: ${JSON.stringify(value.mdmTags)}`);
  out.push(...serializeSkillFrontmatter(value.standard));
  return `${out.join("\n")}\n`;
}
