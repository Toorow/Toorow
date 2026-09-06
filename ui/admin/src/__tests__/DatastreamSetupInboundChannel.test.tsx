import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamSetupWizard from "../datastreams/preconfiguration/DatastreamSetupWizard";
import * as wizardApi from "../datastreams/wizard/wizardApi";

/** Story 57.3 — what an inbound channel shows when there is nothing to discover.
 *
 *  A CONTRACT IS NOT AN OBSERVATION, and everything below follows from that. An
 *  email address and a webhook token cannot be listed: no provider will answer,
 *  and until a sender sends there is nothing at all. So the Source section
 *  states a promise and the state of the deployment that has to honour it, and
 *  each absence names who settles it.
 *
 *  Five defects are pinned here, and each existed before this story: `webhook`
 *  was declared everywhere except in the list where it is chosen; `Discover
 *  source` was disabled forever on both channels because it demanded a Template
 *  neither ever carries; no arrival frequency was ever asked, and the server
 *  invented 1440 minutes; no sender could be declared anywhere; and an empty
 *  channel read like a broken adapter.
 *
 *  No domain of this test is real (`.invalid` is reserved by RFC 2606), no
 *  address is ever composed, and nothing here sends or receives anything. */

vi.mock("../datastreams/wizard/wizardApi", async () => {
  const actual = await vi.importActual<typeof import("../datastreams/wizard/wizardApi")>("../datastreams/wizard/wizardApi");
  return {
    ...actual,
    createDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupSourceOptions: vi.fn(),
    createDatastreamSetupObservation: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
  };
});

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_1", current_revision: 1, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "/resume",
  idempotent_replay: false, operator_input: {},
};

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1", source_accounts: [], connectors: [],
  managed_channels: [
    { channel: "file_upload", availability: "available", template_ref: null },
    { channel: "google_sheets", availability: "available", template_ref: null },
    { channel: "inbound_email", availability: "available", template_ref: null, domain: "feeds.toorow-test.invalid" },
    { channel: "webhook", availability: "available", template_ref: null, domain: "feeds.toorow-test.invalid" },
  ],
  external_access: [],
};

/** The ordinary state of a channel nobody has sent to: the deployment can
 *  receive, no address is minted yet, no Template is bound, no sender is
 *  declared, no arrival is expected and no file has landed. */
const observation: wizardApi.DatastreamSetupObservation = {
  observation_ref: "dso_1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_2", draft_revision: 2,
  mode: "managed_feed", discovery_kind: "channel_contract",
  adapter_ref: "managed_feed.channel_contract.v1",
  connector_contract_version_ref: null, request_fingerprint: "a".repeat(64),
  evidence_fingerprint: "b".repeat(64), schema_hash: null,
  safe_metadata: { fields: [] },
  coverage: {
    domain: "configured", capability: "not_addressable_yet", format: "not_declared",
    sender: "token_only", arrival: "not_declared", delivery: "none",
  },
  exceptions: [{ code: "channel_not_addressable_yet" }, { code: "arrival_expectation_not_declared" }, { code: "no_delivery_received_yet" }],
  observed_at: "2026-08-05T09:00:00Z", expires_at: null, idempotent_replay: false,
};

beforeEach(() => {
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue(observation);
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => {
    const current = (input.source as { observation_ref?: string } | undefined)?.observation_ref ? revision : revision + 1;
    return { ...draft, current_revision: current, current_revision_ref: `dsdr_${current}`, operator_input: input };
  });
});

async function chooseChannel(user: ReturnType<typeof userEvent.setup>, channel: string) {
  const view = render(<DatastreamSetupWizard projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Managed feed" }));
  // CINQ ARRÊTS RATIFIÉS : le mode est la première question de `Source`, et le
  // canal la suivante — même étape, aucun clic entre les deux. Un bouton
  // primaire contextuel par arrêt mène de l'un à l'autre, une seule section
  // visible à la fois.
  await user.selectOptions(await screen.findByLabelText(/Channel/), channel);
  return view;
}

it("offers webhook in the list where a channel is actually chosen", async () => {
  // It was in CHANNELS server-side, in the TypeScript union and in the cadence
  // label — everywhere except the one place an operator can pick it. A token
  // declared everywhere but the select is a dead branch.
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Managed feed" }));
  // La liste des canaux vit à l'étape `Source`, dont le mode est la première
  // question — même étape, aucun arrêt entre les deux.
  const select = await screen.findByLabelText(/Channel/);
  expect(await screen.findByRole("option", { name: /webhook/i })).toBeInTheDocument();
  await user.selectOptions(select, "webhook");
  expect(select).toHaveValue("webhook");
});

it("enables Discover source on an inbound channel with no template", async () => {
  // `canDiscover` required `template_ref`, and no Template is ever attached to
  // these two channels — the button was disabled forever on both.
  const user = userEvent.setup();
  const first = await chooseChannel(user, "inbound_email");
  expect(screen.getByRole("button", { name: "Discover source" })).toBeEnabled();
  // Une SEULE section visible à la fois : deux
  // assistants montés ensemble finissent tous deux sur `Source`, et leurs
  // contrôles se confondent (`/Channel/` rendu en double). On démonte le
  // premier avant de marcher le second — ce qui est épinglé, c'est le bouton
  // activé pour CHAQUE canal, pas la cohabitation des deux rendus.
  first.unmount();
  await chooseChannel(user, "webhook");
  expect(screen.getByRole("button", { name: "Discover source" })).toBeEnabled();
});

it("states the four clauses of the contract, each with its own state", async () => {
  const user = userEvent.setup();
  await chooseChannel(user, "inbound_email");
  expect(await screen.findByText(/Where deliveries arrive — ds_<token>@feeds.toorow-test.invalid/)).toBeInTheDocument();
  expect(screen.getByText(/What is expected — no template bound/)).toBeInTheDocument();
  expect(screen.getByText(/Who may send — token only/)).toBeInTheDocument();
  expect(screen.getByText(/How often it is expected — not declared/)).toBeInTheDocument();
});

it("never shows an address, and says where one is issued", async () => {
  // An address is a secret minted against a Datastream, and this step has a
  // draft. One shown here would resolve no token and be a secret nobody created.
  const user = userEvent.setup();
  await chooseChannel(user, "inbound_email");
  expect(await screen.findByText(/The address is issued against the Datastream, on its Overview/)).toBeInTheDocument();
  expect(screen.queryByText(/ds_[A-Za-z0-9]{8}/)).not.toBeInTheDocument();
});

it("says a webhook has a token rather than a dedicated URL", async () => {
  const user = userEvent.setup();
  await chooseChannel(user, "webhook");
  expect(await screen.findByText(/Where deliveries arrive — a token, not a URL/)).toBeInTheDocument();
  expect(screen.queryByText(/ds_<token>@/)).not.toBeInTheDocument();
});

it("names the platform administrator when no verified domain exists", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    managed_channels: options.managed_channels.map((item) =>
      item.channel === "inbound_email" ? { ...item, availability: "setup_required", domain: null } : item,
    ),
  });
  await chooseChannel(user, "inbound_email");
  expect(await screen.findByText(/No verified inbound domain is configured for this deployment/)).toBeInTheDocument();
  expect(screen.getByText(/A platform administrator configures it; this step cannot/)).toBeInTheDocument();
});

it("declares a sender list, and says where it is applied", async () => {
  // The list existed nowhere before this story. It is applied at processing, not
  // at reception: the receipt answers every refusal identically so nobody can
  // enumerate addresses, and the screen says exactly that.
  const user = userEvent.setup();
  await chooseChannel(user, "inbound_email");
  await user.type(await screen.findByLabelText(/Who may send/), "reports@agency.invalid");
  expect(await screen.findByText(/1 sender declaration/)).toBeInTheDocument();
  expect(screen.getByText(/Applied when a delivery is processed, never at reception/)).toBeInTheDocument();
  await waitFor(() => {
    const saved = vi.mocked(wizardApi.updateDatastreamSetupDraft).mock.calls.at(-1)?.[3] as { source?: { channel_contract?: { allowed_senders?: string[] } } };
    expect(saved?.source?.channel_contract?.allowed_senders).toEqual(["reports@agency.invalid"]);
  });
});

it("accepts the list in the shape its own instruction asks for", async () => {
  // The hint said "one per line" while the field was a single-line input split
  // on commas: an operator who followed the instruction wrote a list the field
  // could not hold. The control and its sentence have to be the same thing.
  const user = userEvent.setup();
  await chooseChannel(user, "inbound_email");
  const field = await screen.findByLabelText(/Who may send/);
  expect(field.tagName).toBe("TEXTAREA");
  await user.type(field, "reports@agency.invalid\npartner.invalid");
  await waitFor(() => {
    const saved = vi.mocked(wizardApi.updateDatastreamSetupDraft).mock.calls.at(-1)?.[3] as { source?: { channel_contract?: { allowed_senders?: string[] } } };
    expect(saved?.source?.channel_contract?.allowed_senders).toEqual(["reports@agency.invalid", "partner.invalid"]);
  });
  expect(await screen.findByText(/2 sender declarations/)).toBeInTheDocument();
});

it("declares an arrival expectation, and arms nothing until it is given", async () => {
  // The server used to read `or 1440` and promise a daily delivery on the
  // operator's behalf. The field starts EMPTY and the screen says what an empty
  // one means, on both the step that declares it and the step that activates.
  const user = userEvent.setup();
  await chooseChannel(user, "inbound_email");
  const field = await screen.findByLabelText(/Expected arrival every N minutes/);
  expect(field).toHaveValue(null);
  expect(screen.getByText(/No expected arrival frequency is declared/)).toBeInTheDocument();
  expect(screen.queryByText(/1440/)).not.toBeInTheDocument();
  await user.type(field, "360");
  expect(await screen.findByText(/A delivery is expected every 360 minutes/)).toBeInTheDocument();
  await waitFor(() => {
    const saved = vi.mocked(wizardApi.updateDatastreamSetupDraft).mock.calls.at(-1)?.[3] as { source?: { channel_contract?: { expected_interval_minutes?: number } } };
    expect(saved?.source?.channel_contract?.expected_interval_minutes).toBe(360);
  });
});

it("says nothing has arrived, and never calls it a failure", async () => {
  const user = userEvent.setup();
  await chooseChannel(user, "webhook");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // LE PARCOURS RATIFIÉ : après la découverte, le primaire
  // de `Source` attend le nom et le rôle — ses deux dernières questions — puis
  // à `Configure`, où l'état vide du canal se lit (DiscoveredSchema). Le bouton
  // ne s'active qu'une fois l'observation enregistrée : attendre avant de cliquer.
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  // Le nom n'est plus proposé en chemin : la famille de rapport qui l'alimentait
  // est choisie à `Configure`, une section plus loin. On le saisit.
  await user.type(await screen.findByLabelText(/Datastream name/), "Agency feed");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Spend");
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  expect(await screen.findByText(/No file has arrived on this channel yet/)).toBeInTheDocument();
  expect(screen.getByText(/read from the first file that arrives, never guessed/)).toBeInTheDocument();
  // Not the adapter sentence, and not the sheet or warehouse ones either.
  expect(screen.queryByText(/could not read this channel's state/)).not.toBeInTheDocument();
  expect(screen.queryByText(/The discovery read returned no column/)).not.toBeInTheDocument();
  expect(screen.queryByText(/exposes no readable column/)).not.toBeInTheDocument();
});

it("says a failed discovery is a failure, and names the adapter", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockRejectedValue(
    new wizardApi.WizardApiError(503, "observation_unavailable", "The inbound channel discovery adapter (managed_feed.channel_contract.v1) could not read this channel's state. Nothing was saved."),
  );
  await chooseChannel(user, "webhook");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  expect(await screen.findByText(/could not read this channel's state/)).toBeInTheDocument();
  expect(screen.getByText(/managed_feed.channel_contract.v1/)).toBeInTheDocument();
  // The two emptinesses are NOT this sentence: one waits for a sender, the
  // other waits for an administrator. Neither is a fault.
  expect(screen.queryByText(/No file has arrived on this channel yet/)).not.toBeInTheDocument();
});
