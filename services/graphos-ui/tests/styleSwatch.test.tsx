import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { StyleSwatch } from "../src/views/schema/StyleSwatch";
import { renderWithTheme } from "./render";

// What a sample of the type's edges carries; asked for only once the editor opens.
const sampledTypes: (string | null)[] = [];
vi.mock("../src/data/queries", () => ({
  useEdgeProperties: (type: string | null) => {
    sampledTypes.push(type);
    return { data: type ? ["role", "weight"] : undefined };
  },
}));

const edit = async (kind: "vertex" | "edge", own: { caption: string } | null = null) => {
  renderWithTheme(
    <StyleSwatch
      kind={kind}
      name={kind === "edge" ? "AUTHORED" : "Person"}
      own={own}
      shown={own}
      properties={["name", "email"]}
      editable
      saving={false}
      onSave={() => undefined}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: kind === "edge" ? "Style of AUTHORED" : "Style of Person" }));
  const caption = await screen.findByRole("textbox", { name: "Caption" });
  fireEvent.click(caption);
  // The caption's own list, not the size or icon ones.
  await waitFor(() => expect(caption).toHaveAttribute("aria-controls"));
  const list = document.getElementById(caption.getAttribute("aria-controls")!)!;
  return within(list)
    .getAllByRole("option", { hidden: true })
    .map((option) => option.textContent);
};

describe("a style's caption", () => {
  beforeEach(() => {
    sampledTypes.length = 0;
  });

  it("is picked from a relationship type's own edges, sampled when the editor opens", async () => {
    expect(await edit("edge")).toEqual(["role", "weight"]);
    expect(sampledTypes[0]).toBeNull();
    expect(sampledTypes).toContain("AUTHORED");
  });

  it("keeps the declared caption choosable when no sampled edge carries it", async () => {
    expect(await edit("edge", { caption: "since" })).toEqual(["since", "role", "weight"]);
  });

  it("is picked from a label's properties", async () => {
    expect(await edit("vertex")).toEqual(["name", "email"]);
    expect(sampledTypes.every((type) => type === null)).toBe(true);
  });
});
