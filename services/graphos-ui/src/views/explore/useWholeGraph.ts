import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";
import { keys } from "../../data/queries";
import { settle } from "../../graph/layout";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { useTenantSession } from "../../session/SessionProvider";

/** Up to this many nodes the whole graph goes on the canvas; above it Explore opens on the map. */
export const WHOLE_GRAPH_LIMIT = 5_000;

/**
 * What Explore shows on arrival, decided once on an empty canvas: the whole
 * tenant graph when it is small enough to read (laid out), else the map of
 * its labels, since a sample of a large graph shares almost no edges and
 * shows nothing. The map's counts decide (one cheap request); `load` asks
 * again after a Clear.
 */
export function useWholeGraph(): { load: () => Promise<void> } {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  const { model, overview, setOverview, select, setMode } = useWorkspace();
  const stopLayout = useRef<() => void>(() => undefined);

  const load = useCallback(async () => {
    setOverview({ status: "loading" });
    try {
      const map = await client.fetchQuery({
        queryKey: keys.map(tenant, false),
        queryFn: () => api.graphMap(),
        staleTime: 5 * 60_000,
      });
      const total = map.labels.reduce((sum, l) => sum + l.count, 0);
      if (total > WHOLE_GRAPH_LIMIT) {
        setOverview({ status: "map", total });
        setMode("map");
        return;
      }
      const exported = await api.exportGraph(WHOLE_GRAPH_LIMIT);
      stopLayout.current();
      select(null);
      const drawn = model.load(exported);
      stopLayout.current = settle(model);
      setOverview({ status: "shown", shown: drawn.nodes, total });
      setMode("graph");
    } catch {
      setOverview({ status: "failed" });
    }
  }, [api, client, tenant, model, select, setOverview, setMode]);

  useEffect(() => {
    if (overview.status === "idle" && model.isEmpty) void load();
  }, [overview.status, model, load]);

  useEffect(() => () => stopLayout.current(), []);
  return { load };
}
