import { Button, Group, Loader, Menu, Tooltip } from "@mantine/core";
import {
  IconArrowBackUp,
  IconCheck,
  IconCirclePlus,
  IconFocus2,
  IconLink,
  IconPalette,
  IconTrashX,
  IconCircles,
} from "@tabler/icons-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { ColorBy } from "../../graph/canvasStyle";
import { SIZE_MODES, type SizeBy } from "../../lib/metrics";
import { isApple } from "../../lib/platform";
import classes from "./ExploreView.module.css";

const COLOR_MODES: ColorBy[] = ["none", "label", "group"];

export interface ExploreActionsProps {
  /** Shown first, e.g. the Map / Graph switch. */
  leading?: ReactNode;
  canWrite: boolean;
  hasSelection: boolean;
  isEmpty: boolean;
  busy: boolean;
  colorBy: ColorBy;
  sizeBy: SizeBy;
  /** The metrics some node on the canvas carries: those can size the nodes. */
  sizeOptions: Set<SizeBy>;
  /** What Back would undo, or null. */
  lastStep: string | null;
  /** Only the selection's neighbourhood is shown. */
  focusOnly: boolean;
  onColorBy: (colorBy: ColorBy) => void;
  onSizeBy: (sizeBy: SizeBy) => void;
  onNewNode: () => void;
  onConnect: () => void;
  onBack: () => void;
  onFocus: () => void;
  onClear: () => void;
}

/** The buttons over the canvas: Back, Focus, new node, connect, colours, size, clear. */
export function ExploreActions(props: ExploreActionsProps) {
  const { t } = useTranslation();
  const { canWrite, hasSelection, isEmpty, busy, colorBy, sizeBy, sizeOptions, lastStep, focusOnly } = props;
  const noMetrics = sizeOptions.size === 0;
  const undoKey = isApple() ? "⌘Z" : "Ctrl+Z";
  return (
    <Group gap={6} className={classes.actions}>
      {props.leading}
      <Tooltip
        label={
          lastStep
            ? t("views.explore.actions.backTooltip", { step: lastStep, key: undoKey })
            : t("views.explore.actions.nothingToUndo")
        }
        withArrow
      >
        <Button
          size="compact-xs"
          variant="default"
          leftSection={<IconArrowBackUp size={14} />}
          disabled={!lastStep}
          onClick={props.onBack}
        >
          {t("views.explore.actions.back")}
        </Button>
      </Tooltip>
      {hasSelection ? (
        <Tooltip label={t("views.explore.actions.focusTooltip")} withArrow>
          <Button
            size="compact-xs"
            variant={focusOnly ? "filled" : "default"}
            leftSection={<IconFocus2 size={14} />}
            onClick={props.onFocus}
            aria-pressed={focusOnly}
          >
            {t("views.explore.actions.focus")}
          </Button>
        </Tooltip>
      ) : null}
      {canWrite ? (
        <Button
          size="compact-xs"
          variant="default"
          leftSection={<IconCirclePlus size={14} />}
          onClick={props.onNewNode}
        >
          {t("views.explore.actions.newNode")}
        </Button>
      ) : null}
      {canWrite && hasSelection ? (
        <Button size="compact-xs" variant="default" leftSection={<IconLink size={14} />} onClick={props.onConnect}>
          {t("views.explore.actions.connect")}
        </Button>
      ) : null}
      {!isEmpty ? (
        <Menu position="bottom-start" width={160}>
          <Menu.Target>
            <Button size="compact-xs" variant="default" leftSection={<IconPalette size={14} />}>
              {t("views.explore.actions.colours")}
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            {COLOR_MODES.map((mode) => (
              <Menu.Item
                key={mode}
                onClick={() => props.onColorBy(mode)}
                rightSection={colorBy === mode ? <IconCheck size={14} /> : null}
              >
                {t(`views.explore.colours.${mode}`)}
              </Menu.Item>
            ))}
          </Menu.Dropdown>
        </Menu>
      ) : null}
      {!isEmpty ? (
        <Menu position="bottom-start" width={220}>
          <Menu.Target>
            <Button size="compact-xs" variant="default" leftSection={<IconCircles size={14} />}>
              {t("views.explore.actions.size")}
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            {SIZE_MODES.map((mode) => (
              <Menu.Item
                key={mode}
                disabled={mode !== "connections" && !sizeOptions.has(mode)}
                onClick={() => props.onSizeBy(mode)}
                rightSection={sizeBy === mode ? <IconCheck size={14} /> : null}
              >
                {t(`views.explore.sizes.${mode}`)}
              </Menu.Item>
            ))}
            {noMetrics ? <Menu.Label>{t("views.explore.sizes.noMetrics")}</Menu.Label> : null}
          </Menu.Dropdown>
        </Menu>
      ) : null}
      {!isEmpty ? (
        <Button
          size="compact-xs"
          variant="subtle"
          color="gray"
          leftSection={<IconTrashX size={14} />}
          onClick={props.onClear}
        >
          {t("views.explore.actions.clear")}
        </Button>
      ) : null}
      {busy ? <Loader size={14} /> : null}
    </Group>
  );
}
