/**
 * ErrorBoundary — React error boundary for the GA widget (review-global-gaps).
 *
 * Wraps the App rendering in main.tsx to prevent white-screen crashes.
 * Renders a French fallback message instead (UX-DR10).
 */
import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div
          role="alert"
          data-testid="widget-error-boundary"
          style={{ padding: "16px", color: "#d32f2f" }}
        >
          Une erreur est survenue lors de l&apos;affichage du rapport.
        </div>
      );
    }
    return this.props.children;
  }
}
