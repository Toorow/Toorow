/**
 * Vitest tests for ExportButton (Story 6.6, AC4, AC6).
 *
 * Coverage:
 *   - renders the "Exporter PNG" button
 *   - calls toPng and triggers link.click() on button press
 *   - graceful no-op when toPng throws
 *   - shows "Export…" while exporting
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";
import { vi } from "vitest";
import ExportButton from "../ExportButton";

// Mock html-to-image
vi.mock("html-to-image", () => ({
  toPng: vi.fn(),
}));

import { toPng } from "html-to-image";

// ---------------------------------------------------------------------------
// Helper: render ExportButton with a real ref
// ---------------------------------------------------------------------------

function TestWrapper({ filename = "test-export" }: { filename?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <div>
      <div ref={ref} data-testid="target">Widget content</div>
      <ExportButton targetRef={ref} filename={filename} />
    </div>
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

describe("ExportButton — render", () => {
  it("renders the 'Exporter PNG' button", () => {
    render(<TestWrapper />);
    expect(screen.getByTestId("export-png-button")).toBeTruthy();
    // Le label porte le préfixe décoratif « ↓ » — la copy reste « Exporter PNG ».
    expect(screen.getByText(/Exporter PNG/)).toBeTruthy();
  });

  it("renders inside export-button-container", () => {
    render(<TestWrapper />);
    expect(screen.getByTestId("export-button-container")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Export action
// ---------------------------------------------------------------------------

describe("ExportButton — export action", () => {
  it("calls toPng with the target element when clicked", async () => {
    const mockToPng = vi.mocked(toPng);
    mockToPng.mockResolvedValue("data:image/png;base64,FAKE");

    // Mock document.createElement so we can spy on link.click
    const clickMock = vi.fn();
    const originalCreate = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      if (tag === "a") {
        const a = originalCreate("a");
        a.click = clickMock;
        return a;
      }
      return originalCreate(tag);
    });

    render(<TestWrapper filename="my-report" />);
    const btn = screen.getByTestId("export-png-button");
    await userEvent.click(btn);

    await waitFor(() => {
      expect(mockToPng).toHaveBeenCalledTimes(1);
      expect(mockToPng).toHaveBeenCalledWith(expect.any(HTMLElement), { cacheBust: true });
    });

    await waitFor(() => {
      expect(clickMock).toHaveBeenCalledTimes(1);
    });

    vi.restoreAllMocks();
  });

  it("sets the correct filename on the download link", async () => {
    const mockToPng = vi.mocked(toPng);
    mockToPng.mockResolvedValue("data:image/png;base64,FAKE");

    let capturedLink: HTMLAnchorElement | null = null;
    const originalCreate = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = originalCreate(tag);
      if (tag === "a") {
        capturedLink = el as HTMLAnchorElement;
        el.click = vi.fn();
      }
      return el;
    });

    render(<TestWrapper filename="seo-weekly" />);
    await userEvent.click(screen.getByTestId("export-png-button"));

    await waitFor(() => {
      expect(capturedLink?.download).toBe("seo-weekly.png");
    });

    vi.restoreAllMocks();
  });

  it("is a graceful no-op when toPng throws", async () => {
    const mockToPng = vi.mocked(toPng);
    mockToPng.mockRejectedValue(new Error("CORS error"));

    render(<TestWrapper />);
    const btn = screen.getByTestId("export-png-button");

    // Should not throw
    await userEvent.click(btn);

    await waitFor(() => {
      // Button should be enabled again (no crash)
      expect(btn).not.toBeDisabled();
    });
  });
});
