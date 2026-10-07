import { useMemo, useState } from "react";
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
import { displayName } from "../../graph/GraphModel";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { useTenantSession } from "../../session/SessionProvider";
import { usePalette } from "../../theme/usePalette";
import { CanvasStatus } from "./CanvasStatus";
import { ExploreActions } from "./ExploreActions";
import classes from "./ExploreView.module.css";
import { useExpandNode } from "./useExpandNode";
import { useRecentlyChanged } from "./useRecentlyChanged";
import { useWholeGraph } from "./useWholeGraph";

/**
 * Explore: the canvas (the whole graph on arrival, or nodes found by search
 * and expanded by double-click), with the inspector for the selected node or
 * edge.
 */
export function ExploreView() {
  const { canWrite } = useTenantSession();
  const workspace = useWorkspace();
  const { model, version, selected, select, selectedEdge, selectEdge, colorBy, open, overview } = workspace;
  const { load } = useWholeGraph();
  const palette = usePalette();
  const schema = useSchema();
  const recent = useRecentlyChanged(model);
  const { expand, dialog, isExpanding } = useExpandNode(model);
  const [creating, setCreating] = useState<"node" | "edge" | null>(null);
  const createNode = useCreateNode();
  const createEdge = useCreateEdge();

  // `version` changes with the model, so categories and neighbourhoods are recomputed.
  const categories = useMemo(() => categorize(model, colorBy), [model, colorBy, version]);
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
    }),
    [model, selected, selectedEdge, categories, recent, version],
  );

  const selectedNode = selected ? model.graph.getNodeAttributes(selected) : null;

  const connect = async (type: string, to: SearchHit) => {
    if (!selected || !selectedNode) return;
    const target = { id: String(to.properties.id ?? to.id), label: to.label };
    const result = await createEdge.mutateAsync({
      src_id: selectedNode.id,
      src_label: selectedNode.vertexLabel,
      dst_id: target.id,
      dst_label: target.label,
      type,
      properties: {},
    });
    if (result.edges_upserted === 0) return;
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
          onSelect={select}
          onSelectEdge={selectEdge}
          onExpand={(key) => void expand(key)}
        />
        <CanvasStatus overview={overview} isEmpty={model.isEmpty} onLoad={() => void load()} />
        <CanvasLegend categories={categories} />
        <ExploreActions
          canWrite={canWrite}
          hasSelection={Boolean(selected)}
          isEmpty={model.isEmpty}
          busy={isExpanding}
          colorBy={colorBy}
          onColorBy={workspace.setColorBy}
          onNewNode={() => setCreating("node")}
          onConnect={() => setCreating("edge")}
          onClear={() => {
            model.clear();
            workspace.setOverview({ status: "dismissed" });
          }}
        />
      </section>

      {selected ? (
        <aside className={classes.inspector} aria-label="Inspector">
          <Inspector
            key={selected}
            model={model}
            selected={selected}
            onSelect={select}
            onExpand={(key) => void expand(key)}
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
          await createNode.mutateAsync(node);
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
