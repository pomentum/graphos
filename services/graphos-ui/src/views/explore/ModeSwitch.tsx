import { SegmentedControl } from "@mantine/core";
import type { ExploreMode } from "../../graph/WorkspaceProvider";

/** Switches Explore between the map of labels and the graph canvas; both keep their state. */
export function ModeSwitch({ mode, onChange }: { mode: ExploreMode; onChange: (mode: ExploreMode) => void }) {
  return (
    <SegmentedControl
      size="xs"
      radius="md"
      value={mode}
      onChange={(value) => onChange(value as ExploreMode)}
      data={[
        { value: "map", label: "Map" },
        { value: "graph", label: "Graph" },
      ]}
      aria-label="Map or graph"
    />
  );
}
