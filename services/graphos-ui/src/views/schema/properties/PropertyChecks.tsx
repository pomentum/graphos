import { Alert, Badge, Button, Group, Menu, Modal, Stack, Text, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useQueryClient } from "@tanstack/react-query";
import { IconCheck, IconChevronDown, IconListCheck } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { PropertiesCheck, PropertyMode, TenantSchema } from "../../../api/types";
import { isActiveJob, keys, useAddToSchema, usePropertyCheckJob } from "../../../data/queries";
import { formatCount } from "../../../lib/time";
import { useTenantSession } from "../../../session/SessionProvider";
import { PropertyReport } from "./PropertyReport";

const MODES: PropertyMode[] = ["off", "warn", "strict"];
const COLOR: Record<PropertyMode, string> = { off: "gray", warn: "yellow", strict: "red" };

/**
 * How strictly writes are held to the tenant's property schema, and what its
 * stored rows break of it. Maintainers set the mode (`off`, `warn`,
 * `strict`; turning strict on asks first, with the last check's count) and
 * check the stored rows as a background job, followed whoever started it;
 * its report opens from here. Others see the mode while it is not off. While a
 * program manages the schema, the mode cannot be set here (`locked` says who);
 * checking the stored rows writes nothing and stays.
 */
export function PropertyChecks({
  schema,
  editable,
  locked = null,
}: {
  schema: TenantSchema;
  editable: boolean;
  /** Why the mode cannot be set here (a schema manager), or null. */
  locked?: string | null;
}) {
  const { t } = useTranslation();
  const save = useAddToSchema();
  const check = usePropertyCheck();
  const [confirming, setConfirming] = useState(false);
  const [reading, setReading] = useState(false);
  const mode = schema.property_schema?.mode ?? "off";
  const declared = Boolean(
    Object.keys(schema.property_schema?.vertex ?? {}).length || Object.keys(schema.property_schema?.edge ?? {}).length,
  );
  const last = check.report;

  if (!editable) {
    return mode === "off" ? null : (
      <Tooltip label={t(`views.schema.properties.modeHints.${mode}`)} withArrow multiline w={280}>
        <Badge variant="light" radius="sm" tt="none" color={COLOR[mode]}>
          {t("views.schema.properties.checksAre", { mode: t(`views.schema.properties.modes.${mode}`) })}
        </Badge>
      </Tooltip>
    );
  }

  const set = (next: PropertyMode) =>
    save.mutate({ property_schema: { mode: next } }, { onSuccess: () => setConfirming(false) });
  const choose = (next: PropertyMode) => (next === "strict" && mode !== "strict" ? setConfirming(true) : set(next));

  return (
    <>
      <Menu position="bottom-end" width={300}>
        <Menu.Target>
          <Button
            size="xs"
            variant="default"
            leftSection={<IconListCheck size={14} />}
            rightSection={<IconChevronDown size={12} />}
            loading={save.isPending}
          >
            {t("views.schema.properties.checksAre", { mode: t(`views.schema.properties.modes.${mode}`) })}
          </Button>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Label>{t("views.schema.properties.modeLabel")}</Menu.Label>
          {locked ? (
            <Text size="xs" c="dimmed" px="sm" pb={4}>
              {locked}
            </Text>
          ) : null}
          {MODES.map((value) => (
            <Menu.Item
              key={value}
              leftSection={value === mode ? <IconCheck size={14} /> : <span style={{ width: 14 }} />}
              onClick={() => choose(value)}
              disabled={locked !== null || (!declared && value !== "off")}
            >
              <Text size="sm">{t(`views.schema.properties.modes.${value}`)}</Text>
              <Text size="xs" c="dimmed">
                {t(`views.schema.properties.modeHints.${value}`)}
              </Text>
            </Menu.Item>
          ))}
          <Menu.Divider />
          <Menu.Item onClick={() => void check.start()} disabled={check.running || !declared}>
            {check.running ? t("views.schema.properties.checking") : t("views.schema.properties.check")}
          </Menu.Item>
          {last ? (
            <Menu.Item onClick={() => setReading(true)}>
              {t("views.schema.properties.lastCheck", {
                errors: formatCount(last.rows_with_errors),
                rows: formatCount(last.rows),
              })}
            </Menu.Item>
          ) : null}
        </Menu.Dropdown>
      </Menu>
      <Modal
        opened={confirming}
        onClose={() => setConfirming(false)}
        title={t("views.schema.properties.strictTitle")}
        centered
        radius="lg"
      >
        <Stack gap="sm">
          <Text size="sm">{t("views.schema.properties.strictBody")}</Text>
          {last ? (
            <Alert
              variant="light"
              radius="md"
              color={last.rows_with_errors ? "orange" : "green"}
              title={t("views.schema.properties.strictLast", {
                errors: formatCount(last.rows_with_errors),
                rows: formatCount(last.rows),
              })}
            />
          ) : (
            <Text size="xs" c="dimmed">
              {t("views.schema.properties.strictNoCheck")}
            </Text>
          )}
          <Group justify="flex-end" gap="xs">
            <Button size="xs" variant="default" onClick={() => setConfirming(false)}>
              {t("common.actions.cancel")}
            </Button>
            <Button size="xs" color="red" loading={save.isPending} onClick={() => set("strict")}>
              {t("views.schema.properties.strictEnable")}
            </Button>
          </Group>
        </Stack>
      </Modal>
      {reading && last ? <PropertyReport report={last} onClose={() => setReading(false)} /> : null}
    </>
  );
}

/**
 * The stored-rows check as a background job: starts it, follows the
 * tenant's latest one (whoever started it), says when it ends, and gives its
 * report once it succeeded.
 */
function usePropertyCheck(): { start: () => Promise<void>; running: boolean; report: PropertiesCheck | null } {
  const { t } = useTranslation();
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  const job = usePropertyCheckJob();
  const running = isActiveJob(job.data);
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running && job.data) {
      const result = job.data.result as PropertiesCheck | null;
      if (job.data.status === "succeeded" && result) {
        notifications.show({
          message: t("views.schema.properties.checked", {
            errors: formatCount(result.rows_with_errors),
            rows: formatCount(result.rows),
          }),
        });
      } else {
        notifications.show({
          color: "red",
          title: t("views.schema.properties.checkFailed"),
          message: job.data.error?.detail ?? job.data.status,
        });
      }
    }
    wasRunning.current = running;
  }, [running, job.data, t]);

  const start = async () => {
    try {
      await api.checkProperties();
    } catch (error) {
      notifications.show({
        color: "red",
        title: t("views.schema.properties.checkFailed"),
        message: error instanceof Error ? error.message : String(error),
      });
    }
    await client.invalidateQueries({ queryKey: keys.propertyCheckJob(tenant) });
  };
  const report = job.data?.status === "succeeded" ? (job.data.result as PropertiesCheck | null) : null;
  return { start, running, report };
}
