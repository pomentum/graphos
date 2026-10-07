import { Button, Group, Select, Skeleton, Text } from "@mantine/core";
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
  const profile = usePropertyProfile(label);
  const [pager, setPager] = useState(FIRST_PAGE);

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
        <ColumnPicker options={names} value={columns} onChange={onColumns} />
      </Group>

      <div className={classes.table}>
        {!ready || page.isLoading ? (
          <Skeleton h={240} />
        ) : (
          <NodeTable rows={rows} columns={columns} onOpen={openRow} stale={page.isPlaceholderData} />
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
