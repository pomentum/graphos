import { Alert, Badge, Button, Group, Popover, Skeleton, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconTrash } from "@tabler/icons-react";
import { useDeleteEdge, useDeleteNode, useProperties } from "../data/queries";
import { useNodeEditor } from "../data/useNodeEditor";
import { displayName, type GraphModel } from "../graph/GraphModel";
import { useTenantSession } from "../session/SessionProvider";
import { ConnectionList } from "./ConnectionList";
import classes from "./Inspector.module.css";
import { PropertyEditor } from "./PropertyEditor";

export interface InspectorProps {
  model: GraphModel;
  selected: string;
  onSelect: (key: string | null) => void;
  /** Adds the node's neighbours to the canvas. */
  onExpand: (key: string) => void;
}

/**
 * The selected node: its properties edited in place, its connections on the
 * canvas, and delete. Editing rules (versions, conflicts) live in
 * `useNodeEditor`; this component only presents them.
 */
export function Inspector({ model, selected, onSelect, onExpand }: InspectorProps) {
  const { canWrite } = useTenantSession();
  const attributes = model.graph.getNodeAttributes(selected);
  const ref = { id: attributes.id, label: attributes.vertexLabel };
  const editor = useNodeEditor(ref);
  const properties = useProperties(ref.label);
  const removeNode = useDeleteNode();
  const removeEdge = useDeleteEdge();

  const save = async () => {
    const saved = await editor.save();
    if (saved) model.updateNode(selected, saved.properties);
  };

  const deleteNode = async () => {
    await removeNode.mutateAsync({ ...ref, etag: editor.node?.etag });
    model.removeNode(selected);
    onSelect(null);
  };

  /** Removes one edge in graphos, then from the canvas. */
  const deleteEdge = async (edge: string) => {
    const [src, dst] = model.graph.extremities(edge).map((key) => model.graph.getNodeAttributes(key));
    if (!src || !dst) return;
    await removeEdge.mutateAsync({
      type: model.graph.getEdgeAttribute(edge, "edgeType"),
      srcId: src.id,
      srcLabel: src.vertexLabel,
      dstId: dst.id,
      dstLabel: dst.vertexLabel,
    });
    model.removeEdge(edge);
  };

  return (
    <Stack gap="md" className={classes.root}>
      <div>
        <Text fw={600} size="md" truncate>
          {displayName(editor.node?.properties ?? attributes.properties, ref.id)}
        </Text>
        <Group gap={6} mt={4}>
          <Badge variant="light" size="sm" radius="sm" tt="none">
            {ref.label}
          </Badge>
          {editor.node ? (
            <Text size="xs" c="dimmed">
              Revision {editor.node.rev}
            </Text>
          ) : null}
        </Group>
      </div>

      {editor.stale ? (
        <Alert color="orange" variant="light" radius="md" title={`${editor.changedBy ?? "Someone"} changed this node`}>
          <Text size="xs">Your edits are kept. Saving applies only the fields you changed.</Text>
          <Group gap="xs" mt="xs">
            <Button size="compact-xs" variant="default" onClick={editor.discard}>
              Discard mine
            </Button>
            <Button size="compact-xs" loading={editor.saving} onClick={() => void save()}>
              Save mine
            </Button>
          </Group>
        </Alert>
      ) : null}

      {editor.isLoading ? (
        <Skeleton h={120} radius="lg" />
      ) : (
        <PropertyEditor
          draft={editor.values}
          editable={canWrite}
          onChange={editor.change}
          suggestions={properties.data}
        />
      )}
      {editor.error ? (
        <Text size="xs" c="red">
          {editor.error}
        </Text>
      ) : null}

      {canWrite ? (
        <Group justify="space-between">
          <Popover position="bottom-start" withArrow>
            <Popover.Target>
              <Button size="xs" variant="subtle" color="red" leftSection={<IconTrash size={14} />}>
                Delete
              </Button>
            </Popover.Target>
            <Popover.Dropdown>
              <Text size="xs" mb="xs">
                Delete this node and every edge touching it?
              </Text>
              <Button size="compact-xs" color="red" loading={removeNode.isPending} onClick={() => void deleteNode()}>
                Delete node
              </Button>
            </Popover.Dropdown>
          </Popover>
          {editor.dirty && !editor.stale ? (
            <Group gap="xs">
              <Button size="xs" variant="default" onClick={editor.discard}>
                Revert
              </Button>
              <Button size="xs" loading={editor.saving} onClick={() => void save()}>
                Save
              </Button>
            </Group>
          ) : null}
        </Group>
      ) : null}

      <div>
        <Group justify="space-between" mb={6}>
          <Text size="xs" c="dimmed">
            Connections
          </Text>
          <UnstyledButton onClick={() => onExpand(selected)}>
            <Text size="xs" c="var(--gx-accent-text)">
              Show neighbours
            </Text>
          </UnstyledButton>
        </Group>
        <ConnectionList
          model={model}
          nodeKey={selected}
          canRemove={canWrite}
          onSelect={onSelect}
          onRemove={(edge) => void deleteEdge(edge)}
        />
      </div>
    </Stack>
  );
}
