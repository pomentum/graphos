import { Group, Text } from "@mantine/core";
import type { Categories } from "../graph/canvasStyle";
import { formatCount } from "../lib/time";
import classes from "./CanvasLegend.module.css";

/** The colours on the canvas and what they stand for, most common first. */
export function CanvasLegend({ categories }: { categories: Categories }) {
  const { colorBy, ranked, colors, uncategorised } = categories;
  if (colorBy === "none") return null;
  const coloured = ranked.filter(([category]) => colors.has(category));
  const grey = ranked.length - coloured.length;
  return (
    <div className={classes.root}>
      {coloured.map(([category, count]) => (
        <Group key={category} gap={6} wrap="nowrap">
          <span className={classes.dot} style={{ background: colors.get(category) }} />
          <Text size="xs" truncate maw={140}>
            {colorBy === "group" ? `Group ${category}` : category}
          </Text>
          <Text size="xs" c="dimmed">
            {formatCount(count)}
          </Text>
        </Group>
      ))}
      {grey > 0 ? (
        <Text size="xs" c="dimmed">
          {grey} more {colorBy === "group" ? "groups" : "labels"} in grey
        </Text>
      ) : null}
      {uncategorised > 0 ? (
        <Text size="xs" c="dimmed">
          {formatCount(uncategorised)} not grouped
        </Text>
      ) : null}
    </div>
  );
}
