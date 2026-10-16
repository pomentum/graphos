import { Alert, Badge, Group, List, Loader, ScrollArea, Stack, Table, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { BulkEdgesResult, BulkNodesResult } from "../../api/types";
import {
  INVALID,
  cellValue,
  errorRow,
  idOf,
  type ImportCheck,
  type ImportPlan,
  type IssueExample,
} from "../../lib/csvImport";
import { formatValue } from "../../lib/properties";
import { formatCount } from "../../lib/time";
import classes from "./CsvImport.module.css";

/** Rows shown in the preview. */
export const PREVIEW_ROWS = 20;
/** graphos's refusals listed under its answer. */
const ERRORS_SHOWN = 10;
/** Property problems named in the answer, by `<label>.<property>:<rule>`. */
const TALLIES_SHOWN = 5;

/** The first rows as they will be written: the ids, then each imported property as read. */
export function CsvPreview({ plan, header, rows }: { plan: ImportPlan; header: string[]; rows: string[][] }) {
  const { t } = useTranslation();
  const keys =
    plan.kind === "nodes"
      ? [{ index: plan.id, name: t("csvImport.preview.id") }]
      : [
          { index: plan.src, name: t("csvImport.preview.source") },
          { index: plan.dst, name: t("csvImport.preview.target") },
        ];
  const columns = plan.columns.filter((column) => column.property);
  const shown = rows.slice(0, PREVIEW_ROWS);
  return (
    <Stack gap={4}>
      <Text size="sm" fw={600}>
        {t("csvImport.preview.title")}
      </Text>
      <Text size="xs" c="dimmed">
        {t("csvImport.preview.first", { shown: formatCount(shown.length), total: formatCount(rows.length) })}
      </Text>
      <ScrollArea type="auto">
        <Table className={classes.table} withTableBorder verticalSpacing={2} horizontalSpacing="xs" fz="xs">
          <Table.Thead>
            <Table.Tr>
              {keys.map((key) => (
                <Table.Th key={`key-${key.index}`} title={header[key.index]}>
                  {key.name}
                </Table.Th>
              ))}
              {columns.map((column) => (
                <Table.Th key={column.index} title={column.header}>
                  {column.property}
                </Table.Th>
              ))}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {shown.map((row, at) => (
              <Table.Tr key={at}>
                {keys.map((key) => (
                  <Table.Td key={`key-${key.index}`} className={classes.cell}>
                    {idOf(row, key.index)}
                  </Table.Td>
                ))}
                {columns.map((column) => {
                  const value = cellValue(row[column.index], column.type);
                  return value === INVALID ? (
                    <Table.Td
                      key={column.index}
                      className={`${classes.cell} ${classes.invalid}`}
                      title={t("csvImport.preview.invalid", { type: t(`csvImport.types.${column.type}`) })}
                    >
                      {row[column.index]}
                    </Table.Td>
                  ) : (
                    <Table.Td key={column.index} className={classes.cell}>
                      {formatValue(value)}
                    </Table.Td>
                  );
                })}
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </ScrollArea>
    </Stack>
  );
}

/** An example as `row 3 name "x"`; `rowName` says the row in words. */
function exampleText(example: IssueExample, rowName: (row: string) => string): string {
  const parts = [
    example.row !== undefined ? rowName(formatCount(example.row)) : null,
    example.column ?? null,
    example.value !== undefined ? JSON.stringify(example.value) : null,
  ].filter(Boolean);
  return parts.join(" ");
}

/**
 * What the checks over every row found, refusals first, each with its first
 * examples; while they run (`check` null), how far they are.
 */
export function CsvChecks({ check, progress }: { check: ImportCheck | null; progress: number }) {
  const { t } = useTranslation();
  if (!check) {
    return (
      <Group gap="xs" aria-live="polite">
        <Loader size="xs" />
        <Text size="xs" c="dimmed">
          {t("csvImport.checks.checking", { percent: Math.floor(progress * 100) })}
        </Text>
      </Group>
    );
  }
  return (
    <Stack gap={6} className={classes.issues}>
      <Text size="sm" fw={600}>
        {t("csvImport.checks.title", { rows: formatCount(check.rows) })}
      </Text>
      {check.issues.length ? (
        <Stack gap={6}>
          {check.issues.map((issue) => (
            <div key={issue.code}>
              <Group gap={6} wrap="nowrap" align="flex-start">
                <Badge size="xs" variant="light" color={issue.refused ? "red" : "yellow"} style={{ flex: "none" }}>
                  {t(issue.refused ? "csvImport.checks.refused" : "csvImport.checks.warning")}
                </Badge>
                <Text size="xs">{t(`csvImport.issues.${issue.code}`, { count: issue.count })}</Text>
              </Group>
              {issue.examples.length ? (
                <Text size="xs" c="dimmed" ml={4}>
                  {t("csvImport.checks.examples", {
                    examples: issue.examples
                      .map((example) => exampleText(example, (row) => t("csvImport.checks.row", { row })))
                      .join("; "),
                  })}
                </Text>
              ) : null}
            </div>
          ))}
        </Stack>
      ) : (
        <Text size="xs" c="dimmed">
          {t("csvImport.checks.none")}
        </Text>
      )}
      <Text size="xs" c="dimmed">
        {t("csvImport.checks.hint")}
      </Text>
    </Stack>
  );
}

function tallies(counts: Record<string, number> | undefined): string | null {
  const entries = Object.entries(counts ?? {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return null;
  const named = entries.slice(0, TALLIES_SHOWN).map(([key, count]) => `${key} (${formatCount(count)})`);
  return entries.length > TALLIES_SHOWN ? `${named.join(", ")}, …` : named.join(", ");
}

/** What was sent: the file's rows, the lines they became, and the file's row each line comes from. */
export interface SentRows {
  rows: number;
  lines: number;
  rowOfLine: number[];
}

/**
 * graphos's answer: rows received, written, refused, skipped, and its first
 * refusals, each named by the file's row rather than the line sent.
 */
export function CsvImportAnswer({ result, sent }: { result: BulkNodesResult | BulkEdgesResult; sent: SentRows }) {
  const { t } = useTranslation();
  const errorText = (error: string) => {
    const at = errorRow(error, sent.rowOfLine);
    return at ? t("csvImport.result.errorAt", { row: formatCount(at.row), message: at.message }) : error;
  };
  const written = "nodes_upserted" in result ? result.nodes_upserted : result.edges_upserted;
  const skipped = "edges_skipped" in result ? result.edges_skipped : 0;
  const violations = tallies(result.property_violations);
  const warnings = tallies(result.property_warnings);
  const trouble = result.rows_invalid > 0 || skipped > 0 || result.errors.length > 0;
  return (
    <Alert variant="light" color={trouble ? "yellow" : "gray"} radius="md">
      <Stack gap={4}>
        {sent.lines !== sent.rows ? (
          <Text size="xs">
            {t("csvImport.result.merged", { rows: formatCount(sent.rows), lines: formatCount(sent.lines) })}
          </Text>
        ) : null}
        <Text size="xs">
          {t("csvImport.result.written", {
            received: formatCount(result.rows_received),
            written: formatCount(written),
            seconds: Math.round(result.elapsed_ms / 1000),
          })}
        </Text>
        {result.rows_invalid ? (
          <Text size="xs">{t("csvImport.result.invalid", { count: result.rows_invalid })}</Text>
        ) : null}
        {skipped ? <Text size="xs">{t("csvImport.result.skipped", { count: skipped })}</Text> : null}
        {violations ? <Text size="xs">{t("csvImport.result.violations", { list: violations })}</Text> : null}
        {warnings ? <Text size="xs">{t("csvImport.result.warnings", { list: warnings })}</Text> : null}
        {result.errors.length ? (
          <List size="xs" spacing={2}>
            {result.errors.slice(0, ERRORS_SHOWN).map((error) => (
              <List.Item key={error}>{errorText(error)}</List.Item>
            ))}
            {result.errors.length > ERRORS_SHOWN ? (
              <List.Item>{t("csvImport.result.moreErrors", { count: result.errors.length - ERRORS_SHOWN })}</List.Item>
            ) : null}
          </List>
        ) : null}
      </Stack>
    </Alert>
  );
}
