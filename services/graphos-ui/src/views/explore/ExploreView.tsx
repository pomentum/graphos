import { CloseButton } from "@mantine/core";
import { useHotkeys } from "@mantine/hooks";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import type Sigma from "sigma";
import { ApiError } from "../../api/http";
import type { NodeUpsert, SearchHit } from "../../api/types";
import { CanvasExportMenu } from "../../components/CanvasExportMenu";
import { CanvasLegend } from "../../components/CanvasLegend";
import { EdgeInspector } from "../../components/EdgeInspector";
import { ExpandDialog } from "../../components/ExpandDialog";
import { GraphCanvas } from "../../components/GraphCanvas";
import { Inspector } from "../../components/Inspector";
import { NewEdgeDialog } from "../../components/NewEdgeDialog";
import { NewNodeDialog } from "../../components/NewNodeDialog";
import { useCreateEdge, useCreateNode, useLabelStyles, useSchema } from "../../data/queries";
import { circular, force, layered, type CanvasLayout } from "../../graph/canvasLayouts";
import { categorize, styleMaxima } from "../../graph/canvasStyle";
import {
  displayName,
  moreKey,
  type EdgeAttributes,
  type GraphModel,
  type NodeAttributes,
  type NodeRef,
} from "../../graph/GraphModel";
import { Endpoints } from "../../lib/endpoints";
import { SIZE_MODES, type SizeBy } from "../../lib/metrics";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { useTenantSession } from "../../session/SessionProvider";
import { usePalette } from "../../theme/usePalette";
import { CanvasStatus } from "./CanvasStatus";
import { ExploreActions } from "./ExploreActions";
import classes from "./ExploreView.module.css";
import { GroupsMode } from "./GroupsMode";
import { MapView } from "./MapView";
import { ModeSwitch } from "./ModeSwitch";
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
  const { t } = useTranslation();
  const { canWrite, api, tenant } = useTenantSession();
  const workspace = useWorkspace();
  // The canvas's renderer, for saving it as a picture.
  const renderer = useRef<Sigma<NodeAttributes, EdgeAttributes> | null>(null);
  const { model, version, selected, select, selectedEdge, selectEdge, colorBy, sizeBy, open, overview, frameOn, mode } =
    workspace;
  const { load } = useWholeGraph();
  const palette = usePalette();
  const schema = useSchema();
  const endpoints = useMemo(() => new Endpoints(schema.data ?? { label_hierarchy: {} }), [schema.data]);
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
  // A new key per opening: each dialog starts empty, without the last one's values or error.
  const [dialogKey, setDialogKey] = useState(0);
  const [existing, setExisting] = useState<NodeRef | null>(null);
  const createNode = useCreateNode();
  const createEdge = useCreateEdge();
  // `?new=<label>`: arrived to create a node of that label (from the schema diagram).
  const [params, setParams] = useSearchParams();
  const [newLabel, setNewLabel] = useState<string | undefined>(undefined);

  // `version` changes with the model, so categories and neighbourhoods are recomputed.
  const styles = useLabelStyles();
  const categories = useMemo(() => categorize(model, colorBy, styles), [model, colorBy, styles, version]);
  const styleMax = useMemo(() => styleMaxima(model, styles), [model, styles, version]);
  const hubs = useMemo(() => new Set(model.hubs(HUB_LABELS)), [model, version]);
  // The metrics the canvas nodes carry, and the largest value of the one sized by.
  const metricMax = useMemo(() => canvasMetricMax(model), [model, version]);
  const sizes = useMemo(() => {
    if (sizeBy === "connections") return undefined;
    const max = metricMax.get(sizeBy) ?? 0;
    return max > 0 ? { property: sizeBy, max } : undefined;
  }, [sizeBy, metricMax]);
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
      sizes,
      styles,
      styleMax,
    }),
    [model, selected, selectedEdge, categories, recent, fresh, hubs, focusOnly, sizes, styles, styleMax, version],
  );

  const selectedNode = selected ? model.graph.getNodeAttributes(selected) : null;

  // How the nodes are placed: ForceAtlas2 as they arrive, or circular / layered on request.
  const [layout, setLayout] = useState<CanvasLayout>("force");
  const placeNodes = async (next: CanvasLayout) => {
    setLayout(next);
    if (next === "circular") circular(model);
    else if (next === "layered") await layered(model);
    else force(model);
    // Show the whole canvas once the nodes are in place (ForceAtlas2 keeps moving them).
    renderer.current?.getCamera().animatedReset({ duration: 400 });
  };

  const startCreating = (kind: "node" | "edge") => {
    createNode.reset();
    createEdge.reset();
    setExisting(null);
    setDialogKey((k) => k + 1);
    setCreating(kind);
  };

  useEffect(() => {
    const label = params.get("new");
    if (!label) return;
    setParams({}, { replace: true });
    if (!canWrite) return;
    setNewLabel(label);
    startCreating("node");
    // Only the parameter matters: it is read once and removed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  /** Creates the node unless one with that id exists: creating would overwrite it, so offer it instead. */
  const createNewNode = async (node: NodeUpsert) => {
    const found = await api.node(node.id, node.label).catch(() => null);
    if (found) {
      setExisting({ id: node.id, label: node.label });
      return;
    }
    if (!(await createNode.mutateAsync(node).catch(() => null))) return;
    open({ id: node.id, label: node.label }, { ...node.properties, id: node.id });
    setCreating(null);
  };

  const deselect = () => {
    select(null);
    selectEdge(null);
    setFocusOnly(false);
  };
  const hide = (key: string) => {
    model.removeNode(key);
    select(null);
  };
  const graphKeys = mode === "graph";
  useHotkeys(
    graphKeys
      ? [
          ["mod+Z", () => model.undo()],
          ["Escape", deselect],
          ["F", () => selected && setFocusOnly((on) => !on)],
          ["E", () => selected && void expand(selected)],
          ["C", () => selected && model.collapse(selected)],
          ["H", () => selected && hide(selected)],
        ]
      : [],
  );

  if (mode === "map") return <MapView onMode={workspace.setMode} />;
  if (mode === "groups") return <GroupsMode onMode={workspace.setMode} />;

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
      <section className={classes.canvas} aria-label={t("views.explore.graphRegion")}>
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
          rendererRef={renderer}
        />
        <CanvasStatus overview={overview} isEmpty={model.isEmpty} onLoad={() => void load()} />
        <CanvasLegend categories={categories} styles={styles} />
        <ExploreActions
          leading={<ModeSwitch mode="graph" onChange={workspace.setMode} />}
          trailing={
            <CanvasExportMenu renderer={renderer} graph={model.graph} palette={palette} name={[tenant, "graph"]} />
          }
          canWrite={canWrite}
          hasSelection={Boolean(selected)}
          isEmpty={model.isEmpty}
          busy={isExpanding}
          colorBy={colorBy}
          sizeBy={sizeBy}
          sizeOptions={new Set(metricMax.keys())}
          lastStep={model.lastStep}
          focusOnly={focusOnly}
          onBack={() => model.undo()}
          onFocus={() => setFocusOnly((on) => !on)}
          layout={layout}
          nodeCount={model.graph.order}
          onLayout={(next) => void placeNodes(next)}
          onColorBy={workspace.setColorBy}
          onSizeBy={workspace.setSizeBy}
          onNewNode={() => {
            setNewLabel(undefined);
            startCreating("node");
          }}
          onConnect={() => startCreating("edge")}
          onClear={() => {
            deselect();
            model.clear();
            workspace.setOverview({ status: "dismissed" });
          }}
        />
      </section>

      {selected && selectedNode?.more === undefined ? (
        <aside className={classes.inspector} aria-label={t("views.explore.inspector")}>
          <CloseButton className={classes.close} aria-label={t("views.explore.closeInspector")} onClick={deselect} />
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
        <aside className={classes.inspector} aria-label={t("views.explore.inspector")}>
          <CloseButton className={classes.close} aria-label={t("views.explore.closeInspector")} onClick={deselect} />
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
        key={`node-${dialogKey}`}
        opened={creating === "node"}
        labels={schema.data?.vertex_labels ?? []}
        initialLabel={newLabel}
        saving={createNode.isPending}
        error={createNode.error instanceof ApiError ? createNode.error.message : null}
        existing={existing}
        onEdit={() => setExisting(null)}
        onOpenExisting={(ref) => {
          void api.node(ref.id, ref.label).then((record) => open(ref, record.properties));
          setCreating(null);
        }}
        onCancel={() => setCreating(null)}
        onCreate={(node) => void createNewNode(node)}
      />
      {selectedNode ? (
        <NewEdgeDialog
          key={`edge-${dialogKey}`}
          opened={creating === "edge"}
          fromName={displayName(selectedNode.properties, selectedNode.id)}
          fromLabel={selectedNode.vertexLabel}
          edgeTypes={(schema.data?.edge_labels ?? []).filter((type) => !schema.data?.rule_edge_labels.includes(type))}
          endpoints={endpoints}
          strict={Boolean(schema.data?.strict_endpoints)}
          saving={createEdge.isPending}
          error={
            createEdge.error
              ? createEdge.error.message
              : createEdge.data?.edges_upserted === 0
                ? t("errors.otherNodeGone")
                : null
          }
          onCancel={() => setCreating(null)}
          onCreate={(type, to) => void connect(type, to)}
        />
      ) : null}
    </div>
  );
}

/** For each size-by metric some canvas node carries, its largest value there. */
function canvasMetricMax(model: GraphModel): Map<SizeBy, number> {
  const max = new Map<SizeBy, number>();
  model.graph.forEachNode((_, node) => {
    if (node.more !== undefined) return;
    for (const mode of SIZE_MODES) {
      if (mode === "connections") continue;
      const value = node.properties[mode];
      if (typeof value === "number" && value > (max.get(mode) ?? 0)) max.set(mode, value);
      else if (typeof value === "number" && !max.has(mode)) max.set(mode, 0);
    }
  });
  return max;
}
