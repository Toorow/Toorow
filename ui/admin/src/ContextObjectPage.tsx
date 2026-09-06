import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./lib/apiFetch";
import RawMarkdown from "./lib/RawMarkdown";
import GraphLinks from "./connaissances/GraphLinks";
import KnowledgeEditorDrawer from "./connaissances/KnowledgeEditorDrawer";
import NodeRemarks from "./connaissances/NodeRemarks";
import SkillEditorDrawer, { type SkillEditorSaveValue } from "./connaissances/SkillEditorDrawer";
import { updateSkillFrontmatter } from "./connaissances/skillFrontmatter";
import SkillStepList from "./connaissances/SkillStepList";
import { Button, EvidenceRows, PageHeader, Timestamp } from "./ui";
import {
  CONTEXT_REQUEST_TIMEOUT_MS,
  errorMessage,
  parseProcedure,
  parseTopic,
  parseVersionsEnvelope,
  readSurfaceFailure,
  type ContextProcedure,
  type ContextTopic,
  type ContextVersion,
} from "./lib/contextRuntime";

/**
 * Two rules of the retired knowledge.css, ported into the theme's utility
 * vocabulary (docs/ui-css-strategy.md, wave 3). Theme tokens only, so both
 * stay dark-scheme safe. `PREVIEW_CLASS` is the recessed raw-content box the
 * Content tab and each version snapshot share; its `[&_pre]` rules carry the
 * typography of RawMarkdown's `<pre>` and of the frontmatter fallback.
 */
const PREVIEW_CLASS =
  "flex min-h-[220px] flex-col gap-1.5 overflow-auto rounded-[8px] border border-[color-mix(in_srgb,var(--color-text-secondary)_20%,var(--color-surface-light))] bg-[color-mix(in_srgb,var(--color-text-secondary)_4%,var(--color-surface-light))] px-3 py-[9px] [&_pre]:m-0 [&_pre]:font-primary [&_pre]:text-label [&_pre]:leading-[1.6] [&_pre]:whitespace-pre-wrap [&_pre]:break-words [&_pre]:text-text";
/** .knowledge-load-error — the error block, with its tinted rim. */
const NOTICE_CLASS =
  "flex flex-col items-start gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-error)_28%,var(--color-surface-light))] bg-[color-mix(in_srgb,var(--color-error)_6%,var(--color-surface-light))] px-[22px] py-5 [&_p]:m-0 [&_p]:text-label [&_p]:text-text-secondary";
/**
 * The same block, in the WARNING tint — the retired object and the unreadable
 * one must not be one colour. Theme tokens only, the same `color-mix` recipe
 * as above: a distinct state, and no new stylesheet (`ui-css-strategy.md`).
 */
const STALE_CLASS =
  "flex flex-col items-start gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_28%,var(--color-surface-light))] bg-[color-mix(in_srgb,var(--color-warning)_6%,var(--color-surface-light))] px-[22px] py-5 [&_p]:m-0 [&_p]:text-label [&_p]:text-text-secondary";
/** The legacy `.panel` card recipe (application.css) — border, radius, surface;
 * no padding, exactly as the sections here always rendered it. */
const PANEL_CLASS =
  "overflow-hidden rounded-large border border-divider-base bg-surface-light shadow-card-light";

type Kind = "topic" | "procedure";
type Tab = "content" | "usage" | "versions";
type ContextObject = ContextTopic | ContextProcedure;
/**
 * A version of a Knowledge item or of a Skill is identified by its NUMBER.
 *
 * There is no second identifier to reach for: `app.context_topics_versions` and
 * `app.context_procedures_versions` key a snapshot by `(object_id,
 * version_number)`, and that pair is what an AI Path step, a relationship pin
 * and this address all name. Rendering `v3` while addressing something else
 * would give the same snapshot two names.
 */
function versionAddress(versionNumber: number): string {
  return String(versionNumber);
}
/**
 * FOUR ANSWERS, NOT TWO (story 49-6 AC1).
 *
 * This page had `loading | error | ready`, so a caller who may not read this
 * object, a store that failed, and an object that has been RETIRED all rendered
 * the same red box titled "Context object unavailable" — three different facts,
 * one sentence, and only one of them repaired by the Retry button it offered.
 *
 * `denied` is an answer about the CALLER: the server gives the same
 * non-disclosing 404 for foreign, absent and forbidden (`context_api.py`
 * `_context_not_found`), so the sentence names access and offers the way back,
 * never a retry that will be refused again.
 *
 * `stale` is an answer about the OBJECT: it was archived, so it is no longer
 * current — and it is still readable, which is why it is a banner over the
 * content rather than a wall in front of it. `objectSurfaces.tsx:201-213`
 * refuses to call a pinned version "stale" for exactly this reason: the word
 * means retired, superseded, no longer current, and using it for anything else
 * tells an operator something false.
 */
type LoadState =
  | { status: "loading" }
  | { status: "denied"; message: string }
  | { status: "error"; message: string }
  | { status: "ready"; object: ContextObject; versions: ContextVersion[] | null; versionsError: string | null };

function normalizeTab(value: string | null): Tab {
  return value === "usage" || value === "versions" ? value : "content";
}

/**
 * The top-level SCALAR keys of a pinned frontmatter, as named rows.
 *
 * A version's frontmatter was printed into a `<pre>` — the same
 * `JSON.stringify`-in-a-box defect `ui/Evidence.tsx` was extracted to end, one
 * markup language over. The standardized blocks (`steps`, `acceptance`,
 * `common_errors`, …) are rendered by `SkillStepList`, which already reads
 * them; this covers what is left, which is one `key: value` per line.
 *
 * It is deliberately NOT a YAML parser: a block key is skipped here rather than
 * flattened into a half-read row, and when nothing at all can be named the
 * caller falls back to the stored text. Guessing a shape would be worse than
 * showing the evidence.
 */
export function frontmatterFields(raw: string): Record<string, string> {
  const fields: Record<string, string> = {};
  for (const line of raw.split(/\r?\n/)) {
    if (line.trim() === "" || /^\s/.test(line)) continue;
    const match = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
    if (!match) continue;
    const [, key, value] = match;
    const scalar = value.trim();
    // `steps:` with nothing after it opens a block — SkillStepList owns those.
    if (scalar === "") continue;
    fields[key] = scalar.replace(/^["'](.*)["']$/, "$1");
  }
  return fields;
}

/** A pinned frontmatter, READ: the standardized blocks through the renderer the
 *  Content tab already uses, the remaining keys as named rows — and the stored
 *  text only when nothing at all could be named, because a shape we failed to
 *  read is not a shape that was not there. */
function VersionFrontmatter({ raw, versionNumber }: { raw: string; versionNumber: number }) {
  const fields = frontmatterFields(raw);
  return (
    <>
      <SkillStepList frontmatterYaml={raw} />
      {Object.keys(fields).length > 0 ? (
        <EvidenceRows label={`Version ${versionNumber} frontmatter`} source={fields} />
      ) : (
        <pre>{raw}</pre>
      )}
    </>
  );
}

/**
 * ONE immutable snapshot, READ — the exact same markup whether it is expanded
 * inside the ledger or opened at its own address. Two renderings of one version
 * is how a pinned address starts showing something the list does not.
 */
function VersionSnapshot({ version, kind }: { version: ContextVersion; kind: Kind }) {
  return (
    <div className={PREVIEW_CLASS} aria-label={`Version ${version.version_number} snapshot`}>
      {kind === "topic" ? (
        <h3>{version.title}</h3>
      ) : (
        <>
          <h3>{version.name}</h3>
          <p>{version.description}</p>
          <details>
            <summary>Frontmatter</summary>
            {/* The pinned shape, READ — the standardized blocks through the
                same renderer the Content tab uses, the remaining keys as named
                rows. A version was the one place this object still printed YAML
                at a person. The stored text is the fallback and only that: when
                nothing can be named, showing it verbatim keeps the evidence
                rather than hiding a shape we failed to read. */}
            <VersionFrontmatter
              raw={version.frontmatter_yaml ?? ""}
              versionNumber={version.version_number}
            />
          </details>
        </>
      )}
      <RawMarkdown text={version.body_md} placeholder="No persisted content." />
    </div>
  );
}

export default function ContextObjectPage({
  projectId,
  kind,
  objectId,
  tab,
  versionId = null,
  onNavigateTab,
  onOpenVersion,
  versionHref,
  onBack,
  onOpenGraph,
  onOpenPeer,
}: {
  projectId: string;
  kind: Kind;
  objectId: string;
  tab: string | null;
  /**
   * The version this ADDRESS pins, or `null` for the object as it stands.
   *
   * `objectSurfaces.tsx` used to refuse this address out loud — "the
   * context-topic workbench does not open a pinned version yet" — because the
   * ledger listed versions whose rows opened nothing. The refusal was honest
   * and it was still a dead end: `README.md` invariant 7 asks every projection
   * to deep-link the exact owner, object AND version, and an AI Path step pins
   * a Skill version that no address could open.
   */
  versionId?: string | null;
  onNavigateTab: (tab: Tab) => void;
  /** Pins a version in the address, or returns to the object as it stands. */
  onOpenVersion?: (versionId: string | null) => void;
  /** The real `href` of a pinned version, so the row copies, opens in a new tab
   *  and middle-clicks like any other address. */
  versionHref?: (versionId: string) => string;
  onBack: () => void;
  /**
   * Opens the Knowledge Graph, the surface that OWNS creating and removing a
   * link (Story 44.5). The Usage tab reads them; it no longer writes them.
   */
  onOpenGraph?: () => void;
  /**
   * Opens a related object this console has a workbench for — a Knowledge item
   * or a Skill. The facet names every peer by type and id; only the ones an
   * owner answers for get a control, and the rest are located by name.
   */
  onOpenPeer?: (peer: { type: string; id: string }) => void;
}) {
  const activeTab = normalizeTab(tab);
  const scope = projectId.trim();
  /** The PRODUCT noun, once — `kind` is the delivered token (`glossary.md`). */
  const noun = kind === "topic" ? "Knowledge item" : "Skill";
  const [state, setState] = useState<LoadState>({ status: "loading" });
  /**
   * THE ADJUSTMENT, WHERE THE REMARK IS READ.
   *
   * This workbench rendered content, versions, links and open remarks, and the
   * only thing a reader could do with a remark here was CLOSE it — the exact
   * queue `context-hub.md` names: "a remark can only be closed and not acted
   * on … so the queue empties without the corpus changing". The editor was on
   * the collection screen, one navigation away, and a reader who arrived from
   * the mindmap or from a `skill` address had to leave to correct anything.
   *
   * It is the SAME editor the collection mounts, not a second one, and it saves
   * through the same PATCH carrying `expected_version` — so answering a remark
   * produces a new version, and the remark marks itself "older version" on the
   * reload that follows.
   */
  const [editorOpen, setEditorOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  /**
   * Bumped after a save so the remark queue is RE-READ rather than recomputed
   * from memory: the version each remark speaks of has just moved, and that
   * mark is the evidence the remark was answered.
   */
  const [remarksReload, setRemarksReload] = useState(0);
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (!scope || !objectId.trim()) {
      setState({ status: "error", message: "Select a project and a context object." });
      return;
    }
    const generation = ++generationRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const timeoutId = window.setTimeout(() => controller.abort(), CONTEXT_REQUEST_TIMEOUT_MS);
    setState({ status: "loading" });
    const base = kind === "topic" ? "/api/context/topics" : "/api/context/procedures";
    try {
      const detailResponse = await apiFetch(
        `${base}/${encodeURIComponent(objectId)}?project_id=${encodeURIComponent(scope)}`,
        { signal: controller.signal, cache: "no-store" },
      );
      if (!detailResponse.ok) {
        // `readSurfaceFailure` already TELLS THEM APART (401/403/404 -> denied)
        // and this page threw the distinction away. A denial is not a failure
        // to retry: it is an answer.
        const failure = await readSurfaceFailure(detailResponse);
        if (generation === generationRef.current) {
          setState(
            failure.kind === "denied"
              ? { status: "denied", message: failure.message }
              : { status: "error", message: failure.message },
          );
        }
        return;
      }
      const object = kind === "topic"
        ? parseTopic(await detailResponse.json(), scope)
        : parseProcedure(await detailResponse.json(), scope);
      let versions: ContextVersion[] | null = null;
      let versionsError: string | null = null;
      // A pinned address reads the ledger even before the tab does: the version
      // it names is resolved against the recorded ones, never against the object
      // in front of us.
      if (activeTab === "versions" || versionId) {
        try {
          const versionsResponse = await apiFetch(
            `${base}/${encodeURIComponent(objectId)}/versions?project_id=${encodeURIComponent(scope)}`,
            { signal: controller.signal, cache: "no-store" },
          );
          if (!versionsResponse.ok) {
            versionsError = (await readSurfaceFailure(versionsResponse)).message;
          } else {
            versions = parseVersionsEnvelope(await versionsResponse.json(), kind);
          }
        } catch (error) {
          if (controller.signal.aborted) throw error;
          versionsError = errorMessage(error);
        }
      }
      if (!controller.signal.aborted && generation === generationRef.current) {
        setState({ status: "ready", object, versions, versionsError });
      }
    } catch (error) {
      if (generation !== generationRef.current) return;
      if (controller.signal.aborted) {
        setState({ status: "error", message: "The context object request timed out." });
      } else {
        setState({ status: "error", message: errorMessage(error) });
      }
    } finally {
      window.clearTimeout(timeoutId);
    }
  }, [activeTab, kind, objectId, scope, versionId]);

  useEffect(() => {
    void load();
    return () => { generationRef.current += 1; abortRef.current?.abort(); };
  }, [load]);

  const canWrite = state.status === "ready" && state.object.capabilities?.can_write === true;

  /**
   * ONE write path for both kinds: the same PATCH the collection screen calls,
   * with `expected_version` read off the object this page is showing. A save
   * that omitted it would let a workbench overwrite a version it never read.
   */
  const patchObject = useCallback(async (body: Record<string, unknown>) => {
    if (state.status !== "ready") return;
    const base = kind === "topic" ? "/api/context/topics" : "/api/context/procedures";
    setSaving(true);
    setSaveError(null);
    try {
      const response = await apiFetch(
        `${base}/${encodeURIComponent(objectId)}?project_id=${encodeURIComponent(scope)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_version: state.object.version_number, ...body }),
        },
      );
      if (!response.ok) {
        const failure = await readSurfaceFailure(response);
        // A 409 is not a failed edit, it is a stale baseline: the drawer keeps
        // the draft, the page reloads the version that won, and the message
        // says both. Closing the drawer here would throw away the correction
        // the person came to make.
        if (response.status === 409) {
          await load();
          setSaveError(`${failure.message} The latest version was reloaded; your draft was preserved.`);
          return;
        }
        setSaveError(failure.message);
        return;
      }
      await load();
      setRemarksReload((n) => n + 1);
      setEditorOpen(false);
    } catch (error) {
      setSaveError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }, [kind, load, objectId, scope, state]);

  const saveSkill = useCallback((value: SkillEditorSaveValue) => {
    if (state.status !== "ready" || !("frontmatter_yaml" in state.object)) return;
    void patchObject({
      frontmatter_yaml: updateSkillFrontmatter(state.object.frontmatter_yaml, {
        name: value.name,
        description: value.description,
        toolBindings: value.toolBindings,
        mdmTags: value.mdmTags,
        standard: value.standard,
      }),
      body_md: value.bodyMd,
      owner: value.owner.trim() || null,
    });
  }, [patchObject, state]);

  const title = state.status === "ready"
    ? ("title" in state.object ? state.object.title : state.object.name)
    : "Context object";

  /**
   * WHOSE OBJECT THIS IS — the fact both collections show and this workbench
   * dropped.
   *
   * `GET /api/context/topics` and `…/procedures` return the platform-scope rows
   * (`project_id: null`) beside this project's own, and both list screens badge
   * the first kind `Platform` (`KnowledgeBasePage.tsx:845`,
   * `Procedures.tsx:850`). Opening one lost the badge: a person editing a
   * definition every project reads could not tell it from one only this project
   * reads, and the two edits do not have the same consequence.
   *
   * The word is the LIST's word. A workbench that renamed the same fact would
   * make an operator learn it twice.
   */
  const isPlatform = state.status === "ready" && state.object.project_id === null;
  const owner = state.status === "ready" ? (state.object.owner ?? "").trim() : "";

  /**
   * THE VERSION THIS ADDRESS PINS, RESOLVED AGAINST THE LEDGER — and only
   * against the ledger.
   *
   * `pinnedUnknown` is a state of its own on purpose. Showing the current
   * version at an address that names another one is the exact failure a pinned
   * address exists to prevent, and it is what `README.md` invariant 7 and the
   * story's own "no `latest`, no silent fall back to a current version" forbid.
   * A version the ledger does not hold is said, not substituted.
   */
  const versions = state.status === "ready" ? state.versions : null;
  const pinnedVersion = versionId && versions
    ? versions.find((version) => versionAddress(version.version_number) === versionId) ?? null
    : null;
  const pinnedUnknown = Boolean(versionId) && versions !== null && pinnedVersion === null;

  return (
    <div>
      <nav className="module-breadcrumb" aria-label="Breadcrumb">
        {/* THE PRODUCT NOUN, NOT THE ROUTE TOKEN. `kind` is the delivered token
            (`topic` / `procedure`) that the wire and the DB still carry until
            the rename of `alignment-register.md` item 4 lands; it is a column
            value, and it was being interpolated straight into two sentences a
            person reads. `glossary.md` reserves `Procedure` as a token-only
            word: the object is a Skill. */}
        <button className="crumb-link" type="button" onClick={onBack}>Back to {kind === "topic" ? "Knowledge" : "Skills"}</button>
      </nav>
      {/* THE GESTURE THIS PAGE SHOWS, ON THIS PAGE. The workbench rendered the
          content and sent anyone who wanted to change a word back to the
          collection. The button is rendered only when the SERVER said this
          caller may write (the detail route answers that now, as the list
          route always did) — an edit the server will refuse is not an offer. */}
      <PageHeader
        eyebrow={state.status === "ready" ? (isPlatform ? "Platform" : "This project") : undefined}
        title={title}
        description={
          state.status === "ready"
            ? isPlatform
              ? `A ${noun} kept with its history and read by every project on this platform.`
              : `A ${noun} this project has agreed on, kept with its history.`
            : undefined
        }
        actions={
          canWrite ? (
            <Button
              variant="secondary"
              type="button"
              data-testid="context-object-edit"
              onClick={() => { setSaveError(null); setEditorOpen(true); }}
            >
              Edit this {kind === "topic" ? "Knowledge item" : "Skill"}
            </Button>
          ) : undefined
        }
      />
      {/* WHO ANSWERS FOR IT, and — when nobody may change it — WHY, with the
          gesture that repairs. The Edit control simply vanished when the server
          said `can_write: false`, so a reader saw a workbench with nothing to
          do on it and no reason given. The two refusals are not the same
          refusal, and neither is retried by clicking again. */}
      {/* WHO ANSWERS FOR IT, and — when nobody may change it — WHY, with the
          gesture that repairs. The Edit control simply vanished when the server
          said `can_write: false`, so a reader saw a workbench with nothing to
          do on it and no reason given. The two refusals are not the same
          refusal, and neither is retried by clicking again. */}
      {state.status === "ready" && (
        <p className="mt-0 mb-3 text-label text-text-secondary" data-testid="context-object-stewardship">
          {owner
            ? `Owner: ${owner}.`
            : `No owner named — name one the next time this ${noun} is edited.`}
          {!canWrite && (isPlatform
            ? ` It is shared by every project on this platform, and this account cannot change it from here. Ask a platform administrator.`
            : ` This account can read this ${noun} and not change it. Ask an owner of this project for edit rights.`)}
        </p>
      )}
      <nav className="mt-[14px] flex gap-2.5" aria-label="Context object tabs">
        {(["content", "usage", "versions"] as const).map((item) => (
          <Button key={item} type="button" variant={item === activeTab ? "default" : "secondary"} aria-current={item === activeTab ? "page" : undefined} onClick={() => onNavigateTab(item)}>
            {item[0].toUpperCase() + item.slice(1)}
          </Button>
        ))}
      </nav>
      {state.status === "loading" && <p role="status">Loading context object…</p>}
      {/* THE STORE FAILED — interrupting (`role="alert"`), and repaired by
          reading again. */}
      {state.status === "error" && (
        <section className={NOTICE_CLASS} role="alert" data-testid="context-object-error">
          <h2>This {noun} could not be read</h2>
          <p>{state.message} It has not been removed, and nothing here has changed.</p>
          <Button variant="secondary" type="button" onClick={() => void load()}>Retry</Button>
        </section>
      )}
      {/* THE CALLER MAY NOT READ IT — announced politely, because retrying
          changes nothing, and the gesture that repairs is asking for access.
          The sentence discloses nothing: the server answers the same for
          foreign, absent and forbidden, and so does this. */}
      {state.status === "denied" && (
        <section className={NOTICE_CLASS} role="status" aria-live="polite" data-testid="context-object-denied">
          <h2>You do not have access to this {noun}</h2>
          <p>
            {state.message} Ask an owner of this project for access to the {kind === "topic" ? "Knowledge library" : "Skills registry"},
            then open it again.
          </p>
          <Button variant="secondary" type="button" onClick={onBack}>
            Back to {kind === "topic" ? "Knowledge" : "Skills"}
          </Button>
        </section>
      )}
      {/* THE OBJECT IS RETIRED — no longer current, and still readable. A
          banner, not a wall: hiding an archived object's content would lose the
          very evidence a reader came for, and its versions are intact. */}
      {state.status === "ready" && state.object.status === "archived" && (
        <section className={STALE_CLASS} role="status" aria-live="polite" data-testid="context-object-stale">
          <h2>This {noun} was retired</h2>
          <p>
            It is no longer current, and nothing was destroyed: its versions and its
            relations are kept. Turn on <strong>Show archived</strong> in the{" "}
            {kind === "topic" ? "Knowledge library" : "Skills registry"} and restore it there to
            make it current again.
          </p>
        </section>
      )}
      {state.status === "ready" && activeTab === "content" && (
        <section className={`${PANEL_CLASS} ${PREVIEW_CLASS}`}>
          <h2>Content</h2>
          {/* Une Skill se LIT comme elle est ecrite : la sequence standardisee
              d'abord (etapes, acceptance, erreurs, references MDM), la prose
              ensuite. SkillStepList ne vivait que dans l'aperçu de l'editeur --
              les etapes n'etaient visibles en lecture nulle part. Il rend null
              quand rien n'est standardise : le repli est le body seul. */}
          {kind === "procedure" && "frontmatter_yaml" in state.object && (
            <SkillStepList
              frontmatterYaml={state.object.frontmatter_yaml}
              mdmReferences={state.object.mdm_references}
            />
          )}
          <RawMarkdown text={state.object.body_md} placeholder="No persisted content." />
          <p>Version v{state.object.version_number} · {state.object.status}</p>
        </section>
      )}
      {/* AI-158 : ce qu'on reproche a cet objet, la ou on le lit.
          Le panneau ne vivait que dans le tiroir du mindmap -- or on ne
          travaille pas une Skill dans un mindmap, on l'ouvre sur son etabli, et
          l'etabli n'en disait rien. Sur l'onglet `content` parce que la
          remarque parle du contenu, et a une VERSION du contenu : la ligne
          « Version v… » juste au-dessus est ce que « older version » compare. */}
      {state.status === "ready" && activeTab === "content" && (
        <NodeRemarks
          projectId={scope}
          nodeId={objectId}
          nodeType={kind}
          nodeVersion={state.object.version_number}
          className={PANEL_CLASS}
          testIdPrefix="context-review"
          reloadToken={remarksReload}
          /* The remark asks for a correction; this opens the editor that makes
             one. Same editor as the header button — the queue does not get a
             second, quieter write path of its own. */
          onAdjust={() => { setSaveError(null); setEditorOpen(true); }}
          adjustDisabled={!canWrite}
        />
      )}
      {/* READ HERE, WRITTEN ON THE GRAPH — and read from the ONE authority.
          `GraphEdges` used to be mounted on this tab while declaring itself
          superseded since 2026-07-31. What replaced it still read every edge of
          the project in the browser; since story 49-6 lot 3 the facet is the
          server's own bounded projection over `app.context_relationships`, so a
          Semantic View, a metric and a Datastream peer are visible at last. */}
      {state.status === "ready" && activeTab === "usage" && kind === "procedure" && (
        <SkillWalksPanel projectId={scope} procedureId={objectId} />
      )}
      {state.status === "ready" && activeTab === "usage" && (
        <section className={PANEL_CLASS}>
          <h2>Used by / Related</h2>
          <GraphLinks
            nodeId={objectId}
            nodeType={kind}
            projectId={scope}
            onOpenGraph={onOpenGraph}
            onOpenPeer={onOpenPeer}
          />
        </section>
      )}
      {state.status === "ready" && activeTab === "versions" && (
        <section className={PANEL_CLASS}>
          <h2>Immutable versions</h2>
          {/* THE ADDRESS NAMES A VERSION THE LEDGER DOES NOT HOLD. Its own
              answer, and nothing is opened in its place — a workbench that fell
              back to the current version here would answer a pinned question
              with an unpinned fact. */}
          {pinnedUnknown && (
            <section className={NOTICE_CLASS} role="alert" data-testid="context-version-unknown">
              <h3>This address pins version v{versionId}</h3>
              <p>
                This {noun} has {versions?.length ?? 0} recorded version
                {(versions?.length ?? 0) === 1 ? "" : "s"} and none of them is v{versionId}.
                Nothing was opened in its place. Choose one below, or open the {noun} as it
                stands today.
              </p>
              {onOpenVersion && (
                <Button variant="secondary" type="button" onClick={() => onOpenVersion(null)}>
                  Open this {noun} as it stands
                </Button>
              )}
            </section>
          )}
          {/* THE PINNED VERSION, OPEN. Immutable, so it says so: what is on
              screen is the snapshot as it was recorded, not the object as it is
              now — and the way back to the object as it is now is right here. */}
          {pinnedVersion && (
            <section data-testid="context-version-pinned">
              <h3>Version v{pinnedVersion.version_number}, pinned by this address</h3>
              <p>
                Recorded by {pinnedVersion.changed_by} on{" "}
                <Timestamp value={pinnedVersion.changed_at} />. It cannot change.
                {state.object.version_number !== pinnedVersion.version_number
                  ? ` This ${noun} has since moved to v${state.object.version_number}.`
                  : ` It is also the current version of this ${noun}.`}
              </p>
              <VersionSnapshot version={pinnedVersion} kind={kind} />
              {onOpenVersion && (
                <Button variant="secondary" type="button" onClick={() => onOpenVersion(null)}>
                  Back to this {noun} as it stands
                </Button>
              )}
            </section>
          )}
          {state.versionsError ? (
            <p role="alert">
              {state.versionsError}
              {versionId ? ` The version this address pins (v${versionId}) could not be resolved, and it is kept in the address.` : ""}
            </p>
          ) : state.versions && state.versions.length > 0 ? (
            <>
            {/* ONE version is a FACT about this object, not a shortage: it has
                been written once and nothing has replaced it. Saying nothing
                here let a first version read like a truncated list. */}
            {state.versions.length === 1 && (
              <p data-testid="context-versions-first">
                This is the first version. Nothing has replaced it yet — every
                later edit is added here and none of them overwrite this one.
              </p>
            )}
            <ol>
              {state.versions.map((version) => {
                /* EACH ROW IS AN ADDRESS. It used to be a disclosure widget: the
                   snapshot opened in place and nothing could be copied, shared,
                   opened in a tab or referenced by an owner link. `buildPath`
                   composes it — this file never writes a URL — and the plain
                   click is intercepted so the console navigates instead of
                   reloading, exactly as the Governance ledger does. Modifier and
                   middle clicks are left to the browser. */
                const address = versionAddress(version.version_number);
                const href = versionHref?.(address);
                const current = address === versionId;
                const label = (
                  <>
                    {/* One instant, one rendering — the console's `Timestamp`,
                        which keeps the exact instant in `dateTime` and names
                        the zone. The raw ISO string was being printed. */}
                    <strong>v{version.version_number}</strong> · {version.changed_by} ·{" "}
                    <Timestamp value={version.changed_at} /> · {version.status}
                  </>
                );
                return (
                  <li key={version.version_number} data-selected={current ? "true" : undefined}>
                    {onOpenVersion ? (
                      <a
                        className="underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                        href={href ?? undefined}
                        aria-current={current ? "page" : undefined}
                        onClick={(event) => {
                          if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                          event.preventDefault();
                          onOpenVersion(address);
                        }}
                      >
                        {label}
                      </a>
                    ) : (
                      /* No navigation was handed to this workbench, so the row
                         states the fact and offers no control. It is NOT a
                         second way to read a snapshot: a version is read at its
                         address, and only there. */
                      label
                    )}
                  </li>
                );
              })}
            </ol>
            </>
          ) : (
            /* TWO DIFFERENT FACTS, and one sentence used to serve both. An
               object on screen has been written at least once, so its ledger
               carries at least v1: an empty answer is a ledger that could not
               be read, never an object without history. Saying "no version
               history was returned" made a healthy v1 object read as broken and
               a broken read as normal. */
            <p role="alert" data-testid="context-versions-unreadable">
              The version ledger returned no row for an object that is at v
              {state.object.version_number}. Its history could not be read — this is not an object
              without one. Reopen this tab, and report it if the list stays empty.
            </p>
          )}
        </section>
      )}
      {/* The Skill's OWN editor, the one the collection mounts. Not a copy:
          same component, same frontmatter writer, same discard guard — so a
          Skill saved from here and a Skill saved from the list are the same
          save. */}
      {editorOpen && state.status === "ready" && kind === "procedure" && "frontmatter_yaml" in state.object && (
        <SkillEditorDrawer
          projectId={scope}
          initial={{
            mode: "edit",
            procedureId: state.object.id,
            name: state.object.name,
            description: state.object.description,
            owner: state.object.owner ?? "",
            frontmatterYaml: state.object.frontmatter_yaml,
            bodyMd: state.object.body_md,
            mdmReferences: state.object.mdm_references,
          }}
          saving={saving}
          error={saveError}
          onClose={() => setEditorOpen(false)}
          onSave={saveSkill}
        />
      )}
      {editorOpen && state.status === "ready" && kind === "topic" && "title" in state.object && (
        <KnowledgeEditorDrawer
          initial={{
            topicId: state.object.id,
            title: state.object.title,
            owner: state.object.owner ?? "",
            bodyMd: state.object.body_md,
          }}
          saving={saving}
          error={saveError}
          onClose={() => setEditorOpen(false)}
          onSave={(value) => void patchObject({
            title: value.title,
            body_md: value.bodyMd,
            owner: value.owner.trim() || null,
          })}
        />
      )}
    </div>
  );
}

/** What a Skill's walks say about it (iteration 3, 2026-09-05). */
interface SkillWalkStep {
  step: string;
  label?: string | null;
  tool?: string | null;
  crossed: number;
  skipped: number;
  unobservable: number;
}

interface SkillWalks {
  walks: number;
  window: string;
  verdicts: Record<string, number>;
  versions: Record<string, number>;
  steps: SkillWalkStep[];
  most_skipped: SkillWalkStep[];
  recent: { path_id: string; started_at?: string | null; verdict?: string | null; skill_version?: string | null; crossed?: string[]; skipped?: string[] }[];
}

/**
 * THE SKILL READS ITS OWN WALKS. Jean, 2026-09-05: « améliorer la production et le
 * suivi d'insights cohérents ». A step skipped nine times in ten is either
 * unrealistic or a habit of the model to correct; only the ranking tells the
 * author which. Derived by the server from the recorded paths, bounded to the
 * last twenty walks, never stored. An unreadable history says so; an empty one
 * names the gesture that fills it.
 */
export function SkillWalksPanel({ projectId, procedureId }: { projectId: string; procedureId: string }) {
  const [walks, setWalks] = useState<SkillWalks | null | "unavailable">(null);
  useEffect(() => {
    let cancelled = false;
    apiFetch(
      `/api/projects/${encodeURIComponent(projectId)}/context/ai-paths/skills/${encodeURIComponent(procedureId)}/walks`,
    )
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) {
          setWalks("unavailable");
          return;
        }
        setWalks((await res.json()) as SkillWalks);
      })
      .catch(() => {
        if (!cancelled) setWalks("unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, procedureId]);
  return (
    <section className={PANEL_CLASS} data-testid="skill-walks">
      <h2>Walks of this Skill</h2>
      {walks === null && <p>Reading the recorded walks…</p>}
      {walks === "unavailable" && (
        <p data-testid="skill-walks-unavailable">
          The walks of this Skill could not be read. This is not an empty history.
        </p>
      )}
      {walks !== null && walks !== "unavailable" && walks.walks === 0 && (
        <p data-testid="skill-walks-empty">
          No interaction has taken this Skill yet. A walk appears here the first time an agent
          asks for this procedure and calls one of the tools its steps name.
        </p>
      )}
      {walks !== null && walks !== "unavailable" && walks.walks > 0 && (
        <>
          <p data-testid="skill-walks-summary">
            {walks.window}:{" "}
            {Object.entries(walks.verdicts)
              .map(([verdict, count]) => `${count} ${verdict}`)
              .join(", ")}
            {Object.keys(walks.versions).length > 1
              ? ` · across ${Object.keys(walks.versions).length} versions`
              : ""}
            .
          </p>
          {walks.most_skipped.length > 0 && (
            <p data-testid="skill-walks-most-skipped">
              Most skipped: {walks.most_skipped.map((s) => `step ${s.step} (${s.skipped}×)`).join(", ")}.
            </p>
          )}
          <table>
            <thead>
              <tr>
                <th>Step</th>
                <th>Prescribed</th>
                <th>Tool</th>
                <th>Crossed</th>
                <th>Skipped</th>
                <th>Not observable</th>
              </tr>
            </thead>
            <tbody>
              {walks.steps.map((s) => (
                <tr key={s.step} data-testid={`skill-walk-step-${s.step}`}>
                  <td>{s.step}</td>
                  <td>{s.label ?? "—"}</td>
                  <td>{s.tool ?? "—"}</td>
                  <td>{s.crossed}</td>
                  <td>{s.skipped}</td>
                  <td>{s.unobservable}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <ul data-testid="skill-walks-recent">
            {walks.recent.map((w) => (
              <li key={w.path_id}>
                {w.path_id}
                {w.started_at ? ` · ${w.started_at.slice(0, 16)}` : ""}
                {w.verdict ? ` · ${w.verdict}` : ""}
                {w.skipped && w.skipped.length > 0 ? ` · skipped ${w.skipped.join(", ")}` : ""}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
