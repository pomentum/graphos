import { Group, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { Categories } from "../graph/canvasStyle";
import { LABEL_ICONS } from "../lib/labelIcons";
import { formatCount } from "../lib/time";
import type { LabelStyles } from "../theme/labelStyles";
import classes from "./CanvasLegend.module.css";

/** The colours on the canvas and what they stand for, most common first; a label's icon when it has one. */
export function CanvasLegend({ categories, styles }: { categories: Categories; styles?: LabelStyles }) {
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
            <Swatch
              color={colors.get(category)!}
              icon={colorBy === "label" ? styles?.vertex.get(category)?.icon : null}
            />
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

function Swatch({ color, icon }: { color: string; icon?: keyof typeof LABEL_ICONS | null }) {
  const Icon = icon ? LABEL_ICONS[icon] : null;
  return Icon ? (
    <Icon size={12} color={color} aria-hidden />
  ) : (
    <span className={classes.dot} style={{ background: color }} />
  );
}
