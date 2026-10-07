import { Group, Text, UnstyledButton } from "@mantine/core";
import { IconArrowLeft, IconArrowRight } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { displayName, type GraphModel } from "../graph/GraphModel";
import classes from "./ConnectionList.module.css";

export interface ConnectionListProps {
  model: GraphModel;
  nodeKey: string;
  /** Selects the node at the other end. */
  onSelect: (key: string) => void;
  /** Selects the edge itself, to see or edit its properties. */
  onSelectEdge: (edgeKey: string) => void;
  /** At most this many rows. */
  max?: number;
}

/**
 * The node's edges on the canvas: type and direction (opens the edge) and the
 * node at the other end (selects it). Deleting is done from the edge itself,
 * where it asks first.
 */
export function ConnectionList({ model, nodeKey, onSelect, onSelectEdge, max = 12 }: ConnectionListProps) {
  const { t } = useTranslation();
  const all = model.graph.edges(nodeKey).filter((edge) => !model.graph.getEdgeAttribute(edge, "more"));
  const edges = all.slice(0, max);
  if (edges.length === 0) {
    return (
      <Text size="xs" c="dimmed">
        {t("components.connectionList.empty")}
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
            <UnstyledButton
              className={classes.edge}
              onClick={() => onSelectEdge(edge)}
              title={t("components.connectionList.showEdge")}
            >
              <Group gap={6} wrap="nowrap">
                <Arrow
                  size={12}
                  className="gx-muted"
                  aria-label={
                    outgoing ? t("components.connectionList.outgoing") : t("components.connectionList.incoming")
                  }
                />
                <Text size="xs" c="dimmed">
                  {model.graph.getEdgeAttribute(edge, "edgeType")}
                </Text>
              </Group>
            </UnstyledButton>
            <UnstyledButton className={classes.link} onClick={() => onSelect(other)} title={node.id}>
              <Text size="sm" truncate>
                {displayName(node.properties, node.id)}
              </Text>
            </UnstyledButton>
          </div>
        );
      })}
      {all.length > edges.length ? (
        <Text size="xs" c="dimmed" className={classes.row}>
          {t("components.connectionList.more", { count: all.length - edges.length })}
        </Text>
      ) : null}
    </div>
  );
}
