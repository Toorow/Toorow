import { clearDeletedAccountSession } from "../shell/pages/AccountSettings";

beforeEach(() => {
  localStorage.setItem("api_token", "legacy-token");
  sessionStorage.setItem("toorow_browser_identity", '{"name":"Person"}');
});

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

test("clears both browser auth modes and logs out the HttpOnly session before reload", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 204 });
  const navigate = vi.fn();
  vi.stubGlobal("fetch", fetchMock);

  await clearDeletedAccountSession(navigate);

  expect(localStorage.getItem("api_token")).toBeNull();
  expect(sessionStorage.getItem("toorow_browser_identity")).toBeNull();
  expect(fetchMock).toHaveBeenCalledWith("/api/auth/logout", {
    method: "POST",
    credentials: "same-origin",
  });
  expect(navigate).toHaveBeenCalledOnce();
  expect(fetchMock.mock.invocationCallOrder[0]).toBeLessThan(
    navigate.mock.invocationCallOrder[0],
  );
});

test("still leaves the erased account surface when logout transport fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  const navigate = vi.fn();

  await expect(clearDeletedAccountSession(navigate)).resolves.toBeUndefined();
  expect(navigate).toHaveBeenCalledOnce();
});
