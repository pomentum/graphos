import { Button, Stack, Text } from "@mantine/core";
import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  error: Error | null;
}

/**
 * Keeps a failing view from blanking the whole app: shows what went wrong
 * and a way back, while the sidebar and toolbar keep working. `resetKey`
 * (the route) clears the error when the user moves to another view.
 */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey: string }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(previous: { resetKey: string }) {
    if (previous.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("view failed", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Stack align="center" justify="center" h="100%" gap="xs" p="xl">
        <Text fw={600}>This view stopped working</Text>
        <Text size="sm" c="dimmed" ta="center" maw={420}>
          {this.state.error.message}
        </Text>
        <Button size="xs" variant="default" mt="sm" onClick={() => this.setState({ error: null })}>
          Try again
        </Button>
      </Stack>
    );
  }
}
