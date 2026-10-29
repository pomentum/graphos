import { notifications } from "@mantine/notifications";
import { MutationCache, QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/http";
import { t } from "../i18n";

/** Client errors (4xx) are answers, not outages: they are not retried. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    // A write that fails always says so, whichever screen started it.
    mutationCache: new MutationCache({
      onSuccess: (_data, _variables, _context, mutation) => {
        // `meta.success` and `meta.errorTitle` are i18n keys (see data/queries).
        if (mutation.meta?.success) notifications.show({ message: t(mutation.meta.success), autoClose: 2000 });
      },
      onError: (error, _variables, _context, mutation) =>
        !mutation.meta?.silent &&
        notifications.show({
          color: "red",
          // A node, an edge or the schema changed since it was read (412), or a
          // program manages the schema's structure (409).
          title:
            error instanceof ApiError && error.isConflict
              ? t("errors.conflict")
              : error instanceof ApiError && error.isSchemaManaged
                ? t("errors.schemaManaged")
                : t(mutation.meta?.errorTitle ?? "errors.notSaved"),
          message: error.message,
        }),
    }),
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      },
    },
  });
}
