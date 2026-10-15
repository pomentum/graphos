import { fireEvent, screen, within } from "@testing-library/react";
import { describeError } from "../src/api/http";
import type { EffectivePolicy, PropertyEntry } from "../src/api/types";
import {
  enumValues,
  rulesOf,
  undeclaredNames,
  withDefinition,
  withSettings,
  writtenByGraphos,
} from "../src/lib/propertySchema";
import { PropertyDialog } from "../src/views/schema/properties/PropertyDialog";
import { PropertyPanel } from "../src/views/schema/properties/PropertyPanel";
import { renderWithTheme } from "./render";

const PERSON: PropertyEntry = {
  description: "Someone who writes",
  properties: { name: { type: "string", required: true }, email: { type: "string", pattern: "[^@]+@[^@]+" } },
};

const POLICY: EffectivePolicy = {
  mode: "warn",
  undeclared: "allow",
  undeclared_source: "*",
  properties: {
    source: { source: "*", definition: { type: "string", required: true } },
    name: { source: "Person", definition: { type: "string", required: true } },
    email: { source: "Person", definition: { type: "string", pattern: "[^@]+@[^@]+" } },
  },
};

describe("property definitions in words and edits", () => {
  it("words each keyword a definition states", () => {
    expect(rulesOf({ type: "string", required: true, enum: ["lei", "euid", "other"] })).toEqual([
      "text",
      "required",
      "one of lei, euid, other",
    ]);
    expect(rulesOf({ type: "number", minimum: 0, maximum: 1 })).toEqual(["number", "0 to 1"]);
    expect(rulesOf({ format: "date", recommended: true })).toEqual(["recommended", "a date"]);
    expect(rulesOf({ type: "array", items: { type: "string", pattern: "C-\\d{7}" } })).toEqual([
      "list",
      "items: text, matches C-\\d{7}",
    ]);
    expect(rulesOf({ enum: [1, 2, 3, 4, 5, 6] })).toEqual(["one of 1, 2, 3, 4 and 2 more"]);
    expect(rulesOf({ enum: [1, 2, 3, 4, 5, 6] }, true)).toEqual(["one of 1, 2, 3, 4, 5, 6"]);
    expect(rulesOf({ type: "boolean", enum: [true] })).toEqual(["true/false", "always true"]);
    expect(rulesOf({})).toEqual([]);
  });

  it("sets and removes one definition, and an empty entry becomes null", () => {
    expect(withDefinition(PERSON, "born", { format: "date" })?.properties).toHaveProperty("born", { format: "date" });
    expect(withDefinition(PERSON, "email", null)?.properties).not.toHaveProperty("email");
    expect(withDefinition({ properties: { x: {} } }, "x", null)).toBeNull();
    expect(withDefinition({ description: "kept", properties: { x: {} } }, "x", null)).toEqual({
      description: "kept",
      properties: {},
    });
  });

  it("replaces an entry's settings, dropping empty ones", () => {
    expect(withSettings(PERSON, { description: "  ", mode: "strict" })).toEqual({
      mode: "strict",
      properties: PERSON.properties,
    });
    expect(withSettings(undefined, { description: "" })).toBeNull();
  });

  it("names what the rows carry undeclared, never graphos's own", () => {
    expect(undeclaredNames(["name", "nmae", "graphos_pagerank", "community_id", "id", "nmae"], POLICY)).toEqual([
      "nmae",
    ]);
    expect(writtenByGraphos("graphos_degree")).toBe(true);
    expect(writtenByGraphos("graphos")).toBe(false);
  });

  it("reads typed allowed values as the property's type", () => {
    expect(enumValues(["1", "2.5"], "number")).toEqual([1, 2.5]);
    expect(enumValues(["1.5"], "integer")).toBeNull();
    expect(enumValues(["true", "false"], "boolean")).toEqual([true, false]);
    expect(enumValues(["x"], "number")).toBeNull();
    expect(enumValues(["7"], "string")).toEqual(["7"]);
  });

  it("names a refusal's first violations", () => {
    const body = {
      detail: "3 property violations (property_schema, mode strict); nothing was written",
      violations: [
        { label: "Person", property: "name", message: "required" },
        { label: "Person", property: "email", message: "'x' does not match [^@]+@[^@]+" },
        { type: "WROTE", property: "role", message: "'editor' is not one of lead, co" },
        { label: "Doc", property: "source", message: "required" },
      ],
    };
    expect(describeError(422, body)).toBe(
      "3 property violations (property_schema, mode strict); nothing was written: Person.name: required; " +
        "Person.email: 'x' does not match [^@]+@[^@]+; WROTE.role: 'editor' is not one of lead, co; …",
    );
  });
});

// A large modal, slow to render while the whole suite runs in parallel.
describe("the property dialog", { timeout: 15_000 }, () => {
  const open = (props: Partial<Parameters<typeof PropertyDialog>[0]> = {}) => {
    const onSave = vi.fn();
    renderWithTheme(
      <PropertyDialog
        part="vertex"
        owner="Author"
        taken={["name"]}
        suggestions={["born", "rank"]}
        inheritedOf={() => undefined}
        saving={false}
        onCancel={() => undefined}
        onSave={onSave}
        {...props}
      />,
    );
    return onSave;
  };
  const save = () => fireEvent.click(screen.getByRole("button", { name: "Save" }));

  it("declares a new property: name, type, presence and bounds", async () => {
    const onSave = open();
    fireEvent.change(screen.getByRole("textbox", { name: "Property" }), { target: { value: "name" } });
    expect(screen.getByText("Already defined here")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Property" }), { target: { value: "rank" } });
    // Untyped, only allowed values are asked for; a numeric type adds the bounds.
    expect(screen.queryByRole("textbox", { name: "Minimum" })).toBeNull();
    fireEvent.click(screen.getByRole("textbox", { name: "Type" }));
    fireEvent.click(await screen.findByRole("option", { name: "Integer", hidden: true }));
    fireEvent.click(screen.getByRole("radio", { name: "Required" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Minimum" }), { target: { value: "1" } });
    save();
    expect(onSave).toHaveBeenCalledWith("rank", { type: "integer", required: true, minimum: 1 });
  });

  it("keeps an inherited type and adds rules to it", () => {
    const onSave = open({
      name: "name",
      inheritedOf: (name) =>
        name === "name" ? { source: "Person", definition: { type: "string", required: true } } : undefined,
    });
    expect(screen.getByText(/From Person: text, required/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Type" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Pattern" }), { target: { value: "[A-Z].*" } });
    save();
    expect(onSave).toHaveBeenCalledWith("name", { pattern: "[A-Z].*" });
  });

  it("keeps the type of a name that is inherited as it is typed", () => {
    const onSave = open({
      inheritedOf: (name) =>
        name === "country_code" ? { source: "LegalEntity", definition: { type: "string" } } : undefined,
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Property" }), { target: { value: "country_code" } });
    expect(screen.getByText(/From LegalEntity: text/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Type" })).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: "Required" }));
    save();
    expect(onSave).toHaveBeenCalledWith("country_code", { required: true });
  });

  it("refuses graphos's own names and asks only a pattern of the id", () => {
    const onSave = open();
    fireEvent.change(screen.getByRole("textbox", { name: "Property" }), { target: { value: "community_id" } });
    expect(screen.getByText("graphos writes this property itself")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Property" }), { target: { value: "id" } });
    expect(screen.queryByRole("textbox", { name: "Type" })).toBeNull();
    save();
    expect(onSave).toHaveBeenCalledWith("id", {});
  });
});

describe("the property panel", () => {
  const show = (editable: boolean) => {
    const onSave = vi.fn();
    renderWithTheme(
      <PropertyPanel
        part="vertex"
        owner="Person"
        entry={PERSON}
        policy={POLICY}
        inheritedOf={(name) => (name === "source" ? POLICY.properties.source : undefined)}
        carried={["name", "nmae", "graphos_degree"]}
        editable={editable}
        saving={false}
        onSave={onSave}
      />,
    );
    return onSave;
  };

  it("lists the definitions in effect with where they come from, and what is carried undeclared", () => {
    show(false);
    expect(screen.getByText("Someone who writes")).toBeInTheDocument();
    const row = screen.getByText("source").closest("tr")!;
    expect(within(row).getByText("from every label")).toBeInTheDocument();
    expect(screen.getByText("nmae")).toBeInTheDocument();
    expect(screen.queryByText("graphos_degree")).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove email" })).toBeNull();
  });

  it("removes a definition declared here, and offers narrowing an inherited one", () => {
    const onSave = show(true);
    expect(screen.queryByRole("button", { name: "Remove source" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove email" }));
    expect(onSave.mock.calls[0]![0]).toEqual({
      description: "Someone who writes",
      properties: { name: { type: "string", required: true } },
    });
  });
});
