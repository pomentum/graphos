import { Button, Group, Stack, Text, Title } from "@mantine/core";
import { IconTrash } from "@tabler/icons-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { DeleteTenantDialog } from "../../components/TenantLifecycle";
import { useSessionContext, useTenantSession } from "../../session/SessionProvider";
import classes from "./OverviewView.module.css";

/**
 * For the tenant's owners, at the foot of the Overview: unregister the tenant
 * (its data kept, to restore) or delete it with its data.
 */
export function DeleteTenantSection() {
  const { t } = useTranslation();
  const { tenant, isOwner } = useTenantSession();
  const { refreshMe } = useSessionContext();
  const [open, setOpen] = useState(false);
  if (!isOwner) return null;
  return (
    <div className={classes.section}>
      <Stack gap="xs">
        <Title order={2} size="sm">
          {t("shell.tenantLife.delete.section")}
        </Title>
        <Group justify="space-between" wrap="wrap" gap="sm">
          <Text size="xs" c="dimmed">
            {t("shell.tenantLife.delete.sectionWhat")}
          </Text>
          <Button
            size="xs"
            variant="light"
            color="red"
            leftSection={<IconTrash size={14} />}
            onClick={() => setOpen(true)}
          >
            {t("shell.tenantLife.delete.button")}
          </Button>
        </Group>
      </Stack>
      <DeleteTenantDialog
        name={tenant}
        registered
        opened={open}
        onClose={() => setOpen(false)}
        // Gone from the list: another tenant, or the tenant chooser.
        onDone={() => void refreshMe(null)}
      />
    </div>
  );
}
