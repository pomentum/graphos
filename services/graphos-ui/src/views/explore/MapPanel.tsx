import { Badge, Button, Group, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconTable, IconTargetArrow } from "@tabler/icons-react";
import { Link } from "react-router-dom";
import type { GraphMap } from "../../api/types";
import { linksOf } from "../../graph/graphMap";
import { formatCount } from "../../lib/time";
import classes from "./MapPanel.module.css";

/** Links listed per direction; the rest are summarised. */
const SHOWN_LINKS = 12;

export interface MapPanelProps {
  map: GraphMap;
  label: string;
  /** Opening the label's hubs is under way. */
  opening: boolean;
  onOpen: (label: string) => void;
  onSelectLabel: (label: string) => void;
}

/**
 * The selected label on the map: how many nodes it has, what it links to
 * and from (with edge counts), and the ways in: its best-connected nodes on
 * the canvas, or its nodes in Browse.
 */
export function MapPanel({ map, label, opening, onOpen, onSelectLabel }: MapPanelProps) {
  const count = map.labels.find((l) => l.label === label)?.count ?? 0;
  const { outgoing, incoming } = linksOf(map, label);
  const approx = map.estimated ? "≈ " : "";

  return (
    <Stack gap="md" className={classes.root}>
      <div>
        <Text fw={600} size="md" truncate title={label}>
          {label}
        </Text>
        <Group gap={6} mt={4}>
          <Badge variant="light" size="sm" radius="sm" tt="none">
            {formatCount(count)} nodes
          </Badge>
        </Group>
        <Stack gap={6} mt="sm">
          <Button size="xs" leftSection={<IconTargetArrow size={14} />} loading={opening} onClick={() => onOpen(label)}>
            Start from its best-connected nodes
          </Button>
          <Button
            size="xs"
            variant="default"
            leftSection={<IconTable size={14} />}
            component={Link}
            to={`/browse?label=${encodeURIComponent(label)}`}
          >
            Browse its nodes
          </Button>
        </Stack>
      </div>

      <LinkList title="Relationships to" rows={outgoing} approx={approx} arrow="→" onSelectLabel={onSelectLabel} />
      <LinkList title="Relationships from" rows={incoming} approx={approx} arrow="←" onSelectLabel={onSelectLabel} />
    </Stack>
  );
}

function LinkList({
  title,
  rows,
  approx,
  arrow,
  onSelectLabel,
}: {
  title: string;
  rows: { label: string; type: string; count: number }[];
  approx: string;
  arrow: string;
  onSelectLabel: (label: string) => void;
}) {
  if (rows.length === 0) return null;
  return (
    <div>
      <Text size="xs" c="dimmed" mb={6}>
        {title}
      </Text>
      <div className={classes.group}>
        {rows.slice(0, SHOWN_LINKS).map((row) => (
          <UnstyledButton
            key={`${row.type}|${row.label}`}
            className={classes.row}
            onClick={() => onSelectLabel(row.label)}
          >
            <Text size="xs" c="dimmed" className={classes.type}>
              {row.type} {arrow}
            </Text>
            <Text size="sm" truncate className={classes.label} title={row.label}>
              {row.label}
            </Text>
            <Text size="xs" c="dimmed">
              {approx}
              {formatCount(row.count)}
            </Text>
          </UnstyledButton>
        ))}
        {rows.length > SHOWN_LINKS ? (
          <Text size="xs" c="dimmed" className={classes.row}>
            and {rows.length - SHOWN_LINKS} more
          </Text>
        ) : null}
      </div>
    </div>
  );
}
