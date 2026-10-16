import {
  Alert,
  Button,
  FileInput,
  Group,
  Modal,
  Progress,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Text,
} from "@mantine/core";
import { IconFileUpload, IconTableImport } from "@tabler/icons-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../../api/http";
import type { CsvImportResult, TenantSchema } from "../../api/types";
import { usePropertyDefinitions, useRefreshAfterImport, useSchema } from "../../data/queries";
import { readFileText, type CsvDelimiter, type CsvTable } from "../../lib/csv";
import {
  DELIMITER_NAMES,
  HEAD_BYTES,
  ID_NAMES,
  SOURCE_NAMES,
  TARGET_NAMES,
  guessColumn,
  headTable,
  importQuery,
  planColumns,
  policyOf,
  type ColumnChoice,
  type ImportPlan,
  type RowKind,
} from "../../lib/csvImport";
import { Endpoints, lineage } from "../../lib/endpoints";
import { formatBytes, formatCount } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";
import { CsvColumnGrid } from "./CsvColumnGrid";
import { CsvChecks, CsvImportAnswer, CsvPreview } from "./CsvImportReport";
import classes from "./OverviewView.module.css";

/** The Overview's entry to the CSV import, beside the dataset upload (for writers). */
export function CsvImportEntry() {
  const { t } = useTranslation();
  const [opened, setOpened] = useState(false);
  return (
    <Stack gap="xs" className={classes.algorithm}>
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
        <div>
          <Text size="sm" fw={600}>
            {t("csvImport.entryTitle")}
          </Text>
          <Text size="xs" c="dimmed">
            {t("csvImport.entryWhat")}
          </Text>
        </div>
        <Button size="xs" variant="default" leftSection={<IconTableImport size={14} />} onClick={() => setOpened(true)}>
          {t("csvImport.open")}
        </Button>
      </Group>
      {opened ? <CsvImportDialog onClose={() => setOpened(false)} /> : null}
    </Stack>
  );
}

/**
 * The file and its start as read: the text (to read it again with another
 * separator), whether it is the whole file, and its table (header, preview).
 */
interface Loaded {
  file: File;
  text: string;
  whole: boolean;
  table: CsvTable;
}

/** What the user chose; `null` until chosen, then the defaults apply. */
interface Choices {
  kind: RowKind;
  label: string | null;
  id: number | null;
  type: string | null;
  srcLabel: string | null;
  src: number | null;
  dstLabel: string | null;
  dst: number | null;
  columns: Record<number, Partial<ColumnChoice>>;
}

const NO_CHOICES: Choices = {
  kind: "nodes",
  label: null,
  id: null,
  type: null,
  srcLabel: null,
  src: null,
  dstLabel: null,
  dst: null,
  columns: {},
};

type Phase = "sending" | "writing" | "done" | "cancelled" | "stopped" | "failed";

interface Run {
  phase: Phase;
  share: number;
  result?: CsvImportResult;
  error?: string;
}

/** How long the mapping stays unchanged before graphos checks the file again with it. */
const CHECK_DELAY_MS = 400;

const RUNNING: Phase[] = ["sending", "writing"];

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** The choice if still offered, else the first offered. */
function pick<T>(chosen: T | null, offered: T[]): T | null {
  return chosen !== null && offered.includes(chosen) ? chosen : (offered[0] ?? null);
}

/**
 * Labels for one end of a relationship: those a declared pair covers first
 * (a child label stands for its parent; the target's after the chosen
 * source), only those when graphos enforces the pairs, under the two group
 * names given.
 */
function endLabels(
  schema: TenantSchema | undefined,
  type: string | null,
  end: { src: string | null } | "src",
  groups: { declared: string; others: string },
) {
  const labels = schema?.vertex_labels ?? [];
  if (!schema || !type) return { offered: labels, data: labels };
  const endpoints = new Endpoints(schema);
  const pairs = endpoints.declared.get(type) ?? [];
  if (!pairs.length) return { offered: labels, data: labels };
  const covered = (label: string) => {
    const chain = lineage(label, schema.label_hierarchy);
    if (end === "src") return pairs.some((pair) => chain.includes(pair.src));
    if (end.src) return endpoints.check(type, end.src, label).status === "declared";
    return pairs.some((pair) => chain.includes(pair.dst));
  };
  const declared = labels.filter(covered);
  const others = labels.filter((label) => !declared.includes(label));
  if (!declared.length) return { offered: labels, data: labels };
  if (schema.strict_endpoints || !others.length) return { offered: declared, data: declared };
  return {
    offered: [...declared, ...others],
    data: [
      { group: groups.declared, items: declared },
      { group: groups.others, items: others },
    ],
  };
}

/**
 * Loads a CSV file into the tenant: pick the file (its start read for the
 * header, the separator and a preview), say whether its rows are nodes or
 * relationships and which columns hold the ids, map the other columns to
 * properties; graphos checks the whole file with that mapping (a dry run,
 * again whenever it changes), then reads and writes it, with the upload's
 * progress and its answer.
 */
export function CsvImportDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const { api } = useTenantSession();
  const schema = useSchema().data;
  const definitions = usePropertyDefinitions().data;
  const refresh = useRefreshAfterImport();
  const [file, setFile] = useState<File | null>(null);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [choices, setChoices] = useState<Choices>(NO_CHOICES);
  const [run, setRun] = useState<Run | null>(null);
  const [check, setCheck] = useState<CsvImportResult | null>(null);
  const [checkSent, setCheckSent] = useState<number | null>(null);
  const [checkError, setCheckError] = useState<{ mapping: boolean; message: string } | null>(null);
  const sender = useRef<AbortController | null>(null);
  /** Counts the files (and separators) chosen: a read finishing after another began is dropped. */
  const readSeq = useRef(0);
  const running = run !== null && RUNNING.includes(run.phase);

  useEffect(
    () => () => {
      readSeq.current += 1;
      sender.current?.abort();
    },
    [],
  );

  const read = (file: File, text: string, whole: boolean, delimiter?: CsvDelimiter) => {
    setLoaded({ file, text, whole, table: headTable(text, whole, delimiter) });
    setChoices((current) => ({ ...NO_CHOICES, kind: current.kind }));
    setRun(null);
  };

  const choose = async (next: File | null) => {
    const seq = ++readSeq.current;
    setFile(next);
    setLoaded(null);
    setReadError(null);
    setRun(null);
    setReading(Boolean(next));
    if (!next) return;
    try {
      const whole = next.size <= HEAD_BYTES;
      const text = await readFileText(whole ? next : next.slice(0, HEAD_BYTES));
      if (seq !== readSeq.current) return;
      read(next, text, whole);
    } catch (error) {
      if (seq === readSeq.current) setReadError(messageOf(error));
    } finally {
      if (seq === readSeq.current) setReading(false);
    }
  };

  const set = (patch: Partial<Choices>) => setChoices((current) => ({ ...current, ...patch }));
  const setColumn = (index: number, choice: Partial<ColumnChoice>) =>
    setChoices((current) => ({
      ...current,
      columns: { ...current.columns, [index]: { ...current.columns[index], ...choice } },
    }));

  const header = loaded?.table.header ?? [];
  const rows = loaded?.table.rows ?? [];
  const hasRows = Boolean(loaded && (rows.length || !loaded.whole));
  const labels = schema?.vertex_labels ?? [];
  const edgeTypes = (schema?.edge_labels ?? []).filter((type) => !(schema?.rule_edge_labels ?? []).includes(type));
  const columnData = header.map((title, index) => ({ value: String(index), label: title || `#${index + 1}` }));

  const kind = choices.kind;
  const label = pick(choices.label, labels);
  const type = pick(choices.type, edgeTypes);
  const groups = { declared: t("csvImport.declaredFor", { type }), others: t("csvImport.otherLabels") };
  const sources = endLabels(schema, type, "src", groups);
  const srcLabel = pick(choices.srcLabel, sources.offered);
  const targets = endLabels(schema, type, { src: srcLabel }, groups);
  const dstLabel = pick(choices.dstLabel, targets.offered);
  const id = header.length ? (choices.id ?? guessColumn(header, ID_NAMES, 0)) : null;
  const src = choices.src ?? guessColumn(header, SOURCE_NAMES, 0);
  const dst = choices.dst ?? guessColumn(header, TARGET_NAMES, 1);
  const policy = policyOf(definitions, kind, kind === "nodes" ? label : type);

  const plan = useMemo<ImportPlan | null>(() => {
    if (!loaded || !header.length) return null;
    if (kind === "nodes") {
      if (!label || id === null) return null;
      return { kind, label, id, columns: planColumns(header, [id], kind, policy, choices.columns) };
    }
    if (!type || !srcLabel || !dstLabel) return null;
    const keys = [src, dst];
    return {
      kind,
      type,
      srcLabel,
      src,
      dstLabel,
      dst,
      columns: planColumns(header, keys, kind, policy, choices.columns),
    };
  }, [loaded, kind, label, id, type, srcLabel, src, dstLabel, dst, policy, choices.columns]);

  const query = useMemo(
    () => (plan && loaded ? importQuery(plan, loaded.table.header, loaded.table.delimiter) : null),
    [plan, loaded],
  );

  useEffect(() => {
    setCheck(null);
    setCheckError(null);
    setCheckSent(null);
    if (!plan || !query || !loaded || !hasRows) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setCheckSent(0);
      api
        .csvImport(plan.kind, loaded.file, query, {
          dryRun: true,
          signal: controller.signal,
          onSent: (bytes, total) => setCheckSent(total ? bytes / total : 1),
        })
        .then(setCheck, (error: unknown) => {
          if (controller.signal.aborted) return;
          setCheckError({ mapping: error instanceof ApiError && error.status === 422, message: messageOf(error) });
        })
        .finally(() => {
          if (!controller.signal.aborted) setCheckSent(null);
        });
    }, CHECK_DELAY_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [api, plan, query, loaded, hasRows]);

  const start = async () => {
    if (!plan || !query || !loaded) return;
    const controller = new AbortController();
    sender.current = controller;
    setRun({ phase: "sending", share: 0 });
    let received = false;
    try {
      const result = await api.csvImport(plan.kind, loaded.file, query, {
        signal: controller.signal,
        onSent: (bytes, total) => {
          received = bytes >= total;
          setRun(received ? { phase: "writing", share: 1 } : { phase: "sending", share: total ? bytes / total : 0 });
        },
      });
      setRun({ phase: "done", share: 1, result });
    } catch (error) {
      // graphos writes nothing before it has the whole file; once it has, it may go on.
      if (controller.signal.aborted) setRun({ phase: received ? "stopped" : "cancelled", share: 0 });
      else setRun({ phase: "failed", share: 0, error: messageOf(error) });
    } finally {
      if (received) void refresh();
      if (sender.current === controller) sender.current = null;
    }
  };

  const close = () => {
    if (!running) onClose();
  };

  const kindData = (["nodes", "edges"] as const).map((value) => ({ value, label: t(`csvImport.kinds.${value}`) }));
  const delimiterData = (Object.keys(DELIMITER_NAMES) as CsvDelimiter[]).map((value) => ({
    value,
    label: t(`csvImport.delimiters.${DELIMITER_NAMES[value]}`),
  }));
  const missing = kind === "nodes" ? !labels.length : !edgeTypes.length || !labels.length;

  return (
    <Modal
      opened
      onClose={close}
      title={t("csvImport.title")}
      size="xl"
      radius="lg"
      closeOnClickOutside={!running}
      closeOnEscape={!running}
      withCloseButton={!running}
    >
      <Stack gap="sm">
        <FileInput
          size="xs"
          label={t("csvImport.file")}
          placeholder={t("csvImport.filePlaceholder")}
          accept=".csv,.tsv,.txt,text/csv,text/tab-separated-values"
          leftSection={<IconFileUpload size={14} />}
          value={file}
          onChange={(next) => void choose(next)}
          error={readError ? t("csvImport.readFailed", { message: readError }) : null}
          disabled={running}
          clearable
        />
        {reading ? (
          <Text size="xs" c="dimmed">
            {t("csvImport.reading")}
          </Text>
        ) : null}

        {loaded ? (
          <Group justify="space-between" align="flex-end" gap="sm">
            <Text size="xs" c="dimmed">
              {t("csvImport.detected", {
                columns: formatCount(header.length),
                size: formatBytes(loaded.file.size),
              })}
            </Text>
            <Stack gap={2}>
              <Text size="xs" fw={500}>
                {t("csvImport.separator")}
              </Text>
              <SegmentedControl
                size="xs"
                data={delimiterData}
                value={loaded.table.delimiter}
                disabled={running}
                onChange={(next) => read(loaded.file, loaded.text, loaded.whole, next as CsvDelimiter)}
              />
            </Stack>
          </Group>
        ) : null}
        {loaded?.table.openQuote ? (
          <Alert color="yellow" variant="light" radius="md">
            <Text size="xs">{t("csvImport.openQuote")}</Text>
          </Alert>
        ) : null}
        {loaded && !hasRows ? (
          <Text size="xs" c="red">
            {t("csvImport.empty")}
          </Text>
        ) : null}

        {hasRows ? (
          <>
            <Stack gap={2}>
              <Text size="xs" fw={500}>
                {t("csvImport.rowsAre")}
              </Text>
              <SegmentedControl
                size="xs"
                data={kindData}
                value={kind}
                disabled={running}
                onChange={(next) => set({ kind: next as RowKind, columns: {} })}
              />
            </Stack>
            {missing ? (
              <Text size="xs" c="dimmed">
                {t(kind === "nodes" || !labels.length ? "csvImport.noLabels" : "csvImport.noTypes")}
              </Text>
            ) : kind === "nodes" ? (
              <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
                <Select
                  size="xs"
                  label={t("csvImport.label")}
                  data={labels}
                  value={label}
                  searchable
                  allowDeselect={false}
                  onChange={(next) => set({ label: next, columns: {} })}
                />
                <Select
                  size="xs"
                  label={t("csvImport.idColumn")}
                  data={columnData}
                  value={id === null ? null : String(id)}
                  allowDeselect={false}
                  onChange={(next) => next !== null && set({ id: Number(next) })}
                />
              </SimpleGrid>
            ) : (
              <Stack gap="xs">
                <Select
                  size="xs"
                  label={t("csvImport.relationship")}
                  data={edgeTypes}
                  value={type}
                  searchable
                  allowDeselect={false}
                  onChange={(next) => set({ type: next, srcLabel: null, dstLabel: null, columns: {} })}
                />
                <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
                  <Select
                    size="xs"
                    label={t("csvImport.sourceLabel")}
                    data={sources.data}
                    value={srcLabel}
                    searchable
                    allowDeselect={false}
                    onChange={(next) => set({ srcLabel: next })}
                  />
                  <Select
                    size="xs"
                    label={t("csvImport.sourceColumn")}
                    data={columnData}
                    value={String(src)}
                    allowDeselect={false}
                    onChange={(next) => next !== null && set({ src: Number(next) })}
                  />
                  <Select
                    size="xs"
                    label={t("csvImport.targetLabel")}
                    data={targets.data}
                    value={dstLabel}
                    searchable
                    allowDeselect={false}
                    onChange={(next) => set({ dstLabel: next })}
                  />
                  <Select
                    size="xs"
                    label={t("csvImport.targetColumn")}
                    data={columnData}
                    value={String(dst)}
                    allowDeselect={false}
                    onChange={(next) => next !== null && set({ dst: Number(next) })}
                  />
                </SimpleGrid>
              </Stack>
            )}

            {plan ? (
              <>
                <Stack gap={4}>
                  <Text size="sm" fw={600}>
                    {t("csvImport.columns.title")}
                  </Text>
                  <CsvColumnGrid columns={plan.columns} example={rows[0]} policy={policy} onChange={setColumn} />
                </Stack>
                <CsvChecks check={check} sending={checkSent} error={checkError} />
                <CsvPreview plan={plan} header={header} rows={rows} />
              </>
            ) : null}
          </>
        ) : null}

        {run ? <RunProgress run={run} /> : null}
        {run?.result ? <CsvImportAnswer result={run.result} /> : null}
        {run?.phase === "failed" ? (
          <Alert color="red" variant="light" radius="md">
            <Text size="xs">{t("csvImport.failed", { message: run.error })}</Text>
          </Alert>
        ) : null}

        <Group justify="flex-end" gap="xs" mt="xs">
          {running ? (
            <Button size="xs" variant="subtle" color="red" onClick={() => sender.current?.abort()}>
              {t("common.actions.cancel")}
            </Button>
          ) : (
            <Button size="xs" variant="default" onClick={onClose}>
              {t("common.actions.close")}
            </Button>
          )}
          <Button
            size="xs"
            leftSection={<IconTableImport size={14} />}
            disabled={!plan || !hasRows || missing || running || Boolean(checkError?.mapping)}
            loading={running}
            onClick={() => void start()}
          >
            {check
              ? t("csvImport.import", { count: check.rows, rows: formatCount(check.rows) })
              : t("csvImport.importFile")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function RunProgress({ run }: { run: Run }) {
  const { t } = useTranslation();
  const color =
    run.phase === "failed"
      ? "red"
      : run.phase === "stopped"
        ? "yellow"
        : run.phase === "cancelled"
          ? "gray"
          : undefined;
  return (
    <Stack gap={4} aria-live="polite">
      <Text size="xs" fw={600}>
        {t(`csvImport.progress.${run.phase}`, { percent: Math.floor(run.share * 100) })}
      </Text>
      <Progress
        size="sm"
        value={run.phase === "done" || run.phase === "writing" ? 100 : run.share * 100}
        color={color}
        animated={run.phase === "writing"}
      />
    </Stack>
  );
}
