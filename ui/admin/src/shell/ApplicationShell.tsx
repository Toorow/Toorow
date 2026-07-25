/**
 * ApplicationShell — faithful mockup frame (mockups/application.css): sticky
 * sidebar + app column (topbar + main). Existing panels render inside .main
 * unchanged until each is recadré from its own mockup.
 */
import "./application.css";
import StableSidebar from "./StableSidebar";
import TopBar from "./TopBar";
import ContentRouter from "./ContentRouter";
import ErrorBoundary from "../ErrorBoundary";
import { useRoute } from "./router";

export default function ApplicationShell() {
  const { route } = useRoute();
  return (
    <div className="frame">
      <StableSidebar />
      <section className="app">
        <TopBar />
        <main className="main">
          <ErrorBoundary resetKey={`${route.workspace}/${route.section ?? ""}`}>
            <ContentRouter />
          </ErrorBoundary>
        </main>
      </section>
    </div>
  );
}
