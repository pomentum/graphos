import { Button, Group, Loader, Menu } from "@mantine/core";
import { IconCheck, IconCirclePlus, IconLink, IconPalette, IconTrashX } from "@tabler/icons-react";
import type { ColorBy } from "../../graph/canvasStyle";
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
  onColorBy: (colorBy: ColorBy) => void;
  onNewNode: () => void;
  onConnect: () => void;
  onClear: () => void;
}

/** The buttons over the canvas: new node, connect, colours, clear. */
export function ExploreActions(props: ExploreActionsProps) {
  const { canWrite, hasSelection, isEmpty, busy, colorBy } = props;
  return (
    <Group gap={6} className={classes.actions}>
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
