import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Loader,
  Menu,
  Table,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { IconDots, IconRefresh } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TableStat, VacuumRequest } from "../../api/types";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ListFilter, matches } from "../../components/ListFilter";
import { formatBytes, formatCount, relativeTime } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";
import classes from "./DatabaseView.module.css";

/** Dead rows beyond this share of a table's rows are flagged: a vacuum would win space back. */
const DEAD_SHARE = 0.2;
/** The slowest statements listed. */
const SLOW_LIMIT = 20;

const qualified = (table: TableStat) => `${table.schema_name}.${table.table_name}`;

/** The table's last vacuum, by hand or by autovacuum, whichever is later. */
export function lastVacuum(table: TableStat): string | null {
  const times = [table.last_vacuum, table.last_autovacuum].filter((v): v is string => Boolean(v));
  return times.length ? times.sort().at(-1)! : null;
}

/** The share of a table's rows that are dead, 0 when it has none. */
export function deadShare(table: TableStat): number {
  const all = table.n_live_tuples + table.n_dead_tuples;
  return all ? table.n_dead_tuples / all : 0;
}

/**
 * The database, for admins: its size and versions, each table's rows, dead
 * rows, size and last vacuum, a vacuum (of one table or all), and the
 * slowest statements (from pg_stat_statements).
 */
export function DatabaseView() {
  const { t } = useTranslation();
  const { api } = useTenantSession();
  const client = useQueryClient();
  const stats = useQuery({ queryKey: ["db", "stats"], queryFn: () => api.dbStats() });
  const slow = useQuery({ queryKey: ["db", "slow"], queryFn: () => api.slowQueries(SLOW_LIMIT) });
  const [filter, setFilter] = useState("");
  // A vacuum waiting for its confirmation: FULL locks the table, and all of it takes a while.
  const [asking, setAsking] = useState<VacuumRequest | null>(null);
  const vacuum = useMutation({
    meta: { errorTitle: "views.database.vacuum.failed" },
    mutationFn: (request: VacuumRequest) => api.vacuum(request),
    onSuccess: () => setAsking(null),
    onSettled: () => client.invalidateQueries({ queryKey: ["db"] }),
  });
  const run = (request: VacuumRequest) =>
    request.full || !request.tables ? setAsking(request) : vacuum.mutate(request);

  const tables = [...(stats.data?.tables ?? [])]
    .sort((a, b) => b.total_bytes - a.total_bytes)
    .filter((table) => matches(filter, qualified(table)));

  return (
    <div className={classes.root}>
      <Group justify="space-between" align="flex-start" mb="md">
        <div>
          <Title order={1} size="h4">
            {t("views.database.title")}
          </Title>
          <Text size="xs" c="dimmed">
            {stats.data
              ? t("views.database.summary", {
                  postgres: stats.data.postgres_version,
                  age: stats.data.age_version ?? "?",
                  size: formatBytes(stats.data.database_size_bytes),
                })
              : t("views.database.hint")}
          </Text>
        </div>
        <Group gap="xs">
          <Button
            size="xs"
            variant="default"
            leftSection={<IconRefresh size={14} />}
            onClick={() => void client.invalidateQueries({ queryKey: ["db"] })}
          >
            {t("views.database.refresh")}
          </Button>
          <Button size="xs" variant="default" loading={vacuum.isPending} onClick={() => run({ analyze: true })}>
            {t("views.database.vacuum.all")}
          </Button>
        </Group>
      </Group>

      <section>
        <Group justify="space-between" mb="xs">
          <Title order={2} size="sm">
            {t("views.database.tables")} · {formatCount(stats.data?.tables.length ?? 0)}
          </Title>
          <ListFilter value={filter} onChange={setFilter} label={t("views.database.filter")} />
        </Group>
        {stats.isLoading ? <Loader size="sm" /> : null}
        {stats.error ? (
          <Alert color="red" variant="light">
            {stats.error.message}
          </Alert>
        ) : null}
        {stats.data ? (
          <div className={classes.group}>
            <Table verticalSpacing={5} highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t("views.database.columns.table")}</Table.Th>
                  <Table.Th ta="right">{t("views.database.columns.rows")}</Table.Th>
                  <Table.Th ta="right">{t("views.database.columns.dead")}</Table.Th>
                  <Table.Th ta="right">{t("views.database.columns.size")}</Table.Th>
                  <Table.Th>{t("views.database.columns.vacuumed")}</Table.Th>
                  <Table.Th w={40} />
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {tables.map((table) => {
                  const share = deadShare(table);
                  const last = lastVacuum(table);
                  return (
                    <Table.Tr key={qualified(table)}>
                      <Table.Td>
                        <Text size="sm" className="gx-mono">
                          {qualified(table)}
                        </Text>
                      </Table.Td>
                      <Table.Td ta="right">{formatCount(table.n_live_tuples)}</Table.Td>
                      <Table.Td ta="right">
                        {formatCount(table.n_dead_tuples)}
                        {share > DEAD_SHARE ? (
                          <Tooltip label={t("views.database.deadTooltip")} withArrow multiline w={240}>
                            <Badge ml={6} size="xs" color="orange" variant="light">
                              {Math.round(share * 100)}%
                            </Badge>
                          </Tooltip>
                        ) : null}
                      </Table.Td>
                      <Table.Td ta="right">{formatBytes(table.total_bytes)}</Table.Td>
                      <Table.Td>
                        <Text size="xs" c="dimmed">
                          {last ? relativeTime(last) : t("views.database.never")}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <Menu position="bottom-end">
                          <Menu.Target>
                            <ActionIcon
                              variant="subtle"
                              color="gray"
                              aria-label={t("views.database.actionsFor", { table: qualified(table) })}
                            >
                              <IconDots size={16} />
                            </ActionIcon>
                          </Menu.Target>
                          <Menu.Dropdown>
                            <Menu.Item onClick={() => run({ tables: [qualified(table)], analyze: true })}>
                              {t("views.database.vacuum.one")}
                            </Menu.Item>
                            <Menu.Item
                              color="red"
                              onClick={() => run({ tables: [qualified(table)], analyze: true, full: true })}
                            >
                              {t("views.database.vacuum.full")}
                            </Menu.Item>
                          </Menu.Dropdown>
                        </Menu>
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </div>
        ) : null}
      </section>

      <section className={classes.section}>
        <Title order={2} size="sm" mb={4}>
          {t("views.database.slow")}
        </Title>
        <Text size="xs" c="dimmed" mb="xs">
          {t("views.database.slowHint")}
        </Text>
        {slow.isLoading ? <Loader size="sm" /> : null}
        {slow.error ? (
          <Alert color="red" variant="light">
            {slow.error.message}
          </Alert>
        ) : null}
        {slow.data && !slow.data.available ? (
          <Alert color="gray" variant="light">
            {slow.data.note ?? t("views.database.slowUnavailable")}
          </Alert>
        ) : null}
        {slow.data?.available ? (
          <div className={classes.group}>
            <Table verticalSpacing={5}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th ta="right">{t("views.database.columns.mean")}</Table.Th>
                  <Table.Th ta="right">{t("views.database.columns.calls")}</Table.Th>
                  <Table.Th ta="right">{t("views.database.columns.total")}</Table.Th>
                  <Table.Th>{t("views.database.columns.statement")}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {slow.data.queries.map((query, i) => (
                  <Table.Tr key={i}>
                    <Table.Td ta="right">{query.mean_exec_ms.toFixed(1)} ms</Table.Td>
                    <Table.Td ta="right">{formatCount(query.calls)}</Table.Td>
                    <Table.Td ta="right">{(query.total_exec_ms / 1000).toFixed(1)} s</Table.Td>
                    <Table.Td className={classes.statement}>
                      <Code className={classes.code} title={query.query}>
                        {query.query}
                      </Code>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </div>
        ) : null}
      </section>

      <ConfirmDialog
        opened={asking !== null}
        title={
          asking?.full
            ? t("views.database.vacuum.fullTitle", { table: asking.tables?.[0] ?? "" })
            : t("views.database.vacuum.allTitle")
        }
        message={asking?.full ? t("views.database.vacuum.fullText") : t("views.database.vacuum.allText")}
        confirmLabel={asking?.full ? t("views.database.vacuum.full") : t("views.database.vacuum.all")}
        busy={vacuum.isPending}
        onConfirm={() => asking && vacuum.mutate(asking)}
        onClose={() => setAsking(null)}
      />
    </div>
  );
}
