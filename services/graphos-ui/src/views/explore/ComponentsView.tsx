import { Group, Loader, Select, Text } from "@mantine/core";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { COMPONENT_LIMIT, useComponent, useComponents } from "../../data/metricViews";
import type { ExploreMode } from "../../graph/WorkspaceProvider";
import type { Lens } from "../../lib/metrics";
import classes from "./ExploreView.module.css";
import { LensSwitch } from "./LensSwitch";
import { ModeSwitch } from "./ModeSwitch";
import { NotComputed, ScoreCanvas } from "./ScoreView";

/**
 * The connected components of the last metrics run: the islands of the
 * graph, largest first. One is drawn at a time; the small ones are often
 * what to look at (orphans, a load that failed to link up). A component
 * larger than `COMPONENT_LIMIT` is drawn by its best-connected nodes.
 */
export function ComponentsView({
  lens,
  onLens,
  onMode,
}: {
  lens: Lens;
  onLens: (lens: Lens) => void;
  onMode: (mode: ExploreMode) => void;
}) {
  const { t } = useTranslation();
  const components = useComponents();
  const list = components.data ?? [];
  const [picked, setPicked] = useState<number | null>(null);
  // The first small island when there is one, else the largest.
  const chosen = picked ?? (list.length > 1 ? list[1]!.id : (list[0]?.id ?? null));
  const component = useComponent(chosen);

  const actions = (exportMenu?: ReactNode) => (
    <Group gap={6} className={classes.actions}>
      <ModeSwitch mode="groups" onChange={onMode} />
      <LensSwitch lens={lens} onChange={onLens} />
      {list.length > 0 ? (
        <Select
          size="xs"
          w={240}
          aria-label={t("views.explore.lens.componentLabel")}
          data={list.map((c, i) => ({
            value: String(c.id),
            label:
              i === 0
                ? t("views.explore.lens.componentLargest", { count: c.size })
                : t("views.explore.lens.componentOption", { id: c.id, count: c.size }),
          }))}
          value={chosen === null ? null : String(chosen)}
          onChange={(value) => value !== null && setPicked(Number(value))}
          allowDeselect={false}
          searchable
        />
      ) : null}
      {exportMenu}
    </Group>
  );

  if (!component.data || component.data.nodes.length === 0) {
    const loading = components.isLoading || component.isLoading;
    return (
      <div className={classes.root} data-inspector="closed">
        <section className={classes.canvas}>
          <div className={classes.empty}>
            {loading ? <Loader size="sm" /> : list.length === 0 ? <NotComputed /> : null}
            {components.error ? (
              <Text size="sm" c="red">
                {components.error.message}
              </Text>
            ) : null}
          </div>
          {actions()}
        </section>
      </div>
    );
  }
  const size = list.find((c) => c.id === chosen)?.size ?? component.data.nodes.length;
  return (
    <ScoreCanvas
      data={component.data}
      property="graphos_degree"
      view="components"
      actions={actions}
      note={t("views.explore.lens.componentNote", {
        shown: component.data.nodes.length,
        count: size,
        islands: list.length,
        limit: COMPONENT_LIMIT,
      })}
    />
  );
}
