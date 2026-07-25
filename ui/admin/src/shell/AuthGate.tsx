/**
 * Per-user Google Sign-In gate.
 *
 * The connector runs in TOOROW_AUTH_MODE=oauth: it verifies a Google ID token
 * (a JWT) against Google's JWKS. This gate populates `localStorage.api_token`
 * with a fresh Google ID token via Google Identity Services (GIS).
 *
 * The token only reaches the API through `src/lib/apiFetch.ts`, which attaches
 * `Authorization: Bearer <api_token>` to every call. A bare `fetch("/api/...")`
 * sends NO credential and the server answers 401 — this gate holding a valid
 * token is necessary but not sufficient (finding F-010).
 *
 * The ID token carries the verified `email` / `email_verified` claims the
 * connector uses for identity + invitation acceptance. It expires after ~1h;
 * when it lapses the gate re-prompts.
 */
import { useEffect, useRef, useState } from "react";

const CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID as string | undefined;

function tokenValid(): boolean {
  const t = localStorage.getItem("api_token");
  if (!t) return false;
  try {
    const payload = JSON.parse(atob(t.split(".")[1] ?? ""));
    // Keep a 60s safety margin so a call mid-flight does not expire.
    return typeof payload.exp === "number" && payload.exp * 1000 > Date.now() + 60_000;
  } catch {
    return false;
  }
}

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [authed, setAuthed] = useState<boolean>(tokenValid());
  const btnRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (authed) return;
    let cancelled = false;

    function init() {
      const g = (window as unknown as { google?: any }).google;
      if (!g?.accounts?.id || cancelled) return;
      g.accounts.id.initialize({
        client_id: CLIENT_ID,
        callback: (resp: { credential?: string }) => {
          if (resp.credential) {
            localStorage.setItem("api_token", resp.credential);
            setAuthed(true);
          }
        },
        auto_select: false,
      });
      if (btnRef.current) {
        g.accounts.id.renderButton(btnRef.current, {
          theme: "outline",
          size: "large",
          text: "signin_with",
          shape: "pill",
        });
      }
    }

    const existing = document.getElementById("gsi-script");
    if (existing) {
      init();
    } else {
      const s = document.createElement("script");
      s.src = "https://accounts.google.com/gsi/client";
      s.async = true;
      s.defer = true;
      s.id = "gsi-script";
      s.onload = init;
      document.head.appendChild(s);
    }
    return () => {
      cancelled = true;
    };
  }, [authed]);

  if (authed) return <>{children}</>;

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 28,
        padding: 24,
        fontFamily: "Geist, Inter, system-ui, sans-serif",
        background: "#0f1115",
        color: "#f5f5f5",
      }}
    >
      <img
        src="/brand/toorow-logo-horizontal-light.png"
        alt="toorow"
        style={{ width: "min(880px, 90vw)", height: "auto", marginBottom: 8 }}
      />
      <div style={{ opacity: 0.7, fontSize: 15 }}>Sign in to continue</div>
      <div ref={btnRef} />
      {!CLIENT_ID && (
        <div style={{ color: "#ff6b6b", fontSize: 12 }}>
          VITE_GOOGLE_CLIENT_ID is not set at build time.
        </div>
      )}
    </div>
  );
}
