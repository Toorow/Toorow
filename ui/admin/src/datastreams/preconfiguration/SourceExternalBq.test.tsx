/**
 * AI-209 — la marche BigQuery nomme l'outil qu'elle veut lire.
 *
 * CE QUE CE FICHIER EPINGLE, ET RIEN D'AUTRE : que l'appel de listing porte
 * `?connector=bigquery`. Un seul caractere de query, et sans lui l'ecran ne
 * marche pas — mesure du 2026-08-16 :
 *
 *   `resolve_connection_connector`, consentement portant bigquery + analytics
 *     sans ?connector=       -> REFUSE
 *     ?connector=bigquery    -> bigquery
 *
 * La console demande TOUS les scopes Google sur un seul ecran de consentement,
 * donc la ligne du haut est la forme de production. Sans le parametre,
 * l'endpoint garde `module=None`, decouvre contre le provider de la ligne —
 * `google`, qui ne declare aucune topologie — et repond 409. L'ecran retombait
 * alors sur la saisie libre sans dire pourquoi.
 *
 * Le commentaire de `wizardApi.ts` a porte l'affirmation INVERSE pendant des
 * semaines (« naming the tool here would turn a working walk into a 404 »), et
 * c'est ce qui a produit l'omission. Un commentaire ne se teste pas ; l'URL, si.
 */
import { render, waitFor } from "@testing-library/react";
import SourceExternalBq from "./SourceExternalBq";
import type { ExternalInput } from "./sourceStepShared";

const CONNECTION_ID = "conn_EXAMPLE";

const INPUT = {
  source: {
    access_ref: "acct_EXAMPLE",
    object_ref: "proj_EXAMPLE.ds_EXAMPLE.table_EXAMPLE",
    writer_identity: "",
    read_only_ack: false,
  },
} as unknown as ExternalInput;

const OPTIONS = {
  external_access: [
    {
      object_ref: { id: "acct_EXAMPLE" },
      connector_ref: { id: "google" },
      connection_ref: { id: CONNECTION_ID },
      label: "Entrepot BigQuery",
      states: { availability: "available" },
      external_object_ref: null,
      truncated: false,
    },
  ],
} as never;

function mockFetch() {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ connection_ref_id: CONNECTION_ID, topology: null, accounts: [] }),
  } as unknown as Response);
  vi.stubGlobal("fetch", spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SourceExternalBq", () => {
  it("names bigquery on the listing call, because an unnamed one answers 409", async () => {
    const spy = mockFetch();

    render(
      <SourceExternalBq
        input={INPUT}
        options={OPTIONS}
        hasDependentEvidence={false}
        cfg={{ apiBase: "https://api.example.com" } as never}
        onChooseAccess={() => {}}
        onEditUpstream={() => {}}
        onEdit={() => {}}
      />,
    );

    await waitFor(() => expect(spy).toHaveBeenCalled());
    const [url] = spy.mock.calls[0] as [string];
    expect(url).toContain(`/api/connections/${CONNECTION_ID}/accounts`);
    // Le coeur du garde. `toContain` plutot qu'une egalite d'URL : ce qui compte
    // est que l'outil soit nomme, pas l'ordre des parametres d'une URL future.
    expect(url).toContain("connector=bigquery");
  });
});
