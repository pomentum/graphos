import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ChangeStream, type StreamStatus } from "../api/changeStream";
import type { ChangeItem } from "../api/types";
import { useTenantSession } from "../session/SessionProvider";
import { keys } from "./queries";

/** How many recent changes are kept in memory. */
const KEEP = 200;

interface ActivityValue {
  /** Newest first. */
  changes: ChangeItem[];
  status: StreamStatus;
  /** Changes since `markSeen` was last called. */
  unseen: number;
  markSeen: () => void;
  /** Calls `listener` for each change as it arrives; returns the unsubscribe function. */
  subscribe: (listener: (change: ChangeItem) => void) => () => void;
}

const ActivityContext = createContext<ActivityValue | null>(null);
/** Only `subscribe`: it never changes, so its users do not re-render on every change. */
const SubscribeContext = createContext<ActivityValue["subscribe"] | null>(null);

/**
 * Follows the tenant's change feed once for the whole app: keeps recent
 * changes for the Activity view and the bell, lets the canvas react to them,
 * and refreshes cached nodes that someone else changed.
 */
export function ActivityProvider({ children }: { children: ReactNode }) {
  const { api, tenant, me } = useTenantSession();
  const client = useQueryClient();
  const [changes, setChanges] = useState<ChangeItem[]>([]);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [unseen, setUnseen] = useState(0);
  const listeners = useRef(new Set<(change: ChangeItem) => void>());

  useEffect(() => {
    setChanges([]);
    setUnseen(0);
    // One pass over the cache per change, however many nodes it names.
    const refreshCached = (change: ChangeItem) => {
      if (change.kind !== "node" || !change.label || !change.ids?.length) return;
      const ids = new Set(change.ids);
      const [kind, , label] = keys.node(tenant, change.label, "");
      void client.invalidateQueries({
        predicate: ({ queryKey }) =>
          queryKey[0] === kind && queryKey[1] === tenant && queryKey[2] === label && ids.has(String(queryKey[3])),
      });
    };
    const stream = new ChangeStream(api, {
      onStatus: setStatus,
      onChange: (change) => {
        setChanges((current) => [change, ...current].slice(0, KEEP));
        if (change.consumer !== me.consumer) setUnseen((n) => n + 1);
        refreshCached(change);
        for (const listener of listeners.current) listener(change);
      },
    });
    stream.start();
    return () => stream.stop();
  }, [api, tenant, me.consumer, client]);

  const markSeen = useCallback(() => setUnseen(0), []);
  const subscribe = useCallback((listener: (change: ChangeItem) => void) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const value = useMemo(
    () => ({ changes, status, unseen, markSeen, subscribe }),
    [changes, status, unseen, markSeen, subscribe],
  );
  return (
    <SubscribeContext.Provider value={subscribe}>
      <ActivityContext.Provider value={value}>{children}</ActivityContext.Provider>
    </SubscribeContext.Provider>
  );
}

/**
 * Subscribes to changes as they arrive, without re-rendering for each one
 * (unlike `useActivity`, whose list changes with every change).
 */
export function useChangeSubscription(): ActivityValue["subscribe"] {
  const subscribe = useContext(SubscribeContext);
  if (!subscribe) throw new Error("useChangeSubscription must be used inside ActivityProvider");
  return subscribe;
}

/** The live activity of the selected tenant; throws outside `ActivityProvider`. */
export function useActivity(): ActivityValue {
  const value = useContext(ActivityContext);
  if (!value) throw new Error("useActivity must be used inside ActivityProvider");
  return value;
}
