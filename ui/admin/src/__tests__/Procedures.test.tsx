/**
 * Procedures (Context surface) — Story 44.1 rewiring onto the governed
 * `/api/context/procedures` corpus.
 *
 * Pins: the page reads GET /api/context/procedures (not the legacy
 * /api/procedures per-metric list, which moved to Governance as
 * ReconciliationMethods), a 422 frontmatter_invalide / 409 nom_deja_utilise is
 * shown verbatim with the draft preserved, and the empty state is honest.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Procedures from "../shell/pages/Procedures";

const PROCEDURE = {
  id: "proc_01JABCDEF",
  project_id: "p1",
  name: "Post-click attribution reconciliation",
  description: "GA4 is authoritative for the canonical conversion count.",
  frontmatter_yaml: 'name: "Post-click attribution reconciliation"\ndescription: "GA4 is authoritative"\n',
  body_md: "When GA4 and the ad platforms disagree, GA4 wins.",
  status: "active",
  created_by: "mary@toorow.com",
  created_at: "2026-07-20T10:00:00+00:00",
  updated_at: "2026-07-24T10:00:00+00:00",
  version_number: 1,
};

function resp(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}

interface Call {
  url: string;
  init: RequestInit;
}

function stubFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const mock = vi.fn((url: string, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    return Promise.resolve(handler(String(url), init));
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Procedures (Context) — data source", () => {
  it("reads GET /api/context/procedures, never the legacy /api/procedures list", async () => {
    const calls = stubFetch((url) => {
      if (url.includes("/api/context/procedures")) return resp(200, { procedures: [PROCEDURE] });
      return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
    });

    render(<Procedures projectId="p1" />);

    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());

    const urls = calls.map((c) => c.url);
    expect(urls.some((u) => u.includes("/api/context/procedures?project_id=p1"))).toBe(true);
    expect(urls.some((u) => u === "/api/procedures?project_id=p1")).toBe(false);
  });

  it("renders an honest empty state when there are no procedures", async () => {
    stubFetch((url) =>
      url.includes("/api/context/procedures") ? resp(200, { procedures: [] }) : resp(404, {}),
    );
    render(<Procedures projectId="p1" />);

    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());
    expect(screen.getByTestId("procedures-empty")).toHaveTextContent(/No procedures defined yet/i);
  });
});

describe("Procedures (Context) — extra frontmatter keys survive an edit", () => {
  it("preserves extra YAML keys beyond name/description when saving an edit", async () => {
    const user = userEvent.setup();
    const withExtraKeys = {
      ...PROCEDURE,
      frontmatter_yaml:
        'name: "Post-click attribution reconciliation"\n' +
        'description: "GA4 is authoritative"\n' +
        "owner: data-team\n" +
        "severity: high\n",
    };
    let patchBody: { frontmatter_yaml?: string } | null = null;
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { procedures: [withExtraKeys] });
      }
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}`) && init.method === "PATCH") {
        patchBody = JSON.parse(String(init.body)) as { frontmatter_yaml?: string };
        return resp(200, { ...withExtraKeys, name: "Renamed procedure", version_number: 2 });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Edit procedure" }));
    const nameInput = await screen.findByDisplayValue(PROCEDURE.name);
    await user.clear(nameInput);
    await user.type(nameInput, "Renamed procedure");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.getByText("Renamed procedure")).toBeInTheDocument());

    expect(patchBody).not.toBeNull();
    const sentYaml = patchBody?.frontmatter_yaml ?? "";
    expect(sentYaml).toContain('name: "Renamed procedure"');
    expect(sentYaml).toContain(`description: ${JSON.stringify(PROCEDURE.description)}`);
    // Extra keys the client never asked about are re-emitted verbatim.
    expect(sentYaml).toContain("owner: data-team");
    expect(sentYaml).toContain("severity: high");
  });

  it("consumes a block-scalar description's continuation lines instead of orphaning them (44.1 re-review)", async () => {
    const user = userEvent.setup();
    const withBlockScalar = {
      ...PROCEDURE,
      frontmatter_yaml:
        'name: "Post-click attribution reconciliation"\n' +
        "description: >\n" +
        "  GA4 is authoritative for the canonical\n" +
        "  conversion count across platforms.\n" +
        "owner: data-team\n",
    };
    let patchBody: { frontmatter_yaml?: string } | null = null;
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { procedures: [withBlockScalar] });
      }
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}`) && init.method === "PATCH") {
        patchBody = JSON.parse(String(init.body)) as { frontmatter_yaml?: string };
        return resp(200, { ...withBlockScalar, version_number: 2 });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() =>
      expect(screen.getByText("Post-click attribution reconciliation")).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "Edit procedure" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(patchBody).not.toBeNull());
    const sentYaml = patchBody?.frontmatter_yaml ?? "";
    // The folded scalar is replaced by a single flow-scalar description line;
    // its indented continuation lines must NOT be re-emitted as orphans
    // (which would be invalid YAML), while unrelated keys survive.
    expect(sentYaml).toContain("description: ");
    expect(sentYaml).not.toContain("  GA4 is authoritative for the canonical");
    expect(sentYaml).not.toContain("  conversion count across platforms.");
    expect(sentYaml).toContain("owner: data-team");
    const lines = sentYaml.split("\n").filter((l) => l.trim() !== "");
    for (const line of lines) {
      expect(line).not.toMatch(/^\s+/); // no orphan indented lines anywhere
    }
  });

  it("submits an empty name and shows the server's verbatim 422 rather than blocking client-side", async () => {
    const user = userEvent.setup();
    let createCalls = 0;
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { procedures: [] });
      }
      if (url === "/api/context/procedures" && init.method === "POST") {
        createCalls += 1;
        return resp(422, {
          code: "frontmatter_invalide",
          message: "Le frontmatter YAML doit contenir une clé 'name'.",
        });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Add procedure/i }));
    // Name left empty on purpose: Save must not be client-side disabled for
    // this — the server's 422 is the sole authority (Story 44.1 review).
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByTestId("procedure-editor-error")).toHaveTextContent(
        "Le frontmatter YAML doit contenir une clé 'name'.",
      );
    });
    expect(createCalls).toBe(1);
  });
});

describe("Procedures (Context) — owner (Story 44.11)", () => {
  it("sends the typed Owner value on the create POST body", async () => {
    const user = userEvent.setup();
    let createBody: { owner?: string | null } | null = null;
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { procedures: [] });
      }
      if (url === "/api/context/procedures" && init.method === "POST") {
        createBody = JSON.parse(String(init.body)) as { owner?: string | null };
        return resp(201, { ...PROCEDURE, name: "New proc", owner: "owner@toorow.com" });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Add procedure/i }));
    await user.type(screen.getByPlaceholderText(/Post-click attribution/i), "New proc");
    await user.type(screen.getByPlaceholderText(/what this procedure governs/i), "desc");
    await user.type(screen.getByTestId("procedure-editor-owner"), "owner@toorow.com");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(createBody).not.toBeNull());
    expect(createBody?.owner).toBe("owner@toorow.com");
  });

  it("pre-fills the Owner input from the procedure's raw owner when editing", async () => {
    const user = userEvent.setup();
    const owned = { ...PROCEDURE, owner: "prefilled@toorow.com" };
    stubFetch((url) =>
      url.includes("/api/context/procedures") ? resp(200, { procedures: [owned] }) : resp(404, {}),
    );
    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Edit procedure" }));
    expect(await screen.findByDisplayValue("prefilled@toorow.com")).toBeInTheDocument();
  });
});

describe("Procedures (Context) — archive failures are not silent", () => {
  it("shows the server's exact message on the failing card when archive fails", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { procedures: [PROCEDURE] });
      }
      if (
        url.includes(`/api/context/procedures/${PROCEDURE.id}/archive`) &&
        init.method === "POST"
      ) {
        return resp(409, { code: "conflict", message: "Cette procédure est référencée ailleurs." });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Archive" }));

    await waitFor(() => {
      expect(screen.getByTestId(`procedure-archive-error-${PROCEDURE.id}`)).toHaveTextContent(
        "Cette procédure est référencée ailleurs.",
      );
    });
    expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument();
  });
});

describe("Procedures (Context) — save failures preserve the draft", () => {
  it("shows the verbatim 422 frontmatter_invalide message and keeps the draft", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { procedures: [] });
      }
      if (url === "/api/context/procedures" && init.method === "POST") {
        return resp(422, {
          code: "frontmatter_invalide",
          message: "Le frontmatter YAML doit contenir une clé 'description'.",
        });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Add procedure/i }));
    const nameInput = await screen.findByPlaceholderText(/Post-click attribution/i);
    await user.type(nameInput, "Draft name kept on failure");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByTestId("procedure-editor-error")).toHaveTextContent(
        "Le frontmatter YAML doit contenir une clé 'description'.",
      );
    });
    expect(screen.getByDisplayValue("Draft name kept on failure")).toBeInTheDocument();
  });

  it("shows the verbatim 409 nom_deja_utilise message and keeps the draft", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { procedures: [PROCEDURE] });
      }
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}`) && init.method === "PATCH") {
        return resp(409, {
          code: "nom_deja_utilise",
          message: "Une procédure portant ce nom existe déjà dans ce périmètre.",
        });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Edit procedure" }));
    const nameInput = await screen.findByDisplayValue(PROCEDURE.name);
    await user.clear(nameInput);
    await user.type(nameInput, "Renamed to a duplicate");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByTestId("procedure-editor-error")).toHaveTextContent(
        "Une procédure portant ce nom existe déjà dans ce périmètre.",
      );
    });
    expect(screen.getByDisplayValue("Renamed to a duplicate")).toBeInTheDocument();
  });
});
