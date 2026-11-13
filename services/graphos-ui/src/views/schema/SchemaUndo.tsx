import { Button, Tooltip } from "@mantine/core";
import { useHotkeys } from "@mantine/hooks";
import { IconArrowBackUp } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { useUndoSchema } from "../../data/queries";
import type { StepLabel } from "../../data/schemaUndo";
import { isApple } from "../../lib/platform";

/**
 * Undo on the Schema page: the button, and Ctrl/⌘+Z anywhere on the page but
 * in a text field, where the field's own undo applies. Its tooltip names what
 * it would undo, or the change that cannot be undone and emptied the history.
 */
export function SchemaUndo() {
  const { t } = useTranslation();
  const { step, blocked, undo, pending } = useUndoSchema();
  // Mantine ignores the keys in inputs, text areas and selects by default.
  useHotkeys([["mod+Z", undo]]);
  const name = (label: StepLabel) => t(`views.schema.undo.steps.${label.key}`, label.params ?? {});
  const tooltip = step
    ? t("views.schema.undo.tooltip", { step: name(step), key: isApple() ? "⌘Z" : "Ctrl+Z" })
    : blocked
      ? t("views.schema.undo.blocked", { step: name(blocked) })
      : t("views.schema.undo.nothing");
  return (
    <Tooltip label={tooltip} withArrow multiline maw={280}>
      <Button
        size="xs"
        variant="default"
        leftSection={<IconArrowBackUp size={14} />}
        disabled={!step}
        loading={pending}
        onClick={undo}
      >
        {t("views.schema.undo.button")}
      </Button>
    </Tooltip>
  );
}
