/**
 * La lentille Conflicts de Controls & Quality, a qui governance.md donne la decision.
 *
 * MESURE DU 2026-08-14 : `navigation.ts` declarait la lentille `conflicts`
 * depuis la story 49.1, et GovernanceCollection ne rendait RIEN pour elle -- elle
 * tombait sur la liste generique d'objets gouvernes, qui ne porte aucun conflit
 * MDM. Une lentille declaree et vide se lit « ce projet n'a pas de conflit »,
 * qui est une affirmation, pas une absence de rendu.
 *
 * Les trois etats que ce fichier separe sont ceux qu'un ecran confond toujours :
 * rien a arbitrer, rien de LISIBLE, et une lecture refusee. Les trois se
 * ressemblent a l'oeil et ne demandent pas le meme geste.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import MdmConflictsPanel from "../governance/MdmConflictsPanel";

function response(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

const CONFLICT = {
  field: { name: "revenue", display_name: "Revenue" },
  conflict: {
    code: "CURRENCY_CONFLICT",
    message: "Two sources report this field in different currencies.",
    affected_streams: ["ds-one", "ds-two"],
    severity: "refusal",
  },
  resolutions_by_module: { "meta-ads": null },
};

function stub(status: number, body: unknown) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/reference/currencies")) {
      return Promise.resolve(response(200, { items: [{ code: "EUR", display_name: "Euro" }] }));
    }
    return Promise.resolve(response(status, body));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  localStorage.setItem("api_token", "test-token");
});

afterEach(() => vi.unstubAllGlobals());

test("un conflit se lit, et porte le geste qui le ferme", async () => {
  stub(200, [CONFLICT]);
  render(<MdmConflictsPanel projectId="proj_EXAMPLE" />);

  expect(await screen.findByText("Revenue")).toBeInTheDocument();
  expect(screen.getByText("CURRENCY_CONFLICT")).toBeInTheDocument();
  expect(screen.getByText("2 Datastreams affected")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Resolve" }));
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  expect(screen.getByDisplayValue("meta-ads")).toBeInTheDocument();
});

test("rien a arbitrer le DIT, et nomme ce qui ferait apparaitre le premier", async () => {
  stub(200, []);
  render(<MdmConflictsPanel projectId="proj_EXAMPLE" />);

  expect(await screen.findByText("Nothing to arbitrate")).toBeInTheDocument();
  expect(screen.getByText(/as soon as a mapping version is published/i)).toBeInTheDocument();
});

test("une lecture refusee n'est pas un projet sain", async () => {
  stub(403, { code: "forbidden" });
  render(<MdmConflictsPanel projectId="proj_EXAMPLE" />);

  expect(await screen.findByText(/were not disclosed/i)).toBeInTheDocument();
  //  Et surtout : jamais la phrase de l'etat vide, qui affirmerait le contraire.
  expect(screen.queryByText("Nothing to arbitrate")).not.toBeInTheDocument();
});

test("une lecture cassee propose de la refaire au lieu d'annoncer zero", async () => {
  const fetchMock = stub(503, { code: "unavailable" });
  render(<MdmConflictsPanel projectId="proj_EXAMPLE" />);

  expect(await screen.findByText(/unavailable right now/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).startsWith("/api/mdm/conflicts")).length,
    ).toBe(2),
  );
});
