import { Group, Select, SimpleGrid, Skeleton, Stack, Text, Title } from "@mantine/core";
import { useState } from "react";
import { CountBars } from "../../components/CountBars";
import { StatTile } from "../../components/StatTile";
import { useHistogram, useProperties, useStats } from "../../data/queries";
import { formatBytes, formatCount } from "../../lib/time";
import classes from "./OverviewView.module.css";

/**
 * Overview: how big the tenant is, which labels and relationships it has,
 * and how the values of one property are spread across a label.
 */
export function OverviewView() {
  const stats = useStats();
  const [label, setLabel] = useState<string | null>(null);
  const [property, setProperty] = useState<string | null>(null);
  const properties = useProperties(label);
  const histogram = useHistogram(label, property);

  const labels = (stats.data?.labels ?? []).filter((l) => l.kind === "v");
  const edges = (stats.data?.labels ?? []).filter((l) => l.kind === "e");
  const toRows = (items: typeof labels) =>
    [...items]
      .sort((a, b) => b.count - a.count)
      .map((l) => ({ name: l.label, count: l.count, detail: formatBytes(l.total_bytes) }));

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
              setLabel(name);
              setProperty(null);
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

      <Stack gap="xs" mt="xl">
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
            onChange={setProperty}
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
