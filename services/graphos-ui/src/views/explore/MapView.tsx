import { Button, CloseButton, Group, Loader, Text } from "@mantine/core";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { GraphCanvas } from "../../components/GraphCanvas";
import { useGraphMap, useLabelStyles } from "../../data/queries";
import { buildMapModel, largestBubbles, mapCategories, mapEdgeDisplay, mapNodeDisplay } from "../../graph/graphMap";
import { useWorkspace, type ExploreMode } from "../../graph/WorkspaceProvider";
import { formatCount } from "../../lib/time";
import { usePalette } from "../../theme/usePalette";
import classes from "./ExploreView.module.css";
import { MapPanel } from "./MapPanel";
import { ModeSwitch } from "./ModeSwitch";
import { useOpenHubs } from "./useOpenHubs";

const MAP_STYLE = { node: mapNodeDisplay, edge: mapEdgeDisplay };
const NO_FOCUS = { nodes: new Set<string>(), edges: new Set<string>() };

/**
 * The map of the tenant: one bubble per label, sized by its node count, and
 * a line per pair of labels with edges between them, thicker with more
 * edges. Click a label to see what it links to; double-click (or "Start
 * from its best-connected nodes") to put its hubs on the graph canvas.
 */
export function MapView({ onMode }: { onMode: (mode: ExploreMode) => void }) {
  const { t } = useTranslation();
  const [exact, setExact] = useState(false);
  const map = useGraphMap(exact);
  const palette = usePalette();
  // Kept in the workspace: going to Browse and back keeps the label selected.
  const { mapLabel: selected, setMapLabel: setSelected } = useWorkspace();
  const { openHubs, opening } = useOpenHubs();

  const model = useMemo(() => (map.data ? buildMapModel(map.data) : null), [map.data]);
  const styles = useLabelStyles();
  const categories = useMemo(() => (map.data ? mapCategories(map.data, styles) : undefined), [map.data, styles]);
  const largest = useMemo(() => (model ? largestBubbles(model) : undefined), [model]);
  const emphasis = useMemo(
    () => ({
      selected,
      categories,
      focus: selected && model?.graph.hasNode(selected) ? model.neighborhood(selected) : NO_FOCUS,
      recent: new Set<string>(),
      hubs: largest,
    }),
    [selected, categories, model, largest],
  );
  const totals = map.data
    ? {
        labels: map.data.labels.length,
        nodes: map.data.labels.reduce((sum, l) => sum + l.count, 0),
        edges: map.data.links.reduce((sum, l) => sum + l.count, 0),
      }
    : null;

  return (
    <div className={classes.root} data-inspector={selected ? "open" : "closed"}>
      <section className={classes.canvas} aria-label={t("views.explore.map.region")}>
        {model ? (
          <GraphCanvas
            model={model}
            emphasis={emphasis}
            palette={palette}
            style={MAP_STYLE}
            onSelect={setSelected}
            onExpand={(label) => void openHubs(label)}
          />
        ) : (
          <div className={classes.empty}>
            {map.error ? (
              <Text size="sm" c="red">
                {map.error.message}
              </Text>
            ) : (
              <>
                <Loader size="sm" />
                <Text size="sm" c="dimmed">
                  {t("views.explore.map.counting")}
                </Text>
              </>
            )}
          </div>
        )}
        <Group gap={6} className={classes.actions}>
          <ModeSwitch mode="map" onChange={onMode} />
          {opening ? <Loader size={14} /> : null}
        </Group>
        {totals ? (
          <Group gap={8} className={classes.note} wrap="nowrap">
            <Text size="xs" c="dimmed">
              {t(map.data?.estimated ? "views.explore.map.totalsEstimated" : "views.explore.map.totals", {
                labels: t("common.counts.labels", { count: totals.labels, formatted: formatCount(totals.labels) }),
                nodes: t("common.counts.nodes", { count: totals.nodes, formatted: formatCount(totals.nodes) }),
                edges: t("common.counts.edges", { count: totals.edges, formatted: formatCount(totals.edges) }),
              })}
            </Text>
            {map.data?.estimated ? (
              <Button size="compact-xs" variant="subtle" loading={map.isFetching} onClick={() => setExact(true)}>
                {t("views.explore.map.countExactly")}
              </Button>
            ) : null}
          </Group>
        ) : null}
      </section>

      {selected && map.data ? (
        <aside className={classes.inspector} aria-label={t("views.explore.map.panel")}>
          <CloseButton
            className={classes.close}
            aria-label={t("common.actions.close")}
            onClick={() => setSelected(null)}
          />
          <MapPanel
            map={map.data}
            label={selected}
            opening={opening === selected}
            onOpen={(label) => void openHubs(label)}
            onSelectLabel={setSelected}
          />
        </aside>
      ) : null}
    </div>
  );
}
