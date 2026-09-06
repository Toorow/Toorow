/**
 * CopyButton — put a string on the clipboard, and say what happened.
 *
 * WHY IT EXISTS. Seven flows in the console wrote to the clipboard and one of
 * them handled a refusal. The other six called `navigator.clipboard.writeText`
 * and immediately declared success — so on a browser that denies the
 * permission, on an insecure origin, or in any context where `clipboard` is
 * simply absent, the screen said "copied" and the person pasted whatever was
 * there before. A share link, a run id, an address: silently the wrong one.
 *
 * REFUSAL IS A STATE OF THIS BUTTON, NOT AN EXCEPTION IT SWALLOWS. The
 * pattern is `analyze/ResultWorkbench.tsx:1425-1430`, the one place that got
 * it right: the value stays reachable and the screen says to take it by hand.
 * Here that means a visible message naming the gesture that repairs — select
 * the text and copy it — never the technical cause.
 *
 * SUCCESS IS ANNOUNCED BRIEFLY AND THEN WITHDRAWN. A permanent "Copied" is a
 * claim about a clipboard the page stopped being able to see; after
 * `ANNOUNCE_MS` the button returns to its label so a second copy is legible as
 * a second copy. A REFUSAL DOES NOT EXPIRE: it is an instruction to act, and
 * an instruction that vanishes while it is being followed is worse than none.
 *
 * BOTH OUTCOMES REACH A SCREEN READER through one polite live region. It is
 * mounted from the start rather than swapped in with the message, because a
 * region that appears together with its text is frequently not announced at
 * all.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { CopyIcon, CheckIcon } from "lucide-react";

import { Button } from "../components/ui/button";
import { cn } from "../lib/cn";

/** How long "Copied" stands before the button returns to its label. */
const ANNOUNCE_MS = 2_500;

type CopyState = "idle" | "copied" | "refused";

/** Named once: the refusal message is the same instruction on every screen. */
const REFUSED_MESSAGE = "Your browser did not allow copying. Select the text and copy it by hand.";

export interface CopyButtonProps {
  /** The exact string to place on the clipboard. */
  value: string;
  /** The resting label. Say WHAT is copied — "Copy link", not "Copy". */
  label?: string;
  /** What the button says while the copy is being announced. */
  copiedLabel?: string;
  variant?: "default" | "secondary" | "ghost" | "destructive" | "link";
  size?: "default" | "sm" | "xs" | "lg";
  disabled?: boolean;
  className?: string;
  /**
   * Test hooks, and the only props here that are not design decisions — the
   * same exemption `Status` and `ConfirmDialog` document beside their own.
   */
  "data-testid"?: string;
  messageTestId?: string;
}

export function CopyButton({
  value,
  label = "Copy",
  copiedLabel = "Copied",
  variant = "secondary",
  size,
  disabled = false,
  className,
  "data-testid": testId,
  messageTestId,
}: CopyButtonProps) {
  const [state, setState] = useState<CopyState>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // A copy landing after the screen has moved on must not set state on an
  // unmounted button, and a pending withdrawal must not outlive the button
  // that scheduled it.
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const copy = useCallback(async () => {
    if (timer.current) clearTimeout(timer.current);
    try {
      // `navigator.clipboard` is undefined on an insecure origin — reading
      // `writeText` off it would throw a TypeError rather than reject, so both
      // failures are caught by the same `try`, deliberately.
      await navigator.clipboard.writeText(value);
      setState("copied");
      timer.current = setTimeout(() => setState("idle"), ANNOUNCE_MS);
    } catch {
      // No permission, no clipboard, or the write was denied. The value is
      // still on screen beside this button; the message says to take it there.
      setState("refused");
    }
  }, [value]);

  const copied = state === "copied";

  return (
    <div className={cn("inline-flex flex-col items-start", className)}>
      <Button
        type="button"
        variant={variant}
        size={size}
        disabled={disabled}
        onClick={() => void copy()}
        data-testid={testId}
      >
        {copied ? <CheckIcon aria-hidden /> : <CopyIcon aria-hidden />}
        {copied ? copiedLabel : label}
      </Button>
      {/* Mounted always, filled on the transition — see the header. The spacing
          lives on the message rather than on a `gap`, so an empty region does
          not push the button off its row's rhythm. */}
      <span role="status" aria-live="polite">
        {state === "refused" ? (
          <span className="mt-1 block max-w-[42ch] text-caption text-danger" data-testid={messageTestId}>
            {REFUSED_MESSAGE}
          </span>
        ) : null}
        {copied ? <span className="sr-only">{copiedLabel}</span> : null}
      </span>
    </div>
  );
}
