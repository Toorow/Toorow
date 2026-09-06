/**
 * An address this console offers is an address this console can open.
 *
 * `parsePath` refuses everything whose first segment is not `org`, `account` or
 * `platform` (`router.tsx:118-130`), and everything naming a workspace, section,
 * object type or tab the registry does not declare. An `<a href>` carrying such
 * an address is a control that looks live, is reported by the screen as a way
 * forward, and lands on the unknown-route screen with no clue as to why.
 *
 * The console has now paid for that five times, always by writing a second
 * address grammar beside the router's:
 *
 *     /project/{p}/data/…            five Data lenses    (data_surface.py, 2026-08-03)
 *     /projects/{id}/…               wizard owner links  (story 57.4)
 *     /data/datastreams/o/{id}/…     wizard success link (story 57.5)
 *     /p/{id}/…                      Workbench tab band  (story 57.5)
 *     /data/connectors?id=…          Sources connector column, /p/{id}/… in the
 *     /sources/{acct}                Workbench sandbox and its header fixture
 *                                                        (AI-218, this file)
 *
 * WHY THE GUARD OF 57.4 SAW NONE OF IT. It looked for `a[href^="/projects/"]` on
 * one screen: it pinned the ONE prefix that had just been removed, on the ONE
 * screen it had been removed from. Every address above uses a different prefix,
 * and four of them are on other screens. A guard written against an instance
 * cannot catch a class.
 *
 * So this file asks the property instead, twice and from both ends:
 *
 *   - over the SOURCE, so an address written into a file is caught whether or
 *     not any test renders that file;
 *   - over the DOM of rendered screens, so an address that arrives as DATA — a
 *     server link, a stored `owner_href`, a sandbox fixture — is caught too. No
 *     source guard can see those, and three of the five above were data.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import Sources from "../shell/pages/Sources";
import DatastreamWorkbenchRoute from "../datastreams/workbench/DatastreamWorkbenchRoute";
import DatastreamWorkbenchSandbox from "../shell/DatastreamWorkbenchSandbox";
import { WORKSPACES } from "../shell/navigation";
import { parsePath } from "../shell/router";

const CONSOLE_SRC = resolve(__dirname, "..");
const REPO = resolve(__dirname, "..", "..", "..", "..");

/** Chaque `src/` de paquet front, derive de l'endroit ou vit un `package.json`.
 *
 *  2026-08-22, story 67.14 -- ET C'EST LA LECON DE CE FICHIER, APPLIQUEE A
 *  LUI-MEME. Son en-tete dit : << a guard written against an instance cannot
 *  catch a class >>, et il balayait `ui/admin/src` seul, un arbre front sur
 *  quinze. Les adresses ne sont pas ecrites la seule : `ui/cards/shell` dessine
 *  l'AI Path et compose des liens de proprietaire, les widgets en composent
 *  aussi. Un garde qui nomme la classe et se scope a l'instance est le meme
 *  defaut, une couche plus haut -- exactement celui que sa garde soeur
 *  `ScreensDoNotSpeakTheDatabase` a paye le 2026-08-21 (143 fichiers passaient
 *  dessous).
 *
 *  La forme du balayage est celle de `ScreensDoNotPrintIdentifiersAsProse`, pas
 *  une seconde : un paquet sans `src/` tombe tout seul, donc aucun paquet n'est
 *  nomme et un nouveau entre sans edition ici. */
const FRONT_TREES = ["ui", "web"];

function frontRoots(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = resolve(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "node_modules" || entry === "dist") continue;
      frontRoots(full, out);
      continue;
    }
    if (entry !== "package.json") continue;
    const src = resolve(full, "..", "src");
    try {
      if (statSync(src).isDirectory()) out.push(src);
    } catch {
      // Un paquet sans `src/` ne rend rien et n'est lu par personne.
    }
  }
  return out;
}

const FRONT_ROOTS = FRONT_TREES.flatMap((tree) => frontRoots(resolve(REPO, tree))).sort();

/** Les fichiers que le balayage LIT, pour que sa portee soit mesurable. */
function filesUnder(root: string, out: string[] = []): string[] {
  for (const entry of readdirSync(root)) {
    const full = resolve(root, entry);
    if (statSync(full).isDirectory()) {
      if (entry !== "__tests__" && entry !== "node_modules") filesUnder(full, out);
      continue;
    }
    if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

const scannedFiles = () => FRONT_ROOTS.flatMap((root) => filesUnder(root));
const adminFiles = () => filesUnder(CONSOLE_SRC);

/** LE BALAYAGE DOIT BALAYER, et il faut le dire ici plutot que l'esperer.
 *
 *  Un `frontRoots` casse rendrait une liste vide, `literalAddresses()` rendrait
 *  zero adresse, et chaque assertion de ce fichier passerait en ne mesurant
 *  rien -- une garde verte parce qu'elle ne regarde nulle part. Le compte est
 *  donc verifie, et il porte le paquet dont l'absence a motive l'elargissement.
 */
it("scans every front package, not the console alone", () => {
  expect(FRONT_ROOTS.length).toBeGreaterThan(5);
  expect(FRONT_ROOTS.some((root) => root.includes("cards"))).toBe(true);
  expect(FRONT_ROOTS.some((root) => root.endsWith(CONSOLE_SRC))).toBe(true);
  // Et il LIT VRAIMENT hors de la console : une liste de racines correcte
  // au-dessus d'un lecteur casse balaierait quinze arbres sans rien y voir.
  // Mesure du 2026-08-22 : 44 litteraux `*href` dans `ui/admin/src`, 46 sur les
  // quinze arbres -- le compte de fichiers lus est donc la preuve qui tient,
  // pas le compte d'adresses (deux seulement passent le filtre `/`, et un
  // paquet peut legitimement n'en composer aucune).
  expect(scannedFiles().length).toBeGreaterThan(adminFiles().length);
});
/** Where the OTHER composer of console addresses lives. Half the addresses this
 *  console has shipped and could not open were written in Python. */
const SERVER_CORE = resolve(__dirname, "../../../..", "server/core");

/** The router's own answer, for an address written as one string. */
function resolves(address: string): boolean {
  const index = address.indexOf("?");
  const pathname = index === -1 ? address : address.slice(0, index);
  const search = index === -1 ? "" : address.slice(index);
  return parsePath(pathname, search).kind === "resolved";
}

/** Every `href`-bearing literal in the console's own source.
 *
 *  Any key ENDING in `href`, not just the JSX attribute: `owner_href` and
 *  `resume_href` are fixture fields that are rendered straight into an anchor,
 *  and both have shipped addresses the router refuses. A guard that only read
 *  `href=` would have missed both.
 *
 *  A `${…}` hole becomes `x`. What is being judged is the SHAPE of the address —
 *  `/org/{org}/project/{p}/data/sources` resolves whatever the ids are, and
 *  `/data/connectors` is refused whatever they are. */
function literalAddresses(): Array<{ file: string; value: string }> {
  const found: Array<{ file: string; value: string }> = [];
  // `[hH]ref`: a camelCase key such as `targetHref` (DatastreamIssueBadge, found
  // 2026-08-30) is an address too, and a lower-case-only sweep never saw it.
  const pattern = /([A-Za-z_]*[hH]ref)\s*[:=]\s*\{?\s*(`[^`]*`|"[^"]*")/g;
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = resolve(dir, entry);
      if (statSync(full).isDirectory()) {
        // Tests carry deliberately refused addresses as fixtures — that is what
        // the two DOM cases below are FOR — so the source guard reads production
        // files only.
        if (entry !== "__tests__" && entry !== "node_modules") walk(full);
        continue;
      }
      if (!/\.tsx?$/.test(entry) || /\.test\.tsx?$/.test(entry)) continue;
      const body = readFileSync(full, "utf-8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/\/\/.*/g, "");
      for (const match of body.matchAll(pattern)) {
        const value = match[2].slice(1, -1).replace(/\$\{[^}]*\}/g, "x");
        // A fragment, a query-only address, an external URL and an API endpoint
        // are not this router's business; a leading `/` is what claims to be a
        // console address.
        if (!value.startsWith("/") || value.startsWith("/api/")) continue;
        // The vitrine (`web/`) has a router of its own (Astro pages): only an
        // address under a CONSOLE root claims to be this router's business
        // there, and a trailing `/` is the vitrine's own convention, never
        // ours (2026-08-30, the widened key match met `/connectors/x/`).
        const tree = full.slice(REPO.length + 1).split(/[\\/]/)[0];
        if (tree === "web" && (value.endsWith("/") || !CONSOLE_ROOTS.has(value.slice(1).split("/")[0]))) continue;
        found.push({ file: full.slice(REPO.length + 1), value });
      }
    }
  };
  for (const root of FRONT_ROOTS) walk(root);
  return found;
}

/** The first segment of an address that CLAIMS to be a console address.
 *
 *  A server file composes provider endpoints as well — `/crm/v3/objects/…`,
 *  `/userprofiles/{p}/reportData/query` — and those are nobody's console route.
 *  What is judged is the set of first segments the console itself owns, so a
 *  string claiming one of them is claiming to be openable here. `/project_id`
 *  (a JSON-Pointer in a validation error) is not `/project`: the whole segment
 *  has to match. */
const CONSOLE_ROOTS = new Set([
  "org", "orgs", "account", "platform", "p", "project", "projects",
  ...WORKSPACES.map((workspace) => workspace.slug as string),
  "settings", "getting-started", "sources", "datastreams",
]);

const HOLE = " ";

/**
 * Fill an interpolation hole with a value that is legal WHERE IT STANDS.
 *
 * An id can be anything, so `x` judges it fine. A workspace, a section, an
 * object type or a tab cannot: those are checked against the registry, and
 * substituting `x` would make every address carrying a computed section look
 * refused — `data_surface.py` composes exactly that, and it is the one call site
 * whose repair (2026-08-03) this guard exists to hold.
 *
 * So the hole is filled from the registry, by POSITION in the grammar. What is
 * being judged is then the address's SKELETON: its root, and whether the
 * `object` and `tab` literals sit where the router requires them — which is
 * precisely what was wrong in all four cases this repository has paid for.
 */
function fillHoles(address: string): string {
  const parts = address.slice(1).split("/");
  const workspaceAt = parts[0] === "org" && parts[2] === "project" ? 4 : -1;
  const pick = (index: number): string | null => {
    if (workspaceAt === -1) return null;
    const declared = parts[workspaceAt];
    const workspace = declared === HOLE
      ? WORKSPACES[0]
      : WORKSPACES.find((candidate) => candidate.slug === declared);
    if (!workspace) return null;
    if (index === workspaceAt) return workspace.slug;
    // A section standing in for a hole must be able to CARRY the rest of the
    // address: `data`'s first section declares no object, and picking it made a
    // perfectly good `/object/{type}/{id}/tab/{tab}` address look refused.
    const carriesObject = parts[workspaceAt + 2] === "object";
    const sectionSlug = parts[workspaceAt + 1] === HOLE
      ? (carriesObject
          ? workspace.subnav.find((candidate) => candidate.objects.length > 0)
          : workspace.subnav[0])?.slug
      : parts[workspaceAt + 1];
    const section = workspace.subnav.find((candidate) => candidate.slug === sectionSlug);
    if (!section) return null;
    if (index === workspaceAt + 1) return section.slug;
    // `/object/{type}/{id}` and `/tab/{tab}` hang off the section.
    const declaredType = parts[workspaceAt + 3];
    const object = declaredType === HOLE
      ? section.objects[0]
      : section.objects.find((candidate) => candidate.type === declaredType);
    if (!object) return null;
    if (carriesObject && index === workspaceAt + 3) return object.type;
    if (parts[workspaceAt + 5] === "tab" && index === workspaceAt + 6) {
      return object.defaultTab ?? object.tabs?.[0] ?? null;
    }
    return null;
  };
  return "/" + parts.map((part, index) => (part === HOLE ? pick(index) ?? "x" : part)).join("/");
}

/**
 * Every address `server/core` composes that claims to be a console address.
 *
 * Adjacent literals are JOINED first: Python splits a long f-string across two
 * lines, and reading the halves separately reports `/org/{o}/project/{p}` — a
 * legitimate prefix — as a refused address. Docstrings and full-line comments go
 * first, because both quote the old broken addresses on purpose so the next
 * reader knows what changed; a matcher that reads the explanation as the defect
 * is a trap this repository has already fallen into twice.
 */
function serverAddresses(): Array<{ file: string; value: string }> {
  const found: Array<{ file: string; value: string }> = [];
  const literal = /(?:[fbru]{0,2})("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')/g;
  for (const entry of readdirSync(SERVER_CORE)) {
    if (!entry.endsWith(".py")) continue;
    const body = readFileSync(resolve(SERVER_CORE, entry), "utf-8")
      .replace(/"""[\s\S]*?"""/g, '""')
      .replace(/'''[\s\S]*?'''/g, "''")
      .replace(/^[ \t]*#.*$/gm, "");
    let pending = "";
    let pendingEnd = -1;
    const flush = () => {
      // A value ending in `/` is a PREFIX being tested (`startswith("/org/")`),
      // never an address: the router refuses a trailing slash outright.
      const value = pending.replace(/\{[^}]*\}/g, HOLE);
      const root = value.slice(1).split("/")[0];
      if (value.startsWith("/") && !value.endsWith("/") && CONSOLE_ROOTS.has(root)) {
        found.push({ file: entry, value: fillHoles(value) });
      }
      pending = "";
      pendingEnd = -1;
    };
    for (const match of body.matchAll(literal)) {
      const start = match.index ?? 0;
      const text = match[1].slice(1, -1);
      const joins = pendingEnd !== -1 && /^\s*$/.test(body.slice(pendingEnd, start));
      if (!joins && pending) flush();
      pending = joins ? pending + text : text;
      pendingEnd = start + match[0].length;
    }
    if (pending) flush();
  }
  return found;
}

/** Every address the rendered document offers, judged by the router. */
function unresolvableAddresses(): string[] {
  return Array.from(document.querySelectorAll("a[href]"))
    .map((anchor) => anchor.getAttribute("href") ?? "")
    .filter((href) => href.startsWith("/") && !href.startsWith("/api/"))
    .filter((href) => !resolves(href));
}

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("composes no address in its own source that its router refuses", () => {
  const refused = literalAddresses().filter(({ value }) => !resolves(value));
  expect(refused).toEqual([]);
});

it("is sent no address by the server that its router refuses", () => {
  // The server composes console addresses too, and it is where four of them
  // were written: `/projects/{p}/datastreams/{ds}/first-report/rendered`
  // (`first_report_render.py`, AI-218) and the five Data lens links repaired on
  // 2026-08-03. Guarding only the console would leave the class open on the side
  // that has produced most of it.
  const addresses = serverAddresses();
  // The scan itself has to be alive: a regex that silently stops matching would
  // make this test pass by seeing nothing at all.
  expect(addresses.length).toBeGreaterThan(8);
  expect(addresses.filter(({ value }) => !resolves(value))).toEqual([]);
});

function stubSourcesFetch() {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response({
    schema_version: "data-sources.v1",
    project_ref: { object_type: "project", id: "p1" },
    generated_at: "2026-07-29T10:00:00Z",
    evidence_as_of: "2026-07-29T09:00:00Z",
    items: [{
      object_ref: { object_type: "source-account", id: "sacct_1" },
      label: "Analytics account",
      connector_ref: { object_type: "connector", id: "connector_x" },
      authorization_ref: { owner_scope: "organization" },
      states: { authorization: "authorized", availability: "available", freshness: "current", usage: "used" },
      evidence: { used_by_count: 2 },
      evidence_as_of: "2026-07-29T09:00:00Z",
      links: {},
    }],
    unavailable_reasons: [],
    allowed_actions: [],
  }))));
}

it("offers no address on Sources that its router refuses", async () => {
  stubSourcesFetch();
  const openConnector = vi.fn();

  render(<Sources projectId="p1" onOpenConnector={openConnector} />);

  // The Connector column is still a way in — it goes through the mount's
  // resolver instead of composing `/data/connectors?id=…`, which the router
  // refused on its first segment.
  fireEvent.click(await screen.findByRole("button", { name: "connector_x" }));
  expect(openConnector).toHaveBeenCalledWith("connector_x");
  expect(unresolvableAddresses()).toEqual([]);
});

it("names the Connector without offering it when the mount passes no resolver", async () => {
  // The other half of the `ObjectNav` rule: no address AND no callback is inert
  // text, never a link — and never a disappeared column either, because which
  // Connector serves an account is the reading of the cell.
  stubSourcesFetch();

  render(<Sources projectId="p1" />);

  expect(await screen.findByText("connector_x")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "connector_x" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "connector_x" })).not.toBeInTheDocument();
});

it("offers no address on the Workbench that its router refuses, whatever the header carries", async () => {
  // A header composed the way `/p/{project}/…` used to be composed, plus a
  // stored `owner_href` of the same shape. Both are addresses the SERVER can
  // send, so the screen has to survive them without offering a dead link.
  const header = {
    schema: "datastream_workbench.header.v1",
    identity: { datastream_id: "ds_1", project_id: "project_1", name: "Orders", mode: "connector_pull", data_role: "fact", owner: "owner@example.com", module: "shopify" },
    axes: { lifecycle: "Active", configuration: "Ready", operations: "Healthy", publication: "Current" },
    versions: { active_plan: "plan_1", active_mapping: "map_1", proposed_plan: null, proposed_mapping: null },
    operations_evidence: { next_run_at: null, missed_run_count: 0, schedule_state_known: true, late_reasons: [] },
    runs: { latest: "run_1", latest_state: "published" },
    publications: { candidate: null, current: "run_1", last_known_good: "run_0" },
    links: { source: "/p/project_1/data/sources", project_settings: "/settings", governance: "/governance" },
    primary_action: { kind: "prepare_change", label: "Prepare change", reason: "Stable.", tab: "processing" },
  };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(
    String(input).endsWith("/outputs")
      ? response({
          schema: "datastream_workbench.outputs.v1",
          tab: "outputs",
          project_id: "project_1",
          datastream_id: "ds_1",
          evidence: {
            outputs: [],
            used_by: [{
              output_id: "out_1",
              output_version_id: "outv_1",
              consumer_kind: "semantic_view",
              consumer_ref: "sv_1",
              consumer_version_ref: null,
              owner_href: "/p/project_1/governance/semantic-model",
              created_at: "2026-08-01T02:20:00Z",
            }],
          },
        })
      : response(header),
  )));

  render(
    <DatastreamWorkbenchRoute projectId="project_1" datastreamId="ds_1" tab="outputs" onNavigateTab={vi.fn()} />,
  );

  // The downstream consumer is still named. Naming it is the panel's reason to
  // exist; linking it was never proven possible.
  expect(await screen.findByText("Semantic View · sv_1")).toBeInTheDocument();
  expect(unresolvableAddresses()).toEqual([]);
});

it("offers no address on the Workbench sandbox that its router refuses", async () => {
  // `/debug/screen` is the one place this design can be LOOKED at, so a dead
  // link there is a dead link in every capture and every design review. Its
  // fixture carried three of them under the state axes.
  render(<DatastreamWorkbenchSandbox />);

  await waitFor(() => expect(screen.getByRole("navigation", { name: "Datastream owners" })).toBeInTheDocument());
  expect(screen.getByRole("link", { name: "Source owner" })).toBeInTheDocument();
  expect(unresolvableAddresses()).toEqual([]);
});
