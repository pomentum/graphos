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
import type { BulkEdgesResult, BulkNodesResult, TenantSchema } from "../../api/types";
import { usePropertyDefinitions, useRefreshAfterImport, useSchema } from "../../data/queries";
import { parseCsvAsync, readFileText, type CsvDelimiter, type CsvTable } from "../../lib/csv";
import {
  ID_NAMES,
  SOURCE_NAMES,
  TARGET_NAMES,
  checkRowsAsync,
  guessColumn,
  planColumns,
  policyOf,
  prepareImport,
  type ColumnChoice,
  type ImportCheck,
  type ImportPlan,
  type RowKind,
} from "../../lib/csvImport";
import { Endpoints, lineage } from "../../lib/endpoints";
import { formatCount } from "../../lib/time";
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

const DELIMITER_NAMES: Record<CsvDelimiter, "comma" | "semicolon" | "tab"> = {
  ",": "comma",
  ";": "semicolon",
  "\t": "tab",
};

/** The file as read: its text (to read it again with another separator) and its table. */
interface Loaded {
  name: string;
  text: string;
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

type Phase = "preparing" | "sending" | "writing" | "done" | "cancelled" | "stopped" | "failed";

interface Run {
  phase: Phase;
  share: number;
  result?: BulkNodesResult | BulkEdgesResult;
  error?: string;
  /** The file's rows and the lines they were sent as, with the row each line comes from. */
  sent?: { rows: number; lines: number; rowOfLine: number[] };
}

/** How long the mapping stays unchanged before the rows are checked again. */
const CHECK_DELAY_MS = 200;

const RUNNING: Phase[] = ["preparing", "sending", "writing"];

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
 * Loads a CSV file into the tenant: pick the file (its separator detected),
 * say whether its rows are nodes or relationships and which columns hold
 * the ids, map the other columns to properties, look at the first rows and
 * at the checks over all of them, then send every row to graphos's NDJSON
 * bulk endpoint, with its progress and its answer.
 */
export function CsvImportDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const { api } = useTenantSession();
  const schema = useSchema().data;
  const definitions = usePropertyDefinitions().data;
  const refresh = useRefreshAfterImport();
  const [file, setFile] = useState<File | null>(null);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [reading, setReading] = useState<number | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [choices, setChoices] = useState<Choices>(NO_CHOICES);
  const [run, setRun] = useState<Run | null>(null);
  const [check, setCheck] = useState<ImportCheck | null>(null);
  const [checking, setChecking] = useState(0);
  const reader = useRef<AbortController | null>(null);
  const sender = useRef<AbortController | null>(null);
  /** Counts the files (and separators) chosen: a read finishing after another began is dropped. */
  const readSeq = useRef(0);
  const running = run !== null && RUNNING.includes(run.phase);

  useEffect(
    () => () => {
      readSeq.current += 1;
      reader.current?.abort();
      sender.current?.abort();
    },
    [],
  );

  const read = async (name: string, text: string, delimiter?: CsvDelimiter, seq = ++readSeq.current) => {
    reader.current?.abort();
    const controller = new AbortController();
    reader.current = controller;
    setReading(0);
    try {
      const table = await parseCsvAsync(text, {
        delimiter,
        signal: controller.signal,
        onProgress: (share) => setReading(share),
      });
      if (controller.signal.aborted || seq !== readSeq.current) return;
      setLoaded({ name, text, table });
      setChoices((current) => ({ ...NO_CHOICES, kind: current.kind }));
      setRun(null);
    } catch (error) {
      if (!controller.signal.aborted) setReadError(error instanceof Error ? error.message : String(error));
    } finally {
      if (reader.current === controller) setReading(null);
    }
  };

  const choose = async (next: File | null) => {
    const seq = ++readSeq.current;
    setFile(next);
    setLoaded(null);
    setReadError(null);
    setRun(null);
    reader.current?.abort();
    setReading(next ? 0 : null);
    if (!next) return;
    try {
      const text = await readFileText(next);
      if (seq === readSeq.current) await read(next.name, text, undefined, seq);
    } catch (error) {
      if (seq === readSeq.current) {
        setReading(null);
        setReadError(error instanceof Error ? error.message : String(error));
      }
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

  // `header` and `rows` change only with `loaded`, which `plan` follows.
  useEffect(() => {
    setCheck(null);
    setChecking(0);
    if (!plan) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      checkRowsAsync(plan, header, rows, policy, schema, {
        signal: controller.signal,
        onProgress: setChecking,
      }).then(setCheck, () => undefined);
    }, CHECK_DELAY_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [plan, policy, schema]);

  const start = async () => {
    if (!plan) return;
    const controller = new AbortController();
    sender.current = controller;
    setRun({ phase: "preparing", share: 0 });
    let sending = false;
    try {
      const prepared = await prepareImport(plan, rows, controller.signal);
      const sent = { rows: rows.length, lines: prepared.lines, rowOfLine: prepared.rowOfLine };
      sending = true;
      setRun({ phase: "sending", share: 0, sent });
      const progress = {
        signal: controller.signal,
        onSent: (bytes: number, total: number) =>
          setRun(
            bytes >= total
              ? { phase: "writing", share: 1, sent }
              : { phase: "sending", share: total ? bytes / total : 0, sent },
          ),
      };
      const body = prepared.body;
      const result = plan.kind === "nodes" ? await api.bulkNodes(body, progress) : await api.bulkEdges(body, progress);
      setRun({ phase: "done", share: 1, result, sent });
    } catch (error) {
      if (controller.signal.aborted) setRun({ phase: sending ? "stopped" : "cancelled", share: 0 });
      else setRun({ phase: "failed", share: 0, error: error instanceof Error ? error.message : String(error) });
    } finally {
      // graphos writes as it reads: after an answer, a stop or a failure, what it wrote is shown.
      if (sending) void refresh();
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
        {reading !== null ? (
          <Text size="xs" c="dimmed">
            {t("csvImport.reading", { percent: Math.floor(reading * 100) })}
          </Text>
        ) : null}

        {loaded ? (
          <Group justify="space-between" align="flex-end" gap="sm">
            <Text size="xs" c="dimmed">
              {t("csvImport.detected", {
                count: rows.length,
                rows: formatCount(rows.length),
                columns: formatCount(header.length),
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
                disabled={running || reading !== null}
                onChange={(next) => void read(loaded.name, loaded.text, next as CsvDelimiter)}
              />
            </Stack>
          </Group>
        ) : null}
        {loaded?.table.openQuote ? (
          <Alert color="yellow" variant="light" radius="md">
            <Text size="xs">{t("csvImport.openQuote")}</Text>
          </Alert>
        ) : null}
        {loaded && !rows.length ? (
          <Text size="xs" c="red">
            {t("csvImport.empty")}
          </Text>
        ) : null}

        {loaded && rows.length ? (
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
                <CsvPreview plan={plan} header={header} rows={rows} />
                <CsvChecks check={check} progress={checking} />
              </>
            ) : null}
          </>
        ) : null}

        {run ? <RunProgress run={run} /> : null}
        {run?.result && run.sent ? <CsvImportAnswer result={run.result} sent={run.sent} /> : null}
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
            disabled={!plan || !rows.length || missing || running}
            loading={running}
            onClick={() => void start()}
          >
            {t("csvImport.import", { count: rows.length, rows: formatCount(rows.length) })}
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
        animated={run.phase === "writing" || run.phase === "preparing"}
      />
    </Stack>
  );
}
