import { Group, Kbd, Modal, SimpleGrid, Stack, Text, Title } from "@mantine/core";
import { useTranslation } from "react-i18next";
import { isApple, searchShortcut } from "../lib/platform";

/** One shortcut: the keys, `mod` standing for ⌘ or Ctrl, and what they do (an i18n key under `shell.shortcuts.do`). */
type Shortcut = [keys: string[], does: string];

/** Every keyboard shortcut, by where it works. Keys in a text field go to the field. */
const SECTIONS: [section: string, shortcuts: Shortcut[]][] = [
  [
    "everywhere",
    [
      [["search"], "search"],
      [["?"], "shortcuts"],
    ],
  ],
  [
    "explore",
    [
      [["mod", "Z"], "back"],
      [["F"], "focus"],
      [["E"], "expand"],
      [["C"], "collapse"],
      [["H"], "hide"],
      [["mod", "A"], "selectAll"],
      [["Esc"], "deselect"],
      [["Shift", "drag"], "lasso"],
    ],
  ],
  [
    "schema",
    [
      [["mod", "Z"], "undo"],
      [["V"], "listOrDiagram"],
      [["L"], "addLabel"],
      [["R"], "addRelationship"],
      [["M"], "maximize"],
      [["Esc"], "restore"],
    ],
  ],
  ["query", [[["mod", "Enter"], "run"]]],
];

/** The list of keyboard shortcuts, opened with `?`. */
export function ShortcutsDialog({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const key = (name: string) =>
    name === "mod"
      ? isApple()
        ? "⌘"
        : "Ctrl"
      : name === "search"
        ? searchShortcut()
        : name === "drag"
          ? t("shell.shortcuts.drag")
          : name;
  return (
    <Modal opened={opened} onClose={onClose} title={t("shell.shortcuts.title")} size="lg">
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg">
        {SECTIONS.map(([section, shortcuts]) => (
          <Stack key={section} gap={6}>
            <Title order={3} size="h6">
              {t(`shell.shortcuts.sections.${section}` as "shell.shortcuts.sections.everywhere")}
            </Title>
            {shortcuts.map(([keys, does]) => (
              <Group key={does} justify="space-between" wrap="nowrap" gap="sm">
                <Text size="sm">{t(`shell.shortcuts.do.${does}` as "shell.shortcuts.do.search")}</Text>
                <Group gap={4} wrap="nowrap">
                  {keys.map((k) => (
                    <Kbd key={k} size="xs">
                      {key(k)}
                    </Kbd>
                  ))}
                </Group>
              </Group>
            ))}
          </Stack>
        ))}
      </SimpleGrid>
      <Text size="xs" c="dimmed" mt="md">
        {t("shell.shortcuts.inFields")}
      </Text>
    </Modal>
  );
}
