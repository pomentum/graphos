import { Badge, Button, Group, Stack, Text, Tooltip } from "@mantine/core";
import { IconArrowsMaximize, IconArrowsMinimize, IconEyeOff } from "@tabler/icons-react";
import { useDeleteNode, useProperties } from "../data/queries";
import { useNodeEditor } from "../data/useNodeEditor";
import { displayName, type GraphModel } from "../graph/GraphModel";
import { useTenantSession } from "../session/SessionProvider";
import { ConfirmDelete } from "./ConfirmDelete";
import { ConnectionList } from "./ConnectionList";
import classes from "./Inspector.module.css";
import { RecordForm } from "./RecordForm";

export interface InspectorProps {
  model: GraphModel;
  selected: string;
  onSelect: (key: string | null) => void;
  /** Selects one of the node's edges. */
  onSelectEdge: (key: string) => void;
  /** Adds the node's neighbours to the canvas. */
  onExpand: (key: string) => void;
  /** Takes the node off the canvas (not out of graphos). */
  onHide: (key: string) => void;
}

/**
 * The selected node: what to do with it on the canvas (expand, collapse,
 * hide), its properties edited in place, its connections on the canvas, and
 * delete. Editing rules (versions, conflicts) live in `useNodeEditor`; this
 * component only presents them.
 */
export function Inspector({ model, selected, onSelect, onSelectEdge, onExpand, onHide }: InspectorProps) {
  const { canWrite } = useTenantSession();
  const attributes = model.graph.getNodeAttributes(selected);
  const ref = { id: attributes.id, label: attributes.vertexLabel };
  const editor = useNodeEditor(ref);
  const properties = useProperties(ref.label);
  const removeNode = useDeleteNode();

  /** Deletes in graphos, then from the canvas; a failure is shown by the query client. */
  const deleteNode = async () => {
    try {
      await removeNode.mutateAsync({ ...ref, etag: editor.record?.etag });
    } catch {
      return;
    }
    model.forget(selected);
    onSelect(null);
  };

  return (
    <Stack gap="md" className={classes.root}>
      <div>
        <Text fw={600} size="md" truncate title={ref.id}>
          {displayName(editor.record?.properties ?? attributes.properties, ref.id)}
        </Text>
        <Group gap={6} mt={4}>
          <Badge variant="light" size="sm" radius="sm" tt="none">
            {ref.label}
          </Badge>
          {editor.record ? (
            <Text size="xs" c="dimmed">
              Revision {editor.record.rev}
            </Text>
          ) : null}
        </Group>
        <Group gap={6} mt="sm">
          <Tooltip label="Add its neighbours, a page at a time (E)" withArrow>
            <Button
              size="compact-xs"
              variant="default"
              leftSection={<IconArrowsMaximize size={13} />}
              onClick={() => onExpand(selected)}
            >
              Expand
            </Button>
          </Tooltip>
          {model.canCollapse(selected) ? (
            <Tooltip label="Take back the neighbours it added (C)" withArrow>
              <Button
                size="compact-xs"
                variant="default"
                leftSection={<IconArrowsMinimize size={13} />}
                onClick={() => model.collapse(selected)}
              >
                Collapse
              </Button>
            </Tooltip>
          ) : null}
          <Tooltip label="Take it off the canvas; Back brings it back (H)" withArrow>
            <Button
              size="compact-xs"
              variant="default"
              leftSection={<IconEyeOff size={13} />}
              onClick={() => onHide(selected)}
            >
              Hide
            </Button>
          </Tooltip>
        </Group>
      </div>

      <RecordForm
        editor={editor}
        editable={canWrite}
        suggestions={properties.data}
        onSaved={(saved) => model.updateNode(selected, saved.properties)}
        actions={
          <ConfirmDelete
            question="Delete this node and every edge touching it?"
            confirmLabel="Delete node"
            busy={removeNode.isPending}
            onConfirm={() => void deleteNode()}
          />
        }
      />

      <div>
        <Text size="xs" c="dimmed" mb={6}>
          Connections on the canvas
        </Text>
        <ConnectionList model={model} nodeKey={selected} onSelect={onSelect} onSelectEdge={onSelectEdge} />
      </div>
    </Stack>
  );
}
