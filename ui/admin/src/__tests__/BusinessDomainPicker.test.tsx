/**
 * The picker may only offer what the server will accept.
 *
 * `governance.md:72` makes a Semantic View linkable to Business Domains, and
 * `_validate_business_domain_refs` (`server/core/semantic_model.py`) refuses an
 * id that resolves to nothing, one from another organization, an archived one,
 * and a duplicate. A form that could produce any of those would be a screen that
 * invites a refusal — worse than no screen.
 *
 * So these tests pin the three properties that keep the two sides in step:
 * archived domains are never offered, an unreadable taxonomy says so instead of
 * rendering an empty list, and selection cannot produce a duplicate.
 *
 * The empty case matters as much: `README.md` invariant 8 — unknown or
 * unavailable is never presented as healthy. "No Business Domain exists" and
 * "the taxonomy could not be read" are different sentences and must stay so.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import BusinessDomainPicker from "../governance/BusinessDomainPicker";

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock("../lib/apiFetch", () => ({
  apiGet: apiGetMock,
  apiPost: vi.fn(),
  ApiError: class extends Error {},
}));

beforeEach(() => apiGetMock.mockReset());

it("offers the active domains of the organization", async () => {
  apiGetMock.mockResolvedValue({
    domains: [
      { id: "bd_sales", name: "Sales", status: "active" },
      { id: "bd_finance", name: "Finance", status: "active" },
    ],
  });
  render(<BusinessDomainPicker projectId="proj_EXAMPLE" value={[]} onChange={vi.fn()} />);
  expect(await screen.findByText("Sales")).toBeInTheDocument();
  expect(screen.getByText("Finance")).toBeInTheDocument();
  // 2026-08-22, story 67.8 — CE TEST GRAVAIT LE DEFAUT COMME LA CIBLE. Il
  // affirmait que l'appel devait partir SANS `project_id`, au motif qu'« une
  // portee projet serait une seconde source de verite ». Le raisonnement est
  // celui qui a fait le bug : la route EST scopee projet cote serveur, et
  // `_run_scoped` (`business_taxonomy_api.py:117`) refuse par un 422
  // `project_id is required` avant d'ouvrir quoi que ce soit. Envoyer le
  // Projet n'est pas une devinette, c'est ce que le serveur exige — sans lui,
  // le picker rendait « indisponible » a chaque ouverture, pour tout le monde.
  expect(apiGetMock).toHaveBeenCalledWith(
    "/api/context/business-taxonomy?project_id=proj_EXAMPLE",
  );
});

it("never offers an archived domain, because the server refuses one", async () => {
  apiGetMock.mockResolvedValue({
    domains: [
      { id: "bd_live", name: "Live", status: "active" },
      { id: "bd_old", name: "Retired", status: "archived" },
    ],
  });
  render(<BusinessDomainPicker projectId="proj_EXAMPLE" value={[]} onChange={vi.fn()} />);
  expect(await screen.findByText("Live")).toBeInTheDocument();
  expect(screen.queryByText("Retired")).not.toBeInTheDocument();
});

it("distinguishes an unreadable taxonomy from an organization with no domain", async () => {
  // `...Once`, and it is not interchangeable here: the persistent
  // `mockRejectedValue` made this runner report `Error: HTTP 503` as an
  // unhandled error and fail the test, while a throwaway probe proved the
  // component caught it and rendered exactly what is asserted below. A
  // synchronous `throw` behaved the same way. `ClaimInstance.test.tsx:153`
  // already uses the `Once` form for a failing call — this follows it rather
  // than inventing a fourth way to say the same thing.
  apiGetMock.mockRejectedValueOnce(new Error("HTTP 503"));
  render(<BusinessDomainPicker projectId="proj_EXAMPLE" value={[]} onChange={vi.fn()} />);
  const unavailable = await screen.findByTestId("business-domain-picker-unavailable");
  expect(unavailable).toHaveTextContent(/could not be read/i);
  // And it does not claim the object cannot be created — the link is optional.
  expect(unavailable).toHaveTextContent(/without one/i);
});

it("says an organization has no domain without dressing it as a failure", async () => {
  apiGetMock.mockResolvedValue({ domains: [] });
  render(<BusinessDomainPicker projectId="proj_EXAMPLE" value={[]} onChange={vi.fn()} />);
  expect(await screen.findByTestId("business-domain-picker-empty")).toHaveTextContent(
    /no active Business Domain/i,
  );
});

it("toggles a selection off rather than adding it twice", async () => {
  // `duplicate_business_domain_ref` is a server refusal. The form must not be
  // able to produce one at all.
  apiGetMock.mockResolvedValue({ domains: [{ id: "bd_sales", name: "Sales", status: "active" }] });
  const onChange = vi.fn();
  const user = userEvent.setup();
  const { rerender } = render(<BusinessDomainPicker projectId="proj_EXAMPLE" value={[]} onChange={onChange} />);
  await user.click(await screen.findByTestId("business-domain-option-bd_sales"));
  expect(onChange).toHaveBeenCalledWith(["bd_sales"]);

  rerender(<BusinessDomainPicker projectId="proj_EXAMPLE" value={["bd_sales"]} onChange={onChange} />);
  await user.click(screen.getByTestId("business-domain-option-bd_sales"));
  await waitFor(() => expect(onChange).toHaveBeenLastCalledWith([]));
});
