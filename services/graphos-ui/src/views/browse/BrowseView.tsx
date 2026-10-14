import { Button, Group, Select, Skeleton, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconMap } from "@tabler/icons-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ColumnPicker } from "../../components/ColumnPicker";
import { NodeTable, type NodeRow } from "../../components/NodeTable";
import { useNodePage, usePropertyProfile, useSchema, useStats } from "../../data/queries";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { currentCursor, FIRST_PAGE, nextPage, previousPage } from "../../lib/pager";
import { formatCount } from "../../lib/time";
import { browseParams, defaultColumns, readBrowseState, scanFilter, type BrowseFilter } from "./browseState";
import classes from "./BrowseView.module.css";
import { MAX_OPEN_NODES, useOpenNodes } from "../explore/useOpenNodes";
import { BrowseExport } from "./BrowseExport";
import { FilterBar } from "./FilterBar";

/** Rows per page. */
const PAGE_SIZE = 50;

/**
 * Browse: the nodes of one label as a table, filtered on a property and
 * paged in id order. A row opens the node in Explore. The label and filter
 * live in the address, so a view can be bookmarked or sent to someone.
 */
export function BrowseView() {
  const { t } = useTranslation();
  const schema = useSchema();
  const stats = useStats();
  const [params, setParams] = useSearchParams();
  const state = readBrowseState(params);
  // Columns chosen per label, kept while the filter changes.
  const [columnsByLabel, setColumnsByLabel] = useState<Record<string, string[]>>({});

  // Labels come from the schema (quick); counts from stats when they arrive,
  // largest first. A failed stats request only costs the counts.
  const counts = new Map(
    (stats.data?.labels ?? []).filter((l) => l.kind === "v").map((l) => [l.label, l.count] as const),
  );
  const labels = [...(schema.data?.vertex_labels ?? [])].sort(
    (a, b) => (counts.get(b) ?? -1) - (counts.get(a) ?? -1) || a.localeCompare(b),
  );
  const settled = !stats.isLoading;
  const label = state.label ?? (settled ? (labels[0] ?? null) : null);
  const count = label ? counts.get(label) : undefined;
  const profile = usePropertyProfile(label);

  const show = (next: { label: string | null; filter: BrowseFilter | null }) =>
    setParams(browseParams(next), { replace: true });

  // The label on the map, selected: its counts and relationships at a glance.
  const navigate = useNavigate();
  const { setMapLabel, setMode } = useWorkspace();
  const showOnMap = () => {
    setMapLabel(label);
    setMode("map");
    void navigate("/explore");
  };

  return (
    <div className={classes.root}>
      <Group gap="sm" wrap="wrap">
        <Select
          size="xs"
          w={200}
          aria-label={t("views.browse.label")}
          placeholder={t("views.browse.label")}
          data={labels.map((l) => ({
            value: l,
            label: counts.has(l)
              ? t("views.browse.labelOption", { label: l, formatted: formatCount(counts.get(l)!) })
              : l,
          }))}
          value={label}
          onChange={(next) => show({ label: next, filter: null })}
          searchable
          selectFirstOptionOnChange
          allowDeselect={false}
        />
        <FilterBar
          key={JSON.stringify([label, state.filter])}
          properties={(profile.data ?? []).map((p) => p.name)}
          filter={state.filter}
          onApply={(filter) => show({ label, filter })}
        />
        {label ? (
          <Button size="xs" variant="default" leftSection={<IconMap size={14} />} onClick={showOnMap}>
            {t("views.browse.selection.showOnMap")}
          </Button>
        ) : null}
      </Group>

      {label === null ? (
        schema.error ? (
          <Text size="sm" c="red" className={classes.empty}>
            {schema.error.message}
          </Text>
        ) : !settled || schema.isLoading ? (
          <Skeleton h={240} radius="lg" />
        ) : (
          <Text c="dimmed" size="sm" className={classes.empty}>
            {t("views.browse.noLabels")}
          </Text>
        )
      ) : (
        <LabelPages
          key={JSON.stringify([label, state.filter])}
          label={label}
          filter={state.filter}
          total={state.filter ? undefined : count}
          columns={columnsByLabel[label] ?? null}
          onColumns={(columns) => setColumnsByLabel((current) => ({ ...current, [label]: columns }))}
        />
      )}
    </div>
  );
}

/**
 * The pages of one label under one filter, with the column picker and the
 * pager. Remounted (key) when the label or filter changes, which starts
 * again from the first page.
 */
function LabelPages({
  label,
  filter,
  total,
  columns: chosen,
  onColumns,
}: {
  label: string;
  filter: BrowseFilter | null;
  total?: number;
  /** The columns chosen for this label, or null for the most common properties. */
  columns: string[] | null;
  onColumns: (columns: string[]) => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { open } = useWorkspace();
  const openNodes = useOpenNodes();
  const profile = usePropertyProfile(label);
  const [pager, setPager] = useState(FIRST_PAGE);
  // Rows picked across pages, by id, for opening together.
  const [picked, setPicked] = useState<Map<string, NodeRow>>(() => new Map());
  const [opening, setOpening] = useState(false);

  // A filter's value is typed from the profile, so wait for it.
  const ready = !filter || profile.data !== undefined;
  const page = useNodePage(
    ready
      ? {
          labels: [label],
          where: filter ? [scanFilter(filter, profile.data ?? [])] : [],
          project: ["*"],
          limit: PAGE_SIZE,
          cursor: currentCursor(pager),
        }
      : null,
  );

  const names = (profile.data ?? []).map((p) => p.name).filter((name) => name !== "id");
  const columns = chosen ?? defaultColumns(profile.data ?? []);
  const rows = page.data?.items ?? [];
  const from = pager.page * PAGE_SIZE + 1;
  const nextCursor = page.data?.next_cursor ?? null;

  const openRow = (row: NodeRow) => {
    open({ id: row.id, label: row.label }, row.properties);
    void navigate("/explore");
  };

  const toggle = (row: NodeRow, on: boolean) =>
    setPicked((current) => {
      const next = new Map(current);
      if (on && next.size < MAX_OPEN_NODES) next.set(row.id, row);
      else next.delete(row.id);
      return next;
    });
  const toggleAll = (on: boolean) => {
    for (const row of rows) toggle(row, on);
  };

  /** The picked rows and the relationships among them, on the graph canvas. */
  const openPicked = async () => {
    setOpening(true);
    try {
      await openNodes(t("views.browse.selection.history", { count: picked.size, label }), label, [...picked.values()]);
      void navigate("/explore");
    } catch (error) {
      notifications.show({
        color: "red",
        title: t("views.browse.selection.openFailed"),
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setOpening(false);
    }
  };

  return (
    <>
      <Group justify="space-between" gap="sm">
        <Text size="xs" c="dimmed">
          {!ready || page.isLoading
            ? t("common.state.loading")
            : rows.length === 0
              ? t("views.browse.noNodes")
              : total !== undefined
                ? t("views.browse.rowsOf", {
                    from: formatCount(from),
                    to: formatCount(from + rows.length - 1),
                    total: formatCount(total),
                  })
                : t("views.browse.rows", { from: formatCount(from), to: formatCount(from + rows.length - 1) })}
        </Text>
        <Group gap="xs">
          {picked.size > 0 ? (
            <>
              <Text size="xs" c="dimmed">
                {t("views.browse.selection.picked", { count: picked.size })}
              </Text>
              <Button size="xs" loading={opening} onClick={() => void openPicked()}>
                {t("views.browse.selection.open")}
              </Button>
              <Button size="xs" variant="subtle" color="gray" onClick={() => setPicked(new Map())}>
                {t("views.browse.selection.clear")}
              </Button>
            </>
          ) : null}
          <BrowseExport
            label={label}
            where={filter ? [scanFilter(filter, profile.data ?? [])] : []}
            columns={columns}
            rows={rows}
            total={total}
          />
          <ColumnPicker options={names} value={columns} onChange={onColumns} />
        </Group>
      </Group>

      <div className={classes.table}>
        {!ready || page.isLoading ? (
          <Skeleton h={240} />
        ) : (
          <NodeTable
            rows={rows}
            columns={columns}
            onOpen={openRow}
            stale={page.isPlaceholderData}
            selected={new Set(picked.keys())}
            onToggle={toggle}
            onToggleAll={toggleAll}
          />
        )}
      </div>
      {page.error ? (
        <Text size="sm" c="red">
          {page.error.message}
        </Text>
      ) : null}

      <Group justify="flex-end" gap="xs">
        <Button size="xs" variant="default" disabled={pager.page === 0} onClick={() => setPager(previousPage(pager))}>
          {t("common.actions.previous")}
        </Button>
        <Button
          size="xs"
          variant="default"
          disabled={nextCursor === null || page.isPlaceholderData}
          onClick={() => nextCursor && setPager(nextPage(pager, nextCursor))}
        >
          {t("common.actions.next")}
        </Button>
      </Group>
    </>
  );
}
