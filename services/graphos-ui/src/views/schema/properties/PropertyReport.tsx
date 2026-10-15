import { Badge, Group, Modal, Stack, Table, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { LabelCheck, PropertiesCheck } from "../../../api/types";
import { formatCount } from "../../../lib/time";
import classes from "./PropertyPanel.module.css";

/** Example rows shown per rule broken. */
const EXAMPLES_SHOWN = 3;

/**
 * What the stored rows break of the property schema, read as if every rule
 * applied: the totals, then per label and type that breaks something the
 * rules it breaks (errors strict would refuse, warnings it would write) with
 * some example rows, and the properties its rows carry that nothing declares.
 */
export function PropertyReport({ report, onClose }: { report: PropertiesCheck; onClose: () => void }) {
  const { t } = useTranslation();
  const flagged = report.labels.filter(
    (l) => l.rows_with_errors || l.rows_with_warnings || Object.keys(l.undeclared).length,
  );
  const clean = report.labels.length - flagged.length;
  return (
    <Modal opened onClose={onClose} title={t("views.schema.properties.report.title")} centered radius="lg" size="xl">
      <Stack gap="md">
        <Text size="sm">
          {t("views.schema.properties.report.summary", {
            rows: formatCount(report.rows),
            errors: formatCount(report.rows_with_errors),
            warnings: formatCount(report.rows_with_warnings),
            seconds: (report.elapsed_ms / 1000).toFixed(1),
          })}
        </Text>
        {flagged.map((label) => (
          <LabelSection key={`${label.kind}:${label.name}`} label={label} />
        ))}
        {clean ? (
          <Text size="xs" c="dimmed">
            {t("views.schema.properties.report.clean", { count: clean })}
          </Text>
        ) : null}
      </Stack>
    </Modal>
  );
}

function LabelSection({ label }: { label: LabelCheck }) {
  const { t } = useTranslation();
  const rules = [
    ...Object.entries(label.errors).map(([key, count]) => ({ key, count, error: true })),
    ...Object.entries(label.warnings).map(([key, count]) => ({ key, count, error: false })),
  ];
  return (
    <section>
      <Group gap={8} mb={4}>
        <Text size="sm" fw={600}>
          {label.name}
        </Text>
        <Text size="xs" c="dimmed">
          {t("views.schema.properties.report.labelLine", {
            rows: formatCount(label.rows),
            errors: formatCount(label.rows_with_errors),
            warnings: formatCount(label.rows_with_warnings),
          })}
        </Text>
      </Group>
      {rules.length ? (
        <Table verticalSpacing={4} horizontalSpacing={8} fz="xs">
          <Table.Tbody>
            {rules.map(({ key, count, error }) => (
              <Table.Tr key={key}>
                <Table.Td className={classes.name}>{key}</Table.Td>
                <Table.Td className={classes.actions}>
                  <Badge size="xs" variant="light" radius="sm" color={error ? "red" : "yellow"} tt="none">
                    {formatCount(count)}
                  </Badge>
                </Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed" className={classes.carried}>
                    {(label.examples[key] ?? []).slice(0, EXAMPLES_SHOWN).join(", ")}
                  </Text>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : null}
      {Object.keys(label.undeclared).length ? (
        <Text size="xs" c="dimmed" mt={4}>
          {t("views.schema.properties.report.undeclared", {
            names: Object.entries(label.undeclared)
              .map(([name, count]) => `${name} (${formatCount(count)})`)
              .join(", "),
          })}
        </Text>
      ) : null}
    </section>
  );
}
