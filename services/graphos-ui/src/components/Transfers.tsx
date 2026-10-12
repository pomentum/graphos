import { Button, CloseButton, Group, Popover, Progress, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconDownload, IconUpload } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { formatBytes, formatCount } from "../lib/time";
import {
  cancelTransfer,
  dismissTransfer,
  isActive,
  transferShare,
  useTransfers,
  type Transfer,
} from "../lib/transfers";
import { useNow } from "./RunningJobs";
import classes from "./RunningJobs.module.css";

/** "1:05", "12:40": how long a transfer has run, or ran. */
function duration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/**
 * One dataset download or upload: what it is doing, how far it is (rows
 * against the file's header, or bytes against the file's size), for how long,
 * and Cancel while it runs; how it ended once it has.
 */
export function TransferProgress({ transfer, showTenant = false }: { transfer: Transfer; showTenant?: boolean }) {
  const { t } = useTranslation();
  const now = useNow();
  const active = isActive(transfer);
  const share = transferShare(transfer);
  const elapsed = duration((transfer.endedAt ?? now.getTime()) - transfer.startedAt);
  const amount =
    transfer.totalBytes && transfer.kind === "upload" && transfer.phase === "sending"
      ? t("common.transfers.bytesOf", { bytes: formatBytes(transfer.bytes), total: formatBytes(transfer.totalBytes) })
      : formatBytes(transfer.bytes);
  const rows =
    transfer.kind === "download" && transfer.rows
      ? transfer.totalRows
        ? t("common.transfers.rowsOf", {
            rows: formatCount(Math.min(transfer.rows, transfer.totalRows)),
            total: formatCount(transfer.totalRows),
          })
        : t("common.transfers.rows", { rows: formatCount(transfer.rows) })
      : null;
  const state = t(`common.transfers.phase.${transfer.phase}`);
  const detail = [showTenant ? transfer.tenant : null, transfer.filename, amount, rows, elapsed]
    .filter(Boolean)
    .join(" · ");

  return (
    <Stack gap={4} aria-live="polite">
      <Group justify="space-between" wrap="nowrap" gap="xs">
        <Group gap={6} wrap="nowrap" miw={0}>
          {transfer.kind === "download" ? <IconDownload size={14} /> : <IconUpload size={14} />}
          <Text size="xs" fw={600}>
            {state}
            {active && share !== null && transfer.phase !== "writing" ? ` · ${Math.floor(share * 100)}%` : ""}
          </Text>
        </Group>
        {active ? (
          <Button
            size="compact-xs"
            variant="subtle"
            color="red"
            style={{ flex: "none" }}
            onClick={() => cancelTransfer(transfer.id)}
          >
            {t("common.transfers.cancel")}
          </Button>
        ) : (
          <CloseButton
            size="sm"
            aria-label={t("common.transfers.dismiss")}
            onClick={() => dismissTransfer(transfer.id)}
          />
        )}
      </Group>
      <Text size="xs" c="dimmed">
        {detail}
      </Text>
      {active || transfer.phase === "done" ? (
        <Progress
          size="sm"
          radius="xl"
          value={share === null || transfer.phase === "writing" ? 100 : share * 100}
          striped={active && (share === null || transfer.phase === "writing")}
          animated={active && (share === null || transfer.phase === "writing")}
          color={transfer.phase === "done" ? "teal" : undefined}
          aria-label={state}
        />
      ) : null}
      {transfer.error ? (
        <Text size="xs" c="red">
          {transfer.error}
        </Text>
      ) : null}
    </Stack>
  );
}

/** In the toolbar while a dataset download or upload runs, on any page: how many, with each on click. */
export function TransfersIndicator() {
  const { t } = useTranslation();
  const running = useTransfers().filter(isActive);
  if (running.length === 0) return null;
  const label = t("common.transfers.indicator", { count: running.length });
  const share = running.length === 1 ? transferShare(running[0]!) : null;
  return (
    <Popover position="bottom-end" width={360} radius="lg">
      <Popover.Target>
        <UnstyledButton className={classes.pill} aria-label={label} title={label}>
          {running[0]!.kind === "download" ? <IconDownload size={13} /> : <IconUpload size={13} />}
          <Text size="xs" fw={600}>
            {share !== null ? `${Math.round(share * 100)}%` : running.length}
          </Text>
        </UnstyledButton>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="sm">
          <Text size="xs" c="dimmed">
            {label}
          </Text>
          {running.map((transfer) => (
            <TransferProgress key={transfer.id} transfer={transfer} showTenant />
          ))}
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
