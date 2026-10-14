import { Button } from "@mantine/core";
import { IconFileSpreadsheet } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { CSV_TYPE, exportName, toCsv } from "../lib/csv";
import { saveFile } from "../lib/download";

/** A quiet "CSV" button that saves a small table already on the page. */
export function CsvButton({
  name,
  header,
  rows,
  label,
}: {
  /** Parts of the file name. */
  name: string[];
  header: string[];
  rows: unknown[][];
  /** What the table is, for the button's accessible name. */
  label: string;
}) {
  const { t } = useTranslation();
  return (
    <Button
      size="compact-xs"
      variant="subtle"
      color="gray"
      leftSection={<IconFileSpreadsheet size={14} />}
      disabled={rows.length === 0}
      aria-label={t("common.export.csvOf", { what: label })}
      onClick={() => saveFile(exportName(name, "csv"), toCsv(header, rows), CSV_TYPE)}
    >
      {t("common.export.csv")}
    </Button>
  );
}
