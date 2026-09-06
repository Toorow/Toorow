/**
 * The one API stub the screen sandboxes install, and the only one that can be
 * told to REFUSE.
 *
 * Why this exists. Both sandboxes used to assign `window.fetch` unconditionally
 * in their component body. That override wins over Playwright's `page.route`,
 * because no request ever reaches the network layer for Playwright to
 * intercept — so `scripts/shoot_screen.py`, whose whole point is to capture each
 * screen twice (with data, and with the API refusing), produced two BYTE-
 * IDENTICAL images for every screen these sandboxes serve. Measured 2026-08-03
 * on `DataWorkspace` and `Sources`: `-data.png` and `-error.png` were the same
 * file.
 *
 * The consequence is worse than a missing capture. A design review that opened
 * `-error.png` saw the success state and had no way to know it: the error
 * surface of six Data screens has never once been looked at, while the harness
 * reported that it had. That is the instrument polluting its own measurement.
 *
 * The refusal is therefore addressable, like everything else in the sandbox:
 *
 *     /debug/screen?name=DataWorkspace              -> fixtures
 *     /debug/screen?name=DataWorkspace&api=refusing -> every call answers 503
 *
 * A screen's error surface is part of what has to be judged, and it is the state
 * nobody ever checks.
 */

/** What the sandbox answers for one URL: the payload, and the status to send. */
export interface SandboxAnswer {
  body: unknown;
  status: number;
}

/** True when the address asked for the refusing variant. */
export function sandboxIsRefusing(): boolean {
  return new URLSearchParams(window.location.search).get("api") === "refusing";
}

/**
 * Replace `window.fetch` for the sandbox.
 *
 * `answer` maps a URL to its fixture. It is not consulted at all in the refusing
 * variant — a fixture that still answered would make the refusal partial, and a
 * partial refusal is exactly the state a screen is least likely to handle and
 * most likely to look fine in.
 */
export function installSandboxApi(answer: (url: string) => SandboxAnswer): void {
  const refusing = sandboxIsRefusing();
  window.fetch = async (input) => {
    if (refusing) {
      return new Response(
        // No trailing period: the screens append their own sentence to this
        // message ("… No fleet has been substituted"), and the first capture
        // showed the seam as a double period.
        JSON.stringify({ detail: "The sandbox was asked to refuse this call" }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      );
    }
    const { body, status } = answer(String(input));
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  };
}
