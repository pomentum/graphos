import { Button, Group, Loader, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { Overview } from "../../graph/WorkspaceProvider";
import { searchShortcut } from "../../lib/platform";
import { formatCount } from "../../lib/time";
import { WHOLE_GRAPH_LIMIT } from "./useWholeGraph";
import classes from "./ExploreView.module.css";

/**
 * What the canvas says about itself: loading, empty (with a way back to the
 * whole graph), or that it shows a sample of a larger graph.
 */
export function CanvasStatus({
  overview,
  isEmpty,
  onLoad,
  sample = null,
}: {
  overview: Overview;
  isEmpty: boolean;
  onLoad: () => void;
  /** A sample to offer on an empty canvas when the whole graph is too big: the busiest label's hubs. */
  sample?: { label: string; busy: boolean; open: () => void } | null;
}) {
  const tooBig = Boolean(overview.total && overview.total > WHOLE_GRAPH_LIMIT);
  const { t } = useTranslation();
  if (overview.status === "loading") {
    return (
      <div className={classes.empty}>
        <Loader size="sm" />
        <Text size="sm" c="dimmed">
          {t("views.explore.status.loading")}
        </Text>
      </div>
    );
  }
  if (isEmpty) {
    return (
      <div className={classes.empty}>
        <Text fw={600}>
          {overview.status === "failed" ? t("views.explore.status.failed") : t("views.explore.status.empty")}
        </Text>
        <Text size="sm" c="dimmed">
          {t("views.explore.status.emptyHint", { shortcut: searchShortcut() })}
        </Text>
        <Group gap="xs" mt="sm" justify="center">
          {tooBig && sample ? (
            <Button
              size="xs"
              variant="default"
              className={classes.emptyAction}
              loading={sample.busy}
              onClick={sample.open}
            >
              {t("views.explore.status.showSample", { label: sample.label })}
            </Button>
          ) : null}
          <Button size="xs" variant="default" className={classes.emptyAction} onClick={onLoad}>
            {tooBig ? t("views.explore.status.openMap") : t("views.explore.status.showWhole")}
          </Button>
        </Group>
      </div>
    );
  }
  if (overview.status === "shown" && overview.total && overview.shown && overview.total > overview.shown) {
    return (
      <Text size="xs" className={classes.note}>
        {t("views.explore.status.sample", {
          count: overview.total,
          shown: formatCount(overview.shown),
          total: formatCount(overview.total),
        })}
      </Text>
    );
  }
  return null;
}
