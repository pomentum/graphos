import { Button, Group, Loader, Text } from "@mantine/core";
import { useMemo, useState } from "react";
import { GraphCanvas } from "../../components/GraphCanvas";
import { useGraphMap } from "../../data/queries";
import { buildMapModel, mapCategories, mapEdgeDisplay, mapNodeDisplay } from "../../graph/graphMap";
import type { ExploreMode } from "../../graph/WorkspaceProvider";
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
  const [exact, setExact] = useState(false);
  const map = useGraphMap(exact);
  const palette = usePalette();
  const [selected, setSelected] = useState<string | null>(null);
  const { openHubs, opening } = useOpenHubs();

  const model = useMemo(() => (map.data ? buildMapModel(map.data) : null), [map.data]);
  const categories = useMemo(() => (map.data ? mapCategories(map.data) : undefined), [map.data]);
  const emphasis = useMemo(
    () => ({
      selected,
      categories,
      focus: selected && model?.graph.hasNode(selected) ? model.neighborhood(selected) : NO_FOCUS,
      recent: new Set<string>(),
    }),
    [selected, categories, model],
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
      <section className={classes.canvas} aria-label="Map of labels">
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
                  Counting labels and the edges between them…
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
              {formatCount(totals.labels)} labels · {formatCount(totals.nodes)} nodes ·{" "}
              {map.data?.estimated ? "≈ " : ""}
              {formatCount(totals.edges)} edges
              {map.data?.estimated ? " (large tables sampled)" : ""}
            </Text>
            {map.data?.estimated ? (
              <Button size="compact-xs" variant="subtle" loading={map.isFetching} onClick={() => setExact(true)}>
                Count exactly
              </Button>
            ) : null}
          </Group>
        ) : null}
      </section>

      {selected && map.data ? (
        <aside className={classes.inspector} aria-label="Label">
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
