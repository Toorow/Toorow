/**
 * `ui/CopyButton` — a clipboard write that reports its outcome.
 *
 * WHAT IS ASSERTED is the case six of the console's seven copy flows got wrong:
 * a browser that refuses. Those flows called `writeText` and declared success
 * regardless, so on a denied permission or an insecure origin the screen said
 * "copied" and the person pasted whatever was there before.
 *
 * Both failure shapes are covered, because they are not the same failure:
 * `writeText` REJECTING (permission denied) and `navigator.clipboard` being
 * ABSENT (insecure origin), which throws a TypeError rather than rejecting.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CopyButton } from "../ui/CopyButton";

/** Installs a clipboard by direct assignment — see `test-setup.ts` on why not
 *  `vi.stubGlobal`: a sibling file's `unstubAllGlobals` would restore jsdom's
 *  `undefined` and delete the shim mid-suite. */
function withClipboard(writeText: ((text: string) => Promise<void>) | null) {
  const navigatorRef = globalThis.navigator as unknown as Record<string, unknown>;
  const original = navigatorRef.clipboard;
  Object.defineProperty(navigatorRef, "clipboard", {
    value: writeText ? { writeText } : undefined,
    configurable: true,
    writable: true,
  });
  return () => {
    Object.defineProperty(navigatorRef, "clipboard", {
      value: original,
      configurable: true,
      writable: true,
    });
  };
}

let restore: (() => void) | null = null;

afterEach(() => {
  restore?.();
  restore = null;
  vi.restoreAllMocks();
});

describe("CopyButton", () => {
  it("writes the exact value and announces the success", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    restore = withClipboard(writeText);

    render(<CopyButton value="https://example.com/share#abc" label="Copy link" />);
    fireEvent.click(screen.getByRole("button", { name: /Copy link/i }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("https://example.com/share#abc"));
    // The announcement is on the button AND in a live region, so it reaches a
    // reader who is not looking at the button.
    expect(await screen.findByRole("button", { name: /Copied/i })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Copied");
  });

  it("says the copy was refused, visibly, when the browser denies it", async () => {
    restore = withClipboard(() => Promise.reject(new Error("NotAllowedError")));

    render(<CopyButton value="rnd_EXAMPLE" label="Copy link" />);
    fireEvent.click(screen.getByRole("button", { name: /Copy link/i }));

    // Visible, and it names the gesture that repairs rather than the cause.
    const message = await screen.findByText(/did not allow copying/i);
    expect(message).toBeVisible();
    expect(message).toHaveTextContent(/Select the text and copy it by hand/i);
    // And it must NOT have claimed success.
    expect(screen.queryByRole("button", { name: /Copied/i })).toBeNull();
  });

  it("treats a missing clipboard as a refusal rather than throwing", async () => {
    // An insecure origin: `navigator.clipboard` is undefined, so reading
    // `writeText` off it throws instead of rejecting.
    restore = withClipboard(null);

    render(<CopyButton value="rnd_EXAMPLE" label="Copy link" />);
    fireEvent.click(screen.getByRole("button", { name: /Copy link/i }));

    expect(await screen.findByText(/did not allow copying/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Copied/i })).toBeNull();
  });

  /**
   * REAL timers, deliberately. `waitFor` under `vi.useFakeTimers()` never
   * settles here — it hangs to the 20s test timeout — and the component's own
   * 2.5s window is inside the suite's 5s async floor, so the honest wait is
   * cheaper than the machinery to fake it.
   */
  it("withdraws the success announcement so a second copy reads as a second copy", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    restore = withClipboard(writeText);

    render(<CopyButton value="v" label="Copy link" />);
    fireEvent.click(screen.getByRole("button", { name: /Copy link/i }));
    expect(await screen.findByRole("button", { name: /Copied/i })).toBeInTheDocument();

    // "Copied" is a claim about a clipboard the page has stopped watching.
    expect(await screen.findByRole("button", { name: /Copy link/i })).toBeInTheDocument();
  });

  it("never withdraws the refusal — it is an instruction to act", async () => {
    restore = withClipboard(() => Promise.reject(new Error("NotAllowedError")));

    render(<CopyButton value="v" label="Copy link" />);
    fireEvent.click(screen.getByRole("button", { name: /Copy link/i }));
    await screen.findByText(/did not allow copying/i);

    // Well past the window a success would have been withdrawn in.
    await new Promise((resolve) => setTimeout(resolve, 3_000));
    expect(screen.getByText(/did not allow copying/i)).toBeVisible();
  });
});
