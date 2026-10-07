import { Group, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { Categories } from "../graph/canvasStyle";
import { formatCount } from "../lib/time";
import classes from "./CanvasLegend.module.css";

/** The colours on the canvas and what they stand for, most common first. */
export function CanvasLegend({ categories }: { categories: Categories }) {
  const { t } = useTranslation();
  const { colorBy, ranked, colors, uncategorised, groupNames } = categories;
  if (colorBy === "none") return null;
  const coloured = ranked.filter(([category]) => colors.has(category));
  const grey = ranked.length - coloured.length;
  return (
    <div className={classes.root}>
      {colorBy === "group" ? (
        <Text size="xs" c="dimmed" title={t("components.canvasLegend.groupsHint")}>
          {t("components.canvasLegend.groupsHeading")}
        </Text>
      ) : null}
      {coloured.map(([category, count]) => {
        const group = groupNames?.get(category);
        const title = group
          ? t("components.canvasLegend.groupTitle", { title: group.title, category, detail: group.detail })
          : category;
        return (
          <Group key={category} gap={6} wrap="nowrap" title={title}>
            <span className={classes.dot} style={{ background: colors.get(category) }} />
            <div className={classes.name}>
              <Text size="xs" truncate>
                {group
                  ? group.title
                  : colorBy === "group"
                    ? t("components.canvasLegend.group", { category })
                    : category}
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
          {colorBy === "group"
            ? t("components.canvasLegend.moreGroups", { count: grey })
            : t("components.canvasLegend.moreLabels", { count: grey })}
        </Text>
      ) : null}
      {uncategorised > 0 ? (
        <Text size="xs" c="dimmed">
          {t("components.canvasLegend.notGrouped", { formatted: formatCount(uncategorised) })}
        </Text>
      ) : null}
    </div>
  );
}
