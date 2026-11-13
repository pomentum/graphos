import { ActionIcon, CopyButton, Tooltip } from "@mantine/core";
import { IconCheck, IconCopy } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";

/** A small button that copies `value` to the clipboard, and says so. */
export function CopyValue({ value, label, className }: { value: string; label: string; className?: string }) {
  const { t } = useTranslation();
  return (
    <CopyButton value={value} timeout={1500}>
      {({ copied, copy }) => (
        <Tooltip label={copied ? t("components.copy.copied") : label} withArrow>
          <ActionIcon
            variant="subtle"
            color={copied ? "teal" : "gray"}
            size="xs"
            aria-label={label}
            className={className}
            onClick={copy}
          >
            {copied ? <IconCheck size={12} /> : <IconCopy size={12} />}
          </ActionIcon>
        </Tooltip>
      )}
    </CopyButton>
  );
}
