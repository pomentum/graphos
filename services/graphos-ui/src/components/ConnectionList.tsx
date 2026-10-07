import { ActionIcon, Group, Text, UnstyledButton } from "@mantine/core";
import { IconArrowLeft, IconArrowRight, IconX } from "@tabler/icons-react";
import { displayName, type GraphModel } from "../graph/GraphModel";
import classes from "./ConnectionList.module.css";

export interface ConnectionListProps {
  model: GraphModel;
  nodeKey: string;
  /** Shows a remove button per edge. */
  canRemove: boolean;
  onSelect: (key: string) => void;
  onRemove: (edgeKey: string) => void;
  /** At most this many rows. */
  max?: number;
}

/** The node's edges on the canvas: type, direction and the node at the other end. */
export function ConnectionList({ model, nodeKey, canRemove, onSelect, onRemove, max = 12 }: ConnectionListProps) {
  const edges = model.graph.edges(nodeKey).slice(0, max);
  if (edges.length === 0) {
    return (
      <Text size="xs" c="dimmed">
        None on the canvas yet. Double-click the node to add its neighbours.
      </Text>
    );
  }
  return (
    <div className={classes.group}>
      {edges.map((edge) => {
        const outgoing = model.graph.source(edge) === nodeKey;
        const other = model.graph.opposite(nodeKey, edge);
        const node = model.graph.getNodeAttributes(other);
        const Arrow = outgoing ? IconArrowRight : IconArrowLeft;
        return (
          <div key={edge} className={classes.row}>
            <UnstyledButton className={classes.link} onClick={() => onSelect(other)}>
              <Group gap={6} wrap="nowrap">
                <Arrow size={12} className="gx-muted" aria-label={outgoing ? "outgoing" : "incoming"} />
                <Text size="xs" c="dimmed">
                  {model.graph.getEdgeAttribute(edge, "edgeType")}
                </Text>
              </Group>
              <Text size="sm" truncate>
                {displayName(node.properties, node.id)}
              </Text>
            </UnstyledButton>
            {canRemove ? (
              <ActionIcon
                variant="subtle"
                color="gray"
                size="xs"
                aria-label="Remove edge"
                className={classes.remove}
                onClick={() => onRemove(edge)}
              >
                <IconX size={12} />
              </ActionIcon>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
