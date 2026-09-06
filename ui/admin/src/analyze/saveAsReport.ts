/**
 * Save a Result's question as a configured Report (Story 50.3 AC4).
 *
 * WHY THIS FILE EXISTS. `POST /api/projects/{id}/analyze/reports` has been
 * served since 50.3 — `analyze_artifacts_api.py:133` calls `create_report`,
 * which mints the head and its version 1 — and `ui/admin/src` contained **zero**
 * call sites. The Reports screen says so out loud in its empty state: *"Creating
 * one is not yet reachable from this console — the server accepts it, no screen
 * offers it."* That sentence was written because an earlier empty state had
 * INSTRUCTED the path ("Save a question from Explore"), and the path did not
 * exist. This file is the path.
 *
 * WHY FROM A RESULT, and not a "New report" button on the collection. A Report
 * pins a **Query Spec version** (`analyze-and-test.md:50`: *"the Report itself
 * is not a cached answer"*). The only place a Query Spec version exists with a
 * person's intent attached to it is a Result they just obtained in Explore. A
 * create button on the collection would have to invent a question first, which
 * is Explore's job and not a dialog's.
 *
 * WHAT IS NOT SENT. No `presentation`: `analyze-and-test.md` keeps presentation
 * a separate contract and the Reports workbench states plainly when none has
 * been accepted, rather than showing an empty chart picker. Seeding one here
 * would manufacture a presentation intent the person never expressed. Likewise
 * `seed_origin` stays the server default `project` — this Report comes from the
 * Project's own question, not from a connector pack.
 */
import { apiPost } from "../lib/apiFetch";

export interface CreatedReport {
  report_id: string;
  current_version_id?: string;
}

/**
 * Create the Report head and its version 1 from one Result's Query Spec version.
 *
 * Returns the server's answer. A refusal travels out as the `ApiError` the
 * caller renders: a Report the server refused is not one to retry with a
 * quieter body.
 */
export async function createReportFromResult(
  projectId: string,
  querySpecVersionId: string,
  label: string,
  description?: string,
): Promise<CreatedReport> {
  const trimmed = label.trim();
  if (!trimmed) {
    // The server refuses an empty label too (`analyze_artifacts.py:304`). Failing
    // here keeps the refusal legible instead of round-tripping for a 422.
    throw new Error("A Report needs a name.");
  }
  const body: Record<string, unknown> = {
    label: trimmed,
    query_spec_version_id: querySpecVersionId,
  };
  const trimmedDescription = description?.trim();
  if (trimmedDescription) body.description = trimmedDescription;

  return apiPost<CreatedReport>(
    `/api/projects/${encodeURIComponent(projectId)}/analyze/reports`,
    body,
  );
}
