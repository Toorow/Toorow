/** Browser authentication gate: OIDC BFF, hosted Google GIS, or local static token. */
import { useEffect, useRef, useState } from "react";

const CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID as string | undefined;
const SESSION_IDENTITY_KEY = "toorow_browser_identity";

type BrowserMode =
  "oidc" | "google_gis" | "disabled" | "static" | "misconfigured";
type GateState =
  | "loading"
  | "authenticated"
  | "oidc"
  | "google_gis"
  | "static"
  | "misconfigured";

interface BrowserConfig {
  mode?: BrowserMode;
  provider_name?: string;
  reason?: string;
}

interface BrowserSession {
  authenticated?: boolean;
  display_name?: string | null;
  email?: string | null;
  picture?: string | null;
}

export function tokenValid(): boolean {
  const token = localStorage.getItem("api_token");
  if (!token) return false;
  try {
    const payload = JSON.parse(atob(token.split(".")[1] ?? ""));
    return (
      typeof payload.exp === "number" &&
      payload.exp * 1000 > Date.now() + 60_000
    );
  } catch {
    return false;
  }
}

function rememberBrowserIdentity(session: BrowserSession): void {
  sessionStorage.setItem(
    SESSION_IDENTITY_KEY,
    JSON.stringify({
      name: session.display_name ?? "Account",
      email: session.email ?? "",
      picture: session.picture ?? undefined,
    }),
  );
}

export default function BrowserAuthGate({
  children,
}: {
  children: React.ReactNode;
}) {
  const [state, setState] = useState<GateState>("loading");
  const [providerName, setProviderName] = useState("Identity provider");
  const [failure, setFailure] = useState<string | null>(null);
  const googleButtonRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (state !== "loading") return;
    let cancelled = false;

    void (async () => {
      try {
        const configResponse = await fetch("/api/auth/browser-config", {
          cache: "no-store",
          credentials: "same-origin",
        });
        if (!configResponse.ok)
          throw new Error("browser_auth_config_unavailable");
        const config = (await configResponse.json()) as BrowserConfig;
        if (cancelled) return;

        if (config.mode === "disabled") {
          setState("authenticated");
          return;
        }
        if (config.mode === "oidc") {
          localStorage.removeItem("api_token");
          setProviderName(config.provider_name || "Identity provider");
          const sessionResponse = await fetch("/api/auth/session", {
            cache: "no-store",
            credentials: "same-origin",
          });
          if (cancelled) return;
          if (sessionResponse.ok) {
            const session = (await sessionResponse.json()) as BrowserSession;
            if (session.authenticated) {
              rememberBrowserIdentity(session);
              setState("authenticated");
              return;
            }
          }
          setState("oidc");
          return;
        }
        if (config.mode === "google_gis") {
          setState(tokenValid() ? "authenticated" : "google_gis");
          return;
        }
        if (config.mode === "static") {
          setState(
            localStorage.getItem("api_token") ? "authenticated" : "static",
          );
          return;
        }
        setFailure(config.reason || "browser_auth_misconfigured");
        setState("misconfigured");
      } catch (error) {
        if (!cancelled) {
          setFailure(
            error instanceof Error ? error.message : "browser_auth_unavailable",
          );
          setState("misconfigured");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [state]);

  useEffect(() => {
    if (state !== "google_gis" || !CLIENT_ID) return;
    let cancelled = false;

    function initializeGoogle() {
      const google = (window as unknown as { google?: any }).google;
      if (!google?.accounts?.id || cancelled) return;
      google.accounts.id.initialize({
        client_id: CLIENT_ID,
        callback: (response: { credential?: string }) => {
          if (response.credential) {
            localStorage.setItem("api_token", response.credential);
            setState("authenticated");
          }
        },
        auto_select: false,
      });
      if (googleButtonRef.current) {
        google.accounts.id.renderButton(googleButtonRef.current, {
          theme: "outline",
          size: "large",
          text: "signin_with",
          shape: "pill",
        });
      }
    }

    const existing = document.getElementById("gsi-script");
    if (existing) {
      initializeGoogle();
    } else {
      const script = document.createElement("script");
      script.src = "https://accounts.google.com/gsi/client";
      script.async = true;
      script.defer = true;
      script.id = "gsi-script";
      script.onload = initializeGoogle;
      document.head.appendChild(script);
    }
    return () => {
      cancelled = true;
    };
  }, [state]);

  if (state === "authenticated") return <>{children}</>;

  const returnTo = `${window.location.pathname}${window.location.search}`;
  const oidcLogin = `/api/auth/oidc/login?return_to=${encodeURIComponent(returnTo)}`;

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
      <div style={{ opacity: 0.7, fontSize: 15 }}>
        {state === "loading" ? "Checking your session…" : "Sign in to continue"}
      </div>
      {state === "oidc" ? (
        <a href={oidcLogin} style={{ color: "#fff" }}>
          Sign in with {providerName}
        </a>
      ) : null}
      {state === "google_gis" ? <div ref={googleButtonRef} /> : null}
      {state === "google_gis" && !CLIENT_ID ? (
        <div style={{ color: "#ff6b6b", fontSize: 12 }}>
          VITE_GOOGLE_CLIENT_ID is not set at build time.
        </div>
      ) : null}
      {state === "static" ? (
        <div style={{ color: "#ffcc66", fontSize: 13 }}>
          This instance requires a static development token.
        </div>
      ) : null}
      {state === "misconfigured" ? (
        <div role="alert" style={{ color: "#ff6b6b", fontSize: 13 }}>
          Browser authentication is unavailable ({failure}). Contact the
          instance operator.
        </div>
      ) : null}
    </div>
  );
}
