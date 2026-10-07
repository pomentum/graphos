import { Alert, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { PathsResponse } from "../../api/types";

/** One sentence about the result: how many paths and how long, or why none. */
export function PathSummary({ result, maxHops }: { result: PathsResponse; maxHops: number }) {
  const { t } = useTranslation();
  if (result.truncated) {
    return (
      <Alert color="orange" variant="light" radius="md">
        {t("views.path.summary.truncated")}
      </Alert>
    );
  }
  if (result.hops === null) {
    return (
      <Text size="sm" c="dimmed">
        {t("views.path.summary.noPath", { count: maxHops })}
      </Text>
    );
  }
  const n = result.paths.length;
  return (
    <Text size="sm">
      {t("views.path.summary.found", {
        paths: t("views.path.summary.paths", { count: n }),
        hops: t("views.path.summary.hops", { count: result.hops }),
      })}
    </Text>
  );
}
