import { Text } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { Properties } from "../api/types";
import { useCommunityMap } from "../data/queries";
import { displayName } from "../graph/GraphModel";
import { formatMetric, hasMetrics } from "../lib/metrics";
import classes from "./PropertyEditor.module.css";

/**
 * What graphos computed for a node, in plain words: its group (named after
 * the group's best-connected member) and its graph metrics. These are
 * written by graphos only, so they are shown here rather than among the
 * properties one edits.
 */
export function GraphMetrics({ properties }: { properties: Properties }) {
  const { t } = useTranslation();
  const map = useCommunityMap();
  const group = properties.community_id;
  if (!hasMetrics(properties) && group === undefined) return null;

  const hub = map.data?.communities.find((c) => c.community_id === group)?.hub;
  const degree = properties.graphos_degree;
  const component = properties.graphos_component;
  const rows: [string, string][] = [];
  if (group !== undefined) {
    rows.push([
      t("components.graphMetrics.group"),
      hub
        ? t("components.graphMetrics.groupNamed", { name: displayName(hub.properties, hub.id), id: String(group) })
        : String(group),
    ]);
  }
  if (properties.graphos_pagerank !== undefined) {
    rows.push([t("components.graphMetrics.pagerank"), formatMetric(properties.graphos_pagerank)]);
  }
  if (properties.graphos_betweenness !== undefined) {
    rows.push([t("components.graphMetrics.betweenness"), formatMetric(properties.graphos_betweenness)]);
  }
  if (degree !== undefined) {
    rows.push([
      t("components.graphMetrics.degree"),
      t("components.graphMetrics.degreeValue", {
        total: formatMetric(degree),
        in: formatMetric(properties.graphos_in_degree),
        out: formatMetric(properties.graphos_out_degree),
      }),
    ]);
  }
  if (component !== undefined) {
    rows.push([
      t("components.graphMetrics.component"),
      component === 0 ? t("components.graphMetrics.largest") : formatMetric(component),
    ]);
  }

  return (
    <div>
      <Text size="xs" c="dimmed" mb={6}>
        {t("components.graphMetrics.title")}
      </Text>
      <div className={classes.group}>
        {rows.map(([name, value]) => (
          <div key={name} className={classes.row}>
            <Text size="xs" c="dimmed" className={classes.name}>
              {name}
            </Text>
            <Text size="sm" truncate>
              {value}
            </Text>
          </div>
        ))}
      </div>
    </div>
  );
}
