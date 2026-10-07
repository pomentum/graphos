import { fireEvent, screen, within } from "@testing-library/react";
import { TenantSwitcher } from "../src/components/ToolbarMenus";
import { renderWithTheme } from "./render";

const tenants = Array.from({ length: 40 }, (_, i) => `tenant_${String(i).padStart(2, "0")}`);
const selectTenant = vi.fn();

vi.mock("../src/session/SessionProvider", () => ({
  useSessionContext: () => ({ session: { tenant: "tenant_05", me: { tenants } }, selectTenant }),
}));

describe("TenantSwitcher", () => {
  it("lists many tenants in a scrolling, searchable list", () => {
    renderWithTheme(<TenantSwitcher />);
    fireEvent.click(screen.getByRole("button", { name: "Switch tenant" }));

    const listbox = screen.getByRole("listbox");
    expect(within(listbox).getAllByRole("option")).toHaveLength(40);
    // The list is capped to the window and scrolls, however many tenants there are.
    const capped = listbox.querySelector<HTMLElement>("[style*='max-height']");
    expect(capped?.style.maxHeight).toContain("100vh");

    fireEvent.change(screen.getByLabelText("Find a tenant"), { target: { value: "_3" } });
    const found = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(found.map((o) => o.textContent)).toEqual(Array.from({ length: 10 }, (_, i) => `tenant_3${i}`));

    fireEvent.click(found[2]!);
    expect(selectTenant).toHaveBeenCalledWith("tenant_32");
  });
});
