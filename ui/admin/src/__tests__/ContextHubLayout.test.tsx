import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ContextHubLayout from "../connaissances/ContextHubLayout";

const taxonomy = {
  org_id: "org_1",
  domains: [
    {
      id: "bdm_retail",
      slug: "retail",
      name: "Retail",
      description: "Customer-facing commerce",
      owner: null,
      status: "active",
      version_number: 3,
      current_version_id: "mdver_retail_c",
    },
    {
      id: "bdm_legacy",
      slug: "legacy-retail",
      name: "Legacy Retail",
      description: "Archived operating model",
      owner: null,
      status: "archived",
      version_number: 5,
      current_version_id: "mdver_legacy_c",
    },
  ],
  classifications: [
    {
      id: "bcl_subscriptions",
      domain_id: "bdm_retail",
      parent_id: null,
      classification_type: "revenue_model",
      slug: "subscriptions",
      name: "Subscriptions",
      description: "Recurring product offers",
      owner: null,
      status: "active",
      version_number: 2,
      current_version_id: "mdver_subs_c",
    },
  ],
};

const links = {
  links: [
    {
      id: "blink_1",
      org_id: "org_1",
      project_id: "p1",
      taxonomy_type: "business_domain",
      taxonomy_id: "bdm_retail",
      target_type: "target_field",
      target_id: "recurring_revenue",
      relation_type: "explains",
      link_origin: "direct",
      created_by: "owner@example.com",
      created_at: "2026-07-28T10:00:00Z",
    },
    // Hung off the LAYER, not the domain, on purpose: these two prove the two
    // halves of the headline rule and must not crowd the domain's single card,
    // where several tests address "View path" and "Unlink" by their role alone.
    {
      id: "blink_2",
      org_id: "org_1",
      project_id: "p1",
      taxonomy_type: "business_classification",
      taxonomy_id: "bcl_subscriptions",
      target_type: "datastream",
      target_id: "ds_01JPAIDMEDIA",
      relation_type: "uses",
      link_origin: "direct",
      created_by: "owner@example.com",
      created_at: "2026-07-28T10:00:00Z",
    },
    {
      id: "blink_3",
      org_id: "org_1",
      project_id: "p1",
      taxonomy_type: "business_classification",
      taxonomy_id: "bcl_subscriptions",
      target_type: "semantic_view",
      target_id: "smv_01JORPHAN",
      relation_type: "uses",
      link_origin: "direct",
      created_by: "owner@example.com",
      created_at: "2026-07-28T10:00:00Z",
    },
  ],
};

const graph = {
  nodes: [
    {
      id: "recurring_revenue",
      node_type: "target_field",
      title: "Recurring revenue",
      excerpt: "Approved recurring revenue metric",
      owner: null,
      version_number: 4,
      scope: "project",
      status: "active",
    },
    // `graph_projection` synthesizes one of these per linked Datastream, its
    // title READ from `app.datastreams.name` (`_owned_target_nodes`, 49.6) --
    // which is why the console never has to ask the server for a name it holds.
    {
      id: "ds_01JPAIDMEDIA",
      node_type: "datastream",
      title: "Paid media daily",
      excerpt: "Governed Datastream ds_01JPAIDMEDIA",
      owner: null,
      version_number: 1,
      scope: "project",
      status: "active",
    },
    // `smv_01JORPHAN` is deliberately ABSENT: a link whose endpoint the bundle
    // could not resolve is the case the fallback exists for.
  ],
  edges: [],
};

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = String(input);
    if (url.startsWith("/api/context/business-taxonomy")) return response(taxonomy);
    if (url.startsWith("/api/context/business-links") && (init?.method ?? "GET") === "GET") return response(links);
    if (url.startsWith("/api/context/graph")) return response(graph);
    if (url.startsWith("/api/context/business-paths/preview")) {
      return response({
        path_key: "ctxp_retail_route",
        target: { type: "target_field", id: "recurring_revenue" },
        link_origin: "direct",
        ordered_path: [
          { kind: "node", node_type: "business_domain", id: "bdm_retail", title: "Retail", version_number: 3 },
          { kind: "edge", id: "blink_1", edge_type: "explains", link_origin: "direct" },
          { kind: "node", node_type: "target_field", id: "recurring_revenue", version_number: 4 },
        ],
        source_versions: [],
      });
    }
    if (url.startsWith("/api/projects/p1/mdm/canonical-fields")) {
      return response({
        fields: [
          { id: "mdmcf_net_revenue", canonical_name: "Net revenue", scope: "project" },
          { id: "mdmcf_spend", canonical_name: "Spend", scope: "platform" },
        ],
        scope_counts: { platform: 1, project: 1 },
        empty_reason: null,
      });
    }
    // THE IDENTITY ACTS GO TO THE AUTHORITY since the cutover of 2026-08-25:
    // `/api/context/business-domains/{id}` refuses, and this screen creates,
    // renames and archives through the Master Data command instead.
    if (
      url.startsWith("/api/projects/p1/governance/master-data/nodes")
      && init?.method === "POST"
    ) {
      return response({
        operation_id: "op_1",
        outcome: "succeeded",
        result: { node_id: "bdm_legacy", action: "restore" },
      });
    }
    if (url.startsWith("/api/context/business-links") && init?.method === "POST") {
      return response({ ...links.links[0], id: "blink_9" }, 201);
    }
    if (url.startsWith("/api/context/business-links") && init?.method === "DELETE") {
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    return response({ code: "not_found", message: "Unexpected request" }, 404);
  });
});

afterEach(() => vi.restoreAllMocks());

test("renders client-defined business layers and opens a traceable evidence path", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  expect(await screen.findByRole("button", { name: "Retail" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Subscriptions/ })).toBeInTheDocument();
  expect(screen.queryByText("Sales")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Legacy Retail.*Archived/ })).toBeInTheDocument();
  expect(screen.getByLabelText("Context Hub metrics")).toHaveTextContent("2 domains");

  await user.click(screen.getByRole("button", { name: "Retail" }));
  await user.click(screen.getByRole("button", { name: "View path" }));
  expect(await screen.findByText("ctxp_retail_route")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Copy key" })).toBeInTheDocument();
  // The path drawer still shows the raw id for the segment the server sent with
  // no title. The link card no longer does — see the headline tests below.
  expect(screen.getAllByText("recurring_revenue")).toHaveLength(1);
});

// --- A link card is headlined by a name, not by a stored id ------------------

test("headlines a link with the target's name from the graph already in memory", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  await user.click(await screen.findByRole("button", { name: "Retail" }));
  const card = (await screen.findByText("Recurring revenue")).closest("article");
  expect(card).not.toBeNull();
  // The id it replaced is nowhere on the card: `recurring_revenue` is what the
  // row stores, and it was the whole headline.
  expect(card).not.toHaveTextContent("recurring_revenue");
  // The type and relation still say what kind of route this is.
  expect(card).toHaveTextContent("target field");
  expect(card).toHaveTextContent("explains");
});

test("falls back to the id, set as an id, when the bundle resolves no name", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  await user.click(await screen.findByRole("button", { name: /Subscriptions/ }));

  // Resolved: the Datastream the bundle carries a node for.
  expect(await screen.findByText("Paid media daily")).toBeInTheDocument();

  // Unresolved: the id stays, and it is rendered by the console's ONE component
  // for an immutable identifier — mono, demoted, full value in the title.
  // Inventing a name here would be worse than showing the token; the fallback
  // has to be visibly a fallback.
  const orphan = screen.getByText("smv_01JORPHAN");
  expect(orphan).toHaveClass("font-mono");
  expect(orphan).toHaveAttribute("title", "semantic view: smv_01JORPHAN");
});

test("links a governed resource from the selected business key", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="skills" onNavigate={vi.fn()} renderContent={() => <div>Skills surface</div>} />,
  );
  const retailDomain = await screen.findByRole("button", { name: "Retail" });
  await user.click(retailDomain);
  await user.click(screen.getByRole("button", { name: "Link resource" }));
  // Queried by its label, not its placeholder: the placeholder now tells the
  // operator whether the graph can suggest anything for the chosen type, so it
  // is copy that moves with the data rather than a stable handle.
  await user.type(screen.getByLabelText(/^Resource$/), "proc_01JABCDEF");
  await user.type(screen.getByLabelText("Reason"), "Connect subscription reporting");
  await user.click(screen.getByRole("button", { name: "Save change" }));

  await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(([url, init]) =>
      String(url).startsWith("/api/context/business-links") && init?.method === "POST",
    );
    expect(call).toBeDefined();
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      taxonomy_type: "business_domain",
      taxonomy_id: "bdm_retail",
      target_type: "procedure",
      target_id: "proc_01JABCDEF",
      relation_type: "applies_to",
    });
  });
});


test("opens the mindmap unscoped and keeps it ahead of business governance details", async () => {
  render(
    <ContextHubLayout
      projectId="p1"
      activeView="graph"
      onNavigate={vi.fn()}
      renderContent={(context) => (
        <div data-testid="graph-surface" data-graph-ready={String(Boolean(context.graph))}>
          {context.selected?.id ?? "All business"}
        </div>
      )}
    />,
  );

  await screen.findByRole("button", { name: "Retail" });
  const graphSurface = screen.getByTestId("graph-surface");
  await waitFor(() => {
    expect(graphSurface).toHaveTextContent("All business");
    expect(graphSurface).toHaveAttribute("data-graph-ready", "true");
  });

  const content = graphSurface.closest("main");
  // Addressed by its POSITION in the layout, not by a screen-local class: the
  // bar is a direct child of `main`, and that is the fact this test is about.
  const contextBar = screen.getByRole("heading", { name: "Select a business domain" }).closest("main > div");
  expect(content).not.toBeNull();
  expect(contextBar).not.toBeNull();
  expect(Array.from(content!.children).indexOf(graphSurface.parentElement!)).toBeLessThan(
    Array.from(content!.children).indexOf(contextBar!),
  );
});

test("restores an archived business domain through the authority's own command", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  const archivedDomain = await screen.findByRole("button", { name: /Legacy Retail.*Archived/ });
  await user.click(archivedDomain);
  expect(screen.getByRole("button", { name: "Add layer" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Link resource" })).toBeDisabled();

  await user.click(screen.getByRole("button", { name: "Restore" }));
  expect(screen.getByRole("heading", { name: "Restore Legacy Retail" })).toBeInTheDocument();
  await user.type(screen.getByLabelText("Reason"), "Return this domain to active governance");
  await user.click(screen.getByRole("button", { name: "Save change" }));

  await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(([url, init]) =>
      String(url) === "/api/projects/p1/governance/master-data/nodes/bdm_legacy/commands"
      && init?.method === "POST",
    );
    expect(call).toBeDefined();
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      action: "restore",
      reason: "Return this domain to active governance",
    });
    // The route answers 428 without it, and the key is what stops a retry after
    // a client timeout from acting twice.
    const headers = call?.[1]?.headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toMatch(/^md-hub-/);
  });
  // The superseded store is never written by this screen any more.
  expect(
    vi.mocked(fetch).mock.calls.some(([url, init]) =>
      String(url).startsWith("/api/context/business-domains") && init?.method === "PATCH",
    ),
  ).toBe(false);
});

test("a renamed classification carries the two other fields of the same version", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  await user.click(await screen.findByRole("button", { name: /Subscriptions/ }));
  await user.click(screen.getByRole("button", { name: "Edit" }));
  await user.clear(screen.getByLabelText("Name"));
  await user.type(screen.getByLabelText("Name"), "Recurring offers");
  await user.type(screen.getByLabelText("Reason"), "Clearer wording for the same thing");
  await user.click(screen.getByRole("button", { name: "Save change" }));

  await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(([url, init]) =>
      String(url) === "/api/projects/p1/governance/master-data/nodes/bcl_subscriptions/commands"
      && init?.method === "POST",
    );
    expect(call).toBeDefined();
    // The name, the description and the type live in ONE published version, so
    // a rename that sent only the label would erase the other two.
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      action: "rename",
      label: "Recurring offers",
      description: "Recurring product offers",
      classification_type: "revenue_model",
      reason: "Clearer wording for the same thing",
      // The base this dialog was opened on, as the AUTHORITY names it. Not
      // `version_number`: that counter unions two ledgers.
      expected_version: "mdver_subs_c",
    });
  });
});


test("a rename states the version identity the list gave it, never the counter", async () => {
  // `governance.md`, amendment of 2026-08-30. Before it, this door sent no
  // precondition at all, and two people editing one Business Domain from two
  // screens were arbitrated by whoever pressed Save last.
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  await user.click(await screen.findByRole("button", { name: "Retail" }));
  await user.click(screen.getByRole("button", { name: "Edit" }));
  await user.clear(screen.getByLabelText("Name"));
  await user.type(screen.getByLabelText("Name"), "Retail & Commerce");
  await user.type(screen.getByLabelText("Reason"), "One name for one line");
  await user.click(screen.getByRole("button", { name: "Save change" }));

  await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(([url, init]) =>
      String(url) === "/api/projects/p1/governance/master-data/nodes/bdm_retail/commands"
      && init?.method === "POST",
    );
    expect(call).toBeDefined();
    const body = JSON.parse(String(call?.[1]?.body));
    expect(body.expected_version).toBe("mdver_retail_c");
    // The number printed beside the entry is NOT the precondition: it unions the
    // authority's ledger with the superseded store's, so two readers holding
    // different objects can hold the same number.
    expect(body.expected_version).not.toBe(3);
  });
});


test("a stale refusal tells the curator to reopen the entry, and Save stops posting", async () => {
  const user = userEvent.setup();
  const keepTheReads = vi.mocked(fetch).getMockImplementation()!;
  let attempts = 0;
  vi.mocked(fetch).mockImplementation((input, init) => {
    const target = String(input);
    if (target.includes("/governance/master-data/nodes") && init?.method === "POST") {
      attempts += 1;
      return response(
        {
          code: "version_conflict",
          message:
            "This identity has been revised since you read it, so nothing was changed.",
        },
        409,
      );
    }
    return keepTheReads(input, init);
  });

  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  await user.click(await screen.findByRole("button", { name: "Retail" }));
  await user.click(screen.getByRole("button", { name: "Edit" }));
  await user.clear(screen.getByLabelText("Name"));
  await user.type(screen.getByLabelText("Name"), "Retail & Commerce");
  await user.type(screen.getByLabelText("Reason"), "One name for one line");
  await user.click(screen.getByRole("button", { name: "Save change" }));

  // The SHARED sentence, from `masterDataRefusals.ts` -- the same words the
  // Governance workbench says about the same code.
  expect(await screen.findByText(/nothing was overwritten/i)).toBeInTheDocument();
  expect(screen.getByText(/reopen the entry/i)).toBeInTheDocument();
  expect(screen.queryByText(/could not be saved/i)).toBeNull();

  // And pressing Save again does NOT re-post: it would send the same stale base
  // to be refused again, which is exactly what the sentence tells them not to do.
  await user.click(screen.getByRole("button", { name: "Save change" }));
  expect(attempts).toBe(1);
});

/**
 * THE TWO GUARDS ARE DIFFERENT GUARDS, AND BOTH ARE HERE.
 *
 * The idempotency key answers "did this command already run?" -- ONE per
 * submission, reused by every retry of it, so a timeout cannot apply the same
 * change twice. `expected_version` answers a different question: "is the object
 * still the one I read?" -- and no key can answer that, because a second person's
 * rename is a different command with a different key.
 *
 * Until 2026-08-30 only the first existed. The precondition had gone with the
 * legacy writer, whose counter was the superseded store's own ledger, and
 * `governance.md` recorded the gap as open work rather than send a number that
 * could not disagree. The amendment of 2026-08-30 closes it with an ID, and the
 * two tests above hold it.
 */
test("a retry of the same dialog carries the same idempotency key", async () => {
  const user = userEvent.setup();
  const keepTheReads = vi.mocked(fetch).getMockImplementation()!;
  let attempts = 0;
  vi.mocked(fetch).mockImplementation((input, init) => {
    const target = String(input);
    if (target.includes("/governance/master-data/nodes") && init?.method === "POST") {
      attempts += 1;
      if (attempts === 1) {
        return response(
          { code: "governance_unavailable", message: "Governance is unavailable" },
          503,
        );
      }
      return response({ operation_id: "op_1", outcome: "succeeded", result: {} });
    }
    return keepTheReads(input, init);
  });

  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  await user.click(await screen.findByRole("button", { name: /Legacy Retail.*Archived/ }));
  await user.click(screen.getByRole("button", { name: "Restore" }));
  await user.type(screen.getByLabelText("Reason"), "Return this domain to active governance");
  await user.click(screen.getByRole("button", { name: "Save change" }));
  await screen.findByText(/unavailable/i);
  await user.click(screen.getByRole("button", { name: "Save change" }));

  await waitFor(() => expect(attempts).toBe(2));
  const keys = vi
    .mocked(fetch)
    .mock.calls.filter(([url, init]) =>
      String(url).includes("/governance/master-data/nodes") && init?.method === "POST",
    )
    .map(([, init]) => (init?.headers as Record<string, string>)["Idempotency-Key"]);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
});

test("a refused link is reported as a refusal to overwrite, not as a failure to save", async () => {
  const user = userEvent.setup();
  const keepTheReads = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) => {
    const target = String(input);
    if (target.startsWith("/api/context/business-links") && init?.method === "POST") {
      return response({ code: "conflict", message: "This link already exists" }, 409);
    }
    // Every read keeps answering: the point is the refusal, and a test that also
    // broke the reads would show an error surface for the wrong reason.
    return keepTheReads(input, init);
  });

  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  await user.click(await screen.findByRole("button", { name: "Retail" }));
  await user.click(screen.getByRole("button", { name: "Link resource" }));
  await user.type(screen.getByLabelText("Resource"), "recurring_revenue");
  await user.type(screen.getByLabelText("Reason"), "It explains this field");
  await user.click(screen.getByRole("button", { name: "Save change" }));

  // "The change could not be saved" would send the curator to press Save again
  // on the same refused write. Nothing was lost and nothing was overwritten, and
  // the copy has to say which of the two happened.
  expect(await screen.findByText(/nothing was overwritten/i)).toBeInTheDocument();
  expect(screen.queryByText(/could not be saved/i)).toBeNull();
});

test("a convergence refusal names the convergence, not a phantom concurrent editor", async () => {
  // THE CUTOVER OF 2026-08-25, on the authority's side. An organization that has
  // not converged cannot declare an identity, and the refusal names the gesture
  // that unblocks it. Collapsing it into the stale-version sentence would tell
  // the curator somebody else edited the entry -- false, and it hides the one
  // gesture that works.
  const user = userEvent.setup();
  const keepTheReads = vi.mocked(fetch).getMockImplementation()!;
  const refusal =
    "This organization has not converged into Master Data yet, so nothing can be declared "
    + "here. Run the convergence on the Governance Master Data screen first -- it keeps every "
    + "Business Domain and classification at the identity it already has -- then create, "
    + "rename or archive it here.";
  vi.mocked(fetch).mockImplementation((input, init) => {
    const target = String(input);
    if (target.includes("/governance/master-data/nodes") && init?.method === "POST") {
      return response({ code: "master_data_organization_not_converged", message: refusal }, 409);
    }
    return keepTheReads(input, init);
  });

  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  const archivedDomain = await screen.findByRole("button", { name: /Legacy Retail.*Archived/ });
  await user.click(archivedDomain);
  await user.click(screen.getByRole("button", { name: "Restore" }));
  await user.type(screen.getByLabelText("Reason"), "Return this domain to active governance");
  await user.click(screen.getByRole("button", { name: "Save change" }));

  expect(
    await screen.findByText(/Run the convergence on the Governance Master Data screen first/i),
  ).toBeInTheDocument();
  // The other 409's sentence must NOT appear: it would name a person who does
  // not exist and send the curator to reopen an entry nobody touched.
  expect(screen.queryByText(/nothing was overwritten/i)).toBeNull();
});

// --- AI-298: a field a project declared can carry a business key -------------

test("suggests canonical fields by name, from the registry Governance reads", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="skills" onNavigate={vi.fn()} renderContent={() => <div>Skills surface</div>} />,
  );
  await user.click(await screen.findByRole("button", { name: "Retail" }));
  await user.click(screen.getByRole("button", { name: "Link resource" }));
  await user.selectOptions(screen.getByLabelText("Resource type"), "canonical_field");

  // The ids are `mdmcf_...` and live in Governance. Shipping this type without
  // the read would have meant a field whose only instruction is "paste the ID".
  // Read from the datalist itself: its options are not rendered, so they carry
  // no accessible role -- the suggestion is the pair (value, label).
  const suggestions = () =>
    Array.from(document.querySelectorAll("#business-target-options option")).map(
      (option) => [option.getAttribute("value"), option.textContent],
    );
  // The scope travels with the name: a project and the platform may each carry
  // the same canonical name, and the list has to say which is which.
  await waitFor(() =>
    expect(suggestions()).toEqual([
      ["mdmcf_net_revenue", "Net revenue (project)"],
      ["mdmcf_spend", "Spend (platform)"],
    ]),
  );
  expect(screen.getByLabelText(/^Resource$/)).toHaveAttribute("placeholder", "Search by name or ID");

  await user.type(screen.getByLabelText(/^Resource$/), "mdmcf_net_revenue");
  await user.type(screen.getByLabelText("Reason"), "Net revenue is a Retail measure");
  await user.click(screen.getByRole("button", { name: "Save change" }));

  await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(([url, init]) =>
      String(url).startsWith("/api/context/business-links") && init?.method === "POST",
    );
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      target_type: "canonical_field",
      target_id: "mdmcf_net_revenue",
    });
  });
});

test("an unreadable canonical vocabulary says so instead of reading as empty", async () => {
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = String(input);
    if (url.startsWith("/api/projects/p1/mdm/canonical-fields")) {
      return response({ code: "canonical_fields_unavailable", message: "unreadable" }, 503);
    }
    if (url.startsWith("/api/context/business-taxonomy")) return response(taxonomy);
    if (url.startsWith("/api/context/business-links") && (init?.method ?? "GET") === "GET") return response(links);
    if (url.startsWith("/api/context/graph")) return response(graph);
    return response({ code: "not_found", message: "Unexpected request" }, 404);
  });
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="skills" onNavigate={vi.fn()} renderContent={() => <div>Skills surface</div>} />,
  );
  await user.click(await screen.findByRole("button", { name: "Retail" }));
  await user.click(screen.getByRole("button", { name: "Link resource" }));
  await user.selectOptions(screen.getByLabelText("Resource type"), "canonical_field");

  // "No canonical field is declared yet" would be a claim about the client's
  // project made from a failure of ours.
  await waitFor(() =>
    expect(screen.getByLabelText(/^Resource$/)).toHaveAttribute(
      "placeholder",
      expect.stringContaining("could not be read"),
    ),
  );
});

// --- Every target type the bundle can name is suggested ----------------------

const suggestions = () =>
  Array.from(document.querySelectorAll("#business-target-options option")).map(
    (option) => [option.getAttribute("value"), option.textContent],
  );

async function openLinkDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Retail" }));
  await user.click(screen.getByRole("button", { name: "Link resource" }));
}

test("suggests a Datastream by name, because the bundle already carries its node", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );
  await openLinkDialog(user);
  await user.selectOptions(screen.getByLabelText("Resource type"), "datastream");

  // The type used to fall through to "Paste the datastream ID - none are
  // suggested here" while the console held the name on screen elsewhere.
  expect(suggestions()).toEqual([["ds_01JPAIDMEDIA", "Paid media daily"]]);
  expect(screen.getByLabelText(/^Resource$/)).toHaveAttribute("placeholder", "Search by name or ID");

  await user.type(screen.getByLabelText(/^Resource$/), "ds_01JPAIDMEDIA");
  await user.type(screen.getByLabelText("Reason"), "Retail is measured from this Datastream");
  await user.click(screen.getByRole("button", { name: "Save change" }));

  await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(([url, init]) =>
      String(url).startsWith("/api/context/business-links") && init?.method === "POST",
    );
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      target_type: "datastream",
      target_id: "ds_01JPAIDMEDIA",
    });
  });
});

test("keeps the honest paste sentence for a type the bundle carries no node for", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );
  await openLinkDialog(user);

  // Nothing in this project's bundle is a semantic concept, so the field says so
  // instead of showing an empty list under a "search by name" prompt.
  await user.selectOptions(screen.getByLabelText("Resource type"), "semantic_concept");
  expect(suggestions()).toEqual([]);
  expect(screen.getByLabelText(/^Resource$/)).toHaveAttribute(
    "placeholder",
    "Paste the semantic concept ID - none are suggested here",
  );

  // A report view's id has a SHAPE, and it is the one thing nobody can guess.
  // The old placeholder said only `connector/report_id` — including when the graph
  // could have suggested one, which is why the shape now rides on the same
  // sentence as every other unsuggestable type.
  await user.selectOptions(screen.getByLabelText("Resource type"), "report_view");
  expect(screen.getByLabelText(/^Resource$/)).toHaveAttribute(
    "placeholder",
    "Paste the report view ID as connector/report_id - none are suggested here",
  );
});

// --- Unlinking is a governed write like every other on this surface ----------

const deleteCalls = () =>
  vi.mocked(fetch).mock.calls.filter(([url, init]) =>
    String(url).startsWith("/api/context/business-links") && init?.method === "DELETE",
  );

test("asks before unlinking, and cancelling sends nothing", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );
  await user.click(await screen.findByRole("button", { name: "Retail" }));
  await user.click(screen.getByRole("button", { name: "Unlink" }));

  // Named by what it removes, not by the id the row stores.
  expect(await screen.findByRole("dialog")).toHaveTextContent("Unlink Recurring revenue?");

  await user.click(screen.getByTestId("context-hub-unlink-cancel"));
  await waitFor(() => expect(screen.queryByTestId("context-hub-unlink-confirm")).toBeNull());
  expect(deleteCalls()).toHaveLength(0);
  // The link is still on the surface: cancelling changed nothing.
  expect(screen.getByText("Recurring revenue")).toBeInTheDocument();
});

test("refuses to unlink without a reason, then sends the reason it was given", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );
  await user.click(await screen.findByRole("button", { name: "Retail" }));
  await user.click(screen.getByRole("button", { name: "Unlink" }));
  await screen.findByRole("dialog");

  // The same refusal every other governed change on this surface gives. Removing
  // a link used to be the ONE write here that asked nothing and sent itself a
  // hardcoded "Removed from the Context Hub".
  await user.click(screen.getByTestId("context-hub-unlink-accept"));
  expect(await screen.findByText(/Add a reason so the governance change remains auditable/)).toBeInTheDocument();
  expect(deleteCalls()).toHaveLength(0);

  await user.type(screen.getByLabelText("Reason"), "Retail no longer reports on this metric");
  await user.click(screen.getByTestId("context-hub-unlink-accept"));

  await waitFor(() => expect(deleteCalls()).toHaveLength(1));
  const [url, init] = deleteCalls()[0];
  expect(String(url)).toContain("/api/context/business-links/blink_1");
  expect(JSON.parse(String(init?.body))).toEqual({
    reason: "Retail no longer reports on this metric",
  });
});

// --- Closing a governed change costs something once anything was typed -------

test("asks before discarding a typed governed change, and keeps it when cancelled", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );
  await user.click(await screen.findByRole("button", { name: "Retail" }));
  await user.click(screen.getByRole("button", { name: "Add layer" }));
  await user.type(screen.getByLabelText("Name"), "Annual plans");

  // Escape is a close gesture like any other, and it used to throw the form
  // away without a word — the same defect the skill drawer was repaired for.
  await user.keyboard("{Escape}");
  expect(await screen.findByTestId("context-hub-discard-confirm")).toBeInTheDocument();

  await user.click(screen.getByTestId("context-hub-discard-cancel"));
  await waitFor(() => expect(screen.queryByTestId("context-hub-discard-confirm")).toBeNull());
  // The dialog is still there, and so is what was typed.
  expect(screen.getByRole("heading", { name: "Add descriptive layer" })).toBeInTheDocument();
  expect(screen.getByLabelText("Name")).toHaveValue("Annual plans");

  // Accepting is the only path that loses it.
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  await user.click(await screen.findByTestId("context-hub-discard-accept"));
  await waitFor(() => expect(screen.queryByRole("heading", { name: "Add descriptive layer" })).toBeNull());
});

test("closes an untouched dialog immediately — a question nobody needs is one people stop reading", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );
  await user.click(await screen.findByRole("button", { name: "Retail" }));

  // `edit` opens PREFILLED with the domain's name and description. A dirty flag
  // set on the first keystroke would be wrong here in the other direction; the
  // baseline is what makes an untouched prefilled form still count as clean.
  await user.click(screen.getByRole("button", { name: "Edit" }));
  expect(screen.getByLabelText("Name")).toHaveValue("Retail");
  await user.keyboard("{Escape}");

  await waitFor(() => expect(screen.queryByRole("heading", { name: "Edit Retail" })).toBeNull());
  expect(screen.queryByTestId("context-hub-discard-confirm")).toBeNull();
});

// --- An empty taxonomy is not a search that matched nothing ------------------

test("an empty taxonomy explains what a business domain is and names the way in", async () => {
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = String(input);
    if (url.startsWith("/api/context/business-taxonomy")) {
      return response({ org_id: "org_1", domains: [], classifications: [] });
    }
    if (url.startsWith("/api/context/business-links") && (init?.method ?? "GET") === "GET") {
      return response({ links: [] });
    }
    if (url.startsWith("/api/context/graph")) return response({ nodes: [], edges: [] });
    return response({ code: "not_found", message: "Unexpected request" }, 404);
  });
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );

  // "No matching business layer." is what a fruitless query earns, and nothing
  // had been searched. It also left a "+" as the only way forward.
  const create = await screen.findByRole("button", { name: "Create your first business domain" });
  expect(screen.queryByText("No matching business layer.")).toBeNull();
  expect(screen.getByText(/A Business Domain is the top of this map/)).toBeInTheDocument();

  // And it is the way in, not a label: it opens the creation dialog.
  await user.click(create);
  expect(screen.getByRole("heading", { name: "Add business domain" })).toBeInTheDocument();
});

test("a search that matched nothing keeps the sentence it earned", async () => {
  const user = userEvent.setup();
  render(
    <ContextHubLayout projectId="p1" activeView="graph" onNavigate={vi.fn()} renderContent={() => <div>Graph surface</div>} />,
  );
  await screen.findByRole("button", { name: "Retail" });

  await user.type(screen.getByLabelText("Search taxonomy"), "wholesale");

  expect(await screen.findByText("No matching business layer.")).toBeInTheDocument();
  // The taxonomy is not empty, so nothing here invites a first domain.
  expect(screen.queryByRole("button", { name: "Create your first business domain" })).toBeNull();
});
