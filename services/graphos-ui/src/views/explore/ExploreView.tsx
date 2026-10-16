import { CloseButton } from "@mantine/core";
import { useHotkeys } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
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
import { holdView } from "../../components/canvasGestures";
import { GraphCanvas } from "../../components/GraphCanvas";
import { Inspector } from "../../components/Inspector";
import { NewEdgeDialog, type NewEdgeDialogProps } from "../../components/NewEdgeDialog";
import { NewNodeDialog } from "../../components/NewNodeDialog";
import { useCreateEdge, useCreateNode, useDeleteNodes, useLabelStyles, useSchema } from "../../data/queries";
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
import { selectedNodes, selectionCsv, toggled } from "../../graph/selection";
import { canvasJson } from "../../lib/canvasExport";
import { CSV_TYPE, exportName } from "../../lib/csv";
import { saveFile } from "../../lib/download";
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
import { SelectionBar } from "./SelectionBar";
import { useExpandNode } from "./useExpandNode";
import { useFreshNodes } from "./useFreshNodes";
import { useRecentlyChanged } from "./useRecentlyChanged";
import { useWholeGraph } from "./useWholeGraph";

/** The best-connected nodes on the canvas are labelled while nothing is selected. */
const HUB_LABELS = 12;
/** Nodes of a selection expanded at once: each is one request. */
const EXPAND_MANY = 20;

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
 * Several nodes are picked with Ctrl, ⌘ or Shift click, or by drawing a loop
 * with Shift-drag on the empty canvas; the selection bar then expands, hides,
 * exports or deletes them together. With write access, a double-click on the
 * empty canvas creates a node there, and Alt-drag from one node onto another
 * connects them.
 *
 * Keys: Escape deselects, F focus, E expand, C collapse, H hide, Ctrl/⌘+A
 * select all, Ctrl/⌘+Z back.
 */
export function ExploreView() {
  const { t } = useTranslation();
  const { canWrite, api, tenant } = useTenantSession();
  const workspace = useWorkspace();
  // The canvas's renderer, for saving it as a picture.
  const renderer = useRef<Sigma<NodeAttributes, EdgeAttributes> | null>(null);
  const {
    model,
    version,
    selected,
    select,
    selectedEdge,
    selectEdge,
    multi,
    setMulti,
    colorBy,
    sizeBy,
    open,
    overview,
    frameOn,
    mode,
  } = workspace;
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
  const { expand, expandMany, dialog, isExpanding } = useExpandNode(model, onExpanded);
  const [expandingMany, setExpandingMany] = useState(false);
  const deleteNodes = useDeleteNodes();
  // Where a double-click asked for a new node, and a link drawn between two nodes.
  const [createAt, setCreateAt] = useState<{ x: number; y: number } | null>(null);
  const [link, setLink] = useState<{ src: string; dst: string } | null>(null);
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
      multi,
    }),
    [
      model,
      selected,
      selectedEdge,
      categories,
      recent,
      fresh,
      hubs,
      focusOnly,
      sizes,
      styles,
      styleMax,
      multi,
      version,
    ],
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

  /** Opens a dialog; a node is placed `at` a point of the canvas when given, else beside the selection. */
  const startCreating = (kind: "node" | "edge", at: { x: number; y: number } | null = null) => {
    setCreateAt(at);
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
    const ref = { id: node.id, label: node.label };
    const properties = { ...node.properties, id: node.id };
    if (createAt && renderer.current)
      select(holdView(renderer.current, () => model.addNodeAt(ref, properties, createAt)));
    else open(ref, properties);
    setCreateAt(null);
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
  const hideMany = () => {
    model.removeNodes(multi);
    setMulti(new Set());
  };
  const selectAll = () => setMulti(new Set(model.graph.filterNodes((_, node) => node.more === undefined)));
  const graphKeys = mode === "graph";
  useHotkeys(
    graphKeys
      ? [
          ["mod+Z", () => model.undo()],
          ["Escape", deselect],
          ["F", () => selected && setFocusOnly((on) => !on)],
          ["E", () => selected && void expand(selected)],
          ["C", () => selected && model.collapse(selected)],
          ["H", () => (multi.size > 0 ? hideMany() : selected && hide(selected))],
          ["mod+A", selectAll],
        ]
      : [],
  );

  if (mode === "map") return <MapView onMode={workspace.setMode} />;
  if (mode === "groups") return <GroupsMode onMode={workspace.setMode} />;

  const connect = async (from: string | null, type: string, to: SearchHit) => {
    if (!from || !model.graph.hasNode(from)) return;
    const source = model.graph.getNodeAttributes(from);
    const target = { id: String(to.properties.id ?? to.id), label: to.label };
    const result = await createEdge
      .mutateAsync({
        src_id: source.id,
        src_label: source.vertexLabel,
        dst_id: target.id,
        dst_label: target.label,
        type,
        properties: {},
      })
      .catch(() => null);
    if (!result || result.edges_upserted === 0) return;
    const targetKey = model.addNode(target, to.properties, from);
    model.addEdge(type, from, targetKey);
    setCreating(null);
    setLink(null);
  };

  const startLink = (src: string, dst: string) => {
    createEdge.reset();
    setDialogKey((k) => k + 1);
    setLink({ src, dst });
  };

  const expandSelection = async () => {
    const keys = selectedNodes(model, multi).slice(0, EXPAND_MANY);
    setExpandingMany(true);
    const added = await expandMany(keys).finally(() => setExpandingMany(false));
    frameOn([...keys, ...added]);
  };

  const deleteSelection = async () => {
    const keys = selectedNodes(model, multi);
    const refs = keys.map((key) => {
      const node = model.graph.getNodeAttributes(key);
      return { id: node.id, label: node.vertexLabel };
    });
    // Each batch graphos deleted leaves the canvas, even when a later one fails.
    const onDeleted = (count: number) => model.forget(...keys.splice(0, count));
    const deleted = await deleteNodes.mutateAsync({ nodes: refs, onDeleted }).catch(() => null);
    if (deleted === null) return;
    setMulti(new Set());
    notifications.show({ message: t("notices.nodesDeleted", { count: deleted }), color: "green" });
  };

  const inspectorOpen = Boolean(selected || selectedEdge);
  return (
    <div className={classes.root} data-inspector={inspectorOpen ? "open" : "closed"}>
      <section className={classes.canvas} aria-label={t("views.explore.graphRegion")}>
        <GraphCanvas
          model={model}
          emphasis={emphasis}
          palette={palette}
          onSelect={(key, additive) => {
            if (key && additive) {
              setMulti(toggled(multi.size > 0 ? multi : new Set(selected ? [selected] : []), [key]));
              return;
            }
            select(key);
            if (!key) setFocusOnly(false);
          }}
          onLasso={(keys, additive) =>
            setMulti(additive ? new Set([...multi, ...(selected ? [selected] : []), ...keys]) : new Set(keys))
          }
          onSelectEdge={selectEdge}
          onExpand={(key) => void expand(key)}
          onCreateAt={
            canWrite
              ? (point) => {
                  setNewLabel(undefined);
                  startCreating("node", point);
                }
              : undefined
          }
          onLink={canWrite ? startLink : undefined}
          frame={workspace.frame}
          rendererRef={renderer}
          minimap
        />
        <CanvasStatus overview={overview} isEmpty={model.isEmpty} onLoad={() => void load()} />
        {multi.size > 0 ? (
          <SelectionBar
            count={multi.size}
            canWrite={canWrite}
            expandLimit={EXPAND_MANY}
            expanding={expandingMany}
            deleting={deleteNodes.isPending}
            onExpand={() => void expandSelection()}
            onHide={hideMany}
            onExportJson={() => saveFile(exportName([tenant, "selection"], "json"), canvasJson(model.graph, multi))}
            onExportCsv={() => saveFile(exportName([tenant, "selection"], "csv"), selectionCsv(model, multi), CSV_TYPE)}
            onDelete={() => void deleteSelection()}
            onClear={() => setMulti(new Set())}
          />
        ) : null}
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
        onCancel={() => {
          setCreating(null);
          setCreateAt(null);
        }}
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
          onCreate={(type, to) => void connect(selected, type, to)}
        />
      ) : null}
      {link && model.graph.hasNode(link.src) && model.graph.hasNode(link.dst) ? (
        <LinkDialog
          key={`link-${dialogKey}`}
          model={model}
          link={link}
          endpoints={endpoints}
          edgeTypes={(schema.data?.edge_labels ?? []).filter((type) => !schema.data?.rule_edge_labels.includes(type))}
          strict={Boolean(schema.data?.strict_endpoints)}
          saving={createEdge.isPending}
          error={createEdge.error ? createEdge.error.message : null}
          onCancel={() => setLink(null)}
          onCreate={(type, to) => void connect(link.src, type, to)}
        />
      ) : null}
    </div>
  );
}

/** Connects two nodes drawn on the canvas: only the relationship type is asked. */
function LinkDialog({
  model,
  link,
  ...props
}: { model: GraphModel; link: { src: string; dst: string } } & Omit<
  NewEdgeDialogProps,
  "opened" | "fromName" | "fromLabel" | "target"
>) {
  const source = model.graph.getNodeAttributes(link.src);
  const target = model.graph.getNodeAttributes(link.dst);
  return (
    <NewEdgeDialog
      {...props}
      opened
      fromName={displayName(source.properties, source.id)}
      fromLabel={source.vertexLabel}
      target={{
        id: 0,
        label: target.vertexLabel,
        properties: { ...target.properties, id: target.id },
        score: null,
        matched: null,
      }}
    />
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
