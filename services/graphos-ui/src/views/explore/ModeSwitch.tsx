import { SegmentedControl } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { ExploreMode } from "../../graph/WorkspaceProvider";

/** Switches Explore between the map of labels and the graph canvas; both keep their state. */
export function ModeSwitch({ mode, onChange }: { mode: ExploreMode; onChange: (mode: ExploreMode) => void }) {
  const { t } = useTranslation();
  return (
    <SegmentedControl
      size="xs"
      radius="md"
      value={mode}
      onChange={(value) => onChange(value as ExploreMode)}
      data={[
        { value: "map", label: t("views.explore.mode.map") },
        { value: "graph", label: t("views.explore.mode.graph") },
      ]}
      aria-label={t("views.explore.mode.label")}
    />
  );
}
