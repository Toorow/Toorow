/**
 * The media plan of a carrier Datastream — chantier 67-25, against the two
 * decisions ratified 2026-08-24 (commit `a379ec50`).
 *
 * WHAT EACH ASSERTION IS, and each one is an `Incomplete if` of a ratified
 * document rather than a nicety:
 *
 *   1. `analyze-and-test.md` — « a plan can be created or imported from a
 *      console surface that is not the carrier Datastream's Workbench, so two
 *      doors diverge ». Both gestures are HERE, and they reach the routes that
 *      already exist: `POST /api/projects/{p}/mediaplans` for the creation (with
 *      the carrier on it) and `POST /api/datastreams/{id}/imports` — the
 *      governed upload ingress every file of this Datastream already takes — for
 *      the revision. A second import path would be the second engine chantier
 *      67-25 exists to end.
 *   2. `analyze-and-test.md` — « the Workbench door imports a plan without
 *      saying which dated version it just became ». After an import the screen
 *      names the version NUMBER, its day and the fact that it is a candidate.
 *   3. `file-source-ingestion.md` — « a dated update replaces the previous
 *      version instead of landing beside it ». The versions table still holds
 *      the previous one, and the sentence says so.
 *   4. `file-source-ingestion.md` — « importing a plan provisions a Datastream
 *      the person never asked for ». Nothing here calls a Datastream-creating
 *      route, and the panel is not even drawn unless the SERVER says this
 *      Datastream carries a plan cycle.
 *
 * `fetch` is stubbed rather than `apiFetch`, like every sibling suite: the seam
 * guard is what proves the bearer is attached, and stubbing one level lower
 * would hide it.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import WorkbenchMediaPlanPanel from "../datastreams/workbench/WorkbenchMediaPlanPanel";

const PROJECT = "proj_EXAMPLE";
const DATASTREAM = "ds_EXAMPLE";

function response(body: unknown, status = 200): Response {
  return {
    ok: status < 400,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

/** The server's block for a carrier that carries NO plan yet. */
const EMPTY = {
  carrier: true,
  plan: null,
  versions: [],
  empty_message: "This Datastream carries no media plan yet",
  empty_reason:
    "Its template reads a spreadsheet as plan lines, so the file it receives becomes a media plan rather than warehouse rows.",
};

/** The server's block once a plan exists, with one dated version. */
const CARRIED = {
  carrier: true,
  plan: { id: "plan_EXAMPLE", name: "Q1 Brand", currency: "EUR", created_at: "2026-08-01T09:00:00Z" },
  versions: [
    {
      id: "mpv_ONE",
      version_number: 1,
      status: "published",
      is_active: true,
      source_note: "january.xlsx",
      created_at: "2026-08-01T09:10:00Z",
    },
  ],
  revision_reason:
    "A revision of this plan is a new file imported here: it lands BESIDE the versions below, never over them.",
  candidate_reason: "An imported version is a candidate until it is published.",
  no_version_message: "No file has been imported into this plan yet",
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("draws nothing at all on a Datastream the server does not call a carrier", () => {
  const { container } = render(
    <WorkbenchMediaPlanPanel projectId={PROJECT} datastreamId={DATASTREAM} mediaPlan={null} />,
  );
  // NOT an empty panel saying "this is not a media plan": thirty-nine
  // connectors have no relationship with plans, and a sentence nobody asked for
  // is noise on every one of their screens.
  expect(container).toBeEmptyDOMElement();
});

it("creates the plan HERE, on this Datastream, and names no other screen", async () => {
  const calls: Array<{ url: string; body: unknown }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url.includes("/workbench/data")) {
        return Promise.resolve(response({ evidence: { media_plan: CARRIED } }));
      }
      return Promise.resolve(response({ id: "plan_EXAMPLE" }, 201));
    }),
  );

  render(
    <WorkbenchMediaPlanPanel projectId={PROJECT} datastreamId={DATASTREAM} mediaPlan={EMPTY} />,
  );

  expect(screen.getByText("This Datastream carries no media plan yet")).toBeInTheDocument();
  // L'ÉTAT VIDE PORTE LE GESTE, il ne le délègue pas.
  fireEvent.change(screen.getByTestId("media-plan-name"), { target: { value: "Q1 Brand" } });
  fireEvent.click(screen.getByTestId("media-plan-create"));

  await waitFor(() => {
    const created = calls.find((call) => call.url.includes("/mediaplans"));
    expect(created).toBeDefined();
    // LE PORTEUR EST CE DATASTREAM. C'est ce qui fait qu'un second plan du
    // Projet a besoin d'un second Datastream, jamais d'un provisionnement.
    expect(created?.body).toMatchObject({ carrier_datastream_id: DATASTREAM });
  });
  // Aucune route de création de Datastream n'est appelée.
  expect(calls.every((call) => !/\/datastreams$/.test(call.url))).toBe(true);
});

it("imports a revision through the Datastream's own ingress, and says which dated version it became", async () => {
  const calls: string[] = [];
  let reads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/workbench/data")) {
        reads += 1;
        return Promise.resolve(
          response({
            evidence: {
              media_plan: {
                ...CARRIED,
                versions: [
                  {
                    id: "mpv_TWO",
                    version_number: 2,
                    status: "candidate",
                    is_active: false,
                    source_note: "february.xlsx",
                    created_at: "2026-08-24T10:00:00Z",
                  },
                  ...CARRIED.versions,
                ],
              },
            },
          }),
        );
      }
      return Promise.resolve(response({ status: "processed" }));
    }),
  );

  render(
    <WorkbenchMediaPlanPanel projectId={PROJECT} datastreamId={DATASTREAM} mediaPlan={CARRIED} />,
  );

  const input = screen.getByTestId("media-plan-import") as HTMLInputElement;
  const file = new File(["plan"], "february.xlsx", {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  Object.defineProperty(input, "files", { value: [file] });
  input.dispatchEvent(new Event("change", { bubbles: true }));

  await waitFor(() => expect(reads).toBeGreaterThan(0));

  // LA ROUTE EXISTANTE, celle que tout fichier de ce Datastream emprunte déjà.
  expect(calls.some((url) => url.includes(`/api/datastreams/${DATASTREAM}/imports`))).toBe(true);
  // Et JAMAIS l'ancien chemin par contrat de feuille, qui serait la seconde porte.
  expect(calls.every((url) => !url.includes("/mediaplans/plan_EXAMPLE/import"))).toBe(true);

  // QUELLE VERSION DATÉE C'EST DEVENU — clause 3 de l'`Incomplete if`.
  const landed = await screen.findByTestId("media-plan-landed");
  expect(landed).toHaveTextContent("version 2");
  expect(landed).toHaveTextContent("2026-08-24");
  expect(landed).toHaveTextContent("candidate");
  // ET ELLE ATTERRIT À CÔTÉ : la version 1 est toujours là.
  expect(landed).toHaveTextContent(/landed beside them/);
  expect(screen.getByText("january.xlsx")).toBeInTheDocument();
});
