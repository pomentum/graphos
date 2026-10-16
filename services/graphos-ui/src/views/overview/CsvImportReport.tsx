import { Alert, Badge, Group, List, Loader, ScrollArea, Stack, Table, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { CsvImportResult, CsvIssue, CsvIssueExample } from "../../api/types";
import { INVALID, cellValue, idOf, issueMessage, type ImportPlan } from "../../lib/csvImport";
import { formatValue } from "../../lib/properties";
import { formatCount } from "../../lib/time";
import classes from "./CsvImport.module.css";

/** Rows shown in the preview. */
export const PREVIEW_ROWS = 20;
/** graphos's refusals listed under its answer. */
const ERRORS_SHOWN = 10;
/** Property problems named in the answer, by `<label>.<property>:<rule>`. */
const TALLIES_SHOWN = 5;

/** The first rows as graphos will read them: the ids, then each imported property as read. */
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
        {t("csvImport.preview.first", { shown: formatCount(shown.length) })}
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

/** An example as `row 3 Age "x"`; `say` puts the row and the cell count in words. */
function exampleText(
  example: CsvIssueExample,
  say: { row: (row: string) => string; cells: (cells: string) => string },
): string {
  const parts = [
    example.row !== undefined ? say.row(formatCount(example.row)) : null,
    example.column ?? null,
    example.value !== undefined ? JSON.stringify(example.value) : null,
    example.cells !== undefined ? `(${say.cells(formatCount(example.cells))})` : null,
  ].filter(Boolean);
  return parts.join(" ");
}

function tallies(counts: Record<string, number> | undefined): string | null {
  const entries = Object.entries(counts ?? {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return null;
  const named = entries.slice(0, TALLIES_SHOWN).map(([key, count]) => `${key} (${formatCount(count)})`);
  return entries.length > TALLIES_SHOWN ? `${named.join(", ")}, …` : named.join(", ");
}

/** graphos's refusals, as it gives them (`row N: …`, the file's row), the first ten. */
function ErrorList({ errors }: { errors: string[] }) {
  const { t } = useTranslation();
  if (!errors.length) return null;
  return (
    <List size="xs" spacing={2}>
      {errors.slice(0, ERRORS_SHOWN).map((error) => (
        <List.Item key={error}>{error}</List.Item>
      ))}
      {errors.length > ERRORS_SHOWN ? (
        <List.Item>{t("csvImport.result.moreErrors", { count: errors.length - ERRORS_SHOWN })}</List.Item>
      ) : null}
    </List>
  );
}

/** The issues of a check, refusals first, each with its first examples. */
function IssueList({ issues }: { issues: CsvIssue[] }) {
  const { t } = useTranslation();
  const say = {
    row: (row: string) => t("csvImport.checks.row", { row }),
    cells: (cells: string) => t("csvImport.checks.cells", { cells }),
  };
  return (
    <Stack gap={6}>
      {issues.map((issue) => {
        const message = issueMessage(issue.code);
        return (
          <div key={issue.code}>
            <Group gap={6} wrap="nowrap" align="flex-start">
              <Badge size="xs" variant="light" color={issue.refused ? "red" : "yellow"} style={{ flex: "none" }}>
                {t(issue.refused ? "csvImport.checks.refused" : "csvImport.checks.warning")}
              </Badge>
              <Text size="xs">
                {message
                  ? t(`csvImport.issues.${message}`, { count: issue.count })
                  : t("csvImport.issues.unknown", { count: issue.count, code: issue.code })}
              </Text>
            </Group>
            {issue.examples.length ? (
              <Text size="xs" c="dimmed" ml={4}>
                {t("csvImport.checks.examples", {
                  examples: issue.examples.map((example) => exampleText(example, say)).join("; "),
                })}
              </Text>
            ) : null}
          </div>
        );
      })}
    </Stack>
  );
}

export interface CsvChecksProps {
  /** graphos's dry run of the current mapping; null while it runs or when it failed. */
  check: CsvImportResult | null;
  /** How much of the file is sent for the check (0 to 1); null when no check runs. */
  sending: number | null;
  /** Why graphos refused the mapping (422), or could not check the file. */
  error: { mapping: boolean; message: string } | null;
}

/**
 * graphos's dry run of the import as mapped: how many rows and records,
 * what would be written, refused or skipped, the issues it found in the
 * file, the property definitions' verdicts and its first refusals.
 */
export function CsvChecks({ check, sending, error }: CsvChecksProps) {
  const { t } = useTranslation();
  if (error) {
    return (
      <Alert color="red" variant="light" radius="md">
        <Text size="xs">
          {t(error.mapping ? "csvImport.checks.mappingRefused" : "csvImport.checks.failed", {
            message: error.message,
          })}
        </Text>
      </Alert>
    );
  }
  if (!check) {
    return (
      <Group gap="xs" aria-live="polite">
        <Loader size="xs" />
        <Text size="xs" c="dimmed">
          {sending !== null && sending < 1
            ? t("csvImport.checks.sending", { percent: Math.floor(sending * 100) })
            : t("csvImport.checks.checking")}
        </Text>
      </Group>
    );
  }
  const violations = tallies(check.property_violations);
  const warnings = tallies(check.property_warnings);
  const clean = !check.issues.length && !check.rows_invalid && !check.edges_skipped && !violations && !warnings;
  return (
    <Stack gap={6} className={classes.issues}>
      <Text size="sm" fw={600}>
        {t("csvImport.checks.title", { rows: formatCount(check.rows) })}
      </Text>
      <Text size="xs">
        {t("csvImport.checks.summary", { records: formatCount(check.records), written: formatCount(check.written) })}
      </Text>
      {check.rows_invalid ? (
        <Text size="xs">{t("csvImport.checks.wouldRefuse", { count: check.rows_invalid })}</Text>
      ) : null}
      {check.edges_skipped ? (
        <Text size="xs">{t("csvImport.checks.wouldSkip", { count: check.edges_skipped })}</Text>
      ) : null}
      {check.issues.length ? <IssueList issues={check.issues} /> : null}
      {violations ? <Text size="xs">{t("csvImport.checks.violations", { list: violations })}</Text> : null}
      {warnings ? <Text size="xs">{t("csvImport.checks.warnings", { list: warnings })}</Text> : null}
      <ErrorList errors={check.errors} />
      {clean ? (
        <Text size="xs" c="dimmed">
          {t("csvImport.checks.none")}
        </Text>
      ) : null}
      <Text size="xs" c="dimmed">
        {t("csvImport.checks.hint")}
      </Text>
    </Stack>
  );
}

/** graphos's answer: rows, records, written, refused, skipped, and its first refusals by the file's row. */
export function CsvImportAnswer({ result }: { result: CsvImportResult }) {
  const { t } = useTranslation();
  const violations = tallies(result.property_violations);
  const warnings = tallies(result.property_warnings);
  const trouble = result.rows_invalid > 0 || result.edges_skipped > 0 || result.errors.length > 0;
  return (
    <Alert variant="light" color={trouble ? "yellow" : "gray"} radius="md">
      <Stack gap={4}>
        <Text size="xs">
          {t("csvImport.result.written", {
            rows: formatCount(result.rows),
            records: formatCount(result.records),
            written: formatCount(result.written),
            seconds: Math.round(result.elapsed_ms / 1000),
          })}
        </Text>
        {result.rows_invalid ? (
          <Text size="xs">{t("csvImport.result.invalid", { count: result.rows_invalid })}</Text>
        ) : null}
        {result.edges_skipped ? (
          <Text size="xs">{t("csvImport.result.skipped", { count: result.edges_skipped })}</Text>
        ) : null}
        {violations ? <Text size="xs">{t("csvImport.result.violations", { list: violations })}</Text> : null}
        {warnings ? <Text size="xs">{t("csvImport.result.warnings", { list: warnings })}</Text> : null}
        <ErrorList errors={result.errors} />
      </Stack>
    </Alert>
  );
}
