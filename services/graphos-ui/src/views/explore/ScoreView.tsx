import { Badge, Button, Group, Loader, Select, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconArrowRight } from "@tabler/icons-react";
import { useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import type Sigma from "sigma";
import { CanvasExportMenu } from "../../components/CanvasExportMenu";
import { GraphCanvas } from "../../components/GraphCanvas";
import { useTopByMetric, type MetricGraph } from "../../data/metricViews";
import { displayName, nodeKey, type EdgeAttributes, type NodeAttributes } from "../../graph/GraphModel";
import { buildScoreModel, rankHeat, SCORE_LABELS, scoreEdgeDisplay, scoreNodeDisplay } from "../../graph/scoreGraph";
import { useWorkspace, type ExploreMode } from "../../graph/WorkspaceProvider";
import { formatMetric, type Lens } from "../../lib/metrics";
import { useTenantSession } from "../../session/SessionProvider";
import { usePalette } from "../../theme/usePalette";
import classes from "./ExploreView.module.css";
import { LensSwitch } from "./LensSwitch";
import { ModeSwitch } from "./ModeSwitch";

const SCORE_STYLE = { node: scoreNodeDisplay, edge: scoreEdgeDisplay };
const COUNTS = [25, 50, 100, 200];

/**
 * The tenant through one graph metric: its top nodes by the score, across
 * every label, and the relationships among them, sized and coloured by the
 * score (hottest first), with the ranking beside the canvas. A click selects
 * a node; double-click, or "Open in Graph", takes it to the graph canvas to
 * expand. The scores are those of the last metrics run (Overview).
 */
export function ScoreView({
  property,
  lens,
  onLens,
  onMode,
}: {
  property: "graphos_pagerank" | "graphos_betweenness" | "graphos_degree";
  lens: Lens;
  onLens: (lens: Lens) => void;
  onMode: (mode: ExploreMode) => void;
}) {
  const { t } = useTranslation();
  const [count, setCount] = useState(50);
  const top = useTopByMetric(property, count);
  const actions = (exportMenu?: ReactNode) => (
    <Group gap={6} className={classes.actions}>
      <ModeSwitch mode="groups" onChange={onMode} />
      <LensSwitch lens={lens} onChange={onLens} />
      <Select
        size="xs"
        w={120}
        aria-label={t("views.explore.lens.countLabel")}
        data={COUNTS.map((n) => ({ value: String(n), label: t("views.explore.lens.top", { count: n }) }))}
        value={String(count)}
        onChange={(value) => value && setCount(Number(value))}
        allowDeselect={false}
      />
      {exportMenu}
    </Group>
  );
  if (!top.data || top.data.nodes.length === 0) {
    return (
      <div className={classes.root} data-inspector="closed">
        <section className={classes.canvas}>
          <div className={classes.empty}>
            {top.isLoading || !top.data ? (
              top.error ? (
                <Text size="sm" c="red">
                  {top.error.message}
                </Text>
              ) : (
                <Loader size="sm" />
              )
            ) : (
              <NotComputed />
            )}
          </div>
          {actions()}
        </section>
      </div>
    );
  }
  return <ScoreCanvas data={top.data} property={property} view={lens} actions={actions} />;
}

/** Shown when no node has the metric: it has not been computed (or not over these relationships). */
export function NotComputed() {
  const { t } = useTranslation();
  return (
    <Stack gap={6} align="center" maw={440} className={classes.emptyAction}>
      <Text fw={600}>{t("views.explore.lens.notComputedTitle")}</Text>
      <Text size="sm" c="dimmed" ta="center">
        {t("views.explore.lens.notComputedBody")}
      </Text>
      <Button component={Link} to="/overview" size="xs" variant="default" rightSection={<IconArrowRight size={14} />}>
        {t("views.explore.lens.toOverview")}
      </Button>
    </Stack>
  );
}

/** The canvas and the ranking of a metric view (also used for one component). */
export function ScoreCanvas({
  data,
  property,
  view,
  actions,
  note,
}: {
  data: MetricGraph;
  property: string;
  /** The view's name in exported files. */
  view: string;
  /** The toolbar, given the canvas's export menu. */
  actions: (exportMenu: ReactNode) => ReactNode;
  note?: string;
}) {
  const { t } = useTranslation();
  const workspace = useWorkspace();
  const palette = usePalette();
  const { tenant } = useTenantSession();
  const renderer = useRef<Sigma<NodeAttributes, EdgeAttributes> | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const model = useMemo(() => buildScoreModel(data.nodes, data.edges, property), [data, property]);
  const keys = useMemo(() => data.nodes.map((n) => nodeKey({ id: n.id, label: n.label })), [data]);
  const emphasis = useMemo(
    () => ({
      selected,
      categories: rankHeat(keys),
      focus:
        selected && model.graph.hasNode(selected)
          ? model.neighborhood(selected)
          : { nodes: new Set<string>(), edges: new Set<string>() },
      recent: new Set<string>(),
      hubs: new Set(keys.slice(0, SCORE_LABELS)),
    }),
    [selected, keys, model],
  );
  const open = (key: string) => {
    const node = model.graph.getNodeAttributes(key);
    workspace.open({ id: node.id, label: node.vertexLabel }, node.properties);
  };

  return (
    <div className={classes.root} data-inspector="open">
      <section className={classes.canvas}>
        <GraphCanvas
          model={model}
          emphasis={emphasis}
          palette={palette}
          style={SCORE_STYLE}
          onSelect={setSelected}
          onExpand={open}
          rendererRef={renderer}
        />
        {actions(<CanvasExportMenu renderer={renderer} graph={model.graph} palette={palette} name={[tenant, view]} />)}
        <Text size="xs" c="dimmed" className={classes.note}>
          {note ??
            t("views.explore.lens.totals", {
              nodes: data.nodes.length,
              edges: model.graph.size,
            })}
          {data.truncated ? ` ${t("views.explore.lens.truncated")}` : ""}
        </Text>
      </section>
      <aside className={classes.inspector} aria-label={t("views.explore.lens.ranking")}>
        <Stack gap={6} p="md">
          {selected && model.graph.hasNode(selected) ? (
            <Group justify="space-between" wrap="nowrap" gap="xs">
              <Text size="sm" fw={600} truncate>
                {displayName(
                  model.graph.getNodeAttributes(selected).properties,
                  model.graph.getNodeAttributes(selected).id,
                )}
              </Text>
              <Button
                size="compact-xs"
                variant="default"
                rightSection={<IconArrowRight size={13} />}
                onClick={() => open(selected)}
              >
                {t("views.explore.lens.openInGraph")}
              </Button>
            </Group>
          ) : (
            <Text size="xs" c="dimmed">
              {t("views.explore.lens.rankingHint")}
            </Text>
          )}
          <div>
            {data.nodes.map((node, i) => {
              const key = keys[i]!;
              return (
                <UnstyledButton
                  key={key}
                  className={classes.rankRow}
                  data-selected={key === selected || undefined}
                  onClick={() => setSelected(key)}
                  onDoubleClick={() => open(key)}
                >
                  <Text size="xs" c="dimmed" w={28} ta="right">
                    {i + 1}
                  </Text>
                  <Text size="sm" truncate style={{ flex: 1 }}>
                    {displayName(node.properties, node.id)}
                  </Text>
                  <Badge size="xs" variant="light" radius="sm" tt="none">
                    {node.label}
                  </Badge>
                  <Text size="xs" className="gx-mono" w={64} ta="right">
                    {formatMetric(node.properties[property])}
                  </Text>
                </UnstyledButton>
              );
            })}
          </div>
        </Stack>
      </aside>
    </div>
  );
}
