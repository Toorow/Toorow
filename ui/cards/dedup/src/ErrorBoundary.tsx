/**
 * ErrorBoundary — évite un écran blanc en cas d'erreur dans la carte Déduplication.
 * Fallback en français (UX-DR10).
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
          data-testid="card-error-boundary"
          style={{ padding: "16px", color: "#d32f2f" }}
        >
          Une erreur est survenue lors de l&apos;affichage de la carte Déduplication.
        </div>
      );
    }
    return this.props.children;
  }
}
