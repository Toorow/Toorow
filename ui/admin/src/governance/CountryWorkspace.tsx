import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";

import { apiFetch } from "../lib/apiFetch";
import { Badge, Button, Checkbox, ConfirmDialog, EmptyState, Input, label, NativeSelect, Panel, PanelHeader, Stack, stateLabel, stateTone, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, Timestamp, wireWord, Retry } from "../ui";

interface PresetMember {
  parent_key: string;
  value: string;
  optional?: boolean;
}

interface CountryPreset {
  id: string;
  key: string;
  label: string;
  description: string;
  classification: string;
  source_authority: string;
  source_reference?: string | null;
  version: string;
  payload: {
    nodes?: Array<{ key: string; node_kind: string; label: string }>;
    members?: PresetMember[];
  };
}

interface CountryOption {
  code: string;
  display_name: string;
  aliases: string[];
}

interface WorkspaceNode {
  id: string;
  label: string;
  node_kind: "market" | "region" | "rest_of_world";
}

interface WorkspaceMembership {
  parent_node_id: string;
  child_node_id?: string;
  child_value?: string;
  display_order?: number;
}

interface WorkspaceVersion {
  id: string;
  version_number: number;
  status: string;
  content_hash: string;
  memberships: WorkspaceMembership[];
  payload?: {
    rest_of_world?: {
      node_id?: string;
      label?: string;
      parent_node_id?: string | null;
      default_drill?: "country" | "aggregate";
    };
  };
}

/** One row of `app.master_data_object_versions`, as the envelope sends it
 *  (`master_data.py:115` names the columns). Only what a reader can act on is
 *  declared: the rest of the row is storage, not an answer. */
interface WorkspaceVersionRow {
  id?: string;
  version_number?: number;
  status?: string;
  content_hash?: string;
  effective_date?: string | null;
  created_at?: string | null;
  published_at?: string | null;
}

/** One consumer that reads a node of this hierarchy — composed by
 *  `country_workspace.py:184`. `consumer_label` is nullable, so the id is the
 *  fallback and "Unnamed consumer" the last resort; a row is never dropped for
 *  being unnamed. */
interface WorkspaceConsumer {
  node_id?: string;
  consumer_kind?: string;
  consumer_id?: string;
  consumer_label?: string | null;
  consumer_version_id?: string | null;
  hierarchy_version_id?: string | null;
}

interface CountryEnvelope {
  state: "preset_required" | "draft" | "published";
  registry: Record<string, unknown> | null;
  presets: CountryPreset[];
  vocabulary: CountryOption[];
  nodes: WorkspaceNode[];
  draft: WorkspaceVersion | null;
  current: WorkspaceVersion | null;
  versions: WorkspaceVersionRow[];
  used_by: WorkspaceConsumer[];
}

interface EditorNode {
  ref: string;
  id?: string;
  kind: "market" | "region";
  label: string;
}

interface EditorMembership {
  parent_ref: string;
  child_ref?: string;
  child_value?: string;
}

interface Impact {
  node_id: string;
  consumers?: Array<Record<string, unknown>>;
}

const endpoint = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/governance/master-data/country`;

async function readJson(response: Response) {
  return response.json().catch(() => ({}));
}

function Flag({ code, name }: { code: string; name: string }) {
  const normalized = code.trim().toUpperCase();
  const glyph = /^[A-Z]{2}$/.test(normalized)
    ? String.fromCodePoint(
        ...[...normalized].map((letter) => letter.codePointAt(0)! + 127397),
      )
    : normalized;
  return (
    <span
      className="inline-flex min-w-[2.75rem] items-center justify-center gap-1 rounded-control border border-divider-base bg-surface-light px-1 py-0.5 text-center"
      role="img"
      aria-label={name}
      title={`${name} (${normalized})`}
    >
      <span aria-hidden="true" className="text-base leading-none">
        {glyph}
      </span>
      <span aria-hidden="true" className="font-mono text-[0.625rem] font-semibold leading-none">
        {normalized}
      </span>
    </span>
  );
}

/** The hierarchy as it stands, read-only — the same nesting the draft editor
 *  shows, without a single control.
 *
 *  WHY IT EXISTS. A published Project used to show one green box carrying the
 *  version's raw UUID and a button. The Markets, the Regions and the country
 *  assignments — the entire answer to "what is live right now" — were not on the
 *  screen at all, so the only way to see what every report is currently grouped
 *  by was to open a DRAFT of it. That is an edit performed to satisfy a read,
 *  and it leaves a pending version behind on a Project nobody meant to change.
 *
 *  It reads what the envelope already carries: `nodes` (already filtered by the
 *  server to the selected version) and the memberships of that version. Nothing
 *  is fetched a second time and nothing is inferred. */
function HierarchyTree({
  nodes,
  memberships,
  vocabulary,
}: {
  nodes: WorkspaceNode[];
  memberships: WorkspaceMembership[];
  vocabulary: CountryOption[];
}) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const parentOf = new Map<string, string>();
  for (const membership of memberships) {
    if (membership.child_node_id) parentOf.set(membership.child_node_id, membership.parent_node_id);
  }
  const countryName = (code: string) =>
    vocabulary.find((country) => country.code === code)?.display_name ?? code;
  const countriesOf = (nodeId: string) =>
    memberships
      .filter((membership) => membership.parent_node_id === nodeId && membership.child_value)
      .map((membership) => membership.child_value as string);
  const childrenOf = (nodeId: string) =>
    memberships
      .filter((membership) => membership.parent_node_id === nodeId && membership.child_node_id)
      .map((membership) => byId.get(membership.child_node_id as string))
      .filter((node): node is WorkspaceNode => Boolean(node));

  // A Market with no Region is a root, exactly as it is in the editor: a
  // reporting Region is optional, and hiding an unparented Market would hide
  // countries that are genuinely assigned.
  //
  // Rest of World is deliberately NOT a branch here. Its parent lives in the
  // version payload, not in a membership, so drawing it as a root would place it
  // outside a reporting Region it may well sit inside — a wrong answer rendered
  // confidently. Its own statement below says where it sits and how it drills.
  const roots = nodes.filter(
    (node) => node.node_kind !== "rest_of_world" && !parentOf.has(node.id),
  );

  // `seen` is not defensive decoration: the server accepts a hierarchy edit,
  // not a proof of acyclicity, and a cycle must render as a truncated branch
  // rather than freeze the browser.
  const branch = (node: WorkspaceNode, seen: ReadonlySet<string>): ReactNode => {
    if (seen.has(node.id)) return null;
    const nextSeen = new Set(seen).add(node.id);
    const countries = countriesOf(node.id);
    const children = childrenOf(node.id);
    return (
      <li key={node.id} className="list-none">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-ui font-semibold text-text">{node.label}</span>
          <Badge outline>{node.node_kind.replaceAll("_", " ")}</Badge>
        </div>
        {countries.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {countries.map((code) => (
              <Badge outline key={code}>
                <Flag code={code} name={countryName(code)} />
                <span className="ml-2">{countryName(code)}</span>
              </Badge>
            ))}
          </div>
        )}
        {node.node_kind === "market" && countries.length === 0 && (
          <p className="mt-2 mb-0 text-caption text-text-secondary">
            No country is assigned to this Market.
          </p>
        )}
        {children.length > 0 && (
          <ul className="mt-2 ml-2 list-none space-y-3 border-l border-divider-base pl-4">
            {children.map((child) => branch(child, nextSeen))}
          </ul>
        )}
      </li>
    );
  };

  if (roots.length === 0) {
    return (
      <EmptyState
        title="This version defines no Market"
        description="It was published with no Market and no reporting Region, so every country falls to Rest of World. Open a draft to define one."
      />
    );
  }

  return (
    <ul aria-label="Country hierarchy" className="m-0 list-none space-y-3 p-0">
      {roots.map((node) => branch(node, new Set<string>()))}
    </ul>
  );
}

/** What leaves with the node, counted, in the reader's words. Only the parts
 *  that are actually non-zero are said: "0 countries and 1 nested Market" makes
 *  a reader check a number that was never at stake. */
function removalSentence({ countries, children }: { countries: number; children: number }) {
  const parts: string[] = [];
  if (countries > 0) parts.push(`${countries} ${countries === 1 ? "country" : "countries"}`);
  if (children > 0) {
    parts.push(`${children} nested ${children === 1 ? "Market" : "Markets"}`);
  }
  return (
    `This removes ${parts.join(" and ")} from the draft. ` +
    "Nothing published changes until you save and publish, and the countries return to Rest of World."
  );
}

/*
 * THE PRIVATE VERSION MAP IS GONE (76-2). It drew `archived` GREY where the
 * union has always drawn it red -- an archived object cannot be bound, which is
 * the one thing `error` means in this scale -- so this screen was the only place
 * in the console where archiving looked like a filing decision. `current`,
 * `candidate` and `superseded` are declared words now.
 */

/** The versions this hierarchy has had. The envelope has carried them since the
 *  workbench existed and no screen read them, so "what did this Project group by
 *  last quarter" had no answer anywhere in the console. */
function VersionHistory({
  versions,
  currentId,
}: {
  versions: WorkspaceVersionRow[];
  currentId?: string | null;
}) {
  return (
    <Panel flush>
      <PanelHeader
        title="Version history"
        description="Every revision this hierarchy has had. A published version is immutable, so this is the only record of what a past report was grouped by."
      />
      {versions.length === 0 ? (
        <EmptyState
          title="No version has been recorded"
          description="A revision appears here the first time a draft is saved. Nothing has been saved on this Project yet."
        />
      ) : (
        <TableScroll label="Country hierarchy versions">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Version</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Recorded</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {versions.map((version, index) => (
                <TableRow key={String(version.id ?? index)}>
                  <TableCell className="font-semibold text-text">
                    Version {version.version_number ?? "unnumbered"}
                    {version.id && String(version.id) === String(currentId) && (
                      // `Live` is the healthy state of a version, not a label:
                      // it was the same grey as the country chips beside it.
                      <Badge tone="success" className="ml-2">Live</Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <Status tone={stateTone(version.status as string | null)}>
                      {stateLabel(version.status as string | null)}
                    </Status>
                  </TableCell>
                  {/* Published wins over created: for a live version the date a
                      reader cares about is the day it started answering. */}
                  <TableCell className="text-ui text-text-secondary">
                    <Timestamp
                      value={version.published_at ?? version.created_at ?? null}
                      absentMeaning="Never published"
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      )}
    </Panel>
  );
}

/** Who reads this hierarchy, by name. The same rows the impact refusal quotes
 *  when an edit would change bound meaning — shown BEFORE the edit, so the
 *  consequence is known while there is still a choice. */
function UsedBy({
  consumers,
  nodes,
}: {
  consumers: WorkspaceConsumer[];
  nodes: WorkspaceNode[];
}) {
  // THE SERVED WORD, OR THE NAMED ABSENCE -- never the identifier in between.
  // `?? nodeId ?? "Unnamed node"` put a `mdnode_<ULID>` in a name's position
  // while the honest word for that state was already written one term to its
  // right, and unreachable. `visualization-and-rendering.md` ("A member's label
  // is a name, never its identifier") makes the middle term the defect.
  const nodeLabel = (nodeId?: string) =>
    nodes.find((node) => node.id === nodeId)?.label ?? "Unnamed node";
  return (
    <Panel flush>
      <PanelHeader
        title="What reads this hierarchy"
        description="Everything bound to a Market or a Region defined here. Renaming or removing one changes what these read."
      />
      {consumers.length === 0 ? (
        <EmptyState
          title="Nothing reads this hierarchy yet"
          description="No report, view or datastream currently groups by a Market or a Region defined here. Bind one to a geographic dimension and it names itself in this list."
        />
      ) : (
        <TableScroll label="Country hierarchy consumers">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Consumer</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Reads</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {consumers.map((consumer, index) => (
                <TableRow key={`${consumer.consumer_id ?? index}:${consumer.node_id ?? ""}`}>
                  <TableCell className="font-semibold text-text">
                    {consumer.consumer_label ?? "Unnamed consumer"}
                  </TableCell>
                  <TableCell className="text-ui text-text-secondary">
                    {consumer.consumer_kind ? label(consumer.consumer_kind) : "Unstated"}
                  </TableCell>
                  <TableCell className="text-ui text-text-secondary">
                    {nodeLabel(consumer.node_id)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      )}
    </Panel>
  );
}

export default function CountryWorkspace({ projectId }: { projectId: string }) {
  const [model, setModel] = useState<CountryEnvelope | null>(null);
  const [nodes, setNodes] = useState<EditorNode[]>([]);
  const [memberships, setMemberships] = useState<EditorMembership[]>([]);
  const [optional, setOptional] = useState<Record<string, boolean>>({});
  const [countryDraft, setCountryDraft] = useState<Record<string, string>>({});
  const [impacts, setImpacts] = useState<Impact[]>([]);
  const [restOfWorldLabel, setRestOfWorldLabel] = useState("Rest of World");
  const [restOfWorldParent, setRestOfWorldParent] = useState("");
  const [restOfWorldDrill, setRestOfWorldDrill] = useState<"country" | "aggregate">("country");
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pendingRemoval, setPendingRemoval] = useState<{
    ref: string;
    countries: number;
    children: number;
  } | null>(null);

  const load = useCallback(async () => {
    setError(null);
    const response = await apiFetch(endpoint(projectId), {
      cache: "no-store",
    });
    const body = await readJson(response);
    if (!response.ok) {
      throw new Error(String(body?.message ?? "Country Governance is unavailable."));
    }
    setModel(body as CountryEnvelope);
  }, [projectId]);

  useEffect(() => {
    void load().catch((cause) =>
      setError(cause instanceof Error ? cause.message : "Country Governance is unavailable."),
    );
  }, [load]);

  useEffect(() => {
    if (!model?.draft) return;
    const editable = model.nodes
      .filter((node) => node.node_kind === "market" || node.node_kind === "region")
      .map((node) => ({
        ref: node.id,
        id: node.id,
        kind: node.node_kind as "market" | "region",
        label: node.label,
      }));
    const refs = new Set(editable.map((node) => node.ref));
    setNodes(editable);
    setMemberships(
      model.draft.memberships.flatMap<EditorMembership>((membership) => {
        if (!refs.has(membership.parent_node_id)) return [];
        if (membership.child_node_id && refs.has(membership.child_node_id)) {
          return [
            {
              parent_ref: membership.parent_node_id,
              child_ref: membership.child_node_id,
            },
          ];
        }
        if (membership.child_value) {
          return [
            {
              parent_ref: membership.parent_node_id,
              child_value: membership.child_value,
            },
          ];
        }
        return [];
      }),
    );
    const rest = model.draft.payload?.rest_of_world;
    setRestOfWorldLabel(rest?.label ?? "Rest of World");
    setRestOfWorldParent(rest?.parent_node_id ?? "");
    setRestOfWorldDrill(rest?.default_drill ?? "country");
    setDirty(false);
  }, [model]);

  const command = async (
    action: string,
    payload: Record<string, unknown>,
    acknowledge = false,
  ) => {
    setBusy(true);
    setError(null);
    try {
      const response = await apiFetch(endpoint(projectId), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          action,
          payload: acknowledge ? { ...payload, acknowledge_impact: true } : payload,
        }),
      });
      const body = await readJson(response);
      if (
        response.status === 409 &&
        body?.code === "country_workspace_impact_acknowledgement_required"
      ) {
        setImpacts(Array.isArray(body.impacts) ? body.impacts : []);
        return false;
      }
      if (!response.ok) {
        throw new Error(String(body?.message ?? `Country command failed (${response.status}).`));
      }
      setImpacts([]);
      await load();
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Country command failed.");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const regions = useMemo(
    () => nodes.filter((node) => node.kind === "region"),
    [nodes],
  );

  const applyPreset = (preset: CountryPreset) =>
    command("apply_preset", {
      preset_id: preset.id,
      selected_optional_values: (preset.payload.members ?? [])
        .filter((member) => member.optional && optional[`${preset.id}:${member.value}`])
        .map((member) => member.value),
    });

  const savePayload = () => ({
    version_id: model?.draft?.id,
    expected_content_hash: model?.draft?.content_hash,
    nodes,
    memberships,
    rest_of_world_label: restOfWorldLabel,
    rest_of_world_parent_ref: restOfWorldParent,
    rest_of_world_drill: restOfWorldDrill,
  });

  const addNode = (kind: "market" | "region") => {
    setDirty(true);
    const ref = `new:${kind}:${crypto.randomUUID()}`;
    setNodes((current) => [
      ...current,
      {
        ref,
        kind,
        label: kind === "market" ? "New Market" : "New Region",
      },
    ]);
  };

  const setParentRegion = (marketRef: string, regionRef: string) => {
    setDirty(true);
    setMemberships((current) => [
      ...current.filter((item) => item.child_ref !== marketRef),
      ...(regionRef ? [{ parent_ref: regionRef, child_ref: marketRef }] : []),
    ]);
  };

  const addCountry = (market: EditorNode) => {
    const raw = (countryDraft[market.ref] ?? "").trim();
    const match = model?.vocabulary.find(
      (country) =>
        country.code.toLowerCase() === raw.toLowerCase() ||
        country.display_name.toLowerCase() === raw.toLowerCase() ||
        country.aliases.some((alias) => alias.toLowerCase() === raw.toLowerCase()),
    );
    if (!match) {
      setError("Choose a canonical country from the governed vocabulary.");
      return;
    }
    setDirty(true);
    setMemberships((current) => [
      ...current.filter((item) => item.child_value !== match.code),
      { parent_ref: market.ref, child_value: match.code },
    ]);
    setCountryDraft((current) => ({ ...current, [market.ref]: "" }));
  };

  const removeCountry = (countryCode: string) => {
    setDirty(true);
    setMemberships((current) =>
      current.filter((item) => item.child_value !== countryCode),
    );
  };

  const applyRemoveNode = (nodeRef: string) => {
    setDirty(true);
    setNodes((current) => current.filter((item) => item.ref !== nodeRef));
    setMemberships((current) =>
      current.filter(
        (item) => item.parent_ref !== nodeRef && item.child_ref !== nodeRef,
      ),
    );
    if (restOfWorldParent === nodeRef) {
      setRestOfWorldParent("");
    }
  };

  /** Removing a node takes its contents with it, and the contents were not on
   *  the button. A Region holding four Markets, or a Market holding thirty
   *  countries, vanished on one click with nothing naming what left with it —
   *  and the draft is only recoverable by reloading, which discards every other
   *  unsaved edit too.
   *
   *  An EMPTY node still goes in one click: a confirmation that fires when
   *  nothing is at stake teaches people to dismiss it without reading, which is
   *  how the one that matters gets dismissed. Same reason a single country chip
   *  stays one click — it is one visible badge, put back by typing its name. */
  const removeNode = (nodeRef: string) => {
    const countries = memberships.filter(
      (item) => item.parent_ref === nodeRef && item.child_value,
    ).length;
    const children = memberships.filter(
      (item) => item.parent_ref === nodeRef && item.child_ref,
    ).length;
    if (countries === 0 && children === 0) {
      applyRemoveNode(nodeRef);
      return;
    }
    setPendingRemoval({ ref: nodeRef, countries, children });
  };

  const pendingNode = pendingRemoval
    ? nodes.find((item) => item.ref === pendingRemoval.ref)
    : undefined;

  const publishVersion = async () => {
    const draft = model?.draft;
    if (!draft || dirty) {
      setError("Save the draft before publishing this exact reviewed version.");
      return;
    }
    setBusy(true);
    setError(null);
    const idempotencyKey = crypto.randomUUID();
    const publication = {
      version_id: draft.id,
      expected_content_hash: draft.content_hash,
    };
    try {
      const prepareResponse = await apiFetch(endpoint(projectId), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({ action: "prepare_publish", payload: publication }),
      });
      const prepared = await readJson(prepareResponse);
      if (!prepareResponse.ok) {
        throw new Error(
          String(prepared?.message ?? "Country publication review could not be prepared."),
        );
      }
      const publishResponse = await apiFetch(endpoint(projectId), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({
          action: "publish",
          payload: {
            ...publication,
            confirmation_id: prepared.confirmation_id,
            confirmation_secret: prepared.confirmation_secret,
          },
        }),
      });
      const published = await readJson(publishResponse);
      if (!publishResponse.ok) {
        throw new Error(
          String(published?.message ?? "Country publication failed."),
        );
      }
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Country publication failed.");
    } finally {
      setBusy(false);
    }
  };
  if (error && !model) {
    return (
      <Status as="block" tone="error" title="Country Governance is unavailable"
          action={<Retry onClick={() => void load()} />}
        >
        {error}
      </Status>
    );
  }
  if (!model) {
    return <p role="status" className="text-ui text-text-secondary">Loading Country Governance…</p>;
  }

  if (model.state === "published" && model.current) {
    const rest = model.current.payload?.rest_of_world;
    const restParent = rest?.parent_node_id
      ? model.nodes.find((node) => node.id === rest.parent_node_id)?.label
      : null;
    return (
      <Stack className="gap-4">
        {error && <Status as="block" tone="error">{error}</Status>}
        <PanelHeader
          title="Published Country hierarchy"
          description={`Version ${model.current.version_number} is what every report reads today. It is immutable: open a draft to change it.`}
        />
        {/* The hierarchy itself, before anything else. Reading what is live must
            not require creating a draft — see `HierarchyTree`. */}
        <Panel className="space-y-3">
          <HierarchyTree
            nodes={model.nodes}
            memberships={model.current.memberships}
            vocabulary={model.vocabulary}
          />
        </Panel>
        {model.nodes.some((node) => node.node_kind === "rest_of_world") && (
          <Status as="block" tone="info" title="Rest of World in this version">
            Valid countries not assigned above are reported as{" "}
            {rest?.label ?? "Rest of World"}
            {restParent ? ` under ${restParent}` : ", under no reporting Region"}, and are{" "}
            {rest?.default_drill === "aggregate"
              ? "kept aggregated by default."
              : "explorable by country."}{" "}
            Missing or invalid values remain separate DQ evidence.
          </Status>
        )}
        <Panel className="space-y-3">
          <p className="m-0 text-ui text-text-secondary">
            Editing never touches this version. A draft is a separate revision, and
            it replaces this one only when you publish it.
          </p>
          <Button
            disabled={busy}
            onClick={() =>
              void command("start_draft", {
                current_version_id: model.current?.id,
                expected_content_hash: model.current?.content_hash,
              })
            }
          >
            Create editable draft
          </Button>
        </Panel>
        <VersionHistory versions={model.versions} currentId={model.current.id} />
        <UsedBy consumers={model.used_by} nodes={model.nodes} />
      </Stack>
    );
  }

  if (model.state === "preset_required" || !model.draft) {
    return (
      <Stack className="gap-4">
        {error && <Status as="block" tone="error">{error}</Status>}
        <PanelHeader
          title="Choose a qualified starting point"
          description="A preset is inert until you choose it. It materializes as an editable Project draft and never remains a live dependency."
        />
        {model.presets.length === 0 ? (
          <EmptyState
            title="No qualified Country preset is available"
            description="Nothing has been guessed and no blank hierarchy has been created."
          />
        ) : (
          model.presets.map((preset) => (
            <Panel key={preset.id} className="space-y-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="m-0 text-h3 font-h3 text-text">{preset.label}</h3>
                  <p className="mt-1 mb-0 text-ui text-text-secondary">{preset.description}</p>
                </div>
                <Badge outline>{preset.classification.replaceAll("_", " ")}</Badge>
              </div>
              <p className="m-0 text-caption text-text-secondary">
                <span>{preset.source_authority}</span>
                <span className="ml-2">Version {preset.version}</span>
              </p>
              {(preset.payload.members ?? [])
                .filter((member) => member.optional)
                .map((member) => {
                  const country = model.vocabulary.find(
                    (item) => item.code === member.value,
                  );
                  const key = `${preset.id}:${member.value}`;
                  return (
                    <label key={key} className="flex items-center gap-2 text-ui text-text">
                      <Checkbox
                        checked={Boolean(optional[key])}
                        onCheckedChange={(next) =>
                          setOptional((current) => ({
                            ...current,
                            [key]: next === true,
                          }))
                        }
                      />
                      <Flag
                        code={member.value}
                        name={country?.display_name ?? member.value}
                      />
                      Include {country?.display_name ?? member.value}
                    </label>
                  );
                })}
              <Button
                disabled={busy}
                onClick={() => void applyPreset(preset)}
              >
                Use {preset.label}
              </Button>
            </Panel>
          ))
        )}
      </Stack>
    );
  }

  return (
    <Stack className="gap-4">
      <PanelHeader
        title="Country hierarchy draft"
        description="Edit stable Markets, nest them into reporting Regions, and assign each canonical country at most once."
      />
      {error && <Status as="block" tone="error">{error}</Status>}
      {dirty && (
        <Status as="block" tone="warning" title="Unsaved hierarchy changes">
          Save this draft before publishing so the reviewed hash matches what you see.
        </Status>
      )}
      {model.nodes.some((node) => node.node_kind === "rest_of_world") && (
        <Status as="block" tone="info" title="Rest of World remains drillable">
          Valid countries not assigned below stay in Rest of World. Missing or invalid values remain separate DQ evidence.
        </Status>
      )}
      {model.nodes.some((node) => node.node_kind === "rest_of_world") && (
        <Panel className="space-y-3">
          <h3 className="m-0 text-h3 font-h3 text-text">Rest of World policy</h3>
          <label className="block text-ui font-semibold text-text">
            Reporting label
            <Input
              className="mt-1"
              aria-label="Rest of World label"
              value={restOfWorldLabel}
              onChange={(event) => {
                setDirty(true);
                setRestOfWorldLabel(event.target.value);
              }}
            />
          </label>
          <label className="block text-ui font-semibold text-text">
            Parent reporting Region
            <NativeSelect
              className="mt-1"
              aria-label="Rest of World parent Region"
              value={restOfWorldParent}
              onChange={(event) => {
                setDirty(true);
                setRestOfWorldParent(event.target.value);
              }}
            >
              <option value="">No reporting Region</option>
              {regions.map((region) => (
                <option key={region.ref} value={region.ref}>
                  {region.label}
                </option>
              ))}
            </NativeSelect>
          </label>
          <label className="block text-ui font-semibold text-text">
            Default exploration
            <NativeSelect
              className="mt-1"
              aria-label="Rest of World default exploration"
              value={restOfWorldDrill}
              onChange={(event) => {
                setDirty(true);
                setRestOfWorldDrill(event.target.value as "country" | "aggregate");
              }}
            >
              <option value="country">Explore Countries</option>
              <option value="aggregate">Keep aggregated by default</option>
            </NativeSelect>
          </label>
        </Panel>
      )}
      {impacts.length > 0 && (
        <Status
          as="block"
          tone="warning"
          title="This edit changes bound geographic meaning"
          data-testid="country-impact"
        >
          <ul>
            {impacts.flatMap((impact) =>
              (impact.consumers ?? []).map((consumer, index) => (
                <li key={`${impact.node_id}:${String(consumer.consumer_id ?? index)}`}>
                  {String(consumer.consumer_label ?? "Unnamed consumer")}
                </li>
              )),
            )}
          </ul>
          <Button
            className="mt-3"
            variant="destructive"
            disabled={busy}
            onClick={() => void command("save_hierarchy", savePayload(), true)}
          >
            Save and acknowledge impact
          </Button>
        </Status>
      )}

      {nodes.map((node) => (
        <Panel key={node.ref} className="space-y-3">
          <div className="flex items-end gap-3">
            <label className="grow text-ui font-semibold text-text">
              Name for {node.label}
              <Input
                className="mt-1"
                aria-label={`Name for ${node.label}`}
                value={node.label}
                onChange={(event) => {
                  setDirty(true);
                  setNodes((current) =>
                    current.map((item) =>
                      item.ref === node.ref
                        ? { ...item, label: event.target.value }
                        : item,
                    ),
                  );
                }}
              />
            </label>
            <Badge outline>{wireWord(node.kind)}</Badge>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={() => removeNode(node.ref)}
            >
              Remove {node.kind}
            </Button>
          </div>
          {node.kind === "market" && (
            <>
              <label className="block text-ui font-semibold text-text">
                Parent region for {node.label}
                <NativeSelect
                  className="mt-1"
                  aria-label={`Parent region for ${node.label}`}
                  value={
                    memberships.find((item) => item.child_ref === node.ref)
                      ?.parent_ref ?? ""
                  }
                  onChange={(event) => setParentRegion(node.ref, event.target.value)}
                >
                  <option value="">No reporting Region</option>
                  {regions.map((region) => (
                    <option key={region.ref} value={region.ref}>
                      {region.label}
                    </option>
                  ))}
                </NativeSelect>
              </label>
              <div className="flex flex-wrap gap-2">
                {memberships
                  .filter(
                    (item) =>
                      item.parent_ref === node.ref && item.child_value,
                  )
                  .map((item) => {
                    const country = model.vocabulary.find(
                      (candidate) => candidate.code === item.child_value,
                    );
                    return (
                      <Badge outline key={item.child_value}>
                        <Flag
                          code={item.child_value as string}
                          name={country?.display_name ?? (item.child_value as string)}
                        />
                        <span className="ml-2">
                          {country?.display_name ?? item.child_value}
                        </span>
                        <button
                          type="button"
                          className="ml-2 rounded px-1 text-ui font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
                          aria-label={"Remove " + (country?.display_name ?? item.child_value) + " from " + node.label}
                          onClick={() => removeCountry(item.child_value as string)}
                        >
                          ×
                        </button>
                      </Badge>
                    );
                  })}
              </div>
              <div className="flex gap-2">
                <Input
                  list="country-vocabulary"
                  aria-label={`Add country to ${node.label}`}
                  placeholder="Search country name, code or alias"
                  value={countryDraft[node.ref] ?? ""}
                  onChange={(event) =>
                    setCountryDraft((current) => ({
                      ...current,
                      [node.ref]: event.target.value,
                    }))
                  }
                />
                <Button
                  variant="secondary"
                  onClick={() => addCountry(node)}
                >
                  Add country
                </Button>
              </div>
            </>
          )}
        </Panel>
      ))}

      <datalist id="country-vocabulary">
        {model.vocabulary.map((country) => (
          <option
            key={country.code}
            value={country.display_name}
          >{country.code}</option>
        ))}
      </datalist>

      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" onClick={() => addNode("market")}>
          Add Market
        </Button>
        <Button variant="secondary" onClick={() => addNode("region")}>
          Add Region
        </Button>
        <Button
          disabled={busy}
          onClick={() => void command("save_hierarchy", savePayload())}
        >
          Save draft
        </Button>
        <Button
          variant="secondary"
          disabled={busy || dirty}
          onClick={() => void publishVersion()}
        >
          Publish version
        </Button>
      </div>

      <VersionHistory
        versions={model.versions}
        currentId={(model.registry?.current_version_id as string | undefined) ?? null}
      />
      <UsedBy consumers={model.used_by} nodes={model.nodes} />

      <ConfirmDialog
        open={Boolean(pendingRemoval && pendingNode)}
        onOpenChange={(open) => {
          if (!open) setPendingRemoval(null);
        }}
        title={`Remove ${pendingNode?.label ?? "this node"}?`}
        description={pendingRemoval ? removalSentence(pendingRemoval) : ""}
        confirmLabel={`Remove ${pendingNode?.kind ?? "node"}`}
        destructive
        cancelTestId="country-remove-cancel"
        confirmTestId="country-remove-confirm"
        onConfirm={() => {
          if (pendingRemoval) applyRemoveNode(pendingRemoval.ref);
          setPendingRemoval(null);
        }}
      />
    </Stack>
  );
}
