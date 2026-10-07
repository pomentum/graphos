import { Button, Group, Loader, Menu, Tooltip } from "@mantine/core";
import {
  IconArrowBackUp,
  IconCheck,
  IconCirclePlus,
  IconFocus2,
  IconLink,
  IconPalette,
  IconTrashX,
} from "@tabler/icons-react";
import type { ColorBy } from "../../graph/canvasStyle";
import { isApple } from "../../lib/platform";
import classes from "./ExploreView.module.css";

const COLOR_MODES: { value: ColorBy; label: string }[] = [
  { value: "none", label: "No colours" },
  { value: "label", label: "By label" },
  { value: "group", label: "By group" },
];

export interface ExploreActionsProps {
  canWrite: boolean;
  hasSelection: boolean;
  isEmpty: boolean;
  busy: boolean;
  colorBy: ColorBy;
  /** What Back would undo, or null. */
  lastStep: string | null;
  /** Only the selection's neighbourhood is shown. */
  focusOnly: boolean;
  onColorBy: (colorBy: ColorBy) => void;
  onNewNode: () => void;
  onConnect: () => void;
  onBack: () => void;
  onFocus: () => void;
  onClear: () => void;
}

/** The buttons over the canvas: Back, Focus, new node, connect, colours, clear. */
export function ExploreActions(props: ExploreActionsProps) {
  const { canWrite, hasSelection, isEmpty, busy, colorBy, lastStep, focusOnly } = props;
  const undoKey = isApple() ? "⌘Z" : "Ctrl+Z";
  return (
    <Group gap={6} className={classes.actions}>
      <Tooltip label={lastStep ? `Undo “${lastStep}” (${undoKey})` : "Nothing to undo"} withArrow>
        <Button
          size="compact-xs"
          variant="default"
          leftSection={<IconArrowBackUp size={14} />}
          disabled={!lastStep}
          onClick={props.onBack}
        >
          Back
        </Button>
      </Tooltip>
      {hasSelection ? (
        <Tooltip label="Show only the selection and its neighbours (F)" withArrow>
          <Button
            size="compact-xs"
            variant={focusOnly ? "filled" : "default"}
            leftSection={<IconFocus2 size={14} />}
            onClick={props.onFocus}
            aria-pressed={focusOnly}
          >
            Focus
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
          Node
        </Button>
      ) : null}
      {canWrite && hasSelection ? (
        <Button size="compact-xs" variant="default" leftSection={<IconLink size={14} />} onClick={props.onConnect}>
          Connect
        </Button>
      ) : null}
      {!isEmpty ? (
        <Menu position="bottom-start" width={160}>
          <Menu.Target>
            <Button size="compact-xs" variant="default" leftSection={<IconPalette size={14} />}>
              Colours
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            {COLOR_MODES.map((mode) => (
              <Menu.Item
                key={mode.value}
                onClick={() => props.onColorBy(mode.value)}
                rightSection={colorBy === mode.value ? <IconCheck size={14} /> : null}
              >
                {mode.label}
              </Menu.Item>
            ))}
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
          Clear
        </Button>
      ) : null}
      {busy ? <Loader size={14} /> : null}
    </Group>
  );
}
