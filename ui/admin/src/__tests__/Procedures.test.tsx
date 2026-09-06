/**
 * Procedures (Context surface) — Story 44.1 rewiring onto the governed
 * `/api/context/procedures` corpus.
 *
 * Pins: the page reads GET /api/context/procedures (not the legacy
 * /api/procedures per-metric list, which moved to Governance as
 * the Controls & Quality workbenches), a 422 invalid_frontmatter / 409 duplicate_name is
 * shown verbatim with the draft preserved, and the empty state is honest.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Procedures from "../shell/pages/Procedures";

const CAPS = { can_write: true, version_history: true, usage: false };

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
  capabilities: CAPS,
};

function resp(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}

interface Call {
  url: string;
  init: RequestInit;
}

/**
 * The bodies a test reads back off the stub.
 *
 * Recorded into a one-slot array rather than a `let body: T | null = null`: the
 * assignment happens inside the fetch stub, which TypeScript cannot order
 * against the assertions, so it kept the declaration's `null` and every read
 * came back `never` — the assertions below were typed against nothing at all.
 * Asserting the array's length says exactly what `not.toBeNull()` said.
 */
interface PatchBody {
  frontmatter_yaml?: string;
}
interface CreateBody {
  owner?: string | null;
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
      if (url.includes("/api/context/procedures")) return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
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
      url.includes("/api/context/procedures") ? resp(200, { capabilities: CAPS, procedures: [] }) : resp(404, {}),
    );
    render(<Procedures projectId="p1" />);

    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());
    expect(screen.getByTestId("procedures-empty")).toHaveTextContent(/No Skill has been written yet/i);
  });
});

describe("Procedures (Context) — what a Skill designates of the data model (AI-157)", () => {
  const WITH_REFERENCES = {
    ...PROCEDURE,
    frontmatter_yaml:
      'name: "Post-click attribution reconciliation"\n' +
      'description: "GA4 is authoritative"\n' +
      'mdm_tags: ["media_cost"]\n',
    body_md: "Compute {{media_cost}} per {{country_gone}}.",
  };
  const RESOLVED = {
    inline: {
      resolved: [
        {
          name: "media_cost",
          display_name: "Media cost",
          data_type: "numeric",
          field_kind: "metric",
          measure: "sum",
          description: "Net media spend",
          status: "approved",
        },
      ],
      unresolved: ["country_gone"],
    },
    tags: { resolved: [], unresolved: [] },
  };

  it("reads the DETAIL route on open and renders the resolved fields", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}`) && (!init.method || init.method === "GET")) {
        return resp(200, { ...WITH_REFERENCES, mdm_references: RESOLVED });
      }
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        // ⚠️ La LISTE ne porte pas `mdm_references` -- et c'est voulu : la
        // resoudre par ligne couterait une lecture de `app.target_fields` par
        // Skill pour un panneau qu'on n'ouvre pas.
        return resp(200, { capabilities: CAPS, procedures: [WITH_REFERENCES] });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit Skill" }));

    // AI-157 : `SkillStepList` savait rendre cette prop et RIEN ne la
    // remplissait -- la resolution ne vivait que dans l'outil MCP
    // `get_procedure`. Un agent savait de quoi parlait une Skill ; la personne
    // qui l'ecrit, non.
    expect(await screen.findByText("Media cost")).toBeInTheDocument();
    expect(screen.getByText("{{media_cost}}")).toBeInTheDocument();
    // Une reference qui NE resout PAS est MONTREE : c'est la seule trace qu'un
    // champ a disparu, et cette trace est le retour sur la Skill.
    expect(screen.getByText("{{country_gone}}")).toBeInTheDocument();
    expect(screen.getByText(/names no governed field/i)).toBeInTheDocument();

    expect(
      calls.some((c) => c.url.includes(`/api/context/procedures/${PROCEDURE.id}?project_id=p1`)),
    ).toBe(true);
  });

  it("stays editable when the catalogue read fails — it is a complement, not the Skill", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}`) && (!init.method || init.method === "GET")) {
        return resp(500, { code: "db_error", message: "catalogue unavailable" });
      }
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [WITH_REFERENCES] });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit Skill" }));

    // Le drawer s'ouvre et reste utilisable : faire echouer l'edition d'une
    // Skill parce qu'un catalogue n'a pas repondu serait un mauvais echange.
    expect(await screen.findByDisplayValue(PROCEDURE.name)).toBeInTheDocument();
    // Et le panneau se TAIT plutot que d'affirmer « aucun champ ».
    expect(screen.queryByText(/names no governed field/i)).not.toBeInTheDocument();
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
    const patched: PatchBody[] = [];
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [withExtraKeys] });
      }
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}`) && init.method === "PATCH") {
        patched.push(JSON.parse(String(init.body)) as PatchBody);
        return resp(200, { ...withExtraKeys, name: "Renamed procedure", version_number: 2 });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Edit Skill" }));
    const nameInput = await screen.findByDisplayValue(PROCEDURE.name);
    await user.clear(nameInput);
    await user.type(nameInput, "Renamed procedure");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.getByText("Renamed procedure")).toBeInTheDocument());

    expect(patched).toHaveLength(1);
    const sentYaml = patched[0]?.frontmatter_yaml ?? "";
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
    const patched: PatchBody[] = [];
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [withBlockScalar] });
      }
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}`) && init.method === "PATCH") {
        patched.push(JSON.parse(String(init.body)) as PatchBody);
        return resp(200, { ...withBlockScalar, version_number: 2 });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() =>
      expect(screen.getByText("Post-click attribution reconciliation")).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "Edit Skill" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(patched).toHaveLength(1));
    const sentYaml = patched[0]?.frontmatter_yaml ?? "";
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
        return resp(200, { capabilities: CAPS, procedures: [] });
      }
      if (url.startsWith("/api/context/procedures?") && init.method === "POST") {
        createCalls += 1;
        return resp(422, {
          code: "invalid_frontmatter",
          message: "Le frontmatter YAML doit contenir une clé 'name'.",
        });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Add Skill/i }));
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
    const created: CreateBody[] = [];
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [] });
      }
      if (url.startsWith("/api/context/procedures?") && init.method === "POST") {
        created.push(JSON.parse(String(init.body)) as CreateBody);
        return resp(201, { ...PROCEDURE, name: "New proc", owner: "owner@toorow.com" });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Add Skill/i }));
    await user.type(screen.getByPlaceholderText(/Post-click attribution/i), "New proc");
    await user.type(screen.getByPlaceholderText(/what this Skill governs/i), "desc");
    await user.type(screen.getByTestId("procedure-editor-owner"), "owner@toorow.com");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]?.owner).toBe("owner@toorow.com");
  });

  it("pre-fills the Owner input from the procedure's raw owner when editing", async () => {
    const user = userEvent.setup();
    const owned = { ...PROCEDURE, owner: "prefilled@toorow.com" };
    stubFetch((url) =>
      url.includes("/api/context/procedures") ? resp(200, { capabilities: CAPS, procedures: [owned] }) : resp(404, {}),
    );
    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Edit Skill" }));
    expect(await screen.findByDisplayValue("prefilled@toorow.com")).toBeInTheDocument();
  });
});

describe("Procedures (Context) — archiving asks before it happens", () => {
  it("names the Skill and names the way back before archiving anything", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Archive" }));

    const dialog = await screen.findByTestId("procedure-archive-confirm");
    expect(dialog).toHaveTextContent(PROCEDURE.name);
    // Since 2026-08-18 an archive IS reversible (`_restore_procedure`,
    // context_api.py:2298), so the copy names the gesture that reverses it
    // instead of claiming finality it no longer has.
    expect(dialog).toHaveTextContent(/Show archived/i);
    expect(dialog).toHaveTextContent(/restore it/i);
    expect(dialog).not.toHaveTextContent(/no way back/i);
    // Nothing has been archived yet: no POST left, and the card is still there.
    expect(calls.some((call) => call.url.includes("/archive"))).toBe(false);

    await user.click(screen.getByTestId("procedure-archive-confirm-cancel"));
    expect(calls.some((call) => call.url.includes("/archive"))).toBe(false);
    expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument();
  });

  it("archives only once the confirmation is accepted", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
      }
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}/archive`) && init.method === "POST") {
        return resp(200, { ...PROCEDURE, status: "archived", version_number: 2 });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Archive" }));
    await user.click(await screen.findByTestId("procedure-archive-confirm-accept"));

    await waitFor(() => expect(screen.queryByText(PROCEDURE.name)).not.toBeInTheDocument());
    expect(calls.filter((call) => call.url.includes("/archive")).length).toBe(1);
  });
});

describe("Procedures (Context) — the archive has a way back (2026-08-25)", () => {
  const ARCHIVED = {
    ...PROCEDURE,
    id: "proc_archived",
    name: "Retired anomaly reading",
    status: "archived",
    version_number: 4,
  };

  it("hides archived Skills until Show archived is turned on, then reads ?status=all", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/procedures?") && (!init.method || init.method === "GET")) {
        return url.includes("status=all")
          ? resp(200, { capabilities: CAPS, procedures: [PROCEDURE, ARCHIVED] })
          : resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());
    expect(screen.queryByText(ARCHIVED.name)).not.toBeInTheDocument();
    expect(calls.some((call) => call.url.includes("status=all"))).toBe(false);

    await user.click(screen.getByTestId("procedures-show-archived"));

    await waitFor(() => expect(screen.getByText(ARCHIVED.name)).toBeInTheDocument());
    expect(calls.some((call) => call.url.includes("status=all"))).toBe(true);
    // The archived row is marked, and does not pretend to be editable.
    expect(screen.getByTestId(`procedure-card-archived-${ARCHIVED.id}`)).toBeInTheDocument();
    expect(screen.getByText("Archived")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
    // Only the ACTIVE row keeps Edit/Archive — the server refuses a PATCH on
    // an archived row, so the console does not offer one.
    expect(screen.getAllByRole("button", { name: "Edit Skill" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Archive" })).toHaveLength(1);
  });

  it("asks before restoring, and cancelling sends nothing", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/procedures?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: url.includes("status=all") ? [ARCHIVED] : [] });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());
    await user.click(screen.getByTestId("procedures-show-archived"));
    await user.click(await screen.findByRole("button", { name: "Restore" }));

    const dialog = await screen.findByTestId("procedure-restore-confirm");
    expect(dialog).toHaveTextContent(ARCHIVED.name);
    expect(calls.some((call) => call.url.includes("/restore"))).toBe(false);

    await user.click(screen.getByTestId("procedure-restore-confirm-cancel"));
    expect(calls.some((call) => call.url.includes("/restore"))).toBe(false);
    expect(screen.getByText(ARCHIVED.name)).toBeInTheDocument();
  });

  it("posts the restore route with the row's version once the confirmation is accepted", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/procedures?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: url.includes("status=all") ? [ARCHIVED] : [] });
      }
      if (url.includes(`/api/context/procedures/${ARCHIVED.id}/restore`) && init.method === "POST") {
        return resp(200, { ...ARCHIVED, status: "active", version_number: 5 });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());
    await user.click(screen.getByTestId("procedures-show-archived"));
    await user.click(await screen.findByRole("button", { name: "Restore" }));
    await user.click(await screen.findByTestId("procedure-restore-confirm-accept"));

    const restore = calls.filter((call) => call.url.includes("/restore"));
    expect(restore).toHaveLength(1);
    expect(restore[0].url).toContain("project_id=p1");
    expect(JSON.parse(String(restore[0].init.body))).toEqual({ expected_version: 4 });
    // The row comes back active in place: the badge and the Restore action go.
    await waitFor(() =>
      expect(screen.queryByTestId(`procedure-card-archived-${ARCHIVED.id}`)).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Edit Skill" })).toBeInTheDocument();
    expect(screen.getByText("v5")).toBeInTheDocument();
  });

  it("shows the server's exact refusal on the card when the restore fails", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: url.includes("status=all") ? [ARCHIVED] : [] });
      }
      if (url.includes(`/api/context/procedures/${ARCHIVED.id}/restore`) && init.method === "POST") {
        return resp(409, {
          code: "not_archived",
          message: "This procedure is not archived, so there is nothing to restore.",
        });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());
    await user.click(screen.getByTestId("procedures-show-archived"));
    await user.click(await screen.findByRole("button", { name: "Restore" }));
    await user.click(await screen.findByTestId("procedure-restore-confirm-accept"));

    await waitFor(() =>
      expect(screen.getByTestId(`procedure-archive-error-${ARCHIVED.id}`)).toHaveTextContent(
        "This procedure is not archived, so there is nothing to restore.",
      ),
    );
  });

  it("keeps the archived row visible in place when archiving with Show archived on", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
      }
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}/archive`) && init.method === "POST") {
        return resp(200, { ...PROCEDURE, status: "archived", version_number: 2 });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());
    await user.click(screen.getByTestId("procedures-show-archived"));
    await user.click(await screen.findByRole("button", { name: "Archive" }));
    await user.click(await screen.findByTestId("procedure-archive-confirm-accept"));

    // It does not vanish: it becomes the archived row the person can restore.
    await waitFor(() =>
      expect(screen.getByTestId(`procedure-card-archived-${PROCEDURE.id}`)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
  });
});

describe("Procedures (Context) — archive failures are not silent", () => {
  it("shows the server's exact message on the failing card when archive fails", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
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
    await user.click(await screen.findByTestId("procedure-archive-confirm-accept"));

    await waitFor(() => {
      expect(screen.getByTestId(`procedure-archive-error-${PROCEDURE.id}`)).toHaveTextContent(
        "Cette procédure est référencée ailleurs.",
      );
    });
    expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument();
  });
});

describe("Procedures (Context) — save failures preserve the draft", () => {
  it("shows the verbatim 422 invalid_frontmatter message and keeps the draft", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [] });
      }
      if (url.startsWith("/api/context/procedures?") && init.method === "POST") {
        return resp(422, {
          code: "invalid_frontmatter",
          message: "Le frontmatter YAML doit contenir une clé 'description'.",
        });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("procedures-empty")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Add Skill/i }));
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

  it("shows the verbatim 409 duplicate_name message and keeps the draft", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
      }
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}`) && init.method === "PATCH") {
        return resp(409, {
          code: "duplicate_name",
          message: "Une procédure portant ce nom existe déjà dans ce périmètre.",
        });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Edit Skill" }));
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

describe("Procedures — optimistic concurrency", () => {
  it("reloads the authoritative version after a conflict while preserving the draft", async () => {
    const user = userEvent.setup();
    let patchCalls = 0;
    let retriedExpectedVersion: number | null = null;
    stubFetch((url, init) => {
      if (url.includes("/api/context/procedures?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
      }
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}?`) && (!init.method || init.method === "GET")) {
        return resp(200, { ...PROCEDURE, name: "Server procedure", version_number: 2 });
      }
      if (url.includes(`/api/context/procedures/${PROCEDURE.id}?`) && init.method === "PATCH") {
        patchCalls += 1;
        const body = JSON.parse(String(init.body)) as { expected_version: number; frontmatter_yaml: string };
        if (patchCalls === 1) {
          return resp(409, { code: "version_conflict", message: "Procedure changed elsewhere." });
        }
        retriedExpectedVersion = body.expected_version;
        return resp(200, { ...PROCEDURE, name: "My preserved procedure", frontmatter_yaml: body.frontmatter_yaml, version_number: 3 });
      }
      return resp(404, {});
    });

    render(<Procedures projectId="p1" />);
    await screen.findByText(PROCEDURE.name);
    await user.click(screen.getByRole("button", { name: "Edit Skill" }));
    const name = screen.getByDisplayValue(PROCEDURE.name);
    await user.clear(name);
    await user.type(name, "My preserved procedure");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText(/Latest version v2 loaded/)).toBeInTheDocument();
    expect(screen.getByDisplayValue("My preserved procedure")).toBeInTheDocument();
    expect(screen.getByText("Server procedure")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(retriedExpectedVersion).toBe(2));
  });
});
// ── AI-158 — VOIR le retour, et AGIR dessus
//
// Une file qu'on ne peut que fermer est une file qu'on ferme sans rien
// corriger. La remarque est epinglee a une VERSION : la reponse juste n'est pas
// d'effacer la note, c'est de sortir une NOUVELLE VERSION de la Skill. C'est
// pour cela que la boucle se ferme ici, ou vit l'editeur — et non sur l'etabli,
// qui aurait duplique le chemin de sauvegarde et sa reprise de conflit.

describe("Procedures (Context) — see the feedback, adjust the Skill (AI-158)", () => {
  const REMARK = {
    id: "crr_1",
    node_id: PROCEDURE.id,
    node_type: "procedure",
    node_version: 1,
    note: "GA4 is no longer authoritative here.",
    requested_by: "reader@example.com",
    origin: "human",
    status: "open",
    created_at: "2026-08-04T09:00:00Z",
    proposed_change: null,
  };

  /** La page + sa file, avec la liste des remarques rendue a chaque relecture.
   *
   * ⚠️ Le droit d'ecrire est porte par LA PROCEDURE, pas par la page : la carte
   * lit `procedure.capabilities.can_write`. Un test qui ne baisse que les
   * capabilities de l'enveloppe croit tester le lecture-seule et ne teste rien.
   */
  function stubWithQueue(queues: unknown[][], procedure: unknown = PROCEDURE) {
    let read = 0;
    return stubFetch((url) => {
      if (url.includes("/api/context/review-requests")) {
        const rows = queues[Math.min(read, queues.length - 1)];
        read += 1;
        return resp(200, { requests: rows, can_resolve: true });
      }
      if (url.includes("/api/context/procedures")) {
        return resp(200, { capabilities: CAPS, procedures: [procedure] });
      }
      return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
    });
  }

  it("reads the project queue ONCE and distributes it, never one request per Skill", async () => {
    const calls = stubWithQueue([[REMARK]]);
    render(<Procedures projectId="p1" />);
    await waitFor(() =>
      expect(screen.getByText("GA4 is no longer authoritative here.")).toBeInTheDocument(),
    );
    const queueCalls = calls.filter((c) => c.url.includes("/review-requests"));
    expect(queueCalls).toHaveLength(1);
    // Sans `node_id` : c'est la file du PROJET, distribuee par Skill cote vue.
    expect(queueCalls[0].url).not.toContain("node_id");
  });

  it("offers « Adjust this Skill » on the remark and opens the editor prefilled", async () => {
    const user = userEvent.setup();
    stubWithQueue([[REMARK]]);
    render(<Procedures projectId="p1" />);
    const adjust = await screen.findByTestId(`procedure-review-${PROCEDURE.id}-adjust-crr_1`);
    await user.click(adjust);
    // L'editeur existant, sur CETTE Skill — pas un second chemin d'ecriture.
    await waitFor(() =>
      expect(screen.getByDisplayValue(PROCEDURE.body_md)).toBeInTheDocument(),
    );
  });

  it("answers a remark with a NEW VERSION — the PATCH carries expected_version", async () => {
    const user = userEvent.setup();
    let patched: RequestInit | null = null;
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/review-requests")) {
        return resp(200, { requests: [REMARK], can_resolve: true });
      }
      if (init?.method === "PATCH") {
        patched = init;
        return resp(200, { ...PROCEDURE, version_number: 2, body_md: "GA4 is no longer authoritative." });
      }
      if (url.includes("/api/context/procedures")) {
        return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
      }
      return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
    });

    render(<Procedures projectId="p1" />);
    await user.click(await screen.findByTestId(`procedure-review-${PROCEDURE.id}-adjust-crr_1`));
    const body = await screen.findByDisplayValue(PROCEDURE.body_md);
    await user.clear(body);
    await user.type(body, "GA4 is no longer authoritative.");
    await user.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(patched).not.toBeNull());
    const sent = JSON.parse(String((patched as unknown as RequestInit).body));
    // Epinglage des deux cotes : la remarque parle de v1, l'ecriture exige v1.
    expect(sent.expected_version).toBe(PROCEDURE.version_number);
    // `contain` et non `toBe` : l'editeur structure le corps en etapes
    // (`SkillStepList`), donc il ressort « ## Step 1\n\n<texte> ». Ce que ce
    // test tient est que la correction PART, pas la mise en forme de l'editeur.
    expect(sent.body_md).toContain("GA4 is no longer authoritative.");
    // Et la file est RELUE apres la sauvegarde : la remarque epinglee a v1 doit
    // maintenant se lire « older version ». On ne le recalcule pas de tete.
    await waitFor(() =>
      expect(calls.filter((c) => c.url.includes("/review-requests")).length).toBeGreaterThan(1),
    );
  });

  it("marks the remark « older version » once the Skill has moved past it", async () => {
    // Deuxieme lecture : la Skill est en v2, la remarque parle toujours de v1.
    stubFetch((url) => {
      if (url.includes("/api/context/review-requests")) {
        return resp(200, { requests: [REMARK], can_resolve: true });
      }
      if (url.includes("/api/context/procedures")) {
        return resp(200, { capabilities: CAPS, procedures: [{ ...PROCEDURE, version_number: 2 }] });
      }
      return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
    });
    render(<Procedures projectId="p1" />);
    expect(
      await screen.findByTestId(`procedure-review-${PROCEDURE.id}-stale-crr_1`),
    ).toBeInTheDocument();
  });

  it("says NOTHING on a Skill with no remark — N empty panels would bury the real ones", async () => {
    stubWithQueue([[]]);
    render(<Procedures projectId="p1" />);
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());
    expect(screen.queryByText(/No open remark on this node/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Open remarks/)).not.toBeInTheDocument();
  });

  it("refuses to offer the adjustment on a read-only Skill", async () => {
    stubWithQueue([[REMARK]], {
      ...PROCEDURE,
      capabilities: { ...CAPS, can_write: false },
    });
    render(<Procedures projectId="p1" />);
    const adjust = await screen.findByTestId(`procedure-review-${PROCEDURE.id}-adjust-crr_1`);
    // Presente mais inerte : cacher le geste ferait croire qu'il n'existe pas,
    // alors que le manque est un DROIT — et la remarque reste lisible.
    expect(adjust).toBeDisabled();
  });

  it("keeps the Skills readable when the remark queue is unavailable", async () => {
    stubFetch((url) => {
      if (url.includes("/api/context/review-requests")) {
        return resp(500, { code: "db_error", message: "Failed to retrieve review requests" });
      }
      if (url.includes("/api/context/procedures")) {
        return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
      }
      return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
    });
    render(<Procedures projectId="p1" />);
    // On perd le retour, jamais l'outil.
    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Edit Skill/i })).toBeEnabled();
  });
});

describe("Procedures (Context) — the fallback is readable (AI-210)", () => {
  it("shows what the search keeps dropping, and opens the skill where the link is assigned", async () => {
    const user = userEvent.setup();
    stubFetch((url) => {
      if (url.includes("/api/context/recurrent-fates")) {
        return resp(200, {
          minimum: 3,
          fates: [{
            candidate_id: PROCEDURE.id,
            candidate_kind: "procedure",
            reason: "out_of_scope",
            times: 7,
            last_query: "attribution window",
          }],
        });
      }
      if (url.includes("/api/context/procedures")) {
        return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
      }
      return resp(200, { requests: [], can_resolve: false });
    });

    render(<Procedures projectId="p1" />);

    const row = await screen.findByTestId(`recurrent-fate-${PROCEDURE.id}`);
    expect(row).toHaveTextContent(PROCEDURE.name);
    expect(row).toHaveTextContent("out of scope");
    expect(row).toHaveTextContent("7 times");
    expect(row).toHaveTextContent("attribution window");

    // La reparation d'un lien manquant est une affectation de taxonomie, donc
    // elle se fait dans l'editeur -- le panneau y mene, il n'invente rien.
    await user.click(screen.getByRole("button", { name: "Open skill" }));
    expect(await screen.findByRole("dialog", { name: /Edit/ })).toBeInTheDocument();
  });

  it("stays silent when nothing was dropped often enough", async () => {
    stubFetch((url) => {
      if (url.includes("/api/context/recurrent-fates")) return resp(200, { minimum: 3, fates: [] });
      if (url.includes("/api/context/procedures")) return resp(200, { capabilities: CAPS, procedures: [PROCEDURE] });
      return resp(200, { requests: [], can_resolve: false });
    });

    render(<Procedures projectId="p1" />);

    await waitFor(() => expect(screen.getByText(PROCEDURE.name)).toBeInTheDocument());
    expect(screen.queryByTestId("procedures-recurrent-fates")).toBeNull();
  });
});
