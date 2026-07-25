/**
 * ErrorBoundary — prevents white-screen crashes in the keywords card. French fallback.
 */
import { Component, type ReactNode } from "react";
import { ERROR } from "../../../tokens/dist/theme";

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
          style={{ padding: "16px", color: ERROR }}
        >
          Une erreur est survenue lors de l&apos;affichage de la carte.
        </div>
      );
    }
    return this.props.children;
  }
}
