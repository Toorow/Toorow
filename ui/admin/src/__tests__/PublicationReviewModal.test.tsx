/**
 * Governed publication — the human confirmation surface.
 *
 * `publication_reviews_api.py#_prepare_publication_review_console` says plainly
 * what is at stake: *"without an out-of-band
 * retrieval path, nobody can ever call `confirm_and_publish` and governed
 * publication is unreachable."* This modal IS that path, and it implemented a
 * flow that did not exist — no prepare, no body, no header — so the whole
 * capability was unreachable while three routes served it.
 *
 * Pinned here, because each of these was a separate fatal defect:
 *   - the review is MINTED by this surface, not received ready-made;
 *   - the confirmation secret goes back on confirm, and NEVER into the DOM;
 *   - both mutations carry `Idempotency-Key` (422 without it);
 *   - rollback carries `target_mapping_version_id` (422 without it) and is not
 *     offered at all when there is no prior version to return to.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import PublicationReviewModal from "../governance/PublicationReviewModal";

const SECRET = "opaque-secret-returned-once";

const REVIEW = {
  confirmation_id: "conf_1",
  scope: { actor: "owner@example.com", project_id: "proj_1", datastream_id: "ds_1" },
  versions: {
    candidate_mapping_version_id: "mv_candidate",
    prior_mapping_version_id: "mv_live",
    policy_version: "policy_7",
  },
  impact: { advances_pointer: true, from_version_id: "mv_live", to_version_id: "mv_candidate" },
};

function stub(overrides: { review?: unknown; mutation?: { ok: boolean; status: number; body?: unknown } } = {}) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    if (url.endsWith("/publication-reviews")) {
      return {
        ok: true, status: 201,
        json: async () => overrides.review ?? { ...REVIEW, confirmation_secret: SECRET },
      };
    }
    const m = overrides.mutation ?? { ok: true, status: 200, body: { published: true } };
    return { ok: m.ok, status: m.status, json: async () => m.body ?? {} };
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

const props = {
  open: true, projectId: "proj_1", proposalId: "prop_1",
  onClose: () => undefined, onPublished: () => undefined,
};

describe("Publication review", () => {
  it("mints the review itself and shows which pointer moves where", async () => {
    const calls = stub();
    render(<PublicationReviewModal {...props} />);

    expect(await screen.findByTestId("publication-review")).toBeInTheDocument();
    const prepare = calls.find((c) => c.url.endsWith("/publication-reviews"));
    expect(prepare).toBeDefined();
    expect(JSON.parse(String(prepare!.init.body))).toEqual({ proposal_id: "prop_1" });

    expect(screen.getByText("mv_live")).toBeInTheDocument();
    expect(screen.getByText("mv_candidate")).toBeInTheDocument();
  });

  it("never renders the confirmation secret", async () => {
    stub();
    const { container } = render(<PublicationReviewModal {...props} />);
    await screen.findByTestId("publication-review");
    // It is returned exactly once, to a human. It must live in a ref and reach
    // the DOM nowhere — an agent reading the rendered tree must not find it.
    expect(container.ownerDocument.body.innerHTML).not.toContain(SECRET);
  });

  it("sends the secret and an Idempotency-Key on confirm", async () => {
    const calls = stub();
    render(<PublicationReviewModal {...props} />);
    await screen.findByTestId("publication-review");

    await userEvent.click(screen.getByTestId("publication-confirm"));

    await waitFor(() => {
      const confirm = calls.find((c) => c.url.includes("/confirm"));
      expect(confirm).toBeDefined();
      expect(JSON.parse(String(confirm!.init.body))).toEqual({ confirmation_secret: SECRET });
      expect((confirm!.init.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
    });
  });

  it("rolls back to the named prior version, with its own key", async () => {
    const calls = stub();
    render(<PublicationReviewModal {...props} />);
    await screen.findByTestId("publication-review");

    await userEvent.click(screen.getByTestId("publication-rollback"));

    await waitFor(() => {
      const rollback = calls.find((c) => c.url.includes("/rollback"));
      expect(rollback).toBeDefined();
      // 422 without it — the old modal sent no body at all.
      expect(JSON.parse(String(rollback!.init.body))).toEqual({ target_mapping_version_id: "mv_live" });
      expect((rollback!.init.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
    });
  });

  it("offers no rollback on a first publication", async () => {
    stub({ review: { ...REVIEW, versions: { ...REVIEW.versions, prior_mapping_version_id: null }, confirmation_secret: SECRET } });
    render(<PublicationReviewModal {...props} />);
    await screen.findByTestId("publication-review");

    expect(screen.queryByTestId("publication-rollback")).not.toBeInTheDocument();
    expect(screen.getByText(/first publication/i)).toBeInTheDocument();
  });

  it("refuses to offer a confirmation it cannot carry", async () => {
    // The secret is returned ONCE. A review that arrives without it can never be
    // confirmed, and saying so beats a button that answers 422.
    stub({ review: { ...REVIEW } });
    render(<PublicationReviewModal {...props} />);
    await screen.findByTestId("publication-review");

    expect(screen.getByTestId("publication-confirm")).toBeDisabled();
    expect(screen.getByTestId("publication-review-error")).toHaveTextContent(/returned once/i);
  });

  it("reports a refused publication instead of claiming it happened", async () => {
    stub({ mutation: { ok: false, status: 409, body: { code: "stale_review", message: "Confirmation refused" } } });
    render(<PublicationReviewModal {...props} />);
    await screen.findByTestId("publication-review");

    await userEvent.click(screen.getByTestId("publication-confirm"));

    expect(await screen.findByTestId("publication-review-error")).toHaveTextContent(/Confirmation refused/i);
  });
});
