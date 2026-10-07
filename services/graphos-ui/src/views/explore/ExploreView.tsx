import { Button, Group, Loader, Text } from "@mantine/core";
import { IconCirclePlus, IconLink, IconTrashX } from "@tabler/icons-react";
import { useMemo, useState } from "react";
import { ApiError } from "../../api/http";
import type { SearchHit } from "../../api/types";
import { ExpandDialog } from "../../components/ExpandDialog";
import { GraphCanvas } from "../../components/GraphCanvas";
import { Inspector } from "../../components/Inspector";
import { NewEdgeDialog } from "../../components/NewEdgeDialog";
import { NewNodeDialog } from "../../components/NewNodeDialog";
import { useCreateEdge, useCreateNode, useSchema } from "../../data/queries";
import { displayName } from "../../graph/GraphModel";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { useTenantSession } from "../../session/SessionProvider";
import { usePalette } from "../../theme/usePalette";
import classes from "./ExploreView.module.css";
import { formatCount } from "../../lib/time";
import { searchShortcut } from "../../lib/platform";
import { useExpandNode } from "./useExpandNode";
import { useWholeGraph } from "./useWholeGraph";
import { useRecentlyChanged } from "./useRecentlyChanged";

/**
 * Explore: the canvas of nodes found by search and expanded by
 * double-click, with the inspector for the selected node.
 */
export function ExploreView() {
  const { canWrite } = useTenantSession();
  const { model, version, selected, select, open, overview, setOverview } = useWorkspace();
  const { load } = useWholeGraph();
  const palette = usePalette();
  const schema = useSchema();
  const recent = useRecentlyChanged(model);
  const { expand, dialog, isExpanding } = useExpandNode(model);
  const [creating, setCreating] = useState<"node" | "edge" | null>(null);
  const createNode = useCreateNode();
  const createEdge = useCreateEdge();

  const emphasis = useMemo(
    () => ({
      selected,
      focus: selected ? model.neighborhood(selected) : { nodes: new Set<string>(), edges: new Set<string>() },
      recent,
    }),
    // `version` changes with the model, so the neighbourhood is recomputed.
    [model, selected, recent, version],
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

  return (
    <div className={classes.root} data-inspector={selected ? "open" : "closed"}>
      <section className={classes.canvas} aria-label="Graph">
        <GraphCanvas
          model={model}
          emphasis={emphasis}
          palette={palette}
          onSelect={select}
          onExpand={(key) => void expand(key)}
        />
        {overview.status === "loading" ? (
          <div className={classes.empty}>
            <Loader size="sm" />
            <Text size="sm" c="dimmed">
              Loading the graph…
            </Text>
          </div>
        ) : model.isEmpty ? (
          <div className={classes.empty}>
            <Text fw={600}>{overview.status === "failed" ? "Couldn't load the graph" : "The canvas is empty"}</Text>
            <Text size="sm" c="dimmed">
              Find a node with search ({searchShortcut()}), then double-click it to see its neighbours.
            </Text>
            <Button size="xs" variant="default" mt="sm" className={classes.emptyAction} onClick={() => void load()}>
              Show the whole graph
            </Button>
          </div>
        ) : null}
        {overview.status === "shown" && overview.total && overview.shown && overview.total > overview.shown ? (
          <Text size="xs" className={classes.note}>
            A sample of {formatCount(overview.shown)} of {formatCount(overview.total)} nodes. Search to focus on one.
          </Text>
        ) : null}
        <Group gap={6} className={classes.actions}>
          {canWrite ? (
            <>
              <Button
                size="compact-xs"
                variant="default"
                leftSection={<IconCirclePlus size={14} />}
                onClick={() => setCreating("node")}
              >
                Node
              </Button>
              {selected ? (
                <Button
                  size="compact-xs"
                  variant="default"
                  leftSection={<IconLink size={14} />}
                  onClick={() => setCreating("edge")}
                >
                  Connect
                </Button>
              ) : null}
            </>
          ) : null}
          {!model.isEmpty ? (
            <Button
              size="compact-xs"
              variant="subtle"
              color="gray"
              leftSection={<IconTrashX size={14} />}
              onClick={() => {
                model.clear();
                setOverview({ status: "dismissed" });
              }}
            >
              Clear
            </Button>
          ) : null}
          {isExpanding ? <Loader size={14} /> : null}
        </Group>
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
