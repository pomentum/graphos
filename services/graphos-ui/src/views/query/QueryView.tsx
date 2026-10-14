import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  Menu,
  Modal,
  Radio,
  Stack,
  Table,
  Text,
  Textarea,
  TextInput,
  Title,
  UnstyledButton,
} from "@mantine/core";
import {
  IconDots,
  IconDeviceFloppy,
  IconFileSpreadsheet,
  IconPlayerPlay,
  IconTopologyStar3,
} from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import type { CypherResult, SavedQuery } from "../../api/types";
import { useServiceInfo } from "../../data/queries";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { CSV_TYPE, exportName, toCsv } from "../../lib/csv";
import { saveFile } from "../../lib/download";
import { formatCount } from "../../lib/time";
import { isApple } from "../../lib/platform";
import { useTenantSession } from "../../session/SessionProvider";
import { cellText, paramValue, queryParams } from "./cypherText";
import classes from "./QueryView.module.css";

const SAVED_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/;

// The query, its parameters and the last result, per tenant, while the page
// is open: coming back to the view finds them (nothing in browser storage).
const memory = new Map<string, { query: string; params: Record<string, string>; result: CypherResult | null }>();

/**
 * Query: read-only Cypher on this tenant's graph, for people who know
 * Cypher (when graphos's console is on). Results as a table (CSV) and on the
 * Explore canvas; saved queries beside, run with a form of their parameters.
 * graphos refuses writes and calls of database functions, and stops a query
 * that runs too long.
 */
export function QueryView() {
  const { t } = useTranslation();
  const { api, tenant, canMaintain } = useTenantSession();
  const info = useServiceInfo();
  const kept = memory.get(tenant);
  const [query, setQuery] = useState(kept?.query ?? "MATCH (n) RETURN n LIMIT 25");
  const [params, setParams] = useState<Record<string, string>>(kept?.params ?? {});
  const [result, setResult] = useState<CypherResult | null>(kept?.result ?? null);
  const [active, setActive] = useState<SavedQuery | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);
  const stop = useRef<AbortController | null>(null);
  const client = useQueryClient();
  const saved = useQuery({ queryKey: ["cypher-saved", tenant], queryFn: () => api.savedQueries() });
  const remember = (next: Partial<{ query: string; params: Record<string, string>; result: CypherResult | null }>) =>
    memory.set(tenant, { query, params, result, ...next });

  if (info.data && !info.data.features.cypher) {
    return (
      <div className={classes.root}>
        <Alert color="gray" variant="light" radius="md">
          {t("views.query.off")}
        </Alert>
      </div>
    );
  }

  const names = queryParams(query);
  const declared = active && active.query === query ? active.params : {};

  const run = async (event?: FormEvent) => {
    event?.preventDefault();
    if (running) return;
    const controller = new AbortController();
    stop.current = controller;
    setRunning(true);
    setError(null);
    try {
      const values = Object.fromEntries(names.map((n) => [n, paramValue(params[n] ?? "", declared[n]?.type)]));
      const answer =
        active && active.query === query
          ? await api.runSavedQuery(active.name, values, controller.signal)
          : await api.cypher(query, values, { signal: controller.signal });
      setResult(answer);
      remember({ result: answer });
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      stop.current = null;
      setRunning(false);
    }
  };

  const choose = (item: SavedQuery) => {
    setActive(item);
    setQuery(item.query);
    remember({ query: item.query });
  };

  const csv = () => {
    if (!result) return;
    saveFile(
      exportName([tenant, "query"], "csv"),
      toCsv(
        result.columns,
        result.rows.map((row) => row.map(cellText)),
      ),
      CSV_TYPE,
    );
  };

  return (
    <div className={classes.root}>
      <section className={classes.main} aria-label={t("views.query.title")}>
        <div>
          <Title order={1} size="h4">
            {t("views.query.title")}
          </Title>
          <Text size="xs" c="dimmed">
            {t("views.query.hint")}
          </Text>
        </div>
        <form onSubmit={(event) => void run(event)}>
          <Stack gap="xs">
            <Textarea
              className={classes.editor}
              aria-label={t("views.query.editor")}
              value={query}
              autosize
              minRows={5}
              maxRows={18}
              spellCheck={false}
              onChange={(event) => {
                setQuery(event.currentTarget.value);
                remember({ query: event.currentTarget.value });
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) void run();
              }}
            />
            {names.length ? (
              <Group gap="xs" align="flex-end">
                {names.map((name) => (
                  <TextInput
                    key={name}
                    size="xs"
                    label={declared[name]?.label || `$${name}`}
                    value={params[name] ?? ""}
                    onChange={(event) => {
                      const next = { ...params, [name]: event.currentTarget.value };
                      setParams(next);
                      remember({ params: next });
                    }}
                  />
                ))}
              </Group>
            ) : null}
            <Group justify="space-between" gap="xs">
              <Text size="xs" c="dimmed">
                {t("views.query.keys", { key: isApple() ? "⌘↩" : "Ctrl+Enter" })}
              </Text>
              <Group gap="xs">
                {canMaintain ? (
                  <Button
                    size="xs"
                    variant="default"
                    leftSection={<IconDeviceFloppy size={14} />}
                    onClick={() => setSaving(true)}
                    disabled={!query.trim()}
                  >
                    {t("views.query.save")}
                  </Button>
                ) : null}
                {running ? (
                  <Button size="xs" color="red" variant="light" onClick={() => stop.current?.abort()}>
                    {t("common.actions.cancel")}
                  </Button>
                ) : null}
                <Button
                  size="xs"
                  type="submit"
                  loading={running}
                  disabled={!query.trim()}
                  leftSection={<IconPlayerPlay size={14} />}
                >
                  {t("views.query.run")}
                </Button>
              </Group>
            </Group>
          </Stack>
        </form>

        {error ? (
          <Alert color="red" variant="light" radius="md" title={t("views.query.failed")}>
            <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
              {error}
            </Text>
          </Alert>
        ) : null}

        {result ? (
          <Results result={result} onCsv={csv} preferGraph={active?.query === query && active.show === "graph"} />
        ) : null}
      </section>

      <aside className={classes.saved} aria-label={t("views.query.savedTitle")}>
        <Title order={2} size="sm">
          {t("views.query.savedTitle")}
        </Title>
        {saved.data?.length ? (
          saved.data.map((item) => (
            <Group key={item.name} gap={4} wrap="nowrap" align="flex-start">
              <UnstyledButton
                className={classes.savedItem}
                data-active={active?.name === item.name || undefined}
                onClick={() => choose(item)}
              >
                <Group gap={6} wrap="nowrap">
                  <Text size="sm" fw={600} truncate>
                    {item.name}
                  </Text>
                  {Object.keys(item.params).length ? (
                    <Badge size="xs" variant="light" color="gray">
                      {t("views.query.paramsCount", { count: Object.keys(item.params).length })}
                    </Badge>
                  ) : null}
                </Group>
                {item.description ? (
                  <Text size="xs" c="dimmed" lineClamp={2}>
                    {item.description}
                  </Text>
                ) : null}
              </UnstyledButton>
              {canMaintain ? (
                <Menu position="bottom-end">
                  <Menu.Target>
                    <ActionIcon variant="subtle" color="gray" aria-label={t("views.query.more", { name: item.name })}>
                      <IconDots size={14} />
                    </ActionIcon>
                  </Menu.Target>
                  <Menu.Dropdown>
                    <Menu.Item
                      color="red"
                      onClick={() =>
                        void api.deleteSavedQuery(item.name).then(() => {
                          if (active?.name === item.name) setActive(null);
                          return client.invalidateQueries({ queryKey: ["cypher-saved", tenant] });
                        })
                      }
                    >
                      {t("common.actions.delete")}
                    </Menu.Item>
                  </Menu.Dropdown>
                </Menu>
              ) : null}
            </Group>
          ))
        ) : (
          <Text size="xs" c="dimmed">
            {t("views.query.noSaved")}
          </Text>
        )}
      </aside>

      {saving ? (
        <SaveQueryDialog
          query={query}
          names={names}
          initial={active}
          onClose={() => setSaving(false)}
          onSaved={(item) => {
            setActive(item);
            setSaving(false);
            void client.invalidateQueries({ queryKey: ["cypher-saved", tenant] });
          }}
        />
      ) : null}
    </div>
  );
}

/** The result: how many rows and how fast, CSV, the canvas, and the table. */
function Results({ result, onCsv, preferGraph }: { result: CypherResult; onCsv: () => void; preferGraph: boolean }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { model, select, setMode, frameOn } = useWorkspace();
  const openOnCanvas = () => {
    const keys = model.addGroup(
      t("views.query.history"),
      result.graph.nodes.map((n) => ({ ref: { id: n.id, label: n.label }, properties: n.properties })),
      result.graph.edges.map((e) => ({
        type: e.type,
        src: { id: e.src_id, label: e.src_label ?? "" },
        dst: { id: e.dst_id, label: e.dst_label ?? "" },
        properties: e.properties,
      })),
    );
    select(null);
    setMode("graph");
    frameOn(keys);
    void navigate("/explore");
  };
  return (
    <>
      <Group justify="space-between" gap="xs">
        <Text size="xs" c="dimmed">
          {t("views.query.summary", {
            count: result.rows.length,
            rows: formatCount(result.rows.length),
            ms: result.elapsed_ms,
          })}
          {result.truncated ? ` · ${t("views.query.truncated")}` : ""}
        </Text>
        <Group gap="xs">
          <Button
            size="xs"
            variant={preferGraph ? "filled" : "default"}
            leftSection={<IconTopologyStar3 size={14} />}
            disabled={result.graph.nodes.length === 0}
            onClick={openOnCanvas}
          >
            {t("views.query.openOnCanvas", { count: result.graph.nodes.length })}
          </Button>
          <Button
            size="xs"
            variant="default"
            leftSection={<IconFileSpreadsheet size={14} />}
            disabled={result.rows.length === 0}
            onClick={onCsv}
          >
            {t("common.export.csv")}
          </Button>
        </Group>
      </Group>
      <div className={classes.table}>
        <Table stickyHeader striped highlightOnHover fz="sm">
          <Table.Thead>
            <Table.Tr>
              {result.columns.map((column) => (
                <Table.Th key={column}>{column}</Table.Th>
              ))}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {result.rows.map((row, i) => (
              <Table.Tr key={i}>
                {row.map((cell, j) => {
                  const text = cellText(cell);
                  return (
                    <Table.Td key={j} className={classes.cell} title={text}>
                      {text}
                    </Table.Td>
                  );
                })}
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
        {result.rows.length === 0 ? (
          <Text size="sm" c="dimmed" ta="center" py="xl">
            {t("views.query.noRows")}
          </Text>
        ) : null}
      </div>
    </>
  );
}

/** Saves the query under a name, for the tenant's readers (maintainers). */
function SaveQueryDialog({
  query,
  names,
  initial,
  onClose,
  onSaved,
}: {
  query: string;
  names: string[];
  initial: SavedQuery | null;
  onClose: () => void;
  onSaved: (item: SavedQuery) => void;
}) {
  const { t } = useTranslation();
  const { api } = useTenantSession();
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [show, setShow] = useState<"table" | "graph">(initial?.show ?? "table");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const nameError = name && !SAVED_NAME.test(name) ? t("views.query.badName") : null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name || nameError) return;
    setBusy(true);
    setError(null);
    try {
      const params = Object.fromEntries(
        names.map((n) => [
          n,
          initial?.query === query && initial.params[n] ? initial.params[n] : { type: "string" as const },
        ]),
      );
      onSaved(await api.saveQuery(name, { description, query, params, show }));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal opened onClose={onClose} title={t("views.query.saveTitle")} centered radius="lg">
      <form onSubmit={(event) => void submit(event)}>
        <Stack gap="sm">
          <TextInput
            label={t("views.query.name")}
            description={t("views.query.nameHint")}
            value={name}
            error={nameError}
            onChange={(event) => setName(event.currentTarget.value)}
            data-autofocus
          />
          <Textarea
            label={t("views.query.description")}
            value={description}
            autosize
            minRows={2}
            onChange={(event) => setDescription(event.currentTarget.value)}
          />
          <Radio.Group label={t("views.query.show")} value={show} onChange={(v) => setShow(v as "table" | "graph")}>
            <Group gap="md" mt={4}>
              <Radio value="table" label={t("views.query.showTable")} />
              <Radio value="graph" label={t("views.query.showGraph")} />
            </Group>
          </Radio.Group>
          {names.length ? (
            <Text size="xs" c="dimmed">
              {t("views.query.savedParams", { names: names.map((n) => `$${n}`).join(", ") })}
            </Text>
          ) : null}
          {error ? (
            <Alert color="red" variant="light" radius="md">
              {error}
            </Alert>
          ) : null}
          <Group justify="flex-end" gap="xs">
            <Button variant="default" onClick={onClose}>
              {t("common.actions.cancel")}
            </Button>
            <Button type="submit" loading={busy} disabled={!name || Boolean(nameError)}>
              {t("views.query.saveConfirm")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}
