import { Alert, Button, Group, Select, SimpleGrid, Skeleton, Stack, Text, Title } from "@mantine/core";
import { useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { CountBars } from "../../components/CountBars";
import { StatTile } from "../../components/StatTile";
import { useHistogram, useProperties, useStats } from "../../data/queries";
import { formatBytes, formatCount } from "../../lib/time";
import classes from "./OverviewView.module.css";

/**
 * Overview: how big the tenant is, which labels and relationships it has,
 * and how the values of one property are spread across a label. The label
 * and property live in the address, so they survive moving between views.
 */
export function OverviewView() {
  const stats = useStats();
  const [params, setParams] = useSearchParams();
  const label = params.get("label") || null;
  const property = (label && params.get("property")) || null;
  const values = useRef<HTMLDivElement>(null);
  const show = (next: { label: string | null; property: string | null }) => {
    const query = new URLSearchParams();
    if (next.label) query.set("label", next.label);
    if (next.label && next.property) query.set("property", next.property);
    setParams(query, { replace: true });
  };
  const properties = useProperties(label);
  const histogram = useHistogram(label, property);

  const labels = (stats.data?.labels ?? []).filter((l) => l.kind === "v");
  const edges = (stats.data?.labels ?? []).filter((l) => l.kind === "e");
  const toRows = (items: typeof labels) =>
    [...items]
      .sort((a, b) => b.count - a.count)
      .map((l) => ({ name: l.label, count: l.count, detail: formatBytes(l.total_bytes) }));

  if (stats.error) {
    return (
      <div className={classes.root}>
        <Alert color="red" variant="light" radius="md" title="Couldn't load the tenant's counts">
          <Text size="sm">{stats.error.message}</Text>
          <Button
            size="compact-xs"
            variant="default"
            mt="xs"
            loading={stats.isFetching}
            onClick={() => void stats.refetch()}
          >
            Try again
          </Button>
        </Alert>
      </div>
    );
  }

  return (
    <div className={classes.root}>
      {stats.isLoading ? (
        <Skeleton h={80} radius="lg" />
      ) : (
        <SimpleGrid cols={{ base: 1, sm: 3 }}>
          <StatTile label="Nodes" value={formatCount(stats.data?.n_vertices ?? 0)} />
          <StatTile label="Edges" value={formatCount(stats.data?.n_edges ?? 0)} />
          <StatTile label="Storage" value={formatBytes(stats.data?.total_bytes ?? 0)} />
        </SimpleGrid>
      )}

      <SimpleGrid cols={{ base: 1, md: 2 }} mt="xl" spacing="xl">
        <Stack gap="xs">
          <Title order={2} size="sm">
            Labels
          </Title>
          <CountBars
            rows={toRows(labels)}
            onPick={(name) => {
              show({ label: name, property: null });
              // With many labels the values are below the fold: bring them into view.
              requestAnimationFrame(() => values.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
            }}
            active={label}
            empty="No nodes yet"
          />
        </Stack>
        <Stack gap="xs">
          <Title order={2} size="sm">
            Relationships
          </Title>
          <CountBars rows={toRows(edges)} empty="No edges yet" />
        </Stack>
      </SimpleGrid>

      <Stack gap="xs" mt="xl" ref={values}>
        <Title order={2} size="sm">
          Values
        </Title>
        <Group gap="sm" align="flex-end">
          <Text size="sm" c={label ? undefined : "dimmed"}>
            {label ? `${label} ·` : "Pick a label above, then a property"}
          </Text>
          <Select
            size="xs"
            placeholder="Property"
            aria-label="Property"
            data={properties.data ?? []}
            value={property}
            disabled={!label}
            searchable
            selectFirstOptionOnChange
            onChange={(next) => show({ label, property: next })}
          />
        </Group>
        {histogram.data ? (
          <>
            <Text size="xs" c="dimmed">
              {formatCount(histogram.data.n_distinct)} distinct values across {formatCount(histogram.data.n_total)}{" "}
              nodes
            </Text>
            <CountBars rows={histogram.data.bins.map((b) => ({ name: b.value, count: b.count }))} />
          </>
        ) : null}
        {histogram.error ? (
          <Text size="sm" c="red">
            {histogram.error.message}
          </Text>
        ) : null}
      </Stack>
    </div>
  );
}
