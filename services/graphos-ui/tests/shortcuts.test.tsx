import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import "../src/i18n";
import { ShortcutsDialog } from "../src/components/ShortcutsDialog";
import { renderWithTheme } from "./render";

describe("the keyboard shortcuts list", () => {
  it("lists each place's keys, with Ctrl off Apple devices", async () => {
    renderWithTheme(<ShortcutsDialog opened onClose={() => undefined} />);
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(dialog).getByText("Schema")).toBeInTheDocument();
    const undo = within(dialog).getByText("Undo the last change").parentElement!;
    expect(within(undo).getByText("Ctrl")).toBeInTheDocument();
    expect(within(undo).getByText("Z")).toBeInTheDocument();
    expect(within(dialog).getByText("Maximize or restore")).toBeInTheDocument();
    expect(within(dialog).queryByText("⌘")).toBeNull();
  });
});
