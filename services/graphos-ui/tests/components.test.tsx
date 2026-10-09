import { fireEvent, screen } from "@testing-library/react";
import { ExpandDialog } from "../src/components/ExpandDialog";
import { NewNodeDialog } from "../src/components/NewNodeDialog";
import { PropertyEditor } from "../src/components/PropertyEditor";
import { renderWithTheme } from "./render";

describe("PropertyEditor", () => {
  it("edits values by type and leaves system keys read-only", () => {
    const onChange = vi.fn();
    renderWithTheme(<PropertyEditor draft={{ name: "Ada", age: 36, id: "u1" }} editable onChange={onChange} />);
    fireEvent.change(screen.getByLabelText("Value of name"), { target: { value: "Ada L" } });
    expect(onChange).toHaveBeenLastCalledWith({ name: "Ada L", age: 36, id: "u1" });
    expect(screen.queryByLabelText("Value of id")).toBeNull();
    fireEvent.click(screen.getByLabelText("Remove age"));
    expect(onChange).toHaveBeenLastCalledWith({ name: "Ada", id: "u1" });
  });

  it("leaves computed values (graphos_*, community_id) to the metrics block and refuses them as new names", () => {
    const onChange = vi.fn();
    renderWithTheme(
      <PropertyEditor
        draft={{ name: "Ada", graphos_pagerank: 0.0012, community_id: 3 }}
        editable
        onChange={onChange}
      />,
    );
    // Not listed here (GraphMetrics shows them), and nothing to edit or remove.
    expect(screen.queryByText("graphos_pagerank")).toBeNull();
    expect(screen.queryByText("community_id")).toBeNull();
    expect(screen.queryByLabelText("Remove graphos_pagerank")).toBeNull();
    // Editing another property keeps them in the draft, so a save never removes them.
    fireEvent.change(screen.getByLabelText("Value of name"), { target: { value: "Ada L." } });
    expect(onChange).toHaveBeenLastCalledWith({ name: "Ada L.", graphos_pagerank: 0.0012, community_id: 3 });
    fireEvent.click(screen.getByText("Add property"));
    fireEvent.change(screen.getByRole("textbox", { name: "New property name" }), { target: { value: "graphos_x" } });
    expect(screen.getByText("graphos keeps this name for itself")).toBeInTheDocument();
  });

  it("shows plain values without inputs to read-only tokens", () => {
    renderWithTheme(<PropertyEditor draft={{ name: "Ada" }} editable={false} onChange={() => undefined} />);
    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByText("Add property")).toBeNull();
  });
});

describe("PropertyEditor input", () => {
  it("keeps a half-typed number out of the draft", () => {
    const onChange = vi.fn();
    renderWithTheme(<PropertyEditor draft={{ lines: 10 }} editable onChange={onChange} />);
    const input = screen.getByLabelText("Value of lines");
    fireEvent.change(input, { target: { value: "-" } });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "-5" } });
    expect(onChange).toHaveBeenLastCalledWith({ lines: -5 });
  });

  it("adds a property from name and value, typed, and cancels with Escape", () => {
    const onChange = vi.fn();
    renderWithTheme(<PropertyEditor draft={{ name: "a" }} editable onChange={onChange} />);
    fireEvent.click(screen.getByText("Add property"));
    fireEvent.change(screen.getByRole("textbox", { name: "New property name" }), { target: { value: "size" } });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "New property name" }), { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled(); // Return in the name only moves on
    const value = screen.getByLabelText("New property value");
    fireEvent.change(value, { target: { value: "42" } });
    fireEvent.keyDown(value, { key: "Enter" });
    expect(onChange).toHaveBeenLastCalledWith({ name: "a", size: 42 });

    fireEvent.click(screen.getByText("Add property"));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "New property name" }), { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: "New property name" })).toBeNull();
  });
});

describe("ExpandDialog", () => {
  it("adds only the chosen relationships", () => {
    const onConfirm = vi.fn();
    renderWithTheme(
      <ExpandDialog
        opened
        nodeName="hub"
        counts={[
          { type: "CALLS", out: 30, in: 12 },
          { type: "OWNS", out: 1, in: 0 },
        ]}
        truncated={false}
        onCancel={() => undefined}
        onConfirm={onConfirm}
      />,
    );
    const add = screen.getByRole("button", { name: "Add" });
    expect(add).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/CALLS/));
    fireEvent.click(screen.getByRole("button", { name: "Add 42" }));
    expect(onConfirm).toHaveBeenCalledWith(["CALLS"]);
  });
});

describe("NewNodeDialog", () => {
  it("offers an existing node instead of overwriting it", () => {
    const onOpenExisting = vi.fn();
    const onCreate = vi.fn();
    renderWithTheme(
      <NewNodeDialog
        opened
        labels={["Person"]}
        saving={false}
        error={null}
        existing={{ id: "ada", label: "Person" }}
        onOpenExisting={onOpenExisting}
        onCancel={() => undefined}
        onCreate={onCreate}
      />,
    );
    expect(screen.getByText("This Person exists already")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Open it" }));
    expect(onOpenExisting).toHaveBeenCalledWith({ id: "ada", label: "Person" });
  });
});
