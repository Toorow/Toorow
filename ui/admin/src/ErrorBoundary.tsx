/**
 * ErrorBoundary -- admin console panel-level crash guard.
 *
 * Without this, a render exception in ANY panel whitescreens the whole console
 * (observed repeatedly during Epic 8: a single bad fetch shape took down the
 * entire admin). This boundary contains the failure to the content area and
 * shows a designed French fallback instead of a blank page.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";
import { Box, Button, Typography } from "@mui/material";

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
        <Box sx={{ p: 6, textAlign: "center", color: "text.secondary" }}>
          <Typography variant="h6" sx={{ mb: 1, color: "text.primary" }}>
            Something went wrong in this section.
          </Typography>
          <Typography variant="body2" sx={{ mb: 3 }}>
            {this.state.message || "Unexpected error."}
          </Typography>
          <Button
            variant="text"
            color="primary"
            onClick={() => this.setState({ hasError: false, message: "" })}
          >
            Retry
          </Button>
        </Box>
      );
    }
    return this.props.children;
  }
}
