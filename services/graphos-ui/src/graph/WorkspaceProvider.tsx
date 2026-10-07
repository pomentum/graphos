import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import type { Properties } from "../api/types";
import { useTenantSession } from "../session/SessionProvider";
import { GraphModel, nodeKey, type NodeRef } from "./GraphModel";

interface WorkspaceValue {
  /** The Explore canvas; kept while the user moves between views. */
  model: GraphModel;
  /** Changes whenever the model does, so components re-render. */
  version: number;
  selected: string | null;
  select: (key: string | null) => void;
  /** Puts a node on the canvas (if it isn't there) and selects it. */
  open: (ref: NodeRef, properties: Properties) => string;
}

const WorkspaceContext = createContext<WorkspaceValue | null>(null);

/**
 * Holds what the user is working on in Explore: the nodes on the canvas and
 * the selection. Search, Activity and Path all hand nodes to it. A new tenant
 * starts with an empty canvas.
 */
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { tenant } = useTenantSession();
  const [model] = useState(() => new GraphModel());
  const [selected, setSelected] = useState<string | null>(null);
  const version = useSyncExternalStore(
    useCallback((onChange: () => void) => model.subscribe(onChange), [model]),
    () => model.revision,
  );

  useEffect(() => {
    model.clear();
    setSelected(null);
  }, [model, tenant]);

  // A selected node that was deleted is no longer selected.
  useEffect(() => {
    if (selected && !model.graph.hasNode(selected)) setSelected(null);
  }, [model, selected, version]);

  const open = useCallback(
    (ref: NodeRef, properties: Properties) => {
      const key = model.graph.hasNode(nodeKey(ref))
        ? nodeKey(ref)
        : model.addNode(ref, properties, selected ?? undefined);
      setSelected(key);
      return key;
    },
    [model, selected],
  );

  const value = useMemo(
    () => ({ model, version, selected, select: setSelected, open }),
    [model, version, selected, open],
  );
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

/** The Explore workspace; throws outside `WorkspaceProvider`. */
export function useWorkspace(): WorkspaceValue {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return value;
}
