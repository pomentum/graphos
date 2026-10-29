import { notifications } from "@mantine/notifications";
import { ApiError } from "../src/api/http";
import { createQueryClient } from "../src/data/queryClient";
import { t } from "../src/i18n";

describe("a schema save refused because the schema changed (412)", () => {
  it("says it was changed by someone else, with the server's explanation", async () => {
    const show = vi.spyOn(notifications, "show").mockImplementation(() => "");
    const client = createQueryClient();
    const refused = new ApiError(412, "If-Match failed: the schema of 'acme' changed since it was read", '"v2"');
    const save = client.getMutationCache().build(client, {
      meta: { success: "notices.schemaUpdated", errorTitle: "errors.schemaNotUpdated" },
      mutationFn: () => Promise.reject(refused),
    });
    await expect(save.execute(undefined)).rejects.toBe(refused);
    expect(show).toHaveBeenCalledWith(
      expect.objectContaining({ color: "red", title: t("errors.conflict"), message: refused.message }),
    );
    show.mockRestore();
  });
});

describe("a schema save refused because a program manages the schema (409)", () => {
  it("says it is managed by a program, with the server's explanation", async () => {
    const show = vi.spyOn(notifications, "show").mockImplementation(() => "");
    const client = createQueryClient();
    const refused = new ApiError(409, "The schema of 'acme' is managed by user:importer", null, "schema_managed");
    const save = client.getMutationCache().build(client, {
      meta: { success: "notices.schemaUpdated", errorTitle: "errors.schemaNotUpdated" },
      mutationFn: () => Promise.reject(refused),
    });
    await expect(save.execute(undefined)).rejects.toBe(refused);
    expect(show).toHaveBeenCalledWith(
      expect.objectContaining({ color: "red", title: t("errors.schemaManaged"), message: refused.message }),
    );
    show.mockRestore();
  });
});
