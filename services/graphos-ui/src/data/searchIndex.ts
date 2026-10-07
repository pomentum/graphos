import type { TenantSchema } from "../api/types";

/**
 * The search index the search box uses: an `identifier` index over every
 * label if there is one, else any `identifier` index, else none (graphos
 * then matches `name` by substring).
 */
export function pickSearchIndex(schema: TenantSchema | undefined): string | undefined {
  const entries = Object.entries(schema?.search_indexes ?? {});
  const identifier = entries.filter(([, spec]) => spec.analyzer === "identifier");
  const everyLabel = identifier.find(([, spec]) => spec.labels.includes("*"));
  return (everyLabel ?? identifier[0])?.[0];
}
