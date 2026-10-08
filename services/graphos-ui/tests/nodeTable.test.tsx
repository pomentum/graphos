import { fireEvent, screen } from "@testing-library/react";
import { NodeTable } from "../src/components/NodeTable";
import { renderWithTheme } from "./render";

const rows = [
  { id: "a", label: "Doc", properties: { name: "A" } },
  { id: "b", label: "Doc", properties: { name: "B" } },
];

describe("picking rows in a table", () => {
  it("picks one row without opening it, and every row of the page at once", () => {
    const onOpen = vi.fn();
    const onToggle = vi.fn();
    const onToggleAll = vi.fn();
    renderWithTheme(
      <NodeTable
        rows={rows}
        columns={["name"]}
        onOpen={onOpen}
        selected={new Set(["a"])}
        onToggle={onToggle}
        onToggleAll={onToggleAll}
      />,
    );
    fireEvent.click(screen.getByLabelText("Pick b"));
    expect(onToggle).toHaveBeenCalledWith(rows[1], true);
    expect(onOpen).not.toHaveBeenCalled();
    const all = screen.getByLabelText("Pick every row on this page") as HTMLInputElement;
    expect(all.indeterminate).toBe(true);
    fireEvent.click(all);
    expect(onToggleAll).toHaveBeenCalledWith(true);
    fireEvent.click(screen.getByText("B"));
    expect(onOpen).toHaveBeenCalledWith(rows[1]);
  });

  it("shows no checkboxes without a selection", () => {
    renderWithTheme(<NodeTable rows={rows} columns={["name"]} onOpen={() => undefined} />);
    expect(screen.queryByLabelText("Pick a")).toBeNull();
  });
});
