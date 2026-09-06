/**
 * The share page follows the reader's colour scheme.
 *
 * The runtime decides light or dark from ONE thing: an ancestor carrying the
 * `.dark` class (`vizTheme.readCssVizTheme`), which is also what every Tailwind
 * `dark:` utility keys on (`styles/theme.css:7`). The console sets that class
 * from a stored preference or the OS (`shell/themeMode.ts`). The public share
 * page set it from nothing: a recipient whose OS is dark got the light rendering
 * on a dark page -- measured by G14-T05 on 2026-09-04, « le rendu sombre est
 * identique au clair, au pixel près » on the table family, where nothing but the
 * theme could differ.
 *
 * A recipient has no account and no stored preference, so the OS is the only
 * source, and it is kept in step when the OS flips. Same rule as the console's
 * `system` mode, without the stored half.
 */
export const DARK_SCHEME_MEDIA = "(prefers-color-scheme: dark)";

export function followColorScheme(
  root: HTMLElement | null = typeof document === "undefined" ? null : document.documentElement,
  match: ((query: string) => MediaQueryList) | undefined = typeof window === "undefined"
    ? undefined
    : window.matchMedia?.bind(window),
): () => void {
  if (!root || typeof match !== "function") return () => {};
  const media = match(DARK_SCHEME_MEDIA);
  const apply = () => root.classList.toggle("dark", media.matches);
  apply();
  if (typeof media.addEventListener === "function") {
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }
  return () => {};
}
