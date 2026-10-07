import { Table, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { Properties } from "../api/types";
import { formatValue } from "../lib/properties";
import classes from "./NodeTable.module.css";

/** Width of the id column; other columns get at least `MIN_COLUMN_WIDTH`. */
const ID_WIDTH = 240;
const MIN_COLUMN_WIDTH = 140;

export interface NodeRow {
  id: string;
  label: string;
  properties: Properties;
}

export interface NodeTableProps {
  rows: NodeRow[];
  /** Property names, one column each, after the id. */
  columns: string[];
  /** Called when a row is clicked or opened with Enter. */
  onOpen: (row: NodeRow) => void;
  /** Dims the rows while the next page loads. */
  stale?: boolean;
}

/** Nodes as rows: the id, then one column per property; long values are cut, with the full value on hover. */
export function NodeTable({ rows, columns, onOpen, stale }: NodeTableProps) {
  const { t } = useTranslation();
  return (
    <Table
      stickyHeader
      highlightOnHover
      className={classes.table}
      // Every column keeps a readable width; past the view, the table scrolls sideways.
      style={{ minWidth: ID_WIDTH + columns.length * MIN_COLUMN_WIDTH }}
      data-stale={stale ? "true" : undefined}
    >
      <Table.Thead>
        <Table.Tr>
          <Table.Th className={classes.id}>id</Table.Th>
          {columns.map((column) => (
            <Table.Th key={column}>{column}</Table.Th>
          ))}
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {rows.map((row) => (
          <Table.Tr
            key={`${row.label}:${row.id}`}
            className={classes.row}
            tabIndex={0}
            aria-label={t("components.nodeTable.openRow", { id: row.id })}
            onClick={() => onOpen(row)}
            onKeyDown={(event) => {
              if (event.key === "Enter") onOpen(row);
            }}
          >
            <Table.Td>
              <Text size="xs" ff="monospace" truncate title={row.id}>
                {row.id}
              </Text>
            </Table.Td>
            {columns.map((column) => {
              const value = formatValue(row.properties[column]);
              return (
                <Table.Td key={column}>
                  <Text size="sm" truncate title={value}>
                    {value}
                  </Text>
                </Table.Td>
              );
            })}
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}
