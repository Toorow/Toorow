/**
 * The Business Domain links are LINKS, and an unresolved one is still shown.
 *
 * `governance.md:72` asks a Semantic View to be "linkable to Business Domains".
 * The refs already reached the browser — the server puts `business_domain_refs`
 * in `summary` (`governance_read_model.py:761`, `:830`) — and the workbench
 * printed them through its generic field dump, as a raw value. A value is not a
 * link, which is the same defect the Project Settings associations had.
 *
 * The case worth a test of its own is the UNRESOLVED ref. Versions published
 * before `_validate_business_domain_refs` existed can carry a dangling id, and a
 * published version is IMMUTABLE — it can never be corrected in place. Dropping
 * such a ref from the list would make an unfixable row look clean; showing the
 * raw id with a warning is the only honest rendering.
 */
import { render, screen } from "@testing-library/react";
import { BusinessDomainLinks } from "../governance/SemanticModelTabs";
import { parsePath } from "../shell/router";

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock("../lib/apiFetch", () => ({
  apiGet: apiGetMock,
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  ApiError: class extends Error {},
}));

beforeEach(() => apiGetMock.mockReset());

const SCOPE = { organizationId: "org_EXAMPLE", projectId: "proj_EXAMPLE" };

it("renders each linked domain as a link to its owner in Master Data", async () => {
  apiGetMock.mockResolvedValue({ domains: [{ id: "bd_sales", name: "Sales" }] });
  render(<BusinessDomainLinks refs={["bd_sales"]} {...SCOPE} />);
  const link = await screen.findByTestId("business-domain-link-bd_sales");
  // Governance > Master Data owns the Business Domain (`README.md:97`).
  expect(link).toHaveAttribute(
    "href",
    "/org/org_EXAMPLE/project/proj_EXAMPLE/governance/master-data/object/business-domain/bd_sales/tab/overview",
  );
  expect(link).toHaveTextContent("Sales");
});

it("builds that address with the router, so it parses back to the same object", async () => {
  // This component was the last place in the file assembling an address by
  // concatenation, which the file's own header forbids: the shape was right,
  // and nothing checked that `master-data` declares a `business-domain` with an
  // `overview` tab. A rename would have kept producing a well-formed string
  // that `parsePath` refuses, and the link would have opened the unknown-route
  // screen. The round trip is what proves it is built, not typed.
  apiGetMock.mockResolvedValue({ domains: [{ id: "bd_sales", name: "Sales" }] });
  render(<BusinessDomainLinks refs={["bd_sales"]} {...SCOPE} />);
  const href = (await screen.findByTestId("business-domain-link-bd_sales")).getAttribute("href");
  const parsed = parsePath(href as string);
  expect(parsed.kind).toBe("resolved");
  if (parsed.kind !== "resolved") return;
  expect(parsed.route.workspace).toBe("governance");
  expect(parsed.route.section).toBe("master-data");
  expect(parsed.route.objectType).toBe("business-domain");
  expect(parsed.route.objectId).toBe("bd_sales");
  expect(parsed.route.tab).toBe("overview");
});

it("shows an unresolved ref as its raw id rather than hiding it", async () => {
  // The version that carries it is immutable. A list that silently drops the
  // dangling reference would show a clean object that cannot be repaired.
  apiGetMock.mockResolvedValue({ domains: [{ id: "bd_sales", name: "Sales" }] });
  render(<BusinessDomainLinks refs={["bd_sales", "bd_ghost"]} {...SCOPE} />);
  expect(await screen.findByTestId("business-domain-link-bd_ghost")).toHaveTextContent("bd_ghost");
  expect(screen.getByText(/unresolved/i)).toBeInTheDocument();
});

it("keeps the links clickable when the names cannot be read", async () => {
  // Names unreadable is not links unreadable: the ids belong to the object, not
  // to the taxonomy call.
  apiGetMock.mockRejectedValueOnce(new Error("HTTP 503"));
  render(<BusinessDomainLinks refs={["bd_sales"]} {...SCOPE} />);
  expect(await screen.findByTestId("business-domain-link-bd_sales")).toHaveAttribute(
    "href",
    expect.stringContaining("business-domain/bd_sales"),
  );
});

it("says none is linked instead of rendering an empty list", async () => {
  apiGetMock.mockResolvedValue({ domains: [] });
  render(<BusinessDomainLinks refs={[]} {...SCOPE} />);
  expect(await screen.findByTestId("business-domain-links-empty")).toHaveTextContent(
    /No Business Domain is linked/i,
  );
});
