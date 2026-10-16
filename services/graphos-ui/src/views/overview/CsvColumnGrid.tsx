import { Checkbox, ScrollArea, Select, Table, Text, TextInput } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { EffectivePolicy } from "../../api/types";
import {
  COLUMN_TYPES,
  propertyNameOf,
  specOf,
  type ColumnChoice,
  type ColumnPlan,
  type ColumnType,
} from "../../lib/csvImport";
import { rulesOf } from "../../lib/propertySchema";
import classes from "./CsvImport.module.css";

export interface CsvColumnGridProps {
  columns: ColumnPlan[];
  /** The file's first row, to show what each column holds. */
  example: string[] | undefined;
  /** The definitions of the chosen label or type, to show what a property is declared as. */
  policy: EffectivePolicy | undefined;
  onChange: (index: number, choice: Partial<ColumnChoice>) => void;
}

/** Every column but the ids: imported or not, under which property name, as which type. */
export function CsvColumnGrid({ columns, example, policy, onChange }: CsvColumnGridProps) {
  const { t } = useTranslation();
  if (!columns.length) {
    return (
      <Text size="xs" c="dimmed">
        {t("csvImport.columns.none")}
      </Text>
    );
  }
  const types = COLUMN_TYPES.map((type) => ({ value: type, label: t(`csvImport.types.${type}`) }));
  return (
    <ScrollArea.Autosize mah={320} type="auto">
      <Table className={classes.table} withTableBorder verticalSpacing={4} horizontalSpacing="xs" fz="xs">
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={32} />
            <Table.Th>{t("csvImport.columns.column")}</Table.Th>
            <Table.Th>{t("csvImport.columns.example")}</Table.Th>
            <Table.Th>{t("csvImport.columns.property")}</Table.Th>
            <Table.Th w={140}>{t("csvImport.columns.type")}</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {columns.map((column) => {
            const included = column.property !== null;
            const spec = column.property ? specOf(policy, column.property) : undefined;
            const rules = spec ? rulesOf(spec) : [];
            return (
              <Table.Tr key={column.index}>
                <Table.Td>
                  <Checkbox
                    size="xs"
                    aria-label={t("csvImport.columns.include", { column: column.header })}
                    checked={included}
                    onChange={(e) =>
                      onChange(column.index, {
                        property: e.currentTarget.checked ? propertyNameOf(column.header, column.index) : null,
                      })
                    }
                  />
                </Table.Td>
                <Table.Td className={classes.cell} title={column.header}>
                  {column.header}
                </Table.Td>
                <Table.Td className={classes.cell} title={example?.[column.index]}>
                  <Text span size="xs" c="dimmed">
                    {example?.[column.index] ?? ""}
                  </Text>
                </Table.Td>
                <Table.Td>
                  {included ? (
                    <>
                      <TextInput
                        size="xs"
                        aria-label={t("csvImport.columns.propertyOf", { column: column.header })}
                        value={column.property ?? ""}
                        onChange={(e) => onChange(column.index, { property: e.currentTarget.value })}
                      />
                      {rules.length ? (
                        <Text size="xs" c="dimmed" mt={2}>
                          {t("csvImport.columns.declared", { rules: rules.join(", ") })}
                        </Text>
                      ) : null}
                    </>
                  ) : (
                    <Text size="xs" c="dimmed">
                      {t("csvImport.columns.skipped")}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  <Select
                    size="xs"
                    aria-label={t("csvImport.columns.typeOf", { column: column.header })}
                    data={types}
                    value={column.type}
                    disabled={!included}
                    allowDeselect={false}
                    onChange={(next) => next && onChange(column.index, { type: next as ColumnType })}
                  />
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
    </ScrollArea.Autosize>
  );
}
