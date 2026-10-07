import { useEffect, useState } from "react";
import { useChangeSubscription } from "../../data/ActivityProvider";
import { nodeKey, type GraphModel } from "../../graph/GraphModel";
import { useTenantSession } from "../../session/SessionProvider";

/** How long a node stays marked after someone else changed it. */
const MARK_MS = 8_000;

/**
 * Canvas nodes that another person changed in the last few seconds, so the
 * canvas can mark them. The user's own changes are not marked.
 */
export function useRecentlyChanged(model: GraphModel): Set<string> {
  const subscribe = useChangeSubscription();
  const { me } = useTenantSession();
  const [recent, setRecent] = useState<Set<string>>(() => new Set());

  useEffect(
    () =>
      subscribe((change) => {
        if (change.kind !== "node" || !change.label || change.consumer === me.consumer) return;
        const keys = (change.ids ?? [])
          .map((id) => nodeKey({ id, label: change.label! }))
          .filter((key) => model.graph.hasNode(key));
        if (keys.length === 0) return;
        setRecent((current) => new Set([...current, ...keys]));
        setTimeout(() => {
          setRecent((current) => new Set([...current].filter((key) => !keys.includes(key))));
        }, MARK_MS);
      }),
    [subscribe, model, me.consumer],
  );
  return recent;
}
