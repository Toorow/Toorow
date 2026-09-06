/**
 * One question, asked of the ONE router: does this address open anything?
 *
 * `buildPath` is how the console PRODUCES an address, and everything the console
 * composes itself must go through it. This module answers the other half: an
 * address the console did NOT build — a link the server sent, a fixture, a row
 * of a read model — before it is rendered as something a person can click.
 *
 * Why it has to be asked. `parsePath` refuses anything whose first segment is
 * not `org`, `account` or `platform` (`router.tsx:118-130`), and it refuses a
 * workspace or a section the registry does not declare. An `<a href>` carrying
 * such an address is a control that looks live, is reported by the screen as a
 * way forward, and lands on the unknown-route screen. That failure has now been
 * paid for four times — `/project/{p}/data/…` across five Data lenses
 * (data_surface.py, 2026-08-03), `/projects/{id}/…` in the wizard's owner links
 * (story 57.4), `/data/datastreams/o/{id}/overview` in its success link (story
 * 57.5), and `/p/{id}/…` in the Workbench tab band (story 57.5) — always for the
 * same reason: a second address grammar written beside the router's.
 *
 * A refused address is turned into an ABSENCE, never into a repaired guess.
 * Guessing which route was meant would open something adjacent to what the link
 * named, from inside a screen where nobody can see the substitution. The caller
 * then renders the owner's name without a link, which is the `ObjectNav` rule
 * for a tab with neither address nor callback: visible, inert, and honest about
 * what is missing.
 */
import { parsePath } from "./router";

/** The address if the router resolves it, `null` otherwise.
 *
 *  Only console addresses are judged — a value that is not an absolute path
 *  (`#anchor`, `?name=x`, `https://…`, `mailto:`) is not this router's business
 *  and is refused rather than waved through, so a caller cannot use this
 *  function to bless a fragment as a destination. `"#"` in particular was a
 *  Workbench fallback that rendered a live-looking link going nowhere. */
export function resolvableHref(href: string | null | undefined): string | null {
  if (typeof href !== "string") return null;
  const value = href.trim();
  if (!value.startsWith("/")) return null;
  const index = value.indexOf("?");
  const pathname = index === -1 ? value : value.slice(0, index);
  const search = index === -1 ? "" : value.slice(index);
  return parsePath(pathname, search).kind === "resolved" ? value : null;
}

/** Every address of a record, each one judged on its own.
 *
 *  A record of owner links is not all-or-nothing: one owner may be reachable
 *  while another is not, and dropping the whole record because of one would hide
 *  two working destinations. */
export function resolvableHrefs<T extends Record<string, string | null | undefined>>(
  links: T | null | undefined,
): { [K in keyof T]: string | null } {
  const resolved = {} as { [K in keyof T]: string | null };
  if (!links) return resolved;
  for (const key of Object.keys(links) as Array<keyof T>) {
    resolved[key] = resolvableHref(links[key]);
  }
  return resolved;
}
