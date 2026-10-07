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
import type { ColorBy } from "./canvasStyle";
import { GraphModel, nodeKey, type NodeRef } from "./GraphModel";

/** Whether the canvas holds the tenant as a whole (in full or as a sample). */
export interface Overview {
  /** `dismissed`: the user cleared it; it is not loaded again by itself. */
  status: "idle" | "loading" | "shown" | "failed" | "dismissed";
  /** Nodes drawn, and nodes in the tenant. */
  shown?: number;
  total?: number;
}

interface WorkspaceValue {
  /** The Explore canvas; kept while the user moves between views. */
  model: GraphModel;
  /** Changes whenever the model does, so components re-render. */
  version: number;
  /** The selected node; selecting an edge clears it, and the other way round. */
  selected: string | null;
  select: (key: string | null) => void;
  selectedEdge: string | null;
  selectEdge: (key: string | null) => void;
  colorBy: ColorBy;
  setColorBy: (colorBy: ColorBy) => void;
  /** Puts a node on the canvas (if it isn't there) and selects it. */
  open: (ref: NodeRef, properties: Properties) => string;
  overview: Overview;
  setOverview: (overview: Overview) => void;
}

const WorkspaceContext = createContext<WorkspaceValue | null>(null);

/**
 * Holds what the user is working on in Explore: the nodes on the canvas, the
 * selection, and whether the whole graph was loaded. Search, Activity and Path
 * all hand nodes to it. A new tenant starts with an empty canvas.
 */
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { tenant } = useTenantSession();
  const [model] = useState(() => new GraphModel());
  const [selected, setSelected] = useState<string | null>(null);
  const [overview, setOverview] = useState<Overview>({ status: "idle" });
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null);
  const [colorBy, setColorBy] = useState<ColorBy>("none");
  const select = useCallback((key: string | null) => {
    setSelected(key);
    if (key) setSelectedEdge(null);
  }, []);
  const selectEdge = useCallback((key: string | null) => {
    setSelectedEdge(key);
    if (key) setSelected(null);
  }, []);
  const version = useSyncExternalStore(
    useCallback((onChange: () => void) => model.subscribe(onChange), [model]),
    () => model.revision,
  );

  useEffect(() => {
    model.clear();
    setSelected(null);
    setSelectedEdge(null);
    setOverview({ status: "idle" });
  }, [model, tenant]);

  // A selected node that was deleted is no longer selected.
  useEffect(() => {
    if (selected && !model.graph.hasNode(selected)) setSelected(null);
    if (selectedEdge && !model.graph.hasEdge(selectedEdge)) setSelectedEdge(null);
  }, [model, selected, selectedEdge, version]);

  const open = useCallback(
    (ref: NodeRef, properties: Properties) => {
      const key = model.graph.hasNode(nodeKey(ref))
        ? nodeKey(ref)
        : model.addNode(ref, properties, selected ?? undefined);
      select(key);
      return key;
    },
    [model, selected, select],
  );

  const value = useMemo(
    () => ({
      model,
      version,
      selected,
      select,
      selectedEdge,
      selectEdge,
      colorBy,
      setColorBy,
      open,
      overview,
      setOverview,
    }),
    [model, version, selected, select, selectedEdge, selectEdge, colorBy, open, overview],
  );
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

/** The Explore workspace; throws outside `WorkspaceProvider`. */
export function useWorkspace(): WorkspaceValue {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return value;
}
