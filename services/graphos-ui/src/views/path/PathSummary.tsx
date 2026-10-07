import { Alert, Text } from "@mantine/core";
import type { PathsResponse } from "../../api/types";

/** One sentence about the result: how many paths and how long, or why none. */
export function PathSummary({ result, maxHops }: { result: PathsResponse; maxHops: number }) {
  if (result.truncated) {
    return (
      <Alert color="orange" variant="light" radius="md">
        The search reached a very connected part of the graph and stopped. Choose relationships or a direction to narrow
        it.
      </Alert>
    );
  }
  if (result.hops === null) {
    return (
      <Text size="sm" c="dimmed">
        No path within {maxHops} hops.
      </Text>
    );
  }
  const n = result.paths.length;
  return (
    <Text size="sm">
      {n} shortest path{n === 1 ? "" : "s"} of {result.hops} hop{result.hops === 1 ? "" : "s"}
    </Text>
  );
}
