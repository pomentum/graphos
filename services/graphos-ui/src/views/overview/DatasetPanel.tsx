import { Alert, Button, Checkbox, FileInput, Group, List, Radio, Stack, Text, Title } from "@mantine/core";
import { IconDownload, IconFileUpload, IconUpload } from "@tabler/icons-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { DatasetHeader, DatasetImportResult } from "../../api/types";
import { TransferProgress } from "../../components/Transfers";
import { useRefreshAfterImport, useStats } from "../../data/queries";
import { headerProblem, readDatasetHeader } from "../../lib/datasetFile";
import { pickSaveTarget } from "../../lib/download";
import { formatCount } from "../../lib/time";
import { isActive, startDownload, startUpload, useTransfers } from "../../lib/transfers";
import { useTenantSession } from "../../session/SessionProvider";
import { CsvImportEntry } from "./CsvImportDialog";
import classes from "./OverviewView.module.css";

/**
 * The tenant's data as one file. Anyone who can read the tenant downloads it
 * (every node and edge, with the schema in the first line); whoever can write
 * to it uploads such a file, added to what is there or, into an empty tenant,
 * as an exact restore. Maintainers can have the file's schema registered
 * first. Each transfer shows how far it is and can be cancelled; it goes on,
 * and its progress stays (here and in the toolbar), when the user moves to
 * another page. A download is written straight to the file where the
 * browser allows it; nothing is kept in browser storage.
 */
export function DatasetPanel() {
  const { t } = useTranslation();
  const { api, tenant, canWrite, canMaintain } = useTenantSession();
  const stats = useStats();
  const refresh = useRefreshAfterImport();
  const mine = useTransfers().filter((transfer) => transfer.tenant === tenant);
  const download = mine.filter((x) => x.kind === "download").at(-1);
  const upload = mine.filter((x) => x.kind === "upload").at(-1);
  const [gzip, setGzip] = useState(true);
  const [computed, setComputed] = useState(true);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [header, setHeader] = useState<DatasetHeader | null>(null);
  const [fileProblem, setFileProblem] = useState<string | null>(null);
  const [mode, setMode] = useState<"merge" | "empty">("merge");
  const [applySchema, setApplySchema] = useState(false);

  const startDownloading = async () => {
    setDownloadError(null);
    try {
      // Asked in the click itself: a picked file is written as the data arrives.
      const target = await pickSaveTarget(`${tenant}.ndjson${gzip ? ".gz" : ""}`);
      if (target === undefined) return;
      startDownload(api, tenant, { gzip, computed }, target);
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : String(error));
    }
  };

  const choose = async (next: File | null) => {
    setFile(next);
    setHeader(null);
    setFileProblem(null);
    if (!next) return;
    const read = await readDatasetHeader(next);
    const problem = read ? headerProblem(read) : null;
    if (read && problem) setFileProblem(t(`views.overview.dataset.${problem}`, { version: read.version }));
    else setHeader(read);
  };

  const startUploading = () => {
    if (!file) return;
    startUpload(api, tenant, file, { mode, applySchema: applySchema && Boolean(header?.schema), header }, refresh);
  };

  const size =
    typeof stats.data?.n_vertices === "number" && typeof stats.data.n_edges === "number"
      ? t("views.overview.dataset.size", {
          nodes: formatCount(stats.data.n_vertices),
          edges: formatCount(stats.data.n_edges),
        })
      : "";

  return (
    <Stack gap="xs">
      <Title order={2} size="sm">
        {t("views.overview.dataset.title")}
      </Title>
      <Text size="xs" c="dimmed">
        {t("views.overview.dataset.hint")}
      </Text>
      <div className={classes.algorithms}>
        <Stack gap="xs" className={classes.algorithm}>
          <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
            <div>
              <Text size="sm" fw={600}>
                {t("views.overview.dataset.downloadTitle")}
              </Text>
              <Text size="xs" c="dimmed">
                {t("views.overview.dataset.downloadWhat", { size })}
              </Text>
              <Group gap="md" mt={8}>
                <Checkbox
                  size="xs"
                  label={t("views.overview.dataset.gzip")}
                  checked={gzip}
                  onChange={(e) => setGzip(e.currentTarget.checked)}
                />
                <Checkbox
                  size="xs"
                  label={t("views.overview.dataset.computed")}
                  checked={computed}
                  onChange={(e) => setComputed(e.currentTarget.checked)}
                />
              </Group>
            </div>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconDownload size={14} />}
              disabled={download ? isActive(download) : false}
              onClick={() => void startDownloading()}
            >
              {t("views.overview.dataset.download")}
            </Button>
          </Group>
          {download ? <TransferProgress transfer={download} /> : null}
          {downloadError ? (
            <Text size="xs" c="red">
              {downloadError}
            </Text>
          ) : null}
        </Stack>

        {canWrite ? (
          <Stack gap="xs" className={classes.algorithm}>
            <div>
              <Text size="sm" fw={600}>
                {t("views.overview.dataset.uploadTitle")}
              </Text>
              <Text size="xs" c="dimmed">
                {t("views.overview.dataset.uploadWhat")}
              </Text>
            </div>
            <FileInput
              size="xs"
              aria-label={t("views.overview.dataset.file")}
              placeholder={t("views.overview.dataset.filePlaceholder")}
              accept=".ndjson,.gz,.jsonl,application/x-ndjson,application/gzip"
              leftSection={<IconFileUpload size={14} />}
              value={file}
              onChange={(next) => void choose(next)}
              error={fileProblem}
              clearable
            />
            {header ? (
              <Text size="xs" c="dimmed">
                {t("views.overview.dataset.source", {
                  tenant: header.tenant ?? "?",
                  date: header.exported_at ?? "?",
                  nodes: formatCount(header.counts?.nodes ?? 0),
                  edges: formatCount(header.counts?.edges ?? 0),
                })}
              </Text>
            ) : file && !fileProblem ? (
              <Text size="xs" c="dimmed">
                {t("views.overview.dataset.noHeader")}
              </Text>
            ) : null}
            <Group justify="space-between" align="flex-end" wrap="wrap" gap="sm">
              <Stack gap={6}>
                <Radio.Group size="xs" value={mode} onChange={(next) => setMode(next as "merge" | "empty")}>
                  <Group gap="md">
                    <Radio size="xs" value="merge" label={t("views.overview.dataset.merge")} />
                    <Radio size="xs" value="empty" label={t("views.overview.dataset.empty")} />
                  </Group>
                </Radio.Group>
                {canMaintain && header?.schema ? (
                  <Checkbox
                    size="xs"
                    label={t("views.overview.dataset.applySchema")}
                    checked={applySchema}
                    onChange={(e) => setApplySchema(e.currentTarget.checked)}
                  />
                ) : null}
              </Stack>
              <Button
                size="xs"
                leftSection={<IconUpload size={14} />}
                disabled={!file || Boolean(fileProblem) || (upload ? isActive(upload) : false)}
                onClick={startUploading}
              >
                {t("views.overview.dataset.upload")}
              </Button>
            </Group>
            {upload ? <TransferProgress transfer={upload} /> : null}
            {upload?.result ? <ImportSummary result={upload.result} /> : null}
          </Stack>
        ) : null}
        {canWrite ? <CsvImportEntry /> : null}
      </div>
    </Stack>
  );
}

function ImportSummary({ result }: { result: DatasetImportResult }) {
  const { t } = useTranslation();
  return (
    <Alert variant="light" color={result.complete === false || result.errors.length ? "yellow" : "gray"} radius="md">
      <Stack gap={4}>
        <Text size="xs">
          {t("views.overview.dataset.imported", {
            nodes: formatCount(result.nodes_upserted),
            edges: formatCount(result.edges_upserted),
            seconds: Math.round(result.elapsed_ms / 1000),
          })}
        </Text>
        {result.edges_skipped ? (
          <Text size="xs">{t("views.overview.dataset.skipped", { count: result.edges_skipped })}</Text>
        ) : null}
        {result.complete === false ? <Text size="xs">{t("views.overview.dataset.incomplete")}</Text> : null}
        {result.errors.length ? (
          <List size="xs" spacing={2}>
            {result.errors.slice(0, 5).map((error) => (
              <List.Item key={error}>{error}</List.Item>
            ))}
          </List>
        ) : null}
      </Stack>
    </Alert>
  );
}
