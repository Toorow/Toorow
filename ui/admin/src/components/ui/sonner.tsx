/**
 * Toaster — the shadcn component, adapted.
 *
 * Two changes from the registry source. `next-themes` is gone: this console is
 * not a Next.js app and has one theme, so the hook added a dependency we do not
 * carry and a hydration concern we do not have. And the CSS variables now name
 * the mapped surfaces from `@theme`, so a toast matches the panels it sits over
 * instead of falling back to shadcn's unshipped palette.
 *
 * `notify` is the console's entry point. It takes the same five tones as the
 * inline dot and the banner in `Status`, so a message cannot pick its own
 * colours at the call site — that is how six ways of saying green/amber/red
 * ended up in the stylesheets.
 *
 * A toast is for something that happened and needs no answer. Anything the
 * person must act on belongs in a `Status as="block"`, which stays on screen.
 */
import {
  CircleCheckIcon,
  InfoIcon,
  Loader2Icon,
  OctagonXIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Toaster as Sonner, toast, type ToasterProps } from "sonner";
import type { CSSProperties, ReactNode } from "react";
// One tone scale for the whole console — the same one `Status` and `Badge`
// read. A toast declaring its own would be the sixth way of saying red.
import type { Tone } from "@/ui/tone";

const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      className="toaster group"
      position="bottom-right"
      expand
      visibleToasts={4}
      icons={{
        success: <CircleCheckIcon className="size-4" />,
        info: <InfoIcon className="size-4" />,
        warning: <TriangleAlertIcon className="size-4" />,
        error: <OctagonXIcon className="size-4" />,
        loading: <Loader2Icon className="size-4 animate-spin" />,
      }}
      style={
        {
          "--normal-bg": "var(--color-popover)",
          "--normal-text": "var(--color-popover-foreground)",
          "--normal-border": "var(--color-border)",
          "--border-radius": "var(--radius-large)",
        } as CSSProperties
      }
      {...props}
    />
  );
};

export type NotifyOptions = {
  tone?: Tone;
  description?: ReactNode;
  /** A single follow-up. Two actions in a toast means it should be a dialog. */
  action?: { label: string; onClick: () => void };
};

export function notify(
  message: string,
  { tone = "neutral", description, action }: NotifyOptions = {},
) {
  const options = {
    description,
    action: action ? { label: action.label, onClick: action.onClick } : undefined,
    // An error stays until it is dismissed: a message announcing a failure must
    // not disappear before it has been read.
    duration: tone === "error" ? Infinity : 5000,
  };
  switch (tone) {
    case "success":
      return toast.success(message, options);
    case "warning":
      return toast.warning(message, options);
    case "error":
      return toast.error(message, options);
    case "info":
      return toast.info(message, options);
    default:
      return toast(message, options);
  }
}

export { Toaster };
