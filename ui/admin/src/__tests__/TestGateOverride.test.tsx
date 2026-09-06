/**
 * La porte Test, et le seul geste qui la franchit.
 *
 * MESURE DU 2026-08-14. Rien dans ce depot n'ecrit un verdict
 * `semantic_model.test_gate` -- `semantic_model.py:766-773` le dit lui-meme et
 * l'assume : la porte est LATENTE et EPINGLEE exprès, pour qu'aucun `pass`
 * perime ne puisse exister avant qu'un producteur de verdicts n'atterrisse.
 * Conséquence pratique : toute publication de Concept ou de Vue semantique passe
 * par l'override, dont la seule condition est une raison de 20 caracteres.
 *
 * Donc l'override N'EST PAS un cas limite : c'est le chemin de publication de
 * tous les jours. Il n'avait aucun test. Ce fichier en fait un contrat.
 *
 * On y assied trois choses, et la troisieme est celle qui compte : ce qui part
 * sur le fil au second `prepare`. Un panneau qui s'affiche et un corps qui ne
 * porte pas `test_gate_override` sont indiscernables a l'oeil.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import NewConceptDialog from "../governance/NewConceptDialog";
import {
  OVERRIDE_MINIMUM_REASON,
  gateBlocksPublication,
} from "../governance/TestGateOverride";

const PROJECT = "proj_EXAMPLE";
const CHANGE_SET = "scs_01EXAMPLE0000000000000000";
const GATE_MESSAGE = "No Test verdict was ever recorded for this object.";

interface Posted {
  url: string;
  body: Record<string, unknown>;
}

/** `prepare` refuse sur la porte tant qu'aucun override n'arrive dans son corps. */
function stubFetch() {
  const posted: Posted[] = [];
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const address = String(url);
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
    if (init?.method === "POST") posted.push({ url: address, body });

    if (address.includes("/business-domains")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ items: [] }) });
    }
    // Precis, et c'est necessaire : l'adresse d'un change set CONTIENT
    // `/governance/semantic-model`. Une condition large ici rend la collection a
    // la place du change set, et le dialogue prepare `/undefined/prepare`.
    if (
      address.includes("/governance/semantic-model?") ||
      address.endsWith("/governance/semantic-model")
    ) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ items: [], coverage: {}, unavailable_reasons: [], lens: "concepts" }),
      });
    }
    if (address.endsWith("/change-sets")) {
      return Promise.resolve({ ok: true, status: 201, json: async () => ({ change_set_id: CHANGE_SET }) });
    }
    if (address.endsWith("/prepare")) {
      const overridden = Boolean(body.test_gate_override);
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          // Toujours minte, refus ou non -- c'est la forme qui a deja fait lire
          // un change set refuse comme accepte.
          confirmation_token: "token-EXAMPLE",
          refusals: [],
          validation: {
            publishable: overridden,
            refusals: [],
            test_gate: overridden
              ? { state: "overridden" }
              : { state: "no_test_coverage", reason: "no_test_coverage", message: GATE_MESSAGE },
          },
        }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ result_version_id: "scv_1" }) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return posted;
}

afterEach(() => vi.unstubAllGlobals());

function open() {
  render(<NewConceptDialog open projectId={PROJECT} onClose={() => {}} onCreated={() => {}} />);
}

async function fillAndSubmit(name: string) {
  await userEvent.type(screen.getByLabelText("Canonical Name (ID/Code)"), name);
  await userEvent.selectOptions(screen.getByLabelText("Aggregation behaviour"), "additive");
  await userEvent.click(screen.getByRole("button", { name: "Create Concept" }));
}

// ---------------------------------------------------------------------------
// Ce que la porte est, et ce qu'elle n'est pas
// ---------------------------------------------------------------------------

it("un refus NOMME n'est pas la porte : on ne propose pas d'ecrire une raison pour un champ faux", () => {
  const gate = { state: "no_test_coverage", message: GATE_MESSAGE };
  expect(gateBlocksPublication(gate, [{ code: "unknown_operation" }])).toBe(false);
  expect(gateBlocksPublication(gate, [])).toBe(true);
});

it("une porte passee ou deja franchie ne bloque rien", () => {
  expect(gateBlocksPublication({ state: "pass" }, [])).toBe(false);
  expect(gateBlocksPublication({ state: "overridden" }, [])).toBe(false);
  // Une porte qui n'a pas parle n'est pas un blocage : la question n'existe pas
  // avant que la reponse precedente ne la rende necessaire.
  expect(gateBlocksPublication({}, [])).toBe(false);
  expect(gateBlocksPublication(undefined, [])).toBe(false);
});

// ---------------------------------------------------------------------------
// Le parcours, et ce qui part sur le fil
// ---------------------------------------------------------------------------

it("la porte parle : le panneau porte la phrase DU SERVEUR et rien n'est publie", async () => {
  const posted = stubFetch();
  open();
  await fillAndSubmit("sessions_per_user");

  expect(await screen.findByText(/has no Test verdict/i)).toBeInTheDocument();
  //  La phrase vient du serveur. En ecrire une seconde ici serait une seconde
  //  reponse, libre de contredire la premiere.
  expect(screen.getByText(GATE_MESSAGE)).toBeInTheDocument();
  //  Et surtout : la publication ne s'est PAS faite malgre le jeton minte.
  expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(0);
});

it("une raison trop courte ne part pas, et l'ecran dit combien il manque", async () => {
  const posted = stubFetch();
  open();
  await fillAndSubmit("sessions_per_user");
  await screen.findByText(/has no Test verdict/i);

  await userEvent.type(screen.getByLabelText(/Why publish it anyway/i), "trop court");
  expect(
    screen.getByText(new RegExp(`${OVERRIDE_MINIMUM_REASON - "trop court".length} more characters`)),
  ).toBeInTheDocument();

  //  Le geste n'est pas REFUSE apres coup, il n'est pas OFFERT : une raison
  //  blanche est indiscernable d'une absence de porte, et le serveur la refuse
  //  de toute facon. L'ecran le dit avant le clic plutot qu'apres.
  expect(screen.getByRole("button", { name: "Publish with this reason" })).toBeDisabled();
  expect(posted.filter((entry) => entry.url.endsWith("/prepare"))).toHaveLength(1);
  expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(0);
});

it("une raison suffisante voyage dans le corps de prepare, et la publication reprend", async () => {
  const posted = stubFetch();
  open();
  await fillAndSubmit("sessions_per_user");
  await screen.findByText(/has no Test verdict/i);

  await userEvent.type(
    screen.getByLabelText(/Why publish it anyway/i),
    "Nothing produces a Test verdict yet; the formula is covered by the compiler.",
  );
  await userEvent.click(screen.getByRole("button", { name: "Publish with this reason" }));

  await waitFor(() =>
    expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(1),
  );
  const prepares = posted.filter((entry) => entry.url.endsWith("/prepare"));
  expect(prepares).toHaveLength(2);
  //  Le premier prepare ne portait rien : on n'ecrit pas une raison avant que la
  //  porte ait parle.
  expect(prepares[0].body.test_gate_override).toBeUndefined();
  const override = prepares[1].body.test_gate_override as { reason: string };
  expect(override.reason.length).toBeGreaterThanOrEqual(OVERRIDE_MINIMUM_REASON);
  expect(override.reason).toContain("Test verdict");
});
