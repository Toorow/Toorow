/**
 * ErrorBoundary -- admin console panel-level crash guard.
 *
 * Without this, a render exception in ANY panel whitescreens the whole console
 * (observed repeatedly during Epic 8: a single bad fetch shape took down the
 * entire admin). This boundary contains the failure to the content area and
 * shows a designed French fallback instead of a blank page.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button, EmptyState } from "./ui";

interface Props {
  children: ReactNode;
  /** Remount the boundary when this key changes (e.g. active section) to clear a stale error. */
  resetKey?: string;
}

interface State {
  hasError: boolean;
  message: string;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: "" };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, message: error.message };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Admin panel error:", error, info);
  }

  componentDidUpdate(prev: Props) {
    if (prev.resetKey !== this.props.resetKey && this.state.hasError) {
      this.setState({ hasError: false, message: "" });
    }
  }

  render() {
    if (this.state.hasError) {
      return (
        // `EmptyState` already IS this shape — centred, a title at h3, a muted
        // description, an action underneath. The MUI version rebuilt it out of a
        // padded Box and two Typography variants, which is the reinvention this
        // library exists to stop.
        <EmptyState
          title="Something went wrong in this section."
          description={this.state.message || "Unexpected error."}
          action={
            <Button
              variant="ghost"
              onClick={() => this.setState({ hasError: false, message: "" })}
            >
              Retry
            </Button>
          }
        />
      );
    }
    return this.props.children;
  }
}
