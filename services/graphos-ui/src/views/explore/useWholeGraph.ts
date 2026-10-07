import { useCallback, useEffect, useRef } from "react";
import { settle } from "../../graph/layout";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { useTenantSession } from "../../session/SessionProvider";

/** Above this many nodes the canvas shows a sample of this size instead. */
export const WHOLE_GRAPH_LIMIT = 5_000;

/**
 * The whole tenant graph on the canvas: loaded once when Explore first opens
 * on an empty canvas (all of it up to `WHOLE_GRAPH_LIMIT` nodes, a sample of
 * that size beyond), then laid out. `load` brings it back after a Clear.
 */
export function useWholeGraph(): { load: () => Promise<void> } {
  const { api } = useTenantSession();
  const { model, overview, setOverview, select } = useWorkspace();
  const stopLayout = useRef<() => void>(() => undefined);

  const load = useCallback(async () => {
    setOverview({ status: "loading" });
    try {
      const [stats, exported] = await Promise.all([api.stats(), api.exportGraph(WHOLE_GRAPH_LIMIT)]);
      stopLayout.current();
      select(null);
      const drawn = model.load(exported);
      stopLayout.current = settle(model);
      setOverview({ status: "shown", shown: drawn.nodes, total: stats.n_vertices });
    } catch {
      setOverview({ status: "failed" });
    }
  }, [api, model, select, setOverview]);

  useEffect(() => {
    if (overview.status === "idle" && model.isEmpty) void load();
  }, [overview.status, model, load]);

  useEffect(() => () => stopLayout.current(), []);
  return { load };
}
