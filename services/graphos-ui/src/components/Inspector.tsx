import { Badge, Group, Stack, Text, UnstyledButton } from "@mantine/core";
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
}

/**
 * The selected node: its properties edited in place, its connections on the
 * canvas, and delete. Editing rules (versions, conflicts) live in
 * `useNodeEditor`; this component only presents them.
 */
export function Inspector({ model, selected, onSelect, onSelectEdge, onExpand }: InspectorProps) {
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
    model.removeNode(selected);
    onSelect(null);
  };

  return (
    <Stack gap="md" className={classes.root}>
      <div>
        <Text fw={600} size="md" truncate>
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
        <ConnectionList model={model} nodeKey={selected} onSelect={onSelect} onSelectEdge={onSelectEdge} />
      </div>
    </Stack>
  );
}
