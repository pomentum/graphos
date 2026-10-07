import { Button, Loader, Text } from "@mantine/core";
import type { Overview } from "../../graph/WorkspaceProvider";
import { searchShortcut } from "../../lib/platform";
import { formatCount } from "../../lib/time";
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
  if (overview.status === "loading") {
    return (
      <div className={classes.empty}>
        <Loader size="sm" />
        <Text size="sm" c="dimmed">
          Loading the graph…
        </Text>
      </div>
    );
  }
  if (isEmpty) {
    return (
      <div className={classes.empty}>
        <Text fw={600}>{overview.status === "failed" ? "Couldn't load the graph" : "The canvas is empty"}</Text>
        <Text size="sm" c="dimmed">
          Find a node with search ({searchShortcut()}), then double-click it to see its neighbours.
        </Text>
        <Button size="xs" variant="default" mt="sm" className={classes.emptyAction} onClick={onLoad}>
          Show the whole graph
        </Button>
      </div>
    );
  }
  if (overview.status === "shown" && overview.total && overview.shown && overview.total > overview.shown) {
    return (
      <Text size="xs" className={classes.note}>
        A sample of {formatCount(overview.shown)} of {formatCount(overview.total)} nodes. Search to focus on one.
      </Text>
    );
  }
  return null;
}
