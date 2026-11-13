import { Button, Group, Text, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconPlayerPlay } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { useResolveEdges } from "../../data/queries";
import { formatCount } from "../../lib/time";

/**
 * Runs a derived relationship type's resolve rule now, for a writer:
 * graphos adds the edges the rule derives from the data as it is (a program
 * that loads the data usually runs the rules after it), and says how many it
 * wrote. Running it again on unchanged data adds nothing.
 */
export function ResolveRule({ type }: { type: string }) {
  const { t } = useTranslation();
  const resolve = useResolveEdges();
  const run = () =>
    resolve.mutate(type, {
      onSuccess: (result) => {
        const warning = result.results.find((r) => r.warning)?.warning;
        notifications.show({
          color: warning ? "yellow" : "teal",
          title: t("views.schema.resolve.done", { type }),
          message: [
            t("views.schema.resolve.written", {
              count: result.total_edges_written,
              formatted: formatCount(result.total_edges_written),
              seconds: (result.total_duration_ms / 1000).toFixed(1),
            }),
            warning,
          ]
            .filter(Boolean)
            .join(" · "),
        });
      },
    });
  return (
    <Group gap="xs" mb="xs">
      <Tooltip label={t("views.schema.resolve.tooltip")} withArrow multiline w={280}>
        <Button
          size="compact-xs"
          variant="default"
          leftSection={<IconPlayerPlay size={12} />}
          loading={resolve.isPending}
          onClick={run}
        >
          {t("views.schema.resolve.run")}
        </Button>
      </Tooltip>
      {resolve.isPending ? (
        <Text size="xs" c="dimmed">
          {t("views.schema.resolve.running")}
        </Text>
      ) : null}
    </Group>
  );
}
