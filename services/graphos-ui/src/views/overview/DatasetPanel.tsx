import { Alert, Button, Checkbox, FileInput, Group, List, Radio, Stack, Text, Title } from "@mantine/core";
import { IconDownload, IconFileUpload, IconUpload } from "@tabler/icons-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { DatasetHeader, DatasetImportResult } from "../../api/types";
import { headerProblem, readDatasetHeader } from "../../lib/datasetFile";
import { pickSaveTarget, saveResponse } from "../../lib/download";
import { formatCount } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";
import classes from "./OverviewView.module.css";

export interface DatasetPanelProps {
  /** Imports a file into this tenant; resolves with what it wrote. */
  onImport: (
    file: File,
    mode: "merge" | "empty",
    applySchema: boolean,
    header: DatasetHeader | null,
  ) => Promise<DatasetImportResult>;
}

/**
 * The tenant's data as one file. Anyone who can read the tenant downloads it
 * (every node and edge, with the schema in the first line); whoever can write
 * to it uploads such a file, added to what is there or, into an empty tenant,
 * as an exact restore. Maintainers can have the file's schema registered
 * first. A download is written straight to the file where the browser allows
 * it; nothing is kept in browser storage.
 */
export function DatasetPanel({ onImport }: DatasetPanelProps) {
  const { t } = useTranslation();
  const { api, tenant, canWrite, canMaintain } = useTenantSession();
  const [gzip, setGzip] = useState(true);
  const [computed, setComputed] = useState(true);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [header, setHeader] = useState<DatasetHeader | null>(null);
  const [fileProblem, setFileProblem] = useState<string | null>(null);
  const [mode, setMode] = useState<"merge" | "empty">("merge");
  const [applySchema, setApplySchema] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<DatasetImportResult | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const download = async () => {
    setDownloadError(null);
    const filename = `${tenant}.ndjson${gzip ? ".gz" : ""}`;
    try {
      // Asked first, in the click: a picked file is written as the data arrives.
      const target = await pickSaveTarget(filename);
      if (target === undefined) return;
      setDownloading(true);
      await saveResponse(filename, await api.exportDataset({ gzip, computed }), target);
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : String(error));
    } finally {
      setDownloading(false);
    }
  };

  const choose = async (next: File | null) => {
    setFile(next);
    setHeader(null);
    setFileProblem(null);
    setResult(null);
    setUploadError(null);
    if (!next) return;
    const read = await readDatasetHeader(next);
    const problem = read ? headerProblem(read) : null;
    if (read && problem) setFileProblem(t(`views.overview.dataset.${problem}`, { version: read.version }));
    else setHeader(read);
  };

  const upload = async () => {
    if (!file) return;
    setUploading(true);
    setResult(null);
    setUploadError(null);
    try {
      setResult(await onImport(file, mode, applySchema && Boolean(header?.schema), header));
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : String(error));
    } finally {
      setUploading(false);
    }
  };

  return (
    <Stack gap="xs">
      <Title order={2} size="sm">
        {t("views.overview.dataset.title")}
      </Title>
      <Text size="xs" c="dimmed">
        {t("views.overview.dataset.hint")}
      </Text>
      <div className={classes.algorithms}>
        <Stack gap={6}>
          <Group gap="md">
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
          <Group>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconDownload size={14} />}
              loading={downloading}
              onClick={() => void download()}
            >
              {t("views.overview.dataset.download")}
            </Button>
          </Group>
          {downloadError ? (
            <Text size="xs" c="red">
              {downloadError}
            </Text>
          ) : null}
        </Stack>

        {canWrite ? (
          <Stack gap={6}>
            <FileInput
              size="xs"
              label={t("views.overview.dataset.file")}
              description={t("views.overview.dataset.fileHint")}
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
            <Group>
              <Button
                size="xs"
                variant="default"
                leftSection={<IconUpload size={14} />}
                disabled={!file || Boolean(fileProblem)}
                loading={uploading}
                onClick={() => void upload()}
              >
                {t("views.overview.dataset.upload")}
              </Button>
            </Group>
            {result ? <ImportSummary result={result} /> : null}
            {uploadError ? (
              <Alert variant="light" color="red" radius="md">
                <Text size="xs">{uploadError}</Text>
              </Alert>
            ) : null}
          </Stack>
        ) : null}
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
