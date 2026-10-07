import { Group, Text } from "@mantine/core";
import type { Categories } from "../graph/canvasStyle";
import { formatCount } from "../lib/time";
import classes from "./CanvasLegend.module.css";

/** The colours on the canvas and what they stand for, most common first. */
export function CanvasLegend({ categories }: { categories: Categories }) {
  const { colorBy, ranked, colors, uncategorised, groupNames } = categories;
  if (colorBy === "none") return null;
  const coloured = ranked.filter(([category]) => colors.has(category));
  const grey = ranked.length - coloured.length;
  return (
    <div className={classes.root}>
      {coloured.map(([category, count]) => {
        const group = groupNames?.get(category);
        const title = group ? `${group.title}, group ${category} (${group.detail})` : category;
        return (
          <Group key={category} gap={6} wrap="nowrap" title={title}>
            <span className={classes.dot} style={{ background: colors.get(category) }} />
            <div className={classes.name}>
              <Text size="xs" truncate>
                {group ? group.title : colorBy === "group" ? `Group ${category}` : category}
              </Text>
              {group ? (
                <Text size="xs" c="dimmed" truncate>
                  {group.detail}
                </Text>
              ) : null}
            </div>
            <Text size="xs" c="dimmed">
              {formatCount(count)}
            </Text>
          </Group>
        );
      })}
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
