import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { theme } from "../src/theme/mantineTheme";

/** Renders a component inside the app's Mantine theme and, as the app has, a query cache of its own. */
export function renderWithTheme(ui: ReactElement) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MantineProvider theme={theme}>{ui}</MantineProvider>
    </QueryClientProvider>,
  );
}
