import { fireEvent, screen } from "@testing-library/react";
import type { TenantSchema } from "../src/api/types";
import { AddToSchemaDialog } from "../src/views/schema/AddToSchemaDialog";
import { renderWithTheme } from "./render";

const schema: TenantSchema = {
  name: "t",
  vertex_labels: ["Symbol", "Function"],
  edge_labels: ["CALLS"],
  label_hierarchy: {},
  search_indexes: { by_name: { labels: ["*"], properties: ["name"], analyzer: "identifier", language: "simple" } },
  rule_edge_labels: [],
};

const open = (kind: "label" | "relationship" | "index") => {
  const onAdd = vi.fn();
  renderWithTheme(
    <AddToSchemaDialog
      kind={kind}
      schema={schema}
      properties={["name", "path"]}
      saving={false}
      onCancel={() => undefined}
      onAdd={onAdd}
    />,
  );
  return onAdd;
};

describe("adding to the schema", () => {
  it("checks a label name and refuses one that exists", () => {
    const onAdd = open("label");
    const name = screen.getByRole("textbox", { name: "Name" });
    fireEvent.change(name, { target: { value: "invoice" } });
    expect(screen.getByText("Not a valid name")).toBeInTheDocument();
    fireEvent.change(name, { target: { value: "Function" } });
    expect(screen.getByText("Already in the schema")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    fireEvent.change(name, { target: { value: "Invoice" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onAdd).toHaveBeenCalledWith({ vertex_labels: ["Invoice"] });
  });

  it("adds a relationship type", () => {
    const onAdd = open("relationship");
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "DEPENDS_ON" } });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Name" }), { key: "Enter" });
    expect(onAdd).toHaveBeenCalledWith({ edge_labels: ["DEPENDS_ON"] });
  });

  it("declares a drawn label pair, registering the type only when it is new", () => {
    const onAdd = vi.fn();
    renderWithTheme(
      <AddToSchemaDialog
        kind="relationship"
        schema={schema}
        properties={[]}
        saving={false}
        pair={{ src: "Function", dst: "Symbol" }}
        onCancel={() => undefined}
        onAdd={onAdd}
      />,
    );
    const name = screen.getByRole("textbox", { name: "Name" });
    fireEvent.change(name, { target: { value: "CALLS" } });
    expect(screen.queryByText("Already in the schema")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Declare" }));
    expect(onAdd).toHaveBeenLastCalledWith({ edge_endpoints: { CALLS: [["Function", "Symbol"]] } });
    fireEvent.change(name, { target: { value: "USES" } });
    fireEvent.click(screen.getByRole("button", { name: "Declare" }));
    expect(onAdd).toHaveBeenLastCalledWith({
      edge_labels: ["USES"],
      edge_endpoints: { USES: [["Function", "Symbol"]] },
    });
  });

  it("adds a search index over every label by default", () => {
    const onAdd = open("index");
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "by_name" } });
    expect(screen.getByText("Already in the schema")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "by_path" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onAdd).toHaveBeenCalledWith({
      search_indexes: { by_path: { labels: ["*"], properties: ["name"], analyzer: "identifier", language: "simple" } },
    });
  });
});
