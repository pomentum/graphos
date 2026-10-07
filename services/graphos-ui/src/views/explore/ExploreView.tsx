import { useHotkeys } from "@mantine/hooks";
import { useCallback, useMemo, useState } from "react";
import { ApiError } from "../../api/http";
import type { SearchHit } from "../../api/types";
import { CanvasLegend } from "../../components/CanvasLegend";
import { EdgeInspector } from "../../components/EdgeInspector";
import { ExpandDialog } from "../../components/ExpandDialog";
import { GraphCanvas } from "../../components/GraphCanvas";
import { Inspector } from "../../components/Inspector";
import { NewEdgeDialog } from "../../components/NewEdgeDialog";
import { NewNodeDialog } from "../../components/NewNodeDialog";
import { useCreateEdge, useCreateNode, useSchema } from "../../data/queries";
import { categorize } from "../../graph/canvasStyle";
import { displayName, moreKey } from "../../graph/GraphModel";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { useTenantSession } from "../../session/SessionProvider";
import { usePalette } from "../../theme/usePalette";
import { CanvasStatus } from "./CanvasStatus";
import { ExploreActions } from "./ExploreActions";
import classes from "./ExploreView.module.css";
import { useExpandNode } from "./useExpandNode";
import { useFreshNodes } from "./useFreshNodes";
import { useRecentlyChanged } from "./useRecentlyChanged";
import { useWholeGraph } from "./useWholeGraph";

/** The best-connected nodes on the canvas are labelled while nothing is selected. */
const HUB_LABELS = 12;

/**
 * Explore: the canvas (the whole graph on arrival, or nodes found by search
 * and expanded by double-click), with the inspector for the selected node or
 * edge.
 *
 * Built to stay followable on a large graph: an expand adds a page of
 * neighbours fanned out beside the node, selects it, labels what appeared
 * and moves the camera there, while the rest fades; Back undoes any step,
 * Focus shows only the selection's neighbourhood, and only the hubs, the
 * selection and new nodes carry labels.
 *
 * Keys: Escape deselects, F focus, E expand, C collapse, H hide, Ctrl/⌘+Z back.
 */
export function ExploreView() {
  const { canWrite } = useTenantSession();
  const workspace = useWorkspace();
  const { model, version, selected, select, selectedEdge, selectEdge, colorBy, open, overview, frameOn } = workspace;
  const { load } = useWholeGraph();
  const palette = usePalette();
  const schema = useSchema();
  const recent = useRecentlyChanged(model);
  const [fresh, markFresh] = useFreshNodes(selected);
  const [focusOnly, setFocusOnly] = useState(false);
  // After an expand: select the node, label what it added, and show both.
  const onExpanded = useCallback(
    (center: string, added: string[]) => {
      select(center);
      markFresh(center, added);
      // What was added, or (nothing new) the node among its neighbours.
      frameOn(added.length > 0 ? [center, ...added, moreKey(center)] : [center, ...model.graph.neighbors(center)]);
    },
    [model, select, markFresh, frameOn],
  );
  const { expand, dialog, isExpanding } = useExpandNode(model, onExpanded);
  const [creating, setCreating] = useState<"node" | "edge" | null>(null);
  const createNode = useCreateNode();
  const createEdge = useCreateEdge();

  // `version` changes with the model, so categories and neighbourhoods are recomputed.
  const categories = useMemo(() => categorize(model, colorBy), [model, colorBy, version]);
  const hubs = useMemo(() => new Set(model.hubs(HUB_LABELS)), [model, version]);
  const emphasis = useMemo(
    () => ({
      selected,
      selectedEdge,
      categories,
      focus: selected
        ? model.neighborhood(selected)
        : selectedEdge
          ? { nodes: new Set(model.graph.extremities(selectedEdge)), edges: new Set<string>() }
          : { nodes: new Set<string>(), edges: new Set<string>() },
      recent,
      fresh,
      hubs,
      isolate: focusOnly,
    }),
    [model, selected, selectedEdge, categories, recent, fresh, hubs, focusOnly, version],
  );

  const selectedNode = selected ? model.graph.getNodeAttributes(selected) : null;

  const deselect = () => {
    select(null);
    selectEdge(null);
    setFocusOnly(false);
  };
  const hide = (key: string) => {
    model.removeNode(key);
    select(null);
  };
  useHotkeys([
    ["mod+Z", () => model.undo()],
    ["Escape", deselect],
    ["F", () => selected && setFocusOnly((on) => !on)],
    ["E", () => selected && void expand(selected)],
    ["C", () => selected && model.collapse(selected)],
    ["H", () => selected && hide(selected)],
  ]);

  const connect = async (type: string, to: SearchHit) => {
    if (!selected || !selectedNode) return;
    const target = { id: String(to.properties.id ?? to.id), label: to.label };
    const result = await createEdge
      .mutateAsync({
        src_id: selectedNode.id,
        src_label: selectedNode.vertexLabel,
        dst_id: target.id,
        dst_label: target.label,
        type,
        properties: {},
      })
      .catch(() => null);
    if (!result || result.edges_upserted === 0) return;
    const targetKey = model.addNode(target, to.properties, selected);
    model.addEdge(type, selected, targetKey);
    setCreating(null);
  };

  const inspectorOpen = Boolean(selected || selectedEdge);
  return (
    <div className={classes.root} data-inspector={inspectorOpen ? "open" : "closed"}>
      <section className={classes.canvas} aria-label="Graph">
        <GraphCanvas
          model={model}
          emphasis={emphasis}
          palette={palette}
          onSelect={(key) => {
            select(key);
            if (!key) setFocusOnly(false);
          }}
          onSelectEdge={selectEdge}
          onExpand={(key) => void expand(key)}
          frame={workspace.frame}
        />
        <CanvasStatus overview={overview} isEmpty={model.isEmpty} onLoad={() => void load()} />
        <CanvasLegend categories={categories} />
        <ExploreActions
          canWrite={canWrite}
          hasSelection={Boolean(selected)}
          isEmpty={model.isEmpty}
          busy={isExpanding}
          colorBy={colorBy}
          lastStep={model.lastStep}
          focusOnly={focusOnly}
          onBack={() => model.undo()}
          onFocus={() => setFocusOnly((on) => !on)}
          onColorBy={workspace.setColorBy}
          onNewNode={() => setCreating("node")}
          onConnect={() => setCreating("edge")}
          onClear={() => {
            deselect();
            model.clear();
            workspace.setOverview({ status: "dismissed" });
          }}
        />
      </section>

      {selected && selectedNode?.more === undefined ? (
        <aside className={classes.inspector} aria-label="Inspector">
          <Inspector
            key={selected}
            model={model}
            selected={selected}
            onSelect={select}
            onSelectEdge={selectEdge}
            onExpand={(key) => void expand(key)}
            onHide={hide}
          />
        </aside>
      ) : selectedEdge ? (
        <aside className={classes.inspector} aria-label="Inspector">
          <EdgeInspector
            key={selectedEdge}
            model={model}
            edge={selectedEdge}
            onSelectNode={select}
            onDeleted={() => selectEdge(null)}
          />
        </aside>
      ) : null}

      <ExpandDialog {...dialog} />
      <NewNodeDialog
        opened={creating === "node"}
        labels={schema.data?.vertex_labels ?? []}
        saving={createNode.isPending}
        error={createNode.error instanceof ApiError ? createNode.error.message : null}
        onCancel={() => setCreating(null)}
        onCreate={async (node) => {
          if (!(await createNode.mutateAsync(node).catch(() => null))) return;
          open({ id: node.id, label: node.label }, { ...node.properties, id: node.id });
          setCreating(null);
        }}
      />
      {selectedNode ? (
        <NewEdgeDialog
          opened={creating === "edge"}
          fromName={displayName(selectedNode.properties, selectedNode.id)}
          edgeTypes={(schema.data?.edge_labels ?? []).filter((t) => !schema.data?.rule_edge_labels.includes(t))}
          saving={createEdge.isPending}
          error={
            createEdge.error
              ? createEdge.error.message
              : createEdge.data?.edges_upserted === 0
                ? "The other node no longer exists."
                : null
          }
          onCancel={() => setCreating(null)}
          onCreate={(type, to) => void connect(type, to)}
        />
      ) : null}
    </div>
  );
}
