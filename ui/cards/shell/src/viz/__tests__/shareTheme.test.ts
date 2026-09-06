import { describe, expect, it } from "vitest";

import { DARK_SCHEME_MEDIA, followColorScheme } from "../entries/shareTheme";

function fakeMedia(matches: boolean) {
  const listeners = new Set<() => void>();
  const media = {
    matches,
    media: DARK_SCHEME_MEDIA,
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
    flip(to: boolean) {
      media.matches = to;
      listeners.forEach((fn) => fn());
    },
  };
  return media;
}

describe("the share page follows the reader's colour scheme (G14-T05)", () => {
  it("sets .dark on the root when the OS is dark, and nothing when it is light", () => {
    const dark = document.createElement("html");
    followColorScheme(dark, () => fakeMedia(true) as unknown as MediaQueryList);
    expect(dark.classList.contains("dark")).toBe(true);

    const light = document.createElement("html");
    followColorScheme(light, () => fakeMedia(false) as unknown as MediaQueryList);
    expect(light.classList.contains("dark")).toBe(false);
  });

  it("keeps following the OS when it flips, until unsubscribed", () => {
    const root = document.createElement("html");
    const media = fakeMedia(false);
    const stop = followColorScheme(root, () => media as unknown as MediaQueryList);
    expect(root.classList.contains("dark")).toBe(false);
    media.flip(true);
    expect(root.classList.contains("dark")).toBe(true);
    stop();
    media.flip(false);
    expect(root.classList.contains("dark")).toBe(true);
  });

  it("asks for the OS scheme and no other source -- a recipient has no stored preference", () => {
    const asked: string[] = [];
    followColorScheme(document.createElement("html"), (query) => {
      asked.push(query);
      return fakeMedia(false) as unknown as MediaQueryList;
    });
    expect(asked).toEqual([DARK_SCHEME_MEDIA]);
  });

  it("does nothing without a document or matchMedia", () => {
    expect(() => followColorScheme(null, undefined)()).not.toThrow();
  });
});
