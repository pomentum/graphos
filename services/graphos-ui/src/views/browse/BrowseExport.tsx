import { Button, Group, Menu, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconFileSpreadsheet } from "@tabler/icons-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ScanFilter } from "../../api/types";
import type { NodeRow } from "../../components/NodeTable";
import { CSV_TYPE, exportName, toCsv } from "../../lib/csv";
import { pickSaveTarget, saveFile } from "../../lib/download";
import { exportNodesCsv } from "../../lib/tableExport";
import { formatCount } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";

/**
 * The table as CSV: the page shown, or every node of the label that matches
 * the filter (paged through graphos and written as it arrives), with the
 * columns chosen; id and label first.
 */
export function BrowseExport({
  label,
  where,
  columns,
  rows,
  total,
}: {
  label: string;
  where: ScanFilter[];
  columns: string[];
  rows: NodeRow[];
  total?: number;
}) {
  const { t } = useTranslation();
  const { api, tenant } = useTenantSession();
  const [written, setWritten] = useState<number | null>(null);
  const stop = useRef<AbortController | null>(null);
  const filename = exportName([tenant, label], "csv");

  const page = () =>
    saveFile(
      filename,
      toCsv(
        ["id", "label", ...columns],
        rows.map((row) => [row.id, row.label, ...columns.map((c) => row.properties[c])]),
      ),
      CSV_TYPE,
    );

  const all = async () => {
    const controller = new AbortController();
    try {
      // Asked in the click itself: a picked file is written as the rows arrive.
      const target = await pickSaveTarget(filename);
      if (target === undefined) return;
      stop.current = controller;
      setWritten(0);
      const count = await exportNodesCsv(
        api,
        { label, where, columns, filename },
        target,
        setWritten,
        controller.signal,
      );
      notifications.show({ message: t("views.browse.export.done", { count, formatted: formatCount(count) }) });
    } catch (error) {
      if (!controller.signal.aborted) {
        notifications.show({
          color: "red",
          title: t("common.export.failed"),
          message: error instanceof Error ? error.message : String(error),
        });
      }
    } finally {
      stop.current = null;
      setWritten(null);
    }
  };

  if (written !== null) {
    return (
      <Group gap={6} wrap="nowrap">
        <Text size="xs" c="dimmed">
          {total !== undefined
            ? t("views.browse.export.progressOf", { rows: formatCount(written), total: formatCount(total) })
            : t("views.browse.export.progress", { rows: formatCount(written) })}
        </Text>
        <Button size="compact-xs" variant="subtle" color="red" onClick={() => stop.current?.abort()}>
          {t("common.transfers.cancel")}
        </Button>
      </Group>
    );
  }
  return (
    <Menu position="bottom-end" width={240}>
      <Menu.Target>
        <Button size="xs" variant="default" leftSection={<IconFileSpreadsheet size={14} />}>
          {t("common.export.csv")}
        </Button>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Item disabled={rows.length === 0} onClick={page}>
          {t("views.browse.export.page", { count: rows.length })}
        </Menu.Item>
        <Menu.Item onClick={() => void all()}>
          {total !== undefined
            ? t("views.browse.export.allOf", { formatted: formatCount(total) })
            : t("views.browse.export.all")}
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}
