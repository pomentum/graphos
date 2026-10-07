import { Badge, Group, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconArrowRight } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { useDeleteEdge } from "../data/queries";
import { useEdgeEditor } from "../data/useNodeEditor";
import { displayName, type GraphModel } from "../graph/GraphModel";
import { useTenantSession } from "../session/SessionProvider";
import { ConfirmDelete } from "./ConfirmDelete";
import classes from "./Inspector.module.css";
import { RecordForm } from "./RecordForm";

export interface EdgeInspectorProps {
  model: GraphModel;
  edge: string;
  /** Selects one of the edge's end nodes. */
  onSelectNode: (key: string) => void;
  onDeleted: () => void;
}

/** The selected edge: its two ends, its properties edited in place, and delete. */
export function EdgeInspector({ model, edge, onSelectNode, onDeleted }: EdgeInspectorProps) {
  const { t } = useTranslation();
  const { canWrite } = useTenantSession();
  const ref = model.edgeRef(edge);
  const [srcKey, dstKey] = model.graph.extremities(edge);
  const editor = useEdgeEditor(ref);
  const remove = useDeleteEdge();

  const end = (key: string) => {
    const node = model.graph.getNodeAttributes(key);
    return (
      <UnstyledButton onClick={() => onSelectNode(key)} title={`${node.vertexLabel} ${node.id}`}>
        <Text size="sm" fw={600} truncate maw={110} title={displayName(node.properties, node.id)}>
          {displayName(node.properties, node.id)}
        </Text>
      </UnstyledButton>
    );
  };

  /** Deletes in graphos, then from the canvas; a failure is shown by the query client. */
  const deleteEdge = async () => {
    try {
      await remove.mutateAsync({ ...ref, etag: editor.record?.etag });
    } catch {
      return;
    }
    model.forget(edge);
    onDeleted();
  };

  return (
    <Stack gap="md" className={classes.root}>
      <div>
        <Group gap={6} wrap="nowrap">
          {end(srcKey!)}
          <IconArrowRight size={14} className="gx-muted" aria-label={t("components.edgeInspector.to")} />
          {end(dstKey!)}
        </Group>
        <Group gap={6} mt={4}>
          <Badge variant="light" size="sm" radius="sm" tt="none">
            {ref.type}
          </Badge>
          {editor.record ? (
            <Text size="xs" c="dimmed">
              {t("common.revision", { rev: editor.record.rev })}
            </Text>
          ) : null}
        </Group>
      </div>
      <RecordForm
        editor={editor}
        editable={canWrite}
        onSaved={(saved) => model.updateEdge(edge, saved.properties)}
        actions={
          <ConfirmDelete
            question={t("components.edgeInspector.deleteQuestion")}
            confirmLabel={t("components.edgeInspector.deleteConfirm")}
            busy={remove.isPending}
            onConfirm={() => void deleteEdge()}
          />
        }
      />
    </Stack>
  );
}
