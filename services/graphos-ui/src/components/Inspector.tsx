import { ActionIcon, Badge, Button, Group, Menu, Stack, Text, Tooltip } from "@mantine/core";
import { IconArrowsMaximize, IconArrowsMinimize, IconChevronDown, IconEyeOff } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { useDeleteNode, useProperties } from "../data/queries";
import { useNodeEditor } from "../data/useNodeEditor";
import { displayName, type GraphModel } from "../graph/GraphModel";
import { useTenantSession } from "../session/SessionProvider";
import { ConfirmDelete } from "./ConfirmDelete";
import { CopyValue } from "./CopyValue";
import { GraphMetrics } from "./GraphMetrics";
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
  /** Adds every node within `hops` hops, and the relationships among them. */
  onExpandHops?: (key: string, hops: number) => void;
  /** Takes the node off the canvas (not out of graphos). */
  onHide: (key: string) => void;
}

/**
 * The selected node: what to do with it on the canvas (expand, collapse,
 * hide), its properties edited in place, its connections on the canvas, and
 * delete. Editing rules (versions, conflicts) live in `useNodeEditor`; this
 * component only presents them.
 */
export function Inspector({ model, selected, onSelect, onSelectEdge, onExpand, onExpandHops, onHide }: InspectorProps) {
  const { t } = useTranslation();
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
        <Group gap={4} wrap="nowrap">
          <Text fw={600} size="md" truncate title={ref.id}>
            {displayName(editor.record?.properties ?? attributes.properties, ref.id)}
          </Text>
          <CopyValue value={ref.id} label={t("components.copy.id")} />
        </Group>
        <Group gap={6} mt={4}>
          <Badge variant="light" size="sm" radius="sm" tt="none">
            {ref.label}
          </Badge>
          {editor.record ? (
            <Text size="xs" c="dimmed">
              {t("common.revision", { rev: editor.record.rev })}
            </Text>
          ) : null}
        </Group>
        <Group gap={6} mt="sm">
          <Tooltip label={t("components.inspector.expandTooltip")} withArrow>
            <Button
              size="compact-xs"
              variant="default"
              leftSection={<IconArrowsMaximize size={13} />}
              onClick={() => onExpand(selected)}
            >
              {t("components.inspector.expand")}
            </Button>
          </Tooltip>
          {onExpandHops ? (
            <Menu position="bottom-start" withinPortal>
              <Menu.Target>
                <ActionIcon variant="default" size={22} aria-label={t("components.inspector.expandHops")}>
                  <IconChevronDown size={12} />
                </ActionIcon>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Label>{t("components.inspector.expandHopsTooltip")}</Menu.Label>
                {[2, 3].map((hops) => (
                  <Menu.Item key={hops} onClick={() => onExpandHops(selected, hops)}>
                    {t("components.inspector.hops", { count: hops })}
                  </Menu.Item>
                ))}
              </Menu.Dropdown>
            </Menu>
          ) : null}
          {model.canCollapse(selected) ? (
            <Tooltip label={t("components.inspector.collapseTooltip")} withArrow>
              <Button
                size="compact-xs"
                variant="default"
                leftSection={<IconArrowsMinimize size={13} />}
                onClick={() => model.collapse(selected)}
              >
                {t("components.inspector.collapse")}
              </Button>
            </Tooltip>
          ) : null}
          <Tooltip label={t("components.inspector.hideTooltip")} withArrow>
            <Button
              size="compact-xs"
              variant="default"
              leftSection={<IconEyeOff size={13} />}
              onClick={() => onHide(selected)}
            >
              {t("components.inspector.hide")}
            </Button>
          </Tooltip>
        </Group>
      </div>

      <GraphMetrics properties={editor.record?.properties ?? attributes.properties} />

      <RecordForm
        editor={editor}
        editable={canWrite}
        suggestions={properties.data}
        onSaved={(saved) => model.updateNode(selected, saved.properties)}
        actions={
          <ConfirmDelete
            question={t("components.inspector.deleteQuestion")}
            confirmLabel={t("components.inspector.deleteConfirm")}
            busy={removeNode.isPending}
            onConfirm={() => void deleteNode()}
          />
        }
      />

      <div>
        <Text size="xs" c="dimmed" mb={6}>
          {t("components.inspector.relationships")}
        </Text>
        <ConnectionList model={model} nodeKey={selected} onSelect={onSelect} onSelectEdge={onSelectEdge} />
      </div>
    </Stack>
  );
}
