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
import type { Lens, SizeBy } from "../lib/metrics";
import type { ColorBy } from "./canvasStyle";
import { GraphModel, nodeKey, type NodeRef } from "./GraphModel";

/** Explore shows the map of labels or the graph canvas. */
export type ExploreMode = "map" | "groups" | "graph";

/** Whether the canvas holds the tenant as a whole (in full or as a sample). */
export interface Overview {
  /**
   * `shown`: the whole tenant is on the canvas; `map`: too big for that, the
   * map is shown instead; `dismissed`: the user cleared the canvas, which is
   * not loaded again by itself.
   */
  status: "idle" | "loading" | "shown" | "map" | "failed" | "dismissed";
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
  /** Nodes picked together (Ctrl or Shift click, lasso); empty when one node or none is selected. */
  multi: ReadonlySet<string>;
  setMulti: (keys: ReadonlySet<string>) => void;
  colorBy: ColorBy;
  setColorBy: (colorBy: ColorBy) => void;
  /** What nodes are sized by: their connections on the canvas, or a graph metric. */
  sizeBy: SizeBy;
  setSizeBy: (sizeBy: SizeBy) => void;
  /** What the Groups mode shows: the groups, a metric's ranking, or the components. */
  lens: Lens;
  setLens: (lens: Lens) => void;
  /** Puts a node on the canvas (if it isn't there), selects it and shows the graph. */
  open: (ref: NodeRef, properties: Properties) => string;
  overview: Overview;
  setOverview: (overview: Overview) => void;
  /** Where the camera should go next: these nodes, whenever `token` changes. */
  frame: { keys: string[]; token: number } | undefined;
  /** Moves the camera to show these nodes. */
  frameOn: (keys: string[]) => void;
  mode: ExploreMode;
  setMode: (mode: ExploreMode) => void;
  /** The label selected on the map, kept while the user goes elsewhere. */
  mapLabel: string | null;
  setMapLabel: (label: string | null) => void;
  /** The group selected under Groups (its bubble key), kept likewise. */
  selectedGroup: string | null;
  setSelectedGroup: (key: string | null) => void;
}

const WorkspaceContext = createContext<WorkspaceValue | null>(null);
const NONE: ReadonlySet<string> = new Set();

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
  const [multi, setMultiState] = useState<ReadonlySet<string>>(NONE);
  const [colorBy, setColorBy] = useState<ColorBy>("none");
  const [sizeBy, setSizeBy] = useState<SizeBy>("connections");
  const [lens, setLens] = useState<Lens>("groups");
  const [frame, setFrame] = useState<{ keys: string[]; token: number }>();
  const [mode, setMode] = useState<ExploreMode>("graph");
  const [mapLabel, setMapLabel] = useState<string | null>(null);
  const [selectedGroup, setSelectedGroup] = useState<string | null>(null);
  const frameOn = useCallback(
    (keys: string[]) => setFrame((current) => ({ keys, token: (current?.token ?? 0) + 1 })),
    [],
  );
  const select = useCallback((key: string | null) => {
    setSelected(key);
    setMultiState(NONE);
    if (key) setSelectedEdge(null);
  }, []);
  const selectEdge = useCallback((key: string | null) => {
    setSelectedEdge(key);
    setMultiState(NONE);
    if (key) setSelected(null);
  }, []);
  // Several nodes replace the single selection; one is the single selection.
  const setMulti = useCallback((keys: ReadonlySet<string>) => {
    setSelectedEdge(null);
    if (keys.size === 1) {
      setSelected([...keys][0]!);
      setMultiState(NONE);
    } else {
      setSelected(null);
      setMultiState(keys.size === 0 ? NONE : keys);
    }
  }, []);
  const version = useSyncExternalStore(
    useCallback((onChange: () => void) => model.subscribe(onChange), [model]),
    () => model.revision,
  );

  useEffect(() => {
    model.clear();
    setSelected(null);
    setSelectedEdge(null);
    setMultiState(NONE);
    setOverview({ status: "idle" });
    setMode("graph");
    setMapLabel(null);
    setSelectedGroup(null);
  }, [model, tenant]);

  // A selected node that was deleted is no longer selected.
  useEffect(() => {
    if (selected && !model.graph.hasNode(selected)) setSelected(null);
    if (selectedEdge && !model.graph.hasEdge(selectedEdge)) setSelectedEdge(null);
    if ([...multi].some((key) => !model.graph.hasNode(key))) {
      setMultiState(new Set([...multi].filter((key) => model.graph.hasNode(key))));
    }
  }, [model, selected, selectedEdge, multi, version]);

  const open = useCallback(
    (ref: NodeRef, properties: Properties) => {
      const key = model.graph.hasNode(nodeKey(ref))
        ? nodeKey(ref)
        : model.addNode(ref, properties, selected ?? undefined);
      select(key);
      setMode("graph");
      frameOn([key, ...model.graph.neighbors(key)]);
      return key;
    },
    [model, selected, select, frameOn],
  );

  // Checked while rendering: after Clear or a delete, no view may read a
  // node or edge that is gone (the effect above only tidies the state).
  const liveSelected = selected && model.graph.hasNode(selected) ? selected : null;
  const liveSelectedEdge = selectedEdge && model.graph.hasEdge(selectedEdge) ? selectedEdge : null;

  const value = useMemo(
    () => ({
      model,
      version,
      selected: liveSelected,
      select,
      selectedEdge: liveSelectedEdge,
      selectEdge,
      multi,
      setMulti,
      colorBy,
      setColorBy,
      sizeBy,
      setSizeBy,
      lens,
      setLens,
      open,
      overview,
      setOverview,
      frame,
      frameOn,
      mode,
      setMode,
      mapLabel,
      setMapLabel,
      selectedGroup,
      setSelectedGroup,
    }),
    [
      model,
      version,
      liveSelected,
      select,
      liveSelectedEdge,
      selectEdge,
      multi,
      setMulti,
      colorBy,
      sizeBy,
      lens,
      open,
      overview,
      frame,
      frameOn,
      mode,
      mapLabel,
      selectedGroup,
    ],
  );
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

/** The Explore workspace; throws outside `WorkspaceProvider`. */
export function useWorkspace(): WorkspaceValue {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return value;
}
