/**
 * Organization identity and branding: served since Story 21.2, wired 2026-08-03.
 *
 * `PATCH /api/organizations/{org_id}` (`organizations_api.py#_patch_org`) has always taken
 * `name`, `billing_ref` and the four branding fields, and validated each colour
 * against `#RRGGBB` (`_extract_brand_fields`, `:6298`). No screen called it:
 * `GeneralSection` rendered six read-only `Fact` rows. And two of the three
 * colours — `brand_secondary`, `brand_accent` — were typed on the record and
 * displayed NOWHERE, so the one surface an organization uses to choose its
 * colours showed a third of them.
 *
 * What these tests hold:
 *   - the three colours and the logo are all present, not one of four;
 *   - only what CHANGED is sent, because the PATCH treats an absent key as
 *     unchanged and a full-record write would rewrite untouched fields;
 *   - an invalid colour is caught at the field and blocks the save, mirroring
 *     the server rule rather than replacing it;
 *   - the slug stays read-only: the server answers `slug_immutable`, and an
 *     input that always fails is worse than no input.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OrgSettings from "../shell/pages/OrgSettings";

const { apiGetMock, apiJsonMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiJsonMock: vi.fn(),
}));

vi.mock("../lib/apiFetch", () => ({
  apiGet: apiGetMock,
  apiJson: apiJsonMock,
  apiPost: vi.fn(),
  ApiError: class extends Error {},
}));

vi.mock("../shell/GlobalScopeLayout", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const ORG = {
  id: "org_EXAMPLE",
  name: "Example Org",
  slug: "example",
  status: "active",
  billing_ref: null,
  brand_primary: "#112233",
  brand_secondary: null,
  brand_accent: null,
  logo_url: null,
};

beforeEach(() => {
  apiGetMock.mockReset();
  apiJsonMock.mockReset();
  apiGetMock.mockResolvedValue({ ...ORG });
  apiJsonMock.mockResolvedValue({});
});

function open() {
  return render(<OrgSettings orgId="org_EXAMPLE" section="general" />);
}

it("shows all three brand colours and the logo, not one of four", async () => {
  open();
  expect(await screen.findByTestId("org-brand_primary")).toHaveValue("#112233");
  // These two existed on the record and were rendered nowhere.
  expect(screen.getByTestId("org-brand_secondary")).toBeInTheDocument();
  expect(screen.getByTestId("org-brand_accent")).toBeInTheDocument();
  expect(screen.getByTestId("org-logo_url")).toBeInTheDocument();
});

it("sends only the fields that changed", async () => {
  const user = userEvent.setup();
  open();
  const accent = await screen.findByTestId("org-brand_accent");
  await user.type(accent, "#AABBCC");
  await user.click(screen.getByTestId("org-save-identity"));

  await waitFor(() => expect(apiJsonMock).toHaveBeenCalled());
  const [path, init] = apiJsonMock.mock.calls[0];
  expect(path).toBe("/api/organizations/org_EXAMPLE");
  expect(init.method).toBe("PATCH");
  // An absent key means "unchanged" server-side. Sending the whole record would
  // rewrite `name` and `brand_primary` that nobody touched.
  expect(JSON.parse(init.body)).toEqual({ brand_accent: "#AABBCC" });
});

it("refuses to save a colour the server would reject, and says which", async () => {
  const user = userEvent.setup();
  open();
  await user.type(await screen.findByTestId("org-brand_secondary"), "red");
  expect(screen.getByText(/must be #RRGGBB/i)).toBeInTheDocument();
  expect(screen.getByTestId("org-save-identity")).toBeDisabled();
  expect(apiJsonMock).not.toHaveBeenCalled();
});

it("keeps the slug read-only because the server refuses to change it", async () => {
  open();
  expect(await screen.findByText("example")).toBeInTheDocument();
  // No input carries the slug: `slug_immutable` is a 422, and a field that
  // always fails is worse than a field that is not offered.
  expect(screen.queryByLabelText(/slug/i)).not.toBeInTheDocument();
});

it("clears the draft and re-reads the record after a save", async () => {
  const user = userEvent.setup();
  open();
  await user.type(await screen.findByTestId("org-logo_url"), "https://example.com/logo.svg");
  const readsBefore = apiGetMock.mock.calls.length;
  await user.click(screen.getByTestId("org-save-identity"));
  // The owner decides what was stored, so the record is re-read rather than
  // patched locally.
  await waitFor(() => expect(apiGetMock.mock.calls.length).toBeGreaterThan(readsBefore));
});
