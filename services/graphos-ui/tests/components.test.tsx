import { fireEvent, screen } from "@testing-library/react";
import { ExpandDialog } from "../src/components/ExpandDialog";
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

  it("shows plain values without inputs to read-only tokens", () => {
    renderWithTheme(<PropertyEditor draft={{ name: "Ada" }} editable={false} onChange={() => undefined} />);
    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByText("Add property")).toBeNull();
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
