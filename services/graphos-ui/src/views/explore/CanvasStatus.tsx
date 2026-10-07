import { Button, Loader, Text } from "@mantine/core";
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
}: {
  overview: Overview;
  isEmpty: boolean;
  onLoad: () => void;
}) {
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
        <Button size="xs" variant="default" mt="sm" className={classes.emptyAction} onClick={onLoad}>
          {overview.total && overview.total > WHOLE_GRAPH_LIMIT
            ? t("views.explore.status.openMap")
            : t("views.explore.status.showWhole")}
        </Button>
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
