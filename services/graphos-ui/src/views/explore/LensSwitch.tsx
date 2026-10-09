import { ActionIcon, Group, List, Popover, SegmentedControl, Stack, Text } from "@mantine/core";
import { IconInfoCircle } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { LENSES, type Lens } from "../../lib/metrics";

/**
 * What the Groups mode shows: the groups (Louvain communities), the nodes
 * ranked by one graph metric, or the connected components; with a note on
 * what the view shows and how to read it.
 */
export function LensSwitch({ lens, onChange }: { lens: Lens; onChange: (lens: Lens) => void }) {
  const { t } = useTranslation();
  return (
    <Group gap={4} wrap="nowrap">
      <SegmentedControl
        size="xs"
        radius="md"
        value={lens}
        onChange={(value) => onChange(value as Lens)}
        data={LENSES.map((value) => ({ value, label: t(`views.explore.lens.${value}`) }))}
        aria-label={t("views.explore.lens.label")}
      />
      <LensHelp lens={lens} />
    </Group>
  );
}

/** What the view shows, what the algorithm finds, and how to read the picture. */
export function LensHelp({ lens }: { lens: Lens }) {
  const { t } = useTranslation();
  const reading = t(`views.explore.lensHelp.${lens}.read`, { returnObjects: true }) as string[];
  return (
    <Popover width={360} position="bottom-start" withArrow shadow="md">
      <Popover.Target>
        <ActionIcon variant="subtle" color="gray" size="md" aria-label={t("views.explore.lensHelp.label")}>
          <IconInfoCircle size={16} />
        </ActionIcon>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap={6}>
          <Text size="sm" fw={600}>
            {t(`views.explore.lensHelp.${lens}.title`)}
          </Text>
          <Text size="sm">{t(`views.explore.lensHelp.${lens}.what`)}</Text>
          <Text size="xs" c="dimmed" fw={600} mt={4}>
            {t("views.explore.lensHelp.howToRead")}
          </Text>
          <List size="sm" spacing={2}>
            {reading.map((line) => (
              <List.Item key={line}>{line}</List.Item>
            ))}
          </List>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
