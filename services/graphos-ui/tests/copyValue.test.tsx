import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { PropertyEditor } from "../src/components/PropertyEditor";
import { renderWithTheme } from "./render";

describe("copying a property's value", () => {
  it("copies the value as shown, for a reader too", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderWithTheme(
      <PropertyEditor draft={{ name: "Ana Ruiz", age: 34 }} editable={false} onChange={() => undefined} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy name" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Ana Ruiz"));
    fireEvent.click(screen.getByRole("button", { name: "Copy age" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("34"));
  });
});
