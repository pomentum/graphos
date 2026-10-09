import { SegmentedControl } from "@mantine/core";
import { useTranslation } from "react-i18next";
import { LENSES, type Lens } from "../../lib/metrics";

/**
 * What the Groups mode shows: the groups (Louvain communities), the nodes
 * ranked by one graph metric, or the connected components.
 */
export function LensSwitch({ lens, onChange }: { lens: Lens; onChange: (lens: Lens) => void }) {
  const { t } = useTranslation();
  return (
    <SegmentedControl
      size="xs"
      radius="md"
      value={lens}
      onChange={(value) => onChange(value as Lens)}
      data={LENSES.map((value) => ({ value, label: t(`views.explore.lens.${value}`) }))}
      aria-label={t("views.explore.lens.label")}
    />
  );
}
