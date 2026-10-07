import { MantineProvider } from "@mantine/core";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { theme } from "../src/theme/mantineTheme";

/** Renders a component inside the app's Mantine theme. */
export function renderWithTheme(ui: ReactElement) {
  return render(<MantineProvider theme={theme}>{ui}</MantineProvider>);
}
