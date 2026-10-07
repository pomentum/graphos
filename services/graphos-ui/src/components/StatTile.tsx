import { Paper, Text } from "@mantine/core";

/** A headline number with its caption, e.g. "12,345 / Nodes". */
export function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <Paper radius="lg" p="md" bg="var(--gx-group)" withBorder style={{ borderColor: "var(--gx-separator)" }}>
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text fw={600} size="xl" mt={2}>
        {value}
      </Text>
    </Paper>
  );
}
